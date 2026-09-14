"""录入管线：PDF → 元数据 → 分类 → 重命名 → 组织 → Zotero → KB 条目 → manifest。"""
import json
import re
import shutil
from datetime import date
from pathlib import Path

from .classifier import Classifier
from .config_loader import Config
from .llm import LLM
from .manifest import Manifest
from .metadata import fetch_arxiv, parse_filename, year_from_arxiv_id
from .zotero_client import ZoteroClient


def _yaml_str(s) -> str:
    """把字符串序列化成 YAML 双引号标量（转义反斜杠与双引号）。"""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


class IngestPipeline:
    def __init__(self, config: Config):
        self.config = config
        self.llm = LLM(config.model)
        self.zotero = ZoteroClient(config.zotero_user_id, config.zotero_api_key)
        self.classifier = Classifier(self.llm, config.taxonomy)
        self.manifest = Manifest(config.root / "knowledge-base" / "_manifest.json")
        self.cache_dir = config.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def run(self, pdf_path: Path, override: dict | None = None) -> dict:
        # 1. 元数据
        fn = parse_filename(pdf_path)
        meta = dict(fn)
        pid = meta.get("arxiv_id") or f"local:{pdf_path.stem}"

        # 幂等：已录入的论文直接返回，避免重复建 Zotero 条目
        if pid in self.manifest.data["entries"]:
            print(f"  [跳过] 已录入: {pid}")
            return self.manifest.get(pid)

        if fn.get("arxiv_id"):
            arxiv = fetch_arxiv(fn["arxiv_id"])
            if arxiv:
                meta.update(arxiv)
        title = meta.get("title") or pdf_path.stem.replace("_", " ")
        abstract = meta.get("abstract", "")

        # 2. 分类（override 时跳过 LLM，直接用给定值）
        if override:
            cls = {"category": override["category"], "area": override["area"],
                   "work_slugs": [override["work"]], "year": override.get("year")}
        else:
            cls = self.classifier.classify(title, abstract)
        area = cls["area"]                       # 形如 "机器学习系统::推理服务"
        work = (cls.get("work_slugs") or ["uncategorized"])[0]
        if override and override.get("year"):
            year = override["year"]
        else:
            year = meta.get("year") or year_from_arxiv_id(meta.get("arxiv_id")) or cls.get("year") or date.today().year

        # 3. 命名 + 组织（复制到 cache/，保留 papers/ 原始）
        filename = self._make_name(cls["category"], area, work, year)
        dest = self.cache_dir / area.split("::")[-1] / work / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf_path, dest)

        # 4. Zotero（条目 + 链接文件 + 标签）
        zot_key = self._zotero_add(meta, title, cls, area, work, year, dest)

        # 5. 双语摘要
        summary = self._summarize(title, abstract, cls)

        # 6. 知识库条目
        self._write_kb(pid, meta, title, cls, area, work, year, filename, zot_key, summary)

        # 7. manifest
        self.manifest.upsert(
            pid,
            title=title, zotero_key=zot_key, path=str(dest),
            area=area, work_slugs=cls.get("work_slugs"), year=year,
            arxiv_id=meta.get("arxiv_id"),
        )

        return {"pid": pid, "title": title, "area": area, "work": work,
                "dest": str(dest), "zotero_key": zot_key}

    def reclassify(self, pdf_path: Path, category: str, area: str, work: str, year: int) -> dict:
        """按给定分类重录一篇已入库论文：删旧条目/文件，再以覆盖值重跑。"""
        fn = parse_filename(pdf_path)
        pid = fn.get("arxiv_id") or f"local:{pdf_path.stem}"
        old = self.manifest.get(pid)
        if old:
            if old.get("zotero_key"):
                self.zotero.delete_item(old["zotero_key"])
            if old.get("path"):
                Path(old["path"]).unlink(missing_ok=True)
                old_area = (old.get("area") or "").split("::")[-1]
                old_work = (old.get("work_slugs") or [""])[0]
                kb_old = (self.config.root / "knowledge-base" / "fields"
                          / old_area / old_work / f"{Path(old['path']).stem}.md")
                kb_old.unlink(missing_ok=True)
        self.manifest.data["entries"].pop(pid, None)
        self.manifest.save()
        return self.run(pdf_path, override={"category": category, "area": area,
                                            "work": work, "year": year})

    def delete(self, pid: str) -> dict:
        """删除一篇已入库论文：Zotero 条目 + 本地缓存 PDF + KB 卡片 + manifest 条目。"""
        old = self.manifest.get(pid)
        if not old:
            raise ValueError(f"未找到已入库论文: {pid}")
        if old.get("zotero_key"):
            self.zotero.delete_item(old["zotero_key"])
        if old.get("path"):
            p = Path(old["path"])
            p.unlink(missing_ok=True)
            area = (old.get("area") or "").split("::")[-1]
            work = (old.get("work_slugs") or [""])[0]
            kb = self.config.root / "knowledge-base" / "fields" / area / work / (p.stem + ".md")
            kb.unlink(missing_ok=True)
        self.manifest.data["entries"].pop(pid, None)
        self.manifest.save()
        return {"pid": pid, "deleted": True}

    def update_metadata(self, pid: str, **fields) -> dict:
        """就地更新一篇已入库论文的元数据（title_en/title_zh/venue/year）。

        改 KB 卡片 frontmatter + manifest；Zotero 标题尽力同步（失败不抛）。
        """
        entry = self.manifest.get(pid)
        if not entry or not entry.get("path"):
            raise ValueError(f"未找到已入库论文: {pid}")
        p = Path(entry["path"])
        area_zh = (entry.get("area") or "").split("::")[-1]
        work = (entry.get("work_slugs") or [""])[0]
        kb = self.config.root / "knowledge-base" / "fields" / area_zh / work / (p.stem + ".md")
        if not kb.exists():
            raise ValueError(f"找不到知识库卡片: {kb}")

        txt = kb.read_text(encoding="utf-8")
        parts = txt.split("---", 2)
        if len(parts) < 3:
            raise ValueError(f"知识库卡片 frontmatter 缺失: {kb}")
        fm = parts[1]
        updated = []
        for key in ("title_en", "title_zh", "venue"):
            if key in fields:
                fm = re.sub(rf"(?m)^{key}:.*$", f"{key}: {_yaml_str(fields[key])}", fm, count=1)
                updated.append(key)
        if "year" in fields:
            fm = re.sub(r"(?m)^year:.*$", f"year: {int(fields['year'])}", fm, count=1)
            updated.append("year")
        kb.write_text(parts[0] + "---" + fm + "---" + parts[2], encoding="utf-8")

        man = {}
        if "title_en" in fields:
            man["title"] = fields["title_en"]
        if "year" in fields:
            man["year"] = int(fields["year"])
        if man:
            self.manifest.upsert(pid, **man)

        if "title_en" in fields and entry.get("zotero_key"):
            self.zotero.update_title(entry["zotero_key"], fields["title_en"])

        return {"pid": pid, "updated": updated}

    def _make_name(self, category, area, work, year):
        area_zh = area.split("::")[-1]
        return f"{category}_{area_zh}_{work}_{year}.pdf"

    def _zotero_add(self, meta, title, cls, area, work, year, dest):
        item = {
            "itemType": "preprint",  # P0 简化；后续按 venue 细化 conferencePaper/journalArticle
            "title": title,
            "date": str(year),
        }
        authors = meta.get("authors")
        if authors:
            item["creators"] = [{"creatorType": "author", "name": a} for a in authors[:10]]
        zot_key = self.zotero.create_item(item)
        if zot_key:
            self.zotero.attach_linked_file(zot_key, dest, dest.name)
            self.zotero.add_tags(zot_key, [cls["category"], area.split("::")[-1], work])
        return zot_key

    def _summarize(self, title, abstract, cls):
        prompt = f"""你是论文解读助手。用中英双语为下面这篇论文写简介，只输出一个 JSON 对象，不要任何其它文字。

论文标题：{title}
摘要：{abstract[:2000] or "（无摘要，请基于你对这篇论文的了解）"}
分类：{cls['category']} / {cls['area']} / {cls.get('work_slugs')}

严格输出如下结构 JSON：
{{"title_zh": "中文标题", "core_zh": "核心内容中文2-3句", "core_en": "core idea in English 2-3 sentences", "sig_zh": "意义中文1-2句", "sig_en": "significance in English 1-2 sentences"}}"""
        raw = self.llm.chat([{"role": "user", "content": prompt}], task="summarize")
        raw = raw.strip().strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        return json.loads(raw.strip())

    def _write_kb(self, pid, meta, title, cls, area, work, year, filename, zot_key, summary):
        area_zh = area.split("::")[-1]
        kb_dir = self.config.root / "knowledge-base" / "fields" / area_zh / work
        kb_dir.mkdir(parents=True, exist_ok=True)
        md = f"""---
id: "{pid}"
zotero_key: "{zot_key}"
title_en: "{title}"
title_zh: "{summary.get('title_zh', title)}"
category: {cls['category']}
area: {area_zh}
work_slugs: {cls.get('work_slugs', [work])}
year: {year}
venue: {meta.get('venue') or 'arXiv'}
arxiv_id: {meta.get('arxiv_id', '')}
pdf_relative: {area_zh}/{work}/{filename}
source: manual
---

## 核心内容（中文）
{summary['core_zh']}

## Core idea (English)
{summary['core_en']}

## 意义（中文）
{summary['sig_zh']}

## Significance (English)
{summary['sig_en']}
"""
        (kb_dir / (Path(filename).stem + ".md")).write_text(md, encoding="utf-8")

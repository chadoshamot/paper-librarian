"""录入管线：PDF → 元数据 → 分类 → 重命名 → 组织 → Zotero → KB 条目 → manifest。"""
import hashlib
import json
import re
import shutil
from pathlib import Path

from .classifier import Classifier
from .config_loader import Config
from .llm import LLM
from .manifest import Manifest
from .metadata import (extract_pdf_metadata_title, fetch_arxiv,
                       is_noncompliant_title, parse_filename, year_from_arxiv_id)
from .pdftext import extract_text
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

        # 2. AI 摘要（关键步骤）：读 PDF 首页真实内容，产出 title_en/title_zh/core/sig。
        #    摘要对论文最关键，必须由大模型读原文生成，不能靠标题脑补/套模板；
        #    后续分类与归档都基于这份摘要。
        pdf_text = extract_text(pdf_path, max_chars=3500)
        summary = self._summarize(title, abstract, pdf_text)
        if is_noncompliant_title(title):
            title = summary.get("title_en") or title

        # 3. 分类（依据摘要结果；override 时跳过 LLM，直接用给定值）
        if override:
            cls = {"category": override["category"], "area": override["area"],
                   "work_slugs": [override["work"]], "year": override.get("year")}
        else:
            cls = self.classifier.classify(
                title, (summary.get("core_zh") or "") + "\n" + (summary.get("core_en") or ""))
        area = cls["area"]                       # 形如 "机器学习系统::推理服务"
        work = (cls.get("work_slugs") or ["uncategorized"])[0]
        if override and override.get("year"):
            year = override["year"]
        else:
            year = meta.get("year") or year_from_arxiv_id(meta.get("arxiv_id")) or cls.get("year")

        # 4. 命名 + 组织（复制到 cache/，保留 papers/ 原始）
        filename = self._make_name(cls["category"], area, work, year, pid)
        dest = self.cache_dir / area.split("::")[-1] / work / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf_path, dest)

        # 5. Zotero（条目 + 链接文件 + 标签）
        zot_key = self._zotero_add(meta, title, cls, area, work, year, dest)

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

    def reclassify(self, pid: str, category: str, area: str, work: str, year: int) -> dict:
        """就地重新归类一篇已入库论文：只改 category/area/work/year，保留标题/摘要/venue 等其余元数据。

        此前用「删旧条目 + 重新 run()」实现，会按原始文件名重新推导标题/摘要，
        把已规范化的标题退回占位名、已重写的摘要退回模板——这里改为就地改 frontmatter。
        """
        old = self.manifest.get(pid)
        if not old or not old.get("path"):
            raise ValueError(f"未找到已入库论文: {pid}")
        old_pdf = Path(old["path"])
        old_area = (old.get("area") or "").split("::")[-1]
        old_work = (old.get("work_slugs") or [""])[0]
        old_kb = (self.config.root / "knowledge-base" / "fields" / old_area / old_work
                  / (old_pdf.stem + ".md"))

        txt = old_kb.read_text(encoding="utf-8") if old_kb.exists() else ""
        parts = txt.split("---", 2)
        if len(parts) < 3:
            raise ValueError(f"知识库卡片 frontmatter 缺失: {old_kb}")
        fm_block, body = parts[1], parts[2]

        area_zh = area.split("::")[-1]
        filename = self._make_name(category, area, work, year, pid)
        dest = self.cache_dir / area_zh / work / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        if old_pdf.exists():
            shutil.move(str(old_pdf), str(dest))

        # 只改分类相关字段；title/venue/arxiv_id/zotero_key/source 原样保留
        fm_block = re.sub(r"(?m)^category:.*$", f"category: {_yaml_str(category)}", fm_block, count=1)
        fm_block = re.sub(r"(?m)^area:.*$", f"area: {_yaml_str(area_zh)}", fm_block, count=1)
        fm_block = re.sub(r"(?m)^work_slugs:.*$", f"work_slugs: ['{work}']", fm_block, count=1)
        fm_block = re.sub(r"(?m)^year:.*$", f"year: {int(year)}", fm_block, count=1)
        fm_block = re.sub(r"(?m)^pdf_relative:.*$",
                          f"pdf_relative: {_yaml_str(f'{area_zh}/{work}/{filename}')}",
                          fm_block, count=1)

        new_kb_dir = self.config.root / "knowledge-base" / "fields" / area_zh / work
        new_kb_dir.mkdir(parents=True, exist_ok=True)
        (new_kb_dir / (filename.stem + ".md")).write_text(
            parts[0] + "---" + fm_block + "---" + body, encoding="utf-8")
        old_kb.unlink(missing_ok=True)

        # Zotero 条目与标题不变，只补新分类标签（best-effort，失败不抛）
        if old.get("zotero_key"):
            try:
                self.zotero.add_tags(old["zotero_key"], [category, area_zh, work])
            except Exception:
                pass

        self.manifest.upsert(pid, path=str(dest), area=area,
                             work_slugs=[work], year=int(year))
        return {"pid": pid, "area": area, "work": work,
                "dest": str(dest), "year": int(year)}

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

    def fix_summary(self, pid: str) -> dict:
        """重新读 PDF 全文，重写一篇论文的中英双语摘要（core/sig/title_zh），就地更新卡片。

        用于修复录入时摘要抓错/套模板的旧条目；摘要由 LLM 读原文生成，不靠标题脑补。
        """
        entry = self.manifest.get(pid)
        if not entry or not entry.get("path"):
            raise ValueError(f"未找到已入库论文: {pid}")
        pdf_path = Path(entry["path"])

        abstract = ""
        arxiv_id = entry.get("arxiv_id")
        if arxiv_id:
            try:
                meta = fetch_arxiv(arxiv_id)
                abstract = (meta or {}).get("abstract") or ""
            except Exception:
                abstract = ""
        pdf_text = extract_text(pdf_path, max_chars=3500)
        if not pdf_text and not abstract:
            raise ValueError(f"{pid} 无 PDF 全文且无摘要，无法重写摘要")

        summary = self._summarize(entry.get("title") or "", abstract, pdf_text)

        area_zh = (entry.get("area") or "").split("::")[-1]
        work = (entry.get("work_slugs") or [""])[0]
        kb = self.config.root / "knowledge-base" / "fields" / area_zh / work / (pdf_path.stem + ".md")
        txt = kb.read_text(encoding="utf-8")
        parts = txt.split("---", 2)
        if len(parts) < 3:
            raise ValueError(f"知识库卡片 frontmatter 缺失: {kb}")
        fm = parts[1]
        if summary.get("title_zh"):
            fm = re.sub(r"(?m)^title_zh:.*$", f"title_zh: {_yaml_str(summary['title_zh'])}", fm, count=1)
        body = (f"\n## 核心内容（中文）\n{summary['core_zh']}\n\n"
                f"## Core idea (English)\n{summary['core_en']}\n\n"
                f"## 意义（中文）\n{summary['sig_zh']}\n\n"
                f"## Significance (English)\n{summary['sig_en']}\n")
        kb.write_text(parts[0] + "---" + fm + "---" + body, encoding="utf-8")
        return {"pid": pid, "title_zh": summary.get("title_zh"),
                "core_zh": summary.get("core_zh")}

    def fix_title(self, pid: str) -> dict:
        """把一篇论文的占位标题（如 arXiv 编号 / 文件名）修正为真实标题。

        解析顺序：arxiv_id → arXiv/Semantic Scholar → PDF /Title 元数据 → LLM 从首页文本识别；
        同步修正 title_en + title_zh（LLM 翻译）+ year，写入 KB/manifest/Zotero（单一事实源）。
        """
        entry = self.manifest.get(pid)
        if not entry or not entry.get("path"):
            raise ValueError(f"未找到已入库论文: {pid}")
        pdf_path = Path(entry["path"])

        title_en = year = None
        arxiv_id = entry.get("arxiv_id")
        if arxiv_id:
            meta = fetch_arxiv(arxiv_id)
            if meta and meta.get("title") and not is_noncompliant_title(meta["title"]):
                title_en = meta["title"]
                year = meta.get("year")

        if not title_en:
            title_en = extract_pdf_metadata_title(pdf_path)

        if not title_en:
            title_en = self._extract_title_llm(extract_text(pdf_path, max_chars=1500))

        if not title_en or is_noncompliant_title(title_en):
            raise ValueError(
                f"未能解析到 {pid} 的真实标题（无 arXiv 元数据，且 PDF 元数据/首页无法识别）。"
                "可改用 edit_paper 手动指定 title_en。")

        title_en = re.sub(r"\s+", " ", title_en).strip()
        fields = {"title_en": title_en, "title_zh": self._translate_title(title_en)}
        if year:
            fields["year"] = int(year)
        result = self.update_metadata(pid, **fields)
        result["title_en"] = title_en
        result["title_zh"] = fields["title_zh"]
        return result

    def _translate_title(self, title_en: str) -> str:
        """英文标题 → 中文标题（LLM）；失败回退英文。"""
        prompt = ("把下面这篇论文的英文标题翻译成简洁准确的中文标题。"
                  "只输出中文标题本身，不要引号、不要解释：\n\n" + title_en)
        try:
            zh = (self.llm.chat([{"role": "user", "content": prompt}],
                                task="summarize") or "").strip().strip('"').strip()
            return zh or title_en
        except Exception:
            return title_en

    def _extract_title_llm(self, first_page_text: str) -> str | None:
        """用 LLM 从 PDF 首页文本识别论文真实标题；无法确定返回 None。"""
        text = (first_page_text or "").strip()
        if not text:
            return None
        prompt = ("下面是一篇论文 PDF 首页文本的开头。请识别这篇论文的真实英文标题，"
                  "只输出标题本身，不要引号、不要解释；如果无法确定，只输出 EMPTY。\n\n"
                  + text[:1500])
        try:
            out = (self.llm.chat([{"role": "user", "content": prompt}],
                                 task="summarize") or "").strip().strip('"').strip()
        except Exception:
            return None
        if not out or out.upper() == "EMPTY" or is_noncompliant_title(out):
            return None
        return out

    def _make_name(self, category, area, work, year, pid):
        """文件名：分类_领域_方向_年份_短哈希。

        短哈希取 pid 的前 8 位 SHA-1，保证同领域/方向/年份下不同论文不重名、
        不互相覆盖（此前仅靠年份区分，年份未知时回退到今天会导致同名覆盖，
        进而使 pdf_relative 指向错误的 PDF —— deep_read 读错论文的根因）。
        """
        area_zh = area.split("::")[-1]
        tag = hashlib.sha1(str(pid).encode("utf-8")).hexdigest()[:8]
        return f"{category}_{area_zh}_{work}_{year or 0}_{tag}.pdf"

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

    def _summarize(self, title, abstract, pdf_text=""):
        """据论文真实内容（arXiv 摘要 + PDF 首页全文）产出中英双语简介，含真实英文标题。

        摘要对论文最关键，必须由大模型读原文生成——abstract 或 pdf_text 至少其一，
        二者都缺时明确告知而非脑补，避免套模板/张冠李戴。
        """
        ground = (abstract or "").strip()[:2000]
        if pdf_text:
            snip = pdf_text.strip()[:3500]
            ground = (ground + "\n\nPDF 全文开头：\n" + snip) if ground else ("PDF 全文开头：\n" + snip)
        if not ground:
            ground = "（无可用文本）"
        prompt = f"""你是论文解读助手。请先仔细阅读下面这篇论文的**真实内容**（标题 + 摘要/PDF 全文开头），据此产出准确的中英双语简介。只输出一个 JSON 对象，不要任何其它文字。

论文标题（可能抓取错误，以正文为准）：{title}
论文内容：
{ground}

严格输出如下结构 JSON：
{{"title_en": "真实英文标题（若上面标题是占位名/编号或错误，据正文纠正；否则原样保留）", "title_zh": "中文标题", "core_zh": "核心内容中文2-3句（必须基于正文，禁止模板套话）", "core_en": "core idea in English 2-3 sentences", "sig_zh": "意义中文1-2句", "sig_en": "significance in English 1-2 sentences"}}"""
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
title_en: {_yaml_str(title)}
title_zh: {_yaml_str(summary.get('title_zh') or title)}
category: {_yaml_str(cls['category'])}
area: {_yaml_str(area_zh)}
work_slugs: {cls.get('work_slugs', [work])}
year: {year if year is not None else 'null'}
venue: {_yaml_str(meta.get('venue') or 'arXiv')}
arxiv_id: {_yaml_str(meta.get('arxiv_id') or '')}
pdf_relative: {_yaml_str(f'{area_zh}/{work}/{filename}')}
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

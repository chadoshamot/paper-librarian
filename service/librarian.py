"""馆长 agent：本地论文库的问答 agent（DeepSeek function-calling + 检索 grounding + 本地记忆）。

与联网对话（chat.py / Kimi web-search）互补，定位不同：
- 知识库问答 → 本模块（DeepSeek），知识来自本地论文库，靠工具按需查库；
- 联网对话   → chat.py（Kimi），知识来自全网检索。

模型复用 .env 的 DEEPSEEK_API_KEY + config 的 model 段（task=librarian，可单独换模型）。
记忆存本地 {root}/agent-memory.md（不入库、不上云），agent 用 remember() 工具自主维护。

用法：
    from .librarian import Librarian
    lib = Librarian(config)
    lib.ask("库里关于 GPU 调度有哪些工作？", history)
        -> {"answer": str, "citations": [{"pid", "title_en"}]}
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from .config_loader import Config
from .llm import LLM
from .pdftext import extract_text
from .retrieval import explore, hybrid_search, keyword_search, load_documents, resolve_targets

MAX_ROUNDS = 6  # 工具调用轮数上限，防死循环

_PENDING: dict[str, dict] = {}  # action_id -> {"action", "params", "summary"}（危险操作待确认）

_FALLBACK_PROMPT = ("你是「论文管家」的馆长 agent，负责回答用户关于其个人论文库的一切问题，"
                    "用中文简洁作答，引用论文时用反引号写 pid。")


def _load_manual(config: Config) -> str:
    """加载 AGENT.md 作为馆长操作手册（身份/任务/要求/规范）；缺失时退回内置兜底提示。"""
    p = config.root / "AGENT.md"
    return p.read_text(encoding="utf-8") if p.exists() else _FALLBACK_PROMPT


def confirm(action_id: str, approve: bool) -> dict:
    """执行/取消一条已排定的危险操作（由 Web 确认按钮触发）。"""
    pending = _PENDING.pop(action_id, None)
    if not pending:
        return {"error": f"未知或已过期的待确认操作: {action_id}"}
    if not approve:
        return {"cancelled": True, "action": pending["action"],
                "summary": pending["summary"]}
    if pending["action"] == "delete":
        from .pipeline import IngestPipeline
        result = IngestPipeline(Config()).delete(pending["params"]["pid"])
        try:  # 删除后同步 ModelScope（无 token 时忽略，不影响本地删除）
            from .cloud import sync
            sync()
        except BaseException:
            pass
        return {"executed": True, "action": "delete", **result}
    if pending["action"] == "push":
        from .cloud import sync
        sync()
        return {"executed": True, "action": "push"}
    return {"error": f"未知动作: {pending['action']}"}


def _fetch_url_pdf(url: str, inbox_dir) -> Path:
    """直链下载 PDF 到 papers/（urllib，3 次重试），返回本地路径。"""
    inbox = Path(inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    name = urlparse(url).path.rsplit("/", 1)[-1] or "download.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    name = re.sub(r'[^\w.\-]', '_', name)
    dest = inbox / name
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            if not data or len(data) < 1000:
                raise RuntimeError("内容过短（可能非 PDF 或被反爬）")
            dest.write_bytes(data)
            return dest
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"下载失败：{url}（{type(last).__name__}）")


TOOLS = [
    {"type": "function", "function": {
        "name": "search_library",
        "description": "在论文库中检索相关论文。mode=search 为语义+关键词混合检索；mode=explore 为探索发现（跨 work 去重、子方向分散）。用于用户想找某主题/问题的论文。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索词，中英文均可"},
            "top_k": {"type": "integer", "description": "返回条数，默认 5"},
            "mode": {"type": "string", "description": "search 或 explore，默认 search"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "get_paper",
        "description": "获取单篇论文的完整信息（核心内容/意义/分类）。用于深入讲解某篇论文。",
        "parameters": {"type": "object", "properties": {
            "pid": {"type": "string", "description": "论文 pid（如 local:xxx 或 arxiv id）"},
        }, "required": ["pid"]},
    }},
    {"type": "function", "function": {
        "name": "library_stats",
        "description": "统计论文库：总数、已读/未读、按方向与分类的分布。用于用户问库的整体情况。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "list_by_area",
        "description": "列出某个方向下的论文，或不带参数时列出所有方向及论文数。",
        "parameters": {"type": "object", "properties": {
            "area": {"type": "string", "description": "方向名（可留空以列出所有方向）"},
        }},
    }},
    {"type": "function", "function": {
        "name": "compare_papers",
        "description": "并排获取多篇论文的信息，用于对比。",
        "parameters": {"type": "object", "properties": {
            "pids": {"type": "array", "items": {"type": "string"}, "description": "要对比的 pid 列表"},
        }, "required": ["pids"]},
    }},
    {"type": "function", "function": {
        "name": "remember",
        "description": "把一条值得长期记住的持久事实写进记忆（如用户的兴趣偏好、重点论文、纠错结论）。",
        "parameters": {"type": "object", "properties": {
            "fact": {"type": "string", "description": "要记住的事实，一句话"},
        }, "required": ["fact"]},
    }},
    {"type": "function", "function": {
        "name": "deep_read",
        "description": "读取某篇论文的 PDF 全文并用 LLM 做深度分析（问题/方法/贡献/结论/与用户研究的关联），结果写入该论文的深读笔记。用于用户要深入理解一篇论文时。",
        "parameters": {"type": "object", "properties": {
            "pid": {"type": "string", "description": "论文 pid"},
        }, "required": ["pid"]},
    }},
    {"type": "function", "function": {
        "name": "update_paper_note",
        "description": "修正或补充你对某篇论文的理解，带日期追加写进该论文的深读笔记。用于对话中产生新洞察时。",
        "parameters": {"type": "object", "properties": {
            "pid": {"type": "string", "description": "论文 pid"},
            "note": {"type": "string", "description": "要追加的洞察/修正，一句话或一小段"},
        }, "required": ["pid", "note"]},
    }},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "全网搜索最新论文与资料（由 Kimi 执行检索，结果回传给你决策）。当用户要求联网搜索、查库外论文/最新进展/未知信息时调用。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索词，越具体越好"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "list_pdfs",
        "description": "列出 papers/ 收件箱里的 PDF 及其是否已录入，便于判断哪些还没入库。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "ingest_paper",
        "description": "把 papers/ 收件箱里的一篇 PDF 录入论文库（元数据/分类/Zotero/双语卡片）。",
        "parameters": {"type": "object", "properties": {
            "filename": {"type": "string", "description": "papers/ 下的文件名，如 arXiv2301.12345.pdf"},
        }, "required": ["filename"]},
    }},
    {"type": "function", "function": {
        "name": "download_paper",
        "description": "下载一篇论文 PDF 到本地并自动完整录入（arxiv id/URL、DOI 或直链 PDF 均可）。",
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "description": "arxiv id（2301.12345）、arxiv abs/pdf 链接、DOI（10.…/… 或 doi.org 链接）、或直链 PDF"},
        }, "required": ["source"]},
    }},
    {"type": "function", "function": {
        "name": "reclassify_paper",
        "description": "把一篇已入库论文重新归类（删除旧条目后按新分类重录）。",
        "parameters": {"type": "object", "properties": {
            "pid": {"type": "string", "description": "论文 pid"},
            "category": {"type": "string", "description": "分类（如 方法 / 系统 / 综述）"},
            "area": {"type": "string", "description": "方向，形如 机器学习系统::推理服务"},
            "work": {"type": "string", "description": "work slug，如 slo-aware-scheduling"},
            "year": {"type": "integer", "description": "年份"},
        }, "required": ["pid", "category", "area", "work", "year"]},
    }},
    {"type": "function", "function": {
        "name": "mark_read",
        "description": "标记一篇论文已读/未读，并同步到 ModelScope。",
        "parameters": {"type": "object", "properties": {
            "pid": {"type": "string", "description": "论文 pid"},
            "read": {"type": "boolean", "description": "true=已读，false=未读"},
        }, "required": ["pid", "read"]},
    }},
    {"type": "function", "function": {
        "name": "pull_library",
        "description": "从 ModelScope 拉取最新的 PDF 与知识库到本地（只读，不会修改云端）。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "push_to_cloud",
        "description": "把本地论文库推送到 ModelScope 公开仓库。外发到公开云的操作：调用后只会生成计划并等待用户确认，绝不立即执行。",
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string", "description": "本次提交说明，可留空"},
        }},
    }},
    {"type": "function", "function": {
        "name": "delete_paper",
        "description": "删除一篇论文（Zotero 条目 + 本地 PDF + 知识库卡片 + 清单条目）。不可逆操作：调用后只会生成计划并等待用户确认，绝不立即执行。",
        "parameters": {"type": "object", "properties": {
            "pid": {"type": "string", "description": "论文 pid"},
        }, "required": ["pid"]},
    }},
    {"type": "function", "function": {
        "name": "run_daily_retrieval",
        "description": "跑一次每日检索：多源抓取 + 质量评分 + LLM 裁判，输出日报到 reports/daily/。",
        "parameters": {"type": "object", "properties": {
            "top_k": {"type": "integer", "description": "返回候选数，默认取配置值"},
            "sources": {"type": "array", "items": {"type": "string"}, "description": "数据源列表（openalex/arxiv/semantic_scholar），可留空用配置"},
        }},
    }},
    {"type": "function", "function": {
        "name": "edit_paper",
        "description": "修改一篇已入库论文的元数据（英文/中文标题、venue、年份）。改分类请用 reclassify_paper。",
        "parameters": {"type": "object", "properties": {
            "pid": {"type": "string", "description": "论文 pid"},
            "title_en": {"type": "string", "description": "英文标题（要改才传）"},
            "title_zh": {"type": "string", "description": "中文标题（要改才传）"},
            "venue": {"type": "string", "description": "发表 venue/会议（要改才传）"},
            "year": {"type": "integer", "description": "年份（要改才传）"},
        }, "required": ["pid"]},
    }},
    {"type": "function", "function": {
        "name": "fix_metadata",
        "description": "重新从 arXiv/Semantic Scholar 抓取一篇论文的元数据并修正标题/年份。用于录入时标题抓错（如显示成 arXiv 编号）的情况。",
        "parameters": {"type": "object", "properties": {
            "pid": {"type": "string", "description": "论文 pid"},
        }, "required": ["pid"]},
    }},
    {"type": "function", "function": {
        "name": "send_daily_email",
        "description": "把最近一次每日检索的候选论文发邮件给收件人（默认用 .env 的 SMTP_TO）。",
        "parameters": {"type": "object", "properties": {
            "recipient": {"type": "string", "description": "收件邮箱，留空用配置默认"},
        }},
    }},
    {"type": "function", "function": {
        "name": "read_daily_report",
        "description": "读取最近一份（或指定日期）每日检索报告。",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD，留空取最近一份"},
        }},
    }},
]


def _serialize(d: dict, full: bool = False) -> dict:
    """把文档压成工具可读的最小字段，控制 token。"""
    out = {
        "pid": d["pid"],
        "title_en": d.get("title_en") or "",
        "title_zh": d.get("title_zh") or "",
        "year": d.get("year") or "",
        "venue": d.get("venue") or "",
        "area": d.get("area") or "",
        "work": d.get("work") or "",
    }
    if full:
        out.update({
            "category": d.get("category") or "",
            "read": bool(d.get("read")),
            "core_zh": d.get("core_zh") or "",
            "core_en": d.get("core_en") or "",
            "sig_zh": d.get("sig_zh") or "",
            "sig_en": d.get("sig_en") or "",
        })
    return out


class Librarian:
    def __init__(self, config: Config):
        self.cfg = config
        self.llm = LLM(config.model)
        self.model = (self.llm.tasks or {}).get("librarian") or self.llm.default
        self.memory_path = config.root / "agent-memory.md"
        self.notes_dir = config.root / "agent-notes"
        self._cited: dict[str, str] = {}  # pid -> title_en（本轮工具实际引用的论文）

    # ── 记忆 ──────────────────────────────────────────────
    def _read_memory(self) -> str:
        if not self.memory_path.exists():
            return ""
        return self.memory_path.read_text(encoding="utf-8")

    def _memory_snippet(self) -> str:
        """把记忆里非空的 section 内容压成一段，注入系统提示。"""
        txt = self._read_memory()
        if not txt:
            return ""
        lines, cur = [], None
        for ln in txt.splitlines():
            if ln.startswith("## "):
                cur = ln[3:].strip()
                continue
            s = ln.strip()
            if cur and s and not s.startswith("#") and not s.startswith(">"):
                lines.append(f"[{cur}] {s}")
        return "\n".join(lines) if lines else ""

    def _remember(self, fact: str) -> str:
        p = self.memory_path
        if not p.exists():
            p.write_text(
                "# 馆长记忆（本地 · 不入库不上云）\n\n"
                "> 这是馆长的长期记忆，通过 remember() 自主维护。\n\n"
                "## 用户画像\n\n## 重点论文\n\n## 纠错记录\n\n## 笔记\n\n",
                encoding="utf-8",
            )
        p.write_text(
            p.read_text(encoding="utf-8").rstrip()
            + f"\n- {fact.strip()}  ({date.today().isoformat()})\n",
            encoding="utf-8",
        )
        return f"已记住：{fact}"

    # ── 单篇深读笔记 ──────────────────────────────────────
    def _note_path(self, pid: str) -> Path:
        safe = pid.replace(":", "_").replace("/", "_").replace("\\", "_")
        return self.notes_dir / f"{safe}.md"

    def _note(self, pid: str) -> str:
        p = self._note_path(pid)
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def _extract_section(self, p: Path, marker: str) -> str:
        if not p.exists():
            return ""
        txt = p.read_text(encoding="utf-8")
        return txt.split(marker, 1)[1].strip() if marker in txt else ""

    def _notes_inventory(self) -> list:
        if not self.notes_dir.exists():
            return []
        pids = []
        for p in sorted(self.notes_dir.glob("*.md")):
            try:
                first = p.read_text(encoding="utf-8").splitlines()[0]
            except Exception:
                continue
            pids.append(first[2:].strip() if first.startswith("# ") else p.stem)
        return pids

    def _set_deep_analysis(self, pid: str, analysis: str) -> None:
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        p = self._note_path(pid)
        insights = self._extract_section(p, "## 洞察")
        p.write_text(
            f"# {pid}\n\n## 深读分析\n\n{analysis.strip()}\n\n## 洞察\n\n{insights}".rstrip() + "\n",
            encoding="utf-8",
        )

    def _append_insight(self, pid: str, note: str) -> None:
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        p = self._note_path(pid)
        if not p.exists():
            p.write_text(f"# {pid}\n\n## 深读分析\n\n## 洞察\n\n", encoding="utf-8")
        p.write_text(
            p.read_text(encoding="utf-8").rstrip()
            + f"\n- {note.strip()}  ({date.today().isoformat()})\n",
            encoding="utf-8",
        )

    # ── 工具实现 ──────────────────────────────────────────
    def _search(self, query, top_k=5, mode="search"):
        docs = load_documents(self.cfg)
        try:
            if mode == "explore":
                results = explore(docs, query, int(top_k))
            else:
                results = hybrid_search(docs, query, int(top_k), self.cfg)
        except Exception:
            results = keyword_search(docs, query, int(top_k))
        for d in results:
            self._cited[d["pid"]] = d.get("title_en") or ""
        return {"count": len(results), "results": [_serialize(d) for d in results]}

    def _get(self, pid):
        doc = next((d for d in load_documents(self.cfg) if d["pid"] == pid), None)
        if not doc:
            return {"error": f"库里没有 pid={pid}"}
        self._cited[doc["pid"]] = doc.get("title_en") or ""
        out = _serialize(doc, full=True)
        out["targets"] = [{"kind": "local", "loc": "本地 PDF（deep_read 可读全文）"}
                          if kind == "local" else {"kind": kind, "loc": loc}
                          for kind, loc in resolve_targets(doc, self.cfg)]
        note = self._note(pid)
        if note:
            out["note"] = note
        return out

    def _stats(self):
        docs = load_documents(self.cfg)
        by_area, by_cat = {}, {}
        read = sum(1 for d in docs if d.get("read"))
        for d in docs:
            a = d.get("area") or "未分类"
            c = d.get("category") or "未分类"
            by_area[a] = by_area.get(a, 0) + 1
            by_cat[c] = by_cat.get(c, 0) + 1
        return {"total": len(docs), "read": read, "unread": len(docs) - read,
                "by_area": dict(sorted(by_area.items(), key=lambda x: -x[1])),
                "by_category": by_cat}

    def _list_area(self, area=None):
        docs = load_documents(self.cfg)
        if area:
            docs = [d for d in docs if area in (d.get("area") or "")]
            for d in docs:
                self._cited[d["pid"]] = d.get("title_en") or ""
            return {"area": area, "count": len(docs),
                    "papers": [_serialize(d) for d in docs]}
        by_area: dict[str, list] = {}
        for d in docs:
            by_area.setdefault(d.get("area") or "未分类", []).append(d)
        return {"areas": [{"area": a, "count": len(v)}
                          for a, v in sorted(by_area.items(), key=lambda x: -len(x[1]))]}

    def _compare(self, pids):
        docs = {d["pid"]: d for d in load_documents(self.cfg)}
        out = []
        for pid in pids:
            d = docs.get(pid)
            if d:
                self._cited[pid] = d.get("title_en") or ""
                item = _serialize(d, full=True)
                note = self._note(pid)
                if note:
                    item["note"] = note
                out.append(item)
        return {"count": len(out), "papers": out}

    def _deep_read(self, pid):
        doc = next((d for d in load_documents(self.cfg) if d["pid"] == pid), None)
        if not doc:
            return {"error": f"库里没有 pid={pid}"}
        self._cited[pid] = doc.get("title_en") or ""
        note = self._note(pid)
        if note:
            return {"pid": pid, "title_en": doc.get("title_en") or "",
                    "cached": True, "analysis": note}
        pdf = None
        for kind, loc in resolve_targets(doc, self.cfg):
            if kind == "local" and loc:
                pdf = loc
                break
        text = extract_text(pdf) if pdf else ""
        if not text:
            return {"pid": pid, "title_en": doc.get("title_en") or "",
                    "error": "本地无 PDF 全文，无法深读（可改用 get_paper 看摘要级信息）"}
        analysis = self._analyze_paper(doc, text)
        self._set_deep_analysis(pid, analysis)
        return {"pid": pid, "title_en": doc.get("title_en") or "",
                "cached": False, "analysis": analysis}

    def _analyze_paper(self, doc, text):
        title = doc.get("title_en") or ""
        prompt = f"""你是论文深度解读助手。基于下面这篇论文的**全文**，写一份结构化深度分析（中文，markdown）。

论文标题：{title}
全文（截断）：\n{text}

严格按以下结构输出，不要输出正文以外的话：

**要解决的问题**：2-3 句。

**核心方法**：关键思路/机制/系统设计，4-6 句。

**关键贡献**：列出 2-4 点。

**实验与结论**：主要结果与结论，2-4 句。

**与用户研究的关联**：用户方向是 MLSys（GPU 调度 / SLO 推理服务 / 共置 / 抢占），点出这篇对他的研究有何可借鉴之处，2-3 句。"""
        return self.llm.chat([{"role": "user", "content": prompt}], task="librarian")

    def _update_note(self, pid, note):
        doc = next((d for d in load_documents(self.cfg) if d["pid"] == pid), None)
        if not doc:
            return {"error": f"库里没有 pid={pid}"}
        self._append_insight(pid, note)
        return {"ok": True, "pid": pid, "note": f"已记录：{note}"}

    # ── 维护 / 联网 / 下载 ─────────────────────────────────
    def _web_search(self, query):
        try:
            from .chat import ChatClient
            cc = ChatClient(self.cfg.config.get("chat") or {})
        except Exception as e:
            return {"error": f"联网检索不可用（缺 MOONSHOT_API_KEY？）: {e}"}
        q = (query or "").strip() + "\n（请把关键结论的来源以链接/URL 形式列出）"
        try:
            return cc.chat([{"role": "user", "content": q}])
        except Exception as e:
            return {"error": f"联网检索失败: {type(e).__name__}: {e}"}

    def _list_pdfs(self):
        from .manifest import Manifest
        inbox = self.cfg.inbox_dir
        if not inbox.exists():
            return {"count": 0, "files": []}
        manifest = Manifest(self.cfg.root / "knowledge-base" / "_manifest.json")
        entries = manifest.data["entries"]
        files = []
        for f in sorted(inbox.glob("*.pdf")):
            pid = f.name[5:-4] if (f.name.startswith("arXiv") and f.name.endswith(".pdf")) \
                else f"local:{f.stem}"
            files.append({"filename": f.name, "pid": pid,
                          "ingested": pid in entries,
                          "size_kb": round(f.stat().st_size / 1024)})
        return {"count": len(files), "files": files}

    def _ingest(self, filename):
        from .pipeline import IngestPipeline
        p = self.cfg.inbox_dir / filename
        if not p.exists():
            return {"error": f"papers/ 下没有 {filename}（可先用 list_pdfs 查看）"}
        return IngestPipeline(self.cfg).run(p)

    def _download(self, source):
        from . import daily
        from .pipeline import IngestPipeline
        s = (source or "").strip()
        if not s:
            return {"error": "缺少下载来源（arxiv id / DOI / 直链）"}
        cand = self._normalize_source(s)
        if isinstance(cand, dict) and cand.get("error"):
            return cand
        if cand.get("url"):
            pdf = _fetch_url_pdf(cand["url"], self.cfg.inbox_dir)
        else:
            pdf = daily.fetch_candidate_pdf(cand, self.cfg.inbox_dir)
        return IngestPipeline(self.cfg).run(pdf)

    @staticmethod
    def _normalize_source(s: str) -> dict:
        m = re.search(r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?', s)
        if m:
            return {"arxiv_id": m.group(1)}
        if re.fullmatch(r'\d{4}\.\d{4,5}(?:v\d+)?', s):
            return {"arxiv_id": s.split("v")[0]}
        m = re.search(r'(10\.\d{4,9}/[^\s]+)', s)
        if m:
            return {"doi": m.group(1).rstrip('.,;)]')}
        if s.lower().startswith(("http://", "https://")):
            return {"url": s}
        return {"error": f"无法识别的来源：{s}（需 arxiv id/链接、DOI 或直链 PDF）"}

    def _find_source_pdf(self, pid):
        papers = self.cfg.inbox_dir
        if pid.startswith("local:"):
            p = papers / (pid[len("local:"):] + ".pdf")
            return p if p.exists() else None
        for f in papers.glob("*.pdf"):
            if pid in f.name:
                return f
        return None

    def _reclassify(self, pid, category, area, work, year):
        from .pipeline import IngestPipeline
        src = self._find_source_pdf(pid)
        if not src:
            return {"error": f"找不到 {pid} 的原始 PDF（papers/ 目录缺失，无法重分类）"}
        return IngestPipeline(self.cfg).reclassify(src, category, area, work, int(year))

    def _mark_read(self, pid, read):
        from .read_state import mark, sync_read
        doc = next((d for d in load_documents(self.cfg) if d["pid"] == pid), None)
        if not doc:
            return {"error": f"库里没有 pid={pid}"}
        mark(self.cfg, pid, bool(read))
        sync_read(self.cfg)
        return {"pid": pid, "read": bool(read)}

    def _pull(self):
        if not os.environ.get("MODELSCOPE_TOKEN"):
            return {"error": "未配置 MODELSCOPE_TOKEN，无法同步 ModelScope"}
        from .cloud import pull
        try:
            pull()
            return {"ok": True, "note": "已从 ModelScope 拉取最新（PDF + 知识库）"}
        except Exception as e:
            return {"error": f"拉取失败: {type(e).__name__}: {e}"}

    def _push(self, message=""):
        if not os.environ.get("MODELSCOPE_TOKEN"):
            return {"error": "未配置 MODELSCOPE_TOKEN，无法推送到 ModelScope"}
        aid = uuid.uuid4().hex[:8]
        summary = ("将本地论文库推送到 ModelScope 公开仓库：commit + push cache/（PDF, git-lfs）"
                   "与 knowledge-base/（Markdown）两个数据集"
                   + (f"，说明：{message}" if message else "，同步全部变更") + "。")
        _PENDING[aid] = {"action": "push", "params": {"message": message}, "summary": summary}
        return {"requires_confirmation": True, "action_id": aid,
                "action": "push", "summary": summary}

    def _delete(self, pid):
        from .pipeline import IngestPipeline
        old = IngestPipeline(self.cfg).manifest.get(pid)
        if not old:
            return {"error": f"库里没有 pid={pid}"}
        aid = uuid.uuid4().hex[:8]
        summary = (f"删除论文 `{pid}`（{old.get('title') or ''}）：将删除 Zotero 条目、"
                   f"本地缓存 PDF、知识库卡片与清单条目，并同步到 ModelScope。")
        _PENDING[aid] = {"action": "delete", "params": {"pid": pid}, "summary": summary}
        return {"requires_confirmation": True, "action_id": aid,
                "action": "delete", "summary": summary}

    def _daily_retrieval(self, top_k=None, sources=None):
        from . import daily
        dcfg = self.cfg.config["daily"]
        srcs = list(sources or dcfg.get("sources", []))
        top = int(top_k or dcfg.get("top_k", 10))
        cands = daily.run(self.cfg, srcs, top, use_llm=True)
        out_dir = self.cfg.root / "reports" / "daily"
        out_dir.mkdir(parents=True, exist_ok=True)
        today = date.today().isoformat()
        (out_dir / f"{today}.md").write_text(daily.render_md(cands, self.cfg), encoding="utf-8")
        (out_dir / f"{today}.json").write_text(
            json.dumps(cands, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        top_pick = [{"title": c.get("title"), "arxiv_id": c.get("arxiv_id"),
                     "doi": c.get("doi"), "score": c.get("score")} for c in cands[:5]]
        return {"date": today, "count": len(cands), "top": top_pick}

    def _edit_paper(self, pid, **fields):
        from .pipeline import IngestPipeline
        clean = {k: v for k, v in fields.items() if v not in (None, "")}
        if not clean:
            return {"error": "没有要修改的字段（title_en/title_zh/venue/year 至少传一个）"}
        try:
            return IngestPipeline(self.cfg).update_metadata(pid, **clean)
        except Exception as e:
            return {"error": f"修改失败: {type(e).__name__}: {e}"}

    def _fix_metadata(self, pid):
        from .metadata import fetch_arxiv
        from .pipeline import IngestPipeline
        pipe = IngestPipeline(self.cfg)
        entry = pipe.manifest.get(pid)
        if not entry:
            return {"error": f"库里没有 pid={pid}"}
        arxiv_id = entry.get("arxiv_id")
        if not arxiv_id:
            return {"error": f"{pid} 没有 arxiv_id，无法自动抓取（请用 edit_paper 手动改标题）"}
        meta = fetch_arxiv(arxiv_id)
        if not meta or not meta.get("title"):
            return {"error": f"未能从 arXiv/Semantic Scholar 抓到 {arxiv_id} 的元数据（可能被限流，稍后再试）"}
        fields = {"title_en": meta["title"]}
        if meta.get("year"):
            fields["year"] = meta["year"]
        return pipe.update_metadata(pid, **fields)

    def _send_daily_email(self, recipient=None):
        from . import daily
        from .emailer import send_daily
        out_dir = self.cfg.root / "reports" / "daily"
        jsons = sorted(out_dir.glob("*.json")) if out_dir.exists() else []
        cands = []
        if jsons:
            try:
                cands = json.loads(jsons[-1].read_text(encoding="utf-8"))
            except Exception:
                cands = []
        if not cands:
            dcfg = self.cfg.config["daily"]
            cands = daily.run(self.cfg, list(dcfg.get("sources", [])),
                              int(dcfg.get("top_k", 10)), use_llm=True)
        if not cands:
            return {"error": "没有可发送的候选论文（可先 run_daily_retrieval）"}
        return send_daily(cands, recipient or None)

    def _read_daily_report(self, date_str=None):
        out_dir = self.cfg.root / "reports" / "daily"
        if not out_dir.exists():
            return {"error": "还没有每日报告（可先 run_daily_retrieval）"}
        if date_str:
            p = out_dir / f"{date_str}.md"
            if not p.exists():
                return {"error": f"没有 {date_str} 的报告"}
        else:
            mds = sorted(out_dir.glob("*.md"))
            if not mds:
                return {"error": "还没有每日报告"}
            p = mds[-1]
        return {"date": p.stem, "report": p.read_text(encoding="utf-8")[:6000]}

    def _dispatch(self, name, args):
        if name == "search_library":
            return self._search(args.get("query", ""), args.get("top_k", 5), args.get("mode", "search"))
        if name == "get_paper":
            return self._get(args.get("pid", ""))
        if name == "library_stats":
            return self._stats()
        if name == "list_by_area":
            return self._list_area(args.get("area") or None)
        if name == "compare_papers":
            return self._compare(args.get("pids") or [])
        if name == "remember":
            return {"ok": True, "note": self._remember(args.get("fact", ""))}
        if name == "deep_read":
            return self._deep_read(args.get("pid", ""))
        if name == "update_paper_note":
            return self._update_note(args.get("pid", ""), args.get("note", ""))
        if name == "web_search":
            return self._web_search(args.get("query", ""))
        if name == "list_pdfs":
            return self._list_pdfs()
        if name == "ingest_paper":
            return self._ingest(args.get("filename", ""))
        if name == "download_paper":
            return self._download(args.get("source", ""))
        if name == "reclassify_paper":
            return self._reclassify(args.get("pid", ""), args.get("category", ""),
                                    args.get("area", ""), args.get("work", ""),
                                    args.get("year"))
        if name == "mark_read":
            return self._mark_read(args.get("pid", ""), args.get("read", True))
        if name == "pull_library":
            return self._pull()
        if name == "push_to_cloud":
            return self._push(args.get("message") or "")
        if name == "delete_paper":
            return self._delete(args.get("pid", ""))
        if name == "run_daily_retrieval":
            return self._daily_retrieval(args.get("top_k"), args.get("sources"))
        if name == "edit_paper":
            return self._edit_paper(args.get("pid", ""),
                                    title_en=args.get("title_en"),
                                    title_zh=args.get("title_zh"),
                                    venue=args.get("venue"),
                                    year=args.get("year"))
        if name == "fix_metadata":
            return self._fix_metadata(args.get("pid", ""))
        if name == "send_daily_email":
            return self._send_daily_email(args.get("recipient"))
        if name == "read_daily_report":
            return self._read_daily_report(args.get("date"))
        return {"error": f"未知工具 {name}"}

    # ── 主循环 ────────────────────────────────────────────
    def ask(self, message: str, history=None) -> dict:
        self._cited = {}
        system = _load_manual(self.cfg)
        mem = self._memory_snippet()
        if mem:
            system += "\n\n关于用户的长期记忆（可信，回答时可参考）：\n" + mem
        notes = self._notes_inventory()
        if notes:
            system += "\n\n你已深读并留有笔记的论文 pid：" + ", ".join(notes) \
                      + "（相关时可用 get_paper 读取笔记）"

        msgs = [{"role": "system", "content": system}]
        for m in (history or []):
            msgs.append({"role": m.get("role", "user"), "content": m.get("content", "")})
        msgs.append({"role": "user", "content": message})

        pending = None
        for _ in range(MAX_ROUNDS):
            resp = self.llm.client.chat.completions.create(
                model=self.model, messages=msgs, tools=TOOLS, temperature=0.2)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return {"answer": msg.content or "",
                        "citations": [{"pid": p, "title_en": t}
                                      for p, t in self._cited.items()]}
            msgs.append({
                "role": "assistant", "content": msg.content,
                "tool_calls": [{"id": tc.id, "type": "function", "function": {
                    "name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls],
            })
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                result = self._dispatch(tc.function.name, args)
                if isinstance(result, dict) and result.get("requires_confirmation"):
                    pending = {"action_id": result["action_id"],
                               "action": result["action"],
                               "summary": result["summary"]}
                msgs.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, ensure_ascii=False)})
            if pending:
                return {"answer": pending["summary"]
                        + "\n\n⚠️ 该操作尚未执行，等你点击「确认执行」后才会真正进行。",
                        "citations": [{"pid": p, "title_en": t}
                                      for p, t in self._cited.items()],
                        "pending_action": pending}
        return {"answer": "",
                "citations": [{"pid": p, "title_en": t} for p, t in self._cited.items()]}

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
from datetime import date
from pathlib import Path

from .config_loader import Config
from .llm import LLM
from .pdftext import extract_text
from .retrieval import hybrid_search, keyword_search, load_documents, resolve_targets

MAX_ROUNDS = 6  # 工具调用轮数上限，防死循环

_FALLBACK_PROMPT = ("你是「论文管家」的馆长 agent，负责回答用户关于其个人论文库的一切问题，"
                    "用中文简洁作答，引用论文时用反引号写 pid。")


def _load_manual(config: Config) -> str:
    """加载 AGENT.md 作为馆长操作手册（身份/任务/要求/规范）；缺失时退回内置兜底提示。"""
    p = config.root / "AGENT.md"
    return p.read_text(encoding="utf-8") if p.exists() else _FALLBACK_PROMPT

TOOLS = [
    {"type": "function", "function": {
        "name": "search_library",
        "description": "在论文库中检索相关论文（语义+关键词混合）。用于用户想找某主题/问题的论文。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索词，中英文均可"},
            "top_k": {"type": "integer", "description": "返回条数，默认 5"},
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
    def _search(self, query, top_k=5):
        docs = load_documents(self.cfg)
        try:
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

    def _dispatch(self, name, args):
        if name == "search_library":
            return self._search(args.get("query", ""), args.get("top_k", 5))
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
                msgs.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, ensure_ascii=False)})
        return {"answer": "",
                "citations": [{"pid": p, "title_en": t} for p, t in self._cited.items()]}

"""Web 多接口（P4）：零依赖 stdlib HTTP 服务，复用检索/跳转/每日检索/录入/上云/对话核心。

四大模块：检索 / 论文库 / 每日简报 / 上传论文。
用法：
    python -m service.web [--host 127.0.0.1 --port 8000]

接口（写操作均走后台任务 POST → job_id → GET /api/job/<id> 轮询）：
    GET  /                      单页前端
    GET  /api/meta              库统计 + 分类学（重分类表单用）
    GET  /api/search            检索（与 CLI 同引擎）
    GET  /api/library/tree      论文库目录树（大领域→方向→work→论文）+ 分类 facet
    GET  /api/paper/<pid>       论文卡片 + 跳转目标
    GET  /pdf/<pid>             本地 PDF 流式回传（站内预览）
    GET  /api/daily/latest      最近一份每日简报
    GET  /api/job/<id>          后台任务进度
    POST /api/librarian         馆长 agent（DeepSeek 总控：查库/深读/联网检索 Kimi/下载/上云/删除）
    POST /api/librarian/confirm 确认执行馆长排定的危险操作（删除/上云）
    POST /api/ingest            上传 PDF 并入库
    POST /api/reclassify        重分类
    POST /api/daily/run         跑一次每日检索
    POST /api/daily/email       把勾选候选发邮箱（Gmail）
    POST /api/daily/add         把候选「推入论文库」
    POST /api/library/delete    删除一篇论文
    POST /api/library/sync      pull + push ModelScope
"""
import argparse
import base64
import json
import os
import threading
import uuid
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import daily
from .research_profile import load as load_profile, save as save_profile, ProfileConflict
from .config_loader import Config
from .retrieval import (embedding_explore, embedding_search, explore,
                        hybrid_search, keyword_search, load_documents,
                        resolve_targets)

_CONFIG = None
_DOCS = None


def _cfg() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config()
    return _CONFIG


def _docs() -> list[dict]:
    # CLI and external editors may change cards while the desktop is open.
    return load_documents(_cfg())


def _invalidate_docs():
    global _DOCS
    _DOCS = None


def _reload_config():
    """设置写入后重置配置/文档缓存，使新配置对后续请求生效。"""
    global _CONFIG, _DOCS
    _CONFIG = None
    _DOCS = None


def _do_search(config, docs, query, mode, engine, top):
    """与 search.py 同引擎的检索封装，返回 (results, note)。"""
    note = ""
    if engine in ("auto", "embedding"):
        try:
            if mode == "explore":
                results = embedding_explore(docs, query, top, config)
            elif engine == "auto":
                results = hybrid_search(docs, query, top, config)
            else:
                results = embedding_search(docs, query, top, config)
        except Exception as e:
            if engine == "embedding":
                raise
            note = f"语义检索不可用，已退回关键词：{e}"
            results = keyword_search(docs, query, top)
    else:
        results = explore(docs, query, top) if mode == "explore" else keyword_search(docs, query, top)
    return results, note


def _serialize_doc(d: dict) -> dict:
    area = d["area"].split("::")[-1] if "::" in d["area"] else d["area"]
    return {
        "pid": d["pid"], "title_en": d["title_en"], "title_zh": d["title_zh"],
        "category": d["category"], "area": area, "work": d["work"],
        "year": d["year"], "venue": d["venue"], "arxiv_id": d["arxiv_id"],
        "read": bool(d.get("read")),
    }


def _jump_targets(d: dict) -> list[dict]:
    """把 resolve_targets 转成前端友好的跳转按钮（local → 站内 /pdf/<pid>）。"""
    out = []
    for kind, loc in resolve_targets(d, _cfg()):
        if kind == "local":
            out.append({"kind": "local", "url": f"/pdf/{d['pid']}"})
        else:
            out.append({"kind": kind, "url": loc})
    return out


def _library_tree() -> dict:
    """论文库目录树：大领域 → 方向 → work → 论文，附分类/大领域 facet 计数。"""
    cfg = _cfg()
    docs = load_documents(cfg)
    groups = cfg.taxonomy["broad_field"]["groups"]
    area2field = {}
    for field, areas in groups.items():
        for a in areas:
            area2field[a] = field
    cats = cfg.taxonomy["category"]["values"]
    cat_counts = {c: 0 for c in cats}
    field_counts: dict[str, int] = {}
    tree: dict[str, dict] = {}
    for d in docs:
        if d["category"] in cat_counts:
            cat_counts[d["category"]] += 1
        field = area2field.get(d["area"], "未分类")
        field_counts[field] = field_counts.get(field, 0) + 1
        tree.setdefault(field, {}).setdefault(d["area"], {}).setdefault(d["work"], []).append(d)

    fields = []
    for field in sorted(tree):
        areas = []
        for area in sorted(tree[field]):
            works = []
            for work in sorted(tree[field][area]):
                papers = sorted(tree[field][area][work],
                                key=lambda x: (-(x.get("year") or 0), x["title_en"]))
                works.append({"work": work, "count": len(papers),
                              "papers": [{**_serialize_doc(p), "targets": _jump_targets(p)}
                                         for p in papers]})
            areas.append({"area": area, "count": sum(w["count"] for w in works), "works": works})
        fields.append({"field": field, "count": sum(a["count"] for a in areas), "areas": areas})
    return {"fields": fields, "category_counts": cat_counts,
            "field_counts": field_counts, "total": len(docs)}


# ── 后台任务 ────────────────────────────────────────────────────────────────
from .jobs import start as _start_job, snapshot as job_snapshot, progress


def _sync_after(result):
    """写操作后自动同步 ModelScope（失败不阻断主操作）。"""
    try:
        from .cloud import sync
        sync()
    except BaseException as e:
        print(f"[warn] 自动同步 ModelScope 失败（不影响本次操作）: {e}")
    return result


def _run_ingest(filename: str) -> dict:
    from .pipeline import IngestPipeline
    cfg = _cfg()
    result = IngestPipeline(cfg).run(cfg.inbox_dir / filename)
    _invalidate_docs()
    return _sync_after(result)


def _run_reclassify(pid, category, area, work, year) -> dict:
    from .pipeline import IngestPipeline
    cfg = _cfg()
    result = IngestPipeline(cfg).reclassify(pid, category, area, work, year)
    _invalidate_docs()
    return _sync_after(result)


def _run_delete(pid) -> dict:
    from .pipeline import IngestPipeline
    result = IngestPipeline(_cfg()).delete(pid)
    _invalidate_docs()
    return _sync_after(result)


def _run_chat(message: str, history: list) -> dict:
    from .chat import ChatClient
    cc = ChatClient(_cfg().config.get("chat") or {})
    msgs = [{"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in (history or [])]
    msgs.append({"role": "user", "content": message})
    return cc.chat(msgs)


def _run_chat_save(history: list) -> dict:
    from .chatlog import save_and_sync
    msgs = [{"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in (history or [])]
    return save_and_sync(msgs, _cfg())


def _run_librarian(message: str, history: list, session_id=None) -> dict:
    from .librarian import Librarian
    cfg = _cfg()
    lib = Librarian(cfg)
    msgs = [{"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in (history or [])]
    result = lib.ask(message, msgs, on_event=progress)
    # 馆长本轮可能已改库（edit_paper / fix_metadata / reclassify_paper / mark_read /
    # ingest_paper / download_paper 等都在 ask() 内同步执行）。统一失效 _DOCS 缓存，
    # 保证下次检索/卡片/统计从 KB 卡片（单一事实源）重读，而非旧缓存。
    _invalidate_docs()

    # 自动保存本次对话到私有云端（每轮覆盖同一会话文件，幂等；缺 token 时只存本地）。
    # 用原始 history（含 citations）重建完整会话，保证历史里每一轮的引用都被保留。
    if session_id:
        from .chatlog import save_session
        full = list(history or []) + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": result.get("answer") or "",
             "citations": result.get("citations"), "tool_events": result.get("tool_events")},
        ]
        model_cfg = cfg.config.get("model") or {}
        model = ((model_cfg.get("tasks") or {}).get("librarian")
                 or model_cfg.get("default") or "deepseek-chat")
        saved = save_session(session_id, full, cfg, model=model)
        result["session_id"] = saved["id"]
        result["saved"] = saved["pushed"]
    return result


def _run_librarian_confirm(action_id: str, approve: bool) -> dict:
    from .librarian import confirm
    result = confirm(action_id, bool(approve))
    # 确认后可能已删除论文（delete）或 pull/push 云端，失效缓存保持一致。
    _invalidate_docs()
    return result


def _delete_chat_session(sid: str) -> dict:
    from .chatlog import delete_session
    return delete_session(sid, _cfg())


def _run_mark_read(pid: str, read: bool) -> dict:
    from .read_state import mark, sync_read
    mark(_cfg(), pid, bool(read))
    _invalidate_docs()
    sync_read(_cfg())
    return {"pid": pid, "read": bool(read)}


def _run_daily_email(to, cands) -> dict:
    from .emailer import send_daily
    return send_daily(cands, to or None)


def _run_daily_add(cand: dict) -> dict:
    from .pipeline import IngestPipeline
    cfg = _cfg()
    pdf = daily.fetch_candidate_pdf(cand, cfg.inbox_dir)
    result = IngestPipeline(cfg).run(pdf)
    _invalidate_docs()
    return _sync_after(result)


def _run_daily_job() -> dict:
    cfg = _cfg()
    options = load_profile(cfg)["options"]
    sources = options["sources"]
    top_k = options["top_k"]
    cands = daily.run(cfg, sources, top_k, use_llm=True)
    return daily.save_report(cands, cfg)


def _run_library_sync() -> dict:
    from .cloud import pull, sync
    pull()
    sync()
    # pull 会用远端知识库覆盖本地 KB 卡片，必须失效缓存。
    _invalidate_docs()
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音默认访问日志
        pass

    # ── 响应工具 ──────────────────────────────────────────────
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # ── 路由 ─────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)
        try:
            if path == "/":
                return self._html((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
            if path in ("/assets/logo.png", "/favicon.ico"):
                root = Path(__file__).resolve().parent.parent
                asset = root / ("assets/logo.png" if path.endswith(".png") else "paperbook.ico")
                data = asset.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png" if path.endswith(".png") else "image/x-icon")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if path.startswith("/static/"):
                name = path.removeprefix("/static/")
                if name not in ("app.js", "app.css"):
                    return self._json({"error": "not found"}, 404)
                data = (STATIC_DIR / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if path == "/api/research-profile":
                return self._json(load_profile(_cfg()))
            if path == "/api/meta":
                return self._handle_meta()
            if path == "/api/search":
                return self._handle_search(qs)
            if path == "/api/library/tree":
                return self._json(_library_tree())
            if path.startswith("/api/paper/"):
                return self._handle_paper(path.rsplit("/", 1)[1])
            if path.startswith("/pdf/"):
                return self._handle_pdf(path[5:])
            if path == "/api/daily/latest":
                return self._handle_daily_latest()
            if path == "/api/settings":
                return self._handle_settings_get()
            if path == "/api/chat/history":
                return self._handle_chat_history()
            if path.startswith("/api/chat/history/"):
                return self._handle_chat_history_item(path.rsplit("/", 1)[1])
            if path.startswith("/api/job/"):
                return self._handle_job(path.rsplit("/", 1)[1])
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/research-profile":
                body = self._read_json_body()
                try:
                    return self._json(save_profile(_cfg(), body.get("content"), body.get("revision")))
                except ProfileConflict as exc:
                    return self._json({"error": str(exc)}, 409)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
            if parsed.path == "/api/ingest":
                return self._handle_ingest(self._read_json_body())
            if parsed.path == "/api/reclassify":
                return self._handle_reclassify(self._read_json_body())
            if parsed.path == "/api/chat":
                return self._handle_chat(self._read_json_body())
            if parsed.path == "/api/chat/save":
                return self._handle_chat_save(self._read_json_body())
            if parsed.path == "/api/librarian":
                return self._handle_librarian(self._read_json_body())
            if parsed.path == "/api/librarian/confirm":
                return self._handle_librarian_confirm(self._read_json_body())
            if parsed.path == "/api/chat/history/delete":
                return self._handle_chat_history_delete(self._read_json_body())
            if parsed.path == "/api/read":
                return self._handle_read(self._read_json_body())
            if parsed.path == "/api/daily/run":
                return self._json({"job_id": _start_job(_run_daily_job)})
            if parsed.path == "/api/daily/email":
                return self._handle_daily_email(self._read_json_body())
            if parsed.path == "/api/daily/add":
                return self._handle_daily_add(self._read_json_body())
            if parsed.path == "/api/library/delete":
                return self._handle_library_delete(self._read_json_body())
            if parsed.path == "/api/library/sync":
                return self._json({"job_id": _start_job(_run_library_sync)})
            if parsed.path == "/api/settings":
                return self._handle_settings_post(self._read_json_body())
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ── 设置 ─────────────────────────────────────────────────
    def _handle_settings_get(self):
        from . import settings
        return self._json(settings.get_settings())

    def _handle_settings_post(self, body):
        from . import settings
        try:
            result = settings.apply_settings(body.get("config") or {}, body.get("env") or {})
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        _reload_config()
        return self._json(result)

    # ── 读接口 ───────────────────────────────────────────────
    def _handle_meta(self):
        cfg = _cfg()
        tax = cfg.taxonomy
        areas = [f"{g}::{a}" for g, lst in tax["broad_field"]["groups"].items() for a in lst]
        return self._json({
            "library_size": len(_docs()),
            "categories": tax["category"]["values"],
            "areas": areas,
            "work_slugs": list(tax["work_slug"]["entries"].keys()),
            "top_k": cfg.config["retrieval"].get("top_k", 10),
        })

    def _handle_search(self, qs):
        cfg, docs = _cfg(), _docs()
        query = (qs.get("q") or [""])[0].strip()
        if not query:
            return self._json({"query": "", "note": "", "results": []})
        mode = (qs.get("mode") or ["search"])[0]
        engine = (qs.get("engine") or ["auto"])[0]
        top = int((qs.get("top") or ["10"])[0])
        results, note = _do_search(cfg, docs, query, mode, engine, top)
        return self._json({"query": query, "note": note,
                           "results": [{**_serialize_doc(d), "targets": _jump_targets(d)}
                                       for d in results]})

    def _handle_paper(self, pid):
        doc = next((d for d in _docs() if d["pid"] == pid), None)
        if not doc:
            return self._json({"error": "not found"}, 404)
        return self._json({"doc": doc, "targets": _jump_targets(doc)})

    def _handle_pdf(self, pid):
        doc = next((d for d in _docs() if d["pid"] == pid), None)
        if not doc:
            return self.send_error(404, "no such paper")
        local = next((loc for k, loc in resolve_targets(doc, _cfg()) if k == "local"), None)
        if not local or not os.path.exists(local):
            return self.send_error(404, "no local pdf")
        with open(local, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        # RFC 5987：中文文件名须 percent-encode，否则 latin-1 编码会抛 UnicodeEncodeError
        self.send_header("Content-Disposition",
                         f"inline; filename*=UTF-8''{quote(os.path.basename(local))}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_daily_latest(self):
        out_dir = _cfg().root / "reports" / "daily"
        jsons = sorted(out_dir.glob("*.json"))
        if not jsons:
            return self._json({"date": None, "candidates": []})
        latest = jsons[-1]
        data = json.loads(latest.read_text(encoding="utf-8"))
        return self._json({"date": latest.stem, "candidates": data})

    def _handle_job(self, jid):
        st = job_snapshot(jid)
        if not st:
            return self._json({"error": "not found"}, 404)
        return self._json(st)

    # ── 写接口 ───────────────────────────────────────────────
    def _handle_ingest(self, body):
        filename = (body.get("filename") or "").strip()
        data_b64 = (body.get("data") or "")
        if not filename or not data_b64:
            return self._json({"error": "缺少 filename 或 data"}, 400)
        filename = os.path.basename(filename)  # 防路径穿越
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        (_cfg().inbox_dir / filename).write_bytes(base64.b64decode(data_b64))
        jid = _start_job(lambda: _run_ingest(filename))
        return self._json({"job_id": jid, "filename": filename})

    def _handle_reclassify(self, body):
        pid = body.get("pid")
        category = body.get("category")
        area = body.get("area")
        work = body.get("work")
        year = body.get("year")
        if not all([pid, category, area, work, year]):
            return self._json({"error": "缺少 pid/category/area/work/year"}, 400)
        jid = _start_job(lambda: _run_reclassify(
            pid, category, area, work, int(year)))
        return self._json({"job_id": jid})

    def _handle_chat(self, body):
        message = (body.get("message") or "").strip()
        if not message:
            return self._json({"error": "缺少 message"}, 400)
        jid = _start_job(lambda: _run_chat(message, body.get("history") or []))
        return self._json({"job_id": jid})

    def _handle_chat_save(self, body):
        history = body.get("history") or []
        if not history:
            return self._json({"error": "没有可保存的对话"}, 400)
        jid = _start_job(lambda: _run_chat_save(history))
        return self._json({"job_id": jid})

    def _handle_librarian(self, body):
        message = (body.get("message") or "").strip()
        if not message:
            return self._json({"error": "缺少 message"}, 400)
        jid = _start_job(lambda: _run_librarian(
            message, body.get("history") or [], body.get("session_id")))
        return self._json({"job_id": jid})

    def _handle_librarian_confirm(self, body):
        action_id = body.get("action_id")
        if not action_id:
            return self._json({"error": "缺少 action_id"}, 400)
        jid = _start_job(lambda: _run_librarian_confirm(action_id, body.get("approve", True)))
        return self._json({"job_id": jid})

    def _handle_chat_history(self):
        from .chatlog import list_sessions
        return self._json({"sessions": list_sessions(_cfg())})

    def _handle_chat_history_item(self, sid):
        from .chatlog import load_session
        return self._json(load_session(sid, _cfg()))

    def _handle_chat_history_delete(self, body):
        sid = (body.get("session_id") or "").strip()
        if not sid:
            return self._json({"error": "缺少 session_id"}, 400)
        jid = _start_job(lambda: _delete_chat_session(sid))
        return self._json({"job_id": jid})

    def _handle_read(self, body):
        pid = body.get("pid")
        if not pid:
            return self._json({"error": "缺少 pid"}, 400)
        jid = _start_job(lambda: _run_mark_read(pid, body.get("read", True)))
        return self._json({"job_id": jid})

    def _handle_daily_email(self, body):
        cands = body.get("cands") or []
        if not cands:
            return self._json({"error": "没有要发送的论文"}, 400)
        jid = _start_job(lambda: _run_daily_email(body.get("to"), cands))
        return self._json({"job_id": jid})

    def _handle_daily_add(self, body):
        cand = body.get("cand") or {}
        if not cand.get("title"):
            return self._json({"error": "缺少候选论文信息"}, 400)
        jid = _start_job(lambda: _run_daily_add(cand))
        return self._json({"job_id": jid})

    def _handle_library_delete(self, body):
        pid = body.get("pid")
        if not pid:
            return self._json({"error": "缺少 pid"}, 400)
        jid = _start_job(lambda: _run_delete(pid))
        return self._json({"job_id": jid})


STATIC_DIR = Path(__file__).resolve().parent / "static"



def create_server(host="127.0.0.1", port=8000) -> ThreadingHTTPServer:
    """构造（但不启动）HTTP 服务，供 CLI 与桌面壳复用。"""
    return ThreadingHTTPServer((host, port), Handler)


def main():
    ap = argparse.ArgumentParser(description="论文管家 Web 界面")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    server = create_server(args.host, args.port)
    print(f"AI 论文管家 Web 已启动： http://{args.host}:{args.port}  （Ctrl+C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()

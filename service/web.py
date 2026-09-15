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
import contextlib
import io
import json
import os
import threading
import uuid
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import daily
from .config_loader import Config
from .retrieval import (embedding_explore, embedding_search, explore,
                        hybrid_search, keyword_search, load_documents,
                        resolve_targets)

_CONFIG = None
_DOCS = None
_JOBS = {}
_JOBS_LOCK = threading.Lock()


def _cfg() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config()
    return _CONFIG


def _docs() -> list[dict]:
    global _DOCS
    if _DOCS is None:
        _DOCS = load_documents(_cfg())
    return _DOCS


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
def _start_job(fn) -> str:
    jid = uuid.uuid4().hex[:8]
    with _JOBS_LOCK:
        _JOBS[jid] = {"status": "running", "result": None, "log": "", "error": None}

    def wrapper():
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                result = fn()
            with _JOBS_LOCK:
                _JOBS[jid].update(status="done", result=result, log=buf.getvalue())
        except BaseException as e:
            with _JOBS_LOCK:
                _JOBS[jid].update(status="error",
                                  error=f"{type(e).__name__}: {e}", log=buf.getvalue())

    threading.Thread(target=wrapper, daemon=True).start()
    return jid


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


def _run_librarian(message: str, history: list) -> dict:
    from .librarian import Librarian
    lib = Librarian(_cfg())
    msgs = [{"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in (history or [])]
    result = lib.ask(message, msgs)
    # 馆长本轮可能已改库（edit_paper / fix_metadata / reclassify_paper / mark_read /
    # ingest_paper / download_paper 等都在 ask() 内同步执行）。统一失效 _DOCS 缓存，
    # 保证下次检索/卡片/统计从 KB 卡片（单一事实源）重读，而非旧缓存。
    _invalidate_docs()
    return result


def _run_librarian_confirm(action_id: str, approve: bool) -> dict:
    from .librarian import confirm
    result = confirm(action_id, bool(approve))
    # 确认后可能已删除论文（delete）或 pull/push 云端，失效缓存保持一致。
    _invalidate_docs()
    return result


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
    sources = list(cfg.config["daily"].get("sources", []))
    top_k = cfg.config["daily"].get("top_k", 10)
    cands = daily.run(cfg, sources, top_k, use_llm=True)
    out_dir = cfg.root / "reports" / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    (out_dir / f"{today}.md").write_text(daily.render_md(cands, cfg), encoding="utf-8")
    (out_dir / f"{today}.json").write_text(
        json.dumps(cands, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"date": today, "count": len(cands)}


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
                return self._html(INDEX_HTML)
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
            if path.startswith("/api/job/"):
                return self._handle_job(path.rsplit("/", 1)[1])
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
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
        result = settings.apply_settings(body.get("config") or {}, body.get("env") or {})
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
        with _JOBS_LOCK:
            st = _JOBS.get(jid)
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
        jid = _start_job(lambda: _run_librarian(message, body.get("history") or []))
        return self._json({"job_id": jid})

    def _handle_librarian_confirm(self, body):
        action_id = body.get("action_id")
        if not action_id:
            return self._json({"error": "缺少 action_id"}, 400)
        jid = _start_job(lambda: _run_librarian_confirm(action_id, body.get("approve", True)))
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


INDEX_HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 论文管家</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&family=Playfair+Display:ital,wght@0,400;0,600;0,700;0,900;1,400&family=Lora:ital,wght@0,400;0,600;1,400&display=swap');

  :root{
    /* Newsprint 调色板 —— 永久浅色，无暗色模式 */
    --bg:#F9F9F7; --card:#F9F9F7; --ink:#111111; --mut:#737373;
    --line:#111111; --line-soft:#E5E5E0; --hover:#F5F5F5;
    --acc:#CC0000; --danger:#CC0000;
    /* 字体栈：衬线标题 / 衬线正文 / 无衬线 UI / 等宽数据 */
    --serif:'Playfair Display','Noto Serif SC','Source Han Serif SC','SimSun',serif;
    --body:'Lora','Noto Serif SC','Source Han Serif SC','SimSun',serif;
    --sans:'Inter','PingFang SC','Microsoft YaHei','Helvetica Neue',sans-serif;
    --mono:'JetBrains Mono','SFMono-Regular','Courier New',monospace;
    /* 版式尺子：头部/导航高度，供 sticky 偏移引用 */
    --head-h:52px; --nav-h:46px; --rail-top:calc(var(--head-h) + var(--nav-h));
  }
  * { box-sizing:border-box; }
  body { margin:0; font-family:var(--sans); color:var(--ink); font-size:15px;
    background-color:var(--bg);
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='4' height='4' viewBox='0 0 4 4'%3E%3Cpath fill='%23111111' fill-opacity='0.035' d='M1 3h1v1H1V3zm2-2h1v1H3V1z'%3E%3C/path%3E%3C/svg%3E"); }
  a { color:var(--ink); }
  a:hover { color:var(--acc); }

  /* ── 报头（masthead）── */
  header { background:var(--card); border-bottom:3px solid var(--ink);
    display:flex; align-items:center; justify-content:space-between; gap:16px;
    padding:0 24px; height:var(--head-h); position:sticky; top:0; z-index:40; }
  header .masthead { display:flex; align-items:baseline; gap:14px; min-width:0; }
  header h1 { font-family:var(--serif); font-size:22px; font-weight:900; margin:0; letter-spacing:.3px; line-height:1; }
  header .edition { font-family:var(--mono); font-size:10.5px; text-transform:uppercase; letter-spacing:.14em; color:var(--mut); white-space:nowrap; }
  header .stat { font-family:var(--mono); font-size:11px; text-transform:uppercase; letter-spacing:.08em; color:var(--mut); white-space:nowrap; }

  /* ── 导航 ── */
  nav { display:flex; gap:0; padding:0 24px; background:var(--card); border-bottom:1px solid var(--line);
        height:var(--nav-h); position:sticky; top:var(--head-h); z-index:40; }
  nav button { border:0; background:none; padding:0 18px; height:100%; font-size:13px; font-family:var(--sans);
               text-transform:uppercase; letter-spacing:.06em; cursor:pointer; color:var(--mut);
               border-bottom:3px solid transparent; transition:color .15s; }
  nav button:hover { color:var(--acc); }
  nav button.on { color:var(--ink); font-weight:700; border-bottom-color:var(--acc); }

  /* ── 布局 ── */
  .app { display:flex; align-items:stretch; }
  main.content { flex:1; min-width:0; padding:22px 24px 60px; max-width:1020px; margin:0 auto; }
  .chat-rail { width:380px; flex-shrink:0; border-left:2px solid var(--ink); background:var(--card);
               display:flex; flex-direction:column; position:sticky; top:var(--rail-top); height:calc(100vh - var(--rail-top)); }
  .rail-resizer { width:6px; flex-shrink:0; cursor:col-resize; position:relative; }
  .rail-resizer::after { content:''; position:absolute; left:2px; top:0; bottom:0; width:2px; background:var(--line-soft); }
  .rail-resizer:hover::after, .rail-resizer.drag::after { background:var(--acc); }
  .panel { display:none; } .panel.on { display:block; animation:fade .2s ease; }
  @keyframes fade { from{opacity:0; transform:translateY(4px)} to{opacity:1; transform:none} }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }

  /* ── 表单控件（底部双线，报纸表格样式）── */
  input[type=text], input[type=number], input[type=email] { padding:9px 4px; font-size:14px;
    font-family:var(--mono); border:0; border-bottom:2px solid var(--ink); border-radius:0;
    background:transparent; color:var(--ink); }
  input[type=text] { flex:1; min-width:200px; }
  input[type=text]:focus, input[type=number]:focus, input[type=email]:focus, textarea:focus { background:var(--hover); }
  select { padding:9px 10px; font-size:13px; font-family:var(--sans); border:1px solid var(--ink);
           border-radius:0; background:var(--card); color:var(--ink); cursor:pointer; }
  input[type=file] { font-family:var(--mono); font-size:12.5px; }
  input[type=checkbox] { accent-color:var(--ink); }
  :focus-visible { outline:2px solid var(--ink); outline-offset:2px; }

  /* ── 按钮（黑白反转，尖角，大写字距）── */
  button.btn { font-family:var(--sans); text-transform:uppercase; letter-spacing:.07em; font-size:12.5px;
    font-weight:600; padding:9px 15px; border:1px solid var(--ink); border-radius:0; background:transparent;
    color:var(--ink); cursor:pointer; transition:background .15s,color .15s; }
  button.btn:hover { background:var(--ink); color:var(--bg); }
  button.primary { background:var(--ink); color:var(--bg); border-color:var(--ink); }
  button.primary:hover { background:transparent; color:var(--ink); }
  button.danger { color:var(--danger); border-color:var(--danger); background:transparent; }
  button.danger:hover { background:var(--danger); color:var(--bg); }
  button.ghost { border:0; background:none; padding:4px 8px; color:var(--mut); text-transform:none; letter-spacing:0; }
  button.ghost:hover { color:var(--ink); background:transparent; }
  button.small { padding:4px 10px; font-size:11px; }

  .muted { color:var(--mut); font-size:13px; }
  .hint { color:var(--mut); font-size:12.5px; margin-top:6px; }
  .card { background:var(--card); border:1px solid var(--ink); border-radius:0; padding:14px 16px; margin-top:12px; }
  .empty { color:var(--mut); padding:28px; text-align:center; }

  /* ── 标签（元数据，等宽大写）── */
  .tag { display:inline-block; font-family:var(--mono); font-size:10px; text-transform:uppercase; letter-spacing:.06em;
         padding:2px 6px; border:1px solid var(--line-soft); border-radius:0; background:transparent;
         color:var(--mut); margin-right:5px; }
  .tag.muted { color:var(--mut); }
  .tag.read { color:var(--ink); border-color:var(--ink); }
  .tag.unread { color:var(--acc); border-color:var(--acc); }

  /* 跳转按钮 */
  .jumps { margin-top:6px; }
  a.jump { display:inline-block; margin:0 8px 6px 0; padding:4px 10px; font-family:var(--mono); font-size:11px;
           text-transform:uppercase; letter-spacing:.05em; border:1px solid var(--ink); border-radius:0;
           text-decoration:none; color:var(--ink); background:var(--card); transition:background .12s,color .12s; }
  a.jump:hover { background:var(--ink); color:var(--bg); }

  /* ── 检索结果 ── */
  .result { display:flex; gap:12px; align-items:flex-start; padding:14px 2px;
            border-bottom:1px solid var(--line-soft); }
  .result:last-child { border-bottom:0; }
  .result .idx { font-family:var(--mono); color:var(--mut); font-weight:500; min-width:22px; font-size:13px; }
  .r-body { flex:1; min-width:0; }
  .t-en { font-family:var(--serif); font-weight:700; cursor:pointer; line-height:1.35; font-size:15.5px; }
  .t-en:hover { color:var(--acc); text-decoration:underline; text-decoration-color:var(--acc); text-decoration-thickness:2px; }
  .t-zh { color:var(--mut); font-size:13px; margin-top:2px; }
  .r-tags { margin-top:6px; }

  /* ── 论文库 ── */
  .lib-wrap { display:flex; gap:18px; align-items:flex-start; }
  .sidebar { width:220px; flex-shrink:0; position:sticky; top:calc(var(--rail-top) + 20px); }
  .sidebar h4 { font-family:var(--mono); font-size:11px; text-transform:uppercase; letter-spacing:.14em; color:var(--mut); margin:14px 0 8px; padding-bottom:4px; border-bottom:1px solid var(--line-soft); }
  .chip { display:block; width:100%; text-align:left; border:1px solid var(--line-soft); background:var(--card);
          border-radius:0; padding:7px 10px; margin-bottom:6px; font-size:13.5px; cursor:pointer;
          color:var(--ink); transition:border-color .12s,background .12s; }
  .chip span { float:right; font-family:var(--mono); color:var(--mut); font-size:11px; }
  .chip:hover { border-color:var(--ink); background:var(--hover); }
  .chip.on { border-color:var(--ink); background:var(--ink); color:var(--bg); font-weight:600; }
  .chip.on span { color:var(--bg); }
  .tree { flex:1; min-width:0; }
  .tree details { margin:2px 0; }
  .tree summary { cursor:pointer; padding:7px 10px; border-radius:0; font-weight:600; font-size:14px;
                  list-style:none; user-select:none; transition:background .12s; }
  .tree summary::-webkit-details-marker { display:none; }
  .tree summary::before { content:"▸"; color:var(--mut); margin-right:8px; display:inline-block; transition:transform .15s; }
  .tree details[open] > summary::before { transform:rotate(90deg); }
  .tree summary:hover { background:var(--hover); }
  .tree details.field > summary { background:var(--card); border:1px solid var(--ink); font-family:var(--serif); font-size:16px; }
  .tree .cnt { font-family:var(--mono); color:var(--mut); font-weight:400; font-size:12px; margin-left:6px; }
  .tree .dot { display:inline-block; width:8px; height:8px; border-radius:0; background:var(--acc); margin-right:8px; }
  .papers { padding:2px 0 8px 22px; }
  .paper { border-left:3px solid var(--line-soft); margin:4px 0 6px 6px; padding:8px 12px; border-radius:0; }
  .paper:hover { border-left-color:var(--acc); background:var(--hover); }
  .p-title { font-family:var(--serif); font-weight:700; cursor:pointer; font-size:14px; line-height:1.35; }
  .p-title:hover { color:var(--acc); }
  .p-zh { color:var(--mut); font-size:12.5px; }

  /* ── 详情浮层 ── */
  .overlay { position:fixed; inset:0; background:rgba(17,17,17,.5); display:none; align-items:flex-start;
             justify-content:center; z-index:100; padding:4vh 16px; overflow-y:auto; }
  .overlay.on { display:flex; }
  .modal { background:var(--card); border:2px solid var(--ink); border-radius:0; width:100%;
           max-width:820px; padding:22px 24px; animation:fade .2s ease; }
  .d-head { display:flex; justify-content:space-between; gap:12px; border-bottom:3px solid var(--ink); padding-bottom:10px; }
  .d-head h3 { margin:0 0 4px; font-family:var(--serif); font-size:20px; line-height:1.3; font-weight:900; }
  .d-zh { color:var(--mut); font-size:13.5px; }
  .d-meta { margin-top:6px; color:var(--mut); font-size:13px; }
  .sec { margin-top:14px; } .sec b { font-family:var(--mono); font-size:11px; text-transform:uppercase; letter-spacing:.1em; color:var(--mut); display:block; margin-bottom:4px; }
  .sec p { margin:0; font-family:var(--body); line-height:1.65; font-size:14.5px; }
  .form-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-top:8px; }

  /* ── 每日简报 ── */
  .daily-layout { display:flex; gap:18px; align-items:flex-start; }
  .daily-left { flex:1.25; min-width:0; }
  .chat-panel { flex:1; min-width:0; position:sticky; top:calc(var(--rail-top) + 20px); }
  .cand { padding:14px; border-bottom:1px solid var(--line-soft); }
  .cand:last-child { border-bottom:0; }
  .cand:hover { background:var(--hover); }
  .cand-head { display:flex; align-items:center; gap:10px; }
  .cand-title { font-family:var(--serif); font-weight:700; flex:1; line-height:1.35; }
  .cand .score { font-family:var(--mono); color:var(--acc); font-weight:700; font-size:13px; white-space:nowrap; }
  .cand-meta { color:var(--mut); font-size:12.5px; margin:4px 0 0 26px; }
  .cand-reason { color:var(--acc); font-size:12.5px; margin:4px 0 0 26px; }
  .cand-abs { font-family:var(--body); color:var(--mut); font-size:12.5px; margin:5px 0 0 26px; line-height:1.55; }
  .cand-actions { margin:8px 0 0 26px; }

  /* ── 聊天（右侧常驻栏）── */
  .chat-head { padding:12px 14px; border-bottom:2px solid var(--ink); }
  .chat-title { font-family:var(--serif); font-weight:700; font-size:16px; letter-spacing:.02em; }
  .chat-hint { color:var(--mut); font-size:12px; line-height:1.5; margin-top:8px; }
  .pending-box { border:2px solid var(--acc); background:var(--bg); padding:8px 10px; margin-top:8px; font-size:13px; }
  .pending-btns { display:flex; gap:8px; margin-top:8px; }
  .pending-btns .btn { padding:5px 12px; }
  .chat-box { flex:1; min-height:0; overflow-y:auto; padding:12px 14px; display:flex; flex-direction:column; gap:10px; }
  .msg { max-width:92%; padding:9px 13px; border:1px solid var(--ink); border-radius:0; font-size:14px; line-height:1.6; }
  .msg.user { align-self:flex-end; background:var(--ink); color:var(--bg); }
  .msg.assistant { align-self:flex-start; background:var(--card); color:var(--ink); font-family:var(--body); }
  .msg.typing { color:var(--mut); font-style:italic; border-style:dashed; }
  .msg a { color:var(--acc); text-decoration:underline; text-decoration-thickness:1.5px; }
  .cites { margin-top:7px; display:flex; flex-wrap:wrap; gap:6px; }
  .cite { font-family:var(--mono); font-size:10.5px; padding:3px 8px; border:1px solid var(--ink); border-radius:0; cursor:pointer;
          background:transparent; color:var(--ink); max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .cite:hover { background:var(--acc); border-color:var(--acc); color:var(--bg); }
  .chat-input-row { display:flex; gap:8px; padding:12px 14px; border-top:2px solid var(--ink); align-items:flex-end; }
  .chat-input-row textarea { flex:1; resize:none; height:56px; min-height:36px; max-height:40vh;
    font-family:var(--body); font-size:14px; line-height:1.5; padding:8px 10px;
    border:1px solid var(--ink); border-radius:0; background:var(--card); color:var(--ink); }
  .chat-input-row textarea:focus { outline:2px solid var(--ink); outline-offset:2px; }
  .input-resizer { height:6px; flex-shrink:0; cursor:row-resize; position:relative; }
  .input-resizer::after { content:''; position:absolute; left:0; right:0; top:2px; height:2px; background:var(--line-soft); }
  .input-resizer:hover::after, .input-resizer.drag::after { background:var(--acc); }

  pre { background:var(--ink); color:var(--bg); padding:12px; border-radius:0; overflow:auto;
        font-family:var(--mono); font-size:12px; line-height:1.5; max-height:300px; }
  #toast { position:fixed; bottom:26px; left:50%; transform:translateX(-50%) translateY(20px);
           background:var(--ink); color:var(--bg); border:1px solid var(--ink); padding:10px 18px; border-radius:0;
           font-family:var(--sans); font-size:13px; opacity:0; pointer-events:none; transition:all .25s; z-index:200; }
  #toast.show { opacity:1; transform:translateX(-50%) translateY(0); }

  @media (max-width:900px){
    .lib-wrap, .daily-layout { flex-direction:column; }
    .sidebar, .chat-panel { position:static; width:100%; }
    .app { flex-direction:column; }
    .chat-rail { width:100% !important; height:520px; position:static; border-left:0; border-top:2px solid var(--ink); }
    .rail-resizer { display:none; }
  }
  @media (prefers-reduced-motion: reduce){
    * { animation:none !important; transition:none !important; }
  }
  /* ── 设置控制台 ── */
  .settings-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; align-items:start; }
  @media (max-width:900px){ .settings-grid { grid-template-columns:1fr; } }
  .field { display:grid; grid-template-columns:190px 1fr; gap:10px; align-items:center; margin:6px 0; }
  .field label { font-size:13px; color:var(--ink); word-break:break-all; }
  .field input, .field textarea {
    width:100%; padding:7px 10px; border:1px solid var(--line, #ddd); border-radius:6px;
    background:var(--panel, #fff); color:var(--ink); font-size:13px; font-family:inherit; }
  .field textarea { min-height:70px; resize:vertical; }
  .field .secret-badge { font-size:12px; color:var(--accent, #0a7); }
  .env-row { display:grid; grid-template-columns:190px 1fr 90px; gap:10px; align-items:center; margin:6px 0; }
  .env-row label { font-size:13px; word-break:break-all; }
  .env-row .clear-hint { font-size:12px; color:var(--muted, #888); display:flex; align-items:center; gap:4px; }
</style>
</head>
<body>
<header><div class="masthead"><h1>AI 论文管家</h1><span class="edition">Vol. 1 · 本地版 · 每日出版</span></div><span class="stat" id="stat"></span></header>
<nav>
  <button data-tab="search" class="on">检索</button>
  <button data-tab="library">论文库</button>
  <button data-tab="daily">每日简报</button>
  <button data-tab="upload">上传论文</button>
  <button data-tab="settings">设置</button>
</nav>
<div class="app">
<main class="content">
  <!-- 检索 -->
  <section id="tab-search" class="panel on">
    <div class="row">
      <input type="text" id="q" placeholder="输入检索词，如「GPU 集群调度」「memory handover」" />
      <select id="mode"><option value="search">精准检索</option><option value="explore">探索发现</option></select>
      <select id="engine">
        <option value="auto">auto（语义+关键词）</option>
        <option value="embedding">纯语义</option>
        <option value="keyword">纯关键词</option>
      </select>
      <select id="top"><option>5</option><option selected>10</option><option>20</option></select>
      <button class="btn primary" onclick="doSearch()">搜索</button>
    </div>
    <div class="hint">「探索发现」= 跨子方向去重（MMR），帮你一次扫到不同研究方向的论文，而不是只看最相关的几篇。</div>
    <div id="search-note" class="muted" style="margin-top:8px"></div>
    <div id="results"></div>
  </section>

  <!-- 论文库 -->
  <section id="tab-library" class="panel">
    <div class="row">
      <button class="btn primary" onclick="librarySync()">⇅ 同步 ModelScope</button>
      <span class="muted" id="sync-status"></span>
      <span class="muted" style="margin-left:auto">所有改动（重分类/删除/录入）都会自动同步上云</span>
    </div>
    <div class="lib-wrap">
      <aside class="sidebar">
        <h4>分类</h4><div id="facet-cat"></div>
        <h4>大领域</h4><div id="facet-field"></div>
      </aside>
      <div class="tree card" id="tree"></div>
    </div>
  </section>

  <!-- 每日简报 -->
  <section id="tab-daily" class="panel">
    <div class="row">
      <button class="btn primary" onclick="runDaily()">立即运行</button>
      <button class="btn" onclick="sendEmail()">✉ 发送到邮箱</button>
      <input type="email" id="email-to" placeholder="收件邮箱（留空用 .env 的 SMTP_TO）" style="min-width:220px" />
      <span class="muted" id="daily-status"></span>
      <span class="muted" id="daily-date" style="margin-left:auto"></span>
    </div>
    <div class="daily-left card" id="daily-list"></div>
  </section>

  <!-- 上传论文 -->
  <section id="tab-upload" class="panel">
    <div class="card">
      <b>上传并入库</b>
      <p class="muted">可一次多选多个 PDF，逐个排队走 分类 → Zotero → 双语卡 → manifest 全流程（每篇约 15–30 秒），全部完成后自动同步 ModelScope。</p>
      <div class="row"><input type="file" id="file" accept="application/pdf" multiple><button class="btn primary" id="upload-btn" onclick="ingest()">上传并入库</button></div>
      <div id="job-log"></div>
    </div>
  </section>

  <!-- 设置 -->
  <section id="tab-settings" class="panel">
    <div class="card">
      <div class="row" style="align-items:center">
        <b>设置控制台</b>
        <span class="muted">修改参数配置与 API Key，保存后立即生效</span>
        <span style="margin-left:auto"><button class="btn primary" onclick="saveSettings()">保存全部</button></span>
      </div>
      <div id="settings-status" class="muted" style="margin-top:8px"></div>
    </div>
    <div class="settings-grid">
      <div class="card">
        <h4>参数配置 <span class="muted">config.yml</span></h4>
        <div id="cfg-fields"></div>
      </div>
      <div class="card">
        <h4>API 密钥 <span class="muted">.env</span></h4>
        <p class="muted">密钥仅存本地 .env（不上云）。输入框留空 = 保持不变；勾选「清除」= 删除该项。</p>
        <div id="env-fields"></div>
      </div>
    </div>
  </section>
</main>

<div class="rail-resizer" id="rail-resizer"></div>
<aside class="chat-rail" id="chat-rail">
  <div class="chat-head">
    <div class="chat-title">馆长</div>
    <div class="chat-hint" id="chat-hint">查库 · 深读 · 全网搜索 · 下载 · 上云 · 删除——一个馆长全搞定</div>
  </div>
  <div class="chat-box" id="chat-box"></div>
  <div class="input-resizer" id="input-resizer" title="拖动调整输入框高度"></div>
  <div class="chat-input-row">
    <textarea id="chat-input" rows="2" placeholder="问论文库 / 联网搜索 / 下载 / 上云…（Enter 发送，Shift+Enter 换行）" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat()}"></textarea>
    <button class="btn" onclick="saveChat()" title="保存当前对话">💾</button>
    <button class="btn primary" onclick="sendChat()">发送</button>
  </div>
</aside>
</div>

<div class="overlay" id="overlay" onclick="if(event.target===this)closeOverlay()">
  <div class="modal" id="modal"></div>
</div>

<script>
const $ = s => document.querySelector(s);
let META = {categories:[], areas:[], work_slugs:[]};
let LIB = null, DAILY = [];
let hist = [];
const FILTER = {cat:null, field:null};
const KIND = {arxiv:'arXiv', doi:'DOI', cloud:'云', zotero:'Zotero', local:'预览'};

function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmtMD(s){ return esc(s)
  .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>')
  .replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')
  .replace(/\n/g,'<br>'); }

function toast(msg){
  let t = $('#toast');
  if(!t){ t=document.createElement('div'); t.id='toast'; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._tm); t._tm = setTimeout(()=>t.classList.remove('show'), 2600);
}
async function postFetch(url, body){
  return await (await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body||{})})).json();
}
function pollJob(jobId, onDone){
  const t = setInterval(async ()=>{
    let s;
    try { s = await (await fetch('/api/job/'+jobId)).json(); }
    catch(e){ clearInterval(t); onDone({status:'error', error:'网络错误'}); return; }
    if(s.status!=='running'){ clearInterval(t); onDone(s); }
  }, 1200);
}

// ── 导航 / 元数据 ──
async function loadMeta(){
  META = await (await fetch('/api/meta')).json();
  $('#stat').textContent = `已收录 ${META.library_size} 篇论文`;
}
function switchTab(name){
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on', b.dataset.tab===name));
  document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('on', p.id==='tab-'+name));
  if(name==='library'){ loadMeta(); if(!LIB) loadLibrary(); }
  if(name==='daily') loadDaily();
  if(name==='settings') loadSettings();
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));

// ── 跳转 / 详情 ──
function jumpButtons(targets){
  return (targets||[]).map(t=>{
    if(t.kind==='local') return `<a class="jump" href="${esc(t.url)}" target="_blank" rel="noopener">📄 预览</a>`;
    return `<a class="jump" href="${esc(t.url)}" target="_blank" rel="noopener">${KIND[t.kind]||t.kind}</a>`;
  }).join('');
}
function readTag(d){
  return d.read ? '<span class="tag read">已读</span>' : '<span class="tag unread">未读</span>';
}
function markRead(pid, read){
  postFetch('/api/read', {pid, read}).then(r=>{
    if(r.error){ toast(r.error); return; }
    toast('标记中…');
    pollJob(r.job_id, s=>{
      if(s.status==='error') toast('标记失败：'+s.error);
      else toast(read?'已标记为已读':'已标记为未读');
      loadMeta(); loadLibrary();
      const q = $('#q').value.trim(); if(q) doSearch();
    });
  });
}
function openOverlay(html){ $('#modal').innerHTML = html; $('#overlay').classList.add('on'); }
function closeOverlay(){ $('#overlay').classList.remove('on'); $('#modal').innerHTML=''; }
async function showPaper(pid){
  const r = await (await fetch('/api/paper/'+encodeURIComponent(pid))).json();
  if(r.error){ toast(r.error); return; }
  openOverlay(detailHTML(r.doc, r.targets));
}
function detailHTML(d, targets){
  const cats = META.categories.map(c=>`<option ${c===d.category?'selected':''}>${esc(c)}</option>`).join('');
  const areas = META.areas.map(a=>`<option value="${esc(a)}" ${a.split('::').pop()===d.area?'selected':''}>${esc(a)}</option>`).join('');
  const slugs = META.work_slugs.map(w=>`<option value="${esc(w)}">`).join('');
  return `
    <div class="d-head">
      <div>
        <h3>${esc(d.title_en)}</h3>
        ${d.title_zh && d.title_zh!==d.title_en ? `<div class="d-zh">${esc(d.title_zh)}</div>`:''}
        <div class="d-meta">${readTag(d)}<span class="tag">${esc(d.category)}</span><span class="tag">${esc(d.area)}</span><span class="tag">${esc(d.work)}</span><span class="tag">${esc(d.year)}</span> ${esc(d.venue||'')}<button class="btn small" onclick="markRead('${esc(d.pid)}',${!d.read})">${d.read?'标未读':'标已读'}</button></div>
      </div>
      <button class="btn ghost" onclick="closeOverlay()">✕</button>
    </div>
    <div class="jumps">${jumpButtons(targets)}</div>
    <div class="sec"><b>核心内容（中文）</b><p>${esc(d.core_zh)}</p></div>
    <div class="sec"><b>Core idea (English)</b><p>${esc(d.core_en)}</p></div>
    <div class="sec"><b>意义（中文）</b><p>${esc(d.sig_zh)}</p></div>
    <div class="sec"><b>Significance (English)</b><p>${esc(d.sig_en)}</p></div>
    <div class="sec">
      <button class="btn small" onclick="togglePreview('${esc(d.pid)}')">📄 站内预览 PDF</button>
      <div id="preview-wrap" style="display:none;margin-top:8px">
        <iframe id="preview-frame" src="" style="width:100%;height:520px;border:1px solid var(--ink);border-radius:0;background:#fff"></iframe>
      </div>
    </div>
    <div class="sec"><b>纠正分类（重分类）</b>
      <div class="form-grid">
        <select id="rc-cat">${cats}</select>
        <select id="rc-area">${areas}</select>
        <input id="rc-work" list="slug-list" value="${esc(d.work)}" placeholder="work slug">
        <datalist id="slug-list">${slugs}</datalist>
        <input id="rc-year" type="number" value="${esc(d.year)}" placeholder="年份">
        <button class="btn primary" onclick="doReclassify('${esc(d.pid)}')">提交重分类</button>
        <button class="btn danger" onclick="doDelete('${esc(d.pid)}')">删除此论文</button>
      </div>
    </div>`;
}
function togglePreview(pid){
  const w = $('#preview-wrap'), f = $('#preview-frame');
  if(w.style.display==='none'){ f.src = '/pdf/'+encodeURIComponent(pid); w.style.display='block'; }
  else { f.src=''; w.style.display='none'; }
}
function doReclassify(pid){
  const body = {pid, category:$('#rc-cat').value, area:$('#rc-area').value,
                work:$('#rc-work').value.trim(), year:parseInt($('#rc-year').value||'0')};
  if(!body.work || !body.year){ toast('请填 work slug 与年份'); return; }
  postFetch('/api/reclassify', body).then(r=>{
    if(r.error){ toast(r.error); return; }
    toast('重分类中…');
    pollJob(r.job_id, s=>{
      if(s.status==='error') toast('重分类失败：'+s.error);
      else { toast('已重分类并同步 ModelScope'); closeOverlay(); loadMeta(); loadLibrary(); }
    });
  });
}
function doDelete(pid){
  if(!confirm('确认删除这篇论文？将同时删除 Zotero 条目、本地缓存与知识库卡片，并同步到 ModelScope。')) return;
  postFetch('/api/library/delete', {pid}).then(r=>{
    if(r.error){ toast(r.error); return; }
    toast('删除中…');
    pollJob(r.job_id, s=>{
      if(s.status==='error') toast('删除失败：'+s.error);
      else { toast('已删除并同步 ModelScope'); closeOverlay(); loadMeta(); loadLibrary(); }
    });
  });
}

// ── 检索 ──
async function doSearch(){
  const q = $('#q').value.trim(); if(!q) return;
  const p = new URLSearchParams({q, mode:$('#mode').value, engine:$('#engine').value, top:$('#top').value});
  const r = await (await fetch('/api/search?'+p)).json();
  $('#search-note').textContent = r.note || '';
  const box = $('#results'); box.innerHTML='';
  if(!r.results.length){ box.innerHTML='<div class="card empty">无结果（可能无相关论文，或语义相似度低于阈值）</div>'; return; }
  const wrap = document.createElement('div'); wrap.className='card';
  r.results.forEach((d,i)=>{
    const el = document.createElement('div'); el.className='result';
    el.innerHTML = `<div class="idx">${i+1}</div><div class="r-body">
      <div class="t-en" onclick="showPaper('${esc(d.pid)}')">${esc(d.title_en)}</div>
      <div class="t-zh">${esc(d.title_zh)}</div>
      <div class="r-tags">${readTag(d)}<span class="tag">${esc(d.category)}</span><span class="tag">${esc(d.area)}</span><span class="tag">${esc(d.work)}</span><span class="tag">${esc(d.year)}</span><button class="btn small" onclick="markRead('${esc(d.pid)}',${!d.read})">${d.read?'标未读':'标已读'}</button></div>
      <div class="jumps">${jumpButtons(d.targets)}</div>
    </div>`;
    wrap.appendChild(el);
  });
  box.appendChild(wrap);
}

// ── 论文库 ──
async function loadLibrary(){
  const r = await (await fetch('/api/library/tree')).json();
  LIB = r; renderFacets(); renderTree();
}
function renderFacets(){
  const catBox = $('#facet-cat'); catBox.innerHTML='';
  Object.entries(LIB.category_counts||{}).forEach(([c,n])=>{
    catBox.insertAdjacentHTML('beforeend', `<button class="chip ${FILTER.cat===c?'on':''}" onclick="setFilter('cat','${esc(c)}')">${esc(c)} <span>${n}</span></button>`);
  });
  const fBox = $('#facet-field'); fBox.innerHTML='';
  Object.entries(LIB.field_counts||{}).forEach(([f,n])=>{
    fBox.insertAdjacentHTML('beforeend', `<button class="chip ${FILTER.field===f?'on':''}" onclick="setFilter('field','${esc(f)}')">${esc(f)} <span>${n}</span></button>`);
  });
}
function setFilter(kind, val){ FILTER[kind] = FILTER[kind]===val ? null : val; renderFacets(); renderTree(); }
function renderTree(){
  const box = $('#tree'); box.innerHTML='';
  if(!LIB || !LIB.total){ box.innerHTML='<div class="empty">论文库为空，先去「上传论文」或「每日简报」录入几篇</div>'; return; }
  let html='';
  for(const f of LIB.fields){
    if(FILTER.field && f.field!==FILTER.field) continue;
    let fhtml='';
    for(const a of f.areas){
      let ahtml='';
      for(const w of a.works){
        const papers = w.papers.filter(p=>!FILTER.cat || p.category===FILTER.cat);
        if(!papers.length) continue;
        ahtml += `<details><summary>${esc(w.work)} <span class="cnt">${papers.length}</span></summary><div class="papers">${papers.map(paperRow).join('')}</div></details>`;
      }
      if(ahtml) fhtml += `<details><summary>${esc(a.area)} <span class="cnt">${a.count}</span></summary>${ahtml}</details>`;
    }
    if(fhtml) html += `<details class="field" open><summary><span class="dot"></span>${esc(f.field)} <span class="cnt">${f.count}</span></summary>${fhtml}</details>`;
  }
  box.innerHTML = html || '<div class="empty">当前筛选下无论文</div>';
}
function paperRow(p){
  return `<div class="paper">
    <div class="p-title" onclick="showPaper('${esc(p.pid)}')">${esc(p.title_en)}</div>
    ${p.title_zh && p.title_zh!==p.title_en ? `<div class="p-zh">${esc(p.title_zh)}</div>`:''}
    <div class="r-tags">${readTag(p)}<span class="tag">${esc(p.category)}</span><span class="tag">${esc(p.year)}</span>${p.venue?`<span class="tag muted">${esc(p.venue)}</span>`:''}<button class="btn small" onclick="markRead('${esc(p.pid)}',${!p.read})">${p.read?'标未读':'标已读'}</button></div>
    <div class="jumps">${jumpButtons(p.targets)}</div>
  </div>`;
}
function librarySync(){
  postFetch('/api/library/sync').then(r=>{
    if(r.error){ toast(r.error); return; }
    $('#sync-status').textContent='同步中（pull + push）…';
    pollJob(r.job_id, s=>{
      $('#sync-status').textContent='';
      if(s.status==='error') toast('同步失败：'+s.error);
      else { toast('已同步 ModelScope'); loadLibrary(); loadMeta(); }
    });
  });
}

// ── 每日简报 ──
async function loadDaily(){
  const r = await (await fetch('/api/daily/latest')).json();
  DAILY = r.candidates || [];
  $('#daily-date').textContent = r.date ? '最近简报：'+r.date : '尚无简报';
  renderDaily();
}
function renderDaily(){
  const box = $('#daily-list'); box.innerHTML='';
  if(!DAILY.length){ box.innerHTML='<div class="empty">尚无每日简报，点「立即运行」生成一次</div>'; return; }
  DAILY.forEach((c,i)=>{
    const el = document.createElement('div'); el.className='cand';
    el.innerHTML = `
      <div class="cand-head"><input type="checkbox" class="cand-check" data-i="${i}" checked>
        <div class="cand-title">${esc(c.title)}</div><span class="score">${Number(c.score).toFixed(3)}</span></div>
      <div class="cand-meta"><span class="tag unread">未读</span>${esc(c.year||'?')} · ${esc(c.venue||'未知')} · 被引 ${c.citations||0}</div>
      ${c.llm_reason?`<div class="cand-reason">${esc(c.llm_reason)}</div>`:''}
      ${c.abstract?`<div class="cand-abs">${esc(c.abstract.slice(0,240))}…</div>`:''}
      <div class="cand-actions">${c.url?`<a class="jump" href="${esc(c.url)}" target="_blank" rel="noopener">原文</a>`:''}<button class="btn small primary" onclick="addToLib(${i}, this)">＋ 推入论文库</button></div>`;
    box.appendChild(el);
  });
}
function runDaily(){
  postFetch('/api/daily/run').then(r=>{
    if(r.error){ toast(r.error); return; }
    $('#daily-status').textContent='运行中（抓取+评分约 30–90 秒）…';
    pollJob(r.job_id, s=>{
      $('#daily-status').textContent='';
      if(s.status==='error') toast('运行失败：'+s.error);
      else { loadDaily(); toast('已生成今日简报'); }
    });
  });
}
function addToLib(i, btn){
  const cand = DAILY[i];
  btn.disabled = true; btn.textContent = '下载入库中…';
  postFetch('/api/daily/add', {cand}).then(r=>{
    if(r.error){ toast(r.error); btn.disabled=false; btn.textContent='＋ 推入论文库'; return; }
    pollJob(r.job_id, s=>{
      if(s.status==='error'){ toast('入库失败：'+s.error); btn.disabled=false; btn.textContent='＋ 推入论文库'; }
      else { toast('已推入论文库并同步 ModelScope'); btn.textContent='✅ 已入库'; loadMeta(); loadLibrary(); }
    });
  });
}
function sendEmail(){
  const cands = DAILY.filter((_,i)=>document.querySelector(`.cand-check[data-i="${i}"]`)?.checked);
  if(!cands.length){ toast('请勾选要发送的论文'); return; }
  const to = $('#email-to').value.trim();
  postFetch('/api/daily/email', {to, cands}).then(r=>{
    if(r.error){ toast(r.error); return; }
    toast('发送中…');
    pollJob(r.job_id, s=>{
      if(s.status==='error') toast('发送失败：'+s.error);
      else toast('已发送到邮箱');
    });
  });
}

// ── 上传论文 ──
async function ingest(){
  const inp = $('#file');
  const files = Array.from(inp.files || []);
  if(!files.length){ toast('请先选择 PDF 文件'); return; }
  const log = $('#job-log'), btn = $('#upload-btn');
  log.innerHTML = '';
  const rows = files.map(f => {
    const el = document.createElement('div');
    el.className = 'muted';
    el.textContent = f.name + ' — 排队中…';
    log.appendChild(el);
    return {f, el};
  });
  btn.disabled = true;
  let ok = 0, fail = 0;
  // 逐个顺序入库：避免并发打 DeepSeek/Zotero，也避免 ModelScope git 同步竞争
  for(const {f, el} of rows){
    el.textContent = f.name + ' — 上传中…';
    let r;
    try {
      const dataUrl = await new Promise((res, rej) => {
        const rd = new FileReader();
        rd.onload = () => res(rd.result);
        rd.onerror = () => rej(rd.error);
        rd.readAsDataURL(f);
      });
      r = await postFetch('/api/ingest', {filename:f.name, data:dataUrl.split(',')[1]});
    } catch(e){
      el.textContent = f.name + ' — ❌ 读取/上传失败：' + e;
      fail++; continue;
    }
    if(r.error){ el.textContent = f.name + ' — ❌ ' + r.error; fail++; continue; }
    el.textContent = f.name + ' — 入库中（分类→Zotero→双语卡，约 15–30 秒）…';
    const s = await new Promise(res => pollJob(r.job_id, res));
    if(s.status === 'error'){
      el.textContent = f.name + ' — ❌ ' + s.error;
      fail++;
    } else {
      el.textContent = f.name + ' — ✅ 已入库并同步 ModelScope';
      ok++;
    }
  }
  btn.disabled = false;
  inp.value = '';
  toast(`批量上传完成：成功 ${ok} 篇，失败 ${fail} 篇`);
  loadMeta(); loadLibrary();
}

// ── 对话（右侧常驻栏：馆长 agent，DeepSeek 总控 + Kimi 联网检索）──
function activeHist(){ return hist; }
function linkPid(html){
  return html.replace(/`(local:[A-Za-z0-9_.-]+|\d{4}\.\d{4,5})`/g,
    (m,pid)=>`<a class="jump" href="javascript:void(0)" onclick="showPaper('${pid}')">${pid}</a>`);
}
function appendMsg(role, content, citations){
  const box = $('#chat-box');
  const el = document.createElement('div');
  el.className = 'msg '+role;
  let inner = role==='assistant' ? linkPid(fmtMD(content)) : esc(content);
  if(role==='assistant' && citations && citations.length){
    inner += '<div class="cites">' + citations.map(c=>
      `<span class="cite" title="${esc(c.title_en)}" onclick="showPaper('${esc(c.pid)}')">📄 ${esc(c.pid)}</span>`).join('') + '</div>';
  }
  el.innerHTML = inner;
  box.appendChild(el); box.scrollTop = box.scrollHeight;
}
function addChatMsg(role, content, citations){
  activeHist().push({role, content, citations: citations||undefined});
  appendMsg(role, content, citations);
}
function renderChat(){
  const box = $('#chat-box'); box.innerHTML='';
  activeHist().forEach(m=>appendMsg(m.role, m.content, m.citations));
  box.scrollTop = box.scrollHeight;
}
function typing(bool){
  let el = $('#chat-typing');
  if(bool && !el){ el=document.createElement('div'); el.id='chat-typing'; el.className='msg assistant typing'; el.textContent = '馆长处理中…'; $('#chat-box').appendChild(el); $('#chat-box').scrollTop=$('#chat-box').scrollHeight; }
  if(!bool && el) el.remove();
}
async function sendChat(){
  const inp = $('#chat-input'); const msg = inp.value.trim(); if(!msg) return;
  inp.value='';
  addChatMsg('user', msg);
  const history = activeHist().slice(0, -1).map(m=>({role:m.role, content:m.content}));
  const r = await postFetch('/api/librarian', {message:msg, history});
  if(r.error){ addChatMsg('assistant', '❌ '+r.error); return; }
  typing(true);
  pollJob(r.job_id, s=>{
    typing(false);
    if(s.status==='error') addChatMsg('assistant', '❌ '+s.error);
    else {
      const res = s.result || {};
      addChatMsg('assistant', res.answer || '(空回答)', res.citations);
      if(res.pending_action) renderPending(res.pending_action);
    }
  });
}
function renderPending(pa){
  const box = $('#chat-box');
  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.innerHTML = `<div class="pending-box">⚠️ 计划待确认：${esc(pa.summary)}</div>
    <div class="pending-btns">
      <button class="btn primary" onclick="confirmAction('${esc(pa.action_id)}', true, this)">确认执行</button>
      <button class="btn" onclick="confirmAction('${esc(pa.action_id)}', false, this)">取消</button>
    </div>`;
  box.appendChild(el); box.scrollTop = box.scrollHeight;
}
async function confirmAction(actionId, approve, btn){
  btn.disabled = true;
  const r = await postFetch('/api/librarian/confirm', {action_id: actionId, approve});
  if(r.error){ toast('❌ '+r.error); btn.disabled = false; return; }
  toast(approve ? '执行中…' : '已取消');
  pollJob(r.job_id, s=>{
    btn.disabled = false;
    if(s.status==='error'){ toast('❌ '+s.error); return; }
    const res = s.result || {};
    if(res.cancelled) toast('已取消');
    else if(res.error) toast('❌ '+res.error);
    else if(res.executed){ toast('已执行：'+res.action); loadMeta(); loadLibrary(); }
    else toast('完成');
  });
}
function saveChat(){
  const h = activeHist().map(m=>({role:m.role, content:m.content}));
  if(!h.length){ toast('暂无对话可保存'); return; }
  postFetch('/api/chat/save', {history: h}).then(r=>{
    if(r.error){ toast(r.error); return; }
    toast('保存中…');
    pollJob(r.job_id, s=>{
      if(s.status==='error') toast('保存失败：'+s.error);
      else toast('已保存'+(s.result && s.result.pushed ? '并推送 ModelScope' : '（本地）'));
    });
  });
}
// ── 侧栏拖拽调宽 ──
(function(){
  const rail = $('#chat-rail'), rz = $('#rail-resizer');
  if(!rail || !rz) return;
  const saved = localStorage.getItem('railW');
  if(saved) rail.style.width = saved;
  rz.addEventListener('pointerdown', e=>{
    e.preventDefault();
    rz.setPointerCapture(e.pointerId);
    rz.classList.add('drag');
    const startX = e.clientX, startW = rail.getBoundingClientRect().width;
    const onMove = ev=>{
      const w = Math.max(280, Math.min(window.innerWidth*0.7, startW + (startX - ev.clientX)));
      rail.style.width = w + 'px';
    };
    const onUp = ()=>{
      rz.classList.remove('drag');
      rz.removeEventListener('pointermove', onMove);
      rz.removeEventListener('pointerup', onUp);
      rz.removeEventListener('pointercancel', onUp);
      document.body.style.cursor=''; document.body.style.userSelect='';
      localStorage.setItem('railW', rail.style.width);
    };
    document.body.style.cursor='col-resize'; document.body.style.userSelect='none';
    rz.addEventListener('pointermove', onMove);
    rz.addEventListener('pointerup', onUp);
    rz.addEventListener('pointercancel', onUp);
  });
})();

// ── 输入框拖拽调高 ──
(function(){
  const ta = $('#chat-input'), rz = $('#input-resizer');
  if(!ta || !rz) return;
  const saved = localStorage.getItem('inputH');
  if(saved) ta.style.height = saved;
  rz.addEventListener('pointerdown', e=>{
    e.preventDefault();
    rz.setPointerCapture(e.pointerId);
    rz.classList.add('drag');
    const startY = e.clientY, startH = ta.getBoundingClientRect().height;
    const onMove = ev=>{
      const maxH = Math.max(120, window.innerHeight*0.4);
      const h = Math.max(36, Math.min(maxH, startH + (startY - ev.clientY)));
      ta.style.height = h + 'px';
    };
    const onUp = ()=>{
      rz.classList.remove('drag');
      rz.removeEventListener('pointermove', onMove);
      rz.removeEventListener('pointerup', onUp);
      rz.removeEventListener('pointercancel', onUp);
      document.body.style.cursor=''; document.body.style.userSelect='';
      localStorage.setItem('inputH', ta.style.height);
    };
    document.body.style.cursor='row-resize'; document.body.style.userSelect='none';
    rz.addEventListener('pointermove', onMove);
    rz.addEventListener('pointerup', onUp);
    rz.addEventListener('pointercancel', onUp);
  });
})();

// ── 设置 ──
function cfgFieldHTML(f){
  const id = 'cfg-'+f.path.replace(/\./g,'-');
  let input;
  if(f.type==='list'){
    input = `<textarea id="${id}" data-path="${esc(f.path)}" data-type="list">${esc(f.value)}</textarea>`;
  } else {
    const t = f.type==='number' ? 'number' : 'text';
    input = `<input id="${id}" type="${t}" data-path="${esc(f.path)}" data-type="${f.type}" value="${esc(f.value)}">`;
  }
  return `<div class="field"><label for="${id}">${esc(f.label)}</label>${input}</div>`;
}
function envFieldHTML(f){
  const id = 'env-'+f.key;
  const input = `<input id="${id}" type="${f.secret?'password':'text'}" data-key="${esc(f.key)}" placeholder="${f.set?'••••••（已配置，留空保持不变）':'（未配置）'}">`;
  return `<div class="env-row"><label for="${id}">${esc(f.label)}</label>${input}<span class="clear-hint"><input type="checkbox" data-clear="${esc(f.key)}"> 清除</span></div>`;
}
async function loadSettings(){
  const s = await (await fetch('/api/settings')).json();
  $('#cfg-fields').innerHTML = (s.config_fields||[]).map(cfgFieldHTML).join('');
  $('#env-fields').innerHTML = (s.env_fields||[]).map(envFieldHTML).join('');
}
async function saveSettings(){
  const cfg = {};
  document.querySelectorAll('#cfg-fields [data-path]').forEach(el=>{
    const p = el.dataset.path, t = el.dataset.type;
    if(t==='list'){ cfg[p] = el.value.split('\n').map(x=>x.trim()).filter(Boolean); }
    else if(t==='number'){ if(el.value.trim()!=='') cfg[p] = Number(el.value); }
    else { if(el.value!=='') cfg[p] = el.value; }
  });
  const env = {};
  document.querySelectorAll('#env-fields [data-key]').forEach(el=>{
    if(el.value.trim()) env[el.dataset.key] = el.value.trim();
  });
  document.querySelectorAll('#env-fields [data-clear]').forEach(el=>{
    if(el.checked) env[el.dataset.clear] = '';
  });
  const r = await postFetch('/api/settings', {config: cfg, env: env});
  const st = $('#settings-status');
  if(r.error){ st.textContent = '保存失败：'+r.error; st.style.color='#c0392b'; return; }
  st.textContent = '✓ 已保存 '+(r.saved||[]).length+' 项（立即生效）';
  st.style.color='#1a7f37';
  toast('设置已保存');
  loadSettings(); loadMeta();
}

loadMeta();
</script>
</body>
</html>
"""


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

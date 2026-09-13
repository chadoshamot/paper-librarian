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
    POST /api/chat              Kimi 联网对话（LLM 自己联网检索并返回论文链接）
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


def _find_source_pdf(pid: str, cfg: Config):
    """由 pid 反查 papers/ 里的原始 PDF（重分类需要）。"""
    papers = cfg.inbox_dir
    if pid.startswith("local:"):
        p = papers / (pid[len("local:"):] + ".pdf")
        return p if p.exists() else None
    for f in papers.glob("*.pdf"):
        if pid in f.name:
            return f
    return None


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
    src = _find_source_pdf(pid, cfg)
    if not src:
        raise ValueError(f"找不到 {pid} 的原始 PDF（papers/ 目录缺失，无法重分类）")
    result = IngestPipeline(cfg).reclassify(src, category, area, work, year)
    _invalidate_docs()
    return _sync_after(result)


def _run_delete(pid) -> dict:
    from .pipeline import IngestPipeline
    cfg = _cfg()
    pipe = IngestPipeline(cfg)
    old = pipe.manifest.get(pid)
    if not old:
        raise ValueError(f"未找到已入库论文: {pid}")
    if old.get("zotero_key"):
        pipe.zotero.delete_item(old["zotero_key"])
    if old.get("path"):
        p = Path(old["path"])
        p.unlink(missing_ok=True)
        area = (old.get("area") or "").split("::")[-1]
        work = (old.get("work_slugs") or [""])[0]
        kb = cfg.root / "knowledge-base" / "fields" / area / work / (p.stem + ".md")
        kb.unlink(missing_ok=True)
    pipe.manifest.data["entries"].pop(pid, None)
    pipe.manifest.save()
    _invalidate_docs()
    return _sync_after({"pid": pid, "deleted": True})


def _run_chat(message: str, history: list) -> dict:
    from .chat import ChatClient
    cc = ChatClient(_cfg().config.get("chat") or {})
    msgs = [{"role": m.get("role", "user"), "content": m.get("content", "")}
            for m in (history or [])]
    msgs.append({"role": "user", "content": message})
    return cc.chat(msgs)


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
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

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
  :root{
    --bg:#f8fafc; --card:#ffffff; --ink:#1e293b; --mut:#64748b; --line:#e2e8f0;
    --acc:#2563eb; --acc-weak:#eef2ff; --cta:#ea580c; --danger:#dc2626; --danger-weak:#fef2f2;
    --radius:10px; --shadow:0 1px 3px rgba(15,23,42,.06),0 1px 2px rgba(15,23,42,.04);
    --shadow-lg:0 12px 40px rgba(15,23,42,.16);
  }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
         background:var(--bg); color:var(--ink); font-size:15px; }
  header { background:var(--card); border-bottom:1px solid var(--line); padding:16px 24px;
           display:flex; align-items:baseline; gap:14px; position:sticky; top:0; z-index:20; }
  header h1 { font-size:17px; margin:0; letter-spacing:.2px; }
  header .stat { color:var(--mut); font-size:13px; }
  nav { display:flex; gap:4px; padding:0 24px; background:var(--card); border-bottom:1px solid var(--line);
        position:sticky; top:52px; z-index:20; }
  nav button { border:0; background:none; padding:13px 18px; font-size:14px; cursor:pointer;
               border-bottom:2px solid transparent; color:var(--mut); transition:color .15s; }
  nav button:hover { color:var(--ink); }
  nav button.on { color:var(--acc); border-bottom-color:var(--acc); font-weight:600; }
  main { padding:22px 24px 60px; max-width:1240px; margin:0 auto; }
  .panel { display:none; } .panel.on { display:block; animation:fade .2s ease; }
  @keyframes fade { from{opacity:0; transform:translateY(4px)} to{opacity:1; transform:none} }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  input[type=text], input[type=number], input[type=email] { padding:10px 12px; font-size:15px;
    border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--ink); }
  input[type=text] { flex:1; min-width:200px; }
  input:focus, select:focus, button:focus-visible { outline:2px solid var(--acc); outline-offset:1px; }
  select { padding:10px 12px; font-size:14px; border:1px solid var(--line); border-radius:8px;
           background:var(--card); color:var(--ink); cursor:pointer; }
  button.btn { padding:10px 16px; font-size:14px; border:1px solid var(--line); border-radius:8px;
               background:var(--card); color:var(--ink); cursor:pointer; transition:all .15s; }
  button.btn:hover { border-color:var(--acc); color:var(--acc); }
  button.primary { background:var(--acc); color:#fff; border-color:var(--acc); }
  button.primary:hover { background:#1d4ed8; color:#fff; }
  button.danger { color:var(--danger); border-color:#fecaca; background:var(--danger-weak); }
  button.danger:hover { border-color:var(--danger); color:#fff; background:var(--danger); }
  button.ghost { border:0; background:none; padding:4px 8px; color:var(--mut); cursor:pointer; }
  button.ghost:hover { color:var(--ink); }
  button.small { padding:6px 12px; font-size:13px; }
  .muted { color:var(--mut); font-size:13px; }
  .hint { color:var(--mut); font-size:12.5px; margin-top:6px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
          padding:14px 16px; margin-top:12px; box-shadow:var(--shadow); }
  .empty { color:var(--mut); padding:28px; text-align:center; }
  .tag { display:inline-block; font-size:11.5px; padding:2px 8px; border-radius:999px;
         background:var(--acc-weak); color:var(--acc); margin-right:5px; }
  .tag.muted { background:#f1f5f9; color:var(--mut); }
  /* 跳转按钮 */
  .jumps { margin-top:6px; }
  a.jump { display:inline-block; margin:0 8px 6px 0; padding:5px 11px; font-size:12.5px;
           border:1px solid var(--line); border-radius:8px; text-decoration:none; color:var(--ink);
           background:var(--card); transition:all .12s; }
  a.jump:hover { border-color:var(--acc); color:var(--acc); }
  /* 检索结果 */
  .result { display:flex; gap:12px; align-items:flex-start; padding:13px 14px;
            border-bottom:1px solid var(--line); }
  .result:last-child { border-bottom:0; }
  .result .idx { color:var(--mut); font-weight:600; min-width:22px; }
  .r-body { flex:1; min-width:0; }
  .t-en { font-weight:600; cursor:pointer; line-height:1.4; }
  .t-en:hover { color:var(--acc); }
  .t-zh { color:var(--mut); font-size:13px; }
  .r-tags { margin-top:5px; }
  /* 论文库布局 */
  .lib-wrap { display:flex; gap:18px; align-items:flex-start; }
  .sidebar { width:220px; flex-shrink:0; position:sticky; top:118px; }
  .sidebar h4 { font-size:12px; text-transform:uppercase; letter-spacing:.6px; color:var(--mut); margin:14px 0 8px; }
  .chip { display:block; width:100%; text-align:left; border:1px solid var(--line); background:var(--card);
          border-radius:8px; padding:7px 10px; margin-bottom:6px; font-size:13.5px; cursor:pointer;
          color:var(--ink); transition:all .12s; }
  .chip span { float:right; color:var(--mut); }
  .chip:hover { border-color:var(--acc); }
  .chip.on { border-color:var(--acc); background:var(--acc-weak); color:var(--acc); font-weight:600; }
  .tree { flex:1; min-width:0; }
  .tree details { margin:2px 0; }
  .tree summary { cursor:pointer; padding:7px 10px; border-radius:8px; font-weight:600; font-size:14px;
                  list-style:none; user-select:none; transition:background .12s; }
  .tree summary::-webkit-details-marker { display:none; }
  .tree summary::before { content:"▸"; color:var(--mut); margin-right:8px; display:inline-block; transition:transform .15s; }
  .tree details[open] > summary::before { transform:rotate(90deg); }
  .tree summary:hover { background:#f1f5f9; }
  .tree details.field > summary { background:var(--card); border:1px solid var(--line); box-shadow:var(--shadow); font-size:15px; }
  .tree .cnt { color:var(--mut); font-weight:400; font-size:12px; margin-left:6px; }
  .tree .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--acc); margin-right:8px; }
  .papers { padding:2px 0 8px 22px; }
  .paper { border-left:2px solid var(--line); margin:4px 0 6px 6px; padding:8px 12px; border-radius:0 8px 8px 0; }
  .paper:hover { border-left-color:var(--acc); background:#f8fafc; }
  .p-title { font-weight:600; cursor:pointer; font-size:14px; }
  .p-title:hover { color:var(--acc); }
  .p-zh { color:var(--mut); font-size:12.5px; }
  /* 详情浮层 */
  .overlay { position:fixed; inset:0; background:rgba(15,23,42,.4); backdrop-filter:blur(2px);
             display:none; align-items:flex-start; justify-content:center; z-index:100; padding:4vh 16px; overflow-y:auto; }
  .overlay.on { display:flex; }
  .modal { background:var(--card); border-radius:14px; box-shadow:var(--shadow-lg); width:100%;
           max-width:820px; padding:22px 24px; animation:fade .2s ease; }
  .d-head { display:flex; justify-content:space-between; gap:12px; }
  .d-head h3 { margin:0 0 4px; font-size:17px; line-height:1.35; }
  .d-zh { color:var(--mut); font-size:13.5px; }
  .d-meta { margin-top:6px; color:var(--mut); font-size:13px; }
  .sec { margin-top:14px; } .sec b { font-size:13px; color:var(--mut); display:block; margin-bottom:3px; }
  .sec p { margin:0; line-height:1.6; }
  .form-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-top:8px; }
  /* 每日简报 */
  .daily-layout { display:flex; gap:18px; align-items:flex-start; }
  .daily-left { flex:1.25; min-width:0; }
  .chat-panel { flex:1; min-width:0; position:sticky; top:118px; }
  .cand { padding:14px; border-bottom:1px solid var(--line); }
  .cand:last-child { border-bottom:0; }
  .cand-head { display:flex; align-items:center; gap:10px; }
  .cand-title { font-weight:600; flex:1; line-height:1.4; }
  .cand .score { color:var(--acc); font-weight:700; font-size:13px; white-space:nowrap; }
  .cand-meta { color:var(--mut); font-size:12.5px; margin:4px 0 0 26px; }
  .cand-reason { color:var(--acc); font-size:12.5px; margin:4px 0 0 26px; }
  .cand-abs { color:var(--mut); font-size:12.5px; margin:5px 0 0 26px; line-height:1.5; }
  .cand-actions { margin:8px 0 0 26px; }
  /* 聊天 */
  .chat-box { height:440px; overflow-y:auto; padding:10px; display:flex; flex-direction:column; gap:10px; }
  .msg { max-width:85%; padding:9px 13px; border-radius:12px; font-size:14px; line-height:1.55; }
  .msg.user { align-self:flex-end; background:var(--acc); color:#fff; border-bottom-right-radius:4px; }
  .msg.assistant { align-self:flex-start; background:#f1f5f9; color:var(--ink); border-bottom-left-radius:4px; }
  .msg.typing { color:var(--mut); font-style:italic; }
  .msg a { color:var(--acc); }
  .chat-input-row { display:flex; gap:8px; margin-top:10px; }
  .chat-input-row input { flex:1; }
  pre { background:#0f172a; color:#d1fae5; padding:12px; border-radius:8px; overflow:auto;
        font-size:12px; line-height:1.5; max-height:300px; }
  #toast { position:fixed; bottom:26px; left:50%; transform:translateX(-50%) translateY(20px);
           background:#1e293b; color:#fff; padding:10px 18px; border-radius:999px; font-size:14px;
           opacity:0; pointer-events:none; transition:all .25s; z-index:200; }
  #toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
  @media (max-width:900px){
    .lib-wrap, .daily-layout { flex-direction:column; }
    .sidebar, .chat-panel { position:static; width:100%; }
  }
  @media (prefers-reduced-motion: reduce){
    * { animation:none !important; transition:none !important; }
  }
</style>
</head>
<body>
<header><h1>AI 论文管家</h1><span class="stat" id="stat"></span></header>
<nav>
  <button data-tab="search" class="on">检索</button>
  <button data-tab="library">论文库</button>
  <button data-tab="daily">每日简报</button>
  <button data-tab="upload">上传论文</button>
</nav>
<main>
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
    <div class="daily-layout">
      <div class="daily-left card" id="daily-list"></div>
      <div class="chat-panel card">
        <b style="font-size:14px">与 LLM 对话 · 联网检索</b>
        <div class="hint" style="margin:2px 0 8px">告诉它你的研究方向/想找的论文，它自己联网搜并返回论文链接。</div>
        <div class="chat-box" id="chat-box"></div>
        <div class="chat-input-row">
          <input type="text" id="chat-input" placeholder="例如：帮我找 2025 年 GPU 集群调度方向的顶会论文" onkeydown="if(event.key==='Enter')sendChat()" />
          <button class="btn primary" onclick="sendChat()">发送</button>
        </div>
      </div>
    </div>
  </section>

  <!-- 上传论文 -->
  <section id="tab-upload" class="panel">
    <div class="card">
      <b>上传并入库</b>
      <p class="muted">PDF 丢到 papers/ 并走 分类 → Zotero → 双语卡 → manifest 全流程（约 15–30 秒），完成后自动同步 ModelScope。</p>
      <div class="row"><input type="file" id="file" accept="application/pdf"><button class="btn primary" onclick="ingest()">上传并入库</button></div>
      <div id="job-log"></div>
    </div>
  </section>
</main>

<div class="overlay" id="overlay" onclick="if(event.target===this)closeOverlay()">
  <div class="modal" id="modal"></div>
</div>

<script>
const $ = s => document.querySelector(s);
let META = {categories:[], areas:[], work_slugs:[]};
let LIB = null, DAILY = [];
let chatHistory = [];
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
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));

// ── 跳转 / 详情 ──
function jumpButtons(targets){
  return (targets||[]).map(t=>{
    if(t.kind==='local') return `<a class="jump" href="${esc(t.url)}" target="_blank" rel="noopener">📄 预览</a>`;
    return `<a class="jump" href="${esc(t.url)}" target="_blank" rel="noopener">${KIND[t.kind]||t.kind}</a>`;
  }).join('');
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
        <div class="d-meta"><span class="tag">${esc(d.category)}</span><span class="tag">${esc(d.area)}</span><span class="tag">${esc(d.work)}</span><span class="tag">${esc(d.year)}</span> ${esc(d.venue||'')}</div>
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
        <iframe id="preview-frame" src="" style="width:100%;height:520px;border:1px solid var(--line);border-radius:8px;background:#fff"></iframe>
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
      <div class="r-tags"><span class="tag">${esc(d.category)}</span><span class="tag">${esc(d.area)}</span><span class="tag">${esc(d.work)}</span><span class="tag">${esc(d.year)}</span></div>
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
    <div class="r-tags"><span class="tag">${esc(p.category)}</span><span class="tag">${esc(p.year)}</span>${p.venue?`<span class="tag muted">${esc(p.venue)}</span>`:''}</div>
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
      <div class="cand-meta">${esc(c.year||'?')} · ${esc(c.venue||'未知')} · 被引 ${c.citations||0}</div>
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
  const f = $('#file').files[0];
  if(!f){ toast('请先选择 PDF 文件'); return; }
  const dataUrl = await new Promise(res=>{ const rd=new FileReader(); rd.onload=()=>res(rd.result); rd.readAsDataURL(f); });
  const r = await postFetch('/api/ingest', {filename:f.name, data:dataUrl.split(',')[1]});
  if(r.error){ toast(r.error); return; }
  $('#job-log').innerHTML = '<div class="muted">入库中（分类→Zotero→双语卡，约 15–30 秒）…</div>';
  pollJob(r.job_id, s=>{
    if(s.status==='error'){ $('#job-log').innerHTML='<pre>'+esc(s.error)+'\n'+esc(s.log||'')+'</pre>'; toast('入库失败'); }
    else { $('#job-log').innerHTML='<pre>'+esc(s.log||'')+'</pre>'; toast('已入库并同步 ModelScope'); loadMeta(); loadLibrary(); }
  });
}

// ── 对话 ──
function addChatMsg(role, content){
  chatHistory.push({role, content});
  const box = $('#chat-box');
  const el = document.createElement('div');
  el.className = 'msg '+role;
  el.innerHTML = role==='assistant' ? fmtMD(content) : esc(content);
  box.appendChild(el); box.scrollTop = box.scrollHeight;
}
function typing(bool){
  let el = $('#chat-typing');
  if(bool && !el){ el=document.createElement('div'); el.id='chat-typing'; el.className='msg assistant typing'; el.textContent='联网检索中…'; $('#chat-box').appendChild(el); $('#chat-box').scrollTop=$('#chat-box').scrollHeight; }
  if(!bool && el) el.remove();
}
async function sendChat(){
  const inp = $('#chat-input'); const msg = inp.value.trim(); if(!msg) return;
  inp.value='';
  addChatMsg('user', msg);
  const history = chatHistory.slice(0, -1);
  const r = await postFetch('/api/chat', {message:msg, history});
  if(r.error){ addChatMsg('assistant', '❌ '+r.error); return; }
  typing(true);
  pollJob(r.job_id, s=>{
    typing(false);
    if(s.status==='error') addChatMsg('assistant', '❌ '+s.error);
    else addChatMsg('assistant', (s.result && s.result.answer) || '(空回答)');
  });
}

loadMeta();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="论文管家 Web 界面")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"AI 论文管家 Web 已启动： http://{args.host}:{args.port}  （Ctrl+C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()

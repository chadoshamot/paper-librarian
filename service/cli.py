"""交互式 CLI：`python -m service` 启动（零依赖，复用检索/每日/管线/云/馆长 agent 核心）。

主菜单：
  1 检索论文库  2 论文库  3 每日简报  4 上传论文  5 云端同步  6 馆长 agent  7 查重/去重  0 退出

与网页版共用同一套核心与 config.yml（多接口共参）。所有写操作（重分类/删除/录入/推入/改元数据）
完成后可选「同步上云」。馆长 agent 是统一入口（查库/深读/联网搜索/下载/上云/删除），危险操作先确认。
"""
import json
import os
import sys
import webbrowser
from datetime import date
from pathlib import Path

from .config_loader import Config
from .retrieval import (embedding_explore, embedding_search, hybrid_search,
                        keyword_search, load_documents, resolve_targets)


# ── 交互小工具 ────────────────────────────────────────────────────────────────
def _ask(prompt, default=None):
    """读一行输入；空输入返回 default（若给）。"""
    p = f"{prompt} [{default}]" if default is not None and default != "" else prompt
    s = input(f"{p}: ").strip()
    return s if s else (default if default is not None else "")


def _ask_int(prompt, default=None):
    while True:
        s = _ask(prompt, default=default)
        if s.isdigit():
            return int(s)
        print("  请输入数字")


def _menu(title, items, default=None):
    """items: [(key, label)]；循环选择，返回选中的 key。"""
    print(f"\n=== {title} ===")
    for k, label in items:
        print(f"  {k}. {label}")
    keys = [k for k, _ in items]
    while True:
        s = _ask("选择", default=default)
        if s in keys:
            return s
        print("  无效选择，请重试")


def _meta(d):
    area = d["area"].split("::")[-1] if "::" in d["area"] else d["area"]
    return f"{d['category']}/{area}/{d['work']}/{d.get('year') or '?'}"


def _read(d):
    return "已读" if d.get("read") else "未读"


def _mark_read_prompt(cfg, docs):
    """交互式标记已读：输入序号（逗号分隔），显式人工确认才从未读→已读。"""
    from .read_state import mark, sync_read
    s = _ask("标记哪些为已读？输入序号（逗号分隔，如 1,3），回车取消", default="")
    if not s:
        return
    idxs = []
    for part in s.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(docs):
            idxs.append(int(part) - 1)
    if not idxs:
        print("  无效序号")
        return
    for i in idxs:
        mark(cfg, docs[i]["pid"], True)
    print(f"  已标记 {len(idxs)} 篇为已读。")
    sync_read()


def _open_targets(doc, cfg):
    targets = resolve_targets(doc, cfg)
    if not targets:
        print("  （无跳转目标）")
        return
    print("  跳转:")
    for i, (kind, loc) in enumerate(targets, 1):
        print(f"    {i}. {kind}: {loc}")
    s = _ask("  打开哪一个（回车跳过）", default="")
    if s.isdigit() and 1 <= int(s) <= len(targets):
        kind, loc = targets[int(s) - 1]
        print(f"  打开 [{kind}] …")
        if kind == "local":
            os.startfile(loc)  # Windows：默认程序打开本地 PDF
        else:
            webbrowser.open(loc)


def _sync_maybe():
    if _ask("同步上云？[y/N]", default="n").lower() in ("y", "yes"):
        try:
            from .cloud import sync
            sync()
        except SystemExit:
            print("[warn] 同步中止（缺 MODELSCOPE_TOKEN 等）")
        except Exception as e:
            print(f"[warn] 同步失败: {e}")


def _pick_category(cfg):
    values = cfg.taxonomy["category"]["values"]
    print("\n类别:")
    for i, v in enumerate(values, 1):
        print(f"  {i}. {v}")
    s = _ask("类别（编号或直接输入）", default="方法")
    if s.isdigit() and 1 <= int(s) <= len(values):
        return values[int(s) - 1]
    return s or "方法"


def _pick_area(cfg):
    groups = cfg.taxonomy["broad_field"]["groups"]
    opts = []
    for bf, dirs in groups.items():
        for d in dirs:
            opts.append(f"{bf}::{d}")
    print("\n大领域（编号选择，或直接输入「大领域::方向」）:")
    for i, o in enumerate(opts, 1):
        print(f"  {i:2d}. {o}")
    s = _ask("选择")
    if s.isdigit() and 1 <= int(s) <= len(opts):
        return opts[int(s) - 1]
    return s


# ── 1. 检索 ─────────────────────────────────────────────────────────────────
def _do_search(docs, query, top, mode, engine, cfg):
    if mode == "2":  # explore
        if engine == "2":
            from .retrieval import explore
            return explore(docs, query, top)
        try:
            return embedding_explore(docs, query, top, cfg)
        except Exception as e:
            print(f"（语义不可用，退回关键词探索：{e}）", file=sys.stderr)
            from .retrieval import explore
            return explore(docs, query, top)
    if engine == "2":
        return keyword_search(docs, query, top)
    if engine == "3":
        try:
            return embedding_search(docs, query, top, cfg)
        except Exception as e:
            print(f"[错误] 语义检索失败: {e}", file=sys.stderr)
            return []
    try:  # auto
        return hybrid_search(docs, query, top, cfg)
    except Exception as e:
        print(f"（语义不可用，退回关键词：{e}）", file=sys.stderr)
        return keyword_search(docs, query, top)


def _search_flow(cfg):
    print("\n=== 检索论文库 ===")
    docs = load_documents(cfg)
    if not docs:
        print("论文库为空，先「上传论文」录入。")
        return
    query = _ask("检索关键词")
    if not query:
        return
    mode = _menu("模式", [("1", "精准搜索（最相关）"),
                        ("2", "探索发现（跨子方向去重）")], default="1")
    engine = _menu("引擎", [("1", "auto（语义+关键词混合）"),
                          ("2", "纯关键词"),
                          ("3", "纯语义")], default="1")
    top = _ask_int("结果数", default=5)

    results = _do_search(docs, query, top, mode, engine, cfg)
    if not results:
        print(f"（无结果：{query}）")
        return
    for i, d in enumerate(results, 1):
        print(f"\n{i}. {d['title_en']}  [{_meta(d)}] · {_read(d)}")
        if d["title_zh"] and d["title_zh"] != d["title_en"]:
            print(f"   {d['title_zh']}")
        core = (d.get("core_zh") or "").strip()
        if core:
            print(f"   {core[:120]}")
    while True:
        s = _ask("输入序号打开原文 / r 标记已读 / 回车返回", default="")
        if not s:
            return
        if s.lower() == "r":
            _mark_read_prompt(cfg, results)
            continue
        if s.isdigit() and 1 <= int(s) <= len(results):
            _open_targets(results[int(s) - 1], cfg)
        else:
            print("  无效序号")


# ── 2. 论文库 ───────────────────────────────────────────────────────────────
def _library_flow(cfg):
    while True:
        docs = load_documents(cfg)
        if not docs:
            print("\n论文库为空，先「上传论文」录入。")
            return
        print(f"\n=== 论文库（共 {len(docs)} 篇）===")
        for i, d in enumerate(docs, 1):
            print(f"  {i:2d}. {d['title_en']}  [{_meta(d)}] · {_read(d)}")
        print("  r. 标记已读   b. 返回主菜单")
        s = _ask("输入序号查看/操作")
        if s.lower() == "r":
            _mark_read_prompt(cfg, docs)
            continue
        if s.lower() == "b":
            return
        if s.isdigit() and 1 <= int(s) <= len(docs):
            _doc_detail(docs[int(s) - 1], cfg)
        else:
            print("  无效序号")


def _doc_detail(doc, cfg):
    print("\n───── 详情 ─────")
    print(f"标题(en): {doc['title_en']}")
    if doc["title_zh"]:
        print(f"标题(zh): {doc['title_zh']}")
    print(f"分类: {_meta(doc)}")
    print(f"venue: {doc.get('venue') or '?'} | arxiv: {doc.get('arxiv_id') or '-'} | pid: {doc['pid']} | 状态: {_read(doc)}")
    core_zh = (doc.get("core_zh") or "").strip()
    core_en = (doc.get("core_en") or "").strip()
    if core_zh:
        print(f"核心: {core_zh[:200]}")
    if core_en:
        print(f"Core:  {core_en[:200]}")

    while True:
        print("\n  [1] 跳转原文  [2] 重分类  [3] 删除  [4] 标记已读/未读  [5] 改元数据  [6] 重抓元数据  [b] 返回")
        a = _ask("操作")
        if a == "1":
            _open_targets(doc, cfg)
        elif a == "2":
            _reclassify(doc, cfg)
            return
        elif a == "3":
            _delete(doc, cfg)
            return
        elif a == "4":
            _toggle_read(doc, cfg)
            return
        elif a == "5":
            _edit_metadata(doc, cfg)
            return
        elif a == "6":
            _fix_metadata(doc, cfg)
            return
        elif a.lower() == "b":
            return
        else:
            print("  无效选择")


def _toggle_read(doc, cfg):
    from .read_state import mark, sync_read
    new = not doc.get("read")
    mark(cfg, doc["pid"], new)
    print(f"  已标记为{'已读' if new else '未读'}。")
    sync_read()


def _reclassify(doc, cfg):
    from .pipeline import IngestPipeline
    pid = doc["pid"]
    category = _pick_category(cfg)
    area = _pick_area(cfg)
    if not category or not area:
        return
    work = _ask("work slug（英文，如 slo-aware-scheduling）")
    year = _ask_int("年份", default=date.today().year)
    print(f"\n重分类: {category} / {area} / {work} / {year}")
    if _ask("确认？[y/N]", default="n").lower() not in ("y", "yes"):
        return
    try:
        result = IngestPipeline(cfg).reclassify(pid, category, area, work, year)
    except Exception as e:
        print(f"[错误] {e}")
        return
    for k, v in result.items():
        print(f"  {k}: {v}")
    _sync_maybe()


def _delete(doc, cfg):
    from .pipeline import IngestPipeline
    print(f"\n将删除: {doc['title_en']}  [{_meta(doc)}]")
    if _ask("确认删除（含 Zotero 条目 + 本地缓存 + KB 卡片）？[y/N]", default="n").lower() not in ("y", "yes"):
        return
    try:
        IngestPipeline(cfg).delete(doc["pid"])
    except Exception as e:
        print(f"[错误] {e}")
        return
    print("已删除。")
    _sync_maybe()


def _edit_metadata(doc, cfg):
    from .pipeline import IngestPipeline
    print("\n修改元数据（回车保留原值）:")
    title_en = _ask("英文标题", default=doc.get("title_en") or "")
    title_zh = _ask("中文标题", default=doc.get("title_zh") or "")
    venue = _ask("venue", default=doc.get("venue") or "")
    year = _ask("年份", default=str(doc.get("year") or ""))
    fields = {}
    if title_en and title_en != doc.get("title_en"):
        fields["title_en"] = title_en
    if title_zh and title_zh != doc.get("title_zh"):
        fields["title_zh"] = title_zh
    if venue and venue != doc.get("venue"):
        fields["venue"] = venue
    if year.isdigit() and int(year) != doc.get("year"):
        fields["year"] = int(year)
    if not fields:
        print("  没有变化。")
        return
    try:
        result = IngestPipeline(cfg).update_metadata(doc["pid"], **fields)
    except Exception as e:
        print(f"[错误] {e}")
        return
    print(f"  已更新: {', '.join(result.get('updated', []))}")


def _fix_metadata(doc, cfg):
    from .pipeline import IngestPipeline
    print(f"解析 {doc['pid']} 的真实标题（arXiv 元数据 / PDF 元数据 / 首页文本）…")
    try:
        result = IngestPipeline(cfg).fix_title(doc["pid"])
    except Exception as e:
        print(f"[错误] {e}")
        return
    print(f"  标题(en): {result.get('title_en')}")
    print(f"  标题(zh): {result.get('title_zh')}")


# ── 3. 每日简报 ─────────────────────────────────────────────────────────────
def _daily_flow(cfg):
    cands: list[dict] = []
    while True:
        print("\n=== 每日简报 ===")
        print("  1. 查看最新日报")
        print("  2. 立即运行检索")
        print("  3. 发送到邮箱")
        print("  4. 推入论文库")
        print("  5. 馆长 agent")
        print("  b. 返回主菜单")
        s = _ask("选择")
        if s == "1":
            _show_latest_daily(cfg)
        elif s == "2":
            cands = _run_daily(cfg)
        elif s == "3":
            _email_daily(cfg, cands)
        elif s == "4":
            _push_daily(cfg, cands)
        elif s == "5":
            _librarian_flow(cfg)
        elif s.lower() == "b":
            return
        else:
            print("  无效选择")


def _show_latest_daily(cfg):
    out_dir = cfg.root / "reports" / "daily"
    mds = sorted(out_dir.glob("*.md"))
    if not mds:
        print("暂无日报，先「立即运行检索」。")
        return
    print("\n" + mds[-1].read_text(encoding="utf-8"))


def _run_daily(cfg):
    from . import daily as daily_mod
    daily_cfg = cfg.config["daily"]
    sources = list(daily_cfg.get("sources", []))
    top_k = daily_cfg.get("top_k", 10)
    use_llm = _ask("使用 LLM 裁判打分？[Y/n]", default="y").lower() in ("y", "yes")
    print(f"\n运行每日检索（源 {sources}，top {top_k}，LLM={use_llm}）…")
    cands = daily_mod.run(cfg, sources, top_k, use_llm=use_llm)
    if not cands:
        print("\n今日无新候选论文。")
        return []
    out_dir = cfg.root / "reports" / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    (out_dir / f"{today}.md").write_text(daily_mod.render_md(cands, cfg), encoding="utf-8")
    (out_dir / f"{today}.json").write_text(
        json.dumps(cands, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n" + "=" * 60)
    for i, c in enumerate(cands, 1):
        print(f"{i:2d}. [{c['score']:.3f}] {c['title']}")
        print(f"     {c.get('year') or '?'} · {c.get('venue') or '未知'} · 被引{c.get('citations') or 0} · 未读 · {c.get('url') or ''}")
    print("=" * 60)
    print(f"日报已写入 {out_dir / (today + '.md')}")
    return cands


def _load_latest_cands(cfg):
    out_dir = cfg.root / "reports" / "daily"
    jsons = sorted(out_dir.glob("*.json"))
    if not jsons:
        return []
    return json.loads(jsons[-1].read_text(encoding="utf-8"))


def _email_daily(cfg, cands):
    from .emailer import send_daily
    if not cands:
        cands = _load_latest_cands(cfg)
        if not cands:
            print("无候选。先「立即运行检索」生成候选。")
            return
    to = _ask("收件邮箱", default=os.environ.get("SMTP_TO") or "")
    try:
        r = send_daily(cands, to or None)
        print(f"已发送 {r['count']} 篇到 {r['to']}")
    except Exception as e:
        print(f"[错误] {e}")


def _push_daily(cfg, cands):
    from .daily import fetch_candidate_pdf
    from .pipeline import IngestPipeline
    if not cands:
        cands = _load_latest_cands(cfg)
        if not cands:
            print("无候选。先「立即运行检索」生成候选。")
            return
    print("\n候选论文:")
    for i, c in enumerate(cands, 1):
        print(f"  {i:2d}. {c['title']}")
    s = _ask("选择要推入论文库的编号（回车返回）", default="")
    if not (s.isdigit() and 1 <= int(s) <= len(cands)):
        return
    cand = cands[int(s) - 1]
    print(f"下载并入库: {cand['title']}")
    try:
        pdf = fetch_candidate_pdf(cand, cfg.inbox_dir)
    except Exception as e:
        print(f"[错误] {e}")
        return
    try:
        result = IngestPipeline(cfg).run(pdf)
    except Exception as e:
        print(f"[错误] {e}")
        return
    for k, v in result.items():
        print(f"  {k}: {v}")
    _sync_maybe()


# ── 4. 上传论文 ─────────────────────────────────────────────────────────────
def _upload_flow(cfg):
    from .pipeline import IngestPipeline
    print("\n=== 上传论文（录入）===")
    print("把 PDF 丢进 papers/ 目录，或直接输入其路径。")
    inbox = cfg.inbox_dir
    pdfs = sorted(inbox.glob("*.pdf"))
    if pdfs:
        print(f"\npapers/ 里的 PDF（{len(pdfs)}）:")
        for i, p in enumerate(pdfs, 1):
            print(f"  {i:2d}. {p.name}")
    else:
        print("（papers/ 目录暂无 PDF）")
    s = _ask("输入编号（多选逗号分隔，如 1,2）/ all / PDF 路径")
    if not s:
        return
    targets = []
    if s.lower() == "all":
        targets = pdfs
    elif "," in s:
        for part in s.split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(pdfs):
                targets.append(pdfs[int(part) - 1])
    elif s.isdigit() and 1 <= int(s) <= len(pdfs):
        targets = [pdfs[int(s) - 1]]
    else:
        p = Path(s)
        if p.exists() and p.suffix.lower() == ".pdf":
            targets = [p]
        else:
            print("[错误] 找不到该 PDF")
            return
    pipe = IngestPipeline(cfg)
    for pdf in targets:
        print(f"\n=== 处理: {pdf.name} ===")
        try:
            result = pipe.run(pdf)
            for k, v in result.items():
                print(f"  {k}: {v}")
        except Exception as e:
            print(f"  [错误] {type(e).__name__}: {e}")
    _sync_maybe()


# ── 5. 云端同步 ─────────────────────────────────────────────────────────────
def _cloud_flow(cfg):
    print("\n=== 云端同步（ModelScope）===")
    print("  1. 同步上云（push 增量）")
    print("  2. 拉取云端（pull）")
    print("  3. 拉取 + 同步（pull+push）")
    print("  b. 返回")
    s = _ask("选择")
    if s == "1":
        _do_sync(pull_first=False)
    elif s == "2":
        _do_pull()
    elif s == "3":
        _do_pull()
        _do_sync(pull_first=False)


def _do_sync(pull_first=False):
    from .cloud import sync, pull
    try:
        if pull_first:
            pull()
        sync()
    except SystemExit:
        print("[warn] 缺 MODELSCOPE_TOKEN（.env）")
    except Exception as e:
        print(f"[错误] {e}")


def _do_pull():
    from .cloud import pull
    try:
        pull()
    except SystemExit:
        print("[warn] 缺 MODELSCOPE_TOKEN（.env）")
    except Exception as e:
        print(f"[错误] {e}")


# ── 查重 / 去重 ──────────────────────────────────────────────────────────────
def _dedup_flow(cfg):
    from .dedup import analyze, execute
    print("\n=== 查重 / 去重 ===")
    try:
        res = analyze(cfg)
    except Exception as e:
        print(f"[错误] {e}")
        return
    definite = res.get("definite") or []
    likely = res.get("likely") or []
    if not definite and not likely:
        print("没有发现重复论文。")
        return
    if definite:
        print(f"\n内容级重复（{len(definite)} 组，可安全去重）:")
        for i, g in enumerate(definite, 1):
            victims = [p for p in g["pids"] if p != g["keep"]]
            print(f"  {i}. 保留 `{g['keep']}`，删除: {', '.join(victims)}")
            for p in g["pids"]:
                print(f"       - {p}: {g['titles'].get(p)}")
    if likely:
        print(f"\n疑似重复（仅标题相同，{len(likely)} 组，建议人工确认，不自动删）:")
        for g in likely:
            print("  - " + " / ".join(g["pids"]))
    if definite and _ask("执行去重（删除重复篇并同步 ModelScope）？[y/N]", default="n").lower() in ("y", "yes"):
        groups = [{"pids": g["pids"], "keep": g["keep"]} for g in definite]
        try:
            r = execute(cfg, groups)
            print(f"  已保留 {len(r['kept'])} 篇，删除 {len(r['removed'])} 篇。")
        except Exception as e:
            print(f"[错误] {e}")


# ── 6. 馆长 agent / 对话保存 ────────────────────────────────────────────────
def _save_chat(cfg, history):
    if not history:
        print("  暂无对话可保存。")
        return
    try:
        from .chatlog import save_and_sync
        r = save_and_sync(history, cfg)
        print(f"  已保存对话: {r['path']}")
        if r["pushed"]:
            print("  已推送到 ModelScope（knowledge-base/chat-logs/）。")
        else:
            print("  [提示] 未推送云端（缺 MODELSCOPE_TOKEN 或 knowledge-base 未初始化 git），已存本地。")
    except Exception as e:
        print(f"  [错误] 保存失败: {e}")


def _librarian_flow(cfg):
    from .librarian import Librarian, confirm
    try:
        lib = Librarian(cfg)
    except Exception as e:
        print(f"[错误] 馆长 agent 初始化失败: {e}")
        return
    print("\n=== 馆长 agent（统一入口）===")
    print("查库 / 深读全文 / 联网搜索(Kimi) / 下载入库 / 上云 / 删除，凡网页能做的它都能做。")
    print("输入问题即可；exit 返回，save 保存对话。危险操作会先列计划等你确认。")
    history = []
    while True:
        q = input("\n你: ").strip()
        if not q:
            continue
        if q.lower() in ("exit", "quit", "q", "0"):
            if history and _ask("保存本次对话到 ModelScope？[y/N]", default="n").lower() in ("y", "yes"):
                _save_chat(cfg, history)
            return
        if q.lower() in ("save", "s"):
            _save_chat(cfg, history)
            continue
        print("  馆长处理中…")
        try:
            res = lib.ask(q, history)
        except Exception as e:
            print(f"  [错误] {type(e).__name__}: {e}")
            continue
        ans = res.get("answer") or "（无回答）"
        print(f"\n馆长:\n{ans}")
        cites = res.get("citations") or []
        if cites:
            print("  [引用] " + ", ".join(c["pid"] for c in cites))
        pending = res.get("pending_action")
        if pending:
            print(f"\n  ⚠️ 计划待确认：{pending['summary']}")
            if _ask("确认执行？[y/N]", default="n").lower() in ("y", "yes"):
                try:
                    r = confirm(pending["action_id"], True)
                except Exception as e:
                    r = {"error": f"{type(e).__name__}: {e}"}
            else:
                r = confirm(pending["action_id"], False)
            print("  执行结果:", json.dumps(r, ensure_ascii=False, default=str))
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": ans})


# ── 主菜单 ──────────────────────────────────────────────────────────────────
_FUNCS = {
    "1": _search_flow,
    "2": _library_flow,
    "3": _daily_flow,
    "4": _upload_flow,
    "5": _cloud_flow,
    "6": _librarian_flow,
    "7": _dedup_flow,
}


def main():
    print("=" * 60)
    print("  AI 论文管家（本地交互式 CLI）")
    print("=" * 60)
    try:
        cfg = Config()
    except Exception as e:
        print(f"[错误] 加载配置失败: {e}")
        sys.exit(1)

    while True:
        n = len(load_documents(cfg))
        try:
            choice = _menu(f"AI 论文管家 · 已收录 {n} 篇", [
                ("1", "检索论文库"),
                ("2", "论文库（浏览 / 跳转 / 重分类 / 改元数据 / 删除）"),
                ("3", "每日简报（查看 / 运行 / 发邮箱 / 推入论文库 / 馆长 agent）"),
                ("4", "上传论文（录入，支持多选）"),
                ("5", "云端同步（ModelScope）"),
                ("6", "馆长 agent（查库 / 深读 / 联网搜索 / 下载 / 上云 / 删除）"),
                ("7", "查重 / 去重"),
                ("0", "退出"),
            ])
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if choice == "0":
            print("再见。")
            break
        try:
            _FUNCS[choice](cfg)
        except KeyboardInterrupt:
            print("\n（已中断，返回主菜单）")
        except EOFError:
            print("\n再见。")
            break
        except Exception as e:
            print(f"[错误] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()

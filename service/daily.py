"""每日检索（P3）：从 arxiv / openalex / semantic_scholar 抓候选论文，
去重（对本库 + 跨源），按质量评分排序，输出日报。

用法：
    python -m service.daily                          # 全量跑一遍，打印 + 写日报
    python -m service.daily --top 10 --no-llm        # 跳过 LLM 裁判（省 token / 离线）
    python -m service.daily --sources openalex       # 只跑指定源

质量评分（权重见 config.yml quality.weights）：
    venue（顶会顶刊层级）+ relevance（与本库语义相似）+ citation（年均被引）
    + recency（年份新鲜度）+ novelty（与本库已有工作的差异度）+ llm_judge（LLM 打分）
"""
import argparse
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

from .config_loader import Config
from .llm import LLM
from .retrieval import _cos, _embed_model, embed_documents, load_documents

UA = {"User-Agent": "paper-librarian/0.1 (personal research agent)"}


# ── HTTP 小工具 ────────────────────────────────────────────────────────────
def _http_json(url, headers=None, timeout=25, retries=2):
    """GET 一个 JSON 端点，带退避重试；失败返回 None。"""
    hdrs = dict(UA)
    if headers:
        hdrs.update(headers)
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    return None


def _reconstruct_abstract(inv: dict | None) -> str:
    """OpenAlex 的 abstract_inverted_index → 纯文本。"""
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def _norm_title(t: str) -> str:
    """标题归一化（去标点/空格/大小写），用于模糊去重。"""
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


# ── 源抓取 ─────────────────────────────────────────────────────────────────
def fetch_arxiv(query: str, limit: int, recent_days: int) -> list[dict]:
    """arXiv API（本机 IP 常被 429 限流，失败返回空列表）。"""
    base = "http://export.arxiv.org/api/query"
    params = urllib.parse.urlencode({
        "search_query": f'all:"{query}"',
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": limit,
    })
    try:
        import feedparser
        req = urllib.request.Request(f"{base}?{params}", headers=UA)
        with urllib.request.urlopen(req, timeout=25) as resp:
            feed = feedparser.parse(resp.read())
    except Exception:
        return []
    out = []
    for e in feed.entries:
        arxiv_id = e.id.rsplit("/abs/", 1)[-1]
        out.append({
            "id": f"arxiv:{arxiv_id}", "arxiv_id": arxiv_id, "doi": "",
            "title": re.sub(r"\s+", " ", e.title).strip(),
            "abstract": re.sub(r"\s+", " ", e.summary).strip(),
            "authors": [a.name for a in e.authors],
            "year": int(e.published[:4]) if getattr(e, "published", "") else None,
            "venue": "arXiv", "citations": 0,
            "url": f"https://arxiv.org/abs/{arxiv_id}", "source": "arxiv",
        })
    return out


def fetch_openalex(query: str, limit: int, recent_days: int) -> list[dict]:
    """OpenAlex（国内可达，含被引次数；主力源）。"""
    since = (date.today() - timedelta(days=recent_days)).isoformat()
    params = urllib.parse.urlencode({
        "search": query,
        "per-page": limit,
        "sort": "relevance_score:desc",   # 相关度优先；被引次数仅作评分成分，不参与排序
        "filter": f"from_publication_date:{since}",
        "mailto": "shamotsu@gmail.com",
    })
    data = _http_json(f"https://api.openalex.org/works?{params}")
    out = []
    for w in (data or {}).get("results", []):
        doi = (w.get("doi") or "").removeprefix("https://doi.org/")
        src = (w.get("primary_location") or {}).get("source") or {}
        authors = [a.get("author", {}).get("display_name", "")
                   for a in (w.get("authorships") or [])]
        out.append({
            "id": w.get("id", ""), "arxiv_id": "", "doi": doi,
            "title": (w.get("display_name") or "").strip(),
            "abstract": _reconstruct_abstract(w.get("abstract_inverted_index")),
            "authors": [a for a in authors if a],
            "year": w.get("publication_year"),
            "venue": src.get("display_name") or "arXiv",
            "citations": int(w.get("cited_by_count") or 0),
            "url": doi and f"https://doi.org/{doi}" or (w.get("id") or ""),
            "source": "openalex",
        })
    return out


def fetch_semantic_scholar(query: str, limit: int, recent_days: int) -> list[dict]:
    """Semantic Scholar（国内可达但偶发 429）。"""
    params = urllib.parse.urlencode({
        "query": query,
        "limit": limit,
        "fields": "title,abstract,year,authors,venue,citationCount,externalIds",
    })
    data = _http_json(f"https://api.semanticscholar.org/graph/v1/paper/search?{params}")
    out = []
    for p in (data or {}).get("data", []):
        ext = p.get("externalIds") or {}
        arxiv_id = ext.get("ArXiv") or ""
        doi = ext.get("DOI") or ""
        out.append({
            "id": arxiv_id and f"arxiv:{arxiv_id}" or doi and f"doi:{doi}" or f"ss:{p.get('paperId')}",
            "arxiv_id": arxiv_id, "doi": doi,
            "title": (p.get("title") or "").strip(),
            "abstract": (p.get("abstract") or "").strip(),
            "authors": [a.get("name") for a in (p.get("authors") or [])],
            "year": p.get("year"),
            "venue": p.get("venue") or "arXiv",
            "citations": int(p.get("citationCount") or 0),
            "url": arxiv_id and f"https://arxiv.org/abs/{arxiv_id}"
                   or doi and f"https://doi.org/{doi}" or "",
            "source": "semantic_scholar",
        })
    return out


_SOURCES = {
    "arxiv": fetch_arxiv,
    "openalex": fetch_openalex,
    "semantic_scholar": fetch_semantic_scholar,
}


def fetch_candidate_pdf(cand: dict, inbox_dir) -> Path:
    """下载候选论文 PDF 到 papers/，返回路径（供「推入论文库」调用）。

    优先 arXiv PDF（arxiv.org/pdf/{id}）；无 arXiv ID 但有 DOI 时，经 OpenAlex
    查开放获取（oa_url/pdf_url）。都失败则抛异常。注意本机 arXiv PDF 下载
    也可能被 429 限流（与 API 同源），失败会友好报错。
    """
    inbox = Path(inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)

    pdf_url = fname = None
    arxiv_id = cand.get("arxiv_id") or ""
    if arxiv_id:
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        fname = f"arXiv{arxiv_id}.pdf"
    else:
        doi = (cand.get("doi") or "").strip()
        if doi:
            oa = _http_json(f"https://api.openalex.org/works/https://doi.org/{doi}")
            loc = (oa or {}).get("open_access") or {}
            pdf_url = loc.get("oa_url") or loc.get("pdf_url")
            if pdf_url:
                fname = (doi.replace("/", "_") + ".pdf")
    if not pdf_url:
        raise RuntimeError(f"无法获取 PDF：{cand.get('title', '')}（无 arXiv ID 且无开放获取）")

    dest = inbox / fname
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(pdf_url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            if not data or len(data) < 1000:
                raise RuntimeError("PDF 内容过短（可能被限流/反爬）")
            dest.write_bytes(data)
            print(f"  [下载] {pdf_url} → {dest.name}（{len(data) / 1024:.0f} KB）")
            return dest
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"下载失败：{pdf_url}（{type(last).__name__}）")


# ── 去重 ───────────────────────────────────────────────────────────────────
def _cand_key(c: dict) -> str:
    """跨源去重键：arxiv id > doi > 归一化标题。"""
    if c.get("arxiv_id"):
        return f"arxiv:{c['arxiv_id']}"
    if c.get("doi"):
        return f"doi:{c['doi'].lower()}"
    return f"title:{_norm_title(c['title'])}"


def merge_candidates(items: list[dict]) -> list[dict]:
    """合并同一论文的多个来源记录，保留信息最全的一条。"""
    by_key: dict[str, dict] = {}
    for c in items:
        k = _cand_key(c)
        old = by_key.get(k)
        if old is None:
            by_key[k] = dict(c)
        else:
            # 信息更全（摘要更长 / 被引更高 / 有 doi）者胜出
            if (len(c.get("abstract") or "") > len(old.get("abstract") or "")
                    or (c.get("citations") or 0) > (old.get("citations") or 0)):
                by_key[k] = {**old, **{kk: vv for kk, vv in c.items() if vv}}
    return list(by_key.values())


def _arxiv_from_doi(doi: str) -> str:
    m = re.search(r"arxiv\.(\d{4}\.\d{4,5})", (doi or "").lower())
    return m.group(1) if m else ""


def filter_known(cands: list[dict], docs: list[dict]) -> list[dict]:
    """去掉本库已有的论文（按 arxiv id / doi / 标题模糊匹配）。"""
    existing_arxiv = {d["arxiv_id"] for d in docs if d.get("arxiv_id")}
    existing_titles = [_norm_title(d["title_en"]) for d in docs if d.get("title_en")]

    def known(c: dict) -> bool:
        if c.get("arxiv_id") in existing_arxiv:
            return True
        if _arxiv_from_doi(c.get("doi") or "") in existing_arxiv:
            return True
        nt = _norm_title(c["title"])
        if not nt:
            return False
        for et in existing_titles:
            if nt == et or (len(nt) > 12 and SequenceMatcher(None, nt, et).ratio() > 0.9):
                return True
        return False

    return [c for c in cands if not known(c)]


# ── 质量评分 ────────────────────────────────────────────────────────────────
# 全名 → 层级（OpenAlex 常给期刊/会议全名而非缩写，做一层关键词映射）
_FULLNAME_TIER = {
    # T0：AI + 系统顶会顶刊
    "neural information processing systems": "T0",
    "international conference on machine learning": "T0",
    "learning representations": "T0",
    "association for computational linguistics": "T0",
    "empirical methods in natural language processing": "T0",
    "computer vision and pattern recognition": "T0",
    "international conference on computer vision": "T0",
    "european conference on computer vision": "T0",
    "artificial intelligence": "T0",  # AAAI / IJCAI
    "knowledge discovery and data mining": "T0",
    "information retrieval": "T0",  # SIGIR
    "operating systems design and implementation": "T0",
    "symposium on operating systems principles": "T0",
    "networked systems design and implementation": "T0",
    "architectural support for programming languages and operating systems": "T0",
    "european conference on computer systems": "T0",
    "usenix annual technical conference": "T0",
    "machine learning and systems": "T0",
    "journal of machine learning research": "T0",
    "pattern analysis and machine intelligence": "T0",
    "transactions of the association for computational linguistics": "T0",
    # T1：数据库 / 并行与高性能计算
    "very large data bases": "T1",
    "management of data": "T1",  # SIGMOD
    "data engineering": "T1",  # ICDE
    "supercomputing": "T1",
    "high performance distributed computing": "T1",
    "international conference on supercomputing": "T1",
    "parallel and distributed systems": "T1",  # TPDS
    "architecture and code optimization": "T1",  # TACO
}

_TIER_SCORE = {"T0": 1.0, "T1": 0.7, "T2": 0.4, "T3": 0.2}


def _venue_score(venue: str, cfg: Config) -> float:
    v = (venue or "").lower().strip()
    if not v or "arxiv" in v:
        return _TIER_SCORE["T3"]
    # 1) 全名关键词
    for frag, tier in _FULLNAME_TIER.items():
        if frag in v:
            return _TIER_SCORE[tier]
    # 2) 缩写整词匹配（\b 边界，避免 "SC" 误配 "diSCover"/"science"）
    for tier in ("T0", "T1"):
        for name in cfg.config["quality"]["venue_rank"].get(tier, []):
            if re.search(rf"\b{re.escape(name.lower())}\b", v):
                return _TIER_SCORE[tier]
    return 0.1  # 未知会议/期刊


def _citation_score(c: dict, current_year: int) -> float:
    cites = c.get("citations") or 0
    age = max(1, current_year - (c.get("year") or current_year) + 1)
    return min(1.0, math.log1p(cites / age) / math.log1p(50))


def _recency_score(c: dict, current_year: int) -> float:
    year = c.get("year") or current_year
    return max(0.0, 1.0 - (current_year - year) / 5.0)


def _relevance_score(cand_vec, centroid) -> float:
    if centroid is None:
        return 0.0
    return max(0.0, float(_cos(cand_vec, centroid)))


def _novelty_score(cand_vec, doc_vecs) -> float:
    if not doc_vecs:
        return 0.5
    return 1.0 - max(float(_cos(cand_vec, v)) for v in doc_vecs)


def _llm_judge(llm: LLM, cands: list[dict], taxonomy_summary: str) -> dict[str, tuple[float, str]]:
    """让 LLM 给一组候选打「与研究兴趣契合度」0-1 分，返回 {id: (score, reason)}。"""
    lines = []
    for i, c in enumerate(cands):
        lines.append(
            f"[{i}] {c['title']} ({c.get('year') or '?'}, {c.get('venue') or '未知'})\n"
            f"    摘要: {(c.get('abstract') or '')[:300]}"
        )
    prompt = f"""你是论文筛选助手。用户研究方向是机器学习系统（GPU 集群调度 / SLO 推理服务 / 共置 / 抢占）。

下面是一批候选论文，请按「与用户研究方向的契合度」给每篇打 0.0~1.0 分：
1.0=高度相关必读，0.5=沾边，0.0=无关。只输出一个 JSON 数组，元素形如
{{"i": 0, "score": 0.85, "reason": "一句话理由（中文）"}}，不要任何其它文字。

候选列表：
{chr(10).join(lines)}"""
    raw = llm.chat([{"role": "user", "content": prompt}], task="judge")
    raw = raw.strip().strip("`")
    if raw.lower().startswith("json"):
        raw = raw[4:]
    try:
        arr = json.loads(raw.strip())
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", raw, re.S)
        arr = json.loads(m.group(0)) if m else []
    result = {}
    for item in arr:
        try:
            idx = int(item.get("i"))
            result[cands[idx]["id"]] = (float(item.get("score", 0.5)), item.get("reason", ""))
        except (KeyError, ValueError, TypeError, IndexError):
            continue
    return result


def score_candidates(cands: list[dict], docs: list[dict], cfg: Config,
                     llm: LLM | None, use_llm: bool = True,
                     min_relevance: float = 0.25) -> list[dict]:
    """给候选打分，返回带 score 字段、按 score 降序的列表。

    先算无 LLM 的成分（含 relevance），把 relevance 低于下限的候选过滤掉，
    再只对留存候选做 LLM 裁判（省 token），最后加权求和。
    """
    weights = cfg.config["quality"]["weights"]
    current_year = date.today().year

    # 本库向量（相关度=到质心的相似；新颖度=到最近已有论文的差异）
    centroid = None
    doc_vecs = []
    try:
        indexed = embed_documents(docs, cfg)
        vecs = [v for _, v in indexed]
        if vecs:
            doc_vecs = vecs
            centroid = np.mean(np.stack(vecs), axis=0)
    except Exception:
        pass

    cand_texts = [f"{c['title']}. {(c.get('abstract') or '')[:400]}" for c in cands]
    try:
        cand_vecs = list(_embed_model().embed(cand_texts))
    except Exception:
        cand_vecs = [None] * len(cands)

    # 1) 基础成分（无 LLM）
    for i, c in enumerate(cands):
        v = cand_vecs[i] if cand_vecs else None
        c["components"] = {
            "venue": _venue_score(c.get("venue"), cfg),
            "relevance": _relevance_score(v, centroid) if v is not None else 0.0,
            "citation": _citation_score(c, current_year),
            "recency": _recency_score(c, current_year),
            "novelty": _novelty_score(v, doc_vecs) if v is not None else 0.5,
            "llm_judge": 0.5,
        }

    # 2) 相关度下限过滤（无关论文不进入 LLM 裁判）
    kept = [c for c in cands if c["components"]["relevance"] >= min_relevance]
    dropped = len(cands) - len(kept)
    if dropped:
        print(f"  [过滤] 相关度低于 {min_relevance} 丢弃 {dropped} 篇")

    # 3) LLM 裁判（只对留存候选）
    judges = {}
    if use_llm and llm is not None and kept:
        try:
            judges = _llm_judge(llm, kept, "")
        except Exception as e:
            print(f"  [warn] LLM 裁判失败（跳过）: {e}")

    # 4) 加权求和
    for c in kept:
        c["components"]["llm_judge"] = judges.get(c["id"], (0.5, ""))[0]
        c["llm_reason"] = judges.get(c["id"], (0.5, ""))[1]
        c["score"] = sum(weights.get(k, 0) * v for k, v in c["components"].items())
    kept.sort(key=lambda x: -x["score"])
    return kept


# ── 主流程 ─────────────────────────────────────────────────────────────────
def run(cfg: Config, sources: list[str], top_k: int, use_llm: bool = True,
        per_query: int | None = None, recent_days: int | None = None) -> list[dict]:
    daily = cfg.config["daily"]
    queries = daily.get("queries") or []
    per_query = per_query or daily.get("per_query", 6)
    recent_days = recent_days or daily.get("recent_days", 180)

    docs = load_documents(cfg)
    print(f"[本库] 现有 {len(docs)} 篇；兴趣点 {len(queries)} 个；源 {sources}")

    raw: list[dict] = []
    for src in sources:
        fetcher = _SOURCES[src]
        for q in queries:
            try:
                items = fetcher(q, per_query, recent_days)
                for it in items:
                    it["query"] = q
                raw.extend(items)
                print(f"  [{src}] {q!r} → {len(items)} 条")
            except Exception as e:
                print(f"  [warn] {src} {q!r} 失败: {type(e).__name__}: {e}")

    cands = merge_candidates(raw)
    print(f"[去重] 跨源合并后 {len(cands)} 篇候选")
    cands = filter_known(cands, docs)
    print(f"[去重] 剔除本库已有后剩 {len(cands)} 篇")

    if not cands:
        print("[结果] 无新候选论文")
        return []

    llm = None
    if use_llm:
        try:
            llm = LLM(cfg.model)
        except Exception as e:
            print(f"  [warn] LLM 不可用，跳过裁判: {e}")
            llm = None

    cands = score_candidates(cands, docs, cfg, llm, use_llm=use_llm and llm is not None,
                             min_relevance=daily.get("min_relevance", 0.25))
    return cands[:top_k]


def render_md(cands: list[dict], cfg: Config) -> str:
    weights = cfg.config["quality"]["weights"]
    lines = [
        f"# 每日论文检索 {date.today().isoformat()}",
        "",
        f"本库已收录 {len(load_documents(cfg))} 篇，以下为按质量评分排序的新候选（共 {len(cands)} 篇）。",
        "",
    ]
    for i, c in enumerate(cands, 1):
        authors = ", ".join((c.get("authors") or [])[:3]) + (" 等" if len(c.get("authors") or []) > 3 else "")
        lines += [
            f"## {i}. {c['title']}",
            f"- **年份** {c.get('year') or '?'} · **venue** {c.get('venue') or '未知'} · **被引** {c.get('citations') or 0}",
            f"- **作者** {authors}",
            f"- **链接** {c.get('url') or ''}",
            f"- **来源** {c.get('source')} · 查询 `{c.get('query', '')}`",
            f"- **综合分** {c['score']:.3f}（venue {c['components']['venue']:.2f} · relevance {c['components']['relevance']:.2f} · citation {c['components']['citation']:.2f} · recency {c['components']['recency']:.2f} · novelty {c['components']['novelty']:.2f} · llm {c['components']['llm_judge']:.2f}）",
        ]
        if c.get("llm_reason"):
            lines.append(f"- **LLM** {c['llm_reason']}")
        if c.get("abstract"):
            lines += ["", (c["abstract"] or "")[:400], ""]
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="每日论文检索")
    ap.add_argument("--top", type=int, default=None, help="输出条数（默认读 config daily.top_k）")
    ap.add_argument("--sources", default=None, help="逗号分隔，如 openalex,arxiv")
    ap.add_argument("--per-query", type=int, default=None)
    ap.add_argument("--recent-days", type=int, default=None)
    ap.add_argument("--no-llm", action="store_true", help="跳过 LLM 裁判")
    ap.add_argument("--json", action="store_true", help="同时写一份 JSON")
    args = ap.parse_args()

    cfg = Config()
    daily = cfg.config["daily"]
    top_k = args.top or daily.get("top_k", 10)
    sources = [s.strip() for s in (args.sources or ",".join(daily.get("sources", []))).split(",") if s.strip()]
    sources = [s for s in sources if s in _SOURCES]
    if not sources:
        print("[错误] 无可用源")
        sys.exit(1)

    cands = run(cfg, sources, top_k, use_llm=not args.no_llm,
                per_query=args.per_query, recent_days=args.recent_days)

    if not cands:
        print("\n今日无新候选论文。")
        return

    out_dir = cfg.root / "reports" / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_md(cands, cfg)
    md_path = out_dir / f"{date.today().isoformat()}.md"
    md_path.write_text(md, encoding="utf-8")

    if args.json:
        json_path = out_dir / f"{date.today().isoformat()}.json"
        json_path.write_text(json.dumps(cands, ensure_ascii=False, indent=2, default=str),
                             encoding="utf-8")

    print("\n" + "=" * 70)
    for i, c in enumerate(cands, 1):
        print(f"{i:2d}. [{c['score']:.3f}] {c['title']}")
        print(f"     {c.get('year') or '?'} · {c.get('venue') or '未知'} · 被引{c.get('citations') or 0} · {c.get('url') or ''}")
    print("=" * 70)
    print(f"日报已写入 {md_path}")


if __name__ == "__main__":
    main()

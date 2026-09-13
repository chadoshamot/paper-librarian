"""检索核心：从知识库抽取文档、关键词/语义检索、解析跳转目标。

- `keyword_search`：零依赖关键词检索（精确词命中加权）。
- `embedding_search` / `embedding_explore`：语义向量检索（fastembed + jina-v2-base-zh，
  双语，768 维），带向量索引缓存与 MMR 去重。
"""
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import numpy as np
import yaml

_EMBED_MODEL = None
_EMBED_NAME = "jinaai/jina-embeddings-v2-base-zh"
MIN_SIM = 0.30  # 语义相似度阈值：top-1 低于此判为「无相关结果」

_SECTIONS = [
    ("core_zh", "核心内容（中文）"),
    ("core_en", "Core idea (English)"),
    ("sig_zh", "意义（中文）"),
    ("sig_en", "Significance (English)"),
]


def load_documents(config) -> list[dict]:
    """从 knowledge-base/fields/**/*.md 解析出检索文档（始终读源文件，无陈旧）。"""
    fields_dir = config.root / "knowledge-base" / "fields"
    docs = []
    for md in sorted(fields_dir.rglob("*.md")):
        parts = md.read_text(encoding="utf-8").split("---", 2)
        if len(parts) < 3:
            continue
        fm = yaml.safe_load(parts[1]) or {}
        body = parts[2]

        def section(title):
            m = re.search(rf"## {re.escape(title)}\s*\n(.*?)(?=\n## |\Z)", body, re.S)
            return m.group(1).strip() if m else ""

        doc = {
            "pid": fm.get("id"),
            "title_en": fm.get("title_en", ""),
            "title_zh": fm.get("title_zh", ""),
            "category": fm.get("category", ""),
            "area": fm.get("area", ""),
            "work": (fm.get("work_slugs") or [""])[0],
            "year": fm.get("year"),
            "venue": fm.get("venue", ""),
            "arxiv_id": fm.get("arxiv_id") or "",
            "zotero_key": fm.get("zotero_key", ""),
            "pdf_relative": fm.get("pdf_relative", ""),
        }
        for key, heading in _SECTIONS:
            doc[key] = section(heading)
        # 归一化空值：PyYAML 把字面量 `None` 解析成字符串 "None"
        for key in ("arxiv_id", "zotero_key", "pdf_relative"):
            if doc[key] in (None, "", "None", "none", "null", "NULL"):
                doc[key] = ""
        docs.append(doc)
    return docs


def _searchable(doc: dict) -> str:
    return " ".join([
        doc["title_zh"], doc["title_en"], doc["area"], doc["work"],
        doc["core_zh"], doc["core_en"], doc["sig_zh"], doc["sig_en"],
    ]).lower()


def _tokens(query: str) -> list[str]:
    """查询分词：ASCII 词 + 中文二元组（比单字更判别）。"""
    q = query.lower()
    toks = re.findall(r"[a-z0-9]+", q)
    for run in re.findall(r"[一-鿿]+", q):
        toks.extend(run[i:i + 2] for i in range(len(run) - 1)) if len(run) >= 2 else toks.append(run)
    return toks


def keyword_search(docs: list[dict], query: str, top_k: int) -> list[dict]:
    """零依赖关键词检索：标题/领域命中加权，按相关度+年份排序。"""
    toks = _tokens(query)
    if not toks:
        return []
    scored = []
    for doc in docs:
        title = (doc["title_zh"] + " " + doc["title_en"] + " " + doc["area"] + " " + doc["work"]).lower()
        body = _searchable(doc)
        score = 0
        for t in toks:
            if t in title:
                score += 3
            elif t in body:
                score += 1
        if score:
            scored.append((score, doc))
    scored.sort(key=lambda x: (-x[0], -(x[1].get("year") or 0)))
    return [d for _, d in scored[:top_k]]


def explore(docs: list[dict], query: str, top_k: int) -> list[dict]:
    """推荐模式：在关键词结果基础上按 work 去重，跨子方向分散。"""
    ranked = keyword_search(docs, query, len(docs))
    picked, seen_work = [], set()
    for d in ranked:
        if d["work"] in seen_work:
            continue
        picked.append(d)
        seen_work.add(d["work"])
        if len(picked) >= top_k:
            break
    return picked


def _embed_model():
    """惰性加载嵌入模型（进程内缓存）。"""
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ.setdefault("FASTEMBED_CACHE_PATH",
                              str(Path(__file__).resolve().parent.parent / "models"))
        from fastembed import TextEmbedding
        _EMBED_MODEL = TextEmbedding(model_name=_EMBED_NAME)
    return _EMBED_MODEL


def _doc_text(doc: dict) -> str:
    return " ".join([
        doc["title_zh"], doc["title_en"], doc["area"], doc["work"],
        doc["core_zh"], doc["core_en"], doc["sig_zh"], doc["sig_en"],
    ])


def _fingerprint(docs: list[dict]) -> str:
    h = hashlib.md5()
    for d in sorted(docs, key=lambda x: x["pid"]):
        h.update(f"{d['pid']}|{_doc_text(d)}".encode("utf-8"))
    return h.hexdigest()


def _embed_paths(config):
    idx = config.root / "index"
    return idx / "embeddings.npy", idx / "embed_pids.json", idx / "embed_fp.txt"


def embed_documents(docs: list[dict], config, force: bool = False) -> list[tuple[dict, np.ndarray]]:
    """对全部文档做向量化，结果缓存到 index/；内容指纹未变则直接读缓存。"""
    npy_path, pids_path, fp_path = _embed_paths(config)
    fp = _fingerprint(docs)
    if (not force and npy_path.exists() and pids_path.exists()
            and fp_path.exists() and fp_path.read_text() == fp):
        vecs = np.load(npy_path)
        pids = json.loads(pids_path.read_text())
        by_pid = {d["pid"]: d for d in docs}
        return [(by_pid[pid], vecs[i]) for i, pid in enumerate(pids) if pid in by_pid]
    model = _embed_model()
    vecs = np.array(list(model.embed([_doc_text(d) for d in docs])), dtype="float32")
    npy_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(npy_path, vecs)
    pids_path.write_text(json.dumps([d["pid"] for d in docs], ensure_ascii=False), encoding="utf-8")
    fp_path.write_text(fp, encoding="utf-8")
    return list(zip(docs, vecs))


def _cos(a, b) -> float:
    a = np.asarray(a, dtype="float32")
    b = np.asarray(b, dtype="float32")
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def embedding_search(docs: list[dict], query: str, top_k: int, config,
                     min_sim: float = MIN_SIM) -> list[dict]:
    """语义检索：query 向量与文档向量余弦相似度 top-k，低于阈值返回空。"""
    indexed = embed_documents(docs, config)
    q = np.array(list(_embed_model().embed([query]))[0], dtype="float32")
    scored = sorted(((_cos(q, v), d) for d, v in indexed), key=lambda x: -x[0])
    if not scored or scored[0][0] < min_sim:
        return []
    return [d for _, d in scored[:top_k]]


def hybrid_search(docs: list[dict], query: str, top_k: int, config,
                  min_sim: float = MIN_SIM) -> list[dict]:
    """混合检索：语义相似度 + 标题关键词命中加分（rerank 的轻量替代），低于阈值返回空。"""
    indexed = embed_documents(docs, config)
    q = np.array(list(_embed_model().embed([query]))[0], dtype="float32")
    toks = set(_tokens(query))
    scored = []
    for d, v in indexed:
        sim = _cos(q, v)
        title = (d["title_zh"] + " " + d["title_en"]).lower()
        boost = 0.15 * sum(1 for t in toks if t in title)
        scored.append((sim + boost, sim, d))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][1] < min_sim:
        return []
    return [d for _, _, d in scored[:top_k]]


def embedding_explore(docs: list[dict], query: str, top_k: int, config,
                      lambda_: float = 0.7, min_sim: float = MIN_SIM) -> list[dict]:
    """推荐模式：MMR（相关性与多样性折中），跨子方向分散结果。"""
    indexed = embed_documents(docs, config)
    q = np.array(list(_embed_model().embed([query]))[0], dtype="float32")
    sim_q = [_cos(q, v) for _, v in indexed]
    if not sim_q or max(sim_q) < min_sim:
        return []
    remaining = list(range(len(indexed)))
    selected = []
    while remaining and len(selected) < top_k:
        best_i, best_s = None, None
        for i in remaining:
            rel = sim_q[i]
            div = max((_cos(indexed[i][1], indexed[j][1]) for j in selected), default=0.0)
            s = lambda_ * rel - (1 - lambda_) * div
            if best_s is None or s > best_s:
                best_i, best_s = i, s
        selected.append(best_i)
        remaining.remove(best_i)
    return [indexed[i][0] for i in selected]


def resolve_targets(doc: dict, config) -> list[tuple[str, str]]:
    """跳转目标链，按 config open_original.order 顺序（local→arxiv→doi→cloud→zotero）。"""
    cloud_cfg = config.config.get("storage", {}).get("cloud", {})
    pdf_repo = cloud_cfg.get("pdf_repo", "")
    rel = doc.get("pdf_relative") or ""
    local = (config.cache_dir / rel) if rel else None
    targets = {
        "local": str(local) if local and local.exists() else None,
        "arxiv": f"https://arxiv.org/abs/{doc['arxiv_id']}" if doc.get("arxiv_id") else None,
        "doi": f"https://doi.org/10.48550/arXiv.{doc['arxiv_id']}" if doc.get("arxiv_id") else None,
        "cloud": (f"https://www.modelscope.cn/datasets/{pdf_repo}/resolve/master/"
                  f"{quote(rel, safe='/')}") if pdf_repo and rel else None,
        "zotero": (f"zotero://select/library/items/{doc['zotero_key']}"
                   if doc.get("zotero_key") else None),
    }
    order = config.config.get("open_original", {}).get(
        "order", ["local", "arxiv", "doi", "cloud", "zotero"])
    return [(k, targets[k]) for k in order if targets.get(k)]

"""论文去重：按内容（arxiv_id / DOI / PDF 哈希 / 归一化标题）检测重复，安全合并。

判定分两级：
- definite：同 arxiv_id / DOI / PDF 内容哈希 → 内容级几乎肯定是同一篇，可自动合并（仍需确认）。
- likely：仅归一化标题相同 → 可能同名不同文，只报告供人工判断，不自动删。

去重后调用 cloud.sync() 把删除同步到 ModelScope（两个仓库 git add -A 会提交删除）。
"""
import hashlib
import re
from pathlib import Path

from .metadata import is_noncompliant_title
from .retrieval import load_documents


def normalize_arxiv_id(arxiv_id) -> str | None:
    """归一化 arXiv ID：去版本号，如 2211.17192v2 → 2211.17192。"""
    s = str(arxiv_id or "").strip()
    if not s:
        return None
    m = re.search(r"(\d{4}\.\d{4,5})", s)
    return m.group(1) if m else None


def normalize_title(title) -> str:
    """归一化标题：小写、去标点、压缩空白。"""
    t = str(title or "").lower()
    t = re.sub(r"[^a-z0-9一-鿿]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def pdf_hash(path) -> str | None:
    """PDF 内容 SHA-256；文件缺失/读取失败返回 None。"""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _local_pdf_path(doc, config):
    rel = doc.get("pdf_relative") or ""
    return (config.cache_dir / rel) if rel else None


def _identity_keys(doc, config) -> list[str]:
    """一篇论文的「身份键」：任一键命中即视为同一篇。"""
    keys = []
    arxiv = normalize_arxiv_id(doc.get("arxiv_id"))
    if arxiv:
        keys.append(f"arxiv:{arxiv}")
        keys.append(f"doi:10.48550/arXiv.{arxiv}")
    pdf = _local_pdf_path(doc, config)
    h = pdf_hash(pdf) if pdf else None
    if h:
        keys.append(f"hash:{h}")
    title_en = doc.get("title_en") or ""
    if title_en and not is_noncompliant_title(title_en):
        t = normalize_title(title_en)
        if t:
            keys.append(f"title:{t}")
    return keys


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _groups(pids, keys_by_pid, key_filter):
    """按身份键做并查集，返回重复组（≥2 篇），每组已排序。

    key_filter(k) 决定哪些键参与连通（内容键 / 标题键分别调用）。
    """
    uf = _UnionFind()
    key2pids = {}
    for pid in pids:
        uf.find(pid)
        for k in keys_by_pid.get(pid, []):
            if not key_filter(k):
                continue
            key2pids.setdefault(k, []).append(pid)
    for members in key2pids.values():
        for m in members[1:]:
            uf.union(members[0], m)
    by_root = {}
    for pid in pids:
        by_root.setdefault(uf.find(pid), []).append(pid)
    return [sorted(v) for v in by_root.values() if len(v) >= 2]


def _is_content_key(k: str) -> bool:
    return k.startswith(("arxiv:", "doi:", "hash:"))


def _is_title_key(k: str) -> bool:
    return k.startswith("title:")


def choose_keeper(docs_by_pid: dict, pids: list) -> str:
    """在重复组里选一篇保留：优先有 arxiv_id、已读、信息更全，再优先非 local。"""
    def score(p):
        d = docs_by_pid[p]
        s = 0.0
        if d.get("arxiv_id"):
            s += 100
        if d.get("read"):
            s += 10
        if d.get("venue"):
            s += 2
        if d.get("year"):
            s += 1
        s += min(len(d.get("title_en") or ""), 300) / 300.0
        return s
    return max(pids, key=lambda p: (score(p), not p.startswith("local:")))


def analyze(config) -> dict:
    """扫描全库，返回 definite / likely 两类重复组。

    - definite：共享 arxiv_id / DOI / PDF 内容哈希（内容级，可安全合并）。
    - likely：仅归一化标题相同（可能同名不同文，只报告不自动删）。

    标题键只在「未落入任何内容级组」的论文里连通，避免一篇仅同名、
    内容不同的论文被并查集传递性地卷进内容级删除组。
    """
    docs = load_documents(config)
    docs_by_pid = {d["pid"]: d for d in docs}
    keys_by_pid = {d["pid"]: _identity_keys(d, config) for d in docs}
    pids = list(docs_by_pid)

    def entry(grp, reason):
        keep = choose_keeper(docs_by_pid, grp)
        return {
            "pids": grp,
            "keep": keep,
            "reason": reason,
            "titles": {p: docs_by_pid[p].get("title_en") for p in grp},
        }

    definite = []
    covered = set()
    for grp in _groups(pids, keys_by_pid, _is_content_key):
        definite.append(entry(grp, "content"))
        covered.update(grp)

    likely = []
    remaining = [p for p in pids if p not in covered]
    for grp in _groups(remaining, keys_by_pid, _is_title_key):
        likely.append(entry(grp, "title"))

    return {"definite": definite, "likely": likely,
            "definite_count": len(definite), "likely_count": len(likely)}


def execute(config, groups) -> dict:
    """执行去重：每组保留 keep、删除其余；转移已读状态；随后同步 ModelScope。"""
    from .cloud import sync
    from .pipeline import IngestPipeline
    from .read_state import mark

    docs_by_pid = {d["pid"]: d for d in load_documents(config)}
    pipe = IngestPipeline(config)
    kept, removed = [], []
    for g in groups:
        pids = g["pids"]
        keep = g.get("keep") or choose_keeper(docs_by_pid, pids)
        victims = [p for p in pids if p != keep]
        if any(docs_by_pid.get(v, {}).get("read") for v in victims):
            mark(config, keep, True)
        for v in victims:
            try:
                pipe.delete(v)
                removed.append(v)
            except Exception as e:
                print(f"[dedup] 删除 {v} 失败: {e}")
        kept.append(keep)
    try:
        sync()
    except BaseException as e:
        print(f"[dedup] 同步 ModelScope 失败（本地已删）: {e}")
    return {"kept": kept, "removed": removed}

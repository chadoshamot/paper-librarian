"""从文件名提取元数据，并用 arXiv API 补全摘要。"""
import json
import re
import time
import urllib.request
from pathlib import Path

import feedparser

ARXIV_RE = re.compile(r"[Aa]r[Xx]iv[_\s]?(\d{4}\.\d{4,5})")
VENUE_YEAR_RE = re.compile(r"_([A-Za-z]+)(\d{4})\.pdf$")


def parse_filename(path: Path) -> dict:
    """从文件名解析粗略元数据：title / venue / year / arxiv_id。"""
    name = path.name
    stem = path.stem
    m = ARXIV_RE.search(name)
    arxiv_id = m.group(1) if m else None

    venue = year = None
    vy = VENUE_YEAR_RE.search(name)
    if vy:
        venue = vy.group(1)
        year = int(vy.group(2))

    title = stem
    title = re.sub(r"[_\s]?[Aa]r[Xx]iv[_\s]?\d+\.\d+.*$", "", title)
    title = re.sub(r"_[A-Za-z]+\d{4}$", "", title)
    title = title.replace("_", " ").strip()
    return {"title": title, "venue": venue, "year": year, "arxiv_id": arxiv_id}


def year_from_arxiv_id(arxiv_id: str | None) -> int | None:
    """从 arXiv ID（YYMM.NNNN 新格式）推导年份，如 2303.13803 → 2023。"""
    if not arxiv_id:
        return None
    m = re.match(r"(\d{2})\d{2}\.\d+", arxiv_id)
    return 2000 + int(m.group(1)) if m else None


def fetch_arxiv(arxiv_id: str):
    """补全标题/摘要/作者/年份：先 Semantic Scholar（国内更可达），失败再 arXiv API。均失败返回 None。"""
    return _fetch_semantic_scholar(arxiv_id) or _fetch_arxiv_api(arxiv_id)


def _fetch_semantic_scholar(arxiv_id: str, retries: int = 3) -> dict | None:
    """按 arXiv ID 从 Semantic Scholar 抓元数据（无需鉴权，国内可达）。"""
    url = f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}?fields=title,abstract,year,authors"
    headers = {"User-Agent": "paper-librarian/0.1 (personal research agent)"}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not data.get("title"):
                return None
            return {
                "title": data["title"],
                "abstract": data.get("abstract") or "",
                "authors": [a["name"] for a in data.get("authors", [])],
                "year": data.get("year"),
                "arxiv_id": arxiv_id,
                "url": f"https://arxiv.org/abs/{arxiv_id}",
            }
        except Exception:
            time.sleep(2 * (attempt + 1))  # SS 限流（约 1req/s）：退避重试
    return None


def _fetch_arxiv_api(arxiv_id: str, retries: int = 2) -> dict | None:
    """arXiv 官方 API 兜底（本机 IP 常被 429 限流）。"""
    url = f"https://export.arxiv.org/api/query?id_list={arxiv_id}"
    headers = {"User-Agent": "paper-librarian/0.1 (personal research agent)"}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                feed = feedparser.parse(resp.read())
        except Exception:
            feed = None
        if feed and feed.entries:
            e = feed.entries[0]
            return {
                "title": re.sub(r"\s+", " ", e.title).strip(),
                "abstract": re.sub(r"\s+", " ", e.summary).strip(),
                "authors": [a.name for a in e.authors],
                "year": int(e.published[:4]),
                "arxiv_id": arxiv_id,
                "url": e.link,
            }
        time.sleep(3 * (attempt + 1))
    return None

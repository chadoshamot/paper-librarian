"""PDF 纯文本抽取（供馆长 deep_read 使用）。

纯 Python（pypdf），无编译依赖；抽取失败时静默返回空串，由调用方优雅降级。
"""
from __future__ import annotations

import logging
from pathlib import Path

# 部分论文 PDF 含 CFF Type1 字体，pypdf 会打印一堆 fontTools 告警；压到 ERROR 保持日志干净。
logging.getLogger("pypdf").setLevel(logging.ERROR)


def extract_text(path, max_chars: int = 16000) -> str:
    """抽取 PDF 正文文本，截断到 max_chars（约够 LLM 做深度分析）。"""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(p))
        parts: list[str] = []
        total = 0
        for page in reader.pages:
            t = page.extract_text() or ""
            if not t.strip():
                continue
            parts.append(t)
            total += len(t)
            if total >= max_chars:
                break
        return "\n\n".join(parts)[:max_chars]
    except Exception:
        return ""


def extract_document(path, max_chars=60000, max_pages=30):
    """Sample across the document with page references and explicit coverage."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        total = len(reader.pages)
        if not total:
            return {"text": "", "total_pages": 0, "pages_read": [], "truncated": False}
        indices = list(range(total)) if total <= max_pages else sorted(set(
            [0, 1, total - 2, total - 1] + [round(i * (total - 1) / (max_pages - 5)) for i in range(max_pages - 4)]))
        quota = max_chars // len(indices)
        parts, pages = [], []
        truncated = len(indices) < total
        for index in indices:
            text = reader.pages[index].extract_text() or ""
            if text.strip():
                pages.append(index + 1)
                truncated |= len(text) > quota
                parts.append(f"[PDF 第 {index + 1} 页]\n{text[:quota]}")
        return {"text": "\n\n".join(parts), "total_pages": total,
                "pages_read": pages, "truncated": truncated or len(pages) < total}
    except Exception:
        return {"text": "", "total_pages": 0, "pages_read": [], "truncated": True}

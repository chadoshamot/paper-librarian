"""Local research requirements, shared by the UI, agent and daily retrieval."""
import hashlib
import re
import threading
from pathlib import Path

import yaml

from .storage import atomic_write

_LOCK = threading.RLock()
FILENAME = "research-profile.md"
SOURCES = {"arxiv", "openalex", "semantic_scholar"}


class ProfileConflict(ValueError):
    pass


def parse(content):
    if not isinstance(content, str) or len(content) > 50000:
        raise ValueError("需求必须是小于 50000 字符的 Markdown 文本")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)(.*)\Z", content, re.S)
    if not match:
        raise ValueError("文件须以 YAML frontmatter 开头（--- 分隔），其后填写研究需求")
    try:
        options = yaml.safe_load(match[1])
    except yaml.YAMLError as exc:
        raise ValueError("需求文件 YAML 格式错误") from exc
    if not isinstance(options, dict):
        raise ValueError("需求配置须为对象")
    for key in ("queries", "sources"):
        values = options.get(key)
        if not isinstance(values, list) or not values or len(values) > 30 or any(
                not isinstance(v, str) or not v.strip() or len(v) > 300 for v in values):
            raise ValueError(f"{key} 须包含 1–30 个非空文本项")
        options[key] = list(dict.fromkeys(v.strip() for v in values))
    if not set(options["sources"]) <= SOURCES:
        raise ValueError("检索源仅支持 arxiv / openalex / semantic_scholar")
    for key, default, low, high in (("top_k", 10, 1, 100), ("recent_days", 180, 1, 3650),
                                   ("per_query", 6, 1, 100)):
        value = options.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} 须为 {low}–{high} 的整数")
        options[key] = value
    value = options.get("min_relevance", 0.25)
    if type(value) not in (int, float) or not 0 <= value <= 1:
        raise ValueError("min_relevance 须为 0–1 的数字")
    options["min_relevance"] = value
    return options, match[2].strip()


def _revision(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load(config):
    path = config.root / FILENAME
    with _LOCK:
        if not path.exists():
            daily = config.config.get("daily", {})
            options = {k: daily.get(k, default) for k, default in (
                ("queries", ["machine learning systems"]),
                ("sources", ["openalex", "arxiv", "semantic_scholar"]),
                ("top_k", 10), ("recent_days", 180), ("per_query", 6), ("min_relevance", 0.25))}
            content = "---\n" + yaml.safe_dump(options, allow_unicode=True, sort_keys=False)
            content += "---\n\n# 我的论文需求\n\n## 研究目标\n\n请填写当前课题、希望解决的问题。\n\n## 推荐偏好\n\n请填写优先方法、实验要求、关注会议及不希望推荐的内容。\n"
            parse(content)
            atomic_write(path, content)
        content = path.read_text(encoding="utf-8")
        options, body = parse(content)
        return {"content": content, "revision": _revision(content), "options": options,
                "requirements": body, "filename": FILENAME}


def save(config, content, revision):
    parse(content)
    with _LOCK:
        current = load(config)
        if revision != current["revision"]:
            raise ProfileConflict("需求已被其他窗口或 agent 修改，请重新加载后保存")
        atomic_write(config.root / FILENAME, content)
        return load(config)

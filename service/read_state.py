"""已读/未读状态：持久化到 knowledge-base/read_status.yml，随 KB 仓库同步到 ModelScope。

key 用论文稳定标识 pid（= 知识库 frontmatter 的 id：arxiv id 或 local:{stem}）。
默认未读（不在集合里）；标记已读需显式调用 mark()，任何视图都只读、不自动翻转。

用 .yml 而非 .json：knowledge-base/.gitattributes 把 *.json 归为 git-lfs，
而 read_status.yml 是几 KB 的纯文本，没必要走 LFS。
"""
from pathlib import Path

import yaml


def _path(config) -> Path:
    return config.root / "knowledge-base" / "read_status.yml"


def read_set(config) -> set:
    """返回已读 pid 集合（文件缺失/损坏时为空集）。"""
    p = _path(config)
    if not p.exists():
        return set()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return set()
    if isinstance(data, dict):
        return set(data.get("read") or [])
    return set(data or [])


def mark(config, pid, read=True) -> set:
    """标记/取消已读，写回磁盘，返回新的已读集合。"""
    p = _path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    s = read_set(config)
    if read:
        s.add(pid)
    else:
        s.discard(pid)
    p.write_text(yaml.safe_dump({"version": 1, "read": sorted(s)},
                                allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    return s


def sync_read(config) -> bool:
    """把已读状态推送到 ModelScope（knowledge-base 仓库）。失败返回 False，不抛。"""
    try:
        from .cloud import push_kb
        return push_kb("chore: 更新已读状态")
    except Exception as e:
        print(f"[read_state] 同步已读状态失败（已存本地）: {e}")
        return False

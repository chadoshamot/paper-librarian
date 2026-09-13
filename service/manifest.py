"""manifest：稳定 ID → 路径 / Zotero key / 云端 URL 的映射。"""
import json
from pathlib import Path


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {"version": 1, "entries": {}}

    def save(self):
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def upsert(self, pid: str, **fields):
        self.data["entries"].setdefault(pid, {}).update(fields)
        self.save()

    def get(self, pid: str):
        return self.data["entries"].get(pid)

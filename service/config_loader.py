"""加载 .env / config.yml / taxonomy.yml，解析 ${VAR} 占位符。"""
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


class Config:
    def __init__(self, root: Path = ROOT):
        self.root = root
        load_dotenv(root / ".env")
        self.config = self._load(root / "knowledge-base" / "config.yml")
        self.taxonomy = self._load(root / "knowledge-base" / "taxonomy.yml")
        self.config = self._resolve(self.config)

    @staticmethod
    def _load(path: Path):
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def _resolve(self, obj):
        if isinstance(obj, dict):
            return {k: self._resolve(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._resolve(v) for v in obj]
        if isinstance(obj, str):
            return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), obj)
        return obj

    @property
    def zotero_user_id(self):
        return self.config["zotero"]["user_id"]

    @property
    def zotero_api_key(self):
        return self.config["zotero"]["api_key"]

    @property
    def model(self):
        return self.config["model"]

    def local_path(self, key):
        p = self.config["storage"]["local"][key]
        if p.startswith("./"):
            p = p[2:]
        return (self.root / p).resolve()

    @property
    def cache_dir(self):
        return self.local_path("cache_dir")

    @property
    def inbox_dir(self):
        return self.local_path("inbox_dir")

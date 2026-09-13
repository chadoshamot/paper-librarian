"""跳转原文：python -m service.jump <pid> [--kind local|arxiv|doi|zotero]"""
import argparse
import os
import sys
import webbrowser

from .config_loader import Config
from .retrieval import load_documents, resolve_targets


def main():
    ap = argparse.ArgumentParser(description="按论文 ID 一键打开原文")
    ap.add_argument("pid")
    ap.add_argument("--kind", default=None, choices=["local", "arxiv", "doi", "cloud", "zotero"])
    args = ap.parse_args()

    config = Config()
    doc = next((d for d in load_documents(config) if d["pid"] == args.pid), None)
    if not doc:
        print(f"[未找到] {args.pid}")
        sys.exit(1)

    targets = resolve_targets(doc, config)
    if not targets:
        print("[无跳转目标]")
        sys.exit(1)

    chosen = next((t for t in targets if t[0] == args.kind), None) if args.kind else targets[0]
    kind, loc = chosen
    print(f"打开 [{kind}] {loc}")
    if kind == "local":
        if sys.platform == "win32":
            os.startfile(loc)  # Windows：用默认程序打开本地 PDF
        else:
            webbrowser.open(loc)  # Linux/macOS：交给系统默认打开
    else:
        webbrowser.open(loc)


if __name__ == "__main__":
    main()

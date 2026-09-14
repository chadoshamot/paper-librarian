"""检索 CLI：python -m service.search "GPU 共享" [--top 3] [--mode search|explore] [--engine auto|keyword|embedding]

默认 engine=auto：优先语义向量检索，不可用时退回关键词检索。
"""
import argparse
import sys

from .config_loader import Config
from .retrieval import (embed_documents, embedding_explore, embedding_search,
                        hybrid_search, keyword_search, load_documents, resolve_targets)


def main():
    ap = argparse.ArgumentParser(description="检索论文库并展示跳转目标")
    ap.add_argument("query")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--mode", default="search", choices=["search", "explore"])
    ap.add_argument("--engine", default="auto", choices=["auto", "keyword", "embedding"])
    ap.add_argument("--rebuild", action="store_true", help="强制重建向量索引")
    args = ap.parse_args()

    config = Config()
    docs = load_documents(config)

    if args.engine in ("auto", "embedding"):
        try:
            if args.rebuild:
                embed_documents(docs, config, force=True)
            if args.mode == "explore":
                results = embedding_explore(docs, args.query, args.top, config)
            elif args.engine == "auto":
                results = hybrid_search(docs, args.query, args.top, config)
            else:
                results = embedding_search(docs, args.query, args.top, config)
        except Exception as e:
            if args.engine == "embedding":
                print(f"[错误] 语义检索失败: {e}", file=sys.stderr)
                sys.exit(1)
            print(f"（语义检索不可用，退回关键词检索：{e}）", file=sys.stderr)
            results = keyword_search(docs, args.query, args.top)
    else:
        if args.mode == "explore":
            from .retrieval import explore
            results = explore(docs, args.query, args.top)
        else:
            results = keyword_search(docs, args.query, args.top)

    if not results:
        print(f"（无结果：{args.query}）")
        return

    for i, d in enumerate(results, 1):
        area_zh = d["area"].split("::")[-1] if "::" in d["area"] else d["area"]
        flag = "已读" if d.get("read") else "未读"
        print(f"{i}. {d['title_en']}  [{d['category']}/{area_zh}/{d['work']}/{d['year']}] · {flag}")
        if d["title_zh"] and d["title_zh"] != d["title_en"]:
            print(f"   {d['title_zh']}")
        for kind, loc in resolve_targets(d, config):
            print(f"   ↳ {kind}: {loc}")


if __name__ == "__main__":
    main()

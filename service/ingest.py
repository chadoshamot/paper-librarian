"""命令行入口：python -m service.ingest <pdf1> [pdf2 ...]"""
import sys
from pathlib import Path

from .config_loader import Config
from .pipeline import IngestPipeline


def main():
    config = Config()
    pipe = IngestPipeline(config)
    for arg in sys.argv[1:]:
        pdf = Path(arg)
        if not pdf.exists():
            print(f"[跳过] 不存在: {pdf}")
            continue
        print(f"\n=== 处理: {pdf.name} ===")
        try:
            result = pipe.run(pdf)
            for k, v in result.items():
                print(f"  {k}: {v}")
        except Exception as e:
            print(f"  [错误] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()

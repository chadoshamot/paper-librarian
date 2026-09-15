"""命令行重分类：python -m service.reclassify <pdf> --category 方法 --area 机器学习系统::调度 --work slo-aware-scheduling --year 2020"""
import argparse
from pathlib import Path

from .config_loader import Config
from .metadata import parse_filename
from .pipeline import IngestPipeline


def main():
    ap = argparse.ArgumentParser(description="按给定分类重录一篇已入库论文")
    ap.add_argument("pdf")
    ap.add_argument("--category", required=True)
    ap.add_argument("--area", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--year", type=int, required=True)
    args = ap.parse_args()

    pipe = IngestPipeline(Config())
    print(f"\n=== 重分类: {Path(args.pdf).name} ===")
    fn = parse_filename(Path(args.pdf))
    pid = fn.get("arxiv_id") or f"local:{Path(args.pdf).stem}"
    result = pipe.reclassify(pid, args.category, args.area, args.work, args.year)
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

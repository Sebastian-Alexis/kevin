"""Side-by-side table from eval_point.py --out files: accuracy on the full set, latency from the -c 1 runs.

  python training/compare.py --dir /workspace/results base teacher student

Reads <dir>/<name>_screenspot.jsonl (accuracy) and <dir>/<name>_lat.jsonl (single-stream latency).
"""
import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

GROUPS = ["ALL", "desktop/macos", "desktop/windows", "web"]


def load(path):
    return [json.loads(line) for line in open(path)] if path.exists() else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/workspace/results")
    ap.add_argument("names", nargs="+")
    a = ap.parse_args()
    d = Path(a.dir)
    print(f"{'model':10s}" + "".join(f"{g:>17s}" for g in GROUPS) + f"{'p50 ms':>9s}{'p90 ms':>9s}")
    for name in a.names:
        acc = defaultdict(list)
        for r in load(d / f"{name}_screenspot.jsonl"):
            for g in GROUPS:
                if g == "ALL" or r["group"].startswith(g):
                    acc[g].append(r["hit"])
        lat = sorted(r["ms"] for r in load(d / f"{name}_lat.jsonl"))
        cells = "".join(f"{sum(acc[g]) / len(acc[g]):>11.1%} n={len(acc[g]):<3d}" if acc[g] else f"{'-':>17s}" for g in GROUPS)
        p50, p90 = (statistics.median(lat), lat[int(len(lat) * 0.9) - 1]) if lat else (float("nan"),) * 2
        print(f"{name:10s}{cells}{p50:>9.0f}{p90:>9.0f}")


if __name__ == "__main__":
    main()

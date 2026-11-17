#!/usr/bin/env python3
"""Summarize an Ascend torch_npu profiler trace: where did the time go?

torch_npu writes one directory per rank under ``torch_profiler_dir``, each
containing ``*_ascend_pt/ASCEND_PROFILER_OUTPUT`` with msprof CSV exports.
This prints the operators sorted by total NPU time, plus the communication
operators on their own, which is what "decode is slow" usually boils down
to.  Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict

CANDIDATES = (
    "op_statistic.csv",
    "kernel_details.csv",
    os.path.join("ASCEND_PROFILER_OUTPUT", "op_statistic.csv"),
    os.path.join("ASCEND_PROFILER_OUTPUT", "kernel_details.csv"),
)

NAME_COLUMNS = ("op_type", "Name", "op_name", "Kernel Name", "name")
TOTAL_COLUMNS = ("total_time(us)", "Total Time(us)", "total_time_us", "Duration(us)", "duration")
COUNT_COLUMNS = ("count", "Count", "Occurrences", "op_count")
COMM_HINTS = ("hccl", "allreduce", "allreduce", "alltoall", "allgather", "all_gather", "reducescatter", "broadcast", "matmulallreduce")


def find_csvs(root: str) -> list[str]:
    found: list[str] = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            if name in ("op_statistic.csv", "kernel_details.csv"):
                found.append(os.path.join(base, name))
    # Prefer aggregated op statistics over per-launch kernel rows.
    found.sort(key=lambda p: (os.path.basename(p) != "op_statistic.csv", p))
    return found


def pick(header: list[str], options: tuple[str, ...]) -> str | None:
    lowered = {h.strip().lower(): h for h in header}
    for option in options:
        if option.lower() in lowered:
            return lowered[option.lower()]
    return None


def summarize(path: str, top: int) -> int:
    with open(path, newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            print(f"{path}: 空文件")
            return 1
        name_col = pick(reader.fieldnames, NAME_COLUMNS)
        total_col = pick(reader.fieldnames, TOTAL_COLUMNS)
        count_col = pick(reader.fieldnames, COUNT_COLUMNS)
        if not name_col or not total_col:
            print(f"{path}: 未识别列 {reader.fieldnames[:8]}")
            return 1

        totals: dict[str, float] = defaultdict(float)
        counts: dict[str, int] = defaultdict(int)
        for row in reader:
            try:
                value = float(row.get(total_col) or 0.0)
            except ValueError:
                continue
            # kernel_details rows are per launch; op rows carry their own count.
            name = (row.get(name_col) or "?").strip()
            totals[name] += value
            if count_col:
                try:
                    counts[name] += int(float(row.get(count_col) or 0))
                except ValueError:
                    counts[name] += 1
            else:
                counts[name] += 1

    if not totals:
        print(f"{path}: 没有可用的耗时数据")
        return 1

    grand = sum(totals.values())
    print(f"== {path}")
    print(f"== 总耗时 {grand / 1e3:.1f} ms（{len(counts)} 个算子）")
    print(f"{'算子':<58}{'总毫秒':>10}{'占比':>8}{'次数':>8}{'均次us':>10}")
    for name, value in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:top]:
        count = counts[name]
        print(
            f"{name[:57]:<58}{value / 1e3:>10.1f}{100 * value / grand:>7.1f}%"
            f"{count:>8}{value / max(count, 1):>10.0f}"
        )

    comm = {n: v for n, v in totals.items() if any(h in n.lower() for h in COMM_HINTS)}
    if comm:
        comm_total = sum(comm.values())
        print(f"\n== 集合通信合计 {comm_total / 1e3:.1f} ms（{100 * comm_total / grand:.1f}%）")
        for name, value in sorted(comm.items(), key=lambda kv: kv[1], reverse=True)[:10]:
            print(f"{name[:57]:<58}{value / 1e3:>10.1f}{100 * value / grand:>7.1f}%")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_dir", help="perf.sh profile 打印的 trace 目录")
    parser.add_argument("--top", type=int, default=25)
    args = parser.parse_args()

    if not os.path.isdir(args.trace_dir):
        print(f"目录不存在: {args.trace_dir}", file=sys.stderr)
        return 1
    csvs = find_csvs(args.trace_dir)
    if not csvs:
        print(f"{args.trace_dir} 下没有 op_statistic.csv / kernel_details.csv", file=sys.stderr)
        print("提示：torch_npu 还在导出时等一会儿；或检查 profiler.json 的 export_type", file=sys.stderr)
        return 1
    # One rank is enough to see the shape of the step; prefer the aggregated
    # op statistics over per-launch kernel rows.
    aggregated = [p for p in csvs if os.path.basename(p) == "op_statistic.csv"]
    return summarize((aggregated or csvs)[0], args.top)


if __name__ == "__main__":
    raise SystemExit(main())

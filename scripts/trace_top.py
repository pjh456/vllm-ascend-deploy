#!/usr/bin/env python3
"""Summarize an Ascend torch_npu profiler trace: where did the time go?

torch_npu writes one ``*_ascend_pt`` directory per rank under
``torch_profiler_dir``; inside it msprof keeps the raw ``PROF_*`` data and,
once exported, ``mindstudio_profiler_output/*.csv``.

    trace_top.py <trace_dir>                 # analyze existing CSVs
    trace_top.py <trace_dir> --export        # run `msprof --export` first
                                             # (needs CANN env, so run it
                                             # inside the engine container)

``--export`` picks the newest rank and prints the operators ranked by total
NPU time, with collective communication reported separately -- which is what
"decode is slow" usually boils down to.  Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import subprocess
import sys
from collections import defaultdict

CSV_PATTERNS = ("op_statistic*.csv", "kernel_details*.csv")
NAME_COLUMNS = ("OP Type", "op_type", "Name", "op_name", "Kernel Name", "name")
TOTAL_COLUMNS = ("Total Time(us)", "total_time(us)", "Total Time(us)", "total_time_us", "Duration(us)")
COUNT_COLUMNS = ("Count", "count", "Occurrences", "op_count")
COMM_HINTS = (
    "hccl",
    "hcom",
    "allreduce",
    "alltoall",
    "allgather",
    "all_gather",
    "reducescatter",
    "reduce_scatter",
    "broadcast",
    "matmulallreduce",
)


def find_csvs(root: str) -> list[str]:
    found: list[str] = []
    for pattern in CSV_PATTERNS:
        found.extend(glob.glob(os.path.join(root, "**", pattern), recursive=True))
    return found


def newest_rank(root: str) -> str | None:
    ranks = sorted(
        glob.glob(os.path.join(root, "*_ascend_pt")),
        key=os.path.getmtime,
        reverse=True,
    )
    return ranks[0] if ranks else None


def export_rank(rank: str) -> str | None:
    """Export one rank's raw PROF data and return the CSV directory."""
    profs = sorted(glob.glob(os.path.join(rank, "PROF_*")), key=os.path.getmtime, reverse=True)
    if not profs:
        print(f"{rank}: 没有找到 PROF_* 目录", file=sys.stderr)
        return None
    prof = profs[0]
    print(f"[trace] 导出 {os.path.basename(rank)} ...")
    done = subprocess.run(
        ["msprof", "--export=on", f"--output={prof}"],
        cwd=rank,
        capture_output=True,
        check=False,
        text=True,
    )
    if done.returncode != 0:
        print(done.stdout[-2000:], file=sys.stderr)
        print(done.stderr[-2000:], file=sys.stderr)
        print("[trace] msprof --export 失败", file=sys.stderr)
        return None
    return os.path.join(prof, "mindstudio_profiler_output")


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
    print(f"== {os.path.basename(path)}")
    print(f"== 总耗时 {grand / 1e3:.1f} ms（{len(counts)} 个算子）")
    print(f"{'operator':<52}{'total_ms':>10}{'pct':>8}{'count':>8}{'avg_us':>9}")
    for name, value in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:top]:
        count = counts[name]
        print(
            f"{name[:51]:<52}{value / 1e3:>10.1f}{100 * value / grand:>7.1f}%"
            f"{count:>8}{value / max(count, 1):>9.0f}"
        )

    comm = {n: v for n, v in totals.items() if any(h in n.lower() for h in COMM_HINTS)}
    if comm:
        comm_total = sum(comm.values())
        print(f"\n== collective comm {comm_total / 1e3:.1f} ms（{100 * comm_total / grand:.1f}%）")
        for name, value in sorted(comm.items(), key=lambda kv: kv[1], reverse=True)[:10]:
            print(f"{name[:51]:<52}{value / 1e3:>10.1f}{100 * value / grand:>7.1f}%")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_dir", help="perf.sh profile 打印的 trace 目录")
    parser.add_argument("--export", action="store_true", help="先跑 msprof --export 再分析")
    parser.add_argument("--top", type=int, default=25)
    args = parser.parse_args()

    if not os.path.isdir(args.trace_dir):
        print(f"目录不存在: {args.trace_dir}", file=sys.stderr)
        return 1

    csvs = find_csvs(args.trace_dir)
    rank = newest_rank(args.trace_dir)
    if rank:
        # Only ever look at the newest rank: stale exports of earlier runs live
        # side by side and would otherwise win the "first CSV found" race.
        print(f"[trace] rank: {os.path.basename(rank)}")
        csvs = find_csvs(rank)
        if not csvs and args.export:
            exported = export_rank(rank)
            if exported:
                csvs = find_csvs(exported)
    if not csvs:
        print(f"{args.trace_dir} 下没有 op_statistic*.csv / kernel_details*.csv", file=sys.stderr)
        print("提示：加 --export 让 msprof 先导出；导出要在容器里跑（需要 CANN 环境）", file=sys.stderr)
        return 1

    aggregated = [p for p in csvs if os.path.basename(p).startswith("op_statistic")]
    return summarize((aggregated or csvs)[0], args.top)


if __name__ == "__main__":
    raise SystemExit(main())

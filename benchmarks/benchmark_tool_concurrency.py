from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from benchmarks.fixtures import create_fixture  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark ToolExecutor read-tool concurrency.")
    parser.add_argument("--runs", type=int, default=30, help="Measured runs per concurrency level.")
    parser.add_argument("--warmups", type=int, default=5, help="Warmup runs per concurrency level.")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Synthetic I/O delay per tool, in seconds.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON output path. A Markdown summary is written next to it.",
    )
    return parser.parse_args()


async def _run_once(delay_seconds: float, max_concurrent_read: int) -> tuple[float, bool, str]:
    fixture = create_fixture(
        delay_seconds=delay_seconds,
        max_concurrent_read=max_concurrent_read,
        cwd=str(ROOT),
    )
    start = time.perf_counter_ns()
    try:
        results = await fixture.executor.execute_all(fixture.calls, fixture.context)
        elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
        failed = [result for result in results if result.is_error]
        return elapsed_ms, not failed and len(results) == len(fixture.calls), ""
    except Exception as exc:  # noqa: BLE001 - benchmark records failures instead of crashing
        elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
        return elapsed_ms, False, f"{type(exc).__name__}: {exc}"


async def _run_group(
    *,
    delay_seconds: float,
    max_concurrent_read: int,
    warmups: int,
    runs: int,
) -> dict[str, Any]:
    for _ in range(warmups):
        await _run_once(delay_seconds, max_concurrent_read)

    timings: list[float] = []
    errors: list[str] = []
    for _ in range(runs):
        elapsed_ms, ok, error = await _run_once(delay_seconds, max_concurrent_read)
        if ok:
            timings.append(elapsed_ms)
        else:
            errors.append(error or "tool result reported failure")

    return summarize(timings, requested_runs=runs, errors=errors)


def summarize(values: list[float], *, requested_runs: int, errors: list[str]) -> dict[str, Any]:
    if not values:
        return {
            "count": requested_runs,
            "successful_runs": 0,
            "failed_runs": len(errors),
            "min_ms": None,
            "max_ms": None,
            "mean_ms": None,
            "median_ms": None,
            "p95_ms": None,
            "stdev_ms": None,
            "errors": errors,
        }

    sorted_values = sorted(values)
    p95_index = max(0, min(len(sorted_values) - 1, math.ceil(len(sorted_values) * 0.95) - 1))
    return {
        "count": requested_runs,
        "successful_runs": len(values),
        "failed_runs": len(errors),
        "min_ms": round(min(values), 3),
        "max_ms": round(max(values), 3),
        "mean_ms": round(statistics.fmean(values), 3),
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(sorted_values[p95_index], 3),
        "stdev_ms": round(statistics.stdev(values), 3) if len(values) > 1 else 0.0,
        "errors": errors,
    }


def add_improvements(payload: dict[str, Any]) -> None:
    serial_median = payload["results"]["concurrency_1"]["median_ms"]
    delay_ms = payload["benchmark"]["delay_seconds"] * 1000
    tool_count = payload["benchmark"]["tool_count"]
    for concurrency in [1, 2, 4]:
        result = payload["results"][f"concurrency_{concurrency}"]
        median = result["median_ms"]
        theoretical_ms = delay_ms * ((tool_count + concurrency - 1) // concurrency)
        result["theoretical_ms"] = round(theoretical_ms, 3)
        if serial_median and median is not None:
            improvement = (serial_median - median) / serial_median * 100
            result["improvement_vs_serial_percent"] = round(improvement, 2)
        else:
            result["improvement_vs_serial_percent"] = None


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "environment": {
            "python": sys.version.replace("\n", " "),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "timestamp": datetime.now(UTC).isoformat(),
        },
        "benchmark": {
            "tool_count": 4,
            "delay_seconds": args.delay,
            "warmups": args.warmups,
            "runs": args.runs,
            "scope": "ToolExecutor.execute_all only; excludes LLM inference and process startup.",
        },
        "results": {},
    }
    for concurrency in [1, 2, 4]:
        payload["results"][f"concurrency_{concurrency}"] = await _run_group(
            delay_seconds=args.delay,
            max_concurrent_read=concurrency,
            warmups=args.warmups,
            runs=args.runs,
        )
    add_improvements(payload)
    return payload


def write_outputs(payload: dict[str, Any], output: Path | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output is None:
        print(text)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    markdown_path = output.with_suffix(".md")
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")


def render_markdown(payload: dict[str, Any]) -> str:
    env = payload["environment"]
    bench = payload["benchmark"]
    lines = [
        "# Tool Concurrency Benchmark",
        "",
        f"- Timestamp: {env['timestamp']}",
        f"- Platform: {env['platform']}",
        f"- Python: {env['python']}",
        f"- Processor: {env['processor'] or 'unknown'}",
        f"- Tool count: {bench['tool_count']}",
        f"- Synthetic I/O delay per tool: {bench['delay_seconds']} seconds",
        f"- Warmups: {bench['warmups']}",
        f"- Measured runs: {bench['runs']}",
        f"- Scope: {bench['scope']}",
        "",
        "| max_concurrent_read | mean ms | median ms | p95 ms | improvement vs serial |",
        "| ---: | ---: | ---: | ---: | ---: |",
    ]
    for concurrency in [1, 2, 4]:
        result = payload["results"][f"concurrency_{concurrency}"]
        improvement = result["improvement_vs_serial_percent"]
        improvement_text = "baseline" if concurrency == 1 else f"{improvement:.2f}%"
        lines.append(
            "| "
            f"{concurrency} | {result['mean_ms']:.3f} | {result['median_ms']:.3f} | "
            f"{result['p95_ms']:.3f} | {improvement_text} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    payload = asyncio.run(run_benchmark(args))
    write_outputs(payload, args.output)


if __name__ == "__main__":
    main()

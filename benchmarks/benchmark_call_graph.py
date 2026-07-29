from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from axiom.rag.call_graph import find_recursive_components, trace_call_paths
from axiom.rag.models import CallEdge


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark local static call graph queries.")
    parser.add_argument("--edges", nargs="+", type=int, default=[100, 1000, 10000])
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    results = {}
    for edge_count in args.edges:
        edges = synthetic_edges(edge_count)
        root = "symbol_0"
        target = f"symbol_{min(edge_count, 99)}"
        cases = {
            "direct_callees": lambda edges=edges, root=root: [
                edge for edge in edges if edge.caller_symbol_id == root
            ],
            "direct_callers": lambda edges=edges, target=target: [
                edge for edge in edges if edge.callee_symbol_id == target
            ],
            "depth_3_traversal": lambda edges=edges, root=root: trace_call_paths(
                root,
                edges,
                max_depth=3,
            ),
            "scc": lambda edges=edges: find_recursive_components(edges),
        }
        results[f"edges_{edge_count}"] = {
            name: run_case(func, warmups=args.warmups, runs=args.runs)
            for name, func in cases.items()
        }

    payload = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "timestamp": datetime.now(UTC).isoformat(),
        },
        "benchmark": {
            "warmups": args.warmups,
            "runs": args.runs,
            "edge_counts": args.edges,
        },
        "results": results,
    }
    text = json.dumps(payload, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def synthetic_edges(count: int) -> list[CallEdge]:
    edges = []
    for index in range(count):
        caller = f"symbol_{index}"
        callee = f"symbol_{index + 1}"
        if index and index % 250 == 0:
            callee = f"symbol_{index - 1}"
        edges.append(
            CallEdge(
                id=f"edge_{index}",
                reference_id=f"ref_{index}",
                caller_symbol_id=caller,
                callee_symbol_id=callee,
                file_path=f"src/file_{index // 100}.py",
                language="python",
                edge_kind="call",
                start_line=index + 1,
                end_line=index + 1,
                resolution_confidence=1.0,
                resolution_reason="synthetic",
            )
        )
    return edges


def run_case(func, *, warmups: int, runs: int) -> dict[str, float | int]:
    for _ in range(warmups):
        func()
    samples = []
    for _ in range(runs):
        started = time.perf_counter_ns()
        func()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return {
        "count": len(samples),
        "min_ms": min(samples),
        "max_ms": max(samples),
        "mean_ms": statistics.fmean(samples),
        "median_ms": statistics.median(samples),
        "p95_ms": percentile(samples, 95),
        "stdev_ms": statistics.stdev(samples) if len(samples) > 1 else 0.0,
    }


def percentile(samples: list[float], percentile_value: int) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, round((percentile_value / 100) * (len(ordered) - 1)))
    return ordered[index]


if __name__ == "__main__":
    main()

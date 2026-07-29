from __future__ import annotations

import argparse
import json
import platform
import shutil
import statistics
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from axiom.config import EmbeddingConfig
from axiom.rag import CodeIndex


class SyntheticEmbeddingProvider:
    provider_name = "synthetic-local"
    model_name = "deterministic-concepts-v1"

    def __init__(self, dimensions: int):
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int | None:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.casefold()
            vector = [0.0] * self._dimensions
            terms = [
                ("auth", ["auth", "login", "credential"]),
                ("config", ["config", "settings"]),
                ("fetch", ["fetch", "retrieve"]),
                ("profile", ["profile", "customer"]),
                ("worker", ["worker", "job"]),
                ("client", ["client", "http"]),
            ]
            for index, (_name, needles) in enumerate(terms[: max(0, self._dimensions - 1)]):
                if any(needle in lowered for needle in needles):
                    vector[index] = 1.0
            if not any(vector):
                vector[-1] = 1.0
            vectors.append(vector)
        return vectors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", type=int, nargs="+", default=[100, 1000, 5000])
    parser.add_argument("--dimensions", type=int, default=64)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "timestamp": datetime.now(UTC).isoformat(),
        },
        "benchmark": {
            "warmups": args.warmups,
            "runs": args.runs,
            "dimensions": args.dimensions,
            "provider": "synthetic-local deterministic provider",
            "note": "No LLM, network, external embedding API, or real repository indexing.",
        },
        "results": {},
    }

    for chunk_count in args.chunks:
        raw_tmp = tempfile.mkdtemp(prefix="axiom-code-search-")
        try:
            root = Path(raw_tmp)
            _write_project(root, chunk_count)
            provider = SyntheticEmbeddingProvider(args.dimensions)
            config = EmbeddingConfig(
                enabled=True,
                search_mode="hybrid",
                candidate_limit=200,
                dimensions=args.dimensions,
            )
            index = CodeIndex(
                root,
                db_path=root / "index.sqlite3",
                embedding_provider=provider,
                search_config=config,
            )
            index.update()
            query = "retrieve customer profile"
            profile = index._current_profile()
            if profile is None:
                raise RuntimeError("benchmark profile was not created")

            result = {}
            for mode in ["lexical", "vector", "hybrid"]:
                search_index = index
                search_query = query
                result[mode] = _measure(
                    lambda selected=mode, idx=search_index, q=search_query: idx.search(
                        q,
                        limit=10,
                        mode=selected,
                    ),
                    warmups=args.warmups,
                    runs=args.runs,
                )
            result["query_embedding"] = _measure(
                lambda selected_provider=provider, q=query: selected_provider.embed([q]),
                warmups=args.warmups,
                runs=args.runs,
            )
            query_vector = provider.embed([query])[0]
            scan_index = index
            scan_profile = profile
            scan_vector = query_vector
            result["vector_scan"] = _measure(
                lambda idx=scan_index, vector=scan_vector, selected_profile=scan_profile: (
                    idx.store.search_vectors(vector, profile=selected_profile, limit=200)
                ),
                warmups=args.warmups,
                runs=args.runs,
            )
            report["results"][f"chunks_{chunk_count}"] = result
        finally:
            time.sleep(0.2)
            shutil.rmtree(raw_tmp, ignore_errors=True)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        markdown = args.output.with_suffix(".md")
        markdown.write_text(_markdown(report), encoding="utf-8")
    else:
        print(json.dumps(report, indent=2))


def _write_project(root: Path, chunk_count: int) -> None:
    modules = max(1, chunk_count // 50)
    for module in range(modules):
        lines = []
        for index in range(50):
            number = module * 50 + index
            if number >= chunk_count:
                break
            concept = ["auth", "config", "fetch", "profile", "worker", "client"][number % 6]
            lines.append(
                f"def {concept}_function_{number}():\n"
                f"    return '{concept} customer profile settings login job http'\n"
            )
        (root / f"module_{module}.py").write_text("\n".join(lines), encoding="utf-8")


def _measure(fn, *, warmups: int, runs: int) -> dict[str, float]:
    for _ in range(warmups):
        fn()
    values: list[float] = []
    for _ in range(runs):
        started = time.perf_counter_ns()
        fn()
        values.append((time.perf_counter_ns() - started) / 1_000_000)
    return {
        "count": len(values),
        "min_ms": min(values),
        "max_ms": max(values),
        "mean_ms": statistics.fmean(values),
        "p50_ms": statistics.median(values),
        "p95_ms": _p95(values),
        "stdev_ms": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return ordered[index]


def _markdown(report: dict) -> str:
    lines = [
        "# Code Search Benchmark",
        "",
        "> Local synthetic benchmark. This is not a production latency claim.",
        "",
        f"- Python: {report['environment']['python']}",
        f"- Platform: {report['environment']['platform']}",
        f"- Dimensions: {report['benchmark']['dimensions']}",
        f"- Runs: {report['benchmark']['runs']}",
        "",
    ]
    for label, result in report["results"].items():
        lines.append(f"## {label}")
        lines.append("")
        lines.append("| Mode | p50 ms | p95 ms | mean ms |")
        lines.append("| --- | ---: | ---: | ---: |")
        for mode, stats in result.items():
            lines.append(
                f"| {mode} | {stats['p50_ms']:.3f} | "
                f"{stats['p95_ms']:.3f} | {stats['mean_ms']:.3f} |"
            )
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()

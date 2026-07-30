from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from rich.console import Console

from axiom.config import AxiomConfig, EmbeddingConfig
from axiom.entrypoints import repl
from axiom.rag import CodeIndex
from axiom.rag.code_index_factory import create_code_index
from axiom.rag.context import estimate_tokens, serialized_item_chars
from axiom.tools.base import ToolContext
from axiom.tools.builtins import get_builtin_tools, get_code_context

FIXTURE = Path(__file__).parent / "fixtures" / "code_context_project"
QUERIES = Path(__file__).parent / "fixtures" / "code_context_queries.json"


def copy_fixture(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    return project


def build_index(project: Path) -> CodeIndex:
    index = CodeIndex(project)
    index.update()
    return index


def symbol_names(index: CodeIndex, result) -> set[str]:
    definitions = {item.id: item for item in index.store.list_symbol_definitions()}
    names = set()
    for item in result.items:
        if item.symbol_id and item.symbol_id in definitions:
            names.add(definitions[item.symbol_id].name)
    return names


def test_code_context_deterministic_ordering_and_reasons(tmp_path: Path) -> None:
    index = build_index(copy_fixture(tmp_path))

    first = index.build_code_context("start_order", mode="lexical")
    second = index.build_code_context("start_order", mode="lexical")

    assert [(item.reason, item.file_path, item.start_line) for item in first.items] == [
        (item.reason, item.file_path, item.start_line) for item in second.items
    ]
    assert first.items[0].reason == "search_seed"
    assert {"callee", "outgoing_path"} & {item.reason for item in first.items}
    assert all(not Path(item.file_path).is_absolute() for item in first.items)


def test_code_context_budget_hard_limit_and_truncation(tmp_path: Path) -> None:
    index = build_index(copy_fixture(tmp_path))

    result = index.build_code_context(
        "start_order",
        mode="lexical",
        max_context_chars=90,
        max_estimated_tokens=30,
        max_items=2,
    )

    assert len(result.items) <= 2
    assert result.estimated_chars <= result.max_context_chars
    assert result.estimated_tokens <= result.max_estimated_tokens
    assert result.truncated is True


def test_code_context_budget_shortage_cases(tmp_path: Path) -> None:
    index = build_index(copy_fixture(tmp_path))

    oversized_seed = index.build_code_context(
        "start_order",
        mode="lexical",
        max_context_chars=260,
        max_estimated_tokens=6000,
    )
    assert oversized_seed.items == []
    assert oversized_seed.truncated is True
    assert oversized_seed.estimated_chars <= oversized_seed.max_context_chars

    seed_only = index.build_code_context(
        "start_order",
        mode="lexical",
        max_items=1,
    )
    assert len(seed_only.items) == 1
    assert seed_only.items[0].reason == "search_seed"
    assert seed_only.truncated is True

    no_graph = index.build_code_context("start_order", mode="lexical", max_graph_depth=0)
    assert all(item.graph_distance == 0 for item in no_graph.items)
    assert {item.reason for item in no_graph.items} <= {"search_seed", "symbol_definition"}

    tight_graph_budget = index.build_code_context(
        "start_order",
        mode="lexical",
        max_context_chars=520,
        max_estimated_tokens=6000,
        max_items=30,
    )
    assert tight_graph_budget.items
    assert tight_graph_budget.truncated is True
    assert tight_graph_budget.estimated_chars <= tight_graph_budget.max_context_chars


def test_code_context_dedupes_cycles_and_diamond_paths(tmp_path: Path) -> None:
    index = build_index(copy_fixture(tmp_path))

    recursive = index.build_code_context("retry_loop", mode="lexical", max_graph_depth=3)
    assert len({item.chunk_id for item in recursive.items}) == len(recursive.items)
    assert "retry_loop" in symbol_names(index, recursive)
    assert {item.reason for item in recursive.items} == {"search_seed"}

    diamond = index.build_code_context("diamond_root", mode="lexical", max_graph_depth=3)
    names = symbol_names(index, diamond)
    assert {"left_branch", "right_branch", "shared_leaf"} <= names
    assert [item.chunk_id for item in diamond.items].count(
        next(item.chunk_id for item in diamond.items if "shared_leaf" in item.content)
    ) == 1


def test_code_context_lexical_fallback_and_ambiguous_symbol(tmp_path: Path) -> None:
    index = build_index(copy_fixture(tmp_path))

    unrelated = index.build_code_context("no symbol should match this billing webhook")
    assert unrelated.items == []

    ambiguous = index.build_code_context("duplicate", mode="lexical")
    assert {item.file_path for item in ambiguous.items if item.reason == "search_seed"} == {
        "ambiguous_a.py",
        "ambiguous_b.py",
    }
    assert "symbol_definition" not in {item.reason for item in ambiguous.items}


def test_get_code_context_tool_and_registry(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    build_index(project)
    context = ToolContext(cwd=str(project), config=AxiomConfig())

    names = {tool.name for tool in get_builtin_tools()}
    result = asyncio.run(get_code_context({"query": "start_order"}, context))

    assert "get_code_context" in names
    assert result.is_error is False
    assert "search_seed" in result.content
    assert "graph_distance=" in result.content
    assert str(project) not in result.content


def test_repl_context_command_dispatches_code_context(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    build_index(project)
    console = Console(record=True)

    class Agent:
        llm_client = None

    class Registry:
        def list_names(self) -> list[str]:
            return []

    should_exit = asyncio.run(
        repl._handle_slash(
            "/context start_order",
            console,
            str(project),
            AxiomConfig(),
            Agent(),
            Registry(),
        )
    )

    output = console.export_text()
    assert should_exit is False
    assert "search_seed" in output
    assert "flow.py:" in output


def test_repl_bare_context_keeps_runtime_view_without_index(tmp_path: Path) -> None:
    console = Console(record=True)

    class Client:
        max_context_window = 4096

    class Agent:
        llm_client = Client()

    class Registry:
        def list_names(self) -> list[str]:
            return ["read_file"]

    should_exit = asyncio.run(
        repl._handle_slash(
            "/context",
            console,
            str(tmp_path),
            AxiomConfig(),
            Agent(),
            Registry(),
        )
    )

    output = console.export_text()
    assert should_exit is False
    assert "Axiom Agent Runtime Context" in output
    assert "code context" not in output.casefold()


def test_embedding_disabled_context_does_not_create_remote_provider(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    config = AxiomConfig(
        embedding=EmbeddingConfig(
            enabled=False,
            model="embed-model",
            api_key="secret-key",
            base_url="https://example.test/v1",
        )
    )
    index = create_code_index(project, config)

    assert index.embedding_provider is None
    index.update()
    assert index.build_code_context("start_order").items


def test_code_context_fixture_metrics(tmp_path: Path) -> None:
    index = build_index(copy_fixture(tmp_path))
    cases = json.loads(QUERIES.read_text(encoding="utf-8"))
    definitions_by_id = {item.id: item for item in index.store.list_symbol_definitions()}

    search_symbol_hits = 0
    graph_symbol_hits = 0
    duplicate_ratios = []
    item_counts = []
    truncation_ok = True
    for case in cases:
        expected_symbols = set(case["relevant_symbol_names"])
        if not expected_symbols:
            assert index.build_code_context(case["query"], mode="lexical").items == []
            continue
        search_names = {
            item.symbol_name
            for item in index.search(case["query"], limit=8, mode="lexical")
            if item.symbol_name
        }
        context = index.build_code_context(case["query"], mode="lexical", max_graph_depth=2)
        context_names = symbol_names(index, context)
        search_symbol_hits += len(search_names & expected_symbols)
        graph_symbol_hits += len(context_names & expected_symbols)
        assert set(case["relevant_files"]) <= {item.file_path for item in context.items}
        assert set(case["required_reasons"]) <= {item.reason for item in context.items}
        duplicate_count = len({item.chunk_id for item in context.items})
        duplicate_ratios.append(1 - (duplicate_count / len(context.items)))
        item_counts.append(len(context.items))
        truncation_ok = truncation_ok and (
            context.estimated_tokens <= context.max_estimated_tokens
            and context.estimated_chars <= context.max_context_chars
            and sum(serialized_item_chars(item) for item in context.items)
            <= context.max_context_chars
        )
        assert all(
            item.symbol_id is None or item.symbol_id in definitions_by_id
            for item in context.items
        )

    metrics = {
        "search_only_relevant_symbol_hits": search_symbol_hits,
        "graph_aware_relevant_symbol_hits": graph_symbol_hits,
        "duplicate_item_ratio": sum(duplicate_ratios) / len(duplicate_ratios),
        "average_context_items": sum(item_counts) / len(item_counts),
        "truncation_correct": truncation_ok,
    }
    assert (
        metrics["graph_aware_relevant_symbol_hits"]
        >= metrics["search_only_relevant_symbol_hits"]
    )
    assert metrics["duplicate_item_ratio"] == 0.0
    assert metrics["average_context_items"] >= 1.0
    assert metrics["truncation_correct"] is True


def test_estimate_tokens_is_deterministic_heuristic() -> None:
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a\nb\nc\n") == 3

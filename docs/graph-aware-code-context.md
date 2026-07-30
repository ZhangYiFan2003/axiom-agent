# Graph-Aware Code Context

Stage 3D assembles deterministic, explainable code context for agent use. It
combines lexical/vector/hybrid search seeds, symbol definitions, direct
references, direct callers/callees, and bounded call paths from the conservative
static call graph. It does not call an LLM or claim answer-generation accuracy.

## Public API

`CodeIndex` exposes:

```python
build_code_context(
    query,
    mode="auto",
    max_context_chars=24000,
    max_estimated_tokens=6000,
    max_seed_chunks=8,
    max_graph_depth=2,
    max_items=30,
)
```

The result contains ordered `CodeContextItem` records with relative path, line
range, optional symbol ID, reason, seed rank, graph distance, content, and
estimated token cost. Result metadata includes seed count, expanded symbol
count, budget usage, and `truncated`.

## Reasons

Context items are explainable. Supported reasons are:

- `search_seed`
- `symbol_definition`
- `direct_reference`
- `caller`
- `callee`
- `incoming_path`
- `outgoing_path`

Ambiguous symbol names are not resolved by file order. The builder can use
unique symbol mentions in natural language as conservative fallback seeds, but
duplicates remain ambiguous unless search itself returns chunks.

## Budgeting

The budget is deterministic and local. `estimate_tokens(text)` approximates
code as one token per four characters, with a line-count floor. It is a
documented heuristic, not a model-specific tokenizer.

Default limits:

```text
max_context_chars = 24000
max_estimated_tokens = 6000
max_seed_chunks = 8
max_graph_depth = 2
max_context_items = 30
```

Hard caps are enforced internally so callers cannot request unbounded graph
expansion or unbounded context dumps.

## Ranking and Deduplication

Ordering is stable and does not add raw BM25, vector cosine, and graph distance
scores together. Items are ordered by reason priority, seed rank, graph
distance, relative path, line range, symbol ID, and chunk ID.

Deduplication handles repeated chunks, repeated symbol definitions, identical
code ranges, cycles, diamond paths, multiple seeds for one symbol, and duplicate
file/module chunks. When possible, the more specific AST chunk is preferred over
a broader file chunk.

## Tools and REPL

The built-in tool `get_code_context` returns reasoned context with line ranges,
symbol IDs, graph distance, content, budget usage, and truncation status.

The REPL keeps bare `/context` as the existing runtime-context view. Use:

```text
/context <query>
```

to print a compact graph-aware code context summary.

## Privacy and Network

The builder uses the local code index. It does not read `.env`, does not read
`.axiom` configuration content, and does not create a remote embedding provider
when embeddings are disabled. Output paths are workspace-relative POSIX paths.
Tests use deterministic local fixtures and do not call external services.

## Evaluation

`tests/fixtures/code_context_project/` and
`tests/fixtures/code_context_queries.json` provide a deterministic fixture for
search-only versus graph-aware comparison. The tests report only local fixture
properties:

- relevant-symbol recall within budget
- relevant-file recall within budget
- duplicate-item ratio
- average context items
- truncation correctness

These metrics validate the context assembly pipeline. They are not production
question-answering accuracy claims.

## Limitations

- No LLM answer generation.
- No member-call exact dispatch.
- No dynamic dispatch.
- No CFG/DFG.
- No runtime tracing.
- No ANN/vector acceleration.
- No reranker model.
- No remote embedding default.
- No repository-wide summarization.
- No unlimited graph expansion.

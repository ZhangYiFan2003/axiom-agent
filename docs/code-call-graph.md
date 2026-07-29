# Conservative Static Call Graph

Stage 3C-2 builds a precision-first static call graph from the symbol reference
index. It persists exact call edges, direct callers/callees, bounded call paths,
and recursive components. It is not a runtime call graph, compiler, language
server, control-flow graph, or data-flow graph.

## Exact Edge Eligibility

Only references that satisfy every rule become call edges:

- `reference_kind` is `call` or `constructor_call`
- `resolution_status` is `resolved`
- `resolution_confidence >= 0.90`
- caller and callee symbol IDs are present
- caller and callee definitions still exist in the workspace symbol table

The graph deliberately excludes:

- `member_call`
- unresolved references
- ambiguous references
- dynamic references
- external references
- weak workspace-name fallback matches with confidence `0.60`
- module-level calls without an enclosing symbol
- dangling caller or callee IDs

`member_call` references are still indexed, and some may be resolved by the
symbol resolver, but they are not promoted to exact call edges in this stage.
Receiver/type semantics are deferred.

## Data Model

`CallEdge` stores:

- stable edge ID
- source `reference_id`
- caller symbol ID
- callee symbol ID
- call site path and line range
- `call` or `constructor_call`
- resolution confidence and reason

Edge IDs are SHA256 hashes of:

```text
reference_id
caller_symbol_id
callee_symbol_id
edge_kind
```

They do not use database autoincrement IDs or timestamps. Moving the call site
can change the reference ID and therefore the edge ID. Changing the resolved
callee also changes the edge ID.

## SQLite Schema

Schema version: `6`.

Stage 3C-2 adds:

```text
call_edges
```

The v5 to v6 migration is additive. It preserves indexed files, AST chunks, FTS5
rows, embedding profiles, vectors, symbol definitions, imports, and references.
Older v2, v3, v4, and v5 databases migrate to v6 in a single `ensure_schema()`
call through a bounded migration loop.

## Workspace Graph Transaction

The update path performs extraction and resolution outside the graph write
transaction:

```text
read definitions/imports/references
-> resolve imports
-> resolve references
-> build exact call edges
-> replace resolved imports, references, and call edges in one transaction
```

The graph builder is pure Python. It does not parse source, read files, query an
embedding provider, or perform symbol resolution.

## Incremental Behavior

Source updates still parse only changed and new files. After file-level writes
and missing-file cleanup, workspace resolution and call-edge rebuild run over the
stored symbol/reference tables without reparsing unchanged files.

This means:

- caller body changes update only that file's references
- callee body changes with the same declaration preserve stable symbol IDs
- callee renames or deletes remove old call edges
- restored definitions can restore call edges on the next update
- high-confidence references that become low-confidence are removed from the
  exact graph

## Queries

`CodeIndex` exposes:

```python
find_callers(symbol_id_or_name, limit=100)
find_callees(symbol_id_or_name, limit=100)
trace_call_paths(symbol_id_or_name, direction="outgoing", max_depth=3, max_paths=100)
find_recursive_components()
```

Symbol inputs can be full symbol IDs or unique names/qualified names. Ambiguous
names return an explicit error with candidates. The graph never chooses a symbol
based on file order or lexical/vector search.

Bounded traversal uses a cycle-safe iterative walk. Defaults are depth 3 and 100
paths. Hard limits are depth 10 and 1,000 paths.

Recursive components are computed with a Tarjan-style SCC pass over prebuilt
adjacency. The graph walk is linear in vertices plus edges after deterministic
neighbor ordering is prepared. Direct self-edges are reported as direct
recursion; SCCs with more than one symbol are mutual recursion.

## REPL and Tools

The REPL adds:

- `/callers`
- `/callees`
- `/callchain`
- `/recursive`

Built-in tools add:

- `find_callers`
- `find_callees`
- `trace_call_chain`
- `find_recursive_components`

Tool descriptions use "static high-confidence call graph" and do not claim
complete runtime dispatch.

## Fixture Metrics

The committed call-graph fixture covers direct calls, imported calls,
constructors, module-level calls, weak matches, member calls, branches, chains,
direct recursion, and mutual recursion.

On the committed exact-edge truth subset:

```text
edge precision = 1.000
edge recall = 1.000
edge F1 = 1.000
```

These metrics validate only the deterministic fixture. They are not production
accuracy claims.

## Benchmark

`benchmarks/benchmark_call_graph.py` measures direct callers, direct callees,
depth-3 traversal, and SCC computation over generated synthetic edges with 5
warmups and 30 measured runs by default. It does not submit machine-specific
result files by default.

## Limitations

- No dynamic dispatch.
- No interface or virtual dispatch.
- No member-call exact edges.
- No runtime tracing.
- No reflection, `eval`, or dynamic import inference.
- No Java compiler-level overload/type resolution.
- No JS/TS bundler, prototype, or complete tsconfig resolution.
- No probabilistic or embedding-assisted call edges.

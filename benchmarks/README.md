# Tool Concurrency Benchmark

This benchmark measures only `ToolExecutor.execute_all` for fixed synthetic I/O
read tools. It does not call an LLM, external APIs, external MCP servers, or the
network.

The fixture registers four independent tools named `io_task_1` through
`io_task_4`. Each tool is read-only, concurrency-safe, and sleeps for a fixed
duration before returning a deterministic result.

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe benchmarks\benchmark_tool_concurrency.py `
  --warmups 5 `
  --runs 30 `
  --delay 0.2 `
  --output benchmarks\results\tool-concurrency-windows-py312.json
```

The JSON output records environment details, benchmark settings, and timing
statistics for `max_concurrent_read` values of 1, 2, and 4. A Markdown summary
is written next to the JSON file.

Machine-specific result files under `benchmarks/results/` are evidence artifacts.
Review them before deciding whether to commit them.

## Code Search Benchmark

`benchmark_code_search.py` measures local lexical, vector-only, and hybrid code
search over generated synthetic Python chunks. It uses a deterministic local
embedding provider and does not call an LLM, a remote embedding endpoint, the
network, or a real repository index.

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe benchmarks\benchmark_code_search.py `
  --chunks 100 1000 5000 `
  --dimensions 64 `
  --warmups 5 `
  --runs 30 `
  --output benchmarks\results\code-search-local.json
```

The benchmark separates query embedding time, vector scan time, and end-to-end
local search timings for lexical, vector-only, and hybrid modes. Result files
are machine-specific evidence artifacts and should be reviewed before commit.

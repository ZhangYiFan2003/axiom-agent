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

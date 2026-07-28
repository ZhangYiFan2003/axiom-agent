# Tool Concurrency Benchmark

> Reference benchmark captured on one Windows 11 development machine.
> This is not a CI performance threshold or a production latency claim.

- Timestamp: 2026-07-28T02:34:55.384620+00:00
- Platform: Windows-11-10.0.22621-SP0
- Python: 3.12.13 (main, Jun 11 2026, 04:05:29) [MSC v.1944 64 bit (AMD64)]
- Processor: Intel64 Family 6 Model 186 Stepping 2, GenuineIntel
- Tool count: 4
- Synthetic I/O delay per tool: 0.2 seconds
- Warmups: 5
- Measured runs: 30
- Scope: ToolExecutor.execute_all only; excludes LLM inference and process startup.

| max_concurrent_read | mean ms | median ms | p95 ms | improvement vs serial |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 828.203 | 826.919 | 845.579 | baseline |
| 2 | 413.791 | 413.564 | 422.207 | 49.99% |
| 4 | 207.773 | 206.789 | 214.937 | 74.99% |

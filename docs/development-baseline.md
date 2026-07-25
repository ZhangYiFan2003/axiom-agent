# Development Baseline

This document records the current functional baseline for the local Axiom Agent Runtime
Python project. It is based on source inspection plus safe local verification.
It does not include credentials, tokens, API keys, or private configuration
values.

## 1. Current runtime environment

- Project path: `D:\Code\axiom-agent`
- OS: Windows NT 10.0.22621.0
- Python in project environment: 3.12.13
- uv: 0.11.21
- Axiom Agent Runtime package version: 0.1.0
- Virtual environment: `.venv`
- Lock file: `uv.lock`
- Package manager: uv
- Test runner: pytest, configured in `pyproject.toml`

Previously verified local commands:

```powershell
uv run axiom --help
uv run axiom doctor --cwd .
uv run axiom --plain -p "Reply with OK"
```

The one-shot AI command above was reported as successful before this baseline
stage. This document does not repeat the model request.

## 2. Feature matrix

| Feature | Code location | Status | Verification |
| --- | --- | --- | --- |
| CLI command | `pyproject.toml`, `src/axiom/entrypoints/cli.py` | Verified | `uv run axiom --help`, `uv run axiom --version`, and `uv run axiom doctor --cwd .` ran successfully. |
| REPL | `src/axiom/entrypoints/repl.py` | Not verified | Interactive REPL was not started in this baseline stage. |
| One-shot prompt | `src/axiom/entrypoints/cli.py`, `src/axiom/agent/query_engine.py` | Verified | Minimal AI request was reported successful before this stage with `uv run axiom --plain -p "Reply with OK"`. |
| Streaming | `src/axiom/llm/openai_compatible.py`, `src/axiom/render/rich_renderer.py` | Partially verified | One-shot prompt uses the streaming client path; renderer streaming behavior has unit coverage. Raw chunk-level live provider behavior was not separately inspected. |
| Tool calling | `src/axiom/agent/query.py`, `src/axiom/tools/executor.py` | Partially verified | Built-in tools have standalone tests. The full QueryEngine tool replay test failed before tool execution because snapshot initialization hit a permission error. |
| Built-in tools | `src/axiom/tools/builtins.py`, `src/axiom/tools/registry.py` | Partially verified | Tool registry and read/write tool tests passed. Not every built-in tool was exercised end to end. |
| ReAct loop | `src/axiom/agent/query.py`, `src/axiom/agent/agent.py` | Partially verified | One-shot prompt verifies the no-tool path through the agent stack. Tool replay test is currently blocked by snapshot initialization failure. |
| Memory | `src/axiom/memory/manager.py` | Not verified | No dedicated memory persistence test was run or found in the current pytest baseline. |
| Skills | `src/axiom/skill/registry.py`, `src/axiom/tools/builtins.py` | Verified | Skill registry, skill context buffer, and `load_skill` context behavior tests passed. |
| Snapshots | `src/axiom/snapshot/service.py` | Partially verified | Agent one-shot did not fail, but dedicated snapshot restore test failed due permission error when creating user-level snapshot state. |
| Plan-execute | `src/axiom/agent/plan_execute.py`, `src/axiom/plan/*` | Partially verified | Plan model and planner parsing tests passed. PlanExecuteAgent execution test failed before execution due snapshot initialization permission error. |
| Multi-agent | `src/axiom/agent/orchestrator.py` | Partially verified | Parsing/review behavior tests passed. Parallel worker execution test failed before execution due snapshot initialization permission error. |
| MCP client | `src/axiom/mcp/client.py`, `src/axiom/mcp/config.py` | Verified | Tests passed for stdio MCP tool discovery/call and stderr suppression. |
| MCP server | `src/axiom/mcp/server.py` | Partially verified | JSON-RPC tools/list handler is covered by tests. Long-running stdio/http server processes were not started in this baseline. |
| Runtime API | `src/axiom/runtime/api.py`, `src/axiom/runtime/tasks.py` | Partially verified | Durable task lifecycle and cancel tests passed. HTTP Runtime API server was not started in this baseline. |

Status definitions:

- Verified: exercised by a successful command or passing test.
- Partially verified: code path or subcomponent was exercised, but important
  end-to-end behavior remains unverified or currently blocked.
- Not verified: implementation exists, but no successful local verification was
  recorded in this baseline.
- Not implemented: no implementation was found.

## 3. Minimal verification cases

These are safe baseline cases for future repeatable verification. They avoid
secrets and external services unless explicitly noted.

| Feature | Minimal case | Cost/network | Data modification |
| --- | --- | --- | --- |
| CLI command | `uv run axiom --help` | None expected | No |
| Doctor | `uv run axiom doctor --cwd .` | None expected | No |
| One-shot prompt | `uv run axiom --plain -p "Reply with OK"` | May call paid API | Creates snapshots and uses configured provider |
| Built-in tools | Run existing `tests/test_tools.py` | None expected | Temp files only |
| Tool calling/ReAct | Run existing `tests/test_query.py` | None expected | Temp files plus snapshot state |
| Skills | Run existing `tests/test_skill.py` | None expected | Temp files and temp home state |
| Snapshots | Run existing `tests/test_snapshot.py` | None expected | Temp project and snapshot state |
| Plan-execute | Run existing `tests/test_plan.py` | None expected | Temp files plus snapshot state |
| Multi-agent | Run existing `tests/test_multi_agent.py` | None expected | Temp files plus snapshot state |
| MCP client/server handler | Run existing `tests/test_mcp.py` | None expected | Temp MCP server files |
| Runtime task store | Run existing `tests/test_runtime.py` | None expected | Temp SQLite file |

## 4. Test baseline

Pytest configuration:

- Location: `pyproject.toml`
- `testpaths = ["tests"]`
- `addopts = "-q"`

Test files:

- `tests/test_config.py`
- `tests/test_image.py`
- `tests/test_lsp.py`
- `tests/test_mcp.py`
- `tests/test_multi_agent.py`
- `tests/test_plan.py`
- `tests/test_policy.py`
- `tests/test_query.py`
- `tests/test_render.py`
- `tests/test_runtime.py`
- `tests/test_skill.py`
- `tests/test_snapshot.py`
- `tests/test_tools.py`

Command executed:

```powershell
uv run pytest
```

Result:

- Passed: 32
- Failed: 0
- Skipped: 0
- Total executed: 32

The pytest baseline is currently green after test home-directory isolation was
added for Windows.

## 5. Current risks

- The one-shot prompt path can call a paid provider when API configuration is
  present. It should not be included in default local test runs.
- REPL behavior is not covered by the current automated baseline.
- Runtime API HTTP behavior is not covered by the current automated baseline.
- MCP long-running stdio/http server behavior is only partially covered through
  request handler and client tests.
- Memory persistence does not have a dedicated baseline test in the current
  run.

## 6. Recommended next-stage tasks

1. Add or adjust tests for memory persistence so memory can move from Not
   verified to Verified.
2. Add a no-network, no-provider test for the no-tool ReAct loop using a fake
   LLM client and a snapshot-safe home path.
3. Add a minimal Runtime API HTTP test that binds localhost on an ephemeral port
   or directly exercises request handling without external services.
4. Add a REPL smoke test for slash command dispatch without launching a fully
   interactive terminal.
5. Keep paid model verification as an explicit manual smoke command, never as a
   default automated test.

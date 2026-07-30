# Runtime Layered Memory Foundation

Stage 4B-1 adds a local, deterministic memory foundation for Runtime threads.
Runtime thread events remain the raw source of truth. Typed memory records are
derived state used to recover recent conversation context, store explicit facts,
keep manually supplied summaries, and retain bounded tool-result digests.

## Memory kinds

`MemoryKind` defines four record types:

- `conversation`: user and assistant messages derived from Runtime thread events.
- `summary`: manually saved summaries with source event provenance.
- `fact`: explicit facts, preferences, constraints, or decisions.
- `tool_result`: bounded digests of tool results.

`MemoryScopeType` supports `thread`, `project`, `user`, and `task` scopes. The
local `user` scope is a profile namespace only; it is not a multi-user security
boundary.

Each `MemoryRecord` keeps a stable ID, kind, scope, content, timestamps, optional
source event range, and metadata.

## Runtime recovery

Runtime turns now restore prior conversation history from persisted thread
events before invoking the engine. The current user message is appended after
history recovery, so it is not duplicated in `RuntimeTurnContext.history`.

The recovered history includes complete prior `user.message` and
`assistant.message` pairs in event-ID order. Orphan assistant messages,
superseded consecutive user messages, tool-only events, and malformed event
payloads are skipped without corrupting the rest of the thread.

Conversation memory is derived after each source event is persisted. Derivation
is best-effort: if a typed memory write fails, the Runtime turn still uses the
thread event log as the source of truth and can rebuild derived memory later.

The Runtime API still does not reconstruct a full long-term memory prompt by
itself. The recovered history boundary is provided so later stages can add
summary generation and richer memory selection.

## Context budgets

`MemoryContextBuilder` assembles deterministic local context sections:

1. latest summary
2. recent conversation
3. explicit facts and preferences
4. tool-result digests

The builder enforces hard character, estimated-token, and record limits. Token
counts use a documented local heuristic and are not provider billing tokens.

When context is over budget, the builder evicts deterministically. Older
conversation turns are removed before newer turns, summaries are preferred when
they fit, and bounded tool digests are lower priority than explicit facts.

## Tool-result digests

Tool-result memory stores a bounded preview, result size, success status, tool
name, digest, truncation flag, and source event ID. It does not store unlimited
raw output. Secret-like strings such as authorization headers, auth-token shaped values,
API-key labels, and `.env` references are redacted from the memory preview.

## Compatibility

`MemoryManager` remains a backwards-compatible facade for existing `/save`,
`/memory`, and `save_memory` behavior. New saves are mirrored into typed fact
records so prompt assembly and Runtime memory can share the same SQLite file.
The prompt and memory context builders read typed records for explicit facts, so
legacy saves are not injected twice.

Legacy `memories(scope, content, created_at)` rows are migrated additively into
`memory_records` as project-scoped facts. The old table is preserved.

Legacy `/memory clear` clears explicit project-scope facts for the current
project. It does not delete Runtime thread events and does not delete
thread-scoped conversation records, which remain reconstructable from the event
log.

## Concurrency

Runtime turns on the same thread are serialized within one server process. A
second simultaneous request for a busy thread receives HTTP 409, and locks are
released after normal completion or errors. This is an in-process guard, not a
distributed cross-process thread lock.

## Current limitations

This foundation intentionally does not implement:

- automatic LLM summarization
- Map-Reduce compression
- automatic fact extraction
- semantic or vector memory retrieval
- cloud or distributed memory
- cross-user security isolation
- memory encryption
- full privacy redaction for arbitrary content
- real-provider CI

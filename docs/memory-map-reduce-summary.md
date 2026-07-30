# Memory Map-Reduce Summary

Stage 4B-2A adds conversation summarization on top of the Runtime layered memory
foundation. Runtime events remain the source of truth; summaries are derived
checkpoints with source event provenance.

## Flow

```text
raw Runtime conversation events
-> complete user/assistant message pairs
-> deterministic segments
-> map summaries
-> reduce summary
-> active summary checkpoint
-> memory context builder
```

The implementation never deletes raw Runtime events. If summarization fails, the
old active summary remains active and Runtime turns can continue using raw
events and recent conversation memory.

## Summarizer interface

`ConversationSummarizer` separates map and reduce calls:

```python
async def summarize_map(messages, *, previous_summary=None) -> str: ...
async def summarize_reduce(partial_summaries, *, previous_summary=None) -> str: ...
```

Default tests use `DeterministicConversationSummarizer`, which is local,
repeatable, and does not call any external model provider. No remote summarizer
is enabled by default.

## Trigger policy

`SummaryPolicy` controls local compression:

- enabled flag
- threshold message count
- map chunk estimated-token limit
- reduce input estimated-token limit
- minimum unsummarized messages
- recent message reserve
- maximum summary characters
- maximum attempts

All values are normalized to hard bounds. Token counts are local estimates from
the memory context heuristic, not exact provider tokenizer counts or billing
tokens.

## Map and reduce

Map summaries are stored as inactive `summary` records with:

- `summary_stage = map`
- source event start/end IDs
- message count
- summarizer version

Reduce writes one active thread summary with:

- `summary_stage = reduce`
- version
- source event range
- map summary IDs
- estimated before/after tokens
- compression ratio
- optional replaced summary ID

Single-active-summary is enforced per thread for reduce summaries. Older reduce
summaries are retained and marked inactive/superseded.

## Incremental behavior

If an active summary covers events `1..40`, the next successful run only maps
eligible complete conversation records after event `40`, while reserving the
configured recent raw-message tail. The new reduce summary expands provenance
to cover the previous active range plus the newly summarized range.

The active summary is used in memory context together with recent unsummarized
conversation, explicit facts/preferences, and tool-result digests.

The context builder does not inject raw conversation records covered by the
active summary. The retained raw tail is selected after the summary source
range, so there is no intentional raw-message overlap with summarized events. It
reports summary usage, source range, raw messages included, raw messages skipped
because summarized, estimated before/after tokens, and compression ratio.

After Stage 4B-2B, summary text may be supplied as context to the fact extractor,
but accepted facts must still trace back to original user events. Summary
records are not treated as an independent source of user preference truth.

## Current limitations

This stage intentionally does not implement:

- semantic/vector memory retrieval
- distributed summarization workers
- background scheduling
- remote summarization in default tests
- deletion or compaction of raw Runtime events
- exact provider billing calculation
- runtime observability

# Memory Fact and Preference Extraction

Stage 4B-2B adds conservative fact and preference extraction on top of the
Runtime layered memory and Map-Reduce summary foundation.

Runtime thread events remain the source of truth. Extracted facts are derived
records with event provenance:

```text
completed user events
-> fact candidates
-> validation and scope policy
-> duplicate merge or conflict supersession
-> active typed facts/preferences
-> memory context builder
```

Extraction failure never fails a completed Runtime turn. Raw events and
conversation memory remain available for retry or inspection.

## Extractor interface

`FactExtractor` accepts message history and optional summary context and returns
structured `FactCandidate` objects. Default tests use
`DeterministicFactExtractor`, which is local, repeatable, and does not call an
external model provider.

The deterministic extractor is intentionally conservative. It validates state
transitions, provenance, scope policy, duplicate/conflict behavior, and privacy
guards. It is not a production natural-language extraction quality benchmark.

## Candidate schema

Fact candidates carry:

- normalized key
- value
- category
- intended scope
- confidence
- explicit/inferred flag
- source event start/end IDs
- optional evidence text
- action: upsert or retract

Supported categories include `preference`, `constraint`, `project_decision`,
`environment`, `identity-neutral_profile`, `workflow`, and legacy `fact`.

## Scope policy

Thread scope is for temporary task constraints and does not cross thread
boundaries.

Project scope is for stable project decisions, environment facts, and workflow
constraints that may be reused across threads in the same project.

User scope is a local profile namespace for explicit high-confidence user
preferences. It is not an authentication or multi-user security boundary.
Automatic user-scope memory only accepts explicit preference statements with
high confidence.

Assistant-only statements do not become user facts. Tool results do not become
user preferences. Summary text may provide context, but accepted facts must
still trace back to original user events.

## Duplicate, conflict, and retraction

For a given scope and normalized key, the store keeps at most one active value.
SQLite also creates a partial unique index for active fact keys, so the invariant
does not depend only on Python-side lookup code.

Equivalent duplicate observations update the active record metadata, including
confidence, last-seen event ID, observation count, and supporting event ranges.
Supporting ranges are bounded so repeated restatements do not grow metadata
without limit.

Conflicting values are handled in one SQLite transaction:

```text
insert new active fact
mark old active fact superseded
record provenance and supersession metadata
```

If the transaction fails, the old active value remains active.

Explicit retractions mark matching active facts inactive/retracted. They do not
delete the source Runtime events.

## Context integration

`MemoryContextBuilder` loads active facts from:

1. current thread constraints
2. current project facts and decisions
3. local user preferences
4. other active high-confidence facts

Superseded and retracted facts are excluded. Thread-scoped constraints do not
leak into other threads. Unrelated project scopes are not loaded.

The serialized memory context includes scope, category, key, and event
provenance metadata and remains subject to the existing character, estimated
token, and record hard limits.

## Privacy

Candidates are rejected before storage when they look like credentials, raw
`.env` content, authorization headers, private keys, session tokens, binary-like
blobs, or arbitrary absolute Windows paths.

The implementation does not infer sensitive traits such as health, religion,
politics, sexuality, race/ethnicity, private financial information, or precise
location history.

No remote extractor is enabled by default. If a remote extractor is added and
configured later, documentation must make clear that conversation content may be
sent to that endpoint.

## REPL controls

The REPL keeps existing `/save` and `/memory` behavior and adds:

```text
/memory facts
/memory preferences
/memory show <id-or-key>
/memory forget <id-or-key>
/memory history <key>
```

`/memory clear` remains scoped to explicit project memory and does not delete
Runtime thread events.

`/memory forget <key>` retracts only when the active project/user match is
unique. Ambiguous key matches are listed with IDs and scopes instead of choosing
one by database order.

## Current limitations

This stage intentionally does not implement:

- semantic or vector memory retrieval
- production LLM extraction accuracy evaluation
- hidden user profiling
- sensitive-trait inference
- multi-user authorization
- cloud synchronization
- distributed fact extraction
- preference sharing across projects by default
- exact provider token accounting

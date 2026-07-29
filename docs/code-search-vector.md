# Vector and Hybrid Code Search

Stage 3B-2 adds optional vector storage and hybrid ranking on top of the AST and
FTS5 lexical index. It does not add call graph analysis, cross-file reference
resolution, ANN indexing, or production semantic accuracy claims.

## Provider Model

`EmbeddingProvider` exposes `provider_name`, `model_name`, `dimensions`, and
`embed(texts)`. Providers return vectors in input order, return an empty list for
empty input, and reject missing vectors, inconsistent dimensions, NaN, infinity,
and empty vectors.

The production adapter is OpenAI-compatible and uses `httpx`:

```text
POST <base_url>/embeddings
Authorization: Bearer <api_key>
Content-Type: application/json
```

Remote embeddings are disabled by default. Enabling a remote embedding provider
will send selected code chunk text to the configured endpoint. API keys are not
stored in the code index database, search results, or public configuration
output.

Tests use a deterministic local provider. It is only a fixture for exercising
the vector and fusion pipeline; it is not a semantic model.

## Configuration

Embedding configuration is independent from LLM configuration and does not reuse
the LLM API key unless the user explicitly sets embedding credentials.

Environment variables:

- `AXIOM_EMBEDDING_ENABLED`
- `AXIOM_EMBEDDING_PROVIDER`
- `AXIOM_EMBEDDING_MODEL`
- `AXIOM_EMBEDDING_API_KEY`
- `AXIOM_EMBEDDING_BASE_URL`
- `AXIOM_EMBEDDING_DIMENSIONS`
- `AXIOM_EMBEDDING_BATCH_SIZE`
- `AXIOM_CODE_SEARCH_MODE`

Search modes are `lexical`, `vector`, `hybrid`, and `auto`. `auto` uses hybrid
search only when embeddings are enabled, a provider can be created, and the
current profile has vectors. Otherwise it falls back to lexical search.

## Embedding Input

Embedding text is built with a stable versioned format containing language, file
path, chunk type, symbol name, qualified name, parent symbol, and content. It
excludes timestamps and database IDs. A deterministic maximum character limit is
applied before hashing, and `embedding_input_hash` changes when chunk path,
symbol metadata, or content changes.

Eligible chunks include classes, interfaces, enums, functions, async functions,
methods, constructors, arrow functions, type declarations, and fallback file
chunks. Parsed file/module chunks with precise child chunks are skipped to avoid
duplicated vector cost and repeated search results.

## Vector Format

Vectors are stored as normalized IEEE-754 float32 little-endian SQLite BLOBs:

```text
list[float] -> L2 normalize -> float32 little-endian bytes
```

The implementation uses only the Python standard library. It validates
dimensions, rejects NaN and infinity, rejects zero vectors, and skips corrupted
stored vectors during search.

## SQLite Schema

Schema version: `4`

Stage 3B-2 adds `embedding_profiles` and `chunk_embeddings`. Profile IDs are
SHA256 hashes of provider, model, dimensions, embedding input version,
normalization version, and vector format. They do not include API keys, endpoint
credentials, or source code.

The v3 to v4 migration preserves `indexed_files`, `code_chunks`, and
`code_chunks_fts`, then creates vector tables and marks `schema_version = 4`.
Embedding tables may remain empty when embeddings are disabled.

## Incremental Embedding

`sync_embeddings()` backfills only eligible chunks that are missing vectors for
the current profile or whose `embedding_input_hash` has changed. Unchanged
chunks are skipped. Provider/model/dimension changes create a new profile and
backfill vectors for that profile.

File updates replace chunks and remove stale embeddings for old chunk IDs. If
embedding fails after lexical indexing, the lexical index remains usable and auto
search falls back to lexical behavior.

## Search

Vector-only search embeds the query and scans the current profile vectors in
SQLite using Python cosine similarity. This is an O(N x dimension) scan and is
intentionally simple for portability.

Hybrid search uses lexical candidates union vector candidates, then Reciprocal
Rank Fusion, dedupe, and limit. Raw BM25 and cosine values are not added
directly. RRF combines ranks with configured lexical and vector weights.

## Benchmark

`benchmarks/benchmark_code_search.py` measures local lexical, vector-only, and
hybrid search with a deterministic provider and generated synthetic chunks. It
reports p50, p95, mean, query embedding time, vector scan time, corpus size, and
dimensions. It does not include remote API latency.

On one Windows/Python 3.12 development run with 64-dimensional local vectors,
5 warmups, and 30 measured runs, synthetic 5,000-chunk search measured about
1.8 ms lexical p50, 107.3 ms vector p50, and 108.2 ms hybrid p50. These numbers
are local benchmark evidence only, not production latency claims.

## Offline Evaluation

The committed fixtures include the original 15 lexical queries and 10
paraphrase-oriented vector queries. The vector queries use the deterministic
test provider to prove candidate storage, vector recall, and fusion behavior.
They do not measure real embedding model quality.

On the vector paraphrase fixture, the local deterministic provider produced:

| Mode | Top-1 | Recall@5 | MRR |
| --- | ---: | ---: | ---: |
| lexical | 0.000 | 0.000 | 0.000 |
| vector | 0.800 | 0.900 | 0.833 |
| hybrid | 0.700 | 0.900 | 0.767 |

Across the combined 25-query fixture set, hybrid has the best aggregate score:

| Mode | Top-1 | Recall@5 | MRR |
| --- | ---: | ---: | ---: |
| lexical | 0.520 | 0.600 | 0.548 |
| vector | 0.680 | 0.800 | 0.717 |
| hybrid | 0.760 | 0.920 | 0.827 |

The aggregate does not mean hybrid wins every query type. It shows that RRF is
more stable when the caller does not know whether a query is lexical or
paraphrase-oriented.

## Limitations

- No ANN index.
- No NumPy or native vector extension dependency.
- No call graph or reference resolution.
- No production semantic accuracy claim.
- Full vector scan is acceptable for the current project scale but will need an
  optional acceleration path for larger corpora.

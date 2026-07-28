# Lexical Code Search

Stage 3B-1 upgrades code search from SQLite `LIKE` matching to offline
FTS5/BM25 lexical retrieval over AST chunks. It does not add embeddings, vector
similarity, semantic search, or call graph analysis.

## Tokenizer

`axiom.rag.tokenizer` builds the query and index text used by search.

It applies:

- Unicode NFKC normalization
- case folding for English identifiers
- snake_case and kebab-case splitting
- camelCase and PascalCase splitting
- letter/number boundary splitting
- preservation of complete identifiers such as `load_user_config`
- jieba segmentation only when Chinese characters are present
- stable token de-duplication

Examples:

- `getUserProfile` becomes `getuserprofile get user profile`
- `load_user_config` becomes `load_user_config load user config`
- `OAuth2Client` becomes `oauth2client oauth 2 client`
- `用户权限校验` becomes `用户 权限 校验`

Short code tokens such as `api`, `db`, `id`, and `http` are retained.

## SQLite Schema

The code index schema is version `3`.

Tables:

- `schema_metadata`
- `indexed_files`
- `code_chunks`
- `code_chunks_fts`

`code_chunks_fts` is an FTS5 virtual table populated explicitly from
`code_chunks`. It stores `chunk_id`, `file_path`, `chunk_type`, `symbol_name`,
`qualified_name`, and generated `lexical_text`.

The FTS rows are synchronized in the same transactions that replace or delete
indexed files. File deletion explicitly removes FTS rows and then relies on the
foreign key to remove chunk rows.

## Migration

Version 2 databases already contain AST chunks, so Stage 3B-1 migrates them in
place:

1. Keep `indexed_files`.
2. Keep `code_chunks`.
3. Create `code_chunks_fts` when FTS5 is available.
4. Backfill FTS rows from existing chunks.
5. Mark `schema_version = 3`.

Pre-v2 line-oriented schemas are still treated as legacy and rebuilt by dropping
only code-index tables.

## Search Backend

When FTS5 is available, search uses an FTS5 `MATCH` query built from tokenized
query terms and retrieves BM25-ranked candidates. User input is never inserted
directly as raw MATCH text; each token is safely quoted and joined with `AND`.

When FTS5 is unavailable, search falls back to tokenized LIKE matching. Results
are marked with `backend = "like-fallback"` and are not described as BM25.

## Ranking

BM25 provides the first candidate order. A small deterministic ranking layer then
adds explainable boosts and demotions:

- exact symbol match
- symbol prefix match
- qualified name match
- file path match
- precise chunk types such as class, function, method, interface, and type
- file chunks
- fallback file chunks

The ranking weights are centralized in `axiom.rag.ranking.RANKING_WEIGHTS`.

## Deduplication

AST indexing stores file, class, and method chunks, so the same source can appear
at multiple granularities. Search deduplicates after ranking and before applying
the final limit using:

- file path
- qualified name
- content hash

This keeps precise method/function chunks from being crowded out by broader file
or class chunks.

## Evaluation

`tests/fixtures/code_search_queries.json` contains a deterministic offline query
set covering Python, Java, TypeScript, camelCase, snake_case, Chinese, classes,
methods, and file paths.

The test suite reports relevance through assertions for:

- Top-1 accuracy
- Recall@5
- MRR

This is a local relevance fixture, not a production performance benchmark.

## Current Limits

- Search is lexical, not semantic.
- There are no embeddings or vector fields.
- There is no hybrid vector ranking.
- There is no symbol reference resolution or call graph.
- No latency claim is made in this stage.

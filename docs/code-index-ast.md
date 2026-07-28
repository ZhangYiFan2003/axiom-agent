# AST Code Index

Stage 3A replaces the previous line-oriented code index with a tree-sitter based
AST index. It keeps the public `CodeIndex` and `search_code` entrypoints
compatible while storing structured file and chunk metadata in SQLite.

## Architecture

- `axiom.rag.languages` maps file extensions to supported languages and filters
  indexable files.
- `axiom.rag.parser` creates tree-sitter parsers from offline grammar wheels.
- `axiom.rag.chunker` converts files into file, class, function, method, and
  structure chunks.
- `axiom.rag.store` owns the SQLite schema, migration detection, file records,
  chunk records, and keyword row lookup.
- `axiom.rag.code_index.CodeIndex` remains the public facade used by `/index`,
  `/search`, and `search_code`.

## Dependencies

The index uses the Python `tree-sitter` binding plus per-language grammar wheels:

- `tree-sitter-python`
- `tree-sitter-java`
- `tree-sitter-javascript`
- `tree-sitter-typescript`

These packages provide Windows-compatible wheels and do not require runtime
downloads in normal indexing or tests.

## Supported Languages

AST chunking currently supports:

- Python: file, class, function, async function, method, async method
- Java: file, class, interface, enum, constructor, method
- JavaScript: file, class, function, method, arrow function assigned to a name
- TypeScript: file, class, function, method, arrow function assigned to a name,
  interface, type alias

Other indexable text files are stored as fallback file chunks with
`parse_status = 'unsupported'`.

## SQLite Schema

The schema is versioned with `schema_metadata.schema_version = 2`.

`indexed_files` stores file-level metadata:

- `path`
- `language`
- `sha256`
- `size`
- `mtime_ns`
- `indexed_at`
- `parse_status`

`code_chunks` stores chunk-level metadata:

- `id`
- `file_path`
- `language`
- `chunk_type`
- `symbol_name`
- `qualified_name`
- `parent_symbol`
- `start_line`
- `end_line`
- `content`
- `content_hash`
- `is_fallback`
- `parse_status`

Indexes are created for file path, symbol name, and chunk type. Chunk rows are
linked to `indexed_files.path` with `on delete cascade`.

## Incremental Indexing

`CodeIndex.update()` scans the requested file or directory, hashes each file, and
skips unchanged files. Changed files are replaced in a per-file transaction. New
files are inserted, and indexed files missing from the scanned path are deleted.

The returned stats include:

- `scanned_files`
- `indexed_files`
- `unchanged_files`
- `deleted_files`
- `failed_files`
- `chunk_count`
- `duration_ms`

`CodeIndex.rebuild()` is kept for compatibility and forces re-indexing before
returning the current chunk count.

## Compatibility

Existing callers can still use:

```python
CodeIndex(root).rebuild(path)
CodeIndex(root).search(query, limit)
```

Search results still expose `path`, `line`, and `snippet`, so `/search` and
`search_code` continue to emit `path:line:snippet`.

## Old Schema Handling

The previous schema only had `code_chunks(root, path, line, content)`. When the
new store sees that schema, it drops and recreates the code index tables with
schema version 2. Source files are the source of truth, so the old line-level
index is rebuilt rather than migrated row by row.

## Current Search Behavior

Search remains keyword based. It matches query terms against chunk content,
symbol names, qualified names, and chunk types. It is not vector search, semantic
search, BM25/FTS5 ranking, or hybrid retrieval.

## Deferred Work

The following are intentionally out of scope for Stage 3A:

- embeddings
- vector similarity
- jieba tokenization
- BM25/FTS5 hybrid ranking
- symbol reference resolution
- caller/callee analysis
- static call graph construction
- Playwright integration
- Memory v2

## Known Limits

- Tree-sitter can parse files with syntax errors, but chunks from malformed files
  should be treated as best-effort.
- TypeScript interface and type chunks are structural only; no reference or
  implementation relationship is inferred.
- Search ranking is simple and deterministic, optimized for compatibility rather
  than retrieval quality.

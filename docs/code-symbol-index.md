# Static Symbol Index

Stage 3C-1 added a conservative static symbol, import, and reference index on
top of the existing AST, lexical, and vector code index. Stage 3C-2 consumes this
data to build high-confidence static call edges, but symbol resolution remains a
separate precision-first layer.

## Parse Once

`analyze_source()` parses each supported AST file once and produces:

- AST chunks
- symbol definitions
- import bindings
- symbol references
- parse status

Unsupported text files still get a file-level fallback chunk, but they do not
pretend to have static symbols or references.

## Data Model

`SymbolDefinition` records the file path, language, symbol kind, name, qualified
name, optional container, declaration signature, source range, current chunk ID,
export/visibility metadata, and a declaration hash.

`ImportBinding` records module/package text, imported name, local alias,
relative import level, source range, resolved workspace file path when known, and
resolution status.

`SymbolReference` records reference kind, name, qualifier, enclosing symbol,
argument count, source range, resolved symbol ID, resolution status, confidence,
and reason.

## Stable Symbol IDs

Symbol IDs are SHA256 hashes of:

```text
language
file_path
symbol_kind
qualified_name
normalized declaration signature
```

They intentionally do not include function bodies, content hashes, line numbers,
chunk IDs, or timestamps. Changing a function body, moving a declaration within
the same file, or changing declaration whitespace keeps the symbol ID stable;
renaming, moving to another file, or changing the declaration signature creates a
new symbol ID.

The current declaration signature is the normalized declaration line. Python,
JavaScript, and TypeScript parameter names and annotations are included. Java
parameter names are also included in this conservative first version; this is
stable and auditable, but it is not compiler-level Java signature
canonicalization.

Chunk IDs remain useful for linking to the current AST chunk, but they are not
graph identity.

## Two-Phase Resolution

Extraction is local to each file. Resolution runs after changed file rows are
written:

```text
definitions + imports + workspace paths -> reference resolution
```

The resolver is conservative. It supports same-file exact definitions, explicit
static imports, simple relative module paths, `self`/`this` member lookup, and
name plus arity filtering when that is all the static data provides.

Resolution statuses:

- `resolved`
- `unresolved`
- `ambiguous`
- `dynamic`
- `external`

Confidence and reason values are stored with each reference. Multiple candidates
are marked ambiguous rather than choosing a result from file traversal order.

## Supported Scope

Python:

- classes, functions, async functions, methods, nested functions
- `import x`, `import x as y`, `from x import y`, relative imports
- direct calls, qualified calls, constructors, and `self` member calls
- dynamic constructs such as `getattr`, `globals`, and `eval` are not guessed

Java:

- package declarations, classes, interfaces, enums, methods, constructors
- normal imports, static imports, wildcard imports as uncertain bindings
- method invocation, constructor creation, static-style calls, and simple arity
  filtering
- no compiler-grade type inference or complete overload resolution

JavaScript and TypeScript:

- functions, classes, methods, interfaces, type aliases, named arrow functions
- default, named, alias, namespace, and simple `require` imports
- direct calls, member calls, constructor calls, namespace alias calls
- no bundler alias, tsconfig path mapping, prototype mutation, or dynamic import
  guessing

## SQLite Schema

Schema version: `5` introduced symbol tables. Stage 3C-2 migrates the database
to version `6` by adding call edges.

New tables:

- `symbol_definitions`
- `import_bindings`
- `symbol_references`

The v4 to v5 migration is additive. It preserves:

- `indexed_files`
- `code_chunks`
- `code_chunks_fts`
- `embedding_profiles`
- `chunk_embeddings`

The symbol tables may be empty immediately after migration. The next `update()`
backfills symbol data for parsed files. The later v6 call-edge migration is also
additive and does not reparse source files.

## Incremental Behavior

Changed or new files are parsed and replaced at file granularity. A file
replacement updates indexed file metadata, chunks, FTS rows, invalidates old
embeddings for replaced chunks, and replaces definitions/imports/references in a
single SQLite transaction.

After file updates and deletion cleanup, the resolver refreshes workspace
references from stored definitions and imports without reparsing unchanged
source files.

## Public APIs

`CodeIndex` adds:

```python
find_definitions(name, file_path=None, language=None, limit=20)
find_references(symbol_id_or_name, limit=100)
resolve_symbol_at(file_path, line, column=None)
```

The REPL exposes `/symbol` and `/references`. Built-in tools expose
`find_symbol` and `find_references`.

## Limitations

- Not a language server.
- No complete type inference.
- No full Java overload resolution.
- No wildcard import guessing.
- No dynamic import/eval/reflection resolution.
- Exact call edges are limited to high-confidence `call` and `constructor_call`
  references.
- No runtime dispatch or full call graph.
- No LLM or vector similarity is used for symbol resolution.

from __future__ import annotations

from functools import cache

import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser, Tree


@cache
def get_language(language: str) -> Language:
    if language == "python":
        return Language(tree_sitter_python.language())
    if language == "java":
        return Language(tree_sitter_java.language())
    if language == "javascript":
        return Language(tree_sitter_javascript.language())
    if language == "typescript":
        return Language(tree_sitter_typescript.language_typescript())
    msg = f"unsupported tree-sitter language: {language}"
    raise ValueError(msg)


@cache
def get_parser(language: str) -> Parser:
    return Parser(get_language(language))


def parse_source(language: str, source: bytes) -> Tree:
    return get_parser(language).parse(source)

from __future__ import annotations

import hashlib
from pathlib import Path

from tree_sitter import Node

from axiom.rag.languages import is_ast_language
from axiom.rag.models import CodeChunk
from axiom.rag.parser import parse_source


def chunk_source(file_path: str, language: str, source: str) -> tuple[list[CodeChunk], str]:
    if not is_ast_language(language):
        return [_file_chunk(file_path, language, source, "unsupported", True)], "unsupported"

    source_bytes = source.encode("utf-8")
    try:
        tree = parse_source(language, source_bytes)
    except Exception:
        return [_file_chunk(file_path, language, source, "parse_error", True)], "parse_error"

    parse_status = "parsed_with_errors" if tree.root_node.has_error else "parsed"
    chunks = [_file_chunk(file_path, language, source, parse_status, False)]
    chunks.extend(_walk_language(language, tree.root_node, source_bytes, file_path, parse_status))
    return chunks, parse_status


def _walk_language(
    language: str,
    root: Node,
    source: bytes,
    file_path: str,
    parse_status: str,
) -> list[CodeChunk]:
    chunks: list[CodeChunk] = []
    if language == "python":
        _walk_python(root, source, file_path, parse_status, [], chunks)
    elif language == "java":
        _walk_java(root, source, file_path, parse_status, [], chunks)
    elif language in {"javascript", "typescript"}:
        _walk_jsts(root, source, file_path, language, parse_status, [], chunks)
    return chunks


def _walk_python(
    node: Node,
    source: bytes,
    file_path: str,
    parse_status: str,
    parents: list[str],
    chunks: list[CodeChunk],
) -> None:
    next_parents = parents
    if node.type == "class_definition":
        next_parents = _append_symbol(
            node, source, file_path, "class", parse_status, parents, chunks
        )
    elif node.type == "function_definition":
        is_method = bool(parents)
        is_async = any(child.type == "async" for child in node.children)
        if is_method and is_async:
            chunk_type = "async_method"
        elif is_method:
            chunk_type = "method"
        elif is_async:
            chunk_type = "async_function"
        else:
            chunk_type = "function"
        next_parents = _append_symbol(
            node, source, file_path, chunk_type, parse_status, parents, chunks
        )
    for child in node.children:
        _walk_python(child, source, file_path, parse_status, next_parents, chunks)


def _walk_java(
    node: Node,
    source: bytes,
    file_path: str,
    parse_status: str,
    parents: list[str],
    chunks: list[CodeChunk],
) -> None:
    next_parents = parents
    type_map = {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "constructor_declaration": "constructor",
        "method_declaration": "method",
    }
    if node.type in type_map:
        next_parents = _append_symbol(
            node, source, file_path, type_map[node.type], parse_status, parents, chunks
        )
    for child in node.children:
        _walk_java(child, source, file_path, parse_status, next_parents, chunks)


def _walk_jsts(
    node: Node,
    source: bytes,
    file_path: str,
    language: str,
    parse_status: str,
    parents: list[str],
    chunks: list[CodeChunk],
) -> None:
    next_parents = parents
    type_map = {
        "class_declaration": "class",
        "function_declaration": "function",
        "method_definition": "method",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
    }
    if node.type in type_map:
        next_parents = _append_symbol(
            node, source, file_path, type_map[node.type], parse_status, parents, chunks
        )
    elif node.type == "variable_declarator" and _has_arrow_function(node):
        next_parents = _append_symbol(
            node, source, file_path, "arrow_function", parse_status, parents, chunks
        )
    for child in node.children:
        _walk_jsts(child, source, file_path, language, parse_status, next_parents, chunks)


def _append_symbol(
    node: Node,
    source: bytes,
    file_path: str,
    chunk_type: str,
    parse_status: str,
    parents: list[str],
    chunks: list[CodeChunk],
) -> list[str]:
    name = _node_text(node.child_by_field_name("name"), source)
    if not name:
        return parents
    qualified = ".".join([*parents, name]) if parents else name
    chunks.append(
        _chunk(
            file_path=file_path,
            language=_language_from_path(file_path),
            chunk_type=chunk_type,
            symbol_name=name,
            qualified_name=qualified,
            parent_symbol=parents[-1] if parents else None,
            start_line=node.start_point.row + 1,
            end_line=node.end_point.row + 1,
            content=_node_source(node, source),
            is_fallback=False,
            parse_status=parse_status,
        )
    )
    if chunk_type in {"class", "interface", "enum"}:
        return [*parents, name]
    return parents


def _has_arrow_function(node: Node) -> bool:
    value = node.child_by_field_name("value")
    return value is not None and value.type in {"arrow_function", "function"}


def _node_text(node: Node | None, source: bytes) -> str | None:
    if node is None:
        return None
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _node_source(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _file_chunk(
    file_path: str,
    language: str,
    source: str,
    parse_status: str,
    is_fallback: bool,
) -> CodeChunk:
    lines = source.splitlines()
    return _chunk(
        file_path=file_path,
        language=language,
        chunk_type="file",
        symbol_name=Path(file_path).name,
        qualified_name=file_path,
        parent_symbol=None,
        start_line=1,
        end_line=max(len(lines), 1),
        content=source,
        is_fallback=is_fallback,
        parse_status=parse_status,
    )


def _chunk(
    *,
    file_path: str,
    language: str,
    chunk_type: str,
    symbol_name: str | None,
    qualified_name: str | None,
    parent_symbol: str | None,
    start_line: int,
    end_line: int,
    content: str,
    is_fallback: bool,
    parse_status: str,
) -> CodeChunk:
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    stable = "|".join(
        [
            file_path,
            chunk_type,
            qualified_name or "",
            str(start_line),
            str(end_line),
            content_hash,
        ]
    )
    return CodeChunk(
        id=hashlib.sha256(stable.encode("utf-8")).hexdigest(),
        file_path=file_path,
        language=language,
        chunk_type=chunk_type,
        symbol_name=symbol_name,
        qualified_name=qualified_name,
        parent_symbol=parent_symbol,
        start_line=start_line,
        end_line=end_line,
        content=content,
        content_hash=content_hash,
        is_fallback=is_fallback,
        parse_status=parse_status,
    )


def _language_from_path(file_path: str) -> str:
    suffix = Path(file_path).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix == ".java":
        return "java"
    if suffix in {".js", ".jsx"}:
        return "javascript"
    if suffix in {".ts", ".tsx"}:
        return "typescript"
    return "text"

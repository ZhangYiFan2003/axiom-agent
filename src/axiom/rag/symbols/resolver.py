from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath

from axiom.rag.models import ImportBinding, SymbolDefinition, SymbolReference


def resolve_references(
    definitions: list[SymbolDefinition],
    imports: list[ImportBinding],
    references: list[SymbolReference],
) -> list[SymbolReference]:
    by_file_name: dict[tuple[str, str], list[SymbolDefinition]] = {}
    by_file_qualified: dict[tuple[str, str], SymbolDefinition] = {}
    by_name: dict[str, list[SymbolDefinition]] = {}
    for definition in definitions:
        by_name.setdefault(definition.name, []).append(definition)
        by_file_name.setdefault((definition.file_path, definition.name), []).append(definition)
        by_file_qualified[(definition.file_path, definition.qualified_name)] = definition

    imports_by_file: dict[str, list[ImportBinding]] = {}
    for binding in imports:
        imports_by_file.setdefault(binding.file_path, []).append(binding)

    resolved: list[SymbolReference] = []
    for reference in references:
        if reference.resolution_status == "dynamic":
            resolved.append(reference)
            continue
        candidates = _local_candidates(reference, by_file_name)
        reason = "same-file exact definition"
        confidence = 1.0
        if not candidates and reference.qualifier in {"self", "this"}:
            candidates = _member_candidates(reference, definitions)
            reason = "self/this member exact definition"
            confidence = 0.90
        if not candidates:
            candidates = _import_candidates(
                reference,
                imports_by_file,
                by_file_name,
                by_file_qualified,
            )
            reason = "static import binding"
            confidence = 0.95
        if not candidates and reference.name in by_name:
            candidates = by_name[reference.name]
            reason = "workspace name match without import"
            confidence = 0.60
        if not candidates:
            status = "external" if _looks_external(reference, imports_by_file) else "unresolved"
            resolved.append(
                replace(
                    reference,
                    resolution_status=status,
                    resolution_confidence=0.0,
                    resolution_reason=status,
                )
            )
        elif len(candidates) == 1:
            resolved.append(
                replace(
                    reference,
                    resolved_symbol_id=candidates[0].id,
                    resolution_status="resolved",
                    resolution_confidence=confidence,
                    resolution_reason=reason,
                )
            )
        else:
            resolved.append(
                replace(
                    reference,
                    resolution_status="ambiguous",
                    resolution_confidence=0.0,
                    resolution_reason=f"{len(candidates)} candidates",
                )
            )
    return resolved


def resolve_import_paths(
    imports: list[ImportBinding],
    file_paths: set[str],
) -> list[ImportBinding]:
    result: list[ImportBinding] = []
    for binding in imports:
        target = _resolve_module_path(binding, file_paths)
        status = binding.resolution_status
        if target:
            status = "resolved"
        elif not binding.module_name.startswith(".") and binding.relative_level == 0:
            status = "external"
        result.append(
            replace(binding, resolved_file_path=target, resolution_status=status)
        )
    return result


def _local_candidates(
    reference: SymbolReference,
    by_file_name: dict[tuple[str, str], list[SymbolDefinition]],
) -> list[SymbolDefinition]:
    if reference.qualifier:
        return []
    candidates = by_file_name.get((reference.file_path, reference.name), [])
    return _filter_arity(candidates, reference.argument_count)


def _member_candidates(
    reference: SymbolReference,
    definitions: list[SymbolDefinition],
) -> list[SymbolDefinition]:
    if not reference.enclosing_qualified_name:
        return []
    container = reference.enclosing_qualified_name.split(".")[0]
    candidates = [
        definition
        for definition in definitions
        if definition.file_path == reference.file_path
        and definition.name == reference.name
        and definition.container_qualified_name == container
    ]
    return _filter_arity(candidates, reference.argument_count)


def _import_candidates(
    reference: SymbolReference,
    imports_by_file: dict[str, list[ImportBinding]],
    by_file_name: dict[tuple[str, str], list[SymbolDefinition]],
    by_file_qualified: dict[tuple[str, str], SymbolDefinition],
) -> list[SymbolDefinition]:
    matches: list[SymbolDefinition] = []
    for binding in imports_by_file.get(reference.file_path, []):
        if binding.resolution_status != "resolved" or not binding.resolved_file_path:
            continue
        if binding.local_name == reference.qualifier and reference.qualifier:
            prefix = binding.imported_name or ""
            qualified = (
                f"{prefix}.{reference.name}"
                if prefix not in {"*", "default"}
                else reference.name
            )
            found = by_file_qualified.get((binding.resolved_file_path, qualified))
            if found:
                matches.append(found)
            matches.extend(by_file_name.get((binding.resolved_file_path, reference.name), []))
        elif binding.local_name == reference.name:
            imported_name = binding.imported_name or reference.name
            if imported_name in {"default", "*"}:
                matches.extend(by_file_name.get((binding.resolved_file_path, reference.name), []))
            else:
                matches.extend(by_file_name.get((binding.resolved_file_path, imported_name), []))
        elif binding.imported_name == reference.name:
            matches.extend(by_file_name.get((binding.resolved_file_path, reference.name), []))
    return _filter_arity(_unique(matches), reference.argument_count)


def _resolve_module_path(binding: ImportBinding, file_paths: set[str]) -> str | None:
    module = binding.module_name
    candidates: list[str] = []
    if binding.language == "python":
        base = PurePosixPath(binding.file_path).parent
        if binding.relative_level:
            for _ in range(max(binding.relative_level - 1, 0)):
                base = base.parent
            module_path = module.replace(".", "/")
            candidates.extend(
                [
                    str(base / f"{module_path}.py"),
                    str(base / module_path / "__init__.py"),
                ]
            )
        else:
            module_path = module.replace(".", "/")
            candidates.extend([f"{module_path}.py", f"{module_path}/__init__.py"])
    elif binding.language == "java":
        candidates.append(binding.module_name.replace(".", "/") + ".java")
    elif binding.language in {"javascript", "typescript"}:
        base = PurePosixPath(binding.file_path).parent
        raw = PurePosixPath(module)
        target = base / raw if module.startswith(".") else raw
        root = str(target).removeprefix("./")
        candidates.extend(
            [
                root,
                f"{root}.ts",
                f"{root}.tsx",
                f"{root}.js",
                f"{root}.jsx",
                f"{root}/index.ts",
                f"{root}/index.js",
            ]
        )
    for candidate in candidates:
        normalized = PurePosixPath(candidate).as_posix()
        if normalized in file_paths:
            return normalized
    return None


def _filter_arity(
    candidates: list[SymbolDefinition],
    argument_count: int | None,
) -> list[SymbolDefinition]:
    if argument_count is None:
        return candidates
    filtered = [
        candidate
        for candidate in candidates
        if candidate.signature is None
        or _parameter_count(candidate.signature) in {None, argument_count}
    ]
    return filtered or candidates


def _parameter_count(signature: str) -> int | None:
    if "(" not in signature or ")" not in signature:
        return None
    raw = signature.split("(", 1)[1].split(")", 1)[0].strip()
    if not raw:
        return 0
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if parts and parts[0] in {"self", "this"}:
        parts = parts[1:]
    return len(parts)


def _unique(candidates: list[SymbolDefinition]) -> list[SymbolDefinition]:
    result: dict[str, SymbolDefinition] = {}
    for candidate in candidates:
        result[candidate.id] = candidate
    return sorted(result.values(), key=lambda item: (item.file_path, item.qualified_name))


def _looks_external(
    reference: SymbolReference,
    imports_by_file: dict[str, list[ImportBinding]],
) -> bool:
    return any(
        binding.resolution_status == "external"
        and (binding.local_name == reference.name or binding.local_name == reference.qualifier)
        for binding in imports_by_file.get(reference.file_path, [])
    )

from __future__ import annotations

from pathlib import Path

AST_SUFFIXES = {
    ".py": "python",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}

TEXT_SUFFIXES = {
    *AST_SUFFIXES.keys(),
    ".kt",
    ".go",
    ".rs",
    ".md",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".xml",
    ".html",
    ".css",
    ".scss",
    ".sql",
    ".sh",
}

SKIP_DIRS = {".git", ".venv", "node_modules", "dist", "build", "target", "__pycache__"}
SUPPORTED_AST_LANGUAGES = frozenset(AST_SUFFIXES.values())


def detect_language(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    return AST_SUFFIXES.get(suffix, "text")


def is_indexable(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES


def is_ast_language(language: str) -> bool:
    return language in SUPPORTED_AST_LANGUAGES

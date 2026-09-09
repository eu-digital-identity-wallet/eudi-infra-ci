#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

SKIP_DIR_NAMES = {
    ".git",
    ".gradle",
    ".eudi-infra-ci",
    "build",
    "node_modules",
    "DerivedData",
    "__pycache__",
    ".idea",
}


def load_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _should_skip(path: Path) -> bool:
    return any(part in SKIP_DIR_NAMES for part in path.parts)


def build_source_index(roots: Sequence[str]) -> List[Path]:
    files: List[Path] = []
    seen = set()
    for root in roots:
        base = Path(root)
        if not base.exists():
            continue
        if base.is_file():
            resolved = base.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(base)
            continue
        for path in base.rglob("*"):
            if not path.is_file() or _should_skip(path):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(path)
    return files


def resolve_source_file(filename: str, indexed_files: Sequence[Path]) -> Optional[Path]:
    if not filename:
        return None
    rel = filename.lstrip("./").replace("\\", "/")
    suffix = "/" + rel
    for path in indexed_files:
        posix = path.as_posix().replace("\\", "/")
        if posix.endswith(suffix) or posix.endswith(rel) or posix == rel:
            return path
    basename = Path(rel).name
    matches = [path for path in indexed_files if path.name == basename]
    if len(matches) == 1:
        return matches[0]
    return None


def collect_sources(
    filenames: Iterable[str],
    roots: Sequence[str],
) -> Dict[str, str]:
    indexed = build_source_index(roots)
    sources: Dict[str, str] = {}
    for filename in filenames:
        if filename in sources:
            continue
        path = resolve_source_file(filename, indexed)
        if path is None:
            continue
        text = load_text(path)
        if text is not None:
            sources[filename] = text
    return sources

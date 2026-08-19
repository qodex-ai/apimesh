import re
from pathlib import Path
from typing import List

from config import Configurations

config = Configurations()

# The annotations that make a file worth parsing. A file carrying none of them
# defines no Spring endpoint, and the extractor would walk it for nothing.
SPRING_WEB_ANNOTATIONS = (
    "RestController",
    "Controller",
    "RequestMapping",
    "GetMapping",
    "PostMapping",
    "PutMapping",
    "DeleteMapping",
    "PatchMapping",
)

_ANNOTATION_PATTERN = re.compile(
    r"@\s*(?:" + "|".join(SPRING_WEB_ANNOTATIONS) + r")\b"
)

# An interface that only passes a contract along carries no annotation of its
# own, and a controller reaches the mappings it serves through it. Leaving such
# a file unparsed broke the chain in the middle and lost every one of them.
_EXTENDING_INTERFACE_PATTERN = re.compile(
    r"\binterface\s+\w+\s*(?:<[^>]*>)?\s+extends\b"
)


def _is_ignored(path: Path, base_path: Path) -> bool:
    # Only components below the scanned repo may be ignored, otherwise a repo
    # living under /var, /tmp or /build discovers nothing.
    try:
        relative = path.relative_to(base_path)
    except ValueError:
        relative = path
    return any(part in config.ignored_dirs for part in relative.parts)


def find_java_files(directory: str) -> List[Path]:
    base_path = Path(directory)
    java_files: List[Path] = []
    for file_path in base_path.rglob("*.java"):
        if _is_ignored(file_path, base_path):
            continue
        java_files.append(file_path)
    return java_files


def file_contains_api_defs(file_path: Path) -> bool:
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(
        _ANNOTATION_PATTERN.search(text) or _EXTENDING_INTERFACE_PATTERN.search(text)
    )


def find_api_definition_files(directory: str) -> List[str]:
    java_files = find_java_files(directory)
    java_files.sort()
    return [str(path) for path in java_files if file_contains_api_defs(path)]

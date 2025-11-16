from pathlib import Path
from typing import List

from config import Configurations

config = Configurations()


def _is_ignored(path: Path) -> bool:
    return any(part in config.ignored_dirs for part in path.parts)


def _is_test_file(path: Path) -> bool:
    return path.name.endswith("_test.go")


def _looks_like_routing_file(path: Path) -> bool:
    """
    Heuristic to bubble up files that are likely to contain router definitions.
    """
    lowered = path.as_posix().lower()
    candidates = (
        "route",
        "router",
        "handler",
        "controller",
        "server",
        "api",
        "http",
    )
    return any(token in lowered for token in candidates)


def find_go_files(directory: str) -> List[Path]:
    base_path = Path(directory)
    go_files: List[Path] = []
    for file_path in base_path.rglob("*.go"):
        if _is_ignored(file_path) or _is_test_file(file_path):
            continue
        go_files.append(file_path)
    return go_files


def find_api_definition_files(directory: str) -> List[str]:
    go_files = find_go_files(directory)
    go_files.sort(key=lambda p: (0 if _looks_like_routing_file(p) else 1, str(p)))
    return [str(path) for path in go_files]


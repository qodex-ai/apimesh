from pathlib import Path
from typing import List

from repo_to_swagger.config import Configurations

config = Configurations()


def _is_ignored(path: Path) -> bool:
    return any(part in config.ignored_dirs for part in path.parts)


def _looks_like_controller(path: Path) -> bool:
    if "app" not in path.parts:
        return False
    if "controllers" not in path.parts:
        return False
    return path.name.endswith("_controller.rb")


def _looks_like_route_file(path: Path) -> bool:
    return path.as_posix().endswith("config/routes.rb")


def find_ruby_files(directory: str) -> List[Path]:
    directory_path = Path(directory)
    ruby_files: List[Path] = []
    for file_path in directory_path.rglob("*.rb"):
        if not _is_ignored(file_path):
            ruby_files.append(file_path)
    return ruby_files


def find_api_definition_files(directory: str) -> List[str]:
    ruby_files = find_ruby_files(directory)
    api_files: List[str] = []

    for ruby_file in ruby_files:
        if _looks_like_route_file(ruby_file):
            api_files.append(str(ruby_file))
            continue
        if _looks_like_controller(ruby_file):
            api_files.append(str(ruby_file))

    api_files.sort(
        key=lambda path: 0 if path.endswith("config/routes.rb") else 1
    )
    return api_files

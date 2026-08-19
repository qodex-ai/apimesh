from pathlib import Path
from typing import List

from config import Configurations
from rails_pipeline.identify_api_functions import is_route_file

config = Configurations()


def _is_ignored(path: Path, root: Path) -> bool:
    # Only components below the scanned root count, otherwise a repo checked out
    # under /var, /tmp or /build would be skipped entirely.
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return any(part in config.ignored_dirs for part in relative.parts)


def _looks_like_controller(path: Path) -> bool:
    if "app" not in path.parts:
        return False
    if "controllers" not in path.parts:
        return False
    return path.name.endswith("_controller.rb")


def _looks_like_route_file(path: Path) -> bool:
    return is_route_file(path)


def find_ruby_files(directory: str) -> List[Path]:
    directory_path = Path(directory)
    ruby_files: List[Path] = []
    for file_path in directory_path.rglob("*.rb"):
        if not _is_ignored(file_path, directory_path):
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

    # Every route file is parsed before the first controller, so the route map
    # is complete by the time a controller is matched against it.
    api_files.sort(key=lambda path: 0 if is_route_file(path) else 1)
    return api_files

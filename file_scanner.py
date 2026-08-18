import os
from typing import List
from config import Configurations
from utils import get_repo_path
import re

config = Configurations()
class FileScanner:

    def __init__(self):
        pass

    def get_all_file_paths(self) -> List[str|bytes]:
        """
        Get all file paths in the repository, ignoring specified directories
        """
        repo_path = get_repo_path()
        file_paths = []
        supported_extensions = ('.py', '.js', '.ts', '.java', '.rb', '.go')

        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in config.ignored_dirs]

            if not self.should_process_directory(root):
                continue

            for file in files:
                if file.endswith(supported_extensions):
                    file_path = os.path.join(root, file)
                    file_paths.append(file_path)
        return file_paths

    @staticmethod
    def find_api_files(file_paths, framework):
        patterns = config.routing_patters_map.get(framework)
        if not patterns:
            print(f"Warning: No routing patterns configured for framework '{framework or 'unknown'}'. Scanning all supported files.")
            return list(file_paths)
        api_files = []
        for file_path in file_paths:
            try:
                with open(file_path, 'r', encoding='utf-8') as file:
                    content = file.read()
                    if any(re.search(pattern, content) for pattern in patterns):
                        if framework == "ruby_on_rails":
                            if file_path.endswith('.rb'):
                                api_files.append(file_path)
                        else:
                            api_files.append(file_path)
            except (UnicodeDecodeError, FileNotFoundError):
                continue
        return api_files

    @staticmethod
    def should_process_directory(dir_path: str) -> bool:
        """
        Check if a directory should be processed or ignored.

        Only the components below the repo root are checked. Matching on the absolute
        path would skip every file in a repo that happens to live under /var, /tmp,
        /build and other names present in ignored_dirs.
        """
        repo_path = get_repo_path()
        try:
            relative_path = os.path.relpath(os.path.abspath(dir_path), repo_path)
        except ValueError:
            # Different drives on Windows: nothing to compare, so do not ignore it.
            return True
        path_parts = [part for part in relative_path.split(os.sep) if part not in ("", os.curdir)]
        if os.pardir in path_parts:
            # Outside the repo root, so the repo's ignore list does not apply.
            return True
        return not any(part in config.ignored_dirs for part in path_parts)

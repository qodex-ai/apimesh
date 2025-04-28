import os
from typing import List
from repo_to_swagger.config import Configurations
import re

config = Configurations()
class FileScanner:

    def __init__(self):
        pass

    def get_all_file_paths(self, repo_path) -> List[str|bytes]:
        """
        Get all file paths in the repository, ignoring specified directories
        """
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
        patterns = config.routing_patters_map[framework]
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
        Check if a directory should be processed or ignored
        """
        path_parts = dir_path.split(os.sep)
        return not any(part in config.ignored_dirs for part in path_parts)
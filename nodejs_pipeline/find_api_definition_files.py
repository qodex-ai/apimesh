import re
from pathlib import Path
from config import Configurations

config = Configurations()

API_DECORATOR_NAMES = {
    'route', 'get', 'post', 'put', 'delete', 'patch',
    'api', 'endpoint', 'router', 'controller', 'middleware', 'rest'
}

# Regex patterns to detect API routes or decorators
ROUTE_METHOD_PATTERN = re.compile(
    r'\b(app|router|route)\s*\.\s*(' + '|'.join(API_DECORATOR_NAMES) + r')\s*\(',
    re.IGNORECASE
)

DECORATOR_PATTERN = re.compile(
    r'@\s*(' + '|'.join(API_DECORATOR_NAMES) + r')\b',
    re.IGNORECASE
)

def find_node_files(directory):
    directory = Path(directory)
    node_files = []
    for file in directory.rglob('*'):
        if file.suffix in ('.js'):
            if not any(part in config.ignored_dirs for part in file.parts):
                node_files.append(file)
    return node_files

def file_contains_api_defs(file_path):
    try:
        text = file_path.read_text(encoding='utf-8')
    except Exception:
        return False

    if ROUTE_METHOD_PATTERN.search(text):
        return True

    if DECORATOR_PATTERN.search(text):
        return True

    return False

def find_api_definition_files(directory):
    node_files = find_node_files(directory)
    api_files = []
    for node_file in node_files:
        if file_contains_api_defs(node_file):
            api_files.append(str(node_file))
    return api_files

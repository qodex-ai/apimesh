import re
from pathlib import Path
from config import Configurations
from nodejs_pipeline.constants import SUPPORTED_NODE_FILE_EXTENSIONS

config = Configurations()

API_DECORATOR_NAMES = {
    'route', 'get', 'post', 'put', 'delete', 'patch', 'options', 'head', 'all',
    'api', 'endpoint', 'router', 'controller', 'module', 'middleware', 'rest'
}

HTTP_METHODS = ['get', 'post', 'put', 'delete', 'patch', 'options', 'head']
ROUTE_OBJECT_PREFIXES = ['app', 'router', 'route', 'api', 'controller', 'server']
ROUTE_OBJECT_SUFFIXES = ['Router', 'Routes', 'Api', 'Controller', 'App', 'Server']

route_prefix_pattern = r'(?:' + '|'.join(ROUTE_OBJECT_PREFIXES) + r')'
route_suffix_pattern = r'(?:[A-Za-z_$][\w$]*?(?:' + '|'.join(ROUTE_OBJECT_SUFFIXES) + r'))'
route_object_pattern = r'(?:' + route_prefix_pattern + r'|' + route_suffix_pattern + r')'

# Regex patterns to detect API routes or decorators
ROUTE_METHOD_PATTERN = re.compile(
    r'\b' + route_object_pattern + r'\s*\.\s*(?:' + '|'.join(HTTP_METHODS) + r')\s*\(',
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
        if file.suffix and file.suffix.lower() in SUPPORTED_NODE_FILE_EXTENSIONS:
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

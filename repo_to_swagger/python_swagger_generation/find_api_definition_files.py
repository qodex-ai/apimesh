from pathlib import Path
import ast
from repo_to_swagger.config import Configurations

config = Configurations()

API_DECORATOR_NAMES = {
    'route', 'get', 'post', 'put', 'delete', 'patch',
    'api', 'endpoint', 'router', 'viewset', 'view'
}
def find_python_files(directory):
    directory = Path(directory)
    python_files = []
    for py_file in directory.rglob('*.py'):
        # Check if any parent directory is in IGNORE_DIRS
        if not any(part in config.ignored_dirs for part in py_file.parts):
            python_files.append(py_file)
    return python_files

def has_api_decorator(decorator_node):
    if isinstance(decorator_node, ast.Call) and hasattr(decorator_node.func, 'attr'):
        if decorator_node.func.attr.lower() in API_DECORATOR_NAMES:
            return True
    if isinstance(decorator_node, ast.Attribute):
        if decorator_node.attr.lower() in API_DECORATOR_NAMES:
            return True
    if isinstance(decorator_node, ast.Name):
        if decorator_node.id.lower() in API_DECORATOR_NAMES:
            return True
    return False

def file_contains_api_defs(file_path):
    try:
        source = file_path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(file_path))
    except Exception:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                if has_api_decorator(decorator):
                    return True
        if isinstance(node, ast.ClassDef):
            for decorator in node.decorator_list:
                if has_api_decorator(decorator):
                    return True
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id.lower() in API_DECORATOR_NAMES:
                    return True
                if isinstance(base, ast.Attribute) and base.attr.lower() in API_DECORATOR_NAMES:
                    return True
    return False

def find_api_definition_files(directory):
    py_files = find_python_files(directory)
    api_files = []
    for py_file in py_files:
        if file_contains_api_defs(py_file):
            api_files.append(str(py_file))
    return api_files

# directory = Path('/Users/ankits/PycharmProjects/data-science-model-serving')
# api_files = find_api_definition_files(directory)
# print(api_files)
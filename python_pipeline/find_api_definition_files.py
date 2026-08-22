from pathlib import Path
import ast
from config import Configurations
# The selector and the extractor have to recognise the same things, otherwise a
# file is scanned and then reports no endpoint at all.
from python_pipeline.identify_api_functions import has_api_base_class, has_api_decorator

config = Configurations()

def find_python_files(directory):
    directory = Path(directory)
    python_files = []
    for py_file in directory.rglob('*.py'):
        # Only the components below the scanned root count: an absolute path that
        # happens to sit under /var or /tmp/build would otherwise hide the repo.
        if not any(part in config.ignored_dirs for part in py_file.relative_to(directory).parts):
            python_files.append(py_file)
    return python_files

def parse_python_file(file_path):
    """The parsed tree of one source file, or None when it cannot be read."""
    try:
        source = Path(file_path).read_text(encoding='utf-8', errors='replace')
        return ast.parse(source, filename=str(file_path))
    except Exception:
        return None

def file_contains_api_defs(file_path, tree=None):
    if tree is None:
        tree = parse_python_file(file_path)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if has_api_decorator(decorator):
                    return True
        if isinstance(node, ast.ClassDef):
            for decorator in node.decorator_list:
                if has_api_decorator(decorator):
                    return True
            if has_api_base_class(node):
                return True
    return False

def find_api_definition_sources(directory):
    """(path, parsed tree) for every file that declares an API.

    The tree is handed to the extractor, so a file is parsed once per run
    instead of once here and again there.
    """
    sources = []
    for py_file in find_python_files(directory):
        tree = parse_python_file(py_file)
        if tree is not None and file_contains_api_defs(py_file, tree=tree):
            sources.append((py_file, tree))
    return sources

def find_api_definition_files(directory):
    return [str(py_file) for py_file, _ in find_api_definition_sources(directory)]

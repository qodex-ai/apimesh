from tree_sitter import Language, Parser, QueryCursor
import tree_sitter_python
import ast
import importlib.util
import os
import sys
from repo_to_swagger.config import Configurations

config = Configurations()

PY_LANGUAGE = Language(tree_sitter_python.language())

parser = Parser(PY_LANGUAGE)


def parse_file(filename):
    with open(filename, 'r', encoding='utf-8') as f:
        code = f.read()
    tree = parser.parse(code.encode('utf-8'))
    return tree, code


def get_module_origin(module_name, base_directory=None):
    try:
        original_path = sys.path.copy()
        if base_directory and base_directory not in sys.path:
            sys.path.insert(0, base_directory)

        spec = importlib.util.find_spec(module_name)
        if spec and spec.origin:
            return spec.origin
        elif spec is None:
            return "<built-in>"
    except Exception:
        pass
    finally:
        sys.path = original_path

    if base_directory and "." in module_name:
        parts = module_name.split(".")
        potential_path = os.path.join(base_directory, *parts)
        for ext in (".py", "/__init__.py"):
            candidate = potential_path + ext
            if os.path.exists(candidate):
                return candidate
    return None


def find_import_usages(tree, imported_names):
    """Find lines where imported names are used in the code."""
    query = PY_LANGUAGE.query("""
        (identifier) @ident
    """)

    cursor = QueryCursor(query)
    captures = cursor.captures(tree.root_node)

    usages = {name: [] for name in imported_names}

    for node in captures.get("ident", []):
        name = node.text.decode("utf-8")
        if name in imported_names:
            line = node.start_point[0] + 1
            if line not in usages[name]:  # Avoid duplicates
                usages[name].append(line)

    return usages


def analyze_imports(filepath, base_directory=None, tree=None):
    imports = []
    imported_names = set()  # Track imported names for usage lookup
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            source = f.read()
            tree_ast = ast.parse(source, filename=filepath)

        for node in ast.walk(tree_ast):
            if isinstance(node, ast.ImportFrom):
                module = node.module
                if module is None:
                    continue  # skip relative imports
                origin = get_module_origin(module, base_directory)
                for alias in node.names:
                    name = alias.asname if alias.asname else alias.name
                    imported_names.add(name)
                    imports.append({
                        'type': 'import',
                        'imported_name': alias.name,
                        'from_module': module,
                        'origin': origin,
                        'line': node.lineno,
                        'path_exists': False,  # Will be updated later
                        'usage_lines': []  # Will be populated later
                    })
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname if alias.asname else alias.name
                    imported_names.add(name)
                    origin = get_module_origin(alias.name, base_directory)
                    imports.append({
                        'type': 'import',
                        'imported_name': alias.name,
                        'from_module': None,
                        'origin': origin,
                        'line': node.lineno,
                        'path_exists': False,  # Will be updated later
                        'usage_lines': []  # Will be populated later
                    })
    except Exception as e:
        print(f"Error analyzing imports in {filepath}: {str(e)}")

    # Find where imported names are used
    if tree and imported_names:
        usages = find_import_usages(tree, imported_names)
        for import_item in imports:
            name = import_item['imported_name']
            if import_item.get('asname'):
                name = import_item['asname']
            import_item['usage_lines'] = list(set(usages.get(name, [])) - set([import_item['line']]))

    return imports


def get_elements(tree):
    query = PY_LANGUAGE.query("""
        (class_definition
            name: (identifier) @class-name) @class
        (function_definition
            name: (identifier) @func-name) @function
        (assignment
            left: (identifier) @var-name) @variable
        (call
            function: (identifier) @called-func) @func-call
        (call
            function: (attribute
                attribute: (identifier) @method-name)) @method-call
        (import_statement
            name: (dotted_name (identifier) @imported-func))
        (import_from_statement
            name: (dotted_name (identifier) @imported-func))
    """)

    cursor = QueryCursor(query)
    captures = cursor.captures(tree.root_node)

    elements = {
        'classes': [],
        'functions': [],
        'variables': [],
        'function_calls': [],
    }

    # Collect function definitions for cross-referencing
    function_defs = {}
    for node in captures.get("func-name", []):
        func_name = node.text.decode("utf-8")
        elements['functions'].append({
            'type': 'function',
            'name': func_name,
            'start_line': node.start_point[0] + 1,
            'end_line': node.parent.end_point[0] + 1
        })
        function_defs[func_name] = {
            'start_line': node.start_point[0] + 1,
            'end_line': node.parent.end_point[0] + 1
        }

    for node in captures.get("class-name", []):
        elements['classes'].append({
            'type': 'class',
            'name': node.text.decode("utf-8"),
            'start_line': node.start_point[0] + 1,
            'end_line': node.parent.end_point[0] + 1
        })

    for node in captures.get("var-name", []):
        elements['variables'].append({
            'type': 'variable',
            'name': node.text.decode("utf-8"),
            'start_line': node.start_point[0] + 1,
            'end_line': node.parent.end_point[0] + 1
        })

    for node in captures.get("called-func", []):
        func_name = node.text.decode("utf-8")
        call_info = {
            'type': 'function_call',
            'name': func_name,
            'start_line': node.start_point[0] + 1,
            'end_line': node.parent.end_point[0] + 1
        }
        if func_name in function_defs:
            call_info['function_start_line'] = function_defs[func_name]['start_line']
            call_info['function_end_line'] = function_defs[func_name]['end_line']
        elements['function_calls'].append(call_info)

    for node in captures.get("method-name", []):
        method_name = node.text.decode("utf-8")
        call_info = {
            'type': 'function_call',
            'name': method_name,
            'start_line': node.start_point[0] + 1,
            'end_line': node.parent.end_point[0] + 1
        }
        if method_name in function_defs:
            call_info['function_start_line'] = function_defs[method_name]['start_line']
            call_info['function_end_line'] = function_defs[method_name]['end_line']
        elements['function_calls'].append(call_info)
    return elements


def check_path_exists(imports, base_directory):
    for import_item in imports:
        origin = import_item.get('origin')
        if origin and origin != "<built-in>" and os.path.isabs(origin):
            try:
                origin = os.path.normpath(origin)
                base_directory = os.path.normpath(base_directory)
                if os.path.exists(origin):
                    common_prefix = os.path.commonpath([origin, base_directory])
                    import_item['path_exists'] = common_prefix == base_directory or origin.startswith(base_directory)
                else:
                    import_item['path_exists'] = False
            except Exception:
                import_item['path_exists'] = False
        else:
            import_item['path_exists'] = False
    return imports


def process_file(filename, base_directory=None):
    if not base_directory:
        base_directory = os.path.dirname(filename)
    tree, code = parse_file(filename)
    elements = get_elements(tree)
    imports = analyze_imports(filename, base_directory, tree)
    imports = check_path_exists(imports, base_directory)
    return {
        'filename': filename,
        'elements': elements,
        'imports': imports
    }


def should_process_directory(dir_path: str) -> bool:
    """
    Check if a directory should be processed or ignored
    """
    path_parts = dir_path.split(os.sep)
    return not any(part in config.ignored_dirs for part in path_parts)


if __name__ == "__main__":
    import json
    filename = "/Users/ankits/PycharmProjects/qpulse-backend/python_scripts/interactive_ai_agent/tools/get_test_scenario_tags.py"
    base_directory = "/Users/ankits/PycharmProjects/qpulse-backend"
    if os.path.exists(filename) and should_process_directory(filename) and filename.endswith(".py"):
        result = process_file(filename, base_directory)
        print(json.dumps(result, indent=2))
    else:
        print(f"File {filename} not found")
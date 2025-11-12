from pathlib import Path
import ast
import json

API_DECORATOR_NAMES = {
    'route', 'get', 'post', 'put', 'delete', 'patch',
    'api', 'endpoint', 'router', 'viewset', 'view'
}

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


def extract_route_from_decorator(decorator_node):
    if isinstance(decorator_node, ast.Call):
        if decorator_node.args:
            first_arg = decorator_node.args[0]
            if isinstance(first_arg, ast.Str):
                return first_arg.s
            elif isinstance(first_arg, ast.Constant):  # For Python 3.8+
                if isinstance(first_arg.value, str):
                    return first_arg.value
    return None


def find_api_endpoints(file_path):
    try:
        source = file_path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(file_path))
    except Exception:
        return []
    endpoints = []
    class_endpoints = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not isinstance(getattr(node, 'parent', None),
                                                                                        ast.ClassDef):
            for dec in node.decorator_list:
                if has_api_decorator(dec):
                    route = extract_route_from_decorator(dec)
                    endpoints.append({
                        "type": "function",
                        "name": node.name,
                        "start_line": node.lineno,
                        "end_line": getattr(node, 'end_lineno', None),
                        "route": route,
                        "file_path": str(file_path)
                    })
        if isinstance(node, ast.ClassDef):
            class_has_decorator = any(has_api_decorator(dec) for dec in node.decorator_list)
            class_route = None
            for dec in node.decorator_list:
                if has_api_decorator(dec):
                    class_route = extract_route_from_decorator(dec)
                    break
            if class_has_decorator:
                class_endpoint = {
                    "type": "class",
                    "name": node.name,
                    "start_line": node.lineno,
                    "end_line": getattr(node, 'end_lineno', None),
                    "route": class_route,
                    "file_path": str(file_path),
                    "methods": []
                }
                class_endpoints[node.name] = class_endpoint
                endpoints.append(class_endpoint)
            for body_item in node.body:
                if isinstance(body_item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_route = None
                    method_has_decorator = any(has_api_decorator(dec) for dec in body_item.decorator_list)
                    if method_has_decorator:
                        for dec in body_item.decorator_list:
                            if has_api_decorator(dec):
                                method_route = extract_route_from_decorator(dec)
                                if method_route:
                                    break
                    if method_has_decorator or class_has_decorator:
                        method_entry = {
                            "type": "method",
                            "name": body_item.name,
                            "start_line": body_item.lineno,
                            "end_line": getattr(body_item, 'end_lineno', None),
                            "route": method_route if method_route else class_route,
                            "file_path": str(file_path)
                        }
                        if node.name in class_endpoints:
                            class_endpoints[node.name]["methods"].append(method_entry)
    return endpoints


def set_parents(tree):
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node


if __name__ == "__main__":
    api_files = ['/Users/ankits/PycharmProjects/data-science-model-serving/app.py', '/Users/ankits/PycharmProjects/data-science-model-serving/apps/training/run.py', '/Users/ankits/PycharmProjects/data-science-model-serving/apps/prediction/run.py']
    py_files = [Path(file) for file in api_files]  # Convert to Path objects
    all_endpoints = []
    for py_file in py_files:
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
            set_parents(tree)
            eps = find_api_endpoints(py_file)
            if eps:
                all_endpoints.extend(eps)
        except Exception:
            continue
    print(json.dumps(all_endpoints, indent=2))
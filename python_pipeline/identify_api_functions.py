from pathlib import Path
import ast
import json
import os

API_DECORATOR_NAMES = {
    'route', 'get', 'post', 'put', 'delete', 'patch',
    'api', 'endpoint', 'router', 'viewset', 'view'
}

HTTP_METHOD_DECORATOR_NAMES = {
    'get', 'post', 'put', 'delete', 'patch', 'options', 'head'
}

# Constructors that create a route group, and the calls that mount one.
ROUTER_FACTORY_KINDS = {'Blueprint': 'blueprint', 'APIRouter': 'router'}
REGISTER_CALL_KINDS = {'register_blueprint': 'blueprint', 'include_router': 'router'}
PREFIX_KEYWORDS = ('url_prefix', 'prefix')

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


def extract_http_method(decorator_node):
    name = None
    if isinstance(decorator_node, ast.Call):
        if hasattr(decorator_node.func, 'attr'):
            name = decorator_node.func.attr
        elif isinstance(decorator_node.func, ast.Name):
            name = decorator_node.func.id
    elif isinstance(decorator_node, ast.Attribute):
        name = decorator_node.attr
    elif isinstance(decorator_node, ast.Name):
        name = decorator_node.id
    if not name:
        return None
    lower = name.lower()
    if lower in HTTP_METHOD_DECORATOR_NAMES:
        return lower.upper()
    return None


def extract_route_from_decorator(decorator_node):
    if isinstance(decorator_node, ast.Call):
        if decorator_node.args:
            first_arg = decorator_node.args[0]
            if isinstance(first_arg, ast.Constant):
                if isinstance(first_arg.value, str):
                    return first_arg.value
    return None


def _dotted_name(node):
    """Source text of a Name/Attribute node as a dotted string, None otherwise."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _string_literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _keyword_string(call_node, keyword_names):
    for keyword in call_node.keywords:
        if keyword.arg in keyword_names:
            return _string_literal(keyword.value)
    return None


def join_route(prefix, route):
    """Concatenate a prefix and a route without producing a double slash."""
    if not prefix:
        return route
    if route is None:
        return prefix
    combined = prefix.rstrip('/') + '/' + route.lstrip('/')
    if not combined.startswith('/'):
        combined = '/' + combined
    return combined


def collect_prefix_definitions(tree):
    """Blueprints and routers created in this file, with the prefix they declare."""
    definitions = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        factory = _dotted_name(node.value.func)
        kind = ROUTER_FACTORY_KINDS.get(factory.split('.')[-1]) if factory else None
        if not kind:
            continue
        prefix = _keyword_string(node.value, PREFIX_KEYWORDS)
        for target in node.targets:
            if isinstance(target, ast.Name):
                definitions[target.id] = {'kind': kind, 'prefix': prefix}
    return definitions


def collect_prefix_registrations(tree):
    """Blueprints and routers mounted in this file, with the prefix used to mount them."""
    registrations = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        call_name = _dotted_name(node.func)
        kind = REGISTER_CALL_KINDS.get(call_name.split('.')[-1]) if call_name else None
        if not kind:
            continue
        target = _dotted_name(node.args[0])
        prefix = _keyword_string(node, PREFIX_KEYWORDS)
        if not target or not prefix:
            continue
        registrations[target] = {'kind': kind, 'prefix': prefix}
    return registrations


def _resolve_prefix(definition, registration):
    """Flask lets register_blueprint override the Blueprint prefix, FastAPI composes them."""
    definition_prefix = (definition or {}).get('prefix')
    registration_prefix = (registration or {}).get('prefix')
    kind = (registration or {}).get('kind') or (definition or {}).get('kind')
    if kind == 'blueprint':
        return registration_prefix or definition_prefix
    return join_route(registration_prefix, definition_prefix)


def build_prefix_index(definitions, registrations, external_registrations=None):
    """Map an object name to the prefix every route decorated with it must carry."""
    external_registrations = external_registrations or {}
    index = {}
    for name in set(definitions) | set(registrations) | set(external_registrations):
        registration = registrations.get(name) or external_registrations.get(name)
        prefix = _resolve_prefix(definitions.get(name), registration)
        if prefix:
            index[name] = prefix
    return index


def _collect_import_bindings(tree):
    module_aliases = {}
    name_sources = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            if not node.module and not level:
                continue
            for alias in node.names:
                name_sources[alias.asname or alias.name] = (node.module or '', alias.name, level)
    return module_aliases, name_sources


def _module_file(module, search_roots):
    parts = module.split('.')
    for root in search_roots:
        base = os.path.join(root, *parts)
        for candidate in (base + '.py', os.path.join(base, '__init__.py')):
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
    return None


def _relative_import_root(package_dir, level):
    """Directory a relative import counts from: level 1 is the file's own package."""
    root = Path(package_dir)
    for _ in range(level - 1):
        root = root.parent
    return str(root)


def _import_source(target, module_aliases, name_sources):
    """(module, relative level, name in that module) the target was imported from."""
    if '.' in target:
        module_part, name = target.rsplit('.', 1)
        if module_part in module_aliases:
            return module_aliases[module_part], 0, name
        if module_part in name_sources:
            module, original, level = name_sources[module_part]
            return (f"{module}.{original}" if module else original), level, name
        return None
    if target in name_sources:
        module, name, level = name_sources[target]
        return module, level, name
    return None


def _resolve_registration_target(target, module_aliases, name_sources, search_roots, package_dir=None):
    """Locate the file that defines a blueprint/router mounted from another file."""
    source = _import_source(target, module_aliases, name_sources)
    if not source:
        return None
    module, level, name = source
    if not module:
        return None
    if level:
        # ``from .routes import router`` is the standard FastAPI layout, and
        # dropping it lost every prefix declared next to the app.
        if not package_dir:
            return None
        roots = [_relative_import_root(package_dir, level)]
    else:
        roots = search_roots
    origin = _module_file(module, roots)
    if not origin:
        return None
    return origin, name


def collect_external_prefixes(file_paths, repo_root=None):
    """Prefixes one file attaches to blueprints/routers that another file defines.

    Returns {defining file path: {object name: registration}} so the file that
    owns the routes can pick up the prefix declared at the registration site.
    """
    external = {}
    for file_path in file_paths:
        path = Path(file_path)
        try:
            source = path.read_text(encoding='utf-8')
        except Exception:
            continue
        # Cheap filter first: most files in a repo mount nothing.
        if not any(call in source for call in REGISTER_CALL_KINDS):
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except Exception:
            continue
        registrations = collect_prefix_registrations(tree)
        if not registrations:
            continue
        definitions = collect_prefix_definitions(tree)
        module_aliases, name_sources = _collect_import_bindings(tree)
        search_roots = [str(path.parent)]
        if repo_root:
            search_roots.insert(0, str(repo_root))
        for target, registration in registrations.items():
            if target in definitions:
                continue
            resolved = _resolve_registration_target(
                target, module_aliases, name_sources, search_roots, str(path.parent)
            )
            if not resolved:
                continue
            origin, name = resolved
            external.setdefault(origin, {})[name] = registration
    return external


def _route_with_prefix(decorator_node, prefix_index):
    route = extract_route_from_decorator(decorator_node)
    if route is None or not prefix_index:
        return route
    func = decorator_node.func if isinstance(decorator_node, ast.Call) else decorator_node
    owner = _dotted_name(func.value) if isinstance(func, ast.Attribute) else None
    if not owner:
        return route
    return join_route(prefix_index.get(owner), route)


def find_api_endpoints(file_path, external_prefixes=None):
    try:
        source = file_path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(file_path))
    except Exception:
        return []
    prefix_index = build_prefix_index(
        collect_prefix_definitions(tree),
        collect_prefix_registrations(tree),
        external_prefixes,
    )
    endpoints = []
    class_endpoints = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not isinstance(getattr(node, 'parent', None),
                                                                                        ast.ClassDef):
            for dec in node.decorator_list:
                if has_api_decorator(dec):
                    route = _route_with_prefix(dec, prefix_index)
                    http_method = extract_http_method(dec)
                    endpoints.append({
                        "type": "function",
                        "name": node.name,
                        "start_line": node.lineno,
                        "end_line": getattr(node, 'end_lineno', None),
                        "route": route,
                        "method": http_method,
                        "file_path": str(file_path)
                    })
        if isinstance(node, ast.ClassDef):
            class_has_decorator = any(has_api_decorator(dec) for dec in node.decorator_list)
            class_route = None
            for dec in node.decorator_list:
                if has_api_decorator(dec):
                    class_route = _route_with_prefix(dec, prefix_index)
                    break
            if class_has_decorator:
                class_endpoint = {
                    "type": "class",
                    "name": node.name,
                    "start_line": node.lineno,
                    "end_line": getattr(node, 'end_lineno', None),
                    "route": class_route,
                    "method": None,
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
                                method_route = _route_with_prefix(dec, prefix_index)
                                if method_route:
                                    break
                    if method_has_decorator or class_has_decorator:
                        http_method = None
                        for dec in body_item.decorator_list:
                            http_method = extract_http_method(dec)
                            if http_method:
                                break
                        method_entry = {
                            "type": "method",
                            "name": body_item.name,
                            "start_line": body_item.lineno,
                            "end_line": getattr(body_item, 'end_lineno', None),
                            "route": method_route if method_route else class_route,
                            "method": http_method,
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

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

# Django and DRF class views, matched on the base class name.
API_BASE_CLASS_SUFFIXES = ('viewset', 'apiview', 'view')

# Constructors that create a route group, and the calls that mount one.
ROUTER_FACTORY_KINDS = {'Blueprint': 'blueprint', 'APIRouter': 'router'}
REGISTER_CALL_KINDS = {'register_blueprint': 'blueprint', 'include_router': 'router'}
PREFIX_KEYWORDS = ('url_prefix', 'prefix')


def _decorator_name(decorator_node):
    """The name a decorator is written with, call or not."""
    func = decorator_node.func if isinstance(decorator_node, ast.Call) else decorator_node
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def has_api_decorator(decorator_node):
    """@app.route, @router.get and @api_view([...]) all name an API here.

    The bare-name call form is what DRF uses, and missing it left every
    ``@api_view`` handler out of the extraction.
    """
    name = _decorator_name(decorator_node)
    if not name:
        return False
    lower = name.lower()
    return lower in API_DECORATOR_NAMES or lower.endswith('_view')


def _base_class_name(base):
    if isinstance(base, ast.Attribute):
        return base.attr
    if isinstance(base, ast.Name):
        return base.id
    return None


def has_api_base_class(class_node):
    """A DRF or Django class view, recognised by what it inherits from.

    The file selector and the extractor have to agree on this, otherwise a
    views.py is scanned and then reports nothing.
    """
    for base in class_node.bases:
        name = _base_class_name(base)
        if name and name.lower().endswith(API_BASE_CLASS_SUFFIXES):
            return True
    return False


def _http_verbs(node):
    """The HTTP verbs a list/tuple of string literals names."""
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return []
    verbs = []
    for element in node.elts:
        value = _string_literal(element)
        if value and value.lower() in HTTP_METHOD_DECORATOR_NAMES and value.upper() not in verbs:
            verbs.append(value.upper())
    return verbs


def extract_http_methods(decorator_node):
    """Every HTTP verb one decorator declares.

    ``@app.route('/x', methods=['GET', 'POST'])`` is two endpoints, and Flask
    defaults a bare ``@app.route`` to GET. An empty list means the decorator
    names no verb at all, and the endpoint carries no method.
    """
    name = _decorator_name(decorator_node)
    if not name:
        return []
    if isinstance(decorator_node, ast.Call):
        for keyword in decorator_node.keywords:
            if keyword.arg == 'methods':
                verbs = _http_verbs(keyword.value)
                if verbs:
                    return verbs
    lower = name.lower()
    if lower in HTTP_METHOD_DECORATOR_NAMES:
        return [lower.upper()]
    if lower == 'route':
        return ['GET']
    return []


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


def _endpoint(kind, node, file_path, route, method):
    return {
        "type": kind,
        "name": node.name,
        "start_line": node.lineno,
        "end_line": getattr(node, 'end_lineno', None),
        "route": route,
        "method": method,
        "file_path": str(file_path),
    }


def _decorated_endpoints(kind, node, file_path, prefix_index, fallback_route=None):
    """One entry per verb the decorators declare, one entry when they name none."""
    entries = []
    for dec in node.decorator_list:
        if not has_api_decorator(dec):
            continue
        route = _route_with_prefix(dec, prefix_index) or fallback_route
        methods = extract_http_methods(dec)
        if not methods:
            entries.append(_endpoint(kind, node, file_path, route, None))
            continue
        for method in methods:
            entries.append(_endpoint(kind, node, file_path, route, method))
    return entries


def _class_endpoints(node, file_path, prefix_index):
    """A class view and the methods of it that actually serve HTTP.

    Every method of a decorated class used to be documented as an endpoint,
    ``__init__`` and helpers included.
    """
    class_route = None
    decorated = False
    for dec in node.decorator_list:
        if not has_api_decorator(dec):
            continue
        decorated = True
        if class_route is None:
            class_route = _route_with_prefix(dec, prefix_index)
    if not decorated and not has_api_base_class(node):
        return []
    class_entry = _endpoint("class", node, file_path, class_route, None)
    class_entry["methods"] = []
    for body_item in node.body:
        if not isinstance(body_item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        entries = _decorated_endpoints(
            "method", body_item, file_path, prefix_index, class_route
        )
        if not entries and body_item.name.lower() in HTTP_METHOD_DECORATOR_NAMES:
            entries = [
                _endpoint("method", body_item, file_path, class_route, body_item.name.upper())
            ]
        class_entry["methods"].extend(entries)
    return [class_entry]


def find_api_endpoints(file_path, external_prefixes=None, tree=None):
    """The endpoints one file declares.

    Accepts a path or a string, and an already parsed tree so a file is read
    once per run. The tree is parented here: walking an unparented one emitted
    every class method twice, once as a method and once as a free function.
    """
    path = Path(file_path)
    if tree is None:
        try:
            source = path.read_text(encoding='utf-8', errors='replace')
            tree = ast.parse(source, filename=str(path))
        except Exception:
            return []
    set_parents(tree)
    prefix_index = build_prefix_index(
        collect_prefix_definitions(tree),
        collect_prefix_registrations(tree),
        external_prefixes,
    )
    endpoints = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if isinstance(getattr(node, 'parent', None), ast.ClassDef):
                continue
            endpoints.extend(_decorated_endpoints("function", node, path, prefix_index))
        elif isinstance(node, ast.ClassDef):
            endpoints.extend(_class_endpoints(node, path, prefix_index))
    return endpoints


def set_parents(tree):
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node

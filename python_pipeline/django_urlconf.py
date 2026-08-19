"""Django URLconf extraction.

A conventional Django project declares its routes in urls.py, which carries no
decorator at all, so the decorator extractor reported zero endpoints for it and
the run fell through to the slow generic path. This walks urlpatterns instead:
path()/re_path()/url() entries, include() chains followed across files, and the
routes a DRF router generates for a registered viewset. Modules are resolved on
the filesystem only, the scanned repo is never imported.
"""

from pathlib import Path
import ast
import os
import re

from python_pipeline.identify_api_functions import (
    HTTP_METHOD_DECORATOR_NAMES,
    _base_class_name,
    _collect_import_bindings,
    _dotted_name,
    _http_verbs,
    _import_source,
    _module_file,
    _relative_import_root,
    _resolve_registration_target,
    _string_literal,
    join_route,
)

URLCONF_CALLS = {'path', 're_path', 'url'}
REGEX_CALLS = {'re_path', 'url'}

# What a DRF router publishes for a viewset: the collection actions live on the
# registered prefix, the rest on the detail route.
STANDARD_VIEWSET_ACTIONS = (
    ('list', 'GET', False),
    ('create', 'POST', False),
    ('retrieve', 'GET', True),
    ('update', 'PUT', True),
    ('partial_update', 'PATCH', True),
    ('destroy', 'DELETE', True),
)
ALL_VIEWSET_ACTIONS = {action for action, _, _ in STANDARD_VIEWSET_ACTIONS}
READ_ONLY_VIEWSET_ACTIONS = {'list', 'retrieve'}

_REGEX_GROUP = re.compile(r"\(\?P<([^>]+)>[^)]*\)")
_REGEX_METACHARACTERS = set("()[]\\*+?|{}^$")


class _Urlconf:
    """One parsed file, with what is needed to follow the names it references."""

    def __init__(self, path, tree):
        self.path = path
        self.tree = tree
        self.module_aliases, self.name_sources = _collect_import_bindings(tree)
        self.routers = _collect_router_registrations(tree)


class _Project:
    """The scanned repo, parsed lazily and at most once per file."""

    def __init__(self, repo_root=None):
        self.repo_root = os.path.abspath(str(repo_root)) if repo_root else None
        self._files = {}

    def load(self, file_path, source=None):
        key = os.path.abspath(str(file_path))
        if key not in self._files:
            self._files[key] = _parse_urlconf(key, source)
        return self._files[key]

    def search_roots(self, file_path):
        """Where an absolute module import is looked up: nearest package first."""
        directory = Path(file_path).parent
        roots = [str(directory)]
        if not self.repo_root:
            return roots
        for parent in directory.parents:
            if not str(parent).startswith(self.repo_root):
                break
            roots.append(str(parent))
        if self.repo_root not in roots:
            roots.append(self.repo_root)
        return roots


def _parse_urlconf(file_path, source=None):
    try:
        if source is None:
            source = Path(file_path).read_text(encoding='utf-8', errors='replace')
        return _Urlconf(file_path, ast.parse(source, filename=str(file_path)))
    except Exception:
        return None


def _collect_router_registrations(tree):
    """{router variable: [(prefix, viewset reference)]} declared in this file."""
    routers = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != 'register' or len(node.args) < 2:
            continue
        owner = _dotted_name(node.func.value)
        prefix = _string_literal(node.args[0])
        if not owner or prefix is None:
            continue
        routers.setdefault(owner, []).append((prefix, node.args[1]))
    return routers


def _absolute(route):
    route = route or ''
    return route if route.startswith('/') else '/' + route


def _module_level_statements(node):
    """The statements that run on import, def and class bodies excluded.

    An ``if settings.DEBUG:`` block still counts, a helper function's body does
    not: its urlpatterns is that function's local, not the module's URLconf.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(child, ast.stmt):
            yield child
            yield from _module_level_statements(child)


def _urlpatterns_target(targets):
    return any(isinstance(target, ast.Name) and target.id == 'urlpatterns'
               for target in targets)


def _urlpattern_values(tree):
    """Every value assigned to urlpatterns at module level, ``+=`` included.

    Only the module's own urlpatterns is a URLconf. One built inside a test or
    a helper function invents endpoints nothing serves.
    """
    values = []
    for node in _module_level_statements(tree):
        if isinstance(node, ast.Assign):
            if _urlpatterns_target(node.targets):
                values.append(node.value)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            if node.value is not None and _urlpatterns_target([node.target]):
                values.append(node.value)
    return values


def _pattern_entries(value):
    """The entries of a urlpatterns value, following ``[...] + router.urls``."""
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        return _pattern_entries(value.left) + _pattern_entries(value.right)
    if isinstance(value, (ast.List, ast.Tuple)):
        return list(value.elts)
    return [value]


def _call_name(node):
    if not isinstance(node, ast.Call):
        return None
    name = _dotted_name(node.func)
    return name.split('.')[-1] if name else None


def _router_reference(node):
    """The router variable behind a ``router.urls`` reference."""
    if isinstance(node, ast.Attribute) and node.attr == 'urls':
        return _dotted_name(node.value)
    return None


def _route_from_regex(pattern):
    """A route read off a re_path regex, or None when it cannot be read.

    ``^users/(?P<pk>[0-9]+)/$`` is ``users/<pk>/``. Anything still carrying
    regex syntax after the named groups are rewritten is skipped rather than
    guessed at.
    """
    route = _REGEX_GROUP.sub(r"<\1>", pattern.strip())
    route = route.lstrip('^').rstrip('$')
    if any(character in _REGEX_METACHARACTERS for character in route):
        return None
    return route


def _pattern_route(node):
    route = _string_literal(node.args[0])
    if route is None:
        return None
    if _call_name(node) in REGEX_CALLS:
        return _route_from_regex(route)
    return route


def _include_target_file(project, urlconf, node):
    """The urls module an ``include(...)`` argument names, as a file path."""
    roots = project.search_roots(urlconf.path)
    module = _string_literal(node)
    if module:
        return _module_file(module, roots)
    target = _dotted_name(node)
    if not target:
        return None
    source = _import_source(target, urlconf.module_aliases, urlconf.name_sources)
    if not source:
        return None
    module, level, name = source
    if level:
        roots = [_relative_import_root(str(Path(urlconf.path).parent), level)]
    candidate = f"{module}.{name}" if module else name
    return _module_file(candidate, roots)


def _resolve_view(project, urlconf, node):
    """(file, symbol) a view reference points at, on the filesystem only."""
    target = _dotted_name(node)
    if not target:
        return None
    resolved = _resolve_registration_target(
        target,
        urlconf.module_aliases,
        urlconf.name_sources,
        project.search_roots(urlconf.path),
        str(Path(urlconf.path).parent),
    )
    if resolved:
        return resolved
    if '.' not in target:
        # A view defined in the URLconf itself.
        return urlconf.path, target
    return None


def _definition(project, file_path, name):
    """The top level def or class a view name resolves to."""
    loaded = project.load(file_path)
    if not loaded or not name:
        return None
    for node in loaded.tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return node
    return None


def _endpoint(file_path, node, name, route, method):
    return {
        "type": "function",
        "name": name,
        "start_line": node.lineno,
        "end_line": getattr(node, 'end_lineno', None),
        "route": _absolute(route),
        "method": method,
        "file_path": os.path.abspath(str(file_path)),
    }


def _decorator_verbs(node):
    """The verbs a ``@api_view(['GET', 'POST'])`` style decorator lists."""
    verbs = []
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        listed = list(_http_verbs(decorator.args[0])) if decorator.args else []
        for keyword in decorator.keywords:
            if keyword.arg == 'methods':
                listed.extend(_http_verbs(keyword.value))
        for verb in listed:
            if verb not in verbs:
                verbs.append(verb)
    return verbs


def _function_view_endpoints(file_path, node, route):
    """A function view, once per verb it declares. Django defaults it to GET."""
    verbs = _decorator_verbs(node) or ['GET']
    return [_endpoint(file_path, node, node.name, route, verb) for verb in verbs]


def _verb_methods(class_node):
    """The HTTP verb methods a class view defines, in source order."""
    methods = []
    for item in class_node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if item.name.lower() in HTTP_METHOD_DECORATOR_NAMES:
            methods.append((item.name.upper(), item))
    return methods


def _class_view_endpoints(file_path, node, route):
    """A class view, once per HTTP method it defines. It defines none: skip it."""
    return [
        _endpoint(file_path, method_node, f"{node.name}.{method_node.name}", route, verb)
        for verb, method_node in _verb_methods(node)
    ]


def _inherited_viewset_actions(class_node):
    """What a viewset serves through its base class alone.

    A ModelViewSet inherits all six, a read-only one only reads, and a bare
    ViewSet serves nothing until it defines something.
    """
    for base in class_node.bases:
        name = (_base_class_name(base) or '').lower()
        if not name.endswith('viewset'):
            continue
        if 'readonly' in name:
            return READ_ONLY_VIEWSET_ACTIONS
        if name.endswith('modelviewset'):
            return ALL_VIEWSET_ACTIONS
    return set()


def _viewset_endpoints(file_path, class_node, base_route):
    """The routes a router publishes for one registered viewset."""
    defined = {
        item.name: item
        for item in class_node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    # Overriding one action does not unregister the other five, so what the
    # class defines is added to what its base class already serves.
    actions = _inherited_viewset_actions(class_node) | {
        action for action in ALL_VIEWSET_ACTIONS if action in defined
    }
    collection_route = _absolute(base_route.rstrip('/') + '/')
    detail_route = _absolute(base_route.rstrip('/') + '/<pk>/')
    endpoints = []
    for action, verb, on_detail in STANDARD_VIEWSET_ACTIONS:
        if action not in actions:
            continue
        target = defined.get(action) or class_node
        endpoints.append(
            _endpoint(
                file_path,
                target,
                f"{class_node.name}.{action}",
                detail_route if on_detail else collection_route,
                verb,
            )
        )
    return endpoints


def _expand_router(project, urlconf, prefix, router_name, endpoints):
    for registered_prefix, viewset_node in urlconf.routers.get(router_name, []):
        resolved = _resolve_view(project, urlconf, viewset_node)
        if not resolved:
            continue
        file_path, name = resolved
        definition = _definition(project, file_path, name)
        if not isinstance(definition, ast.ClassDef):
            continue
        endpoints.extend(
            _viewset_endpoints(file_path, definition, join_route(prefix, registered_prefix))
        )


def _as_view_target(node):
    """The class behind a ``MyView.as_view()`` reference."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == 'as_view':
            return node.func.value
    return None


def _handle_view(project, urlconf, route, node, endpoints):
    resolved = _resolve_view(project, urlconf, _as_view_target(node) or node)
    if not resolved:
        return
    file_path, name = resolved
    definition = _definition(project, file_path, name)
    if isinstance(definition, ast.ClassDef):
        endpoints.extend(_class_view_endpoints(file_path, definition, route))
    elif isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
        endpoints.extend(_function_view_endpoints(file_path, definition, route))


def _handle_include(project, urlconf, prefix, node, endpoints, visited):
    if not node.args:
        return
    target = node.args[0]
    router_name = _router_reference(target)
    if router_name:
        _expand_router(project, urlconf, prefix, router_name, endpoints)
        return
    origin = _include_target_file(project, urlconf, target)
    included = project.load(origin) if origin else None
    if not included or included.path in visited:
        return
    _walk_urlconf(project, included, prefix, endpoints, visited | {included.path})


def _handle_pattern(project, urlconf, prefix, node, endpoints, visited):
    router_name = _router_reference(node)
    if router_name:
        _expand_router(project, urlconf, prefix, router_name, endpoints)
        return
    if _call_name(node) not in URLCONF_CALLS or len(node.args) < 2:
        return
    route = _pattern_route(node)
    if route is None:
        return
    joined = join_route(prefix, route)
    target = node.args[1]
    if _call_name(target) == 'include':
        _handle_include(project, urlconf, joined, target, endpoints, visited)
        return
    _handle_view(project, urlconf, joined, target, endpoints)


def _walk_urlconf(project, urlconf, prefix, endpoints, visited):
    for value in _urlpattern_values(urlconf.tree):
        for entry in _pattern_entries(value):
            _handle_pattern(project, urlconf, prefix, entry, endpoints, visited)


def _included_files(project, urlconf):
    files = set()
    for node in ast.walk(urlconf.tree):
        if _call_name(node) != 'include' or not node.args:
            continue
        origin = _include_target_file(project, urlconf, node.args[0])
        if origin:
            files.add(origin)
    return files


def _load_if_urlconf(project, file_path):
    path = os.path.abspath(str(file_path))
    try:
        source = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    if 'urlpatterns' not in source:
        return None
    return project.load(path, source)


def _dedupe(endpoints):
    seen = set()
    unique = []
    for endpoint in endpoints:
        key = (
            endpoint['file_path'],
            endpoint['start_line'],
            endpoint['end_line'],
            endpoint['route'],
            endpoint['method'],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(endpoint)
    return unique


def collect_django_endpoints(file_paths, repo_root=None):
    """Every endpoint the Django URLconfs of a repo declare.

    Only a URLconf nothing else includes is walked, so an app's urls.py is
    documented under the prefix the project mounts it at instead of twice.
    """
    project = _Project(repo_root)
    urlconfs = []
    for file_path in file_paths:
        loaded = _load_if_urlconf(project, file_path)
        if loaded:
            urlconfs.append(loaded)
    included = set()
    for urlconf in urlconfs:
        included.update(_included_files(project, urlconf))
    # A layout where every URLconf is included by another one would otherwise
    # leave nothing to walk at all.
    roots = [urlconf for urlconf in urlconfs if urlconf.path not in included] or urlconfs
    endpoints = []
    for urlconf in roots:
        _walk_urlconf(project, urlconf, '', endpoints, {urlconf.path})
    return _dedupe(endpoints)

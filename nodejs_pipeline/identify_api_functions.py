from pathlib import Path
import re
from tree_sitter import Language, Parser
import tree_sitter_javascript
import tree_sitter_typescript
from nodejs_pipeline.constants import (
    TYPESCRIPT_FILE_EXTENSIONS,
    TSX_FILE_EXTENSIONS,
)


API_METHODS = {"get", "post", "put", "delete", "patch", "options", "head", "all"}
ROUTE_OBJECT_KEYWORDS = {"app", "router", "route", "api", "controller", "server"}
ROUTE_OBJECT_SUFFIXES = ("router", "routes", "route", "app", "server", "controller", "api")
# apiClient.get(url) and userService.get(id) read as route objects by prefix or
# keyword but are HTTP clients and business services, never route registrations.
ROUTE_OBJECT_EXCLUDED_SUFFIXES = ("client", "service")
FALLBACK_ENDPOINT_PATTERN = re.compile(
    r'(?P<object>[A-Za-z_$][\w$]*)\s*\.\s*(?P<method>GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD|ALL)\s*\(\s*(?P<route>["\'].*?["\'])?',
    re.IGNORECASE | re.DOTALL
)

# express mount detection: const router = express.Router() / Router()
ROUTER_FACTORY_PATTERN = re.compile(
    r'\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=;]+)?=\s*(?:express\s*\.\s*)?Router\s*\('
)
# express mount detection: app.use('/api/v1', router)
USE_MOUNT_PATTERN = re.compile(
    r'\.\s*use\s*\(\s*(?P<quote>[\'"`])(?P<path>[^\'"`]*)(?P=quote)\s*,\s*(?P<ident>[A-Za-z_$][\w$]*)\s*[,)]'
)
# express mount detection: app.use('/api/v1', require('./routes/users'))
USE_INLINE_REQUIRE_MOUNT_PATTERN = re.compile(
    r'\.\s*use\s*\(\s*(?P<quote>[\'"`])(?P<path>[^\'"`]*)(?P=quote)\s*,\s*require\s*\(\s*'
    r'(?P<module_quote>[\'"`])(?P<module>[^\'"`]+)(?P=module_quote)\s*\)'
)
REQUIRE_ASSIGN_PATTERN = re.compile(
    r'\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=;]+)?=\s*require\s*\(\s*'
    r'(?P<quote>[\'"`])(?P<module>[^\'"`]+)(?P=quote)\s*\)'
)
IMPORT_ASSIGN_PATTERN = re.compile(
    r'\bimport\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?:,\s*\{[^}]*\}\s*)?from\s*'
    r'(?P<quote>[\'"`])(?P<module>[^\'"`]+)(?P=quote)'
)

JS_LANGUAGE = Language(tree_sitter_javascript.language())
TS_LANGUAGE = Language(tree_sitter_typescript.language_typescript())
TSX_LANGUAGE = Language(tree_sitter_typescript.language_tsx())


def _extract_endpoints_with_regex(source: str, file_path: Path):
    """Last-resort endpoint detector when the grammar cannot parse the file."""
    endpoints = []
    for match in FALLBACK_ENDPOINT_PATTERN.finditer(source):
        method = match.group('method').upper()
        route_literal = match.group('route')
        route = None
        if route_literal and len(route_literal) >= 2:
            route = route_literal[1:-1]
        start = match.start()
        end = match.end()
        start_line = source.count('\n', 0, start) + 1
        end_line = source.count('\n', 0, end) + 1
        obj = match.group('object') or ""
        if not _looks_like_route_object(obj):
            continue
        endpoints.append({
            "type": "function",
            "method": method,
            "route": route,
            "route_object": obj,
            "start_line": start_line,
            "end_line": end_line,
            "file_path": str(file_path)
        })
    return endpoints


def find_api_endpoints_js(file_path: Path):
    try:
        source = file_path.read_text(encoding='utf-8')
    except Exception:
        return []

    endpoints = _find_api_endpoints_tree_sitter(file_path, source)

    return _apply_same_file_mounts(endpoints, source)


def _walk_tree(root):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        # named_children keeps noise nodes out of traversal
        stack.extend(reversed(getattr(node, "named_children", [])))


def _node_text(node, source_bytes):
    return source_bytes[node.start_byte:node.end_byte].decode('utf-8')


def _clean_literal(value: str):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _clean_template_literal(value: str):
    if "${" in value:
        return None
    if value.startswith("`") and value.endswith("`"):
        return value[1:-1]
    return value

def _find_matching_brace(source: str, start_idx: int):
    """Find the index of the matching closing brace for source[start_idx] == '{'."""
    depth = 0
    for idx in range(start_idx, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return idx
    return -1

def _clean_path_literal(value: str | None):
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"', "`"}:
        return value[1:-1]
    return value

def _collect_decorators(node):
    """Return decorator nodes attached to a class/method or its decorated wrapper."""
    decorators = []
    def _gather(target):
        if not target:
            return
        for child in getattr(target, "children", []):
            if child.type == "decorator":
                decorators.append(child)
    _gather(node)
    # An exported class keeps its decorators on the export_statement wrapper,
    # not on the class_declaration itself.
    parent = getattr(node, "parent", None)
    if parent and getattr(parent, "type", "") == "export_statement":
        _gather(parent)
    return decorators

def _parse_decorator(decorator_node, source_bytes):
    """
    Extract decorator identifier and first string/template argument if present.
    Returns (name, arg_value).
    """
    expr = decorator_node.child_by_field_name("expression") or (decorator_node.children[0] if decorator_node.children else None)
    if not expr:
        return None, None
    if expr.type == "call_expression":
        func_node = expr.child_by_field_name("function")
        name = _node_text(func_node, source_bytes) if func_node else None
        args_node = expr.child_by_field_name("arguments")
        arg_val = None
        if args_node:
            for arg in args_node.named_children:
                if arg.type == "string":
                    arg_val = _clean_literal(_node_text(arg, source_bytes))
                    break
                if arg.type == "template_string":
                    arg_val = _clean_template_literal(_node_text(arg, source_bytes))
                    break
        return name, arg_val
    if expr.type in {"identifier", "property_identifier"}:
        return _node_text(expr, source_bytes), None
    return None, None

def _combine_paths(prefix, path):
    """Combine controller prefix and handler path into a single NestJS route."""
    prefix_part = prefix or "/"
    path_part = path if path is not None else "/"
    if not prefix_part.startswith("/"):
        prefix_part = "/" + prefix_part
    if not path_part.startswith("/"):
        path_part = "/" + path_part
    combined = re.sub(r"//+", "/", prefix_part.rstrip("/") + path_part)
    # NestJS serves @Get() on @Controller('cats') at /cats, not /cats/.
    if len(combined) > 1:
        combined = combined.rstrip("/")
    return combined if combined.startswith("/") else "/" + combined


def join_mount_prefix(prefix, route):
    """Prepend a router mount prefix to a route without producing double slashes."""
    if not prefix or route is None:
        return route
    combined = _combine_paths(prefix, route)
    if len(combined) > 1 and combined.endswith("/"):
        combined = combined.rstrip("/")
    return combined


def _record_mount(mounts: dict, key: str, path: str) -> None:
    """One router may be mounted under several prefixes, so every key holds a list."""
    bucket = mounts.setdefault(key, [])
    if path not in bucket:
        bucket.append(path)


def find_mount_prefixes(source: str):
    """Map identifier -> mount prefixes for every X.use('<prefix>', identifier) call in the source."""
    mounts = {}
    for match in USE_MOUNT_PATTERN.finditer(source):
        path = match.group("path")
        if not path.startswith("/"):
            continue
        _record_mount(mounts, match.group("ident"), path)
    return mounts


def find_inline_require_mounts(source: str):
    """
    Map module string -> mount prefixes for every X.use('<prefix>', require('<module>'))
    call in the source. The router is required inline, so there is no identifier
    to look up in the import map.
    """
    mounts = {}
    for match in USE_INLINE_REQUIRE_MOUNT_PATTERN.finditer(source):
        path = match.group("path")
        if not path.startswith("/"):
            continue
        _record_mount(mounts, match.group("module"), path)
    return mounts


def find_module_imports(source: str):
    """Map identifier -> module string for require() and default import assignments."""
    imports = {}
    for pattern in (REQUIRE_ASSIGN_PATTERN, IMPORT_ASSIGN_PATTERN):
        for match in pattern.finditer(source):
            imports.setdefault(match.group("name"), match.group("module"))
    return imports


def _find_local_router_names(source: str):
    return {match.group("name") for match in ROUTER_FACTORY_PATTERN.finditer(source)}


def _apply_same_file_mounts(endpoints, source: str):
    """
    Prefix routes registered on a router that is both created and mounted in
    this file. A router mounted under several prefixes yields one endpoint per
    mount, since each one is a real, separately reachable path.
    """
    if not endpoints:
        return endpoints
    local_routers = _find_local_router_names(source)
    if not local_routers:
        return endpoints
    mounts = find_mount_prefixes(source)
    mounted_endpoints = []
    for endpoint in endpoints:
        route_object = endpoint.get("route_object")
        prefixes = mounts.get(route_object) if route_object in local_routers else None
        if not prefixes:
            mounted_endpoints.append(endpoint)
            continue
        for prefix in prefixes:
            mounted = dict(endpoint)
            mounted["route"] = join_mount_prefix(prefix, endpoint.get("route"))
            mounted_endpoints.append(mounted)
    return mounted_endpoints


def _looks_like_route_object(name: str) -> bool:
    low = name.lower()
    if low.endswith(ROUTE_OBJECT_EXCLUDED_SUFFIXES):
        return False
    return low in ROUTE_OBJECT_KEYWORDS or any(low.endswith(suf) for suf in ROUTE_OBJECT_SUFFIXES) or low.startswith(("app", "api"))


def _select_language(file_path: Path):
    suffix = file_path.suffix.lower()
    if suffix in TSX_FILE_EXTENSIONS and TSX_LANGUAGE:
        return TSX_LANGUAGE
    if suffix in TYPESCRIPT_FILE_EXTENSIONS and TS_LANGUAGE:
        return TS_LANGUAGE
    return JS_LANGUAGE


def _find_api_endpoints_tree_sitter(file_path: Path, source: str):
    """
    Extract endpoints from a JS/TS/TSX file with the matching tree-sitter
    grammar. The regex extractors below only run when the grammar produced
    nothing usable, so modern syntax never costs the accuracy of a real parse.
    """
    parser = Parser(_select_language(file_path))
    try:
        tree = parser.parse(source.encode('utf-8'))
    except Exception:
        return _extract_endpoints_with_regex(source, file_path)

    endpoints = []
    source_bytes = source.encode('utf-8')
    # A name the file itself assigns express.Router() to is a router whatever it
    # is called, so it passes the name-shape filter on the declaration instead.
    local_routers = _find_local_router_names(source)
    seen = set()

    def _add(endpoint):
        key = (endpoint["method"], endpoint.get("route"), endpoint.get("start_line"))
        if key not in seen:
            endpoints.append(endpoint)
            seen.add(key)

    for endpoint in _extract_nest_endpoints(tree, source_bytes, file_path):
        _add(endpoint)

    for node in _walk_tree(tree.root_node):
        if node.type != "call_expression":
            continue
        endpoint = _extract_endpoint_from_call(node, source_bytes, file_path, local_routers)
        if endpoint:
            _add(endpoint)

    if not endpoints:
        for endpoint in _extract_nest_endpoints_regex(source, file_path):
            _add(endpoint)

    if not endpoints and tree.root_node.has_error:
        for endpoint in _extract_endpoints_with_regex(source, file_path):
            _add(endpoint)
    return endpoints


def _extract_endpoint_from_call(node, source_bytes, file_path: Path, local_routers=()):
    func_node = node.child_by_field_name("function")
    if not func_node or func_node.type != "member_expression":
        return None
    property_node = func_node.child_by_field_name("property")
    object_node = func_node.child_by_field_name("object")
    if not property_node or property_node.type not in {"property_identifier", "identifier"}:
        return None
    method_name = _node_text(property_node, source_bytes).strip().lower()
    if method_name not in API_METHODS:
        return None
    route_object_name = _node_text(object_node, source_bytes) if object_node else ""
    if route_object_name not in local_routers and not _looks_like_route_object(route_object_name):
        return None

    route = None
    arguments_node = node.child_by_field_name("arguments")
    if arguments_node:
        for child in arguments_node.named_children:
            if child.type == "string":
                route = _clean_literal(_node_text(child, source_bytes))
                break
            if child.type == "template_string":
                route = _clean_template_literal(_node_text(child, source_bytes))
                break

    return {
        "type": "function",
        "method": method_name.upper(),
        "route": route,
        "route_object": route_object_name,
        "start_line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
        "file_path": str(file_path),
    }

def _extract_nest_endpoints(tree, source_bytes, file_path: Path):
    """Extract NestJS controller + method decorator routes."""
    endpoints = []
    for node in _walk_tree(tree.root_node):
        if node.type != "class_declaration":
            continue
        controller_prefix = None
        for decorator in _collect_decorators(node):
            name, arg = _parse_decorator(decorator, source_bytes)
            if not name:
                continue
            if name.lower() == "controller":
                controller_prefix = arg or "/"
                break
        if controller_prefix is None:
            continue

        class_body = next((c for c in node.named_children if c.type == "class_body"), None)
        if class_body is None:
            continue

        # Method decorators are siblings of the method inside class_body, preceding it.
        pending_decorators = []
        for child in class_body.named_children:
            if child.type == "decorator":
                pending_decorators.append(child)
                continue
            if child.type not in {"method_definition", "public_field_definition"}:
                pending_decorators = []
                continue
            method_decorators = pending_decorators + _collect_decorators(child)
            pending_decorators = []
            method_http = None
            method_path = None
            for decorator in method_decorators:
                name, arg = _parse_decorator(decorator, source_bytes)
                if not name:
                    continue
                low = name.lower()
                if low in API_METHODS:
                    method_http = low.upper()
                    method_path = arg if arg is not None else "/"
                    break
            if not method_http:
                continue
            start_line = child.start_point[0] + 1 if hasattr(child, "start_point") else None
            end_line = child.end_point[0] + 1 if hasattr(child, "end_point") else None
            route = _combine_paths(controller_prefix, method_path)
            endpoints.append({
                "type": "function",
                "method": method_http,
                "route": route,
                "start_line": start_line,
                "end_line": end_line,
                "file_path": str(file_path)
            })
    return endpoints

def _extract_nest_endpoints_regex(source: str, file_path: Path):
    """
    Fallback extractor for NestJS controllers using regex when tree-sitter parsing misses decorators.
    This is intentionally permissive to avoid empty output on complex TS syntax.
    """
    endpoints = []
    controller_re = re.compile(r'@Controller\s*\(\s*(?P<arg>(`[^`]*`|"[^"]*"|\'[^\']*\'|[^)]*)?)\s*\)', re.MULTILINE)
    class_re = re.compile(r'class\s+[A-Za-z_]\w*\s*[^{]*\{', re.MULTILINE)
    method_re = re.compile(r'@(Get|Post|Put|Delete|Patch|Options|Head|All)\s*\(\s*(?P<arg>(`[^`]*`|"[^"]*"|\'[^\']*\'|[^)]*)?)\s*\)', re.IGNORECASE)

    for controller_match in controller_re.finditer(source):
        prefix_raw = controller_match.group("arg") or ""
        prefix = _clean_path_literal(prefix_raw) or "/"
        search_start = controller_match.end()
        class_match = class_re.search(source, search_start)
        if not class_match:
            continue
        brace_start = source.find("{", class_match.start())
        if brace_start == -1:
            continue
        brace_end = _find_matching_brace(source, brace_start)
        if brace_end == -1:
            continue
        body = source[brace_start:brace_end]
        base_line = source.count("\n", 0, brace_start) + 1

        for method_match in method_re.finditer(body):
            method_http = method_match.group(1).upper()
            path_raw = method_match.group("arg") or ""
            cleaned_path = _clean_path_literal(path_raw) or "/"
            route = _combine_paths(prefix, cleaned_path)
            start_line = base_line + body.count("\n", 0, method_match.start())
            end_line = start_line
            endpoints.append({
                "type": "function",
                "method": method_http,
                "route": route,
                "start_line": start_line,
                "end_line": end_line,
                "file_path": str(file_path)
            })
    return endpoints

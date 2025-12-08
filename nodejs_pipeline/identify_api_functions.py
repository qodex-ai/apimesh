from pathlib import Path
import esprima
import json
import re
from tree_sitter import Language, Parser
import tree_sitter_typescript
from nodejs_pipeline.constants import (
    TYPESCRIPT_FILE_EXTENSIONS,
    TSX_FILE_EXTENSIONS,
)


API_METHODS = {"get", "post", "put", "delete", "patch", "options", "head", "all"}
ROUTE_OBJECT_KEYWORDS = {"app", "router", "route", "api", "controller", "server"}
ROUTE_OBJECT_SUFFIXES = ("router", "routes", "route", "app", "server", "controller", "api")
OPTIONAL_CATCH_PATTERN = re.compile(r'catch\s*(\{)')
FALLBACK_ENDPOINT_PATTERN = re.compile(
    r'(?P<object>[A-Za-z_$][\w$]*)\s*\.\s*(?P<method>GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD|ALL)\s*\(\s*(?P<route>["\'].*?["\'])?',
    re.IGNORECASE | re.DOTALL
)

TS_LANGUAGE = Language(tree_sitter_typescript.language_typescript())
TSX_LANGUAGE = Language(tree_sitter_typescript.language_tsx())


def _parse_with_optional_catch_fallback(source, *, loc=True):
    """
    Attempt to parse JavaScript source. If the parser fails because of
    optional catch binding syntax (catch { ... }), rewrite those blocks
    to catch (__apimesh_err) { ... } and retry once.
    """
    try:
        return esprima.parseModule(source, loc=loc)
    except Exception as first_error:
        patched_source, replaced = OPTIONAL_CATCH_PATTERN.subn('catch (__apimesh_err) {', source)
        if not replaced:
            raise first_error
        try:
            return esprima.parseModule(patched_source, loc=loc)
        except Exception:
            raise first_error


def _extract_endpoints_with_regex(source: str, file_path: Path):
    """Fallback endpoint detector when esprima cannot parse the file."""
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
        low = obj.lower()
        if not (low in ROUTE_OBJECT_KEYWORDS or any(low.endswith(suf) for suf in ROUTE_OBJECT_SUFFIXES) or low.startswith(('app', 'api'))):
            continue
        endpoints.append({
            "type": "function",
            "method": method,
            "route": route,
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

    suffix = file_path.suffix.lower()
    if suffix in TYPESCRIPT_FILE_EXTENSIONS or suffix in TSX_FILE_EXTENSIONS:
        return _find_api_endpoints_ts(file_path, source)

    return _find_api_endpoints_js(file_path, source)


def _find_api_endpoints_js(file_path: Path, source: str):
    try:
        tree = _parse_with_optional_catch_fallback(source, loc=True)
    except Exception as e:
        return _extract_endpoints_with_regex(source, file_path)

    endpoints = []

    def extract_call_expression(node, parent_obj=None):
        """Extract API endpoints from CallExpressions like app.get('/users', handler)"""
        if node.type == "CallExpression":
            callee = node.callee

            # Handle app.get(...) or router.post(...)
            if callee.type == "MemberExpression" and callee.property.type == "Identifier":
                method_name = callee.property.name.lower()

                if method_name in API_METHODS and node.arguments:
                    # Check first argument (the route string)
                    first_arg = node.arguments[0]
                    if first_arg.type == "Literal" and isinstance(first_arg.value, str):
                        route = first_arg.value
                    else:
                        route = None

                    endpoints.append({
                        "type": "function",
                        "method": method_name.upper(),
                        "route": route,
                        "start_line": node.loc.start.line,
                        "end_line": node.loc.end.line,
                        "file_path": str(file_path)
                    })

        # Recurse into child nodes
        for child_name, child in node.__dict__.items():
            if isinstance(child, list):
                for c in child:
                    if hasattr(c, 'type'):
                        extract_call_expression(c, node)
            elif hasattr(child, 'type'):
                extract_call_expression(child, node)

    extract_call_expression(tree)

    return endpoints


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
    parent = getattr(node, "parent", None)
    if parent and getattr(parent, "type", "") == "decorated_definition":
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
    return combined if combined.startswith("/") else "/" + combined


def _looks_like_route_object(name: str) -> bool:
    low = name.lower()
    return low in ROUTE_OBJECT_KEYWORDS or any(low.endswith(suf) for suf in ROUTE_OBJECT_SUFFIXES) or low.startswith(("app", "api"))


def _select_ts_language(file_path: Path):
    suffix = file_path.suffix.lower()
    if suffix in TSX_FILE_EXTENSIONS and TSX_LANGUAGE:
        return TSX_LANGUAGE
    if suffix in TYPESCRIPT_FILE_EXTENSIONS and TS_LANGUAGE:
        return TS_LANGUAGE
    return None


def _find_api_endpoints_ts(file_path: Path, source: str):
    language = _select_ts_language(file_path)
    if not language:
        return _extract_endpoints_with_regex(source, file_path)

    parser = Parser(language)
    try:
        tree = parser.parse(source.encode('utf-8'))
    except Exception:
        return _extract_endpoints_with_regex(source, file_path)

    endpoints = []
    source_bytes = source.encode('utf-8')
    seen = set()
    for endpoint in _extract_nest_endpoints(tree, source_bytes, file_path):
        key = (endpoint["method"], endpoint.get("route"), endpoint.get("start_line"))
        if key not in seen:
            endpoints.append(endpoint)
            seen.add(key)

    for node in _walk_tree(tree.root_node):
        if node.type != "call_expression":
            continue
        endpoint = _extract_endpoint_from_ts_call(node, source_bytes, file_path)
        if endpoint:
            key = (endpoint["method"], endpoint.get("route"), endpoint.get("start_line"))
            if key not in seen:
                endpoints.append(endpoint)
                seen.add(key)

    if not endpoints:
        for endpoint in _extract_nest_endpoints_regex(source, file_path):
            key = (endpoint["method"], endpoint.get("route"), endpoint.get("start_line"))
            if key not in seen:
                endpoints.append(endpoint)
                seen.add(key)
    return endpoints


def _extract_endpoint_from_ts_call(node, source_bytes, file_path: Path):
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
    if not _looks_like_route_object(route_object_name):
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

        for child in node.named_children:
            if child.type not in {"method_definition", "public_field_definition"}:
                continue
            method_http = None
            method_path = None
            for decorator in _collect_decorators(child):
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


# Example usage
if __name__ == "__main__":
    test_file = Path("/Users/ankits/My-Favourite-Playlist/server.js")  # path to Node.js file
    results = find_api_endpoints_js(test_file)
    print(json.dumps(results, indent=2))

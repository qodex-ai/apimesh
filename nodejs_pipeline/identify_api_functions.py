from pathlib import Path
import esprima
import json
import re


API_METHODS = {"get", "post", "put", "delete", "patch", "options", "head"}
ROUTE_OBJECT_KEYWORDS = {"app", "router", "route", "api", "controller", "server"}
ROUTE_OBJECT_SUFFIXES = ("router", "routes", "route", "app", "server", "controller", "api")
OPTIONAL_CATCH_PATTERN = re.compile(r'catch\s*(\{)')
FALLBACK_ENDPOINT_PATTERN = re.compile(
    r'(?P<object>[A-Za-z_$][\w$]*)\s*\.\s*(?P<method>GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD)\s*\(\s*(?P<route>["\'].*?["\'])?',
    re.IGNORECASE | re.DOTALL
)


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
        tree = _parse_with_optional_catch_fallback(source, loc=True)
    except Exception as e:
        print(f"Error parsing {file_path}: {e}")
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


# Example usage
if __name__ == "__main__":
    test_file = Path("/Users/ankits/My-Favourite-Playlist/server.js")  # path to Node.js file
    results = find_api_endpoints_js(test_file)
    print(json.dumps(results, indent=2))

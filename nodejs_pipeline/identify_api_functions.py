from pathlib import Path
import esprima
import json


API_METHODS = {"get", "post", "put", "delete", "patch"}


def find_api_endpoints_js(file_path: Path):
    try:
        source = file_path.read_text(encoding='utf-8')
        tree = esprima.parseModule(source, loc=True)  # loc=True gives line numbers
    except Exception as e:
        print(f"Error parsing {file_path}: {e}")
        return []

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

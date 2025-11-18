from tree_sitter import Language, Parser, QueryCursor
import tree_sitter_javascript
import os
import json

# Load JavaScript grammar
JS_LANGUAGE = Language(tree_sitter_javascript.language())
parser = Parser(JS_LANGUAGE)

def parse_file(filename):
    with open(filename, 'r', encoding='utf-8') as f:
        code = f.read()
    tree = parser.parse(code.encode('utf-8'))
    return tree, code

def get_module_origin(module_name, base_directory):
    """
    Resolve JS import/require origin similar to Node.js module resolution.
    """
    # Relative import
    if module_name.startswith("."):
        path = os.path.normpath(os.path.join(base_directory, module_name))
        for ext in (".js", ".mjs", ".cjs", "/index.js"):
            candidate = path + ext
            if os.path.exists(candidate):
                return os.path.abspath(candidate)
        return None

    # Look in node_modules
    node_module_path = os.path.join(base_directory, "node_modules", module_name)
    if os.path.exists(node_module_path):
        return os.path.abspath(node_module_path)

    return "<node_builtin_or_external>"

def find_import_usages(tree, imported_names):
    """Find where imported identifiers are used."""
    query = JS_LANGUAGE.query("""
        (identifier) @ident
    """)
    cursor = QueryCursor(query)
    captures = cursor.captures(tree.root_node)

    usages = {name: [] for name in imported_names}
    for node in captures.get("ident", []):
        name = node.text.decode("utf-8")
        if name in imported_names:
            line = node.start_point[0] + 1
            if line not in usages[name]:
                usages[name].append(line)
    return usages

def get_elements(tree, code, base_directory):
    """
    Extract classes, functions, variables, function calls, imports.
    """
    query = JS_LANGUAGE.query("""
        (class_declaration
            name: (identifier) @class-name) @class

        (function_declaration
            name: (identifier) @func-name) @function

        (variable_declarator
            name: (identifier) @var-name) @variable

        (call_expression
            function: (identifier) @called-func) @func-call

        (call_expression
            function: (member_expression
                property: (property_identifier) @method-name)) @method-call

        ; ES6 imports
        (import_statement
            (import_clause (identifier) @imported-symbol)?
            source: (string) @import-source)

        ; CommonJS require
        (variable_declarator
            name: (identifier) @var-name
            value: (call_expression
                function: (identifier) @require-func
                arguments: (arguments (string) @require-source)
            )
        )
    """)

    cursor = QueryCursor(query)
    captures = cursor.captures(tree.root_node)

    elements = {
        'classes': [],
        'functions': [],
        'variables': [],
        'function_calls': [],
        'imports': []
    }

    imported_names = set()

    # Collect symbols
    for node in captures.get("func-name", []):
        elements['functions'].append({
            'type': 'function',
            'name': node.text.decode(),
            'line': node.start_point[0] + 1,
            'start_line': node.start_point[0] + 1,
            'end_line': node.end_point[0]+1
        })

    for node in captures.get("class-name", []):
        elements['classes'].append({
            'type': 'class',
            'name': node.text.decode(),
            'line': node.start_point[0] + 1,
            'start_line': node.start_point[0] + 1,
            'end_line': node.end_point[0]+1
        })

    for node in captures.get("var-name", []):
        elements['variables'].append({
            'type': 'variable',
            'name': node.text.decode(),
            'line': node.start_point[0] + 1,
            'start_line': node.start_point[0] + 1,
            'end_line': node.end_point[0]+1
        })

    for node in captures.get("called-func", []):
        elements['function_calls'].append({
            'type': 'function_call',
            'name': node.text.decode(),
            'line': node.start_point[0] + 1,
            'start_line': node.start_point[0] + 1,
            'end_line': node.end_point[0]+1
        })

    for node in captures.get("method-name", []):
        elements['function_calls'].append({
            'type': 'method_call',
            'name': node.text.decode(),
            'line': node.start_point[0] + 1,
            'start_line': node.start_point[0] + 1,
            'end_line': node.end_point[0]+1
        })

    # Handle imports
    sources = captures.get("import-source", []) + captures.get("require-source", [])
    imported_symbols = captures.get("imported-symbol", []) + captures.get("var-name", [])  # align require names

    for i, source_node in enumerate(sources):
        module_name = source_node.text.decode().strip('"\'')
        origin = get_module_origin(module_name, base_directory)
        imported_name = None
        if i < len(imported_symbols):
            imported_name = imported_symbols[i].text.decode()
            imported_names.add(imported_name)

        elements['imports'].append({
            'type': 'import',
            'imported_name': imported_name if imported_name else "require",
            'from_module': module_name,
            'origin': origin,
            'line': source_node.start_point[0] + 1,
            'path_exists': os.path.exists(origin) if origin and origin != "<node_builtin_or_external>" else False,
            'usage_lines': []
        })

    # Find import usages
    if imported_names:
        usages = find_import_usages(tree, imported_names)
        for imp in elements['imports']:
            name = imp['imported_name']
            if name and name in usages:
                imp['usage_lines'] = list(set(usages[name]) - {imp['line']})

    return elements

def process_file(filename, base_directory=None):
    if not base_directory:
        base_directory = os.path.dirname(filename)
    tree, code = parse_file(filename)
    elements = get_elements(tree, code, base_directory)
    return {
        'filename': filename,
        'elements': elements
    }

if __name__ == "__main__":
    filename = "/Users/ankits/My-Favourite-Playlist/server.js"
    base_directory = "/Users/ankits/My-Favourite-Playlist"
    if os.path.exists(filename):
        result = process_file(filename, base_directory)
        print(json.dumps(result, indent=2))
    else:
        print(f"File {filename} not found")

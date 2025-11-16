import os
from typing import Dict, List, Optional

from tree_sitter import Language, Parser, QueryCursor
import tree_sitter_go

from config import Configurations

config = Configurations()

GO_LANGUAGE = Language(tree_sitter_go.language())
parser = Parser(GO_LANGUAGE)
_MODULE_NAME_CACHE: Dict[str, Optional[str]] = {}


def parse_file(filename: str):
    with open(filename, "r", encoding="utf-8") as f:
        code = f.read()
    tree = parser.parse(code.encode("utf-8"))
    return tree, code


def _node_text(source: str, node) -> str:
    return source[node.start_byte : node.end_byte]


def _strip_quotes(value: Optional[str]) -> str:
    if not value:
        return ""
    value = value.strip()
    if value.startswith(("`", '"')) and value.endswith(("`", '"')):
        return value[1:-1]
    return value


def _get_module_name(base_directory: str) -> Optional[str]:
    cached = _MODULE_NAME_CACHE.get(base_directory)
    if cached is not None:
        return cached
    module_name = None
    go_mod_path = os.path.join(base_directory, "go.mod")
    try:
        with open(go_mod_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("module "):
                    parts = line.split()
                    if len(parts) >= 2:
                        module_name = parts[1]
                    break
    except OSError:
        module_name = None
    _MODULE_NAME_CACHE[base_directory] = module_name
    return module_name


def _resolve_import_origin(import_path: str, base_directory: Optional[str]) -> Optional[str]:
    if not base_directory:
        return None
    normalized = _strip_quotes(import_path)
    if not normalized:
        return None
    segments = [segment for segment in normalized.split("/") if segment]
    candidate = os.path.join(base_directory, *segments)
    if os.path.isdir(candidate):
        return os.path.normpath(candidate)
    go_file = f"{candidate}.go"
    if os.path.exists(go_file):
        return os.path.normpath(go_file)
    module_name = _get_module_name(base_directory)
    if module_name and normalized.startswith(module_name):
        rel_path = normalized[len(module_name) :].lstrip("/")
        if rel_path:
            module_candidate = os.path.join(base_directory, rel_path)
            if os.path.isdir(module_candidate):
                return os.path.normpath(module_candidate)
            module_go = f"{module_candidate}.go"
            if os.path.exists(module_go):
                return os.path.normpath(module_go)
    return None


def _collect_functions(root, source: str, file_path: str) -> List[Dict]:
    functions: List[Dict] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in {"function_declaration", "method_declaration"}:
            name_node = node.child_by_field_name("name")
            if not name_node:
                stack.extend(list(node.children))
                continue
            func_name = _node_text(source, name_node)
            receiver_node = node.child_by_field_name("receiver")
            receiver = _node_text(source, receiver_node).strip() if receiver_node else None
            functions.append(
                {
                    "type": "function",
                    "name": func_name,
                    "receiver": receiver,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "file_path": file_path,
                }
            )
        stack.extend(list(node.children))
    return functions


def _collect_types(root, source: str, file_path: str) -> List[Dict]:
    types: List[Dict] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "type_spec":
            name_node = node.child_by_field_name("name")
            type_node = node.child_by_field_name("type")
            if not name_node or not type_node:
                stack.extend(list(node.children))
                continue
            type_name = _node_text(source, name_node)
            types.append(
                {
                    "type": "type",
                    "name": type_name,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "type_kind": type_node.type,
                    "file_path": file_path,
                }
            )
        stack.extend(list(node.children))
    return types


def _extract_call_name(function_node, source: str) -> Optional[str]:
    if function_node is None:
        return None
    if function_node.type == "identifier":
        return _node_text(source, function_node)
    if function_node.type == "selector_expression":
        field_node = function_node.child_by_field_name("field")
        if field_node:
            return _node_text(source, field_node)
    return None


def _collect_function_calls(root, source: str) -> List[Dict]:
    calls: List[Dict] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "call_expression":
            function_node = node.child_by_field_name("function")
            call_name = _extract_call_name(function_node, source)
            if call_name:
                calls.append(
                    {
                        "type": "function_call",
                        "name": call_name,
                        "full_name": _node_text(source, function_node),
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                    }
                )
        stack.extend(list(node.children))
    return calls


def _collect_imports(root, source: str, base_directory: Optional[str]) -> List[Dict]:
    imports: List[Dict] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "import_declaration":
            for child in node.named_children:
                if child.type != "import_spec":
                    continue
                path_node = child.child_by_field_name("path")
                if not path_node:
                    continue
                raw_path = _node_text(source, path_node)
                path_value = _strip_quotes(raw_path)
                alias_node = child.child_by_field_name("name")
                alias = _node_text(source, alias_node) if alias_node else None
                imported_name = alias or (path_value.split("/")[-1] if path_value else None)
                origin = _resolve_import_origin(path_value, base_directory)
                imports.append(
                    {
                        "type": "import",
                        "imported_name": imported_name,
                        "alias": alias,
                        "from_module": path_value,
                        "origin": origin,
                        "line": child.start_point[0] + 1,
                        "path_exists": bool(origin and os.path.exists(origin)),
                        "usage_lines": [],
                    }
                )
        stack.extend(list(node.children))
    return imports


def _annotate_import_usages(tree, source: str, imports: List[Dict]) -> None:
    alias_map = {}
    for item in imports:
        alias_key = item.get("alias") or item.get("imported_name")
        if alias_key and alias_key not in {"_", "."}:
            alias_map[alias_key] = item
    if not alias_map:
        return
    query = GO_LANGUAGE.query("(identifier) @ident")
    cursor = QueryCursor(query)
    captures = cursor.captures(tree.root_node)
    for node in captures.get("ident", []):
        ident = node.text.decode("utf-8")
        import_entry = alias_map.get(ident)
        if not import_entry:
            continue
        line = node.start_point[0] + 1
        if line == import_entry["line"]:
            continue
        usage_lines = import_entry.setdefault("usage_lines", [])
        if line not in usage_lines:
            usage_lines.append(line)


def _attach_call_ranges(functions: List[Dict], calls: List[Dict]) -> None:
    functions_by_name: Dict[str, Dict] = {}
    for func in functions:
        functions_by_name.setdefault(func["name"], func)
    for call in calls:
        target = functions_by_name.get(call["name"])
        if not target:
            continue
        call["function_start_line"] = target["start_line"]
        call["function_end_line"] = target["end_line"]


def get_elements(tree, source: str, base_directory: str) -> Dict:
    elements: Dict = {
        "functions": [],
        "function_calls": [],
        "types": [],
    }
    functions = _collect_functions(tree.root_node, source, "")
    calls = _collect_function_calls(tree.root_node, source)
    _attach_call_ranges(functions, calls)
    elements["functions"] = functions
    elements["function_calls"] = calls
    elements["types"] = _collect_types(tree.root_node, source, "")
    imports = _collect_imports(tree.root_node, source, base_directory)
    _annotate_import_usages(tree, source, imports)
    return elements, imports


def process_file(filename: str, base_directory: Optional[str] = None) -> Dict:
    if not base_directory:
        base_directory = os.path.dirname(filename)
    tree, source = parse_file(filename)
    elements, imports = get_elements(tree, source, base_directory)
    # Ensure file_path for functions is populated after parsing.
    for func in elements.get("functions", []):
        func["file_path"] = filename
    for type_entry in elements.get("types", []):
        type_entry["file_path"] = filename
    return {"filename": filename, "elements": elements, "imports": imports}

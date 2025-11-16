from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from tree_sitter import Language, Node, Parser
import tree_sitter_go

GO_LANGUAGE = Language(tree_sitter_go.language())
parser = Parser(GO_LANGUAGE)


HTTP_METHODS = {
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "HEAD",
}
_CHAIN_TERMINATORS = {"methods"}
_ROUTER_FUNCTIONS = {"handlefunc", "handle"}
_GROUP_METHODS = {"group", "route", "pathprefix"}


@dataclass
class HandlerInfo:
    name: str
    file_path: Optional[str]
    start_line: Optional[int]
    end_line: Optional[int]
    handler_name: Optional[str]
    selector: Optional[str] = None


def _node_text(source: str, node: Node) -> str:
    return source[node.start_byte : node.end_byte]


def _strip_quotes(value: Optional[str]) -> str:
    if not value:
        return ""
    value = value.strip()
    if value.startswith(("`", '"')) and value.endswith(("`", '"')):
        return value[1:-1]
    return value


def _collect_function_definitions(
    root: Node, source: str, file_path: Path
) -> Dict[str, Dict]:
    functions: Dict[str, Dict] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = _node_text(source, name_node)
                entry = {
                    "type": "function",
                    "name": func_name,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "file_path": str(file_path),
                }
                functions.setdefault(func_name, entry)
        stack.extend(list(node.children))
    return functions


def _iter_call_arguments(call_node: Node) -> Sequence[Node]:
    arguments_node = call_node.child_by_field_name("arguments")
    if not arguments_node:
        return ()
    return [
        child
        for child in arguments_node.children
        if child.type not in {"(", ")", ","}
    ]


def _is_string_literal(node: Node) -> bool:
    return node.type in {"interpreted_string_literal", "raw_string_literal"}


def _extract_path_argument(call_node: Node, source: str) -> Optional[str]:
    for arg in _iter_call_arguments(call_node):
        if _is_string_literal(arg):
            return _strip_quotes(_node_text(source, arg))
    return None


def _extract_methods_from_arguments(call_node: Node, source: str) -> List[str]:
    methods: List[str] = []
    for arg in _iter_call_arguments(call_node):
        if _is_string_literal(arg):
            method = _strip_quotes(_node_text(source, arg)).upper()
            if method:
                methods.append(method)
    return methods


def _is_selector_operand_of(node: Node, field_name: str, source: str) -> bool:
    parent = node.parent
    if not parent or parent.type != "selector_expression":
        return False
    field_node = parent.child_by_field_name("field")
    if not field_node:
        return False
    if _node_text(source, field_node).lower() != field_name.lower():
        return False
    grandparent = parent.parent
    return bool(grandparent and grandparent.type == "call_expression")


def _normalize_http_method(name: str) -> Optional[str]:
    if not name:
        return None
    normalized = name.upper()
    if normalized in HTTP_METHODS:
        return normalized
    if name.lower() == "any":
        return "ANY"
    return None


def _extract_handler_info(
    handler_node: Optional[Node],
    source: str,
    file_path: Path,
    functions_by_name: Dict[str, Dict],
) -> Optional[HandlerInfo]:
    if handler_node is None:
        return None

    node_type = handler_node.type
    if node_type == "identifier":
        handler_name = _node_text(source, handler_node)
        definition = functions_by_name.get(handler_name)
        return HandlerInfo(
            name=handler_name,
            handler_name=handler_name,
            file_path=definition.get("file_path") if definition else None,
            start_line=definition.get("start_line") if definition else None,
            end_line=definition.get("end_line") if definition else None,
        )

    if node_type == "selector_expression":
        field_node = handler_node.child_by_field_name("field")
        if not field_node:
            return None
        handler_name = _node_text(source, field_node)
        definition = functions_by_name.get(handler_name)
        return HandlerInfo(
            name=_node_text(source, handler_node),
            handler_name=handler_name,
            file_path=definition.get("file_path") if definition else None,
            start_line=definition.get("start_line") if definition else None,
            end_line=definition.get("end_line") if definition else None,
            selector=_node_text(source, handler_node),
        )

    if node_type == "function_literal":
        return HandlerInfo(
            name=f"inline_handler@{handler_node.start_point[0] + 1}",
            handler_name=None,
            file_path=str(file_path),
            start_line=handler_node.start_point[0] + 1,
            end_line=handler_node.end_point[0] + 1,
        )
    return None


def _extract_handler_node(call_node: Node) -> Optional[Node]:
    args = list(_iter_call_arguments(call_node))
    if len(args) < 2:
        return args[1] if len(args) == 2 else None
    # Frameworks like gin/echo accept multiple middleware + handler arguments.
    for candidate in reversed(args):
        if candidate.type in {"identifier", "selector_expression", "function_literal"}:
            return candidate
    return None


def _join_paths(*segments: Optional[str]) -> str:
    parts = [segment for segment in segments if segment]
    if not parts:
        return ""
    path = ""
    for segment in parts:
        if not segment:
            continue
        if not path:
            path = segment
            continue
        if not path.endswith("/") and not segment.startswith("/"):
            path = f"{path}/{segment}"
        elif path.endswith("/") and segment.startswith("/"):
            path = f"{path}{segment.lstrip('/')}"
        else:
            path = f"{path}{segment}"
    return path


def _collect_path_prefix(node: Optional[Node], source: str) -> Optional[str]:
    segments: List[str] = []
    current = node
    visited = set()
    while current and current.type == "call_expression":
        if current.id in visited:
            break
        visited.add(current.id)
        func_node = current.child_by_field_name("function")
        if not func_node or func_node.type != "selector_expression":
            break
        field_node = func_node.child_by_field_name("field")
        method_lower = _node_text(source, field_node).lower() if field_node else ""
        if method_lower in _GROUP_METHODS:
            prefix = _extract_path_argument(current, source)
            if prefix:
                segments.append(prefix)
        operand = func_node.child_by_field_name("operand")
        if not operand:
            break
        current = operand if operand.type == "call_expression" else None
    if not segments:
        return None
    # segments collected from inner-most outward; reverse to maintain call order.
    return "".join(
        f"{segment if segment.startswith('/') else '/' + segment}"
        for segment in reversed(segments)
    )


def _build_endpoint_entry(
    path: str,
    http_method: str,
    handler_info: HandlerInfo,
    route_file: Path,
) -> Dict:
    entry = {
        "type": "function",
        "route": path,
        "http_method": http_method,
        "route_file": str(route_file),
        "name": handler_info.name,
        "handler_name": handler_info.handler_name or handler_info.name,
        "handler_selector": handler_info.selector,
        "file_path": handler_info.file_path,
        "start_line": handler_info.start_line,
        "end_line": handler_info.end_line,
    }
    return entry


def _extract_routes_from_call(
    call_node: Node,
    source: str,
    file_path: Path,
    functions_by_name: Dict[str, Dict],
) -> List[Dict]:
    func_node = call_node.child_by_field_name("function")
    if not func_node:
        return []

    if func_node.type == "selector_expression":
        field_node = func_node.child_by_field_name("field")
        if not field_node:
            return []
        method_name = _node_text(source, field_node)
        method_lower = method_name.lower()
        operand_node = func_node.child_by_field_name("operand")

        if method_lower in _CHAIN_TERMINATORS and operand_node and operand_node.type == "call_expression":
            base_routes = _extract_routes_from_call(
                operand_node, source, file_path, functions_by_name
            )
            http_methods = _extract_methods_from_arguments(call_node, source)
            if not http_methods:
                return base_routes
            expanded: List[Dict] = []
            for route in base_routes:
                for verb in http_methods:
                    clone = route.copy()
                    clone["http_method"] = verb
                    expanded.append(clone)
            return expanded

        normalized_method = _normalize_http_method(method_name)
        if normalized_method:
            path = _extract_path_argument(call_node, source)
            if not path:
                return []
            prefix = _collect_path_prefix(operand_node, source)
            full_path = _join_paths(prefix, path) if prefix else path
            handler_node = _extract_handler_node(call_node)
            handler_info = _extract_handler_info(
                handler_node, source, file_path, functions_by_name
            )
            if not handler_info:
                return []
            return [
                _build_endpoint_entry(full_path, normalized_method, handler_info, file_path)
            ]

        if method_lower in _ROUTER_FUNCTIONS:
            path = _extract_path_argument(call_node, source)
            if not path:
                return []
            prefix = _collect_path_prefix(operand_node, source)
            full_path = _join_paths(prefix, path) if prefix else path
            handler_node = _extract_handler_node(call_node)
            handler_info = _extract_handler_info(
                handler_node, source, file_path, functions_by_name
            )
            if not handler_info:
                return []
            return [
                _build_endpoint_entry(full_path, "GET", handler_info, file_path)
            ]

    return []


def _is_call_operand_of_methods(call_node: Node, source: str) -> bool:
    return _is_selector_operand_of(call_node, "methods", source)


def find_api_endpoints(file_path: Path, repo_root: str) -> List[Dict]:
    try:
        source = file_path.read_text(encoding="utf-8")
    except OSError:
        return []

    tree = parser.parse(source.encode("utf-8"))
    functions_by_name = _collect_function_definitions(
        tree.root_node, source, file_path
    )

    endpoints: List[Dict] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "call_expression":
            if _is_call_operand_of_methods(node, source):
                stack.extend(list(node.children))
                continue
            routes = _extract_routes_from_call(
                node, source, file_path, functions_by_name
            )
            if routes:
                endpoints.extend(routes)
        stack.extend(list(node.children))

    return endpoints

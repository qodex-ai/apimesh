from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

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
_GROUP_ASSIGNMENTS = {"short_var_declaration", "assignment_statement"}
_FUNCTION_SCOPES = {"function_declaration", "method_declaration"}


@dataclass
class HandlerInfo:
    name: str
    file_path: Optional[str]
    start_line: Optional[int]
    end_line: Optional[int]
    handler_name: Optional[str]
    selector: Optional[str] = None


def _node_text(source: bytes, node: Node) -> str:
    # tree-sitter offsets are byte offsets, so slice the bytes and decode.
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


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


def _join_prefix_segments(segments: Sequence[str]) -> str:
    joined = ""
    for segment in segments:
        cleaned = segment.strip("/")
        if cleaned:
            joined = f"{joined}/{cleaned}"
    return joined


def _walk_group_chain(
    node: Optional[Node], source: bytes
) -> Tuple[List[str], Optional[str], bool]:
    """Walk a receiver expression inside-out.

    Returns the group prefixes in call order, the identifier the chain started
    from (``r`` in ``r.Group("/api").Group("/v1")``), and whether any group call
    was seen at all. Group arguments that are not string literals contribute no
    segment because their value is unknown at parse time.
    """
    segments: List[str] = []
    current = node
    visited = set()
    base_identifier: Optional[str] = None
    saw_group = False
    while current is not None:
        if current.type == "identifier":
            base_identifier = _node_text(source, current)
            break
        if current.type != "call_expression" or current.id in visited:
            break
        visited.add(current.id)
        func_node = current.child_by_field_name("function")
        if not func_node or func_node.type != "selector_expression":
            break
        field_node = func_node.child_by_field_name("field")
        method_lower = _node_text(source, field_node).lower() if field_node else ""
        if method_lower in _GROUP_METHODS:
            saw_group = True
            prefix = _extract_path_argument(current, source)
            if prefix:
                segments.append(prefix)
        current = func_node.child_by_field_name("operand")
    segments.reverse()
    return segments, base_identifier, saw_group


def _single_expression(list_node: Optional[Node]) -> Optional[Node]:
    if not list_node:
        return None
    children = [child for child in list_node.children if child.type != ","]
    return children[0] if len(children) == 1 else None


def _enclosing_scope(node: Optional[Node]) -> Optional[int]:
    """Identity of the function a node sits in, None when it sits at file scope."""
    current = node
    while current is not None:
        if current.type in _FUNCTION_SCOPES:
            return current.id
        current = current.parent
    return None


def _scoped_lookup(table: Dict, scope: Optional[int], name: str):
    """Key for a name: this function first, then file scope, then a unique match.

    The last step keeps a package-level group that some other function assigns
    resolvable, which only stays safe while the name is unambiguous.
    """
    for candidate in ((scope, name), (None, name)):
        if candidate in table:
            return candidate
    matches = [key for key in table if key[1] == name]
    return matches[0] if len(matches) == 1 else None


def _collect_group_prefixes(
    root: Node, source: bytes
) -> Dict[Tuple[Optional[int], str], str]:
    """Map router group variables to their full path prefix, per function.

    ``v1 := r.Group("/api/v1")`` followed by ``v1.GET("/users", h)`` is the
    dominant gin/echo idiom, and the route call alone carries no prefix. Names
    are keyed by the function they were declared in, so two functions that both
    declare ``api := r.Group(...)`` do not steal each other's prefix. Raw
    declarations are collected first and resolved afterwards because the tree
    walk does not visit statements in source order.
    """
    declarations: List[Tuple[int, Optional[int], str, Optional[str], List[str]]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in _GROUP_ASSIGNMENTS:
            target = _single_expression(node.child_by_field_name("left"))
            value = _single_expression(node.child_by_field_name("right"))
            if (
                target is not None
                and target.type == "identifier"
                and value is not None
                and value.type == "call_expression"
            ):
                segments, base_identifier, saw_group = _walk_group_chain(value, source)
                if saw_group:
                    declarations.append(
                        (
                            node.start_byte,
                            _enclosing_scope(node),
                            _node_text(source, target),
                            base_identifier,
                            segments,
                        )
                    )
        stack.extend(list(node.children))

    declarations.sort(key=lambda item: item[0])
    raw: Dict[Tuple[Optional[int], str], Tuple[Optional[str], List[str]]] = {}
    for _, scope, name, base_identifier, segments in declarations:
        raw.setdefault((scope, name), (base_identifier, segments))

    resolved: Dict[Tuple[Optional[int], str], str] = {}

    def _resolve(key: Tuple[Optional[int], str], seen: set) -> str:
        if key in resolved:
            return resolved[key]
        if key in seen:
            return ""
        seen.add(key)
        base_identifier, segments = raw[key]
        parent_key = _scoped_lookup(raw, key[0], base_identifier) if base_identifier else None
        parent = _resolve(parent_key, seen) if parent_key else ""
        prefix = _join_prefix_segments(([parent] if parent else []) + segments)
        resolved[key] = prefix
        return prefix

    for key in raw:
        _resolve(key, set())
    return {key: prefix for key, prefix in resolved.items() if prefix}


def _collect_path_prefix(
    node: Optional[Node],
    source: bytes,
    group_prefixes: Dict[Tuple[Optional[int], str], str],
) -> Optional[str]:
    segments, base_identifier, _ = _walk_group_chain(node, source)
    base_prefix = None
    if base_identifier:
        key = _scoped_lookup(group_prefixes, _enclosing_scope(node), base_identifier)
        base_prefix = group_prefixes.get(key) if key else None
    if base_prefix:
        segments = [base_prefix] + segments
    if not segments:
        return None
    return _join_prefix_segments(segments)


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
    group_prefixes: Dict[Tuple[Optional[int], str], str],
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
                operand_node, source, file_path, functions_by_name, group_prefixes
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
            prefix = _collect_path_prefix(operand_node, source, group_prefixes)
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
            prefix = _collect_path_prefix(operand_node, source, group_prefixes)
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
        source = file_path.read_bytes()
    except OSError:
        return []

    tree = parser.parse(source)
    functions_by_name = _collect_function_definitions(
        tree.root_node, source, file_path
    )
    group_prefixes = _collect_group_prefixes(tree.root_node, source)

    endpoints: List[Dict] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "call_expression":
            if _is_call_operand_of_methods(node, source):
                stack.extend(list(node.children))
                continue
            routes = _extract_routes_from_call(
                node, source, file_path, functions_by_name, group_prefixes
            )
            if routes:
                endpoints.extend(routes)
        stack.extend(list(node.children))

    return endpoints

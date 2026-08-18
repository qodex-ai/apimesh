from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

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
# .Any registers the same handler under every verb. OpenAPI has no "any"
# operation, so it is expanded into the standard set instead.
_ANY_METHOD_NAMES = {"any"}
_ANY_METHOD_VERBS = ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD")
_CHAIN_TERMINATORS = {"methods"}
_ROUTER_FUNCTIONS = {"handlefunc", "handle"}
_GROUP_METHODS = {"group", "route", "pathprefix"}
_MOUNT_METHODS = {"mount"}
_GROUP_ASSIGNMENTS = {"short_var_declaration", "assignment_statement"}
# Every construct that can shadow a name. Blocks are scopes in Go, so an inner
# a := r.Group("/inner") hides the outer a for the rest of that block only.
_SCOPE_TYPES = {"function_declaration", "method_declaration", "func_literal", "block"}
_CALLABLE_SCOPES = {"function_declaration", "method_declaration", "func_literal"}
# What may stand in for a handler argument. Anything else (a string, a struct
# literal, a channel) means the call is not registering a route.
_HANDLER_NODE_TYPES = {"identifier", "selector_expression", "func_literal"}

# Counted across one extraction run and reported once, so a repo that silently
# loses routes says so instead of shipping a thin spec.
_EXTRACTION_DROPS = {"rejected": 0, "no_handler": 0}


@dataclass
class HandlerInfo:
    name: str
    file_path: Optional[str]
    start_line: Optional[int]
    end_line: Optional[int]
    handler_name: Optional[str]
    selector: Optional[str] = None
    receiver_type: Optional[str] = None


def reset_extraction_drops() -> None:
    for key in _EXTRACTION_DROPS:
        _EXTRACTION_DROPS[key] = 0


def extraction_drops() -> Dict[str, int]:
    return dict(_EXTRACTION_DROPS)


def log_extraction_drops() -> None:
    """One line for everything extraction threw away, or nothing at all."""
    rejected = _EXTRACTION_DROPS["rejected"]
    no_handler = _EXTRACTION_DROPS["no_handler"]
    if not rejected and not no_handler:
        return
    print(
        f"apimesh: golang extraction skipped {rejected} calls that register no "
        f"route and {no_handler} routes with no usable handler"
    )


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


def _extract_path_argument(call_node: Node, source: bytes) -> Optional[str]:
    for arg in _iter_call_arguments(call_node):
        if _is_string_literal(arg):
            return _strip_quotes(_node_text(source, arg))
    return None


def _extract_methods_from_arguments(call_node: Node, source: bytes) -> List[str]:
    methods: List[str] = []
    for arg in _iter_call_arguments(call_node):
        if _is_string_literal(arg):
            method = _strip_quotes(_node_text(source, arg)).upper()
            if method:
                methods.append(method)
    return methods


def _route_verbs(method_name: str) -> List[str]:
    """The verbs a router method registers, empty when it registers none."""
    if not method_name:
        return []
    normalized = method_name.upper()
    if normalized in HTTP_METHODS:
        return [normalized]
    if method_name.lower() in _ANY_METHOD_NAMES:
        return list(_ANY_METHOD_VERBS)
    return []


def _split_method_pattern(pattern: str) -> Optional[Tuple[str, str]]:
    """Go 1.22 patterns carry the verb: "GET /items" is GET on /items."""
    parts = pattern.split(None, 1)
    if len(parts) != 2:
        return None
    verb = parts[0].upper()
    path = parts[1].strip()
    if verb in HTTP_METHODS and path.startswith("/"):
        return verb, path
    return None


def _chain_methods(call_node: Node, source: bytes) -> List[str]:
    """Verbs from a .Methods(...) call anywhere above this one in the chain.

    gorilla lets other calls sit in between, and
    HandleFunc(...).Queries(...).Methods("POST") used to lose its POST because
    only a .Methods directly on the route call was looked at.
    """
    current = call_node
    while True:
        parent = current.parent
        if parent is None or parent.type != "selector_expression":
            return []
        operand = parent.child_by_field_name("operand")
        # tree-sitter hands out a fresh wrapper per lookup, so nodes compare by id.
        if operand is None or operand.id != current.id:
            return []
        grandparent = parent.parent
        if grandparent is None or grandparent.type != "call_expression":
            return []
        field_node = parent.child_by_field_name("field")
        field = _node_text(source, field_node).lower() if field_node else ""
        if field in _CHAIN_TERMINATORS:
            return _extract_methods_from_arguments(grandparent, source)
        current = grandparent


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


def _scope_chain(node: Optional[Node]) -> Iterator[int]:
    """Every scope enclosing a node, innermost first."""
    current = node
    while current is not None:
        if current.type in _SCOPE_TYPES:
            yield current.id
        current = current.parent


def _enclosing_scope(node: Optional[Node]) -> Optional[int]:
    for scope in _scope_chain(node):
        return scope
    return None


def _type_name(node: Optional[Node], source: bytes) -> Optional[str]:
    """The bare type name of a type expression: *pkg.Server reads as Server."""
    if node is None:
        return None
    if node.type == "type_identifier":
        return _node_text(source, node)
    for child in node.children:
        found = _type_name(child, source)
        if found:
            return found
    return None


def _composite_type_name(node: Optional[Node], source: bytes) -> Optional[str]:
    """The type of a ``&Server{}`` or ``Server{}`` value, else None."""
    current = node
    if current is not None and current.type == "unary_expression":
        current = current.child_by_field_name("operand")
    if current is None or current.type != "composite_literal":
        return None
    return _type_name(current.child_by_field_name("type"), source)


def _mount_target_name(node: Optional[Node], source: bytes) -> Optional[str]:
    """The router builder a Mount points at: adminRouter or adminRouter()."""
    if node is None:
        return None
    if node.type == "identifier":
        return _node_text(source, node)
    if node.type == "call_expression":
        func_node = node.child_by_field_name("function")
        if func_node is not None and func_node.type == "identifier":
            return _node_text(source, func_node)
    return None


def _handler_info(
    name: str,
    handler_name: str,
    definition: Optional[Dict],
    selector: Optional[str],
    receiver_type: Optional[str] = None,
) -> HandlerInfo:
    return HandlerInfo(
        name=name,
        handler_name=handler_name,
        file_path=definition.get("file_path") if definition else None,
        start_line=definition.get("start_line") if definition else None,
        end_line=definition.get("end_line") if definition else None,
        selector=selector,
        receiver_type=receiver_type,
    )


class _FileSymbols:
    """What one Go file says about its routers and its handlers.

    Built in a single tree walk. Names resolve against the nearest enclosing
    block first, so an inner ``a := r.Group("/inner")`` shadows the outer ``a``
    and a chi closure parameter shadows an outer group of the same name: the
    closure's own prefix applies to it instead.
    """

    def __init__(self, root: Node, source: bytes, file_path: Path):
        self.source = source
        self.file_path = file_path
        self.functions: Dict[str, Dict] = {}
        self.methods: Dict[Tuple[str, str], Dict] = {}
        self.methods_by_name: Dict[str, List[Dict]] = {}
        self.variable_types: Dict[Tuple[int, str], str] = {}
        self.import_aliases: Set[str] = set()
        self.groups: Dict[Tuple[int, str], Node] = {}
        self.shadowed: Set[Tuple[int, str]] = set()
        # func_literal id -> the Route call that owns it and its path segment.
        self.closures: Dict[int, Tuple[Node, str]] = {}
        # router builder name -> the Mount call and its path segment.
        self.mounts: Dict[str, Tuple[Node, str]] = {}
        self._prefix_cache: Dict[Tuple[str, object], str] = {}
        self._resolving: Set[Tuple[str, object]] = set()
        self._collect(root)

    # Collection -----------------------------------------------------------

    def _collect(self, root: Node) -> None:
        declarations: List[Tuple[int, Tuple[int, str], Node]] = []
        stack = [root]
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type == "function_declaration":
                self._collect_function(node)
            elif node_type == "method_declaration":
                self._collect_method(node)
            elif node_type == "parameter_declaration":
                self._collect_parameter(node)
            elif node_type == "var_spec":
                self._collect_var_spec(node)
            elif node_type == "import_spec":
                self._collect_import_alias(node)
            elif node_type in _GROUP_ASSIGNMENTS:
                self._collect_assignment(node, declarations)
            elif node_type == "call_expression":
                self._collect_closure_or_mount(node)
            stack.extend(list(node.children))
        # The walk does not visit statements in source order, so the first
        # declaration of a name in its scope is picked afterwards.
        declarations.sort(key=lambda item: item[0])
        for _, key, value in declarations:
            self.groups.setdefault(key, value)

    def _definition_entry(self, node: Node, name: str) -> Dict:
        return {
            "type": "function",
            "name": name,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "file_path": str(self.file_path),
        }

    def _collect_function(self, node: Node) -> None:
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        name = _node_text(self.source, name_node)
        self.functions.setdefault(name, self._definition_entry(node, name))

    def _collect_method(self, node: Node) -> None:
        """Methods are handlers too: r.POST("/users", s.CreateUser)."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        name = _node_text(self.source, name_node)
        receiver_node = node.child_by_field_name("receiver")
        receiver_type = _type_name(receiver_node, self.source)
        entry = self._definition_entry(node, name)
        entry["receiver_type"] = receiver_type
        if receiver_node is not None:
            entry["receiver"] = _node_text(self.source, receiver_node).strip()
        self.methods_by_name.setdefault(name, []).append(entry)
        if receiver_type:
            self.methods.setdefault((receiver_type, name), entry)

    def _collect_parameter(self, node: Node) -> None:
        owner = node.parent.parent if node.parent is not None else None
        if owner is None or owner.type not in _CALLABLE_SCOPES:
            return
        type_name = _type_name(node.child_by_field_name("type"), self.source)
        for child in node.children:
            if child.type != "identifier":
                continue
            name = _node_text(self.source, child)
            # A parameter is the router itself, never an outer group of the
            # same name, so it stops the lookup rather than inheriting a prefix.
            self.shadowed.add((owner.id, name))
            if type_name:
                self.variable_types[(owner.id, name)] = type_name

    def _collect_var_spec(self, node: Node) -> None:
        scope = _enclosing_scope(node)
        if scope is None:
            return
        type_name = _type_name(node.child_by_field_name("type"), self.source)
        if not type_name:
            return
        for child in node.children:
            if child.type == "identifier":
                self.variable_types[(scope, _node_text(self.source, child))] = type_name

    def _collect_import_alias(self, node: Node) -> None:
        alias_node = node.child_by_field_name("name")
        if alias_node is not None:
            self.import_aliases.add(_node_text(self.source, alias_node))
            return
        path_node = node.child_by_field_name("path")
        if path_node is None:
            return
        path_value = _strip_quotes(_node_text(self.source, path_node))
        if path_value:
            self.import_aliases.add(path_value.split("/")[-1])

    def _collect_assignment(self, node: Node, declarations: List) -> None:
        target = _single_expression(node.child_by_field_name("left"))
        value = _single_expression(node.child_by_field_name("right"))
        if target is None or target.type != "identifier" or value is None:
            return
        scope = _enclosing_scope(node)
        if scope is None:
            return
        name = _node_text(self.source, target)
        if value.type == "call_expression":
            _, _, saw_group = _walk_group_chain(value, self.source)
            if saw_group:
                declarations.append((node.start_byte, (scope, name), value))
                return
        type_name = _composite_type_name(value, self.source)
        if type_name:
            self.variable_types[(scope, name)] = type_name

    def _collect_closure_or_mount(self, node: Node) -> None:
        func_node = node.child_by_field_name("function")
        if func_node is None or func_node.type != "selector_expression":
            return
        field_node = func_node.child_by_field_name("field")
        if field_node is None:
            return
        field = _node_text(self.source, field_node).lower()
        if field not in _GROUP_METHODS and field not in _MOUNT_METHODS:
            return
        args = list(_iter_call_arguments(node))
        if len(args) < 2 or not _is_string_literal(args[0]):
            return
        path = _strip_quotes(_node_text(self.source, args[0]))
        if not path.startswith("/"):
            return
        if field in _MOUNT_METHODS:
            target = _mount_target_name(args[1], self.source)
            if target:
                self.mounts.setdefault(target, (node, path))
            return
        for arg in args[1:]:
            if arg.type == "func_literal":
                self.closures[arg.id] = (node, path)
                break

    # Prefix resolution ----------------------------------------------------

    def prefix_for(self, node: Optional[Node]) -> str:
        """The prefix a route registered on ``node`` inherits.

        A group variable carries its own absolute prefix, so the closure or
        mount a call sits in only applies when the receiver is the router that
        construct handed out.
        """
        segments, base_identifier, _ = _walk_group_chain(node, self.source)
        key = self._lookup_group(node, base_identifier) if base_identifier else None
        if key:
            base = self._group_prefix(key)
        elif base_identifier and base_identifier in self.mounts:
            # A router built into a variable and mounted later: the Mount is
            # what says where its routes live.
            base = self._mount_prefix(base_identifier)
        else:
            base = self.context_prefix(node)
        return _join_prefix_segments(([base] if base else []) + segments)

    def context_prefix(self, node: Optional[Node]) -> str:
        """The prefix pushed by the chi Route closure or mounted router builder
        this node sits in. Neither hands its routes a prefix of its own."""
        current = node
        while current is not None:
            if current.type == "func_literal" and current.id in self.closures:
                return self._closure_prefix(current.id)
            if current.type in {"function_declaration", "method_declaration"}:
                name_node = current.child_by_field_name("name")
                name = _node_text(self.source, name_node) if name_node else ""
                return self._mount_prefix(name) if name in self.mounts else ""
            current = current.parent
        return ""

    def _lookup_group(self, node: Optional[Node], name: str):
        for scope in _scope_chain(node):
            if (scope, name) in self.groups:
                return (scope, name)
            if (scope, name) in self.shadowed:
                return None
        # A package level group assigned inside some other function stays
        # resolvable, which only holds while the name is unambiguous.
        matches = [key for key in self.groups if key[1] == name]
        return matches[0] if len(matches) == 1 else None

    def _resolve(self, cache_key, resolver) -> str:
        cached = self._prefix_cache.get(cache_key)
        if cached is not None:
            return cached
        if cache_key in self._resolving:
            return ""
        self._resolving.add(cache_key)
        try:
            prefix = resolver()
        finally:
            self._resolving.discard(cache_key)
        self._prefix_cache[cache_key] = prefix
        return prefix

    def _group_prefix(self, key: Tuple[int, str]) -> str:
        return self._resolve(("group", key), lambda: self.prefix_for(self.groups[key]))

    def _closure_prefix(self, literal_id: int) -> str:
        call, path = self.closures[literal_id]
        return self._resolve(
            ("closure", literal_id),
            lambda: _join_prefix_segments([self._receiver_prefix(call), path]),
        )

    def _mount_prefix(self, name: str) -> str:
        call, path = self.mounts[name]
        return self._resolve(
            ("mount", name),
            lambda: _join_prefix_segments([self._receiver_prefix(call), path]),
        )

    def _receiver_prefix(self, call_node: Node) -> str:
        func_node = call_node.child_by_field_name("function")
        operand = (
            func_node.child_by_field_name("operand") if func_node is not None else None
        )
        return self.prefix_for(operand)

    # Handlers -------------------------------------------------------------

    def handler_for(self, node: Optional[Node]) -> Optional[HandlerInfo]:
        if node is None:
            return None
        if node.type == "identifier":
            name = _node_text(self.source, node)
            return _handler_info(name, name, self.functions.get(name), None)
        if node.type == "selector_expression":
            field_node = node.child_by_field_name("field")
            if field_node is None:
                return None
            name = _node_text(self.source, field_node)
            selector = _node_text(self.source, node)
            definition = None
            receiver_type = None
            if not self._is_package_selector(node):
                receiver_type = self._receiver_type(node)
                definition = self._method_definition(node, name) or self.functions.get(
                    name
                )
            return _handler_info(selector, name, definition, selector, receiver_type)
        if node.type == "func_literal":
            return HandlerInfo(
                name=f"inline_handler@{node.start_point[0] + 1}",
                handler_name=None,
                file_path=str(self.file_path),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            )
        return None

    def _is_package_selector(self, selector_node: Node) -> bool:
        """``handlers.ListUsers`` names another package's function.

        Nothing in this file can be it, and guessing at a same named local
        method would document the wrong body. The repo wide function index
        resolves it later, by name.
        """
        operand = selector_node.child_by_field_name("operand")
        if operand is None or operand.type != "identifier":
            return False
        return _node_text(self.source, operand) in self.import_aliases

    def _receiver_type(self, selector_node: Node) -> Optional[str]:
        """The type of the variable ``svc.List`` is called on, when the file
        says what it is.

        ``var svc UserService`` and ``svc := UserService{}`` name the type
        outright; ``svc := NewUserService()`` does not, and there the selector
        text is all the repo wide lookup has to go on.
        """
        operand = selector_node.child_by_field_name("operand")
        if operand is None or operand.type != "identifier":
            return None
        return self._variable_type(operand, _node_text(self.source, operand))

    def _method_definition(self, selector_node: Node, name: str) -> Optional[Dict]:
        """The method ``s.CreateUser`` names, picked by what type ``s`` is."""
        type_name = self._receiver_type(selector_node)
        if type_name:
            entry = self.methods.get((type_name, name))
            if entry:
                return entry
        candidates = self.methods_by_name.get(name) or []
        return candidates[0] if len(candidates) == 1 else None

    def _variable_type(self, node: Node, name: str) -> Optional[str]:
        for scope in _scope_chain(node):
            type_name = self.variable_types.get((scope, name))
            if type_name:
                return type_name
        return None


def _handler_node(args: Sequence[Node], start_index: int) -> Optional[Node]:
    """The handler argument, or None when the call carries nothing usable.

    Frameworks accept middleware between the path and the handler, so the last
    handler shaped argument wins.
    """
    for candidate in reversed(list(args[start_index:])):
        if candidate.type in _HANDLER_NODE_TYPES:
            return candidate
    return None


def _route_signature(
    args: Sequence[Node], source: bytes, verbs: List[str], method_lower: str
) -> Optional[Tuple[str, List[str], int]]:
    """(path, verbs, first index past the path), or None for a non-route call.

    The path has to be the first argument and has to look like a path. Without
    that every db.Get(&user, "SELECT ...") and cache.Get(ctx, "key") in the
    repo reads as an endpoint.
    """
    if not args or not _is_string_literal(args[0]):
        return None
    first = _strip_quotes(_node_text(source, args[0]))
    if first.startswith("/"):
        return first, verbs, 1
    split = _split_method_pattern(first)
    if split:
        return split[1], [split[0]], 1
    # gin's r.Handle("GET", "/users", h) puts the verb in front of the path.
    if (
        method_lower in _ROUTER_FUNCTIONS
        and first.upper() in HTTP_METHODS
        and len(args) > 1
        and _is_string_literal(args[1])
    ):
        path = _strip_quotes(_node_text(source, args[1]))
        if path.startswith("/"):
            return path, [first.upper()], 2
    return None


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
        # Which type's method this is, so a repo with two ListUsers methods on
        # different types hydrates to the one this route registered.
        "handler_receiver_type": handler_info.receiver_type,
        "file_path": handler_info.file_path,
        "start_line": handler_info.start_line,
        "end_line": handler_info.end_line,
    }
    return entry


def _extract_routes_from_call(
    call_node: Node,
    source: bytes,
    file_path: Path,
    symbols: _FileSymbols,
) -> List[Dict]:
    func_node = call_node.child_by_field_name("function")
    if not func_node or func_node.type != "selector_expression":
        return []
    field_node = func_node.child_by_field_name("field")
    if not field_node:
        return []
    method_name = _node_text(source, field_node)
    method_lower = method_name.lower()
    verbs = _route_verbs(method_name)
    if not verbs and method_lower not in _ROUTER_FUNCTIONS:
        return []

    args = list(_iter_call_arguments(call_node))
    signature = _route_signature(args, source, verbs, method_lower)
    if signature is None:
        _EXTRACTION_DROPS["rejected"] += 1
        return []
    path, methods, handler_index = signature

    # net/http HandleFunc serves every verb; GET is the one that gets documented.
    if not methods:
        methods = ["GET"]
    chain_methods = _chain_methods(call_node, source)
    if chain_methods:
        methods = chain_methods

    handler_info = symbols.handler_for(_handler_node(args, handler_index))
    if handler_info is None:
        _EXTRACTION_DROPS["no_handler"] += 1
        return []

    prefix = symbols.prefix_for(func_node.child_by_field_name("operand"))
    full_path = _join_paths(prefix, path) if prefix else path
    return [
        _build_endpoint_entry(full_path, verb, handler_info, file_path)
        for verb in methods
    ]


def find_api_endpoints(file_path: Path, repo_root: str) -> List[Dict]:
    try:
        source = file_path.read_bytes()
    except OSError:
        return []

    tree = parser.parse(source)
    symbols = _FileSymbols(tree.root_node, source, file_path)

    endpoints: List[Dict] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "call_expression":
            endpoints.extend(
                _extract_routes_from_call(node, source, file_path, symbols)
            )
        stack.extend(list(node.children))

    return endpoints

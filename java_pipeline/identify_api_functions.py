from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from tree_sitter import Language, Node, Parser
import tree_sitter_java

JAVA_LANGUAGE = Language(tree_sitter_java.language())
parser = Parser(JAVA_LANGUAGE)


# A class only serves HTTP when one of these sits on it. A mapping annotated
# method on anything else (a @Service, a plain helper) is not an endpoint.
CONTROLLER_ANNOTATIONS = {"RestController", "Controller"}

# The shorthand annotations carry their verb in their own name.
VERB_ANNOTATIONS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}

REQUEST_MAPPING = "RequestMapping"

HTTP_METHODS = {
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "HEAD",
    "TRACE",
}

# The annotation attributes that carry the path, in the order Spring reads them.
_PATH_ATTRIBUTES = ("value", "path")

# Where a class or a method declaration keeps its annotations.
_MODIFIERS = "modifiers"
_ANNOTATION_TYPES = {"marker_annotation", "annotation"}

# Every declaration that can hold a controller's methods.
_CLASS_TYPES = {"class_declaration", "interface_declaration"}

# Counted across one extraction run and reported once, so a repo that silently
# loses routes says so instead of shipping a thin spec.
_EXTRACTION_DROPS = {"assumed_get": 0, "unresolved": 0}


def reset_extraction_drops() -> None:
    for key in _EXTRACTION_DROPS:
        _EXTRACTION_DROPS[key] = 0


def extraction_drops() -> Dict[str, int]:
    return dict(_EXTRACTION_DROPS)


def log_extraction_drops() -> None:
    """One line for everything extraction assumed or threw away, or nothing."""
    assumed_get = _EXTRACTION_DROPS["assumed_get"]
    unresolved = _EXTRACTION_DROPS["unresolved"]
    if not assumed_get and not unresolved:
        return
    print(
        f"apimesh: java extraction documented {assumed_get} @RequestMapping "
        "methods with no method attribute as GET only and skipped "
        f"{unresolved} mappings whose arguments it could not read"
    )


def _node_text(source: bytes, node: Node) -> str:
    # tree-sitter offsets are byte offsets, so slice the bytes and decode.
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _string_literal_value(node: Node, source: bytes) -> Optional[str]:
    """The text of a plain string literal, or None for anything else.

    A concatenation or a constant reference has no value at parse time, and
    guessing one would document a path the service does not serve.
    """
    if node.type != "string_literal":
        return None
    return "".join(
        _node_text(source, child)
        for child in node.children
        if child.type == "string_fragment"
    )


def _simple_name(node: Optional[Node], source: bytes) -> str:
    """The last segment of a name: org.springframework...GetMapping is GetMapping."""
    if node is None:
        return ""
    return _node_text(source, node).strip().split(".")[-1]


def _annotations(declaration: Node) -> List[Node]:
    # The modifiers of a declaration are a plain child, not a named field, and
    # the annotations sit inside them.
    for child in declaration.children:
        if child.type != _MODIFIERS:
            continue
        return [node for node in child.children if node.type in _ANNOTATION_TYPES]
    return []


def _annotation_name(annotation: Node, source: bytes) -> str:
    return _simple_name(annotation.child_by_field_name("name"), source)


def _annotation_by_name(declaration: Node, source: bytes, name: str) -> Optional[Node]:
    for annotation in _annotations(declaration):
        if _annotation_name(annotation, source) == name:
            return annotation
    return None


def _has_controller_annotation(declaration: Node, source: bytes) -> bool:
    return any(
        _annotation_name(annotation, source) in CONTROLLER_ANNOTATIONS
        for annotation in _annotations(declaration)
    )


def _argument_nodes(annotation: Node) -> Sequence[Node]:
    arguments = annotation.child_by_field_name("arguments")
    if arguments is None:
        return ()
    return [
        child
        for child in arguments.children
        if child.is_named and child.type not in {"(", ")", ","}
    ]


def _attribute_value(annotation: Node, source: bytes, names: Sequence[str]) -> Optional[Node]:
    """The value node of the first named attribute of this annotation."""
    for argument in _argument_nodes(annotation):
        if argument.type != "element_value_pair":
            continue
        key = argument.child_by_field_name("key")
        if key is not None and _node_text(source, key).strip() in names:
            return argument.child_by_field_name("value")
    return None


def _positional_value(annotation: Node) -> Optional[Node]:
    """``@GetMapping("/x")`` writes its path without naming the attribute."""
    for argument in _argument_nodes(annotation):
        if argument.type != "element_value_pair":
            return argument
    return None


def _array_elements(node: Node) -> List[Node]:
    return [
        child
        for child in node.children
        if child.is_named and child.type not in {"{", "}", ","}
    ]


def _annotation_paths(annotation: Node, source: bytes) -> Tuple[List[str], bool]:
    """The paths one mapping annotation declares, and whether they were readable.

    An annotation with no path attribute at all contributes the empty path, so
    the endpoint sits on the class prefix alone. One that names a path the
    parser cannot resolve to literals is unreadable, and the endpoint is
    dropped rather than documented under an invented route.
    """
    value = _attribute_value(annotation, source, _PATH_ATTRIBUTES)
    if value is None:
        value = _positional_value(annotation)
    if value is None:
        return [""], True
    if value.type == "element_value_array_initializer":
        paths = []
        for element in _array_elements(value):
            literal = _string_literal_value(element, source)
            if literal is None:
                return [], False
            paths.append(literal)
        return (paths, True) if paths else ([""], True)
    literal = _string_literal_value(value, source)
    if literal is None:
        return [], False
    return [literal], True


def _request_method_name(node: Node, source: bytes) -> Optional[str]:
    """``RequestMethod.PUT`` and a statically imported ``PUT`` both read as PUT."""
    if node.type == "field_access":
        name = _simple_name(node.child_by_field_name("field"), source)
    elif node.type in {"identifier", "field_expression", "scoped_identifier"}:
        name = _simple_name(node, source)
    else:
        return None
    name = name.upper()
    return name if name in HTTP_METHODS else None


def _request_mapping_verbs(annotation: Node, source: bytes) -> Tuple[List[str], bool]:
    """The verbs a @RequestMapping declares, and whether they were readable.

    A @RequestMapping with no method attribute serves every verb in Spring.
    OpenAPI would take one operation per verb, all of them invented from a
    single handler, so only GET is emitted and the assumption is counted.
    """
    value = _attribute_value(annotation, source, ("method",))
    if value is None:
        _EXTRACTION_DROPS["assumed_get"] += 1
        return ["GET"], True
    nodes = (
        _array_elements(value)
        if value.type == "element_value_array_initializer"
        else [value]
    )
    verbs = []
    for node in nodes:
        verb = _request_method_name(node, source)
        if verb is None:
            return [], False
        verbs.append(verb)
    return (verbs, True) if verbs else ([], False)


def _mapping_annotation(declaration: Node, source: bytes) -> Tuple[Optional[Node], str]:
    """The mapping annotation a method carries, and its name."""
    for annotation in _annotations(declaration):
        name = _annotation_name(annotation, source)
        if name in VERB_ANNOTATIONS or name == REQUEST_MAPPING:
            return annotation, name
    return None, ""


def _join_paths(prefix: str, path: str) -> str:
    """Class prefix plus method path, with no doubled or missing separators."""
    left = (prefix or "").strip()
    right = (path or "").strip()
    if left and not left.startswith("/"):
        left = f"/{left}"
    if right and not right.startswith("/"):
        right = f"/{right}"
    joined = f"{left.rstrip('/')}{right}"
    while "//" in joined:
        joined = joined.replace("//", "/")
    return joined or "/"


def _class_prefixes(declaration: Node, source: bytes) -> Tuple[List[str], bool]:
    """The prefixes a controller's own @RequestMapping contributes.

    Several of them fan the whole controller out, one endpoint per prefix.
    """
    annotation = _annotation_by_name(declaration, source, REQUEST_MAPPING)
    if annotation is None:
        return [""], True
    return _annotation_paths(annotation, source)


def _class_methods(declaration: Node) -> List[Node]:
    """The methods declared directly on this class.

    A nested class is a class of its own: its methods are endpoints only when
    it carries a controller annotation itself.
    """
    body = declaration.child_by_field_name("body")
    if body is None:
        return []
    return [child for child in body.children if child.type == "method_declaration"]


def _build_endpoint_entry(
    route: str, http_method: str, name: str, declaration: Node, file_path: Path
) -> Dict:
    return {
        "type": "function",
        "method": http_method,
        "route": route,
        "name": name,
        # The declaration node starts at its first annotation, so the span the
        # prompt reads carries @GetMapping, @PathVariable and the body alike.
        "start_line": declaration.start_point[0] + 1,
        "end_line": declaration.end_point[0] + 1,
        "file_path": str(file_path),
    }


def _endpoints_of_class(
    declaration: Node, source: bytes, file_path: Path
) -> List[Dict]:
    prefixes, readable = _class_prefixes(declaration, source)
    if not readable:
        _EXTRACTION_DROPS["unresolved"] += 1
        return []

    endpoints: List[Dict] = []
    for method in _class_methods(declaration):
        annotation, annotation_name = _mapping_annotation(method, source)
        if annotation is None:
            continue
        paths, path_readable = _annotation_paths(annotation, source)
        if not path_readable:
            _EXTRACTION_DROPS["unresolved"] += 1
            continue
        if annotation_name == REQUEST_MAPPING:
            verbs, verbs_readable = _request_mapping_verbs(annotation, source)
            if not verbs_readable:
                _EXTRACTION_DROPS["unresolved"] += 1
                continue
        else:
            verbs = [VERB_ANNOTATIONS[annotation_name]]
        name_node = method.child_by_field_name("name")
        name = _node_text(source, name_node) if name_node else "<anonymous>"
        for prefix in prefixes:
            for path in paths:
                route = _join_paths(prefix, path)
                for verb in verbs:
                    endpoints.append(
                        _build_endpoint_entry(route, verb, name, method, file_path)
                    )
    return endpoints


def find_api_endpoints(file_path, repo_root: Optional[str] = None) -> List[Dict]:
    path = Path(file_path)
    try:
        source = path.read_bytes()
    except OSError:
        return []

    tree = parser.parse(source)

    endpoints: List[Dict] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in _CLASS_TYPES and _has_controller_annotation(node, source):
            endpoints.extend(_endpoints_of_class(node, source, path))
        stack.extend(list(node.children))
    return endpoints

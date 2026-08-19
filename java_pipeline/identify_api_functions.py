from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from tree_sitter import Language, Node, Parser
import tree_sitter_java

from java_pipeline.find_api_definition_files import find_api_definition_files

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

# The attributes that narrow a mapping without changing its route. Two handlers
# on one route and verb are told apart by these.
_NEGOTIATION_ATTRIBUTES = ("consumes", "produces")
_PREDICATE_ATTRIBUTES = ("params", "headers")
_CONDITION_ATTRIBUTES = _NEGOTIATION_ATTRIBUTES + _PREDICATE_ATTRIBUTES

# Where a class or a method declaration keeps its annotations.
_MODIFIERS = "modifiers"
_ANNOTATION_TYPES = {"marker_annotation", "annotation"}

# Counted across one extraction run and reported once, so a repo that silently
# loses routes says so instead of shipping a thin spec.
_EXTRACTION_DROPS = {"assumed_get": 0, "unresolved": 0}

# Repo root -> the mapping interfaces the scan set declares. An implements
# clause is resolved against every scanned file, not against one of them, so
# the index is built once per run and read by every file's extraction.
_TYPE_INDEX_CACHE: Dict[str, Dict] = {}


def reset_extraction_drops() -> None:
    for key in _EXTRACTION_DROPS:
        _EXTRACTION_DROPS[key] = 0


def reset_type_index() -> None:
    """The same checkout path can hold different java sources on the next run."""
    _TYPE_INDEX_CACHE.clear()


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
        f"{unresolved} mappings it could not read or place on an implementation"
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


def _string_values(value: Node, source: bytes) -> List[str]:
    """The literals one attribute lists. A constant reference contributes none."""
    nodes = (
        _array_elements(value)
        if value.type == "element_value_array_initializer"
        else [value]
    )
    values = []
    for node in nodes:
        literal = _string_literal_value(node, source)
        if literal is not None:
            values.append(literal)
    return values


def _mapping_conditions(annotation: Node, source: bytes) -> Dict[str, List[str]]:
    """What narrows a mapping without changing its route.

    consumes, produces, params and headers pick one handler among several on
    the same route and verb, and they are what the operation has to say about
    the media types and the parameters it answers on.
    """
    conditions: Dict[str, List[str]] = {}
    for name in _CONDITION_ATTRIBUTES:
        value = _attribute_value(annotation, source, (name,))
        if value is None:
            continue
        values = _string_values(value, source)
        if values:
            conditions[name] = values
    return conditions


def _compose_conditions(
    class_conditions: Dict[str, List[str]], method_conditions: Dict[str, List[str]]
) -> Dict[str, List[str]]:
    """Class level and method level conditions, the way Spring reads them.

    A method's consumes or produces replaces the class's; its params and
    headers are added to them.
    """
    composed: Dict[str, List[str]] = {}
    for name in _NEGOTIATION_ATTRIBUTES:
        values = method_conditions.get(name) or class_conditions.get(name)
        if values:
            composed[name] = list(values)
    for name in _PREDICATE_ATTRIBUTES:
        values = list(class_conditions.get(name) or [])
        for value in method_conditions.get(name) or []:
            if value not in values:
                values.append(value)
        if values:
            composed[name] = values
    return composed


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


def _class_mapping(declaration: Node, source: bytes) -> Optional[Dict]:
    """A class or interface level @RequestMapping, or None when there is none.

    Several prefixes fan the whole controller out, one endpoint per prefix.
    """
    annotation = _annotation_by_name(declaration, source, REQUEST_MAPPING)
    if annotation is None:
        return None
    prefixes, readable = _annotation_paths(annotation, source)
    return {
        "prefixes": prefixes,
        "conditions": _mapping_conditions(annotation, source),
        "readable": readable,
    }


def _method_mapping(annotation: Node, annotation_name: str, source: bytes) -> Dict:
    """The routes, verbs and conditions one method level mapping declares."""
    paths, readable = _annotation_paths(annotation, source)
    verbs: List[str] = []
    if annotation_name != REQUEST_MAPPING:
        verbs = [VERB_ANNOTATIONS[annotation_name]]
    elif readable:
        # Only asked once the paths are readable, so an endpoint that is
        # dropped anyway is not also counted as an assumed GET.
        verbs, readable = _request_mapping_verbs(annotation, source)
    return {
        "paths": paths,
        "verbs": verbs,
        "conditions": _mapping_conditions(annotation, source),
        "readable": readable,
    }


def _class_methods(declaration: Node) -> List[Node]:
    """The methods declared directly on this class.

    A nested class is a class of its own: its methods are endpoints only when
    it carries a controller annotation itself.
    """
    body = declaration.child_by_field_name("body")
    if body is None:
        return []
    return [child for child in body.children if child.type == "method_declaration"]


def _method_arity(method: Node) -> int:
    parameters = method.child_by_field_name("parameters")
    if parameters is None:
        return 0
    return sum(
        1
        for child in parameters.children
        if child.type in {"formal_parameter", "spread_parameter"}
    )


def _declared_name(declaration: Node, source: bytes) -> str:
    name_node = declaration.child_by_field_name("name")
    return _node_text(source, name_node) if name_node else "<anonymous>"


def _method_key(method: Node, source: bytes) -> Tuple[str, int]:
    """What matches an override to the interface method it implements."""
    return _declared_name(method, source), _method_arity(method)


def _build_endpoint_entry(
    route: str,
    http_method: str,
    name: str,
    declaration: Node,
    file_path: Path,
    conditions: Dict[str, List[str]],
) -> Dict:
    entry = {
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
    if conditions:
        entry["conditions"] = conditions
    return entry


def _endpoints_of_class(
    declaration: Node, source: bytes, file_path: Path, inherited: Dict
) -> List[Dict]:
    # An API first controller declares nothing itself: the interface it
    # implements carries the prefix and the mappings, and the class holds the
    # body worth documenting, so the implementation is what is emitted.
    class_mapping = _class_mapping(declaration, source) or inherited.get("class_mapping")
    if class_mapping and not class_mapping["readable"]:
        _EXTRACTION_DROPS["unresolved"] += 1
        return []
    prefixes = class_mapping["prefixes"] if class_mapping else [""]
    class_conditions = class_mapping["conditions"] if class_mapping else {}
    inherited_methods = inherited.get("methods") or {}

    endpoints: List[Dict] = []
    for method in _class_methods(declaration):
        annotation, annotation_name = _mapping_annotation(method, source)
        if annotation is not None:
            # A method that declares its own mapping overrides the interface's.
            mapping = _method_mapping(annotation, annotation_name, source)
        else:
            mapping = inherited_methods.get(_method_key(method, source))
            if mapping is None:
                continue
        if not mapping["readable"]:
            _EXTRACTION_DROPS["unresolved"] += 1
            continue
        conditions = _compose_conditions(class_conditions, mapping["conditions"])
        name = _declared_name(method, source)
        for prefix in prefixes:
            for path in mapping["paths"]:
                route = _join_paths(prefix, path)
                for verb in mapping["verbs"]:
                    endpoints.append(
                        _build_endpoint_entry(
                            route, verb, name, method, file_path, conditions
                        )
                    )
    return endpoints


def _package_name(root: Node, source: bytes) -> str:
    for child in root.children:
        if child.type != "package_declaration":
            continue
        text = _node_text(source, child).strip().rstrip(";").strip()
        return text[len("package") :].strip()
    return ""


def _imported_types(root: Node, source: bytes) -> Dict[str, str]:
    """Simple type name -> the qualified name this file imported it under."""
    types: Dict[str, str] = {}
    for child in root.children:
        if child.type != "import_declaration":
            continue
        text = _node_text(source, child).strip().rstrip(";").strip()
        text = text[len("import") :].strip()
        if text.startswith("static") or text.endswith(".*"):
            continue
        types[text.split(".")[-1]] = text
    return types


def _implemented_names(declaration: Node, source: bytes) -> List[str]:
    """The interfaces an implements clause names, generic arguments stripped."""
    interfaces = declaration.child_by_field_name("interfaces")
    if interfaces is None:
        return []
    names: List[str] = []
    for child in interfaces.children:
        if child.type != "type_list":
            continue
        for entry in child.children:
            if entry.is_named:
                names.append(_node_text(source, entry).split("<")[0].strip())
    return names


def _interface_entry(
    declaration: Node, source: bytes, package: str, file_path: Path
) -> Optional[Dict]:
    """One scanned interface's mappings, or None when it declares none."""
    class_mapping = _class_mapping(declaration, source)
    methods: Dict[Tuple[str, int], Dict] = {}
    for method in _class_methods(declaration):
        annotation, annotation_name = _mapping_annotation(method, source)
        if annotation is None:
            continue
        methods[_method_key(method, source)] = _method_mapping(
            annotation, annotation_name, source
        )
    if class_mapping is None and not methods:
        return None
    name = _declared_name(declaration, source)
    return {
        "name": name,
        "package": package,
        "fqn": f"{package}.{name}" if package else name,
        "file_path": str(file_path),
        "class_mapping": class_mapping,
        "methods": methods,
        "implemented": False,
    }


def _index_file_types(file_path: str, index: Dict) -> None:
    """This file's mapping interfaces, and the interfaces its controllers claim."""
    path = Path(file_path)
    try:
        source = path.read_bytes()
    except OSError:
        return
    root = parser.parse(source).root_node
    package = _package_name(root, source)
    imported = _imported_types(root, source)
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "interface_declaration":
            entry = _interface_entry(node, source, package, path)
            if entry is not None:
                index["by_fqn"][entry["fqn"]] = entry
                index["by_simple"].setdefault(entry["name"], []).append(entry)
        elif node.type == "class_declaration" and _has_controller_annotation(
            node, source
        ):
            index["implements"].append(
                (package, imported, _implemented_names(node, source))
            )
        stack.extend(list(node.children))


def _resolve_interface(
    name: str, package: str, imported: Dict[str, str], index: Dict
) -> Optional[Dict]:
    """The scanned interface an implements clause names.

    Resolution reads the package the way import resolution does: a qualified
    name is matched whole, a simple one through the file's imports, then its
    own package, and only then by simple name when the scan set holds exactly
    one interface with it.
    """
    if "." in name:
        return index["by_fqn"].get(name)
    qualified = imported.get(name)
    if qualified:
        return index["by_fqn"].get(qualified)
    candidates = index["by_simple"].get(name) or []
    for candidate in candidates:
        if candidate["package"] == package:
            return candidate
    return candidates[0] if len(candidates) == 1 else None


def index_scanned_types(files: Iterable[str], repo_root: str) -> Dict:
    """The mapping interfaces of the scan set, and which of them are implemented.

    An interface nobody in the scan set implements has no body to document, so
    it emits nothing and every mapping it declares is counted as dropped.
    """
    index: Dict = {"by_fqn": {}, "by_simple": {}, "implements": []}
    for file_path in files:
        _index_file_types(file_path, index)
    for package, imported, names in index["implements"]:
        for name in names:
            entry = _resolve_interface(name, package, imported, index)
            if entry is not None:
                entry["implemented"] = True
    for entry in index["by_fqn"].values():
        if not entry["implemented"]:
            _EXTRACTION_DROPS["unresolved"] += len(entry["methods"])
    _TYPE_INDEX_CACHE[repo_root] = index
    return index


def _type_index(repo_root: Optional[str]) -> Dict:
    """The scan set's interfaces, built on first use and kept for the run."""
    if not repo_root:
        return {"by_fqn": {}, "by_simple": {}, "implements": []}
    cached = _TYPE_INDEX_CACHE.get(repo_root)
    if cached is not None:
        return cached
    return index_scanned_types(find_api_definition_files(repo_root), repo_root)


def _inherited_mappings(
    declaration: Node,
    source: bytes,
    package: str,
    imported: Dict[str, str],
    index: Dict,
) -> Dict:
    """The mappings this controller inherits from the interfaces it implements.

    The first interface that declares a prefix contributes it, and the first
    that declares a mapping for a method signature contributes that.
    """
    class_mapping: Optional[Dict] = None
    methods: Dict[Tuple[str, int], Dict] = {}
    for name in _implemented_names(declaration, source):
        entry = _resolve_interface(name, package, imported, index)
        if entry is None:
            continue
        if class_mapping is None:
            class_mapping = entry["class_mapping"]
        for key, mapping in entry["methods"].items():
            methods.setdefault(key, mapping)
    return {"class_mapping": class_mapping, "methods": methods}


def find_api_endpoints(file_path, repo_root: Optional[str] = None) -> List[Dict]:
    path = Path(file_path)
    try:
        source = path.read_bytes()
    except OSError:
        return []

    tree = parser.parse(source)
    root = tree.root_node
    index = _type_index(repo_root)
    package = _package_name(root, source)
    imported = _imported_types(root, source)

    endpoints: List[Dict] = []
    stack = [root]
    while stack:
        node = stack.pop()
        # Only a class is emitted: an interface holds mappings for whatever
        # implements it, and emitting both would document the route twice.
        if node.type == "class_declaration" and _has_controller_annotation(node, source):
            inherited = _inherited_mappings(node, source, package, imported, index)
            endpoints.extend(_endpoints_of_class(node, source, path, inherited))
        stack.extend(list(node.children))
    return endpoints

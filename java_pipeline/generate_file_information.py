import os
from typing import Dict, List, Optional, Tuple

from tree_sitter import Language, Parser
import tree_sitter_java

from config import Configurations

config = Configurations()

JAVA_LANGUAGE = Language(tree_sitter_java.language())
parser = Parser(JAVA_LANGUAGE)

# repo root -> (java files by basename, the directories holding them). Java
# resolves an import by package path, so the whole scan set is what says which
# file com.acme.service.UserService is.
_SOURCE_INDEX_CACHE: Dict[str, Tuple[Dict[str, List[str]], List[str]]] = {}

# Declarations that carry a type another file can import.
_TYPE_DECLARATIONS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "class",
    "annotation_type_declaration": "interface",
}

_METHOD_DECLARATIONS = {
    "method_declaration": "function",
    "constructor_declaration": "function",
}


def parse_file(filename: str):
    # Read and parse bytes: decoding as strict utf-8 first threw away the
    # metadata of every file that is not utf-8, and tree-sitter works on bytes
    # anyway.
    with open(filename, "rb") as f:
        code = f.read()
    tree = parser.parse(code)
    return tree, code


def _node_text(source: bytes, node) -> str:
    # tree-sitter offsets are byte offsets, so slice the bytes and decode.
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def reset_source_index() -> None:
    """The same checkout path can hold different java sources on the next run."""
    _SOURCE_INDEX_CACHE.clear()


def _source_index(base_directory: str) -> Tuple[Dict[str, List[str]], List[str]]:
    """Every java file below the repo root, by basename, and their directories."""
    cached = _SOURCE_INDEX_CACHE.get(base_directory)
    if cached is not None:
        return cached
    by_basename: Dict[str, List[str]] = {}
    directories = set()
    for root, dirs, files in os.walk(base_directory):
        dirs[:] = [name for name in dirs if name not in config.ignored_dirs]
        for filename in files:
            if not filename.endswith(".java"):
                continue
            by_basename.setdefault(filename, []).append(os.path.join(root, filename))
            directories.add(root)
    for paths in by_basename.values():
        paths.sort()
    index = (by_basename, sorted(directories))
    _SOURCE_INDEX_CACHE[base_directory] = index
    return index


def _resolve_type_origin(segments: List[str], base_directory: Optional[str]) -> Optional[str]:
    """The file declaring com.acme.service.UserService, when the repo holds it.

    A java file sits under its package path, so the import is a path suffix:
    any scanned file ending in com/acme/service/UserService.java is the one.
    """
    if not base_directory or not segments:
        return None
    by_basename, _ = _source_index(base_directory)
    suffix = os.sep + os.path.join(*segments) + ".java"
    for candidate in by_basename.get(f"{segments[-1]}.java", []):
        if candidate.endswith(suffix):
            return os.path.normpath(candidate)
    return None


def _resolve_package_origin(segments: List[str], base_directory: Optional[str]) -> Optional[str]:
    """The directory holding com.acme.service, for a wildcard import."""
    if not base_directory or not segments:
        return None
    _, directories = _source_index(base_directory)
    suffix = os.sep + os.path.join(*segments)
    for candidate in directories:
        if candidate.endswith(suffix):
            return os.path.normpath(candidate)
    return None


def _package_class_names(origin: Optional[str]) -> List[str]:
    """The types a wildcard import can bind, read off the package directory."""
    if not origin or not os.path.isdir(origin):
        return []
    try:
        entries = sorted(os.listdir(origin))
    except OSError:
        return []
    return [name[: -len(".java")] for name in entries if name.endswith(".java")]


def _import_statement(node, source: bytes) -> Tuple[List[str], bool, bool]:
    """(name segments, is static, is wildcard) of one import declaration."""
    text = _node_text(source, node).strip().rstrip(";").strip()
    if text.startswith("import"):
        text = text[len("import") :].strip()
    is_static = text.startswith("static")
    if is_static:
        text = text[len("static") :].strip()
    is_wildcard = text.endswith(".*")
    if is_wildcard:
        text = text[: -len(".*")]
    segments = [segment.strip() for segment in text.split(".") if segment.strip()]
    return segments, is_static, is_wildcard


def _collect_imports(root, source: bytes, base_directory: Optional[str]) -> List[Dict]:
    """One entry per import, resolved to the file or package it names.

    ``imported_name`` is the simple type name, which is also how the name is
    written at every usage site, so dependency matching works the way it does
    in the other pipelines.
    """
    imports: List[Dict] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "import_declaration":
            segments, is_static, is_wildcard = _import_statement(node, source)
            if not segments:
                continue
            if is_static:
                # ``import static com.acme.Paths.USERS`` names a member of a
                # type, so the type is what resolves to a file.
                origin = _resolve_type_origin(
                    segments if is_wildcard else segments[:-1], base_directory
                )
                imported_name = segments[-1] if not is_wildcard else "*"
                names = [imported_name] if not is_wildcard else []
            elif is_wildcard:
                origin = _resolve_package_origin(segments, base_directory)
                imported_name = "*"
                names = _package_class_names(origin)
            else:
                origin = _resolve_type_origin(segments, base_directory)
                imported_name = segments[-1]
                names = [imported_name]
            imports.append(
                {
                    "type": "import",
                    "imported_name": imported_name,
                    "from_module": ".".join(segments),
                    "origin": origin,
                    "line": node.start_point[0] + 1,
                    "path_exists": bool(origin and os.path.exists(origin)),
                    "usage_lines": [],
                    "usages": [],
                    "_names": names,
                }
            )
        stack.extend(list(node.children))
    return imports


def _annotate_import_usages(root, source: bytes, imports: List[Dict]) -> None:
    """Record where each imported name is used, and which name it was.

    A wildcard import binds every type of its package, so the usage carries the
    name the handler actually reached for and the resolver looks that one up.
    """
    name_map: Dict[str, List[Dict]] = {}
    for item in imports:
        for name in item.get("_names") or []:
            name_map.setdefault(name, []).append(item)
    if not name_map:
        return
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in {"identifier", "type_identifier"}:
            entries = name_map.get(_node_text(source, node))
            if entries:
                line = node.start_point[0] + 1
                for entry in entries:
                    if line == entry["line"]:
                        continue
                    if line not in entry["usage_lines"]:
                        entry["usage_lines"].append(line)
                    usage = {"name": _node_text(source, node), "line": line}
                    if usage not in entry["usages"]:
                        entry["usages"].append(usage)
        stack.extend(list(node.children))
    for item in imports:
        item.pop("_names", None)
        item["usage_lines"].sort()


def _declarator_names(node, source: bytes) -> List[str]:
    names = []
    for child in node.children:
        if child.type != "variable_declarator":
            continue
        name_node = child.child_by_field_name("name")
        if name_node is not None:
            names.append(_node_text(source, name_node))
    return names


def _collect_elements(root, source: bytes) -> Dict:
    elements: Dict = {
        "classes": [],
        "functions": [],
        "variables": [],
        "function_calls": [],
    }
    stack = [root]
    while stack:
        node = stack.pop()
        node_type = node.type
        if node_type in _TYPE_DECLARATIONS:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                elements["classes"].append(
                    {
                        "type": _TYPE_DECLARATIONS[node_type],
                        "name": _node_text(source, name_node),
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                    }
                )
        elif node_type in _METHOD_DECLARATIONS:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                elements["functions"].append(
                    {
                        "type": _METHOD_DECLARATIONS[node_type],
                        "name": _node_text(source, name_node),
                        # The declaration node opens at its first annotation,
                        # so the span carries @GetMapping and the body alike.
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                    }
                )
        elif node_type == "field_declaration":
            for name in _declarator_names(node, source):
                elements["variables"].append(
                    {
                        "type": "variable",
                        "name": name,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                    }
                )
        elif node_type == "method_invocation":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                elements["function_calls"].append(
                    {
                        "type": "function_call",
                        "name": _node_text(source, name_node),
                        "full_name": _node_text(source, node).split("(")[0].strip(),
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                    }
                )
        stack.extend(list(node.children))
    return elements


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


def get_elements(tree, source: bytes, base_directory: Optional[str]):
    elements = _collect_elements(tree.root_node, source)
    _attach_call_ranges(elements["functions"], elements["function_calls"])
    imports = _collect_imports(tree.root_node, source, base_directory)
    _annotate_import_usages(tree.root_node, source, imports)
    return elements, imports


def process_file(filename: str, base_directory: Optional[str] = None) -> Dict:
    if not base_directory:
        base_directory = os.path.dirname(filename)
    tree, source = parse_file(filename)
    elements, imports = get_elements(tree, source, base_directory)
    for key in ("classes", "functions", "variables"):
        for item in elements.get(key, []):
            item["file_path"] = filename
    return {"filename": filename, "elements": elements, "imports": imports}

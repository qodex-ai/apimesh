import os
from typing import Dict, List, Optional

from tree_sitter import Language, Parser
import tree_sitter_ruby

from config import Configurations

config = Configurations()

RUBY_LANGUAGE = Language(tree_sitter_ruby.language())
parser = Parser(RUBY_LANGUAGE)


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


def _gather_included_modules(node, source: str) -> List[List[str]]:
    """
    The modules a class or module body includes, one group per include
    statement and in source order. Ruby looks a method up in the included
    modules before the superclass, so an action can live in a concern, and the
    ancestor order it builds depends on both which statement an argument came
    from and where it sat in that statement; a flat list loses both. Only the
    body's own statements count, never a nested class's.
    """
    body_node = node.child_by_field_name("body")
    if body_node is None:
        return []

    groups: List[List[str]] = []
    for child in body_node.children:
        if child.type not in {"call", "command", "command_call"}:
            continue
        method_node = child.child_by_field_name("method")
        if method_node is None or _node_text(source, method_node).strip() != "include":
            continue
        arguments_node = child.child_by_field_name("arguments")
        if arguments_node is None:
            continue
        # `include Trackable, Auditable` includes both, Trackable first.
        names = [
            _node_text(source, argument).strip()
            for argument in arguments_node.children
            if argument.type in {"constant", "scope_resolution"}
        ]
        if names:
            groups.append(names)
    return groups


def _gather_class_info(node, source: str) -> Dict:
    name_node = node.child_by_field_name("name")
    name = _node_text(source, name_node) if name_node else "<anonymous>"
    superclass_node = node.child_by_field_name("superclass")
    superclass = None
    if superclass_node:
        superclass = _node_text(source, superclass_node).strip()
        if superclass:
            superclass = superclass.lstrip("<").strip()
    return {
        "type": "class",
        "name": name,
        "start_line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
        "superclass": superclass,
        "includes": _gather_included_modules(node, source),
    }


def _gather_module_info(node, source: str) -> Dict:
    name_node = node.child_by_field_name("name")
    name = _node_text(source, name_node) if name_node else "<anonymous>"
    return {
        "type": "module",
        "name": name,
        "start_line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
        # A concern includes concerns of its own, and those sit in the ancestry
        # of every class that includes it.
        "includes": _gather_included_modules(node, source),
    }


def _is_singleton_method(node) -> bool:
    """
    Whether a `def` defines a method on the class itself rather than on its
    instances. `def self.destroy` is a node type of its own; a plain `def`
    written inside `class << self` is an ordinary method node that happens to
    sit in a singleton scope, and only its ancestors say so.
    """
    if node.type == "singleton_method":
        return True
    parent = node.parent
    while parent is not None:
        if parent.type == "singleton_class":
            return True
        if parent.type in {"class", "module"} and parent.is_named:
            return False
        parent = parent.parent
    return False


def _gather_method_info(node, source: str) -> Dict:
    name_node = node.child_by_field_name("name")
    name = _node_text(source, name_node) if name_node else "<anonymous>"
    return {
        "type": "function",
        "name": name,
        "start_line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
        # Rails routes to instance methods only, so the kind is what keeps a
        # class method out of the action maps while the metadata still has it.
        "kind": "singleton" if _is_singleton_method(node) else "instance",
    }


def _gather_call_info(node, source: str) -> Dict:
    name_node = node.child_by_field_name("method")
    if not name_node:
        name_node = node.child_by_field_name("name")
    name = _node_text(source, name_node) if name_node else "<anonymous>"
    call_info = {
        "type": "function_call",
        "name": name,
        "start_line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
    }

    definition_range = _infer_definition_range(node, source)
    if definition_range:
        call_info.update(definition_range)
    return call_info


def _infer_definition_range(node, source: str) -> Optional[Dict]:
    """
    Attempt to infer the definition range for an inline function call by
    locating the matching method definition within the same source buffer.
    """
    name_node = node.child_by_field_name("method")
    if not name_node:
        name_node = node.child_by_field_name("name")
    if not name_node:
        return None

    name = _node_text(source, name_node)
    # This heuristic scans siblings in the same scope looking for `def name`.
    parent = node.parent
    while parent is not None:
        for sibling in parent.children:
            if sibling.type in {"method", "singleton_method"}:
                method_name_node = sibling.child_by_field_name("name")
                if method_name_node and _node_text(source, method_name_node) == name:
                    return {
                        "function_start_line": sibling.start_point[0] + 1,
                        "function_end_line": sibling.end_point[0] + 1,
                    }
        parent = parent.parent
    return None


def _gather_import_info(node, source: str, base_directory: str) -> Optional[Dict]:
    method_node = node.child_by_field_name("method")
    if not method_node:
        return None

    method_name = _node_text(source, method_node)
    if method_name not in {"require", "require_relative"}:
        return None

    arguments_node = node.child_by_field_name("arguments")
    if arguments_node is None or len(arguments_node.children) == 0:
        return None

    literal = None
    for child in arguments_node.children:
        if child.type == "string":
            content = child.child_by_field_name("content")
            if content:
                literal = _node_text(source, content)
                break
        if child.type == "symbol_literal":
            sym = child.child_by_field_name("name")
            if sym:
                literal = _node_text(source, sym)
                break

    if literal is None:
        return None

    origin = _resolve_required_path(
        literal, base_directory, method_name == "require_relative"
    )

    return {
        "type": "import",
        "imported_name": literal,
        "from_module": literal,
        "origin": origin,
        "line": node.start_point[0] + 1,
        "path_exists": origin is not None and os.path.exists(origin),
        "usage_lines": [],
    }


def _resolve_required_path(
    literal: str, base_directory: str, is_relative: bool
) -> Optional[str]:
    if is_relative:
        candidate = os.path.normpath(os.path.join(base_directory, f"{literal}.rb"))
        if os.path.exists(candidate):
            return candidate
    else:
        candidate = os.path.join(base_directory, f"{literal}.rb")
        if os.path.exists(candidate):
            return os.path.normpath(candidate)
    return None


def get_elements(tree, source: str, base_directory: str) -> Dict:
    elements = {
        "classes": [],
        "modules": [],
        "functions": [],
        "function_calls": [],
    }
    imports: List[Dict] = []

    cursor = [tree.root_node]
    while cursor:
        node = cursor.pop()
        node_type = node.type
        # The `class` and `module` keywords are tokens of the same type as the
        # definition they open, so an unnamed match is the bare keyword and
        # collecting it yielded a phantom anonymous entry per real definition.
        if node_type == "class" and node.is_named:
            elements["classes"].append(_gather_class_info(node, source))
        elif node_type == "module" and node.is_named:
            elements["modules"].append(_gather_module_info(node, source))
        elif node_type in {"method", "singleton_method"}:
            elements["functions"].append(_gather_method_info(node, source))
        elif node_type in {"call", "command", "command_call"}:
            elements["function_calls"].append(_gather_call_info(node, source))

            import_info = _gather_import_info(node, source, base_directory)
            if import_info:
                imports.append(import_info)

        cursor.extend(list(node.children))

    return elements, imports


def process_file(filename: str, base_directory: Optional[str] = None) -> Dict:
    if not base_directory:
        base_directory = os.path.dirname(filename)

    tree, code = parse_file(filename)
    elements, imports = get_elements(tree, code, base_directory)
    return {"filename": filename, "elements": elements, "imports": imports}

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from tree_sitter import Language, Node, Parser
import tree_sitter_ruby

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

# Hash keys a verb call carries as options, so anything else keyed to a
# "controller#action" value is the route path itself.
ROUTE_OPTION_KEYS = {
    "to",
    "via",
    "as",
    "on",
    "constraints",
    "defaults",
    "format",
    "path",
    "module",
    "only",
    "except",
    "controller",
    "action",
    "shallow",
    "concerns",
    "param",
    "path_names",
    "anchor",
}

RUBY_LANGUAGE = Language(tree_sitter_ruby.language())
parser = Parser(RUBY_LANGUAGE)


def is_route_file(file_path) -> bool:
    """
    config/routes.rb plus the split route files Rails' `draw(:name)` loads out
    of config/routes/.
    """
    parts = Path(file_path).as_posix().split("/")
    if len(parts) >= 2 and parts[-2] == "config" and parts[-1] == "routes.rb":
        return True
    return (
        len(parts) >= 3
        and parts[-3] == "config"
        and parts[-2] == "routes"
        and parts[-1].endswith(".rb")
    )


def _iter_block_children(block_node: Optional[Node]):
    """
    Yield the meaningful statement nodes contained within a Ruby block node.
    """
    if block_node is None:
        return
    if block_node.type in {"do_block", "block"}:
        for child in block_node.children:
            if child.type in {"do", "end"}:
                continue
            if child.type in {"body_statement", "statements"}:
                for statement in child.children:
                    if statement.type in {";", "END"}:
                        continue
                    yield statement
            else:
                yield child
    else:
        for child in block_node.children:
            yield child


@dataclass
class ResourceEntry:
    name: str
    shallow: bool = False


@dataclass
class RouteContext:
    path_prefix: str = ""
    controller_prefix: str = ""
    resource_stack: List[ResourceEntry] = field(default_factory=list)
    # shallow: true is inherited by every resource nested below it.
    shallow: bool = False

    def with_namespace(self, namespace: str) -> "RouteContext":
        new_prefix = _join_paths(self.path_prefix, namespace)
        new_controller = _join_controllers(self.controller_prefix, namespace)
        return RouteContext(
            new_prefix, new_controller, list(self.resource_stack), self.shallow
        )

    def with_scope(self, path: Optional[str], module: Optional[str]) -> "RouteContext":
        new_prefix = self.path_prefix
        new_controller = self.controller_prefix
        if path:
            new_prefix = _join_paths(new_prefix, path)
        if module:
            new_controller = _join_controllers(new_controller, module)
        return RouteContext(
            new_prefix, new_controller, list(self.resource_stack), self.shallow
        )


def find_api_endpoints(
    file_path: Path,
    repo_root: str,
    route_map: Dict[str, List[Dict]],
    class_index: Optional[Dict[str, Dict]] = None,
    dropped_routes: Optional[List[str]] = None,
) -> List[Dict]:
    if is_route_file(file_path):
        _update_route_map(route_map, file_path)
        return []
    return _extract_controller_endpoints(
        file_path, repo_root, route_map, class_index, dropped_routes
    )


def _update_route_map(
    route_map: Dict[str, List[Dict]], routes_file: Path
) -> None:
    try:
        source = routes_file.read_bytes()
    except OSError:
        return

    tree = parser.parse(source)
    context = RouteContext()
    routes: List[Dict] = []
    concerns: Dict[str, Node] = {}

    _walk_routes(tree.root_node, source, context, routes, concerns)

    grouped: Dict[str, List[Dict]] = {}
    for route in routes:
        controller = route.get("controller")
        if not controller:
            continue
        grouped.setdefault(controller, []).append(route)

    for controller, entries in grouped.items():
        route_map.setdefault(controller, []).extend(entries)


def _walk_routes(
    node: Node,
    source: str,
    context: RouteContext,
    routes: List[Dict],
    concerns: Dict[str, Node],
):
    if node.type == "call":
        block_node: Optional[Node] = None
        for child in node.children:
            if child.type == "do_block":
                block_node = child
                break
        _handle_command(node, block_node, source, context, routes, concerns)
        return

    if node.type == "method_add_block":
        call_node = node.child_by_field_name("call")
        block_node = node.child_by_field_name("block")
        if call_node:
            _handle_command(call_node, block_node, source, context, routes, concerns)
        return

    if node.type in {"command", "command_call"}:
        _handle_command(node, None, source, context, routes, concerns)
        return

    for child in node.children:
        _walk_routes(child, source, context, routes, concerns)


def _handle_command(
    call_node: Node,
    block_node: Optional[Node],
    source: str,
    context: RouteContext,
    routes: List[Dict],
    concerns: Dict[str, Node],
):
    method_node = call_node.child_by_field_name("method")
    if not method_node:
        return
    method_name = _node_text(source, method_node).strip()
    method_lower = method_name.lower()

    args = _extract_arguments(call_node, source)
    handled = False

    if method_lower == "concern":
        # A concern only defines routes, it never emits them at the definition
        # site: they are replayed inside every resource that asks for it.
        concern_name = _first_symbol_or_string(args)
        if concern_name and block_node is not None:
            concerns[concern_name] = block_node
        return

    if method_lower == "namespace":
        namespace = _first_symbol_or_string(args)
        if not namespace:
            return
        inner_context = context.with_namespace(namespace)
        if block_node:
            _walk_routes(block_node, source, inner_context, routes, concerns)
        handled = True
        return

    if method_lower == "scope":
        options = _collect_hash_options(args)
        # scope '/public' and scope :v2 carry the path as a positional argument.
        scope_path = options.get("path") or _first_symbol_or_string(args)
        scoped_context = context.with_scope(
            scope_path, options.get("module")
        )
        if block_node:
            _walk_routes(block_node, source, scoped_context, routes, concerns)
        handled = True
        return

    if method_lower in {"resources", "resource"}:
        resource_names = _collect_resource_names(args)
        if not resource_names:
            return
        option_args = args[len(resource_names) :]
        resource_options = _extract_hash_arguments(option_args)
        # shallow: true is inherited from the enclosing resource too.
        shallow = _is_truthy(resource_options.get("shallow")) or context.shallow
        only_actions = _normalize_action_list(resource_options.get("only"))
        except_actions = _normalize_action_list(resource_options.get("except"))
        controller_option = resource_options.get("controller")
        concern_names = _normalize_action_list(resource_options.get("concerns")) or []
        plural = method_lower == "resources"

        for idx, resource_name in enumerate(resource_names):
            resource_entry = ResourceEntry(
                name=resource_name,
                shallow=shallow,
            )
            new_context = RouteContext(
                path_prefix=context.path_prefix,
                controller_prefix=context.controller_prefix,
                resource_stack=list(context.resource_stack) + [resource_entry],
                shallow=shallow,
            )
            controller_key = _resource_controller_key(
                context, resource_name, controller_option
            )
            _append_restful_routes(
                routes,
                new_context,
                controller_key,
                plural,
                only_actions=only_actions,
                except_actions=except_actions,
            )
            _replay_concerns(
                concern_names,
                source,
                new_context,
                controller_key,
                routes,
                plural,
                concerns,
                resource_entry.shallow,
            )
            if block_node and idx == 0:
                _walk_resource_block(
                    block_node,
                    source,
                    new_context,
                    controller_key,
                    routes,
                    plural,
                    concerns,
                    shallow=resource_entry.shallow,
                )
        handled = True
        return

    if method_lower == "root":
        target = _extract_option(args, "to")
        if not target:
            # root 'home#index' carries the target as a positional argument.
            candidate = _first_string(args) or _first_symbol(args)
            if candidate and "#" in candidate:
                target = candidate
        controller, action = _split_controller_action(target)
        if not controller or not action:
            return
        routes.append(
            {
                "verb": "GET",
                # A root inside a namespace answers on the namespace's path and
                # is served by the namespaced controller.
                "path": _join_paths(context.path_prefix, ""),
                "controller": _join_controllers(context.controller_prefix, controller),
                "action": action,
            }
        )
        handled = True
        return

    if method_lower == "match":
        path = _first_string(args) or _first_symbol(args)
        target = _extract_option(args, "to")
        via = _extract_option(args, "via")
        verbs = _normalize_via(via)
        controller, action = _split_controller_action(target)
        if not path or not controller or not action:
            return
        for verb in verbs:
            routes.append(
                {
                    "verb": verb,
                    "path": _join_paths(context.path_prefix, path),
                    "controller": _join_controllers(context.controller_prefix, controller),
                    "action": action,
                }
            )
        handled = True
        return

    if method_lower in HTTP_METHODS:
        path = _first_string(args) or _first_symbol(args)
        target = _extract_option(args, "to")
        controller, action = _split_controller_action(target)
        if not path or not controller or not action:
            hash_path, hash_target = _extract_path_target_from_hash(args)
            if not path and hash_path:
                path = hash_path
            if hash_target and (not controller or not action):
                controller, action = _split_controller_action(hash_target)
        if not path or not controller or not action:
            return
        routes.append(
            {
                "verb": method_lower.upper(),
                "path": _join_paths(context.path_prefix, path),
                "controller": _join_controllers(context.controller_prefix, controller),
                "action": action,
            }
        )
        handled = True
        return

    if block_node and not handled:
        _walk_routes(block_node, source, context, routes, concerns)


def _replay_concerns(
    concern_names: List[str],
    source: str,
    context: RouteContext,
    controller_key: str,
    routes: List[Dict],
    plural: bool,
    concerns: Dict[str, Node],
    shallow: bool,
):
    """
    Emit the routes of every named concern inside the resource asking for it,
    which is where Rails puts them. Reached from both spellings: the
    `concerns: :commentable` option and the `concerns :commentable` call.
    """
    for concern_name in concern_names:
        concern_block = concerns.get(concern_name)
        if concern_block is None:
            continue
        _walk_resource_block(
            concern_block,
            source,
            context,
            controller_key,
            routes,
            plural,
            concerns,
            shallow=shallow,
        )


def _collect_concern_names(args: List[Dict]) -> List[str]:
    """`concerns :commentable` and `concerns [:commentable, :taggable]`."""
    names: List[str] = []
    for arg in args:
        if arg["type"] in {"symbol", "string"}:
            names.append(arg["value"])
        elif arg["type"] == "array":
            names.extend(item for item in arg["value"] if isinstance(item, str))
    return names


def _walk_resource_block(
    block_node: Node,
    source: str,
    context: RouteContext,
    controller_key: str,
    routes: List[Dict],
    plural: bool,
    concerns: Dict[str, Node],
    shallow: bool = False,
):
    for child in _iter_block_children(block_node):
        if child.type == "method_add_block":
            call_node = child.child_by_field_name("call")
            inner_block = child.child_by_field_name("block")
            if not call_node:
                continue
            method_node = call_node.child_by_field_name("method")
            if not method_node:
                continue
            method_name = _node_text(source, method_node).strip()
            method_lower = method_name.lower()

            if method_lower in {"member", "collection"} and inner_block:
                _handle_member_collection(
                    method_lower,
                    inner_block,
                    source,
                    context,
                    controller_key,
                    routes,
                    shallow,
                    plural,
                )
            elif method_lower in {"resources", "resource"}:
                # Nested resources inherit the existing context stack.
                _handle_command(
                    call_node,
                    inner_block,
                    source,
                    context,
                    routes,
                    concerns,
                )
        elif child.type in {"command", "command_call", "call"}:
            block = None
            if child.type == "call":
                for grandchild in child.children:
                    if grandchild.type == "do_block":
                        block = grandchild
                        break
            method_node = child.child_by_field_name("method")
            method_lower = (
                _node_text(source, method_node).strip().lower()
                if method_node
                else ""
            )
            if method_lower in {"member", "collection"} and block:
                _handle_member_collection(
                    method_lower,
                    block,
                    source,
                    context,
                    controller_key,
                    routes,
                    shallow,
                    plural,
                )
                continue
            if method_lower == "concerns":
                _replay_concerns(
                    _collect_concern_names(_extract_arguments(child, source)),
                    source,
                    context,
                    controller_key,
                    routes,
                    plural,
                    concerns,
                    shallow,
                )
                continue
            # A verb written straight inside the resource block hangs off the
            # nested scope, where Rails names the parent id :<singular>_id.
            base_path = (
                _resource_nested_path(context, shallow=shallow)
                if plural
                else _resource_collection_path(context)
            )
            if _emit_scoped_route(
                child, source, context, controller_key, base_path, routes
            ):
                continue
            _handle_command(child, block, source, context, routes, concerns)


def _handle_member_collection(
    scope_type: str,
    block_node: Node,
    source: str,
    context: RouteContext,
    controller_key: str,
    routes: List[Dict],
    shallow: bool = False,
    plural: bool = True,
):
    collection_path = _resource_collection_path(context)
    member_path = (
        _resource_member_path(context, shallow=shallow) if plural else collection_path
    )
    base_path = member_path if scope_type == "member" and plural else collection_path

    for child in _iter_block_children(block_node):
        if child.type in {"command", "command_call", "call"}:
            _emit_scoped_route(
                child, source, context, controller_key, base_path, routes
            )


def _emit_scoped_route(
    call_node: Node,
    source: str,
    context: RouteContext,
    controller_key: str,
    base_path: str,
    routes: List[Dict],
) -> bool:
    """
    One verb call written inside a resource scope. The path segment hangs off
    base_path and the enclosing resource owns the action unless `to:` names
    another controller. Returns False when the call is not such a route.
    """
    method_node = call_node.child_by_field_name("method")
    if not method_node:
        return False
    method_lower = _node_text(source, method_node).strip().lower()
    if method_lower not in HTTP_METHODS:
        return False

    args = _extract_arguments(call_node, source)
    path_segment = _first_symbol_or_string(args)
    if not path_segment:
        return False

    controller_name = controller_key
    action_name = path_segment
    target = _extract_option(args, "to")
    if target:
        target_controller, target_action = _split_controller_action(target)
        if target_controller:
            normalized = target_controller.replace("::", "/").strip("/")
            prefix = context.controller_prefix.strip("/")
            if prefix and normalized.startswith(prefix):
                controller_name = normalized
            else:
                controller_name = _join_controllers(
                    context.controller_prefix, normalized
                )
        if target_action:
            action_name = target_action

    routes.append(
        {
            "verb": method_lower.upper(),
            "path": _join_paths(base_path, path_segment),
            "controller": controller_name,
            "action": action_name,
        }
    )
    return True


def _append_restful_routes(
    routes: List[Dict],
    context: RouteContext,
    controller_key: str,
    plural: bool,
    only_actions: Optional[List[str]] = None,
    except_actions: Optional[List[str]] = None,
):
    collection_path = _resource_collection_path(context)

    allowed_actions = _determine_allowed_actions(
        plural, only_actions=only_actions, except_actions=except_actions
    )

    if plural:
        member_path = _resource_member_path(context)
        _append_if_allowed(
            routes,
            allowed_actions,
            "index",
            {
                "verb": "GET",
                "path": collection_path,
                "controller": controller_key,
                "action": "index",
            },
        )
        _append_if_allowed(
            routes,
            allowed_actions,
            "create",
            {
                "verb": "POST",
                "path": collection_path,
                "controller": controller_key,
                "action": "create",
            },
        )
        new_path = _join_paths(collection_path, "new")
        _append_if_allowed(
            routes,
            allowed_actions,
            "new",
            {
                "verb": "GET",
                "path": new_path,
                "controller": controller_key,
                "action": "new",
            },
        )
        _append_if_allowed(
            routes,
            allowed_actions,
            "show",
            {
                "verb": "GET",
                "path": member_path,
                "controller": controller_key,
                "action": "show",
            },
        )
        edit_path = _join_paths(member_path, "edit")
        _append_if_allowed(
            routes,
            allowed_actions,
            "edit",
            {
                "verb": "GET",
                "path": edit_path,
                "controller": controller_key,
                "action": "edit",
            },
        )
        if "update" in allowed_actions:
            routes.extend(
                [
                    {
                        "verb": "PATCH",
                        "path": member_path,
                        "controller": controller_key,
                        "action": "update",
                    },
                    {
                        "verb": "PUT",
                        "path": member_path,
                        "controller": controller_key,
                        "action": "update",
                    },
                ]
            )
        if "destroy" in allowed_actions:
            routes.append(
                {
                    "verb": "DELETE",
                    "path": member_path,
                    "controller": controller_key,
                    "action": "destroy",
                }
            )
    else:
        new_path = _join_paths(collection_path, "new")
        edit_path = _join_paths(collection_path, "edit")
        _append_if_allowed(
            routes,
            allowed_actions,
            "new",
            {
                "verb": "GET",
                "path": new_path,
                "controller": controller_key,
                "action": "new",
            },
        )
        _append_if_allowed(
            routes,
            allowed_actions,
            "create",
            {
                "verb": "POST",
                "path": collection_path,
                "controller": controller_key,
                "action": "create",
            },
        )
        _append_if_allowed(
            routes,
            allowed_actions,
            "show",
            {
                "verb": "GET",
                "path": collection_path,
                "controller": controller_key,
                "action": "show",
            },
        )
        _append_if_allowed(
            routes,
            allowed_actions,
            "edit",
            {
                "verb": "GET",
                "path": edit_path,
                "controller": controller_key,
                "action": "edit",
            },
        )
        if "update" in allowed_actions:
            routes.extend(
                [
                    {
                        "verb": "PATCH",
                        "path": collection_path,
                        "controller": controller_key,
                        "action": "update",
                    },
                    {
                        "verb": "PUT",
                        "path": collection_path,
                        "controller": controller_key,
                        "action": "update",
                    },
                ]
            )
        if "destroy" in allowed_actions:
            routes.append(
                {
                    "verb": "DELETE",
                    "path": collection_path,
                    "controller": controller_key,
                    "action": "destroy",
                }
            )


def _append_if_allowed(
    routes: List[Dict], allowed_actions: set, action_name: str, route_definition: Dict
):
    if action_name in allowed_actions:
        routes.append(route_definition)


def _determine_allowed_actions(
    plural: bool,
    only_actions: Optional[List[str]] = None,
    except_actions: Optional[List[str]] = None,
) -> set:
    if plural:
        base_actions = {"index", "create", "new", "show", "edit", "update", "destroy"}
    else:
        base_actions = {"new", "create", "show", "edit", "update", "destroy"}

    allowed = set(base_actions)
    if only_actions is not None:
        normalized_only = {str(action) for action in only_actions}
        allowed &= normalized_only
    if except_actions:
        normalized_except = {str(action) for action in except_actions}
        allowed -= normalized_except
    return allowed


def _extract_controller_endpoints(
    file_path: Path,
    repo_root: str,
    route_map: Dict[str, List[Dict]],
    class_index: Optional[Dict[str, Dict]] = None,
    dropped_routes: Optional[List[str]] = None,
) -> List[Dict]:
    controller_key = _derive_controller_key(file_path)
    if not controller_key:
        return []

    routes = route_map.get(controller_key, [])
    if not routes:
        return []

    try:
        source = file_path.read_bytes()
    except OSError:
        return []

    tree = parser.parse(source)
    class_methods, class_name = _collect_controller_methods(
        tree.root_node, source, file_path
    )

    methods_by_name = {method["name"]: method for method in class_methods}
    endpoint_methods: List[Dict] = []

    for route in routes:
        action = route["action"]
        method_info = methods_by_name.get(action)
        if method_info:
            method_copy = method_info.copy()
        else:
            # The action can live in a parent controller; anything else is a
            # route with no body to read, and a documented guess is worse than
            # a missing endpoint.
            inherited = _inherited_method_info(
                class_index, class_name, action, file_path
            )
            if not inherited:
                if dropped_routes is not None:
                    dropped_routes.append(
                        f"{route['verb']} {route['path']} "
                        f"({controller_key}#{action})"
                    )
                continue
            method_copy = inherited
        method_copy["route"] = route["path"]
        method_copy["http_method"] = route["verb"]
        endpoint_methods.append(method_copy)

    if not endpoint_methods:
        return []

    return [
        {
            "type": "class",
            "name": class_name,
            "file_path": str(file_path),
            "methods": endpoint_methods,
        }
    ]


def _inherited_method_info(
    class_index: Optional[Dict[str, Dict]],
    class_name: str,
    action: str,
    file_path: Path,
) -> Optional[Dict]:
    """
    The action as a parent controller defines it. Walks the ancestor chain the
    class index tracks and returns the first parent that carries the method, so
    the parent's own body is what gets documented.
    """
    if not class_index or not class_name:
        return None

    entry = class_index.get(class_name)
    visited = set()
    while entry:
        superclass = entry.get("superclass")
        if not superclass or superclass in visited:
            return None
        visited.add(superclass)
        parent = class_index.get(superclass)
        if not parent:
            return None
        meta = (parent.get("methods") or {}).get(action)
        parent_file = parent.get("file_path")
        if meta and parent_file and isinstance(meta.get("start_line"), int):
            return {
                "type": "method",
                "name": action,
                "start_line": meta["start_line"],
                "end_line": meta["end_line"],
                "file_path": str(parent_file),
                "class_name": class_name,
                "inherited_from": superclass,
            }
        entry = parent
    return None


def _collect_controller_methods(root: Node, source: str, file_path: Path):
    """
    Returns (methods, class_name); the class name is the first class the file
    declares, which is the controller even when the file also holds an inner
    class.
    """
    methods: List[Dict] = []
    class_names: List[tuple] = []

    cursor = [root]
    current_class_name = None

    while cursor:
        node = cursor.pop()
        if node.type == "class":
            name_node = node.child_by_field_name("name")
            current_class_name = _node_text(source, name_node) if name_node else None
            if current_class_name:
                class_names.append((node.start_byte, current_class_name))
            cursor.extend(list(node.children))
            continue

        if node.type == "method":
            name_node = node.child_by_field_name("name")
            if not name_node:
                continue
            method_name = _node_text(source, name_node)
            methods.append(
                {
                    "type": "method",
                    "name": method_name,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "file_path": str(file_path),
                    "class_name": current_class_name or file_path.stem,
                }
            )
        cursor.extend(list(node.children))

    class_name = min(class_names)[1] if class_names else file_path.stem
    return methods, class_name


def _derive_controller_key(file_path: Path) -> Optional[str]:
    """
    The route map key of a controller file: whatever follows its last
    app/controllers segment, so an engine's controllers key off their own
    app/controllers instead of being skipped.
    """
    parts = file_path.as_posix().split("/")
    start = None
    for index in range(len(parts) - 1):
        if parts[index] == "app" and parts[index + 1] == "controllers":
            start = index + 2
    if start is None or start >= len(parts):
        return None
    without_suffix = "/".join(parts[start:]).removesuffix(".rb")
    if without_suffix.endswith("_controller"):
        without_suffix = without_suffix[: -len("_controller")]
    return without_suffix


def _extract_arguments(node: Node, source: str) -> List[Dict]:
    args: List[Dict] = []
    arguments_node = node.child_by_field_name("arguments")
    if arguments_node is None:
        arguments_node = node.child_by_field_name("argument_list")
    if not arguments_node:
        return args

    pending_hash: Dict[str, object] = {}

    for child in arguments_node.children:
        node_type = child.type
        if node_type in {"(", ")", ",", "HEREDOC_BEGIN", "HEREDOC_END"}:
            continue
        if node_type == "pair":
            key_node = child.child_by_field_name("key")
            value_node = child.child_by_field_name("value")
            if key_node is None and child.child_count:
                key_node = child.children[0]
            if value_node is None and child.child_count > 1:
                value_node = child.children[-1]
            key_text = _literal_text(key_node, source) if key_node else ""
            if key_text:
                pending_hash[key_text] = _parse_value(value_node, source)
            continue

        if pending_hash:
            args.append({"type": "hash", "value": pending_hash})
            pending_hash = {}

        if node_type in {"symbol_literal", "symbol", "simple_symbol"}:
            args.append({"type": "symbol", "value": _literal_text(child, source)})
        elif node_type == "identifier":
            args.append({"type": "symbol", "value": _literal_text(child, source)})
        elif node_type == "string":
            args.append({"type": "string", "value": _literal_text(child, source)})
        elif node_type == "hash":
            args.append({"type": "hash", "value": _parse_hash(child, source)})
        elif node_type == "array":
            args.append({"type": "array", "value": _parse_array(child, source)})
        elif node_type == "bare_assoc_hash":
            args.append({"type": "hash", "value": _parse_hash(child, source)})
        else:
            args.append({"type": "raw", "value": _node_text(source, child)})

    if pending_hash:
        args.append({"type": "hash", "value": pending_hash})
    return args


def _parse_hash(node: Node, source: str) -> Dict:
    data: Dict[str, object] = {}
    for child in node.children:
        if child.type != "pair":
            continue
        key_node = child.child_by_field_name("key")

        if key_node is None and child.children:
            key_node = child.children[0]
        value_node = child.child_by_field_name("value")
        if value_node is None and len(child.children) > 1:
            for candidate in reversed(child.children[1:]):
                if candidate.type not in {":", "=>"}:
                    value_node = candidate
                    break

        key_text = _literal_text(key_node, source) if key_node else ""
        value = _parse_value(value_node, source)

        data[key_text] = value
    return data


def _parse_array(node: Node, source: str) -> List:
    items: List = []
    for child in node.children:
        if child.type == "SYMBOLS_BEGIN":
            continue
        if child.type in {"symbol_literal", "symbol", "simple_symbol"}:
            items.append(_literal_text(child, source))
        elif child.type == "identifier":
            items.append(_literal_text(child, source))
        elif child.type == "string":
            items.append(_literal_text(child, source))
        elif child.type == "bare_assoc_hash":
            items.append(_parse_hash(child, source))
        elif child.type == "hash":
            items.append(_parse_hash(child, source))
        elif child.type in {"array", "words"}:
            items.extend(_parse_array(child, source))
    return items


def _parse_value(node: Optional[Node], source: str):
    if node is None:
        return None
    if node.type in {"string", "symbol", "symbol_literal", "simple_symbol", "identifier"}:
        return _literal_text(node, source)
    if node.type == "array":
        return _parse_array(node, source)
    if node.type == "hash":
        return _parse_hash(node, source)
    return _node_text(source, node)


def _extract_hash_arguments(args: List[Dict]) -> Dict:
    options: Dict = {}
    for arg in args:
        if arg["type"] == "hash":
            for key, value in arg["value"].items():
                options[key] = value
    return options


def _normalize_action_list(option_value) -> Optional[List[str]]:
    if option_value is None:
        return None
    if isinstance(option_value, list):
        return [str(item) for item in option_value]
    return [str(option_value)]


def _is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "t", "1"}
    return bool(value)


def _effective_resource_entries(
    context: RouteContext, include_current: bool
) -> List[ResourceEntry]:
    stack = context.resource_stack
    if not stack:
        return []
    limit = len(stack) if include_current else len(stack) - 1
    if limit <= 0:
        return []
    start = 0
    current_idx = limit - 1
    for idx in range(current_idx):
        if stack[idx].shallow:
            start = idx
    return stack[start:limit]


def _collect_hash_options(args: List[Dict]) -> Dict[str, str]:
    options: Dict[str, str] = {}
    for arg in args:
        if arg["type"] == "hash":
            for key, value in arg["value"].items():
                if isinstance(value, str):
                    options[key] = value
    return options


def _extract_option(args: List[Dict], key: str):
    for arg in args:
        if arg["type"] == "hash":
            value = arg["value"].get(key)
            if value is not None:
                return value
    return None


def _extract_path_target_from_hash(args: List[Dict]):
    """
    The hashrocket spelling, `get 'legacy/:id' => 'system#legacy'`, where the
    path is the hash key. The key does not have to start with a slash, so
    anything that is not a known route option and points at a controller#action
    counts as one.
    """
    for arg in args:
        if arg["type"] != "hash":
            continue
        for key, value in arg["value"].items():
            if not isinstance(key, str) or key in ROUTE_OPTION_KEYS:
                continue
            if key.startswith("/") or (isinstance(value, str) and "#" in value):
                return key, value
    return None, None


def _normalize_via(via_option) -> List[str]:
    if via_option is None:
        return ["GET", "POST"]
    if isinstance(via_option, list):
        return [str(item).upper() for item in via_option]
    if isinstance(via_option, str):
        return [via_option.upper()]
    return [str(via_option).upper()]


def _first_symbol_or_string(args: List[Dict]) -> Optional[str]:
    for arg in args:
        if arg["type"] == "symbol":
            return arg["value"]
        if arg["type"] == "string":
            return arg["value"]
    return None


def _first_symbol(args: List[Dict]) -> Optional[str]:
    for arg in args:
        if arg["type"] == "symbol":
            return arg["value"]
    return None


def _first_string(args: List[Dict]) -> Optional[str]:
    for arg in args:
        if arg["type"] == "string":
            return arg["value"]
    return None


def _collect_resource_names(args: List[Dict]) -> List[str]:
    names: List[str] = []
    for arg in args:
        if arg["type"] in {"symbol", "string"}:
            names.append(arg["value"])
        else:
            break
    return names


def _singular(name: str) -> str:
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("s"):
        return name[:-1]
    return name


def _join_paths(prefix: str, segment: str) -> str:
    if not prefix:
        prefix = ""
    if not segment:
        segment = ""
    combined = "/".join(
        part.strip("/")
        for part in [prefix, segment]
        if part and part.strip("/") != ""
    )
    return f"/{combined}".replace("//", "/")


def _join_controllers(prefix: str, controller: str) -> str:
    prefix = prefix.strip("/")
    controller = controller.strip("/")
    if not prefix:
        return controller
    if not controller:
        return prefix
    return f"{prefix}/{controller}"


def _split_controller_action(target: Optional[str]):
    if not target:
        return None, None
    if isinstance(target, str):
        parts = target.split("#")
        if len(parts) == 2:
            return parts[0], parts[1]
    return None, None


def _namespace_segments(path_prefix: str) -> List[str]:
    if not path_prefix:
        return []
    segments = [seg for seg in path_prefix.strip("/").split("/") if seg]
    return segments


def _resource_collection_path(context: RouteContext) -> str:
    entries = _effective_resource_entries(context, include_current=True)
    segments = _namespace_segments(context.path_prefix)
    for idx, entry in enumerate(entries):
        segments.append(entry.name)
        if idx < len(entries) - 1:
            segments.append(f":{_singular(entry.name)}_id")
    if not segments:
        return "/"
    return "/" + "/".join(segments)


def _resource_member_path(
    context: RouteContext, shallow: Optional[bool] = None, param: str = "id"
) -> str:
    """
    The member path of the innermost resource. Rails names that segment :id and
    only the ancestors :<singular>_id, and a shallow resource drops the
    ancestors from the path entirely.
    """
    if not context.resource_stack:
        return _resource_collection_path(context)

    current_entry = context.resource_stack[-1]
    is_shallow = (
        shallow if shallow is not None else current_entry.shallow
    )

    if is_shallow:
        base_path = _join_paths(context.path_prefix, current_entry.name)
        return _join_paths(base_path, f":{param}")

    collection_path = _resource_collection_path(context)
    return _join_paths(collection_path, f":{param}")


def _resource_nested_path(context: RouteContext, shallow: Optional[bool] = None) -> str:
    """
    The scope a route written directly inside a resources block hangs off:
    Rails names the parent id :<singular>_id there, not :id.
    """
    if not context.resource_stack:
        return _resource_collection_path(context)
    current_entry = context.resource_stack[-1]
    return _resource_member_path(
        context, shallow=shallow, param=f"{_singular(current_entry.name)}_id"
    )


def _resource_controller_key(
    context: RouteContext, resource_name: str, controller_option
) -> str:
    """
    resources :photos, controller: 'images' is served by images_controller.rb,
    so the route map has to key off the option, not the resource name.
    """
    if isinstance(controller_option, str) and controller_option.strip():
        target = controller_option.strip().replace("::", "/")
        if target.startswith("/"):
            return target.strip("/")
        return _join_controllers(context.controller_prefix, target)
    return _join_controllers(context.controller_prefix, resource_name)


def _literal_text(node: Optional[Node], source: str) -> str:
    if node is None:
        return ""
    text = _node_text(source, node)
    if not text:
        return ""
    text = text.strip()
    if text.startswith(":"):
        text = text[1:]
    if len(text) >= 2 and ((text[0] == "'" and text[-1] == "'") or (text[0] == '"' and text[-1] == '"')):
        text = text[1:-1]
    return text


def _node_text(source: bytes, node: Node) -> str:
    # tree-sitter offsets are byte offsets, so slice the bytes and decode.
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")

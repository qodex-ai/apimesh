"""Load one OpenAPI contract and validate its local reference closure.

Resolution is repo-confined by construction: no URLs, no absolute paths, no
path that escapes the repository after symlinks are applied. A reference that
cannot be resolved under those rules is recorded, and the operations that
depend on it are the reconciler's to exclude. Nothing here ever guesses.

Schema references are validated and collected, never inlined: a recursive
schema (a tree node referencing itself) is legal OpenAPI and must survive as
a reference. Only path-item aliases and parameter entries are structurally
resolved, because folding and routing need their content in place.
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import unquote

import yaml

HTTP_VERBS = ("get", "post", "put", "delete", "patch", "head", "options", "trace")

# Closure limits: a hostile or degenerate spec must not stall the run.
MAX_CLOSURE_FILES = 200
MAX_CLOSURE_BYTES = 50 * 1024 * 1024
MAX_NODE_DEPTH = 128


class RefError(Exception):
    """A reference that resolution refuses: escape, URL, missing, or cycle."""


def _unescape_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _walk_pointer(document: dict, pointer: str):
    """RFC 6901 walk. Raises RefError when the path leads nowhere."""
    if pointer in ("", "#"):
        return document
    if pointer.startswith("#"):
        pointer = pointer[1:]
    if not pointer.startswith("/"):
        raise RefError(f"unsupported pointer {pointer!r}")
    node = document
    for raw_token in pointer[1:].split("/"):
        token = _unescape_pointer_token(raw_token)
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list):
            # RFC 6901 array indexes are non-negative digit sequences only;
            # Python's negative indexing must not leak in.
            if not token.isdigit():
                raise RefError(f"pointer {pointer!r} has a non-numeric index")
            try:
                node = node[int(token)]
            except IndexError as ex:
                raise RefError(f"pointer {pointer!r} misses a list index") from ex
        else:
            raise RefError(f"pointer {pointer!r} has no target")
    return node


class ContractLoader:
    """Loads documents relative to one repository root, with a shared cache."""

    def __init__(self, repo_root: str):
        self.repo_root = Path(os.path.realpath(repo_root))
        self._documents: Dict[str, dict] = {}
        self._bytes_loaded = 0

    def _contained_path(self, base_file: str, reference: str) -> str:
        """The repo-relative path a file reference resolves to, or RefError."""
        # Any scheme at all is refused, whatever its case: HTTPS://, ftp://,
        # ssh://, file:, and a Windows drive letter all match here.
        if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", reference) or reference.startswith("//"):
            raise RefError(f"non-local reference refused: {reference}")
        if reference.startswith("/"):
            raise RefError(f"absolute reference refused: {reference}")
        base_dir = (self.repo_root / base_file).parent
        candidate = os.path.realpath(base_dir / reference)
        if os.path.commonpath([candidate, str(self.repo_root)]) != str(self.repo_root):
            raise RefError(f"reference escapes the repository: {reference}")
        return str(Path(candidate).relative_to(self.repo_root))

    def _document(self, relative_path: str) -> dict:
        if relative_path in self._documents:
            return self._documents[relative_path]
        if len(self._documents) >= MAX_CLOSURE_FILES:
            raise RefError("reference closure exceeds the file limit")
        full_path = self.repo_root / relative_path
        try:
            size = full_path.stat().st_size
        except OSError as ex:
            raise RefError(f"referenced file missing: {relative_path}") from ex
        self._bytes_loaded += size
        if self._bytes_loaded > MAX_CLOSURE_BYTES:
            raise RefError("reference closure exceeds the size limit")
        try:
            text = full_path.read_text(encoding="utf-8", errors="replace")
            if full_path.suffix.lower() == ".json":
                document = json.loads(text)
            else:
                document = yaml.safe_load(text)
        except Exception as ex:
            raise RefError(f"referenced file unparseable: {relative_path}") from ex
        if not isinstance(document, dict):
            raise RefError(f"referenced file is not a mapping: {relative_path}")
        self._documents[relative_path] = document
        return document

    def _split_ref(self, base_file: str, ref: str) -> Tuple[str, str]:
        """(file, pointer) a $ref names, with the file containment-checked.

        Both halves arrive percent-encoded in the wild ({} written as %7B%7D),
        and the pointer walk needs the decoded form.
        """
        if not isinstance(ref, str) or not ref:
            raise RefError(f"malformed $ref: {ref!r}")
        file_part, _, pointer = ref.partition("#")
        pointer = unquote(pointer)
        if not file_part:
            return base_file, f"#{pointer}"
        return self._contained_path(base_file, unquote(file_part)), f"#{pointer}"

    def target(self, base_file: str, ref: str):
        """((file, pointer), target node) one $ref points at."""
        target_file, pointer = self._split_ref(base_file, ref)
        return (target_file, pointer), _walk_pointer(self._document(target_file), pointer)

    def resolve_node(self, node, base_file: str, _depth: int = 0, _stack=None):
        """The node with every $ref inlined, deeply. For small structures only.

        Used for path-item aliases and parameter entries, where the content is
        needed in place. A cycle here is a real error, and sibling keys next
        to a $ref are dropped the way OpenAPI 3.0 reads them.
        """
        if _depth > MAX_NODE_DEPTH:
            raise RefError("node nesting exceeds the depth limit")
        _stack = _stack or []
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                key, target = self.target(base_file, ref)
                if key in _stack:
                    raise RefError(f"reference cycle through {ref}")
                return self.resolve_node(target, key[0], _depth + 1, _stack + [key])
            return {
                key: self.resolve_node(value, base_file, _depth + 1, _stack)
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [
                self.resolve_node(item, base_file, _depth + 1, _stack) for item in node
            ]
        return node

    def collect_closure(self, node, base_file: str) -> Tuple[Dict[str, dict], List[str]]:
        """Every reference target the node reaches, transitively, uninlined.

        Returns ({"file#pointer": target_node}, [unresolvable ref errors]).
        A reference seen before is skipped, which is exactly what makes legal
        recursive schemas terminate.
        """
        closure: Dict[str, dict] = {}
        errors: List[str] = []
        visited = set()
        # YAML anchors alias one object from many places; walking each container
        # once keeps an alias DAG linear instead of exponential.
        seen_containers = set()

        def _walk(current, current_file: str, depth: int) -> None:
            if depth > MAX_NODE_DEPTH:
                errors.append("node nesting exceeds the depth limit")
                return
            if isinstance(current, (dict, list)):
                marker = id(current)
                if marker in seen_containers:
                    return
                seen_containers.add(marker)
            if isinstance(current, dict):
                ref = current.get("$ref")
                if isinstance(ref, str):
                    try:
                        key, target = self.target(current_file, ref)
                    except RefError as ex:
                        errors.append(str(ex))
                        return
                    if key in visited:
                        return
                    visited.add(key)
                    closure[f"{key[0]}#{key[1].lstrip('#')}"] = target
                    _walk(target, key[0], depth + 1)
                    return
                for value in current.values():
                    _walk(value, current_file, depth + 1)
            elif isinstance(current, list):
                for item in current:
                    _walk(item, current_file, depth + 1)

        _walk(node, base_file, 0)
        return closure, errors


def _follow_shallow(loader, node: dict, base_file: str) -> Tuple[dict, str]:
    """Follow a chain of whole-object $refs one level at a time.

    The target's inner references stay in place, which is what lets an alias
    to a path item full of recursive schemas resolve. Returns the final node
    and the file it lives in, so deeper references keep their own base.
    """
    seen = []
    current, current_file = node, base_file
    while isinstance(current, dict) and isinstance(current.get("$ref"), str):
        key, target = loader.target(current_file, current["$ref"])
        if key in seen:
            raise RefError(f"reference cycle through {current['$ref']}")
        seen.append(key)
        current, current_file = target, key[0]
    return current, current_file


def _folded_parameters(loader, path_item: dict, item_file: str, operation: dict):
    """Operation parameters with path-level ones folded in.

    Each parameter entry's own $ref is followed shallowly; the body's inner
    references stay put. Returns [(parameter, origin_file)] so the closure
    walk can validate each body against the file it was written in.
    """
    folded: List[Tuple[dict, str]] = []
    for parameter in operation.get("parameters") or []:
        if isinstance(parameter, dict):
            folded.append(_follow_shallow(loader, parameter, item_file))
    own_keys = {
        (body.get("name"), body.get("in"))
        for body, _ in folded
        if isinstance(body, dict)
    }
    for parameter in path_item.get("parameters") or []:
        if not isinstance(parameter, dict):
            continue
        body, origin = _follow_shallow(loader, parameter, item_file)
        if not isinstance(body, dict):
            continue
        if (body.get("name"), body.get("in")) not in own_keys:
            folded.append((body, origin))
    return [(body, origin) for body, origin in folded if isinstance(body, dict)]


def load_operations(entry: dict, repo_root: str) -> Tuple[List[dict], List[dict]]:
    """The operations one discovered contract declares, closure validated.

    Returns (operations, unresolved). Each operation carries the method, the
    spec-declared path, the operationId and tags when present, the authored
    operation object with path-level parameters folded in and references kept
    in place, and the reference closure those references reach. An operation
    whose closure fails lands in unresolved with its reason, and is never
    emitted as loadable.
    """
    loader = ContractLoader(repo_root)
    base_file = entry["path"]
    loader._documents[base_file] = entry["document"]
    paths = entry["document"].get("paths") or {}

    operations: List[dict] = []
    unresolved: List[dict] = []
    for spec_path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        try:
            # A path-item alias points at another route's whole definition.
            path_item, item_file = _follow_shallow(loader, path_item, base_file)
        except RefError as ex:
            unresolved.append({"path": spec_path, "error": str(ex)})
            continue
        if not isinstance(path_item, dict):
            unresolved.append({"path": spec_path, "error": "path item is not a mapping"})
            continue
        for verb in HTTP_VERBS:
            operation = path_item.get(verb)
            if not isinstance(operation, dict):
                continue
            try:
                parameters = _folded_parameters(loader, path_item, item_file, operation)
            except RefError as ex:
                unresolved.append(
                    {"path": spec_path, "method": verb.upper(), "error": str(ex)}
                )
                continue
            closure, errors = loader.collect_closure(operation, item_file)
            for body, origin in parameters:
                more_closure, more_errors = loader.collect_closure(body, origin)
                closure.update(more_closure)
                errors.extend(more_errors)
            if errors:
                unresolved.append(
                    {"path": spec_path, "method": verb.upper(), "error": errors[0]}
                )
                continue
            merged = dict(operation)
            parameter_origins: List[str] = []
            if parameters:
                merged["parameters"] = [body for body, _ in parameters]
                parameter_origins = [origin for _, origin in parameters]
            operations.append(
                {
                    "method": verb.upper(),
                    "spec_path": spec_path,
                    "operation_id": operation.get("operationId"),
                    "tags": [t for t in operation.get("tags") or [] if isinstance(t, str)],
                    "operation": merged,
                    "ref_closure": closure,
                    # The file the operation body lives in: differs from the
                    # contract's path when a path-item alias crossed files,
                    # and reference rewriting must resolve against it. Folded
                    # parameters keep their own origins for the same reason.
                    "source_file": item_file,
                    "parameter_origins": parameter_origins,
                }
            )
    return operations, unresolved

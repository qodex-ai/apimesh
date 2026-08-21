"""Classify each discovered contract: does this repo serve it?

The decision tree, per docs/contract-lane.md:

1. A spec named by a server-mode generator invocation is SERVED (tier 2).
   Implementation matching only corroborates and supplies path prefixes.
2. A spec named only by client-mode invocations is CONSUMED: excluded,
   reason ``client_generator``.
3. A spec with no build evidence never gets in on name matching alone:
   when its operationIds map onto unannotated methods of controller-shaped
   classes it becomes a CANDIDATE (reported for a human to confirm); when
   they map onto methods the code lane already routed it is documentation
   (``covers_routed_handlers``); otherwise ``no_server_evidence``.

Matching is corroboration and prefix source, never eligibility. Test sources
never prove anything: the shared ignore list keeps them out of the index.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from tree_sitter import Language, Parser
import tree_sitter_java

from config import Configurations
from java_pipeline import identify_api_functions as java_ast

config = Configurations()

_JAVA_LANGUAGE = Language(tree_sitter_java.language())
_parser = Parser(_JAVA_LANGUAGE)

# A file is worth parsing when it can carry serving proof: a controller
# annotation, an implements clause naming an *Api type, or a delegate.
_SNIFF = re.compile(
    rb"@\s*(?:RestController|Controller)\b|implements\s+[\w.<>,\s]*\b\w+(?:Api|Delegate)\b"
)

_SERVICE_ANNOTATIONS = {"Service", "Component"}


def _is_ignored(path: Path, base_path: Path) -> bool:
    try:
        relative = path.relative_to(base_path)
    except ValueError:
        relative = path
    return any(part in config.ignored_dirs for part in relative.parts)


def _candidate_files(repo_root: str) -> List[Path]:
    base_path = Path(repo_root)
    files = []
    for file_path in sorted(base_path.rglob("*.java")):
        if _is_ignored(file_path, base_path):
            continue
        try:
            with open(file_path, "rb") as handle:
                head = handle.read(65536)
        except OSError:
            continue
        if _SNIFF.search(head):
            files.append(file_path)
    return files


def _class_facts(file_path: Path, repo_root: str) -> List[dict]:
    """Every class in one file, with what proving and prefixing need."""
    try:
        source = file_path.read_bytes()
    except OSError:
        return []
    root = _parser.parse(source).root_node
    package = java_ast._package_name(root, source)
    imported = java_ast._imported_types(root, source)
    facts = []
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type != "class_declaration":
            continue
        annotations = {
            java_ast._annotation_name(annotation, source)
            for annotation in java_ast._annotations(node)
        }
        class_mapping = java_ast._class_mapping(node, source)
        prefixes: List[str] = []
        if class_mapping and class_mapping["readable"]:
            prefixes = [p for p in class_mapping["prefixes"] if p]
        methods = {}
        for method in java_ast._class_methods(node):
            annotation, _ = java_ast._mapping_annotation(method, source)
            methods[java_ast._declared_name(method, source)] = annotation is not None
        facts.append(
            {
                "file": str(file_path),
                "package": package,
                "imported": imported,
                "name": java_ast._declared_name(node, source),
                "is_controller": bool(annotations & java_ast.CONTROLLER_ANNOTATIONS),
                "is_component": bool(annotations & _SERVICE_ANNOTATIONS),
                "implements": java_ast._implemented_names(node, source),
                "prefixes": prefixes,
                # method name -> carries its own mapping annotation
                "methods": methods,
            }
        )
    return facts


class SourceIndex:
    """The repo's controller-shaped classes, indexed for spec matching."""

    def __init__(self, repo_root: str):
        self.classes: List[dict] = []
        for file_path in _candidate_files(repo_root):
            self.classes.extend(_class_facts(file_path, repo_root))
        # method name -> classes declaring it
        self.by_method: Dict[str, List[dict]] = {}
        for cls in self.classes:
            for method_name in cls["methods"]:
                self.by_method.setdefault(method_name, []).append(cls)

    def implementers_of_package(self, api_package: str) -> List[dict]:
        """Classes that implement a type imported from the generated package."""
        matches = []
        for cls in self.classes:
            for implemented in cls["implements"]:
                simple = implemented.split(".")[-1]
                qualified = cls["imported"].get(simple)
                if qualified and qualified.rsplit(".", 1)[0] == api_package:
                    matches.append(cls)
                    break
                if "." in implemented and implemented.rsplit(".", 1)[0] == api_package:
                    matches.append(cls)
                    break
                if not qualified and cls["package"] == api_package:
                    matches.append(cls)
                    break
        return matches


def _camelize(value: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", value)
    parts = [p for p in parts if p]
    if not parts:
        return ""
    head = parts[0][0].lower() + parts[0][1:]
    return head + "".join(p[0].upper() + p[1:] for p in parts[1:])


def _derived_method_names(operation: dict) -> List[str]:
    """The Java method names a generator would mint for one operation."""
    names = []
    operation_id = operation.get("operation_id")
    if operation_id:
        names.append(operation_id)
        camelized = _camelize(operation_id)
        if camelized and camelized not in names:
            names.append(camelized)
    else:
        # No operationId: generators derive from the path and the verb,
        # /health/deep GET -> healthDeepGet.
        derived = _camelize(
            re.sub(r"\{[^}]*\}", "", operation.get("spec_path", ""))
        )
        if derived:
            names.append(derived + operation["method"].capitalize())
    return names


def _match_operations(operations: List[dict], index: SourceIndex):
    """Which classes carry methods matching these operations' derived names.

    Returns (matched: {op_index: class}, unannotated_matches, routed_matches).
    """
    matched: Dict[int, dict] = {}
    unannotated = 0
    routed = 0
    for position, operation in enumerate(operations):
        for name in _derived_method_names(operation):
            candidates = index.by_method.get(name) or []
            if not candidates:
                continue
            chosen = candidates[0]
            matched[position] = chosen
            if chosen["methods"].get(name):
                routed += 1
            else:
                unannotated += 1
            break
    return matched, unannotated, routed


def _spec_prefixes(matched: Dict[int, dict], implementers: List[dict]) -> Tuple[str, Dict[int, str]]:
    """The default prefix for a spec and per-operation overrides.

    Each operation takes the class-level prefix of the class implementing it;
    operations nobody visibly implements (default interface methods) take the
    most common prefix among the spec's implementers.
    """
    by_operation: Dict[int, str] = {}
    counts: Dict[str, int] = {}
    for position, cls in matched.items():
        prefix = cls["prefixes"][0] if cls["prefixes"] else ""
        by_operation[position] = prefix
        counts[prefix] = counts.get(prefix, 0) + 1
    for cls in implementers:
        prefix = cls["prefixes"][0] if cls["prefixes"] else ""
        counts[prefix] = counts.get(prefix, 0) + 1
    default = max(counts, key=counts.get) if counts else ""
    return default, by_operation


def classify_contract(
    entry: dict,
    operations: List[dict],
    evidence: List[dict],
    index: SourceIndex,
) -> dict:
    """One contract's verdict, evidence and prefix data.

    Returns {"status": "served" | "excluded" | "candidate", ...}. Only
    ``served`` ever reaches the swagger; a candidate is a report entry.
    """
    server_invocations = [e for e in evidence if e["kind"] == "server"]
    client_invocations = [e for e in evidence if e["kind"] == "client"]

    if server_invocations:
        implementers: List[dict] = []
        for invocation in server_invocations:
            api_package = invocation.get("api_package")
            if api_package:
                implementers.extend(index.implementers_of_package(api_package))
        matched, unannotated, _ = _match_operations(operations, index)
        default_prefix, by_operation = _spec_prefixes(matched, implementers)
        return {
            "status": "served",
            "path": entry["path"],
            "invocations": server_invocations,
            "corroborated": bool(matched or implementers),
            "default_prefix": default_prefix,
            "prefix_by_operation": by_operation,
        }

    if client_invocations:
        return {
            "status": "excluded",
            "path": entry["path"],
            "reason": "client_generator",
            "invocations": client_invocations,
        }

    matched, unannotated, routed = _match_operations(operations, index)
    if matched and routed and not unannotated:
        # Every visible match is a method the code lane already routes: this
        # document describes existing annotated handlers, drift included.
        return {
            "status": "excluded",
            "path": entry["path"],
            "reason": "covers_routed_handlers",
        }
    if matched and unannotated:
        return {
            "status": "candidate",
            "path": entry["path"],
            "reason": "no_server_evidence",
            "matched_operations": len(matched),
            "operations": len(operations),
        }
    return {
        "status": "excluded",
        "path": entry["path"],
        "reason": "no_server_evidence",
    }


def build_source_index(repo_root: str) -> SourceIndex:
    return SourceIndex(repo_root)

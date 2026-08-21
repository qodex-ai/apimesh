"""Merge served contract operations with code-lane routes into one document.

Rules, per docs/contract-lane.md:

- Route identity is the shape (method + path with parameter names collapsed),
  so ``/users/{id}`` and ``/users/{userId}`` collide on purpose.
- When contract and code claim the same shape, the contract owns the route
  string and the operation content; the code candidate is recorded as
  superseded and skipped by LLM generation.
- When two contracts claim the same shape, that is a conflict: one wins
  deterministically (sorted source order), the loser is listed on the
  operation under ``x-apimesh-conflict`` and in the report. Nothing is
  silently overwritten.
- Every contract operation's references are rewritten onto namespaced
  components in the merged document; nothing dangles.
"""

import re
from typing import Dict, List, Optional, Tuple

from contract_lane.loader import ContractLoader

_PARAM = re.compile(r"\{[^}]*\}")

_COMPONENT_CATEGORIES = {
    "schemas",
    "responses",
    "parameters",
    "examples",
    "requestBodies",
    "headers",
    "securitySchemes",
    "links",
    "callbacks",
}


def join_route(prefix: str, path: str) -> str:
    """Controller prefix plus spec path, one leading slash, no doubling."""
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


def route_shape(method: str, route: str) -> Tuple[str, str]:
    return method.upper(), _PARAM.sub("{}", route)


def contract_candidates(verdict: dict, operations: List[dict]) -> List[dict]:
    """The reconciler's input rows for one served contract."""
    candidates = []
    for position, record in enumerate(operations):
        prefix = verdict["prefix_by_operation"].get(position, verdict["default_prefix"])
        route = join_route(prefix, record["spec_path"])
        candidates.append(
            {
                "lane": "contract",
                "method": record["method"],
                "route": route,
                "source_id": f"spec:{verdict['path']}#{record['method'].lower()} {record['spec_path']}",
                "spec_path": verdict["path"],
                "record": record,
            }
        )
    return candidates


class _ComponentStore:
    """Namespaced components for the merged document.

    Every referenced target gets one name derived from its origin file, so 66
    specs each defining ``Error`` stay 66 distinct schemas. Targets that do
    not live under a components category are inlined at the reference site.
    """

    def __init__(self):
        self.components: Dict[str, Dict[str, dict]] = {}
        self._names: Dict[str, Tuple[str, str]] = {}
        self._used: set = set()

    @staticmethod
    def _split_component(closure_key: str) -> Optional[Tuple[str, str, str]]:
        file_part, _, pointer = closure_key.partition("#")
        segments = [s for s in pointer.split("/") if s]
        if len(segments) == 3 and segments[0] == "components" and segments[1] in _COMPONENT_CATEGORIES:
            return file_part, segments[1], segments[2]
        return None

    def _namespace(self, file_part: str, name: str) -> str:
        stem = re.sub(r"[^A-Za-z0-9_]", "_", file_part.rsplit("/", 1)[-1].rsplit(".", 1)[0])
        candidate = f"{stem}_{name}"
        suffix = 1
        while candidate in self._used:
            suffix += 1
            candidate = f"{stem}_{name}_{suffix}"
        self._used.add(candidate)
        return candidate

    def name_for(self, closure_key: str) -> Optional[Tuple[str, str]]:
        """(category, namespaced name) for a component target, cached."""
        if closure_key in self._names:
            return self._names[closure_key]
        split = self._split_component(closure_key)
        if split is None:
            return None
        file_part, category, name = split
        namespaced = self._namespace(file_part, name)
        self._names[closure_key] = (category, namespaced)
        return category, namespaced

    def put(self, category: str, name: str, node: dict) -> None:
        self.components.setdefault(category, {})[name] = node


def _rewrite(node, base_file: str, loader: ContractLoader, store: _ComponentStore, closure: Dict[str, dict]):
    """A deep copy of node with every $ref renamed onto the merged components."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            target_file, pointer = loader._split_ref(base_file, ref)
            closure_key = f"{target_file}#{pointer.lstrip('#')}"
            named = store.name_for(closure_key)
            if named is None:
                # Not a components target: inline the validated target.
                target = closure.get(closure_key)
                return _rewrite(target, target_file, loader, store, closure)
            category, name = named
            return {"$ref": f"#/components/{category}/{name}"}
        return {
            key: _rewrite(value, base_file, loader, store, closure)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_rewrite(item, base_file, loader, store, closure) for item in node]
    return node


def reconcile(contract_rows: List[dict], code_ops: List[dict], repo_root: str) -> dict:
    """One merged path map from both lanes, with a full account of the merge.

    code_ops: [{"method", "route", "source_id"}] from the framework parser.
    Returns {"paths", "components", "conflicts", "superseded_code",
    "code_to_generate"}: the pipeline documents code_to_generate with the LLM
    and grafts the results under the same route keys.
    """
    loader = ContractLoader(repo_root)
    store = _ComponentStore()

    by_shape: Dict[Tuple[str, str], List[dict]] = {}
    for row in sorted(contract_rows, key=lambda r: r["source_id"]):
        by_shape.setdefault(route_shape(row["method"], row["route"]), []).append(row)

    paths: Dict[str, Dict[str, dict]] = {}
    conflicts: List[dict] = []
    rewritten_components_done: set = set()

    def _materialize(row: dict) -> dict:
        record = row["record"]
        closure = record["ref_closure"]
        operation = _rewrite(
            record["operation"], record["source_file"], loader, store, closure
        )
        for closure_key, target in closure.items():
            named = store.name_for(closure_key)
            if named is None or closure_key in rewritten_components_done:
                continue
            rewritten_components_done.add(closure_key)
            category, name = named
            store.put(
                category,
                name,
                _rewrite(target, closure_key.partition("#")[0], loader, store, closure),
            )
        return operation

    for (method, _shape), rows in sorted(by_shape.items(), key=lambda kv: kv[0]):
        winner, losers = rows[0], rows[1:]
        operation = _materialize(winner)
        operation["x-apimesh-source"] = [winner["source_id"]]
        real_conflicts = [
            loser for loser in losers if loser["spec_path"] != winner["spec_path"]
        ]
        if real_conflicts:
            operation["x-apimesh-conflict"] = [r["source_id"] for r in real_conflicts]
            conflicts.append(
                {
                    "method": method,
                    "route": winner["route"],
                    "won": winner["source_id"],
                    "lost": [r["source_id"] for r in real_conflicts],
                }
            )
        paths.setdefault(winner["route"], {})[method.lower()] = operation

    superseded_code: List[dict] = []
    code_to_generate: List[dict] = []
    contract_shapes = {route_shape(r["method"], r["route"]) for r in contract_rows}
    for code_op in code_ops:
        shape = route_shape(code_op["method"], code_op["route"])
        if shape in contract_shapes:
            superseded_code.append(code_op)
        else:
            code_to_generate.append(code_op)

    return {
        "paths": paths,
        "components": {
            category: dict(sorted(items.items()))
            for category, items in sorted(store.components.items())
        },
        "conflicts": conflicts,
        "superseded_code": superseded_code,
        "code_to_generate": code_to_generate,
    }

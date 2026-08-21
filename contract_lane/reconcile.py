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

from contract_lane.loader import ContractLoader, RefError

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
    """The reconciler's input rows for one served contract.

    Every proven prefix fans out: a controller mapped to {"/v1", "/v2"}
    serves each operation under both.
    """
    candidates = []
    seen = set()
    by_operation = verdict.get("prefixes_by_operation")
    for position, record in enumerate(operations):
        if by_operation is not None and position not in by_operation:
            # This operation's prefix was unresolvable; it was dropped and
            # reported, never guessed.
            continue
        prefixes = (
            by_operation[position]
            if by_operation is not None
            else verdict.get("prefixes") or [""]
        )
        for prefix in prefixes:
            route = join_route(prefix, record["spec_path"])
            if (record["method"], route) in seen:
                continue
            seen.add((record["method"], route))
            candidates.append(
                {
                    "lane": "contract",
                    "method": record["method"],
                    "route": route,
                    "source_id": f"spec:{verdict['path']}#{record['method'].lower()} {route}",
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


def _rewrite(node, base_file: str, loader: ContractLoader, store: _ComponentStore, closure: Dict[str, dict], memo: Optional[dict] = None, _inlining: Optional[set] = None):
    """A deep copy of node with every $ref renamed onto the merged components.

    The memo keys container identity per base file, so a YAML alias DAG is
    rewritten once per aliased object instead of once per appearance. A cycle
    among non-component targets (which are inlined, not renamed) has no
    finite expansion and is refused.
    """
    memo = {} if memo is None else memo
    _inlining = set() if _inlining is None else _inlining
    if isinstance(node, (dict, list)):
        memo_key = (id(node), base_file)
        if memo_key in memo:
            return memo[memo_key]
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            target_file, pointer = loader._split_ref(base_file, ref)
            closure_key = f"{target_file}#{pointer.lstrip('#')}"
            named = store.name_for(closure_key)
            if named is None:
                # Not a components target: inline the validated target.
                if closure_key in _inlining:
                    raise RefError(f"non-component reference cycle through {ref}")
                target = closure.get(closure_key)
                result = _rewrite(
                    target, target_file, loader, store, closure, memo,
                    _inlining | {closure_key},
                )
            else:
                category, name = named
                result = {"$ref": f"#/components/{category}/{name}"}
        else:
            result = {
                key: _rewrite(value, base_file, loader, store, closure, memo, _inlining)
                for key, value in node.items()
            }
            # A discriminator mapping names schemas as strings, not $refs, so
            # the rename must reach it or the mapping dangles.
            discriminator = result.get("discriminator")
            if isinstance(discriminator, dict) and isinstance(
                discriminator.get("mapping"), dict
            ):
                rewritten_mapping = {}
                for mapping_key, mapping_value in discriminator["mapping"].items():
                    if isinstance(mapping_value, str) and "#" in mapping_value:
                        target_file, pointer = loader._split_ref(base_file, mapping_value)
                        named = store.name_for(f"{target_file}#{pointer.lstrip('#')}")
                        if named is not None:
                            category, name = named
                            rewritten_mapping[mapping_key] = f"#/components/{category}/{name}"
                            continue
                    rewritten_mapping[mapping_key] = mapping_value
                discriminator = dict(discriminator, mapping=rewritten_mapping)
                result["discriminator"] = discriminator
        memo[(id(node), base_file)] = result
        return result
    if isinstance(node, list):
        result = [
            _rewrite(item, base_file, loader, store, closure, memo, _inlining)
            for item in node
        ]
        memo[(id(node), base_file)] = result
        return result
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

    # Split the code lane first, so a superseded handler's routing conditions
    # can ride on the contract operation that replaces it.
    superseded_code: List[dict] = []
    code_to_generate: List[dict] = []
    conditions_by_shape: Dict[Tuple[str, str], dict] = {}
    contract_shapes = set(by_shape)
    for code_op in code_ops:
        shape = route_shape(code_op["method"], code_op["route"])
        if shape in contract_shapes:
            superseded_code.append(code_op)
            if code_op.get("conditions"):
                conditions_by_shape.setdefault(shape, code_op["conditions"])
        else:
            code_to_generate.append(code_op)

    paths: Dict[str, Dict[str, dict]] = {}
    conflicts: List[dict] = []
    rewrite_failures: List[dict] = []
    rewritten_components_done: set = set()

    def _materialize(row: dict) -> dict:
        record = row["record"]
        closure = record["ref_closure"]
        # Folded parameters may come from other files than the operation body,
        # and their inner references resolve against those origins.
        body = {k: v for k, v in record["operation"].items() if k != "parameters"}
        operation = _rewrite(body, record["source_file"], loader, store, closure)
        raw_parameters = record["operation"].get("parameters") or []
        origins = record.get("parameter_origins") or []
        if raw_parameters:
            operation["parameters"] = [
                _rewrite(
                    parameter,
                    origins[index] if index < len(origins) else record["source_file"],
                    loader,
                    store,
                    closure,
                )
                for index, parameter in enumerate(raw_parameters)
            ]
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
        # Security requirements reference schemes by name; rename the schemes
        # into the merged components and the requirement keys with them.
        schemes = record.get("security_schemes") or {}
        if schemes:
            renamed = {}
            for scheme_name, scheme_node in schemes.items():
                scheme_key = (
                    f"{row['spec_path']}#/components/securitySchemes/{scheme_name}"
                )
                category, new_name = store.name_for(scheme_key)
                if scheme_key not in rewritten_components_done:
                    rewritten_components_done.add(scheme_key)
                    store.put(
                        category,
                        new_name,
                        _rewrite(scheme_node, row["spec_path"], loader, store, closure),
                    )
                renamed[scheme_name] = new_name
            if operation.get("security"):
                operation["security"] = [
                    {renamed.get(key, key): value for key, value in requirement.items()}
                    for requirement in operation["security"]
                    if isinstance(requirement, dict)
                ]
        return operation

    for (method, shape), rows in sorted(by_shape.items(), key=lambda kv: kv[0]):
        winner, losers = rows[0], rows[1:]
        try:
            operation = _materialize(winner)
        except RefError as ex:
            rewrite_failures.append(
                {"method": method, "route": winner["route"], "error": str(ex)}
            )
            continue
        operation["x-apimesh-source"] = [winner["source_id"]]
        if (method, shape) in conditions_by_shape:
            operation["x-apimesh-routing-conditions"] = conditions_by_shape[
                (method, shape)
            ]
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

    return {
        "paths": paths,
        "components": {
            category: dict(sorted(items.items()))
            for category, items in sorted(store.components.items())
        },
        "conflicts": conflicts,
        "rewrite_failures": rewrite_failures,
        "superseded_code": superseded_code,
        "code_to_generate": code_to_generate,
    }

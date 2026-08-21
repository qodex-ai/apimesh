"""One call that runs the whole contract lane for a repository.

Deterministic and LLM-free, so it runs fresh on every invocation: discovery
re-sweeps, evidence re-parses, verdicts re-derive. What persists between runs
is the report (repo_profile.json, written by the pipeline) and the hashes
that overrides bind to; nothing here reads a previous run's conclusions.
"""

import hashlib
import json
import os
from typing import Dict, List, Optional

from contract_lane.build_evidence import collect_build_evidence, evidence_by_spec
from contract_lane.discovery import discover_contract_documents
from contract_lane.loader import load_operations
from contract_lane.reconcile import contract_candidates
from contract_lane.spring_prover import build_source_index, classify_contract

# Operator assertions, versioned with the repo. `exclude` applies
# unconditionally because it is fail-closed; `include` binds to the
# eligibility hash printed in repo_profile.json and goes dormant the moment
# the underlying evidence changes.
OVERRIDES_FILENAME = ".apimesh-overrides.json"


def lane_enabled() -> bool:
    return os.environ.get("APIMESH_INGEST_SPECS", "").strip() != "0"


def _load_overrides(repo_root: str):
    """({spec path: override}, error string or None)."""
    path = os.path.join(repo_root, OVERRIDES_FILENAME)
    if not os.path.isfile(path):
        return {}, None
    try:
        document = json.loads(open(path).read())
    except (OSError, ValueError) as ex:
        return {}, f"{OVERRIDES_FILENAME} unreadable: {ex}"
    overrides = {}
    for entry in document.get("specs", []) if isinstance(document, dict) else []:
        if not isinstance(entry, dict):
            continue
        spec_path = entry.get("path")
        action = entry.get("action")
        if isinstance(spec_path, str) and action in ("include", "exclude"):
            overrides[spec_path] = entry
    return overrides, None


def _sha256_of_file(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


# Bumped when classification rules change: a cached override bound to an old
# policy must go dormant rather than keep applying under new rules.
PROVER_POLICY_VERSION = "1"


def _eligibility_hash(
    repo_root: str, spec_path: str, invocations: List[dict], implementer_files: List[str]
) -> str:
    """Covers the whole proof surface: spec, build files, implementers, policy.

    Editing any of them, or removing the last implementing controller, must
    change this hash so overrides and cached conclusions go stale with it.
    """
    digest = hashlib.sha256()
    digest.update(PROVER_POLICY_VERSION.encode())
    digest.update(_sha256_of_file(os.path.join(repo_root, spec_path)).encode())
    build_files = sorted(
        {invocation["build_file"] for invocation in invocations if invocation.get("build_file")}
    )
    for build_file in build_files:
        digest.update(build_file.encode())
        digest.update(_sha256_of_file(os.path.join(repo_root, build_file)).encode())
    for implementer in sorted(set(implementer_files)):
        digest.update(implementer.encode())
        digest.update(_sha256_of_file(implementer).encode())
    return digest.hexdigest()


def run_lane(repo_root: str) -> Optional[dict]:
    """The served rows and the full account of every decision, or None when
    the lane is disabled via APIMESH_INGEST_SPECS=0."""
    if not lane_enabled():
        return None

    inventory = discover_contract_documents(repo_root)
    evidence = evidence_by_spec(collect_build_evidence(repo_root))
    index = build_source_index(repo_root)
    implementer_files = [cls["file"] for cls in index.classes]
    overrides, override_error = _load_overrides(repo_root)

    rows: List[dict] = []
    served: List[dict] = []
    excluded: List[dict] = []
    candidates: List[dict] = []
    unresolved_operations: List[dict] = []
    overrides_report: List[dict] = []
    operations_loaded = 0

    for entry in inventory["contracts"]:
        operations, unresolved = load_operations(entry, repo_root)
        operations_loaded += len(operations)
        for item in unresolved:
            unresolved_operations.append(dict(item, spec=entry["path"]))
        spec_evidence = evidence.get(entry["path"], [])
        eligibility = _eligibility_hash(
            repo_root, entry["path"], spec_evidence, implementer_files
        )
        verdict = classify_contract(entry, operations, spec_evidence, index)
        verdict["eligibility_hash"] = eligibility
        verdict["operations"] = len(operations)

        override = overrides.get(entry["path"])
        if override is not None:
            if override["action"] == "exclude":
                # Fail-closed, so an exclude needs no hash: it wins over any
                # proof, including a served verdict.
                verdict = {
                    "status": "excluded",
                    "path": entry["path"],
                    "reason": "override_exclude",
                    "eligibility_hash": eligibility,
                    "operations": len(operations),
                }
                overrides_report.append({"path": entry["path"], "action": "exclude", "state": "applied"})
            elif verdict["status"] != "served":
                if override.get("eligibility_hash") == eligibility:
                    verdict = {
                        "status": "served",
                        "path": entry["path"],
                        "invocations": [],
                        "corroborated": False,
                        "override": True,
                        "default_prefix": override.get("prefix", ""),
                        "prefix_variants": [],
                        "prefix_by_operation": {},
                        "eligibility_hash": eligibility,
                        "operations": len(operations),
                    }
                    overrides_report.append({"path": entry["path"], "action": "include", "state": "applied"})
                else:
                    # The evidence this include was written against has
                    # changed; the assertion survives as a record, not as an
                    # effect.
                    overrides_report.append(
                        {
                            "path": entry["path"],
                            "action": "include",
                            "state": "dormant",
                            "expected": override.get("eligibility_hash"),
                            "current": eligibility,
                        }
                    )

        if verdict["status"] == "served":
            served.append(verdict)
            rows.extend(contract_candidates(verdict, operations))
        elif verdict["status"] == "candidate":
            candidates.append(verdict)
        else:
            excluded.append(verdict)

    for spec_path, override in overrides.items():
        if not any(item["path"] == spec_path for item in overrides_report):
            overrides_report.append(
                {"path": spec_path, "action": override["action"], "state": "unmatched"}
            )

    report = {
        "specs_found": len(inventory["contracts"]),
        "components_found": len(inventory["components"]),
        "swagger2_skipped": len(inventory["swagger2"]),
        "parse_errors": inventory["parse_errors"],
        "truncated": inventory["truncated"],
        "operations_loaded": operations_loaded,
        "unresolved_operations": unresolved_operations,
        "served": [
            {
                "path": verdict["path"],
                "operations": verdict["operations"],
                "corroborated": verdict["corroborated"],
                "override": verdict.get("override", False),
                "default_prefix": verdict["default_prefix"],
                "prefix_variants": verdict["prefix_variants"],
                "eligibility_hash": verdict["eligibility_hash"],
                "invocations": [
                    {
                        "build_file": invocation["build_file"],
                        "tool": invocation["tool"],
                        "generator": invocation["generator"],
                    }
                    for invocation in verdict["invocations"]
                ],
            }
            for verdict in served
        ],
        "excluded": [
            {"path": verdict["path"], "reason": verdict["reason"]}
            for verdict in excluded
        ],
        "candidates": [
            {
                "path": verdict["path"],
                "matched_operations": verdict.get("matched_operations", 0),
                "operations": verdict.get("operations", 0),
                # The hash an include override must carry to activate.
                "eligibility_hash": verdict.get("eligibility_hash", ""),
            }
            for verdict in candidates
        ],
        "overrides": overrides_report,
    }
    if override_error:
        report["overrides_error"] = override_error
    return {"rows": rows, "report": report}

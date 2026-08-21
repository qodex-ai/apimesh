"""One call that runs the whole contract lane for a repository.

Deterministic and LLM-free, so it runs fresh on every invocation: discovery
re-sweeps, evidence re-parses, verdicts re-derive. What persists between runs
is the report (repo_profile.json, written by the pipeline) and the hashes
that overrides bind to; nothing here reads a previous run's conclusions.
"""

import hashlib
import os
from typing import Dict, List, Optional

from contract_lane.build_evidence import collect_build_evidence, evidence_by_spec
from contract_lane.discovery import discover_contract_documents
from contract_lane.loader import load_operations
from contract_lane.reconcile import contract_candidates
from contract_lane.spring_prover import build_source_index, classify_contract


def lane_enabled() -> bool:
    return os.environ.get("APIMESH_INGEST_SPECS", "").strip() != "0"


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


def _eligibility_hash(repo_root: str, verdict: dict, implementer_files: List[str]) -> str:
    """Covers the whole proof surface: spec, build files, implementers, policy.

    Editing any of them, or removing the last implementing controller, must
    change this hash so overrides and cached conclusions go stale with it.
    """
    digest = hashlib.sha256()
    digest.update(PROVER_POLICY_VERSION.encode())
    digest.update(_sha256_of_file(os.path.join(repo_root, verdict["path"])).encode())
    build_files = sorted(
        {invocation["build_file"] for invocation in verdict.get("invocations", [])}
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

    rows: List[dict] = []
    served: List[dict] = []
    excluded: List[dict] = []
    candidates: List[dict] = []
    unresolved_operations: List[dict] = []
    operations_loaded = 0

    for entry in inventory["contracts"]:
        operations, unresolved = load_operations(entry, repo_root)
        operations_loaded += len(operations)
        for item in unresolved:
            unresolved_operations.append(dict(item, spec=entry["path"]))
        verdict = classify_contract(
            entry, operations, evidence.get(entry["path"], []), index
        )
        if verdict["status"] == "served":
            verdict["operations"] = len(operations)
            verdict["eligibility_hash"] = _eligibility_hash(
                repo_root, verdict, implementer_files
            )
            served.append(verdict)
            rows.extend(contract_candidates(verdict, operations))
        elif verdict["status"] == "candidate":
            candidates.append(verdict)
        else:
            excluded.append(verdict)

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
            }
            for verdict in candidates
        ],
    }
    return {"rows": rows, "report": report}

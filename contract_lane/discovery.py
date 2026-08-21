"""Find the OpenAPI documents a repository carries.

Discovery is exhaustive on every run and uses its own ignore policy: only
dependency caches and VCS internals are skipped. The code lane's semantic
ignores (docs, vendor, tests) deliberately do not apply here, because those
are exactly the directories contracts live in. Discovery never decides
whether a document is served; it only reports what exists. See
docs/contract-lane.md.
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml

# Dependency caches, tool state, and build OUTPUT dirs, which hold copies of
# the authored specs and would double-discover them. Never semantic
# directories: docs, vendor and tests are where contracts live.
CONTRACT_IGNORED_DIRS = {
    ".git",
    "node_modules",
    "bower_components",
    "venv",
    ".venv",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".gradle",
    ".idea",
    "target",
    "build",
    "dist",
}

_CANDIDATE_SUFFIXES = {".yaml", ".yml", ".json"}

# A document announces itself near the top. Compact JSON starts {"openapi":
# so the match must also fire after braces and commas, not only line starts.
_SNIFF_BYTES = 65536
_SNIFF_PATTERN = re.compile(
    rb'(?:^|[\n{,])\s*"?(openapi|swagger)"?\s*:', re.IGNORECASE
)

# Parse limits: a single runaway file, or a sea of candidates, must not stall
# the run. Exceeding an aggregate budget stops discovery and says so.
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_PARSED_FILES = 2000
MAX_AGGREGATE_BYTES = 200 * 1024 * 1024

_HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "options", "trace"}


def _own_output_files() -> set:
    """ApiMesh's own output files; never re-ingest them.

    Only the files are excluded, not their directory: the default output dir
    can be the repo root or a directory that also holds authored contracts,
    and pruning it once emptied a whole repository's inventory.
    """
    output_filepath = os.environ.get("APIMESH_OUTPUT_FILEPATH")
    if not output_filepath:
        return set()
    output_filepath = os.path.realpath(os.path.abspath(output_filepath))
    output_dir = os.path.dirname(output_filepath)
    return {
        output_filepath,
        os.path.join(output_dir, "api_index.json"),
        os.path.join(output_dir, "repo_profile.json"),
    }


def _sniffs_like_openapi(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            head = handle.read(_SNIFF_BYTES)
    except OSError:
        return False
    return bool(_SNIFF_PATTERN.search(head))


def _parse_documents(path: Path) -> List[dict]:
    """Every mapping the file holds. A broken file holds none."""
    try:
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise ValueError(f"document larger than {MAX_DOCUMENT_BYTES} bytes")
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".json":
            documents = [json.loads(text)]
        else:
            documents = list(yaml.safe_load_all(text))
    except Exception as ex:
        raise ValueError(str(ex)) from ex
    return [doc for doc in documents if isinstance(doc, dict)]


def _operation_count(document: dict) -> int:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return 0
    count = 0
    for item in paths.values():
        if isinstance(item, dict):
            count += sum(1 for verb in item if str(verb).lower() in _HTTP_VERBS)
    return count


def discover_contract_documents(repo_root: str) -> Dict[str, List[dict]]:
    """The repository's OpenAPI inventory, grouped by what each file is.

    Returns a dict with:
      contracts:      openapi 3.x documents that declare paths
      components:     openapi 3.x documents with no paths (shared schema files)
      swagger2:       swagger 2.0 documents, reported and skipped
      parse_errors:   files that sniffed like OpenAPI but did not parse
    Every entry carries the repo-relative path, so reports stay readable.
    """
    repo_root_path = Path(repo_root).resolve()
    own_output = _own_output_files()
    inventory: Dict[str, List[dict]] = {
        "contracts": [],
        "components": [],
        "swagger2": [],
        "parse_errors": [],
        "truncated": False,
    }
    parsed_files = 0
    aggregate_bytes = 0

    for current_dir, dirnames, filenames in os.walk(repo_root_path):
        dirnames[:] = sorted(
            name for name in dirnames if name not in CONTRACT_IGNORED_DIRS
        )
        if inventory["truncated"]:
            break
        for filename in sorted(filenames):
            path = Path(current_dir) / filename
            if path.suffix.lower() not in _CANDIDATE_SUFFIXES:
                continue
            if path.is_symlink():
                # A symlinked document can point anywhere; the loader enforces
                # containment, and discovery simply refuses the ambiguity.
                continue
            if os.path.realpath(path) in own_output:
                continue
            if not _sniffs_like_openapi(path):
                continue
            if parsed_files >= MAX_PARSED_FILES or aggregate_bytes >= MAX_AGGREGATE_BYTES:
                # A truncated inventory must say so: downstream reads this as
                # "the sweep stopped", never as "nothing else exists".
                inventory["truncated"] = True
                break
            relative = str(path.relative_to(repo_root_path))
            parsed_files += 1
            try:
                aggregate_bytes += path.stat().st_size
                documents = _parse_documents(path)
            except (ValueError, OSError) as ex:
                inventory["parse_errors"].append({"path": relative, "error": str(ex)})
                continue
            contract_docs = []
            for doc_index, document in enumerate(documents):
                version = document.get("openapi")
                if isinstance(version, (int, float)):
                    version = str(version)
                if isinstance(version, str) and version.startswith("3"):
                    entry = {
                        "path": relative,
                        "doc_index": doc_index,
                        "version": version,
                        "operations": _operation_count(document),
                        "document": document,
                    }
                    # A path item that is all $ref aliases counts zero verbs
                    # here, so declaring paths at all is what makes a contract.
                    paths = document.get("paths")
                    if isinstance(paths, dict) and paths:
                        contract_docs.append(entry)
                    else:
                        inventory["components"].append(entry)
                elif str(document.get("swagger", "")).startswith("2"):
                    inventory["swagger2"].append({"path": relative})
            # Build evidence names files, not documents inside them, so a file
            # holding several contracts cannot be attributed unambiguously.
            for entry in contract_docs:
                entry["contracts_in_file"] = len(contract_docs)
            inventory["contracts"].extend(contract_docs)
    return inventory

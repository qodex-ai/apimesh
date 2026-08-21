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

# A document announces itself near the top; scanning more buys nothing.
_SNIFF_BYTES = 4096
_SNIFF_PATTERN = re.compile(
    rb'(?:^|\n)\s*"?(openapi|swagger)"?\s*:', re.IGNORECASE
)

# Parse limits: a single runaway file must not stall the run.
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024

_HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "options", "trace"}


def _own_output_dir() -> Optional[str]:
    """ApiMesh's own swagger.json is an OpenAPI document; never re-ingest it."""
    output_filepath = os.environ.get("APIMESH_OUTPUT_FILEPATH")
    if not output_filepath:
        return None
    return os.path.realpath(os.path.dirname(os.path.abspath(output_filepath)))


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
    own_output = _own_output_dir()
    inventory: Dict[str, List[dict]] = {
        "contracts": [],
        "components": [],
        "swagger2": [],
        "parse_errors": [],
    }

    for current_dir, dirnames, filenames in os.walk(repo_root_path):
        dirnames[:] = sorted(
            name for name in dirnames if name not in CONTRACT_IGNORED_DIRS
        )
        if own_output and os.path.realpath(current_dir) == own_output:
            dirnames[:] = []
            continue
        for filename in sorted(filenames):
            path = Path(current_dir) / filename
            if path.suffix.lower() not in _CANDIDATE_SUFFIXES:
                continue
            if path.is_symlink():
                # A symlinked document can point anywhere; the loader enforces
                # containment, and discovery simply refuses the ambiguity.
                continue
            if not _sniffs_like_openapi(path):
                continue
            relative = str(path.relative_to(repo_root_path))
            try:
                documents = _parse_documents(path)
            except ValueError as ex:
                inventory["parse_errors"].append({"path": relative, "error": str(ex)})
                continue
            for document in documents:
                version = document.get("openapi")
                if isinstance(version, (int, float)):
                    version = str(version)
                if isinstance(version, str) and version.startswith("3"):
                    entry = {
                        "path": relative,
                        "version": version,
                        "operations": _operation_count(document),
                        "document": document,
                    }
                    # A path item that is all $ref aliases counts zero verbs
                    # here, so declaring paths at all is what makes a contract.
                    paths = document.get("paths")
                    if isinstance(paths, dict) and paths:
                        inventory["contracts"].append(entry)
                    else:
                        inventory["components"].append(entry)
                elif str(document.get("swagger", "")).startswith("2"):
                    inventory["swagger2"].append({"path": relative})
    return inventory

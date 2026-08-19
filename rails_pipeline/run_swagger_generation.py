import copy
import json
import os
import re
import time
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pipeline_common
from config import Configurations
from utils import (
    get_git_commit_hash,
    get_github_repo_url,
    get_repo_path,
    get_repo_name,
    get_output_filepath,
    get_changed_files_since,
)
from rails_pipeline.definition_swagger_generator import (
    get_batch_definition_swagger,
    get_function_definition_swagger,
)
from rails_pipeline.generate_file_information import (
    process_file,
)
from rails_pipeline.find_api_definition_files import (
    find_api_definition_files,
)
from rails_pipeline.identify_api_functions import (
    collect_route_concerns,
    find_api_endpoints,
    is_route_file,
)

config = Configurations()


_CLASS_INDEX_CACHE: Dict[str, Dict[str, object]] = {}
_CLASS_INDEX_CACHE_ROOT: Optional[str] = None
_CLASS_CODE_BLOCK_CACHE: Dict[str, List[str]] = {}
_FILE_CONTENT_CACHE: Dict[str, List[str]] = {}
_FUNCTION_INDEX_CACHE: Dict[str, List[Dict[str, object]]] = {}

_PARAM_PATTERN = re.compile(r"params\[(?::|['\"])([A-Za-z0-9_]+)['\"]?\]")
_PARAM_HINT_FUNCTIONS = {"apply_filters"}
# Route verbs read as method names in a controller file, never as helper calls.
_NON_HELPER_NAMES = {"get", "post", "put", "delete", "patch"}
_ROUTE_PARAM_PATTERN = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")

MAX_ENDPOINTS_PER_BATCH = pipeline_common.MAX_ENDPOINTS_PER_BATCH
CONTEXT_TOKEN_BUDGET = pipeline_common.CONTEXT_TOKEN_BUDGET
_EFFECTIVE_CONTEXT_BUDGET = pipeline_common.EFFECTIVE_CONTEXT_BUDGET
MAX_HANDLER_TOKENS = pipeline_common.MAX_HANDLER_TOKENS


_EMPTY_EXTRACTION_WARNING = (
    "apimesh: rails parser found 0 endpoints, falling back to generic extraction"
)


def should_process_directory(dir_path: str, root_path: str) -> bool:
    """
    Check if a directory should be processed or ignored.
    Only components below root_path are matched, otherwise a repo checked out
    under /var, /tmp or /build would be skipped entirely.
    """
    try:
        relative_path = os.path.relpath(dir_path, root_path)
    except ValueError:
        relative_path = dir_path
    path_parts = relative_path.split(os.sep)
    return not any(part in config.ignored_dirs for part in path_parts)


def _api_index_output_path() -> str:
    return pipeline_common.api_index_output_path(get_output_filepath())


# Per file metadata is cached under the output directory, never inside the
# scanned repo: a run must not create or delete anything in the user's tree.
METADATA_CACHE_PIPELINE = "rails"

# Bumped when the shape of a metadata entry changes. A missing or stale marker
# wipes this pipeline's cache before anything reads it.
METADATA_CACHE_VERSION = "2"

# File path -> content hash, and file path -> the cache entry holding its
# metadata. Both are per run: two runs in one process are two checkouts.
_CONTENT_HASHES: Dict[str, Optional[str]] = {}
_METADATA_ENTRIES: Dict[str, str] = {}


def _metadata_cache_dir() -> str:
    return pipeline_common.metadata_cache_dir(
        get_output_filepath(), METADATA_CACHE_PIPELINE
    )


def _content_hash(file_path: str) -> Optional[str]:
    return pipeline_common.content_hash(file_path, _CONTENT_HASHES)


def _metadata_cache_filename(file_path: str, content_hash: str) -> str:
    return pipeline_common.metadata_cache_filename(file_path, content_hash)


def _metadata_cache_path(file_path: str) -> Optional[str]:
    """Where this file's metadata sits for the content it holds right now."""
    return pipeline_common.metadata_cache_path(
        file_path, _metadata_cache_dir, _content_hash(file_path)
    )


def _prepare_metadata_cache() -> str:
    return pipeline_common.prepare_metadata_cache(
        _metadata_cache_dir(), METADATA_CACHE_VERSION
    )


def _cache_file_metadata(file_path: str, directory_path: str) -> None:
    pipeline_common.cache_file_metadata(
        file_path,
        directory_path,
        _metadata_cache_path,
        process_file,
        _METADATA_ENTRIES,
    )


def _build_metadata_cache(directory_path: str) -> None:
    """One cache entry per ruby file below the repo root."""
    pipeline_common.build_metadata_cache(
        directory_path,
        _prepare_metadata_cache,
        should_process_directory,
        lambda file_path: file_path.endswith(".rb"),
        _cache_file_metadata,
    )


def _prune_metadata_cache() -> None:
    pipeline_common.prune_metadata_cache(_metadata_cache_dir(), _METADATA_ENTRIES)


def _load_file_metadata(file_path: str):
    return pipeline_common.load_file_metadata(_metadata_cache_path(file_path))


def _normalize_route(route) -> str:
    """
    Canonical path for a route the extractor found. Rails writes params as
    :user_id while OpenAPI expects {user_id}; this single form is used for the
    swagger key, the api_index key and the removal lookup so they always match.
    """
    if not route or not isinstance(route, str):
        return ""
    normalized = _ROUTE_PARAM_PATTERN.sub(r"{\1}", route.strip())
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized


def _endpoint_key(route, method):
    return pipeline_common.endpoint_key(route, method, _normalize_route)


def _job_method(endpoint):
    return endpoint.get("http_method") or endpoint.get("method")


def _rekey_fragment(fragment, route, http_method) -> Optional[Dict]:
    """
    Validate an LLM swagger fragment and re-key it under the route the extractor
    found. The model normalizes paths its own way, so its keys are discarded and
    only the first operation body is kept. Returns None when the fragment is
    unusable.
    """
    route_key = _normalize_route(route)
    if not route_key:
        return None
    verb, payload = pipeline_common.first_operation(fragment)
    if payload is None:
        return None
    method_key = (http_method or verb or "get").lower()
    return {
        "paths": {
            route_key: {method_key: pipeline_common.normalize_operation_fields(payload)}
        }
    }


def _resolve_imported_definitions(import_item, route):
    origin = import_item.get("origin")
    imported_name = import_item.get("imported_name")
    if not origin or not imported_name:
        return []
    metadata = _load_file_metadata(origin)
    if not metadata:
        return []
    elements = metadata.get("elements", {})
    candidates = []
    name_candidates = [imported_name]
    if "." in imported_name:
        name_candidates.append(imported_name.split(".")[-1])
    for key in ("classes", "modules", "functions"):
        for item in elements.get(key, []):
            if item.get("name") not in name_candidates:
                continue
            start_line = item.get("start_line")
            end_line = item.get("end_line")
            if not isinstance(start_line, int) or not isinstance(end_line, int):
                continue
            candidates.append(
                {
                    "type": item.get("type") or key[:-1],
                    "name": item.get("name"),
                    "start_line": start_line,
                    "end_line": end_line,
                    "route": route,
                    "file_path": origin,
                }
            )
            break
        if candidates:
            break
    return candidates


def _ancestor_import_entry(entry: Dict, name: str, route) -> Optional[Dict]:
    file_path = entry.get("file_path")
    start_line = entry.get("start_line")
    end_line = entry.get("end_line")
    if not file_path or not isinstance(start_line, int) or not isinstance(end_line, int):
        return None
    return {
        "type": entry.get("type") or "class",
        "name": name,
        "start_line": start_line,
        "end_line": end_line,
        "route": route,
        "file_path": os.path.abspath(str(file_path)),
    }


def _ancestor_definition_imports(class_name: Optional[str], route) -> List[Dict]:
    """The dependency edges to the files an action's controller descends from.

    Ruby binds no name on require, so a parent controller and a concern are
    dependencies no require based edge ever records: without them, editing the
    parent leaves the child's endpoint looking untouched and it is never
    regenerated. Read off whatever class index the run built; without one there
    are no ancestors to record and the endpoint is dirtied by its own file
    alone.
    """
    imports: List[Dict] = []
    entry = _CLASS_INDEX_CACHE.get(class_name) if class_name else None
    visited = set()
    while entry:
        for module_name in entry.get("includes") or []:
            module_entry = _CLASS_INDEX_CACHE.get(module_name)
            if not module_entry or module_name in visited:
                continue
            visited.add(module_name)
            module_import = _ancestor_import_entry(module_entry, module_name, route)
            if module_import:
                imports.append(module_import)

        superclass = entry.get("superclass")
        if not superclass or superclass in visited:
            break
        visited.add(superclass)
        parent = _CLASS_INDEX_CACHE.get(superclass)
        if not parent:
            break
        parent_import = _ancestor_import_entry(parent, superclass, route)
        if parent_import:
            imports.append(parent_import)
        entry = parent
    return imports


def _endpoint_imports(endpoint, abs_file_path, route):
    imports = pipeline_common.endpoint_imports(
        endpoint,
        abs_file_path,
        route,
        _load_file_metadata,
        get_dependencies,
        _resolve_imported_definitions,
    )
    imports.extend(_ancestor_definition_imports(endpoint.get("class_name"), route))
    return imports


def _build_api_index(endpoints: list) -> dict:
    return pipeline_common.build_api_index(
        endpoints, _endpoint_key, _job_method, _endpoint_imports
    )


def _write_api_index(api_index: dict) -> None:
    pipeline_common.write_api_index(api_index, _api_index_output_path())


def _load_existing_swagger():
    return pipeline_common.load_existing_swagger(get_output_filepath(), _normalize_route)


def _load_existing_api_index():
    return pipeline_common.load_existing_api_index(
        _api_index_output_path(), _endpoint_key
    )


def _remove_endpoint_from_swagger(swagger: dict, key: str) -> None:
    pipeline_common.remove_endpoint_from_swagger(swagger, key, _normalize_route)


def _handler_lines(method_info: Dict) -> List[str]:
    """The handler's own source lines, read for pricing its batch section."""
    lines = _read_file_lines(method_info.get("file_path") or "") or []
    start_line = method_info.get("start_line") or 1
    end_line = method_info.get("end_line") or start_line
    return lines[start_line - 1 : end_line]


def _batch_section_tokens(method_info: Dict) -> int:
    """What this endpoint's section costs the batch."""
    return pipeline_common.section_token_cost(
        f"{_endpoint_label(method_info)}:",
        "".join(_handler_lines(method_info)),
        MAX_HANDLER_TOKENS,
    )


def _batch_endpoint_jobs(endpoint_jobs: List[Dict]) -> List[List[Tuple[Dict, List[Dict]]]]:
    """
    One batch per controller file, packed so the handler sections of a batch stay
    inside CONTEXT_TOKEN_BUDGET: capping each handler on its own still let ten of
    them add up to far more than the prompt can carry. A batch is closed as soon
    as the next section would push it past the budget, with
    MAX_ENDPOINTS_PER_BATCH as the secondary limit. A PUT that only mirrors a
    PATCH on the same route is never requested from the model: it rides along on
    the PATCH result. Each batch entry is (requested, mirrored).
    """
    batches: List[List[Tuple[Dict, List[Dict]]]] = []
    for jobs in pipeline_common.group_jobs_by_file(endpoint_jobs).values():
        patch_routes = {
            _normalize_route(job.get("route"))
            for job in jobs
            if (job.get("http_method") or "").upper() == "PATCH"
        }
        mirrors_by_route: Dict[str, List[Dict]] = {}
        requested: List[Dict] = []
        for job in jobs:
            route = _normalize_route(job.get("route"))
            if (job.get("http_method") or "").upper() == "PUT" and route in patch_routes:
                mirrors_by_route.setdefault(route, []).append(job)
                continue
            requested.append(job)
        entries = [
            (
                job,
                mirrors_by_route.get(_normalize_route(job.get("route")), [])
                if (job.get("http_method") or "").upper() == "PATCH"
                else [],
            )
            for job in requested
        ]
        batches.extend(
            pipeline_common.pack_batches(
                entries,
                lambda entry: _batch_section_tokens(entry[0]),
                MAX_ENDPOINTS_PER_BATCH,
                _EFFECTIVE_CONTEXT_BUDGET,
            )
        )
    return batches


def _apply_context_budget(
    handler_sections: List[Tuple[str, str]], shared_blocks: List, file_label: str
) -> Tuple[List[str], List[str]]:
    """The handler bodies and the shared context, inside CONTEXT_TOKEN_BUDGET."""
    return pipeline_common.apply_context_budget(
        handler_sections,
        shared_blocks,
        file_label,
        MAX_HANDLER_TOKENS,
        _EFFECTIVE_CONTEXT_BUDGET,
    )


def _endpoint_label(method_info: Dict) -> str:
    return _endpoint_key(method_info.get("route"), method_info.get("http_method"))


def _context_hash(context_blocks: List, method_definition: List) -> str:
    """The one context hash recipe every pipeline shares.

    A mirrored PUT lands on exactly its PATCH's hash because the verb is not
    part of it.
    """
    return pipeline_common.context_hash(
        context_blocks, method_definition, MAX_HANDLER_TOKENS
    )


def _collect_batch_context(directory_path: str, batch: List[Tuple[Dict, List[Dict]]]):
    """
    Read the context of every endpoint in the batch. An endpoint whose context
    cannot be read is reported back instead of taking the whole batch down.
    Returns (usable_entries, endpoints_list, shared_context, sections, failures).
    """
    usable_entries: List[Tuple[Dict, List[Dict]]] = []
    endpoint_lines: List[str] = []
    handler_sections: List[Tuple[str, str]] = []
    shared_blocks: List = []
    failures: List[Tuple[Tuple[Dict, List[Dict]], Exception]] = []
    for entry in batch:
        method_info = entry[0]
        try:
            context_blocks, method_definition = provide_context_codeblock(
                directory_path, method_info
            )
        except Exception as exc:
            failures.append((entry, exc))
            continue
        label = _endpoint_label(method_info)
        method_info["context_hash"] = _context_hash(context_blocks, method_definition)
        usable_entries.append(entry)
        endpoint_lines.append(label)
        handler_sections.append(
            (f"{label}:", pipeline_common.block_text(method_definition))
        )
        shared_blocks.extend(context_blocks)
    file_label = batch[0][0].get("file_path") or "unknown file"
    kept_blocks, sections = _apply_context_budget(
        handler_sections, shared_blocks, file_label
    )
    return (
        usable_entries,
        "\n".join(endpoint_lines),
        "\n\n".join(kept_blocks),
        "\n\n".join(sections),
        failures,
    )


def _generate_endpoint_fragment(directory_path: str, method_info: Dict) -> Dict:
    """The per endpoint call, with the same dedupe and token budget as a batch."""
    context_blocks, method_definition = provide_context_codeblock(
        directory_path, method_info
    )
    # Hashed before the verb and mirror hints go in, so the value matches the
    # one the batch path stores for the same endpoint.
    method_info["context_hash"] = _context_hash(context_blocks, method_definition)
    http_method = method_info.get("http_method")
    if http_method:
        context_blocks = [[f"HTTP_METHOD: {http_method}\n"]] + context_blocks
    kept_blocks, sections = _apply_context_budget(
        [("", pipeline_common.block_text(method_definition))],
        context_blocks,
        method_info.get("file_path") or "unknown file",
    )
    return get_function_definition_swagger(
        [sections[0] if sections else ""],
        [[block] for block in kept_blocks],
        method_info.get("route"),
        http_method=http_method,
    )


def _generate_fragments_per_endpoint(
    directory_path: str, entries: List[Tuple[Dict, List[Dict]]]
) -> List[Tuple[Dict, List[Dict], Optional[Dict], Optional[Exception]]]:
    """The pre-batch path: one call per endpoint, each failing on its own."""
    results = []
    for method_info, mirrors in entries:
        try:
            fragment = _generate_endpoint_fragment(directory_path, method_info)
        except Exception as exc:
            results.append((method_info, mirrors, None, exc))
            continue
        results.append((method_info, mirrors, fragment, None))
    return results


def _generate_batch_fragments(
    directory_path: str, batch: List[Tuple[Dict, List[Dict]]]
) -> List[Tuple[Dict, List[Dict], Optional[Dict], Optional[Exception]]]:
    """
    Document a whole batch with one call, retried once. Returns one
    (method_info, mirrors, fragment, error) tuple per requested endpoint; a
    fragment is None when the model left that endpoint out, which counts as a
    failure so the next run retries it. An unusable reply falls back to the per
    endpoint calls.
    """
    usable_entries, endpoints_list, shared_context, sections, failures = _collect_batch_context(
        directory_path, batch
    )
    results = [(entry[0], entry[1], None, error) for entry, error in failures]
    if not usable_entries:
        return results

    response = pipeline_common.retry_batch_call(
        lambda: get_batch_definition_swagger(endpoints_list, shared_context, sections)
    )
    if response is None:
        return results + _generate_fragments_per_endpoint(directory_path, usable_entries)

    # Two model keys can normalize to the same route, so the verbs are merged
    # instead of the second path item being dropped.
    paths_by_route: Dict[str, Dict] = {}
    for path_key, path_item in response["paths"].items():
        if not isinstance(path_item, dict):
            continue
        merged = paths_by_route.setdefault(_normalize_route(path_key), {})
        for verb, payload in path_item.items():
            merged.setdefault(verb, payload)

    for method_info, mirrors in usable_entries:
        route = _normalize_route(method_info.get("route"))
        method = (method_info.get("http_method") or "").lower()
        operation = None
        for key, payload in (paths_by_route.get(route) or {}).items():
            if key.lower() == method and isinstance(payload, dict):
                operation = payload
                break
        fragment = {"paths": {route: {method: operation}}} if operation is not None else None
        results.append((method_info, mirrors, fragment, None))
    return results


def _merge_batch_result(
    swagger: Dict, method_info: Dict, mirrors: List[Dict], raw_fragment: Optional[Dict]
) -> List[Dict]:
    """
    Re-key one generated fragment under the route the extractor found and merge
    it. A mirrored PUT gets its own deep copy of the PATCH operation body.
    Returns the endpoints that landed in the spec, empty when the fragment was
    unusable.
    """
    fragment = _rekey_fragment(
        raw_fragment, method_info.get("route"), method_info.get("http_method")
    )
    if fragment is None:
        return []
    _merge_paths(swagger, fragment)
    merged = [method_info]
    for mirror in mirrors:
        mirror_fragment = _rekey_fragment(
            copy.deepcopy(raw_fragment), mirror.get("route"), mirror.get("http_method")
        )
        if mirror_fragment is None:
            continue
        _merge_paths(swagger, mirror_fragment)
        # A mirror is never sent to the model, so it carries the hash of the
        # PATCH it was copied from: that is the context both keys were answered
        # from, and the value the next run recomputes for either of them.
        mirror["context_hash"] = method_info.get("context_hash")
        merged.append(mirror)
    return merged


def _update_swagger_for_endpoints(
    swagger: dict, directory_path: str, endpoints: list
) -> Tuple[List[Dict], List[Dict]]:
    """
    Generate the given endpoints in controller batches, guarded exactly like the
    full run: one failing batch must never abort the incremental pass.
    Returns (succeeded, failed) as lists of the endpoints themselves, mirrored
    PUTs included, so the caller can tell which index keys are safe to refresh.
    """
    succeeded: List[Dict] = []
    failed: List[Dict] = []
    routable = []
    for method_info in endpoints:
        if not method_info.get("route"):
            failed.append(method_info)
            continue
        routable.append(method_info)

    for batch in _batch_endpoint_jobs(routable):
        try:
            results = _generate_batch_fragments(directory_path, batch)
        except Exception as exc:
            for method_info, mirrors in batch:
                failed.extend([method_info] + mirrors)
                print(f"apimesh: skipped {_endpoint_label(method_info)}: {exc}")
            continue
        for method_info, mirrors, raw_fragment, error in results:
            if error is not None:
                failed.extend([method_info] + mirrors)
                print(f"apimesh: skipped {_endpoint_label(method_info)}: {error}")
                continue
            merged = _merge_batch_result(swagger, method_info, mirrors, raw_fragment)
            if not merged:
                failed.extend([method_info] + mirrors)
                print(
                    f"apimesh: skipped {_endpoint_label(method_info)}: "
                    "LLM response had no usable paths entry"
                )
                continue
            succeeded.extend(merged)
    return succeeded, failed


def _context_is_unchanged(directory_path: str, existing_entry, jobs: List[Dict]) -> bool:
    """
    True when the prompt this endpoint would be regenerated from is the one its
    stored hash was taken over, so the spec already holds the answer. An entry
    without a stored hash always regenerates, and so does one whose context
    cannot be read.
    """
    stored_hash = existing_entry.get("context_hash") if isinstance(existing_entry, dict) else None
    if not stored_hash:
        return False
    for method_info in jobs:
        try:
            context_blocks, method_definition = provide_context_codeblock(
                directory_path, method_info
            )
        except Exception:
            return False
        if _context_hash(context_blocks, method_definition) != stored_hash:
            return False
    return True


def _maybe_incremental_update(
    directory_path: str, endpoint_jobs: list, host: Optional[str] = None
):
    existing_swagger = _load_existing_swagger()
    existing_index = _load_existing_api_index()
    if not existing_swagger or not isinstance(existing_index, dict):
        return None
    base_commit = pipeline_common.base_commit_of(existing_swagger)
    if not base_commit:
        return None
    changed_files = get_changed_files_since(base_commit, directory_path, include_uncommitted=True)
    if changed_files is None:
        return None
    endpoint_map = pipeline_common.group_endpoints(
        endpoint_jobs, _endpoint_key, _job_method
    )
    existing_keys = set(existing_index.keys())
    new_keys = set(endpoint_map.keys())
    removed_keys = existing_keys - new_keys
    added_keys = new_keys - existing_keys
    # An endpoint that failed last run is absent from the index, so it reads as
    # added and still has to be generated when git reports nothing changed.
    if (
        not changed_files
        and not added_keys
        and not removed_keys
        and pipeline_common.index_paths_exist(existing_index)
    ):
        pipeline_common.record_coverage(existing_swagger, len(endpoint_jobs), 0, len(endpoint_jobs), 0)
        return pipeline_common.apply_host(existing_swagger, host)
    changed_keys = set()
    for key in existing_keys & new_keys:
        if pipeline_common.endpoint_has_changed(
            existing_index.get(key), endpoint_map.get(key), changed_files
        ):
            changed_keys.add(key)

    keys_to_update = added_keys | changed_keys
    if pipeline_common.should_regenerate_fully(keys_to_update, new_keys):
        return None
    updated_index = dict(existing_index)

    for key in removed_keys:
        updated_index.pop(key, None)
        _remove_endpoint_from_swagger(existing_swagger, key)

    # A file changing does not mean the prompt for every endpoint in it changed:
    # an endpoint whose context still hashes to the stored value keeps the spec
    # operation and the index entry it already has.
    # Every dirty endpoint left goes through the batch path in one pass:
    # generating them key by key put the endpoints of one changed controller in
    # a call each and left a dirty PATCH and PUT unable to share the one
    # generated body.
    jobs_to_update = []
    unchanged_keys = []
    for key in keys_to_update:
        jobs = endpoint_map.get(key, [])
        if jobs and _context_is_unchanged(directory_path, existing_index.get(key), jobs):
            unchanged_keys.append(key)
            continue
        jobs_to_update.extend(jobs)
    if unchanged_keys:
        print(
            f"apimesh: skipped {len(unchanged_keys)} unchanged endpoints (context hash match)"
        )
    pipeline_common.rebuild_unchanged_index_entries(
        unchanged_keys, endpoint_map, existing_index, updated_index, _build_api_index
    )
    succeeded, failed = _update_swagger_for_endpoints(
        existing_swagger, directory_path, jobs_to_update
    )
    # A failed endpoint has to stay dirty: refreshing its index entry would
    # make the next run see no change and never retry it. Leaving the entry
    # stale (or absent for a new endpoint) is what schedules the retry, and a
    # key is only refreshed when every endpoint behind it made it.
    failed_keys = {_endpoint_label(method_info) for method_info in failed}
    pipeline_common.apply_generated_index_entries(
        updated_index, _build_api_index(succeeded), failed_keys
    )

    pipeline_common.stamp_commit_reference(existing_swagger, get_git_commit_hash())
    _write_api_index(updated_index)
    pipeline_common.record_coverage(
        existing_swagger,
        len(endpoint_jobs),
        len(succeeded),
        max(len(endpoint_jobs) - len(succeeded) - len(failed), 0),
        len(failed),
    )
    return pipeline_common.apply_host(existing_swagger, host)


MAX_DROPPED_ROUTES_LISTED = 20


def _report_dropped_routes(dropped_routes: List[str]) -> None:
    """
    One line for every route whose action no controller (or ancestor) defines.
    They are dropped rather than documented from a neighbouring method's body,
    so the spec never carries an invented endpoint.
    """
    if not dropped_routes:
        return
    listed = ", ".join(dropped_routes[:MAX_DROPPED_ROUTES_LISTED])
    remaining = len(dropped_routes) - MAX_DROPPED_ROUTES_LISTED
    if remaining > 0:
        listed = f"{listed}, +{remaining} more"
    print(
        f"apimesh: dropped {len(dropped_routes)} routed actions "
        f"with no controller method: {listed}"
    )


def run_swagger_generation(host: str) -> Optional[Dict]:
    _reset_caches()
    directory_path = get_repo_path()
    repo_name = get_repo_name()
    # Built before the try: a walk that dies leaves an incomplete picture of
    # which entries are still live, and pruning against it would be wrong.
    _build_metadata_cache(directory_path)

    try:
        # Built here, once, while the run is still single threaded: the workers
        # below read it and a lazy build under five of them races.
        class_index = _ensure_class_index(directory_path)

        api_definition_files = find_api_definition_files(directory_path)
        all_endpoints_dict: Dict[str, List[Dict]] = {}
        route_map: Dict[str, List[Dict]] = {}
        controller_files: List[Path] = []
        route_files: List[Path] = []
        dropped_routes: List[str] = []

        for file in api_definition_files:
            ruby_file = Path(file)
            if is_route_file(ruby_file):
                route_files.append(ruby_file)
            else:
                controller_files.append(ruby_file)

        # Read off every route file before the first one is walked: a concern
        # config/routes.rb defines is referenced from config/routes/admin.rb,
        # and either file can be the one reached first.
        route_concerns = collect_route_concerns(route_files)
        for route_file in route_files:
            find_api_endpoints(
                route_file, directory_path, route_map, concerns=route_concerns
            )

        for controller_file in controller_files:
            endpoints = find_api_endpoints(
                controller_file,
                directory_path,
                route_map,
                class_index,
                dropped_routes,
            )
            if endpoints:
                all_endpoints_dict[str(controller_file)] = endpoints

        _report_dropped_routes(dropped_routes)

        swagger = {
            "openapi": "3.0.0",
            "info": {
                "title": repo_name,
                "version": "1.0.0",
                "description": "This Swagger file was generated using OpenAI GPT.",
                "x-generated-at": datetime.datetime.utcnow().isoformat() + "Z",
                "x-commit-reference": get_git_commit_hash(),
                "x-github-repo-url": get_github_repo_url(),
            },
            "servers": [{"url": host}],
            "paths": {},
        }

        endpoint_jobs: List[Dict] = []
        for _, endpoints in all_endpoints_dict.items():
            for endpoint in endpoints:
                if endpoint["type"] == "class":
                    endpoint_jobs.extend(endpoint.get("methods", []))
                else:
                    endpoint_jobs.append(endpoint)

        # Checked before the incremental pass: an empty extraction there would be
        # read as "every endpoint was deleted" and wipe the index.
        if not endpoint_jobs:
            print(_EMPTY_EXTRACTION_WARNING)
            return None

        incremental_swagger = _maybe_incremental_update(directory_path, endpoint_jobs, host)
        if incremental_swagger is not None:
            return incremental_swagger
        failures: List[str] = []
        generated: List[Dict] = []
        batches = _batch_endpoint_jobs(endpoint_jobs)
        with ThreadPoolExecutor(max_workers=5) as executor:
            start_time = time.time()
            latest_message = ""
            futures = {
                executor.submit(_generate_batch_fragments, directory_path, batch): batch
                for batch in batches
            }
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    results = future.result()
                except Exception as exc:
                    for method_info, mirrors in batch:
                        for endpoint in [method_info] + mirrors:
                            failures.append(f"{_endpoint_label(endpoint)}: {exc}")
                    continue
                for method_info, mirrors, raw_fragment, error in results:
                    if error is not None:
                        for endpoint in [method_info] + mirrors:
                            failures.append(f"{_endpoint_label(endpoint)}: {error}")
                        continue
                    merged = _merge_batch_result(
                        swagger, method_info, mirrors, raw_fragment
                    )
                    if not merged:
                        for endpoint in [method_info] + mirrors:
                            failures.append(
                                f"{_endpoint_label(endpoint)}: "
                                "LLM response had no usable paths entry"
                            )
                        continue
                    generated.extend(merged)
                    end_time = time.time()
                    latest_message = (
                        f"Completed generating endpoint related information for {len(generated)} endpoints in "
                        f"{int(end_time - start_time)} seconds"
                    )
                    print(latest_message, end="\r", flush=True)
            if generated:
                print(latest_message)

        for failure in failures:
            print(f"apimesh: skipped endpoint {failure}")
        print(
            f"generated {len(generated)} of {len(endpoint_jobs)} endpoints "
            f"({len(failures)} failed)"
        )
        if not generated:
            raise RuntimeError(
                "apimesh: rails parser generated 0 endpoints, "
                "falling back to generic extraction"
            )

        # Only endpoints that made it into the spec are indexed, otherwise a
        # failure looks unchanged next run and is never retried.
        _write_api_index(_build_api_index(generated))
        pipeline_common.record_coverage(
            swagger, len(endpoint_jobs), len(generated), 0, len(failures),
            dropped=len(dropped_routes) if dropped_routes is not None else None,
        )

        return swagger
    finally:
        # The cache outlives the run; only entries for content that is gone are
        # dropped, so the next run parses just what changed.
        _prune_metadata_cache()


def _merge_paths(target: Dict, source: Dict) -> None:
    """
    Merge the path map from the LLM response into the aggregated swagger document.
    """
    pipeline_common.merge_paths(target, source)


def _reset_caches() -> None:
    """Drop everything held between runs.

    The index is keyed by repo path alone, so a second run over the same path
    kept the first run's classes, line ranges and file bodies and documented
    endpoints from source that is no longer there.
    """
    global _CLASS_INDEX_CACHE
    global _CLASS_INDEX_CACHE_ROOT
    global _CLASS_CODE_BLOCK_CACHE
    global _FUNCTION_INDEX_CACHE
    _CLASS_INDEX_CACHE = {}
    _CLASS_INDEX_CACHE_ROOT = None
    _CLASS_CODE_BLOCK_CACHE = {}
    _FUNCTION_INDEX_CACHE = {}
    _FILE_CONTENT_CACHE.clear()
    _CONTENT_HASHES.clear()
    _METADATA_ENTRIES.clear()


def _ensure_class_index(directory_path: str) -> Dict[str, Dict[str, object]]:
    global _CLASS_INDEX_CACHE
    global _CLASS_INDEX_CACHE_ROOT
    global _CLASS_CODE_BLOCK_CACHE
    global _FUNCTION_INDEX_CACHE
    if _CLASS_INDEX_CACHE and _CLASS_INDEX_CACHE_ROOT == directory_path:
        return _CLASS_INDEX_CACHE

    _CLASS_INDEX_CACHE = {}
    _CLASS_CODE_BLOCK_CACHE = {}
    _FILE_CONTENT_CACHE.clear()
    _FUNCTION_INDEX_CACHE = {}
    _CLASS_INDEX_CACHE_ROOT = directory_path

    # Only the entries this run touched: the cache also holds entries for
    # content that is no longer anywhere in the repo.
    for cache_path in _METADATA_ENTRIES.values():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        source_file = data.get("filename")
        if not source_file:
            continue

        elements = data.get("elements", {})
        classes = elements.get("classes", [])
        modules = elements.get("modules", [])
        functions = elements.get("functions", [])

        for klass in classes:
            name = klass.get("name")
            if not name or name in _CLASS_INDEX_CACHE:
                continue
            _CLASS_INDEX_CACHE[name] = {
                "type": "class",
                "file_path": source_file,
                "superclass": klass.get("superclass"),
                "includes": klass.get("includes") or [],
                "start_line": klass.get("start_line"),
                "end_line": klass.get("end_line"),
                "methods": _contained_method_map(
                    functions, klass.get("start_line"), klass.get("end_line")
                ),
            }

        # Modules are indexed alongside the classes because a controller concern
        # carries actions, and Ruby resolves them off the same constant name.
        for module in modules:
            name = module.get("name")
            if not name or name in _CLASS_INDEX_CACHE:
                continue
            _CLASS_INDEX_CACHE[name] = {
                "type": "module",
                "file_path": source_file,
                "superclass": None,
                "includes": [],
                "start_line": module.get("start_line"),
                "end_line": module.get("end_line"),
                "methods": _contained_method_map(
                    functions, module.get("start_line"), module.get("end_line")
                ),
            }

        for func in functions:
            func_name = func.get("name")
            start_line = func.get("start_line")
            end_line = func.get("end_line")
            if (
                not func_name
                or not isinstance(start_line, int)
                or not isinstance(end_line, int)
            ):
                continue
            _FUNCTION_INDEX_CACHE.setdefault(func_name, []).append(
                {
                    "file_path": source_file,
                    "start_line": start_line,
                    "end_line": end_line,
                }
            )

    return _CLASS_INDEX_CACHE


def _contained_method_map(functions: List[Dict], start_line, end_line) -> Dict[str, Dict[str, int]]:
    """The methods a class or module body holds, by name."""
    method_map: Dict[str, Dict[str, int]] = {}
    if not isinstance(start_line, int) or not isinstance(end_line, int):
        return method_map
    for func in functions:
        method_name = func.get("name")
        func_start = func.get("start_line")
        func_end = func.get("end_line")
        if (
            method_name
            and isinstance(func_start, int)
            and isinstance(func_end, int)
            and start_line <= func_start <= end_line
        ):
            method_map[method_name] = {
                "start_line": func_start,
                "end_line": func_end,
            }
    return method_map


def _collect_parent_class_names(directory_path: str, class_name: Optional[str]) -> List[str]:
    if not class_name:
        return []
    class_index = _ensure_class_index(directory_path)
    parents: List[str] = []
    visited: set = set()
    current = class_name

    while current:
        entry = class_index.get(current)
        if not entry:
            break
        superclass = entry.get("superclass")
        if not superclass or superclass in visited:
            break
        parent_entry = class_index.get(superclass)
        if not parent_entry:
            break
        parents.append(superclass)
        visited.add(superclass)
        current = superclass

    return parents


def _get_class_code_block(directory_path: str, class_name: str) -> Optional[List[str]]:
    class_index = _ensure_class_index(directory_path)
    entry = class_index.get(class_name)
    if not entry:
        return None

    cache_key = f"{directory_path}:{class_name}"
    cached_block = _CLASS_CODE_BLOCK_CACHE.get(cache_key)
    if cached_block is not None:
        return cached_block

    file_path = entry.get("file_path")
    start_line = entry.get("start_line")
    end_line = entry.get("end_line")
    if not file_path or not isinstance(start_line, int) or not isinstance(end_line, int):
        return None

    lines = _read_file_lines(file_path)
    if lines is None:
        return None

    block = lines[start_line - 1 : end_line]
    _CLASS_CODE_BLOCK_CACHE[cache_key] = block
    return block


def _read_file_lines(file_path: str) -> Optional[List[str]]:
    cached = _FILE_CONTENT_CACHE.get(file_path)
    if cached is not None:
        return cached
    try:
        # errors='replace': the parser reads bytes, so a controller that is not
        # utf-8 has metadata and endpoints. Raising here dropped every one of
        # them instead of documenting them with a replacement character.
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None
    _FILE_CONTENT_CACHE[file_path] = lines
    return lines


def _ancestor_method_names(directory_path: str, parent_names: List[str]) -> List[str]:
    class_index = _ensure_class_index(directory_path)
    names: List[str] = []
    for parent_name in parent_names:
        entry = class_index.get(parent_name)
        methods = entry.get("methods") if entry else None
        if isinstance(methods, dict):
            names.extend(methods.keys())
    return names


def _collect_parent_class_blocks(
    directory_path: str, parent_names: List[str]
) -> List[List[str]]:
    blocks: List[List[str]] = []
    for parent_name in parent_names:
        block = _get_class_code_block(directory_path, parent_name)
        if block:
            blocks.append(block)
    return blocks


def _extract_params_from_lines(lines: List[str]) -> List[str]:
    params: List[str] = []
    for line in lines:
        for match in _PARAM_PATTERN.finditer(line):
            params.append(match.group(1))
    return params


def _build_helper_param_hint_block(
    directory_path: str,
    parent_names: List[str],
    method_definition_block: List[str],
) -> Optional[List[str]]:
    if not parent_names or not method_definition_block:
        return None
    method_text = "".join(method_definition_block)
    if not method_text.strip():
        return None

    class_index = _ensure_class_index(directory_path)
    helper_params: Dict[str, List[str]] = {}

    for parent_name in parent_names:
        entry = class_index.get(parent_name)
        if not entry:
            continue
        methods = entry.get("methods", {})
        file_path = entry.get("file_path")
        if not isinstance(methods, dict) or not file_path:
            continue

        lines = _read_file_lines(file_path)
        if lines is None:
            continue

        for helper_name, meta in methods.items():
            if not helper_name or not re.search(rf"\b{re.escape(helper_name)}\b", method_text):
                continue
            start_line = meta.get("start_line")
            end_line = meta.get("end_line")
            if not isinstance(start_line, int) or not isinstance(end_line, int):
                continue
            helper_lines = lines[start_line - 1 : end_line]
            params = _extract_params_from_lines(helper_lines)
            if params:
                helper_params.setdefault(helper_name, [])
                helper_params[helper_name].extend(params)

    if not helper_params:
        return None

    block = [
        "# Helper-derived request parameters identified from ancestor controllers.\n",
        "# Use these parameter names when documenting request inputs instead of the helper method names.\n",
    ]
    for helper_name in sorted(helper_params.keys()):
        param_values = sorted({name for name in helper_params[helper_name] if name})
        if not param_values:
            continue
        block.append(
            f"# {helper_name}: params -> {', '.join(param_values)}\n"
        )

    if len(block) <= 2:
        return None

    return block


def _build_direct_param_hint_block(
    method_definition_block: List[str],
) -> Optional[List[str]]:
    if not method_definition_block:
        return None
    params = _extract_params_from_lines(method_definition_block)
    unique_params = sorted({name for name in params if name})
    if not unique_params:
        return None

    block = [
        "# Request parameters referenced directly in this action.\n",
        "# Document these params in the request schema.\n",
    ]
    for name in unique_params:
        block.append(f"# param: {name}\n")
    return block


def _collect_special_function_blocks(
    directory_path: str,
    function_names: List[str],
    per_name_limit: int = 2,
) -> List[List[str]]:
    if not function_names:
        return []
    _ensure_class_index(directory_path)
    blocks: List[List[str]] = []
    seen_entries = set()
    for func_name in function_names:
        if func_name not in _PARAM_HINT_FUNCTIONS:
            continue
        entries = _FUNCTION_INDEX_CACHE.get(func_name, [])
        for entry in entries[:per_name_limit]:
            file_path = entry.get("file_path")
            start_line = entry.get("start_line")
            end_line = entry.get("end_line")
            if (
                not file_path
                or not isinstance(start_line, int)
                or not isinstance(end_line, int)
            ):
                continue
            cache_key = (file_path, start_line, end_line)
            if cache_key in seen_entries:
                continue
            seen_entries.add(cache_key)
            lines = _read_file_lines(file_path)
            if lines is None:
                continue
            block = [
                f"# Definition of {func_name} from {file_path}:{start_line}-{end_line}\n"
            ]
            block.extend(lines[start_line - 1 : end_line])
            blocks.append(block)
    return blocks


def _bare_identifier_names(method_lines: List[str], candidate_names) -> List[str]:
    """
    Which of candidate_names the action calls as a bare identifier. Ruby parses
    a no-arg helper call (`set_widget`, `authenticate_user!`) as an identifier,
    not as a call, so the call based pass never sees it and the helper's body
    never reaches the prompt.
    """
    text = "".join(method_lines or [])
    if not text.strip():
        return []
    found: List[str] = []
    for name in candidate_names:
        if not name or name in _NON_HELPER_NAMES:
            continue
        # Not a symbol argument, not a method on another object, not an ivar.
        if re.search(rf"(?<![\w.:@]){re.escape(name)}(?!\w)", text):
            found.append(name)
    return found


def _bare_identifier_dependencies(
    data: Dict,
    method_lines: List[str],
    start_line: int,
    end_line: int,
    file_path: str,
    collected: List[Dict],
) -> List[Dict]:
    """The same-file helpers an action calls bare, as in-file dependencies."""
    definitions: Dict[str, Dict] = {}
    for item in data["elements"]["functions"]:
        name = item.get("name")
        func_start = item.get("start_line")
        func_end = item.get("end_line")
        if not name or not isinstance(func_start, int) or not isinstance(func_end, int):
            continue
        # The action itself is already the prompt's method definition.
        if func_start >= start_line and func_end <= end_line:
            continue
        definitions.setdefault(name, item)

    already = {dependency.get("name") for dependency in collected}
    resolved: List[Dict] = []
    for name in _bare_identifier_names(method_lines, definitions):
        if name in already:
            continue
        already.add(name)
        definition = definitions[name]
        resolved.append(
            {
                "type": "function_call",
                "name": name,
                "start_line": start_line,
                "end_line": end_line,
                "function_start_line": definition["start_line"],
                "function_end_line": definition["end_line"],
                "file_path": file_path,
            }
        )
    return resolved


def get_dependencies(
    data: Dict,
    start_line: int,
    end_line: int,
    file_path: str,
    method_lines: Optional[List[str]] = None,
) -> Tuple[List[Dict], List[Dict]]:
    existing_function_names = [
        item["name"]
        for item in data["elements"]["functions"]
        if item["name"] not in {"get", "post", "put", "delete", "patch"}
    ]
    in_file_dependency_functions: List[Dict] = []
    for item in data["elements"]["function_calls"]:
        if (
            item["name"] in existing_function_names
            and item["start_line"] >= start_line
            and item["end_line"] <= end_line
        ):
            item["file_path"] = file_path
            in_file_dependency_functions.append(item)

    in_file_dependency_functions.extend(
        _bare_identifier_dependencies(
            data,
            method_lines,
            start_line,
            end_line,
            file_path,
            in_file_dependency_functions,
        )
    )

    imported_functions: List[Dict] = []
    for item in data.get("imports", []):
        if not item.get("path_exists"):
            continue
        for usage_line in item.get("usage_lines", []):
            if start_line <= usage_line <= end_line:
                imported_functions.append(item)
            for dep in in_file_dependency_functions:
                if dep["start_line"] <= usage_line <= dep["end_line"]:
                    if item not in imported_functions:
                        imported_functions.append(item)
    return in_file_dependency_functions, imported_functions


def get_code_blocks(
    in_file_dependency_functions: List[Dict],
    imported_functions: List[Dict],
    file_name: str,
    directory_path: str,
) -> List[List[str]]:
    code_blocks: List[List[str]] = []
    try:
        with open(file_name, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        lines = []

    for block in in_file_dependency_functions:
        if lines:
            start = max(block.get("function_start_line", 1) - 1, 0)
            end = block.get("function_end_line", start + 1)
            code_blocks.append(lines[start:end])

    for func in imported_functions:
        origin = func.get("origin")
        if not origin:
            continue
        complete_json_file_path = _metadata_cache_path(str(origin))
        if not complete_json_file_path or not os.path.exists(complete_json_file_path):
            continue

        try:
            with open(complete_json_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        origin_file_name = origin
        try:
            with open(origin_file_name, "r", encoding="utf-8", errors="replace") as f:
                origin_lines = f.readlines()
        except OSError:
            origin_lines = []

        visited = False
        for item in data["elements"]["classes"]:
            if item["name"] == func["imported_name"]:
                visited = True
                if origin_lines:
                    code_blocks.append(
                        origin_lines[item["start_line"] - 1 : item["end_line"]]
                    )
                break
        if visited:
            continue

        for item in data["elements"]["functions"]:
            if item["name"] == func["imported_name"]:
                visited = True
                if origin_lines:
                    code_blocks.append(
                        origin_lines[item["start_line"] - 1 : item["end_line"]]
                    )
                break
        if visited:
            continue

        for item in data["elements"].get("modules", []):
            if item["name"] == func["imported_name"]:
                if origin_lines:
                    code_blocks.append(
                        origin_lines[item["start_line"] - 1 : item["end_line"]]
                    )
                break

    return code_blocks


def provide_context_codeblock(directory_path: str, method_info: Dict):
    file_name = method_info["file_path"]
    try:
        with open(file_name, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        lines = []

    method_definition_code_block = lines[
        method_info["start_line"] - 1 : method_info["end_line"]
    ]

    data = _load_file_metadata(str(file_name)) or {
        "elements": {"functions": [], "function_calls": []},
        "imports": [],
    }

    in_file_dependency_functions, imported_functions = get_dependencies(
        data,
        method_info["start_line"],
        method_info["end_line"],
        method_info["file_path"],
        method_definition_code_block,
    )
    context_code_blocks = get_code_blocks(
        in_file_dependency_functions, imported_functions, file_name, directory_path
    )
    parent_names = _collect_parent_class_names(
        directory_path, method_info.get("class_name")
    )
    parent_class_blocks = _collect_parent_class_blocks(directory_path, parent_names)
    function_calls_in_method: List[str] = []
    for call in data["elements"].get("function_calls", []):
        call_start = call.get("start_line")
        call_end = call.get("end_line")
        if not isinstance(call_start, int) or not isinstance(call_end, int):
            continue
        if (
            call_start >= method_info["start_line"]
            and call_end <= method_info["end_line"]
        ):
            call_name = call.get("name")
            if call_name:
                function_calls_in_method.append(call_name)
    # A helper an ancestor defines is called bare too, so the identifiers of the
    # action are matched against the ancestor chain as well.
    function_calls_in_method.extend(
        _bare_identifier_names(
            method_definition_code_block,
            _ancestor_method_names(directory_path, parent_names),
        )
    )
    special_function_blocks = _collect_special_function_blocks(
        directory_path, function_calls_in_method
    )
    direct_param_block = _build_direct_param_hint_block(
        method_definition_code_block
    )
    helper_hint_block = _build_helper_param_hint_block(
        directory_path, parent_names, method_definition_code_block
    )
    prefix_blocks: List[List[str]] = []
    if direct_param_block:
        prefix_blocks.append(direct_param_block)
    if helper_hint_block:
        prefix_blocks.append(helper_hint_block)
    prefix_blocks.extend(special_function_blocks)
    prefix_blocks.extend(parent_class_blocks)
    context_code_blocks = prefix_blocks + context_code_blocks
    return context_code_blocks, method_definition_code_block

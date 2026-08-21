import os
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pipeline_common
from config import Configurations
from java_pipeline.definition_swagger_generator import (
    HANDLER_TOKEN_BUDGET,
    get_batch_definition_swagger,
    get_function_definition_swagger,
    section_token_cost,
)
from java_pipeline.find_api_definition_files import find_api_definition_files
from java_pipeline.generate_file_information import process_file, reset_source_index
from java_pipeline.identify_api_functions import (
    find_api_endpoints,
    extraction_drops,
    index_scanned_types,
    log_extraction_drops,
    reset_extraction_drops,
    reset_type_index,
)
from utils import (
    get_git_commit_hash,
    get_github_repo_url,
    get_repo_path,
    get_repo_name,
    get_output_filepath,
    get_changed_files_since,
)

config = Configurations()

_EFFECTIVE_CONTEXT_BUDGET = pipeline_common.EFFECTIVE_CONTEXT_BUDGET

_FILE_CONTENT_CACHE: Dict[str, List[str]] = {}
# File path -> content hash, and file path -> the cache entry holding its
# metadata. Both are per run: two runs in one process are two checkouts.
_CONTENT_HASHES: Dict[str, Optional[str]] = {}
_METADATA_ENTRIES: Dict[str, str] = {}


def should_process_directory(dir_path: str, repo_root: str) -> bool:
    # Only components below the scanned repo may be ignored, otherwise a repo
    # living under /var, /tmp or /build processes nothing.
    path_parts = os.path.relpath(dir_path, repo_root).split(os.sep)
    return not any(part in config.ignored_dirs for part in path_parts)


def _api_index_output_path() -> str:
    return pipeline_common.api_index_output_path(get_output_filepath())


# Per file metadata is cached under the output directory, never inside the
# scanned repo: a run must not create or delete anything in the user's tree.
METADATA_CACHE_PIPELINE = "java"

# Bumped when the shape of a metadata entry changes. A missing or stale marker
# wipes this pipeline's cache before anything reads it.
METADATA_CACHE_VERSION = "1"


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
        on_error=lambda path, exc: print(
            f"apimesh: skipping metadata for {path}: {exc}"
        ),
    )


def _build_metadata_cache(directory_path: str) -> None:
    """One cache entry per java file below the repo root."""
    pipeline_common.build_metadata_cache(
        directory_path,
        _prepare_metadata_cache,
        should_process_directory,
        lambda file_path: file_path.endswith(".java"),
        _cache_file_metadata,
    )


def _prune_metadata_cache() -> None:
    pipeline_common.prune_metadata_cache(_metadata_cache_dir(), _METADATA_ENTRIES)


def _load_file_metadata(file_path: str):
    return pipeline_common.load_file_metadata(_metadata_cache_path(file_path))


def _normalize_route(route) -> str:
    """
    Canonical path for a route the extractor found. Spring already writes its
    path variables as {id}, so there is nothing to rewrite: the swagger key, the
    api_index key and the removal lookup only need one leading slash and no
    doubled separators to stay matched across runs.
    """
    if not route or not isinstance(route, str):
        return ""
    normalized = route.strip()
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _endpoint_key(route, method):
    return pipeline_common.endpoint_key(route, method, _normalize_route)


def _job_method(endpoint):
    return endpoint.get("method") or endpoint.get("http_method")


# The mapping conditions the prompt reports, in the order it reports them.
_CONDITION_ORDER = ("consumes", "produces", "params", "headers")


def _job_variants(job: Dict) -> List[Dict]:
    """The handlers one job documents: itself, plus the variants merged onto it."""
    return [job] + list(job.get("variants") or [])


def _conditions_note(job: Dict) -> str:
    """The one line telling the model what narrows this mapping."""
    conditions = job.get("conditions") or {}
    return ", ".join(
        f"{name}={', '.join(conditions[name])}"
        for name in _CONDITION_ORDER
        if conditions.get(name)
    )


def _imported_symbol_names(import_item) -> List[str]:
    """The types the handler actually reached for through this import.

    A wildcard import binds a whole package, so the usage sites are what says
    which of its types this handler needs.
    """
    used = [name for name in import_item.get("used_names") or [] if name]
    if used:
        return used
    imported_name = import_item.get("imported_name")
    if not imported_name or imported_name == "*":
        return []
    return [imported_name]


def _origin_paths(origin: str) -> List[str]:
    """The java files an import origin covers: a package directory or one file."""
    if os.path.isdir(origin):
        return [
            os.path.join(origin, name)
            for name in sorted(os.listdir(origin))
            if name.endswith(".java")
        ]
    return [origin] if origin.endswith(".java") else []


def _resolve_imported_definitions(import_item, route):
    origin = import_item.get("origin")
    if not origin:
        return []
    name_candidates = _imported_symbol_names(import_item)
    if not name_candidates:
        return []

    found: Dict[str, Dict] = {}
    for origin_path in _origin_paths(origin):
        if len(found) == len(name_candidates):
            break
        metadata = _load_file_metadata(origin_path)
        if not metadata:
            continue
        elements = metadata.get("elements", {})
        for key in ("classes", "functions", "variables"):
            for item in elements.get(key, []):
                name = item.get("name")
                if name not in name_candidates or name in found:
                    continue
                start_line = item.get("start_line")
                end_line = item.get("end_line")
                if not isinstance(start_line, int) or not isinstance(end_line, int):
                    continue
                found[name] = {
                    "type": item.get("type") or key[:-1],
                    "name": name,
                    "start_line": start_line,
                    "end_line": end_line,
                    "route": route,
                    "file_path": origin_path,
                }
    return list(found.values())


def _endpoint_imports(endpoint, abs_file_path, route):
    return pipeline_common.endpoint_imports(
        endpoint,
        abs_file_path,
        route,
        _load_file_metadata,
        get_dependencies,
        _resolve_imported_definitions,
    )


def _indexed_endpoints(endpoints: list) -> list:
    """Every merged variant, so an edit to any of their files dirties the key."""
    indexed = []
    for job in endpoints:
        context_hash = job.get("context_hash")
        for variant in _job_variants(job):
            if variant is job:
                indexed.append(job)
                continue
            entry = dict(variant)
            if context_hash:
                entry["context_hash"] = context_hash
            indexed.append(entry)
    return indexed


def _build_api_index(endpoints: list) -> dict:
    return pipeline_common.build_api_index(
        _indexed_endpoints(endpoints), _endpoint_key, _job_method, _endpoint_imports
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


def _normalize_swagger_fragment(
    fragment, route: str, http_method: Optional[str]
) -> Optional[Dict]:
    """Re-key an LLM fragment under the route and method the extractor found.

    The model happily rewrites paths, which would diverge from the api_index
    keys and make incremental removal impossible, so only the first operation
    body is kept and re-keyed.
    """
    route_key = _normalize_route(route)
    if not route_key:
        return None
    _, operation = pipeline_common.first_operation(fragment)
    if operation is None:
        return None
    return {
        route_key: {
            (http_method or "GET").lower(): pipeline_common.normalize_operation_fields(
                operation
            )
        }
    }


def _generate_swagger_fragment(directory_path: str, method_info: Dict) -> Dict:
    context_blocks, method_definition = provide_context_codeblock(
        directory_path, method_info
    )
    http_method = _job_method(method_info) or "GET"
    context_blocks = [[f"HTTP_METHOD: {http_method}\n"]] + context_blocks
    handler_name = method_info.get("name")
    if handler_name:
        context_blocks = [[f"HANDLER: {handler_name}\n"]] + context_blocks
    return get_function_definition_swagger(
        method_definition,
        context_blocks,
        method_info["route"],
        http_method,
        source_file=method_info.get("file_path"),
    )


def _swagger_fragment_for_endpoint(directory_path: str, method_info: Dict) -> Optional[Dict]:
    """One endpoint's swagger fragment, or None when it could not be generated."""
    route = method_info.get("route")
    http_method = _job_method(method_info) or "GET"
    if not route:
        return None
    try:
        raw_fragment = _generate_swagger_fragment(directory_path, method_info)
    except Exception as exc:
        print(f"apimesh: skipped {http_method} {route}: {exc}")
        return None
    normalized = _normalize_swagger_fragment(raw_fragment, route, http_method)
    if normalized is None:
        print(
            f"apimesh: skipped {http_method} {route}: "
            "response had no usable swagger paths"
        )
        return None
    # The fallback stores the batch-shaped hash, not the hash of its own prompt:
    # the next run recomputes the batch shape, and a hash it can never match
    # would make this endpoint regenerate forever.
    context_hash = _endpoint_context_hash(directory_path, method_info)
    if context_hash:
        method_info["context_hash"] = context_hash
    return normalized


def _update_swagger_for_endpoints(
    swagger: dict, directory_path: str, endpoints: list
) -> Tuple[List[Dict], List[Dict]]:
    """Generate every endpoint behind its own error boundary.

    One failing LLM call used to abort the whole incremental run, which dropped
    the CLI into its slow legacy fallback.
    """
    generated: List[Dict] = []
    failed: List[Dict] = []
    for method_info in endpoints:
        normalized = _swagger_fragment_for_endpoint(directory_path, method_info)
        if normalized is None:
            failed.append(method_info)
            continue
        pipeline_common.merge_paths(swagger, {"paths": normalized})
        generated.append(method_info)
    return generated, failed


def _variant_lines(job: Dict) -> List[str]:
    """One handler's own source lines."""
    lines = _read_file_lines(job.get("file_path") or "") or []
    start_line = job.get("start_line") or 1
    end_line = job.get("end_line") or start_line
    return lines[start_line - 1 : end_line]


def _handler_lines(job: Dict) -> List[str]:
    """The handler source this job's prompt section carries.

    A job that merged content negotiation variants carries all of their bodies,
    each under the condition that picks it, so the model documents them as the
    one operation Spring serves them as.
    """
    variants = _job_variants(job)
    lines: List[str] = []
    for variant in variants:
        note = _conditions_note(variant)
        if note or len(variants) > 1:
            lines.append(f"// Mapping conditions: {note or 'none'}\n")
        lines.extend(_variant_lines(variant))
    return lines


def _batch_section_tokens(job: Dict) -> int:
    """What this endpoint's section costs the batch."""
    return section_token_cost(_batch_label(job), "".join(_handler_lines(job)))


def _batch_endpoint_jobs(endpoint_jobs: List[Dict]) -> List[List[Dict]]:
    """Endpoint jobs grouped by source file, packed to fit the context budget.

    Capping each handler on its own still let ten of them add up to far more
    than the whole prompt can carry, so a batch is closed as soon as the next
    section would push its sections past the budget. Ten endpoints stays the
    secondary limit.
    """
    batches: List[List[Dict]] = []
    for jobs in pipeline_common.group_jobs_by_file(endpoint_jobs).values():
        batches.extend(
            pipeline_common.pack_batches(
                jobs,
                _batch_section_tokens,
                pipeline_common.MAX_ENDPOINTS_PER_BATCH,
                _EFFECTIVE_CONTEXT_BUDGET,
            )
        )
    return batches


def _batch_source_file(batch: List[Dict]) -> str:
    if not batch:
        return "unknown file"
    return batch[0].get("file_path") or "unknown file"


def _batch_label(job: Dict) -> str:
    """The METHOD PATH line the model is asked to echo back as its key."""
    method = (_job_method(job) or "GET").upper()
    return f"{method} {_normalize_route(job.get('route'))}"


def _batch_entry(directory_path: str, job: Dict) -> Tuple[str, List[str], List[List[str]]]:
    """One endpoint's prompt material: its label, its handler body, its context."""
    blocks, method_definition = provide_context_codeblock(directory_path, job)
    return _batch_label(job), method_definition, blocks


def _context_hash(context_blocks, method_definition) -> str:
    """The one context hash recipe every pipeline shares."""
    return pipeline_common.context_hash(
        context_blocks, method_definition, HANDLER_TOKEN_BUDGET
    )


def _endpoint_context_hash(directory_path: str, job: Dict) -> Optional[str]:
    """This endpoint's context hash, or None when its source cannot be read."""
    try:
        _, body, blocks = _batch_entry(directory_path, job)
    except Exception:
        return None
    return _context_hash(blocks, body)


def _generate_batch_payload(directory_path: str, batch: List[Dict]) -> Optional[Dict]:
    """One LLM call for a file's endpoints. Returns the model's raw payload."""
    entries: List[Tuple[str, str]] = []
    context_blocks: List[List[str]] = []
    for job in batch:
        label, body, blocks = _batch_entry(directory_path, job)
        job["context_hash"] = _context_hash(blocks, body)
        entries.append((label, "".join(body)))
        context_blocks.extend(blocks)
    return get_batch_definition_swagger(
        entries, context_blocks, _batch_source_file(batch)
    )


def _operation_from_batch(
    payload, route: str, http_method: Optional[str]
) -> Optional[Dict]:
    """The operation the model returned for one requested endpoint.

    The model rewrites paths, so both sides are normalized before they are
    compared, and the result is re-keyed under the extractor's route exactly as
    the per-endpoint path does.
    """
    if not isinstance(payload, dict):
        return None
    paths = payload.get("paths")
    if not isinstance(paths, dict):
        return None
    route_key = _normalize_route(route)
    if not route_key:
        return None
    method_key = (http_method or "GET").lower()
    for model_route, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        if _normalize_route(model_route) != route_key:
            continue
        for name, value in path_item.items():
            if str(name).lower() == method_key and isinstance(value, dict):
                return {
                    route_key: {
                        method_key: pipeline_common.normalize_operation_fields(value)
                    }
                }
    return None


def _apply_batch_payload(
    swagger: dict, directory_path: str, batch: List[Dict], payload
) -> Tuple[List[Dict], List[Dict]]:
    """Merge a batch reply, falling back to per-endpoint calls when it is unusable.

    An endpoint the model simply left out stays failed, so it is kept out of the
    api_index and retried next run.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("paths"), dict):
        print(
            f"apimesh: batch reply unusable for {_batch_source_file(batch)}, "
            "documenting its endpoints one by one"
        )
        return _update_swagger_for_endpoints(swagger, directory_path, batch)
    generated: List[Dict] = []
    failed: List[Dict] = []
    for job in batch:
        route = job.get("route")
        http_method = _job_method(job) or "GET"
        normalized = _operation_from_batch(payload, route, http_method)
        if normalized is None:
            failed.append(job)
            print(
                f"apimesh: skipped {http_method} {route}: "
                "missing from the batch reply"
            )
            continue
        pipeline_common.merge_paths(swagger, {"paths": normalized})
        generated.append(job)
    return generated, failed


def _update_swagger_for_batches(
    swagger: dict, directory_path: str, endpoint_jobs: list
) -> Tuple[List[Dict], List[Dict]]:
    """Every endpoint of every batch, behind the batch's own error boundary."""
    generated: List[Dict] = []
    failed: List[Dict] = []
    for batch in _batch_endpoint_jobs(endpoint_jobs):
        try:
            payload = _generate_batch_payload(directory_path, batch)
        except Exception as exc:
            print(f"apimesh: batch failed for {_batch_source_file(batch)}: {exc}")
            payload = None
        batch_generated, batch_failed = _apply_batch_payload(
            swagger, directory_path, batch, payload
        )
        generated.extend(batch_generated)
        failed.extend(batch_failed)
    return generated, failed


def _report_generation(generated: int, failed: int) -> None:
    """Raising on a total wipeout fails the run instead of shipping an invented spec."""
    total = generated + failed
    if not total:
        return
    print(f"generated {generated} of {total} endpoints ({failed} failed)")
    if generated == 0:
        raise RuntimeError("java swagger generation produced no endpoints")


def _unchanged_context_keys(
    directory_path: str, keys, endpoint_map: Dict, existing_index: Dict
) -> set:
    """The dirty keys whose prompt text is what they were generated from.

    Their stored spec operation and index entry are already correct, so they
    cost no LLM call. This is what makes the dependency hop cheap: an edit in
    an imported file that never reaches the endpoint's own context lands here.
    """
    unchanged = set()
    for key in keys:
        stored = pipeline_common.stored_context_hash(existing_index.get(key))
        if not stored:
            continue
        jobs = endpoint_map.get(key) or []
        # One hash is stored per key, so a key several jobs share has nothing
        # to compare against and is regenerated.
        if len(jobs) != 1:
            continue
        if _endpoint_context_hash(directory_path, jobs[0]) == stored:
            unchanged.add(key)
    return unchanged


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
        # A merged variant lives in its own file, and an edit or an addition
        # there is an edit to this endpoint. Reading only the job that carries
        # them left a route clean while its operation lost a whole variant.
        handlers = [
            variant for job in endpoint_map.get(key) or [] for variant in _job_variants(job)
        ]
        if pipeline_common.endpoint_has_changed(
            existing_index.get(key), handlers, changed_files
        ):
            changed_keys.add(key)

    keys_to_update = added_keys | changed_keys
    if pipeline_common.should_regenerate_fully(keys_to_update, new_keys):
        return None

    unchanged_keys = _unchanged_context_keys(
        directory_path, keys_to_update, endpoint_map, existing_index
    )
    if unchanged_keys:
        keys_to_update -= unchanged_keys
        print(
            f"apimesh: skipped {len(unchanged_keys)} unchanged endpoints "
            "(context hash match)"
        )

    updated_index = dict(existing_index)

    for key in removed_keys:
        updated_index.pop(key, None)
        _remove_endpoint_from_swagger(existing_swagger, key)

    pipeline_common.rebuild_unchanged_index_entries(
        unchanged_keys, endpoint_map, existing_index, updated_index, _build_api_index
    )

    jobs_to_update: List[Dict] = []
    for key in keys_to_update:
        jobs_to_update.extend(endpoint_map.get(key, []))
    generated, failed = _update_swagger_for_batches(
        existing_swagger, directory_path, jobs_to_update
    )
    failed_keys = {_endpoint_key(job.get("route"), _job_method(job)) for job in failed}
    # A failed endpoint keeps whatever the index already held (or stays out of
    # it) so the next run still sees it as new or stale and retries.
    pipeline_common.apply_generated_index_entries(
        updated_index, _build_api_index(generated), failed_keys
    )

    # The incremental pass never raises: the existing spec is still valid, and
    # persisting the index below (failed keys dropped) is what schedules the
    # retry. Raising here would discard both and launch the slow fallback.
    total = len(generated) + len(failed)
    if total:
        print(f"generated {len(generated)} of {total} endpoints ({len(failed)} failed)")
    if failed and not generated:
        print("apimesh: every changed endpoint failed; keeping the previous spec, they will retry next run")

    pipeline_common.stamp_commit_reference(existing_swagger, get_git_commit_hash())
    _write_api_index(updated_index)
    pipeline_common.record_coverage(
        existing_swagger,
        len(endpoint_jobs),
        len(generated),
        max(len(endpoint_jobs) - len(generated) - len(failed), 0),
        len(failed),
    )
    return pipeline_common.apply_host(existing_swagger, host)


def _reset_caches() -> None:
    """Drop everything held between runs.

    Two runs in one process see two checkouts, so a cached file body or import
    origin from the first would be read as the second's.
    """
    _FILE_CONTENT_CACHE.clear()
    _CONTENT_HASHES.clear()
    _METADATA_ENTRIES.clear()
    reset_source_index()
    reset_type_index()


def _read_file_lines(file_path: str) -> Optional[List[str]]:
    cached = _FILE_CONTENT_CACHE.get(file_path)
    if cached is not None:
        return cached
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    _FILE_CONTENT_CACHE[file_path] = lines
    return lines


def get_dependencies(
    data: Dict, start_line: int, end_line: int, file_path: str
) -> Tuple[List[Dict], List[Dict]]:
    existing_function_names = [
        item.get("name") for item in data.get("elements", {}).get("functions", [])
    ]
    in_file_dependency_functions: List[Dict] = []
    for call in data.get("elements", {}).get("function_calls", []):
        call_name = call.get("name")
        call_start = call.get("start_line")
        call_end = call.get("end_line")
        if (
            call_name in existing_function_names
            and isinstance(call_start, int)
            and isinstance(call_end, int)
            and start_line <= call_start <= end_line
        ):
            entry = call.copy()
            entry["file_path"] = file_path
            in_file_dependency_functions.append(entry)

    imported_functions: List[Dict] = []
    for item in data.get("imports", []):
        # An import counts for every handler of the file, not only the ones
        # whose own lines name it. A Spring controller injects its
        # collaborators as fields and constructor parameters, so the type name
        # is written once, far above the handler; scoping imports to the
        # handler's line range recorded no dependency at all and the model saw
        # none of the services and DTOs the endpoint runs on.
        if not item.get("usage_lines"):
            continue
        entry = dict(item)
        # Only the types the file reached for, so a wildcard import does not
        # drag a whole package into the prompt.
        entry["used_names"] = sorted(
            {
                usage.get("name")
                for usage in item.get("usages", [])
                if usage.get("name")
            }
        )
        imported_functions.append(entry)
    return in_file_dependency_functions, imported_functions


def get_code_blocks(
    in_file_dependency_functions: List[Dict],
    imported_functions: List[Dict],
    file_name: str,
    directory_path: str,
) -> List[List[str]]:
    code_blocks: List[List[str]] = []
    lines = _read_file_lines(file_name) or []
    for block in in_file_dependency_functions:
        start = block.get("function_start_line") or block.get("start_line")
        end = block.get("function_end_line") or block.get("end_line")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        segment = lines[start - 1 : end]
        if segment:
            code_blocks.append(segment)

    for imp in imported_functions:
        for definition in _resolve_imported_definitions(imp, None):
            origin_lines = _read_file_lines(definition["file_path"]) or []
            snippet = origin_lines[definition["start_line"] - 1 : definition["end_line"]]
            if snippet:
                code_blocks.append(snippet)
    return code_blocks


def provide_context_codeblock(directory_path: str, method_info: Dict):
    method_definition_code_block = _handler_lines(method_info)

    context_code_blocks: List[List[str]] = []
    # Every merged variant contributes its own dependencies; the duplicates two
    # variants of one handler share are dropped when the context is fitted.
    for variant in _job_variants(method_info):
        file_name = variant["file_path"]
        start_line = variant.get("start_line", 1)
        end_line = variant.get("end_line", start_line)
        data = _load_file_metadata(file_name) or {
            "elements": {"functions": [], "function_calls": []},
            "imports": [],
        }
        in_file_dependency_functions, imported_functions = get_dependencies(
            data, start_line, end_line, file_name
        )
        context_code_blocks.extend(
            get_code_blocks(
                in_file_dependency_functions,
                imported_functions,
                file_name,
                directory_path,
            )
        )
    return context_code_blocks, method_definition_code_block


def _condition_signature(job: Dict) -> Tuple:
    """What tells two handlers on one route and verb apart."""
    conditions = job.get("conditions") or {}
    return tuple(
        (name, tuple(conditions.get(name) or ()))
        for name in _CONDITION_ORDER
        if conditions.get(name)
    )


def _dedupe_endpoint_jobs(endpoint_jobs: List[Dict]) -> List[Dict]:
    """One job per route and method, content negotiation variants merged.

    The same route registered twice, or reached through two class prefixes that
    collapse to one path, used to cost two LLM calls and write the second
    answer over the first. Two handlers that differ by consumes, produces,
    params or headers are not that: they are one OpenAPI operation, so they are
    documented together and the job carries every one of their bodies.
    """
    kept_by_key: Dict[str, Dict] = {}
    unique: List[Dict] = []
    for job in endpoint_jobs:
        if not job.get("route"):
            continue
        key = _endpoint_key(job.get("route"), _job_method(job))
        kept = kept_by_key.get(key)
        if kept is None:
            kept_by_key[key] = job
            unique.append(job)
            continue
        signature = _condition_signature(job)
        if any(
            _condition_signature(variant) == signature
            for variant in _job_variants(kept)
        ):
            continue
        kept.setdefault("variants", []).append(job)
    return unique


def run_swagger_generation(host: str) -> Optional[Dict]:
    _reset_caches()
    directory_path = get_repo_path()
    repo_name = get_repo_name()
    # Built before the try: a walk that dies leaves an incomplete picture of
    # which entries are still live, and pruning against it would be wrong.
    _build_metadata_cache(directory_path)

    try:
        api_files = find_api_definition_files(directory_path)
        endpoints: List[Dict] = []
        reset_extraction_drops()
        # The interfaces the controllers implement are resolved across the whole
        # scan set, so the index is built before any file is extracted.
        index_scanned_types(api_files, directory_path)
        for file in api_files:
            endpoints.extend(find_api_endpoints(Path(file), directory_path))
        log_extraction_drops()

        endpoint_jobs = _dedupe_endpoint_jobs(endpoints)

        # Nothing was extracted: hand back None so the caller reports an honest
        # zero instead of publishing an empty spec (or, worse, incrementally
        # deleting one).
        if not endpoint_jobs:
            print(
                "apimesh: java parser found 0 endpoints, "
                "nothing will be generated"
            )
            return None

        incremental_swagger = _maybe_incremental_update(directory_path, endpoint_jobs, host)
        if incremental_swagger is not None:
            return incremental_swagger

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

        generated: List[Dict] = []
        failed: List[Dict] = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(_generate_batch_payload, directory_path, batch): batch
                for batch in _batch_endpoint_jobs(endpoint_jobs)
            }
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    payload = future.result()
                except Exception as exc:
                    print(
                        f"apimesh: batch failed for {_batch_source_file(batch)}: {exc}"
                    )
                    payload = None
                batch_generated, batch_failed = _apply_batch_payload(
                    swagger, directory_path, batch, payload
                )
                generated.extend(batch_generated)
                failed.extend(batch_failed)

        _report_generation(len(generated), len(failed))

        # Only endpoints that made it into the spec are indexed, otherwise a
        # failure looks unchanged next run and is never retried.
        _write_api_index(_build_api_index(generated))
        pipeline_common.record_coverage(
            swagger, len(endpoint_jobs), len(generated), 0, len(failed),
            # Only the mappings extraction could not read are dropped routes;
            # a @RequestMapping documented as GET is an assumption, not a loss.
            dropped=extraction_drops().get("unresolved", 0),
        )

        return swagger
    finally:
        # The cache outlives the run; only entries for content that is gone are
        # dropped, so the next run parses just what changed.
        _prune_metadata_cache()

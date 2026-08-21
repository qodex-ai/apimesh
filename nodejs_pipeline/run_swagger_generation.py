import os, json, re
import datetime
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from nodejs_pipeline.generate_file_information import process_file, get_module_origin
from nodejs_pipeline.find_api_definition_files import find_api_definition_files, find_node_files
from nodejs_pipeline.identify_api_functions import (
    find_api_endpoints_js,
    find_inline_require_mounts,
    find_module_imports,
    find_mount_prefixes,
    join_mount_prefix,
)
import pipeline_common
from config import Configurations
from nodejs_pipeline.definition_swagger_generator import (
    get_batch_definition_swagger,
    get_function_definition_swagger,
)
from nodejs_pipeline.constants import SUPPORTED_NODE_FILE_EXTENSIONS
from utils import (
    get_git_commit_hash,
    get_github_repo_url,
    get_repo_path,
    get_repo_name,
    get_output_filepath,
    get_changed_files_since,
)

config = Configurations()

# An express route parameter name: ':name'. The inline regex constraint and the
# optional marker that may follow it are not part of the name, and the
# constraint can nest parentheses, so it is consumed separately.
EXPRESS_PARAM_PATTERN = re.compile(r":([A-Za-z_$][A-Za-z0-9_$]*)")

MAX_ENDPOINTS_PER_BATCH = pipeline_common.MAX_ENDPOINTS_PER_BATCH
CONTEXT_TOKEN_BUDGET = pipeline_common.CONTEXT_TOKEN_BUDGET
_EFFECTIVE_CONTEXT_BUDGET = pipeline_common.EFFECTIVE_CONTEXT_BUDGET
MAX_HANDLER_TOKENS = pipeline_common.MAX_HANDLER_TOKENS

# Per file metadata is cached under the output directory, never inside the
# scanned repo: a run must not create or delete anything in the user's tree.
METADATA_CACHE_PIPELINE = "nodejs"

# Bumped when the shape of a metadata entry changes. A missing or stale marker
# wipes this pipeline's cache before anything reads it.
METADATA_CACHE_VERSION = "1"

# File path -> content hash, and file path -> the cache entry holding its
# metadata. Both are per run: two runs in one process are two checkouts.
_CONTENT_HASHES = {}
_METADATA_ENTRIES = {}


def _metadata_cache_dir() -> str:
    return pipeline_common.metadata_cache_dir(
        get_output_filepath(), METADATA_CACHE_PIPELINE
    )


def _content_hash(file_path: str):
    return pipeline_common.content_hash(file_path, _CONTENT_HASHES)


def _metadata_cache_filename(file_path: str, content_hash: str) -> str:
    return pipeline_common.metadata_cache_filename(file_path, content_hash)


def _metadata_cache_path(file_path: str):
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
    """One cache entry per node file below the repo root."""
    pipeline_common.build_metadata_cache(
        directory_path,
        _prepare_metadata_cache,
        should_process_directory,
        lambda file_path: Path(file_path).suffix.lower() in SUPPORTED_NODE_FILE_EXTENSIONS,
        _cache_file_metadata,
    )


def _prune_metadata_cache() -> None:
    pipeline_common.prune_metadata_cache(_metadata_cache_dir(), _METADATA_ENTRIES)


def _reset_metadata_state() -> None:
    """Two runs in one process see two checkouts of the same paths."""
    _CONTENT_HASHES.clear()
    _METADATA_ENTRIES.clear()


def should_process_directory(dir_path: str, root: str = None) -> bool:
    """
    Check if a directory should be processed or ignored.
    Only components below root count, otherwise a repo living under /var or
    /build is ignored entirely.
    """
    relative_path = os.path.relpath(dir_path, root) if root else dir_path
    path_parts = relative_path.split(os.sep)
    return not any(part in config.ignored_dirs for part in path_parts)


def _api_index_output_path() -> str:
    return pipeline_common.api_index_output_path(get_output_filepath())


def _load_file_metadata(file_path: str):
    return pipeline_common.load_file_metadata(_metadata_cache_path(file_path))


def _endpoint_key(route, method):
    # Keys must use the same normalized route the swagger paths are keyed by,
    # otherwise removals of deleted endpoints never match.
    return pipeline_common.endpoint_key(route, method, _normalize_route)


def _job_method(endpoint):
    return endpoint.get("method") or endpoint.get("http_method")


def _resolve_imported_definitions(import_item, route):
    origin = import_item.get("origin")
    imported_name = import_item.get("imported_name")
    if not origin or not imported_name or origin == "<node_builtin_or_external>":
        return []
    metadata = _load_file_metadata(origin)
    if not metadata:
        return []
    elements = metadata.get("elements", {})
    candidates = []
    name_candidates = [imported_name]
    if "." in imported_name:
        name_candidates.append(imported_name.split(".")[-1])
    for key in ("classes", "functions", "variables"):
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


def _endpoint_imports(endpoint, abs_file_path, route):
    return pipeline_common.endpoint_imports(
        endpoint,
        abs_file_path,
        route,
        _load_file_metadata,
        get_dependencies,
        _resolve_imported_definitions,
    )


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


def _endpoint_method(method_info):
    return (method_info.get("method") or method_info.get("http_method") or "").lower()


def _endpoint_label(method_info):
    return _endpoint_key(
        method_info.get("route"), method_info.get("method") or method_info.get("http_method")
    )


def _handler_lines(method_info) -> list:
    """The handler's own source lines, read for pricing its batch section."""
    try:
        with open(method_info.get("file_path") or "", "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    start_line = method_info.get("start_line") or 1
    end_line = method_info.get("end_line") or start_line
    return lines[start_line - 1:end_line]


def _batch_section_tokens(method_info) -> int:
    """What this endpoint's section costs the batch."""
    return pipeline_common.section_token_cost(
        f"{_endpoint_label(method_info)}:",
        "".join(_handler_lines(method_info)),
        MAX_HANDLER_TOKENS,
    )


def _batch_endpoint_jobs(endpoint_jobs: list) -> list:
    """One batch per source file, packed to fit the context budget."""
    batches = []
    for jobs in pipeline_common.group_jobs_by_file(endpoint_jobs).values():
        batches.extend(
            pipeline_common.pack_batches(
                jobs,
                _batch_section_tokens,
                MAX_ENDPOINTS_PER_BATCH,
                _EFFECTIVE_CONTEXT_BUDGET,
            )
        )
    return batches


def _apply_context_budget(handler_sections: list, shared_blocks: list, file_label: str):
    """The handler bodies and the shared context, inside CONTEXT_TOKEN_BUDGET."""
    return pipeline_common.apply_context_budget(
        handler_sections,
        shared_blocks,
        file_label,
        MAX_HANDLER_TOKENS,
        _EFFECTIVE_CONTEXT_BUDGET,
    )


def _context_hash(context_code_blocks, method_definition_code_block) -> str:
    """The one context hash recipe every pipeline shares."""
    return pipeline_common.context_hash(
        context_code_blocks, method_definition_code_block, MAX_HANDLER_TOKENS
    )


def _collect_batch_context(directory_path: str, batch: list):
    """
    Read the context of every endpoint in the batch. An endpoint whose context
    cannot be read is reported back instead of taking the whole batch down.
    Returns (usable_jobs, endpoints_list, shared_context, sections, failures).
    """
    usable_jobs = []
    endpoint_lines = []
    handler_sections = []
    shared_blocks = []
    failures = []
    for method_info in batch:
        try:
            context_code_blocks, method_definition_code_block = provide_context_codeblock(
                directory_path, method_info
            )
        except Exception as ex:
            failures.append((method_info, ex))
            continue
        label = _endpoint_label(method_info)
        method_info["context_hash"] = _context_hash(
            context_code_blocks, method_definition_code_block
        )
        usable_jobs.append(method_info)
        endpoint_lines.append(label)
        handler_sections.append(
            (f"{label}:", pipeline_common.block_text(method_definition_code_block))
        )
        shared_blocks.extend(context_code_blocks)
    file_label = batch[0].get("file_path") or "unknown file"
    kept_blocks, sections = _apply_context_budget(handler_sections, shared_blocks, file_label)
    return (
        usable_jobs,
        "\n".join(endpoint_lines),
        "\n\n".join(kept_blocks),
        "\n\n".join(sections),
        failures,
    )


def _generate_endpoint_fragment(directory_path: str, method_info: dict):
    """The per endpoint call, with the same dedupe and token budget as a batch."""
    context_code_blocks, method_definition_code_block = provide_context_codeblock(
        directory_path, method_info
    )
    method_info["context_hash"] = _context_hash(
        context_code_blocks, method_definition_code_block
    )
    kept_blocks, sections = _apply_context_budget(
        [("", pipeline_common.block_text(method_definition_code_block))],
        context_code_blocks,
        method_info.get("file_path") or "unknown file",
    )
    return get_function_definition_swagger(
        [sections[0] if sections else ""],
        [[block] for block in kept_blocks],
        method_info.get("route"),
        http_method=method_info.get("method") or method_info.get("http_method"),
    )


def _generate_fragments_per_endpoint(directory_path: str, batch: list) -> list:
    """The pre-batch path: one call per endpoint, each failing on its own."""
    results = []
    for method_info in batch:
        try:
            results.append((method_info, _generate_endpoint_fragment(directory_path, method_info), None))
        except Exception as ex:
            results.append((method_info, None, ex))
    return results


def _generate_batch_fragments(directory_path: str, batch: list) -> list:
    """
    Document a whole batch with one call, retried once. Returns one
    (method_info, fragment, error) triple per endpoint; a fragment is None when
    the model left that endpoint out, which counts as a failure so the next run
    retries it. An unusable reply falls back to the per endpoint calls.
    """
    usable_jobs, endpoints_list, shared_context, sections, failures = _collect_batch_context(
        directory_path, batch
    )
    results = [(method_info, None, error) for method_info, error in failures]
    if not usable_jobs:
        return results

    response = pipeline_common.retry_batch_call(
        lambda: get_batch_definition_swagger(endpoints_list, shared_context, sections)
    )
    if response is None:
        return results + _generate_fragments_per_endpoint(directory_path, usable_jobs)

    # Two model keys can normalize to the same route, so the verbs are merged
    # instead of the second path item being dropped.
    paths_by_route = {}
    for path_key, path_item in response["paths"].items():
        if not isinstance(path_item, dict):
            continue
        merged = paths_by_route.setdefault(_normalize_route(path_key), {})
        for verb, payload in path_item.items():
            merged.setdefault(verb, payload)

    for method_info in usable_jobs:
        route = _normalize_route(method_info.get("route"))
        method = _endpoint_method(method_info)
        operation = None
        for key, payload in (paths_by_route.get(route) or {}).items():
            if key.lower() == method and isinstance(payload, dict):
                operation = payload
                break
        fragment = {"paths": {route: {method: operation}}} if operation is not None else None
        results.append((method_info, fragment, None))
    return results


def _update_swagger_for_endpoints(swagger: dict, directory_path: str, endpoints: list):
    """
    Generate the given endpoints in file batches. Returns (succeeded, failed) as
    lists of the endpoints themselves, so the caller can tell which index keys
    are safe to refresh.
    """
    succeeded = []
    failed = []
    routable = []
    for method_info in endpoints:
        if not method_info.get("route"):
            failed.append(method_info)
            continue
        routable.append(method_info)
    for batch in _batch_endpoint_jobs(routable):
        try:
            results = _generate_batch_fragments(directory_path, batch)
        except Exception as ex:
            failed.extend(batch)
            for method_info in batch:
                print(f"apimesh: skipping {_endpoint_label(method_info)}: {ex}")
            continue
        for method_info, fragment, error in results:
            label = _endpoint_label(method_info)
            if error is not None:
                failed.append(method_info)
                print(f"apimesh: skipping {label}: {error}")
                continue
            if not _merge_paths(swagger, fragment, method_info):
                failed.append(method_info)
                print(f"apimesh: skipping {label}: unusable swagger fragment")
                continue
            succeeded.append(method_info)
    return succeeded, failed


def _context_is_unchanged(directory_path: str, existing_entry, jobs) -> bool:
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
            context_code_blocks, method_definition_code_block = provide_context_codeblock(
                directory_path, method_info
            )
        except Exception:
            return False
        if _context_hash(context_code_blocks, method_definition_code_block) != stored_hash:
            return False
    return True


def _maybe_incremental_update(directory_path: str, endpoint_jobs: list, host=None):
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
    # generating them key by key put the endpoints of one changed file in a
    # call each.
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

    _post_process_swagger(existing_swagger)
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

def _constraint_end(route: str, start: int):
    """
    Index just past the inline regex constraint that opens at start, counting
    nested parentheses so '(\\d{2}(?:-\\d{2})?)' is consumed whole. None when the
    parentheses never balance.
    """
    depth = 0
    index = start
    while index < len(route):
        char = route[index]
        if char == "\\":
            # An escaped character never opens or closes the constraint.
            index += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _normalize_route(route: str):
    if not route:
        return route
    # Convert Express-style :param to OpenAPI {param}. An express param name
    # stops at the first non-word character, so '/:from-:to' is two params, and
    # the optional marker and an inline regex constraint are not part of the
    # name: '/u/:id(\\d+)?' is the same parameter as '/u/:id'. A constraint whose
    # parentheses never balance is left exactly as written, together with the
    # parameter it belongs to, rather than rewritten into a mangled path.
    parts = []
    cursor = 0
    for match in EXPRESS_PARAM_PATTERN.finditer(route):
        if match.start() < cursor:
            # Inside a constraint already consumed, so not a parameter.
            continue
        parts.append(route[cursor : match.start()])
        index = match.end()
        if index < len(route) and route[index] == "(":
            end = _constraint_end(route, index)
            if end is None:
                parts.append(route[match.start() : index])
                cursor = index
                continue
            index = end
        if index < len(route) and route[index] == "?":
            index += 1
        parts.append("{" + match.group(1) + "}")
        cursor = index
    parts.append(route[cursor:])
    return "".join(parts)


def _record_mount_prefix(prefixes: dict, module_name: str, base_directory: str, prefix: str) -> None:
    origin = get_module_origin(module_name, base_directory)
    if not origin or origin == "<node_builtin_or_external>":
        return
    bucket = prefixes.setdefault(os.path.abspath(origin), [])
    if prefix not in bucket:
        bucket.append(prefix)


def _build_mount_prefix_map(directory_path: str) -> dict:
    """
    Map an absolute file path to the mount prefixes its router is mounted under
    elsewhere, e.g. app.js doing app.use('/api/v1', require('./routes/users'))
    gives routes/users.js the prefix /api/v1. A router mounted twice keeps both
    prefixes. One level only.
    """
    prefixes = {}
    for node_file in find_node_files(directory_path):
        try:
            source = node_file.read_text(encoding='utf-8')
        except Exception:
            continue
        mounts = find_mount_prefixes(source)
        inline_mounts = find_inline_require_mounts(source)
        if not mounts and not inline_mounts:
            continue
        imports = find_module_imports(source)
        base_directory = os.path.dirname(os.path.abspath(str(node_file)))
        for identifier, identifier_prefixes in mounts.items():
            module_name = imports.get(identifier)
            if not module_name:
                continue
            for prefix in identifier_prefixes:
                _record_mount_prefix(prefixes, module_name, base_directory, prefix)
        # app.use('/api', require('./routes/users')) has no identifier to resolve,
        # the module string is right there in the mount call.
        for module_name, module_prefixes in inline_mounts.items():
            for prefix in module_prefixes:
                _record_mount_prefix(prefixes, module_name, base_directory, prefix)
    return prefixes


def run_swagger_generation(host):
    directory_path = get_repo_path()
    repo_name = get_repo_name()
    _reset_metadata_state()
    # Built before the try: a walk that dies leaves an incomplete picture of
    # which entries are still live, and pruning against it would be wrong.
    _build_metadata_cache(directory_path)
    try:
        api_definition_files = find_api_definition_files(directory_path)
        mount_prefixes = _build_mount_prefix_map(directory_path)
        all_endpoints_dict = dict()
        for file in api_definition_files:
            all_endpoints = []
            py_file = Path(file)
            eps = find_api_endpoints_js(py_file)
            if eps:
                file_prefixes = mount_prefixes.get(os.path.abspath(file))
                if file_prefixes:
                    # One endpoint per mount: a router mounted at /v1 and /v2
                    # serves both path sets, and both belong in the spec.
                    eps = [
                        dict(endpoint, route=join_mount_prefix(prefix, endpoint.get('route')))
                        for endpoint in eps
                        for prefix in file_prefixes
                    ]
                all_endpoints.extend(eps)
                all_endpoints_dict[file] = all_endpoints
        swagger = {
                "openapi": "3.0.0",
                "info": {
                    "title": repo_name,
                    "version": "1.0.0",
                    "description": "This Swagger file was generated using OpenAI GPT.",
                    "x-generated-at": datetime.datetime.utcnow().isoformat() + "Z",
                    "x-commit-reference": get_git_commit_hash(),
                    "x-github-repo-url": get_github_repo_url()
                },
                "servers": [
                    {
                        "url": host
                    }
                ],
                "paths": {}
            }
        endpoint_jobs = []
        for value in all_endpoints_dict.values():
            for item in value:
                if item.get('type') == 'class':
                    endpoint_jobs.extend(item.get('methods', []))
                else:
                    endpoint_jobs.append(item)
        # Normalize paths once to avoid duplicates like /:name vs /{name}
        for job in endpoint_jobs:
            if 'route' in job:
                job['route'] = _normalize_route(job['route'])
        # The contract lane runs before anything else is decided: a spec-first
        # repo can carry its whole surface in contracts the code lane cannot
        # see. Deterministic and LLM-free, so it runs every time.
        lane_result, reconciled, endpoint_jobs = pipeline_common.integrate_contract_lane(
            directory_path, endpoint_jobs, _job_method, _normalize_route
        )
        contract_paths = bool(reconciled and reconciled["paths"])

        if not endpoint_jobs and not contract_paths:
            print("apimesh: nodejs parser found 0 endpoints, nothing will be generated")
            return None

        if not endpoint_jobs:
            print(
                "apimesh: nodejs parser found 0 annotated endpoints; "
                "the contract lane supplies the spec"
            )
            swagger = pipeline_common.base_swagger(
                repo_name,
                host,
                get_git_commit_hash(),
                get_github_repo_url(),
                datetime.datetime.utcnow().isoformat() + "Z",
            )
            # An empty code index is deliberate: code endpoints that existed
            # on a previous run and are gone now must leave the spec.
            _write_api_index(_build_api_index([]))
            pipeline_common.record_coverage(swagger, 0, 0, 0, 0)
            return pipeline_common.finish_with_contract(
                swagger, reconciled, lane_result["report"], get_output_filepath()
            )
        incremental_swagger = _maybe_incremental_update(directory_path, endpoint_jobs, host)
        if incremental_swagger is not None:
            if reconciled is not None:
                return pipeline_common.finish_with_contract(
                    incremental_swagger, reconciled, lane_result["report"], get_output_filepath()
                )
            return incremental_swagger
        batches = _batch_endpoint_jobs(endpoint_jobs)
        max_workers = min(5, len(batches))
        start_time = time.time()
        generated = []
        failed = 0
        latest_message = ""
        with ThreadPoolExecutor(max_workers=max_workers or 1) as executor:
            futures = {
                executor.submit(_generate_batch_fragments, directory_path, batch): batch
                for batch in batches
            }
            for future in as_completed(futures):
                batch = futures[future]
                # One bad batch must never discard the ones that already worked.
                try:
                    results = future.result()
                except Exception as ex:
                    failed += len(batch)
                    for method_info in batch:
                        print(f"apimesh: skipping {_endpoint_label(method_info)}: {ex}")
                    continue
                for method_info, swagger_for_def, error in results:
                    endpoint_label = _endpoint_label(method_info)
                    if error is not None:
                        failed += 1
                        print(f"apimesh: skipping {endpoint_label}: {error}")
                        continue
                    if not _merge_paths(swagger, swagger_for_def, method_info):
                        failed += 1
                        print(f"apimesh: skipping {endpoint_label}: unusable swagger fragment")
                        continue
                    generated.append(method_info)
                    latest_message = (
                        f"Completed generating endpoint related information for {len(generated)} endpoints in "
                        f"{int(time.time() - start_time)} seconds"
                    )
                    print(latest_message, end="\r", flush=True)
        if generated:
            print(latest_message)
        print(f"generated {len(generated)} of {len(endpoint_jobs)} endpoints ({failed} failed)")
        if not generated:
            raise RuntimeError(
                f"nodejs swagger generation failed for all {len(endpoint_jobs)} endpoints"
            )
        _post_process_swagger(swagger)
        # Only endpoints that made it into the spec are indexed, otherwise a
        # failure looks unchanged next run and is never retried.
        _write_api_index(_build_api_index(generated))
        pipeline_common.record_coverage(swagger, len(endpoint_jobs), len(generated), 0, failed)
        if reconciled is not None:
            return pipeline_common.finish_with_contract(
                swagger, reconciled, lane_result["report"], get_output_filepath()
            )
        return swagger
    finally:
        # The cache outlives the run; only entries for content that is gone are
        # dropped, so the next run parses just what changed.
        _prune_metadata_cache()


def get_dependencies(data, start_line, end_line, file_path):
    elements = data.get('elements', {})
    functions = elements.get('functions', [])
    existing_function_names = [item['name'] for item in functions if item['name'] not in ['get', 'post', 'put', 'delete', 'patch']]
    function_lookup = {}
    for func in functions:
        function_lookup.setdefault(func['name'], []).append(func)
    in_file_dependency_functions = []
    for item in elements.get('function_calls', []):
        if (item['name'] in existing_function_names) and item['start_line'] >= start_line and item['end_line'] <= end_line:
            call_line = item.get('start_line')
            definition = None
            candidates = function_lookup.get(item['name'], [])
            if candidates:
                candidates = sorted(candidates, key=lambda func: func.get('start_line', 0))
                for candidate in candidates:
                    start = candidate.get('start_line')
                    end = candidate.get('end_line')
                    if start and end and start <= call_line <= end:
                        definition = candidate
                        break
                    if start and start <= call_line:
                        definition = candidate
                if not definition:
                    definition = candidates[0]
            dependency_info = {
                'name': item['name'],
                'file_path': file_path,
                'call_start_line': item.get('start_line'),
                'call_end_line': item.get('end_line'),
                'function_start_line': None,
                'function_end_line': None
            }
            if definition:
                dependency_info['function_start_line'] = definition.get('start_line')
                dependency_info['function_end_line'] = definition.get('end_line')
            else:
                dependency_info['function_start_line'] = item.get('start_line')
                dependency_info['function_end_line'] = item.get('end_line')
            in_file_dependency_functions.append(dependency_info)
    imported_functions = []
    for item in elements.get('imports', []):
        if not item['path_exists']:
            continue
        for k in item['usage_lines']:
            if start_line<=k<=end_line:
                imported_functions.append(item)
            if in_file_dependency_functions:
                for item1 in in_file_dependency_functions:
                    dep_start = item1.get('call_start_line')
                    dep_end = item1.get('call_end_line')
                    if dep_start and dep_end and dep_start <= k <= dep_end and item not in imported_functions:
                        imported_functions.append(item)
    return in_file_dependency_functions, imported_functions

def get_code_blocks(in_file_dependency_functions, imported_functions, file_name, directory_path):
    code_blocks = []
    for block in in_file_dependency_functions:
        block_file_name = block.get('file_path', file_name)
        start = block.get('function_start_line')
        end = block.get('function_end_line', start)
        if not block_file_name or not start or not end:
            continue
        with open(block_file_name, "r") as f:
            lines = f.readlines()
        code_blocks.append(lines[start - 1: end])
    for func in imported_functions:
        visited = False
        origin_file_name = func.get('origin')
        if not origin_file_name:
            continue
        complete_json_file_path = _metadata_cache_path(origin_file_name)
        if not complete_json_file_path or not os.path.exists(complete_json_file_path):
            continue
        with open(complete_json_file_path, "r") as f:
            data = json.load(f)
        for item in data['elements']['classes']:
            if item['name'] == func['imported_name']:
                visited = True
                with open(origin_file_name, "r") as f:
                    lines = f.readlines()
                code_blocks.append(lines[item['start_line']-1: item['end_line']])
                break
        if not visited:
            for item in data['elements']['functions']:
                if item['name'] == func['imported_name']:
                    visited = True
                    with open(origin_file_name, "r") as f:
                        lines = f.readlines()
                    code_blocks.append(lines[item['start_line'] - 1: item['end_line']])
                    break
        if not visited:
            for item in data['elements']['variables']:
                if item['name'] == func['imported_name']:
                    with open(origin_file_name, "r") as f:
                        lines = f.readlines()
                    code_blocks.append(lines[item['start_line'] - 1: item['end_line']])
                    break
    return code_blocks


def provide_context_codeblock(directory_path, method_info):
    file_name = method_info['file_path']
    with open(method_info['file_path'], "r") as f:
        lines = f.readlines()
    method_definition_code_block = lines[method_info["start_line"]-1: method_info["end_line"]]
    complete_json_file_path = _metadata_cache_path(file_name)

    if not complete_json_file_path or not os.path.exists(complete_json_file_path):
        return [], method_definition_code_block

    with open(complete_json_file_path, "r") as f:
        data = json.load(f)

    in_file_dependency_functions, imported_functions = get_dependencies(
        data,
        method_info["start_line"],
        method_info["end_line"],
        method_info['file_path']
    )
    context_code_blocks = get_code_blocks(
        in_file_dependency_functions,
        imported_functions,
        file_name,
        directory_path
    )
    return context_code_blocks, method_definition_code_block


def _merge_paths(target, source, method_info):
    """
    Merge one generated fragment. The model routinely rewrites the path and the
    method, so the operation is re-keyed under the route we extracted; that is
    also the key the api index and incremental removals use.
    """
    _, operation = pipeline_common.first_operation(source)
    if operation is None:
        return False
    route = _normalize_route(method_info.get("route"))
    method = (method_info.get("method") or method_info.get("http_method") or "").lower()
    if not route or not method:
        return False
    target.setdefault("paths", {}).setdefault(route, {})[method] = (
        pipeline_common.normalize_operation_fields(operation)
    )
    return True


def _post_process_swagger(swagger):
    """
    Framework neutral cleanup of the merged swagger:
    - drop wildcard /* catch-all paths that come from generic middleware
    - re-key any lingering express-style :param segments
    """
    paths = swagger.get("paths", {})
    paths.pop("/*", None)
    paths.pop("*", None)

    for original in list(paths.keys()):
        normalized = _normalize_route(original)
        if normalized != original:
            existing = paths.pop(original)
            if normalized not in paths:
                paths[normalized] = existing
            else:
                paths[normalized].update(existing)

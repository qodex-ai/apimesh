import os, json
import re
import datetime
import time
from pathlib import Path
from openai import APIError
from python_pipeline.generate_file_information import process_file
from python_pipeline.django_urlconf import collect_django_endpoints
from python_pipeline.find_api_definition_files import find_api_definition_sources, find_python_files
from python_pipeline.identify_api_functions import (
    find_api_endpoints,
    collect_external_prefixes,
)
import pipeline_common
from config import Configurations
from python_pipeline.definition_swagger_generator import (
    HANDLER_TOKEN_BUDGET,
    get_batch_definition_swagger,
    get_function_definition_swagger,
    section_token_cost,
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

OPENAI_RETRY_DELAYS = pipeline_common.API_RETRY_DELAYS
MAX_BATCH_ENDPOINTS = pipeline_common.MAX_ENDPOINTS_PER_BATCH

# Flask writes params as <int:pk> or <name>, OpenAPI wants {pk} / {name}.
_ROUTE_PARAM_PATTERN = re.compile(r"<(?:[^<>:]+:)?([^<>]+)>")


def _relative_parts(path: str, root_path: str):
    """Path components below the scanned repo root.

    Matching the whole absolute path means a repo living under /var or
    /tmp/build is ignored in its entirety.
    """
    relative = os.path.relpath(os.path.abspath(path), os.path.abspath(root_path))
    if relative.startswith(".."):
        return ()
    return Path(relative).parts


def should_process_directory(dir_path: str, root_path: str) -> bool:
    """
    Check if a directory should be processed or ignored
    """
    return not any(part in config.ignored_dirs for part in _relative_parts(dir_path, root_path))


def _normalize_route(route):
    """One route spelling, shared by the swagger path key and the api_index key.

    Flask/FastAPI converter syntax is rewritten to OpenAPI templating, because
    the save step rewrites paths too and the index would otherwise stop
    matching the spec on the next run.
    """
    if not route:
        return None
    normalized = _ROUTE_PARAM_PATTERN.sub(r"{\1}", str(route).strip())
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _api_index_output_path() -> str:
    return pipeline_common.api_index_output_path(get_output_filepath())


# Per file metadata is cached under the output directory, never inside the
# scanned repo: a run must not create or delete anything in the user's tree.
METADATA_CACHE_PIPELINE = "python"

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
        on_error=lambda path, exc: print(
            f"apimesh: skipping metadata for {path}: {exc}"
        ),
    )


def _build_metadata_cache(directory_path: str) -> None:
    """One cache entry per python file below the repo root."""
    pipeline_common.build_metadata_cache(
        directory_path,
        _prepare_metadata_cache,
        should_process_directory,
        lambda file_path: file_path.endswith(".py"),
        _cache_file_metadata,
    )


def _prune_metadata_cache() -> None:
    pipeline_common.prune_metadata_cache(_metadata_cache_dir(), _METADATA_ENTRIES)


def _reset_metadata_state() -> None:
    """Two runs in one process see two checkouts of the same paths."""
    _CONTENT_HASHES.clear()
    _METADATA_ENTRIES.clear()


def _load_file_metadata(file_path: str):
    return pipeline_common.load_file_metadata(_metadata_cache_path(file_path))


def _endpoint_key(route, method):
    return pipeline_common.endpoint_key(route, method, _normalize_route)


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


def _merge_paths(target: dict, source: dict) -> None:
    pipeline_common.merge_paths(target, source)


def _rekey_fragment(fragment, route, method):
    """Re-key a model fragment under the route and method the extractor found.

    The model routinely rewrites the path it is given, which leaves the spec
    keyed on something the api_index can never match. Only the first operation
    body is kept, since one fragment describes one endpoint.
    """
    normalized_route = _normalize_route(route)
    if not normalized_route:
        return None
    model_method, payload = pipeline_common.first_operation(fragment)
    if payload is None:
        return None
    # The extractor cannot read the method off a bare Flask @app.route, so
    # the model's method is the only one available there.
    method_key = method.lower() if method else model_method
    return {
        "paths": {
            normalized_route: {
                method_key: pipeline_common.normalize_operation_fields(payload)
            }
        }
    }


def _call_swagger_llm(method_definition_code_block, context_code_blocks, route, http_method=None, source_file=None):
    for attempt in range(len(OPENAI_RETRY_DELAYS) + 1):
        try:
            return get_function_definition_swagger(
                method_definition_code_block, context_code_blocks, route,
                http_method=http_method, source_file=source_file,
            )
        except APIError as exc:
            if attempt == len(OPENAI_RETRY_DELAYS):
                print(f"apimesh: giving up on {route} after {attempt + 1} attempts: {exc}")
                return None
            time.sleep(OPENAI_RETRY_DELAYS[attempt])
        except Exception as exc:
            print(f"apimesh: could not generate swagger for {route}: {exc}")
            return None
    return None


def _swagger_fragment_for_endpoint(directory_path: str, method_info: dict):
    """One endpoint's swagger fragment, or None when it could not be generated."""
    route = method_info.get("route")
    if not route:
        print(f"apimesh: skipping {method_info.get('name')}: no route on the decorator")
        return None
    try:
        context_code_blocks, method_definition_code_block = provide_context_codeblock(
            directory_path, method_info
        )
    except Exception as exc:
        print(f"apimesh: skipping {route}: could not read its source context ({exc})")
        return None
    raw_fragment = _call_swagger_llm(
        method_definition_code_block, context_code_blocks, route,
        http_method=method_info.get("method") or method_info.get("http_method"),
        source_file=method_info.get("file_path"),
    )
    if raw_fragment is None:
        return None
    fragment = _rekey_fragment(
        raw_fragment, route, method_info.get("method") or method_info.get("http_method")
    )
    if fragment is None:
        print(f"apimesh: skipping {route}: the model returned an unusable swagger fragment")
        return fragment
    # The fallback stores the batch-shaped hash, not the hash of its own prompt:
    # the next run recomputes the batch shape, and a hash it could never match
    # would make this endpoint regenerate forever.
    method_info["context_hash"] = _context_hash(
        context_code_blocks, method_definition_code_block
    )
    return fragment


def _update_swagger_for_endpoints(swagger: dict, directory_path: str, endpoints: list):
    """Generate every endpoint behind its own error boundary.

    Returns the endpoints that made it into the spec and the ones that did not,
    so only successful endpoints are written to the api_index.
    """
    generated = []
    failed = []
    for method_info in endpoints:
        fragment = _swagger_fragment_for_endpoint(directory_path, method_info)
        if fragment is None:
            failed.append(method_info)
            continue
        _merge_paths(swagger, fragment)
        generated.append(method_info)
    return generated, failed


def _handler_lines(method_info) -> list:
    """The handler's own source lines, read for pricing its batch section."""
    try:
        with open(method_info.get("file_path") or "", "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    start_line = method_info.get("start_line") or 1
    end_line = method_info.get("end_line") or start_line
    return lines[start_line - 1 : end_line]


def _batch_section_tokens(method_info) -> int:
    """What this endpoint's section costs the batch."""
    return section_token_cost(_batch_label(method_info), "".join(_handler_lines(method_info)))


def _batch_endpoint_jobs(endpoint_jobs: list) -> list:
    """Endpoint jobs grouped by source file, packed to fit the context budget.

    Capping each handler on its own still let ten of them add up to far more
    than the whole prompt can carry, so a batch is closed as soon as the next
    section would push its sections past the budget. Ten endpoints stays the
    secondary limit.
    """
    batches = []
    for jobs in pipeline_common.group_jobs_by_file(endpoint_jobs).values():
        batches.extend(
            pipeline_common.pack_batches(
                jobs,
                _batch_section_tokens,
                MAX_BATCH_ENDPOINTS,
                _EFFECTIVE_CONTEXT_BUDGET,
            )
        )
    return batches


def _batch_source_file(batch: list) -> str:
    if not batch:
        return "unknown file"
    return batch[0].get("file_path") or "unknown file"


def _job_method(method_info):
    return method_info.get("method") or method_info.get("http_method")


def _batch_label(method_info) -> str:
    """The METHOD PATH line the model is asked to echo back as its key.

    A bare Flask @app.route gives the extractor no method, and Flask itself
    defaults such a route to GET, so that is what the model is shown.
    """
    method = (_job_method(method_info) or "GET").upper()
    return f"{method} {_normalize_route(method_info.get('route'))}"


def _batch_reply(entries, context_blocks, source_file):
    """One batch call, with the shared API-error backoff."""
    for delay in OPENAI_RETRY_DELAYS:
        try:
            return get_batch_definition_swagger(entries, context_blocks, source_file)
        except APIError:
            time.sleep(delay)
    return get_batch_definition_swagger(entries, context_blocks, source_file)


def _call_batch_llm(entries, context_blocks, source_file):
    """The model's batch payload, or None when it stays unusable.

    A second unusable reply is not worth a third batch call: the caller spends
    per-endpoint calls on those endpoints instead.
    """
    for _ in range(2):
        try:
            payload = _batch_reply(entries, context_blocks, source_file)
        except APIError as exc:
            print(f"apimesh: giving up on {source_file}: {exc}")
            return None
        except Exception as exc:
            print(f"apimesh: batch failed for {source_file}: {exc}")
            return None
        if payload is not None:
            return payload
    return None


def _batch_entry(directory_path: str, method_info):
    """One endpoint's prompt material: its label, its handler body, its context."""
    context_code_blocks, method_definition_code_block = provide_context_codeblock(
        directory_path, method_info
    )
    return _batch_label(method_info), method_definition_code_block, context_code_blocks


def _context_hash(context_blocks, method_definition) -> str:
    """The one context hash recipe every pipeline shares."""
    return pipeline_common.context_hash(
        context_blocks, method_definition, HANDLER_TOKEN_BUDGET
    )


def _endpoint_context_hash(directory_path: str, method_info):
    """This endpoint's context hash, or None when its source cannot be read."""
    try:
        _, body, blocks = _batch_entry(directory_path, method_info)
    except Exception:
        return None
    return _context_hash(blocks, body)


def _generate_batch_payload(directory_path: str, batch: list):
    """One LLM call for a file's endpoints. Returns the model's raw payload."""
    entries = []
    context_blocks = []
    for method_info in batch:
        label, body, blocks = _batch_entry(directory_path, method_info)
        method_info["context_hash"] = _context_hash(blocks, body)
        entries.append((label, "".join(body)))
        context_blocks.extend(blocks)
    return _call_batch_llm(entries, context_blocks, _batch_source_file(batch))


def _operation_from_batch(payload, route, method):
    """The operation the model returned for one requested endpoint.

    The model rewrites the path it is given, so both sides are normalized before
    they are compared and the result is re-keyed under the extractor's route,
    exactly as the per-endpoint path does.
    """
    if not isinstance(payload, dict):
        return None
    paths = payload.get("paths")
    if not isinstance(paths, dict):
        return None
    normalized_route = _normalize_route(route)
    if not normalized_route:
        return None
    for model_route, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        if _normalize_route(model_route) != normalized_route:
            continue
        for name, value in path_item.items():
            if (
                not isinstance(value, dict)
                or str(name).lower() not in pipeline_common.HTTP_VERB_KEYS
            ):
                continue
            # Without a method on the decorator the model's own verb is the only
            # one available, which is what the per-endpoint path does too.
            if method and str(name).lower() != method.lower():
                continue
            method_key = method.lower() if method else str(name).lower()
            return {
                "paths": {
                    normalized_route: {
                        method_key: pipeline_common.normalize_operation_fields(value)
                    }
                }
            }
    return None


def _apply_batch_payload(swagger: dict, directory_path: str, batch: list, payload):
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
    generated = []
    failed = []
    for method_info in batch:
        fragment = _operation_from_batch(
            payload, method_info.get("route"), _job_method(method_info)
        )
        if fragment is None:
            failed.append(method_info)
            print(
                f"apimesh: skipping {method_info.get('route')}: "
                "missing from the batch reply"
            )
            continue
        _merge_paths(swagger, fragment)
        generated.append(method_info)
    return generated, failed


def _update_swagger_for_batches(swagger: dict, directory_path: str, endpoint_jobs: list):
    """Every endpoint of every batch, behind the batch's own error boundary."""
    generated = []
    failed = []
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
    """Raising on a total wipeout lets the CLI fall back to the generic extractor."""
    total = generated + failed
    if not total:
        return
    print(f"apimesh: generated {generated} of {total} endpoints ({failed} failed)")
    if generated == 0:
        raise RuntimeError("apimesh: every python endpoint failed swagger generation")


def _unchanged_context_keys(directory_path: str, keys, endpoint_map, existing_index) -> set:
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

    jobs_to_update = []
    for key in keys_to_update:
        jobs_to_update.extend(endpoint_map.get(key, []))
    generated, failed = _update_swagger_for_batches(
        existing_swagger, directory_path, jobs_to_update
    )
    failed_keys = {
        _endpoint_key(job.get("route"), _job_method(job)) for job in failed
    }
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

def _routed_endpoints(endpoint_jobs: list) -> list:
    """The jobs that can actually be documented, each of them once.

    A decorator that carries no route (a bare ``@api_view``, whose route lives
    in the URLconf) can never produce a spec entry, and counting it as a
    failure hides the endpoints that did work. The same handler reached from
    both the decorator and the URLconf is one endpoint, not two.
    """
    unique = []
    seen = set()
    for job in endpoint_jobs:
        route = job.get("route")
        if not route:
            continue
        key = (
            job.get("file_path"),
            job.get("start_line"),
            job.get("end_line"),
            _normalize_route(route),
            (_job_method(job) or "").upper(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


def run_swagger_generation(host):
    directory_path = get_repo_path()
    repo_name = get_repo_name()
    _reset_metadata_state()
    # Built before the try: a walk that dies leaves an incomplete picture of
    # which entries are still live, and pruning against it would be wrong.
    _build_metadata_cache(directory_path)
    try:
        python_files = find_python_files(directory_path)
        # Blueprints are often mounted from a file that defines no route itself,
        # so registrations are collected from every scanned file.
        external_prefixes = collect_external_prefixes(python_files, directory_path)
        all_endpoints_dict = dict()
        for py_file, tree in find_api_definition_sources(directory_path):
            eps = find_api_endpoints(
                py_file,
                external_prefixes.get(os.path.abspath(str(py_file))),
                tree=tree,
            )
            if eps:
                all_endpoints_dict[str(py_file)] = eps
        endpoint_jobs = []
        for value in all_endpoints_dict.values():
            for item in value:
                if item.get('type') == 'class':
                    endpoint_jobs.extend(item.get('methods', []))
                else:
                    endpoint_jobs.append(item)
        # Django declares its routes in urls.py, which carries no decorator at
        # all, so nothing above sees a conventional Django project.
        endpoint_jobs.extend(collect_django_endpoints(python_files, directory_path))
        endpoint_jobs = _routed_endpoints(endpoint_jobs)
        if not endpoint_jobs:
            print("apimesh: python parser found 0 endpoints, falling back to generic extraction")
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
                "x-github-repo-url": get_github_repo_url()
            },
            "servers": [
                {
                    "url": host
                }
            ],
            "paths": {}
        }
        generated, failed = _update_swagger_for_batches(swagger, directory_path, endpoint_jobs)
        _report_generation(len(generated), len(failed))
        # Only endpoints that made it into the spec are indexed, otherwise a
        # failure looks unchanged next run and is never retried.
        _write_api_index(_build_api_index(generated))
        pipeline_common.record_coverage(swagger, len(endpoint_jobs), len(generated), 0, len(failed))
        return swagger
    finally:
        # The cache outlives the run; only entries for content that is gone are
        # dropped, so the next run parses just what changed.
        _prune_metadata_cache()


def get_dependencies(data, start_line, end_line, file_path):
    existing_function_names = [item['name'] for item in data['elements']['functions'] if item['name'] not in ['get', 'post', 'put', 'delete', 'patch']]
    in_file_dependency_functions = []
    for item in data['elements']['function_calls']:
        if (item['name'] in existing_function_names) and item['start_line'] >= start_line and item['end_line'] <= end_line:
            item['file_path'] = file_path
            in_file_dependency_functions.append(item)
    imported_functions = []
    for item in data['imports']:
        if not item['path_exists']:
            continue
        for k in item['usage_lines']:
            if start_line<=k<=end_line:
                imported_functions.append(item)
            if in_file_dependency_functions:
                for item1 in in_file_dependency_functions:
                    if item1['start_line'] <= k <= item1['end_line'] and item not in imported_functions:
                        imported_functions.append(item)
    return in_file_dependency_functions, imported_functions

def _source_lines(file_name):
    with open(file_name, "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()


def get_code_blocks(in_file_dependency_functions, imported_functions, file_name, directory_path):
    code_blocks = []
    for block in in_file_dependency_functions:
        lines = _source_lines(file_name)
        # The whole helper is the context, not its signature line.
        code_blocks.append(lines[block['function_start_line'] - 1 : block['function_end_line']])
    for func in imported_functions:
        visited = False
        file_name = func['origin']
        complete_json_file_path = _metadata_cache_path(file_name)
        # A module imported from an ignored directory was never processed, so
        # it has no metadata and simply contributes no context.
        if not complete_json_file_path or not os.path.exists(complete_json_file_path):
            continue
        with open(complete_json_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data['elements']['classes']:
            if item['name'] == func['imported_name']:
                visited = True
                lines = _source_lines(file_name)
                code_blocks.append(lines[item['start_line']-1: item['end_line']])
                break
        if not visited:
            for item in data['elements']['functions']:
                if item['name'] == func['imported_name']:
                    visited = True
                    lines = _source_lines(file_name)
                    code_blocks.append(lines[item['start_line'] - 1: item['end_line']])
                    break
        if not visited:
            for item in data['elements']['variables']:
                if item['name'] == func['imported_name']:
                    lines = _source_lines(file_name)
                    code_blocks.append(lines[item['start_line'] - 1: item['end_line']])
                    break
    return code_blocks


def provide_context_codeblock(directory_path, method_info):
    file_name = method_info['file_path']
    lines = _source_lines(file_name)
    method_definition_code_block = lines[method_info["start_line"]-1: method_info["end_line"]]
    complete_json_file_path = _metadata_cache_path(file_name)
    if not complete_json_file_path:
        raise FileNotFoundError(f"no metadata cached for {file_name}")
    with open(complete_json_file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    in_file_dependency_functions, imported_functions = get_dependencies(data, method_info["start_line"], method_info["end_line"], method_info['file_path'])
    context_code_blocks = get_code_blocks(in_file_dependency_functions, imported_functions, file_name, directory_path)
    return context_code_blocks, method_definition_code_block

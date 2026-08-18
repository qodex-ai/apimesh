import json
import os
import re
import shutil
import tempfile
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import Configurations
from golang_pipeline.definition_swagger_generator import (
    CONTEXT_TOKEN_BUDGET,
    get_batch_definition_swagger,
    get_function_definition_swagger,
    section_token_cost,
)

# Headroom for the separators joined between sections and blocks, so the
# budget holds for the final assembled prompt, not just the parts.
_EFFECTIVE_CONTEXT_BUDGET = CONTEXT_TOKEN_BUDGET - 64
from golang_pipeline.find_api_definition_files import find_api_definition_files
from golang_pipeline.generate_file_information import process_file
from golang_pipeline.identify_api_functions import find_api_endpoints
from utils import (
    get_git_commit_hash,
    get_github_repo_url,
    get_repo_path,
    get_repo_name,
    get_output_filepath,
    get_changed_files_since,
)

config = Configurations()

_FUNCTION_INDEX_CACHE: Dict[str, List[Dict[str, object]]] = {}
_FUNCTION_INDEX_CACHE_ROOT: Optional[str] = None
_FILE_CONTENT_CACHE: Dict[str, List[str]] = {}
_METADATA_DIR: Optional[str] = None
_HEADER_PATTERN = re.compile(
    r"""\.Get(?:String|Header)\(\s*["']([^"']+)["']\s*\)|
        Header\.Get\(\s*["']([^"']+)["']\s*\)""",
    re.VERBOSE,
)
_ROUTE_PARAM_PATTERN = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")
# One LLM call documents a whole file. Past ten endpoints the shared context
# budget leaves too little room per endpoint, so a file is chunked.
_MAX_BATCH_ENDPOINTS = 10
_HTTP_OPERATIONS = {
    "get",
    "post",
    "put",
    "delete",
    "patch",
    "options",
    "head",
    "trace",
}
# Operation keys older prompts asked for, mapped to their OpenAPI 3.0 compliant form.
_LEGACY_OPERATION_FIELDS = {
    "api_description": "description",
    "authorization_tag": "x-authorization-tag",
    "module_tag": "x-module-tag",
    "auth_tag": "x-auth-tag",
    "sensitive_information": "x-sensitive-information",
}


def should_process_directory(dir_path: str, repo_root: str) -> bool:
    # Only components below the scanned repo may be ignored, otherwise a repo
    # living under /var, /tmp or /build processes nothing.
    path_parts = os.path.relpath(dir_path, repo_root).split(os.sep)
    return not any(part in config.ignored_dirs for part in path_parts)


def _api_index_output_path() -> str:
    output_dir = os.path.dirname(get_output_filepath())
    os.makedirs(output_dir, exist_ok=True)
    return os.path.join(output_dir, "api_index.json")


def _load_file_metadata(file_path: str):
    metadata_dir = _METADATA_DIR
    if not metadata_dir:
        return None
    json_file = os.path.join(metadata_dir, _sanitize_json_filename(file_path))
    if not os.path.exists(json_file):
        return None
    try:
        with open(json_file, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _normalize_route(route) -> str:
    """
    Canonical path for a route the extractor found. Go routers write params as
    :id while OpenAPI expects {id}, and the save step rewrites them anyway, so
    the swagger key, the api_index key and the removal lookup all use this one
    form and stay matched across runs. Wildcards (*name) are left alone.
    """
    if not route or not isinstance(route, str):
        return ""
    normalized = _ROUTE_PARAM_PATTERN.sub(r"{\1}", route.strip())
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized


def _first_operation(path_item) -> Optional[Dict]:
    """
    The operation body of a path item. A path item legally carries members that
    are not operations ("parameters", vendor extensions), so only an HTTP verb
    with a dict body counts. A path item without one contributes no operation,
    which is what keeps a vendor-extension-only fragment out of the spec.
    """
    if not isinstance(path_item, dict):
        return None
    for name, value in path_item.items():
        if str(name).lower() in _HTTP_OPERATIONS and isinstance(value, dict):
            return value
    return None


def _normalize_operation_fields(operation: Dict) -> Dict:
    """
    Rename the legacy operation keys an older model reply may still carry to
    their OpenAPI compliant form. A value already under the new name wins, so a
    reply holding both does not end up with duplicated content.
    """
    if not isinstance(operation, dict):
        return operation
    for legacy_key, new_key in _LEGACY_OPERATION_FIELDS.items():
        if legacy_key not in operation:
            continue
        operation.setdefault(new_key, operation.pop(legacy_key))
    return operation


def _endpoint_key(route, method):
    method_value = (method or "UNKNOWN").upper()
    route_value = _normalize_route(route)
    return f"{method_value} {route_value}".strip()


def _normalize_in_file_dependencies(deps, route, file_path):
    imports = []
    for dep in deps:
        start_line = dep.get("function_start_line") or dep.get("start_line")
        end_line = dep.get("function_end_line") or dep.get("end_line")
        name = dep.get("name")
        if not name or not isinstance(start_line, int) or not isinstance(end_line, int):
            continue
        imports.append(
            {
                "type": "function",
                "name": name,
                "start_line": start_line,
                "end_line": end_line,
                "route": route,
                "file_path": file_path,
            }
        )
    return imports


def _resolve_imported_definitions(import_item, route):
    origin = import_item.get("origin")
    imported_name = import_item.get("imported_name")
    if not origin or not imported_name:
        return []
    candidates = []
    origin_paths = []
    if os.path.isdir(origin):
        for name in os.listdir(origin):
            if name.endswith(".go"):
                origin_paths.append(os.path.join(origin, name))
    else:
        origin_paths.append(origin)

    name_candidates = [imported_name]
    if "." in imported_name:
        name_candidates.append(imported_name.split(".")[-1])

    for origin_path in origin_paths:
        metadata = _load_file_metadata(origin_path)
        if not metadata:
            continue
        elements = metadata.get("elements", {})
        for key in ("functions", "types"):
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
                        "file_path": origin_path,
                    }
                )
                break
            if candidates:
                break
        if candidates:
            break
    return candidates


def _dedupe_imports(imports):
    seen = set()
    unique = []
    for item in imports:
        key = (
            item.get("file_path"),
            item.get("name"),
            item.get("start_line"),
            item.get("end_line"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _merge_file_entry(files, entry):
    for existing in files:
        if existing.get("file_path") == entry.get("file_path"):
            merged = existing.get("imports", []) + entry.get("imports", [])
            existing["imports"] = _dedupe_imports(merged)
            return
    files.append(entry)


def _build_api_index(endpoints: list) -> dict:
    api_index = {}
    for endpoint in endpoints:
        route = endpoint.get("route")
        method = endpoint.get("http_method") or endpoint.get("method")
        key = _endpoint_key(route, method)
        file_path = endpoint.get("file_path")
        if not file_path:
            continue
        abs_file_path = os.path.abspath(file_path)
        imports = []
        start_line = endpoint.get("start_line")
        end_line = endpoint.get("end_line")
        if isinstance(start_line, int) and isinstance(end_line, int):
            metadata = _load_file_metadata(abs_file_path)
            if metadata:
                in_file, imported = get_dependencies(
                    metadata, start_line, end_line, abs_file_path
                )
                imports.extend(_normalize_in_file_dependencies(in_file, route, abs_file_path))
                for item in imported:
                    imports.extend(_resolve_imported_definitions(item, route))
        entry = {
            "file_path": abs_file_path,
            "imports": _dedupe_imports(imports),
        }
        api_index.setdefault(key, {"files": []})
        _merge_file_entry(api_index[key]["files"], entry)
    return api_index


def _write_api_index(api_index: dict) -> None:
    output_path = _api_index_output_path()
    try:
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(api_index, handle, indent=2)
    except Exception:
        return


def _load_existing_swagger():
    swagger_path = get_output_filepath()
    if not os.path.exists(swagger_path):
        return None
    try:
        with open(swagger_path, "r", encoding="utf-8") as handle:
            return _migrate_legacy_spec(json.load(handle))
    except Exception:
        return None

_LEGACY_INFO_FIELDS = {
    "generated_at": "x-generated-at",
    "commit_reference": "x-commit-reference",
    "github_repo_url": "x-github-repo-url",
}


def _migrate_legacy_spec(swagger):
    """Upgrade a pre-x-extension spec in place so incremental runs never write
    the legacy spellings back out. New keys win when both exist."""
    if not isinstance(swagger, dict):
        return swagger
    info = swagger.get("info")
    if isinstance(info, dict):
        for old_key, new_key in _LEGACY_INFO_FIELDS.items():
            if old_key in info:
                value = info.pop(old_key)
                info.setdefault(new_key, value)
    paths = swagger.get("paths")
    if isinstance(paths, dict):
        for path_item in paths.values():
            if not isinstance(path_item, dict):
                continue
            for operation in path_item.values():
                if isinstance(operation, dict):
                    _normalize_operation_fields(operation)
    return swagger



def _load_existing_api_index():
    api_index_path = _api_index_output_path()
    if not os.path.exists(api_index_path):
        return None
    try:
        with open(api_index_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _group_endpoints(endpoints: list) -> dict:
    grouped = {}
    for endpoint in endpoints:
        key = _endpoint_key(endpoint.get("route"), endpoint.get("http_method") or endpoint.get("method"))
        grouped.setdefault(key, []).append(endpoint)
    return grouped


def _endpoint_has_changed(existing_entry, endpoints_for_key, changed_files: set) -> bool:
    if existing_entry:
        for file_entry in existing_entry.get("files", []):
            file_path = file_entry.get("file_path")
            if file_path and os.path.abspath(file_path) in changed_files:
                return True
            for imp in file_entry.get("imports", []):
                imp_path = imp.get("file_path")
                if imp_path and os.path.abspath(imp_path) in changed_files:
                    return True
    for endpoint in endpoints_for_key or []:
        file_path = endpoint.get("file_path")
        if file_path and os.path.abspath(file_path) in changed_files:
            return True
    return False


def _split_endpoint_key(key: str):
    if not key:
        return "UNKNOWN", ""
    parts = key.split(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _remove_endpoint_from_swagger(swagger: dict, key: str) -> None:
    method, route = _split_endpoint_key(key)
    # An index written before routes were canonicalized still holds :id keys.
    route = _normalize_route(route)
    if not route:
        return
    paths = swagger.get("paths", {})
    if route not in paths:
        return
    if method == "UNKNOWN":
        paths.pop(route, None)
        return
    method_lower = method.lower()
    if method_lower in paths.get(route, {}):
        del paths[route][method_lower]
        if not paths[route]:
            del paths[route]


def _normalize_swagger_fragment(
    fragment, route: str, http_method: Optional[str]
) -> Optional[Dict]:
    """Re-key an LLM fragment under the route and method the extractor found.

    The model happily rewrites paths (/users/:id comes back as /users/{id}),
    which would diverge from the api_index keys and make incremental removal
    impossible, so only the first operation body is kept and re-keyed.
    """
    if not isinstance(fragment, dict):
        return None
    paths = fragment.get("paths")
    if not isinstance(paths, dict) or not paths:
        return None
    route_key = _normalize_route(route)
    if not route_key:
        return None
    for path_item in paths.values():
        operation = _first_operation(path_item)
        if operation is None:
            continue
        return {
            route_key: {
                (http_method or "GET").lower(): _normalize_operation_fields(operation)
            }
        }
    return None


def _merge_paths(target: Dict, source: Dict) -> None:
    for path_key, methods in source.get("paths", {}).items():
        target.setdefault("paths", {})
        target["paths"].setdefault(path_key, {})
        for method, payload in methods.items():
            target["paths"][path_key][method] = payload


def _generate_swagger_fragment(directory_path: str, method_info: Dict) -> Dict:
    context_blocks, method_definition = provide_context_codeblock(
        directory_path, method_info
    )
    http_method = method_info.get("http_method") or "GET"
    if http_method:
        context_blocks = [[f"HTTP_METHOD: {http_method}\n"]] + context_blocks
    handler_metadata = method_info.get("handler_selector") or method_info.get("name")
    if handler_metadata:
        context_blocks = [[f"HANDLER: {handler_metadata}\n"]] + context_blocks
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
    http_method = method_info.get("http_method") or "GET"
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
        _merge_paths(swagger, {"paths": normalized})
        generated.append(method_info)
    return generated, failed


def _handler_lines(job: Dict) -> List[str]:
    """The handler's own source lines, read for pricing its batch section."""
    lines = _read_file_lines(job.get("file_path") or "") or []
    start_line = job.get("start_line") or 1
    end_line = job.get("end_line") or start_line
    return lines[start_line - 1 : end_line]


def _batch_section_tokens(job: Dict) -> int:
    """What this endpoint's section costs the batch, header hint included."""
    method_definition = _handler_lines(job)
    header_block = _build_header_hint_block(method_definition)
    body = (header_block or []) + method_definition
    return section_token_cost(_batch_label(job), "".join(body))


def _batch_endpoint_jobs(endpoint_jobs: List[Dict]) -> List[List[Dict]]:
    """Endpoint jobs grouped by source file, packed to fit the context budget.

    Capping each handler on its own still let ten of them add up to far more
    than the whole prompt can carry, so a batch is closed as soon as the next
    section would push its sections past the budget. Ten endpoints stays the
    secondary limit.
    """
    by_file: Dict[str, List[Dict]] = {}
    for job in endpoint_jobs:
        by_file.setdefault(job.get("file_path") or "", []).append(job)
    batches: List[List[Dict]] = []
    for jobs in by_file.values():
        current: List[Dict] = []
        used = 0
        for job in jobs:
            cost = _batch_section_tokens(job)
            if current and (
                len(current) >= _MAX_BATCH_ENDPOINTS or used + cost > _EFFECTIVE_CONTEXT_BUDGET
            ):
                batches.append(current)
                current = []
                used = 0
            current.append(job)
            used += cost
        if current:
            batches.append(current)
    return batches


def _batch_source_file(batch: List[Dict]) -> str:
    if not batch:
        return "unknown file"
    return batch[0].get("file_path") or "unknown file"


def _batch_label(job: Dict) -> str:
    """The METHOD PATH line the model is asked to echo back as its key."""
    method = (job.get("http_method") or "GET").upper()
    return f"{method} {_normalize_route(job.get('route'))}"


def _generate_batch_payload(directory_path: str, batch: List[Dict]) -> Optional[Dict]:
    """One LLM call for a file's endpoints. Returns the model's raw payload."""
    entries: List[Tuple[str, str]] = []
    context_blocks: List[List[str]] = []
    for job in batch:
        blocks, method_definition = provide_context_codeblock(directory_path, job)
        # The header hint is read off this one handler, so it belongs to its
        # section rather than to the context the whole file shares.
        header_block = _build_header_hint_block(method_definition)
        body = method_definition
        if header_block:
            blocks = [block for block in blocks if block != header_block]
            body = header_block + method_definition
        entries.append((_batch_label(job), "".join(body)))
        context_blocks.extend(blocks)
    return get_batch_definition_swagger(
        entries, context_blocks, _batch_source_file(batch)
    )


def _operation_from_batch(
    payload, route: str, http_method: Optional[str]
) -> Optional[Dict]:
    """The operation the model returned for one requested endpoint.

    The model rewrites paths (/users/:id comes back as /users/{id}), so both
    sides are normalized before they are compared, and the result is re-keyed
    under the extractor's route exactly as the per-endpoint path does.
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
                return {route_key: {method_key: _normalize_operation_fields(value)}}
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
        http_method = job.get("http_method") or "GET"
        normalized = _operation_from_batch(payload, route, http_method)
        if normalized is None:
            failed.append(job)
            print(
                f"apimesh: skipped {http_method} {route}: "
                "missing from the batch reply"
            )
            continue
        _merge_paths(swagger, {"paths": normalized})
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
    """Raising on a total wipeout lets the CLI fall back to the generic extractor."""
    total = generated + failed
    if not total:
        return
    print(f"generated {generated} of {total} endpoints ({failed} failed)")
    if generated == 0:
        raise RuntimeError("golang swagger generation produced no endpoints")


def _maybe_incremental_update(directory_path: str, endpoint_jobs: list):
    existing_swagger = _load_existing_swagger()
    existing_index = _load_existing_api_index()
    if not existing_swagger or not isinstance(existing_index, dict):
        return None
    existing_info = existing_swagger.get("info", {})
    # Specs written before the extension rename still carry the bare key.
    base_commit = existing_info.get("x-commit-reference") or existing_info.get("commit_reference")
    if not base_commit:
        return None
    changed_files = get_changed_files_since(base_commit, directory_path, include_uncommitted=True)
    if changed_files is None:
        return None
    endpoint_map = _group_endpoints(endpoint_jobs)
    existing_keys = set(existing_index.keys())
    new_keys = set(endpoint_map.keys())
    removed_keys = existing_keys - new_keys
    added_keys = new_keys - existing_keys
    # An endpoint that failed last run is absent from the index, so it reads as
    # added and still has to be generated when git reports nothing changed.
    if not changed_files and not added_keys and not removed_keys:
        return existing_swagger
    changed_keys = set()
    for key in existing_keys & new_keys:
        if _endpoint_has_changed(existing_index.get(key), endpoint_map.get(key), changed_files):
            changed_keys.add(key)

    keys_to_update = added_keys | changed_keys
    updated_index = dict(existing_index)

    for key in removed_keys:
        updated_index.pop(key, None)
        _remove_endpoint_from_swagger(existing_swagger, key)

    jobs_to_update: List[Dict] = []
    for key in keys_to_update:
        jobs_to_update.extend(endpoint_map.get(key, []))
    generated, failed = _update_swagger_for_batches(
        existing_swagger, directory_path, jobs_to_update
    )
    failed_keys = {
        _endpoint_key(job.get("route"), job.get("http_method") or job.get("method"))
        for job in failed
    }
    # A failed endpoint keeps whatever the index already held (or stays out of
    # it) so the next run still sees it as new or stale and retries.
    # A failed key's stale entry is dropped, not kept: once the commit
    # reference advances, a kept entry would hide the failure forever, while
    # an absent key reads as newly added and is retried on the next run.
    for failed_key in failed_keys:
        updated_index.pop(failed_key, None)

    for entry_key, entry_value in _build_api_index(generated).items():
        if entry_key in failed_keys:
            continue
        updated_index[entry_key] = entry_value
    _report_generation(len(generated), len(failed))

    info = existing_swagger.setdefault("info", {})
    info.pop("commit_reference", None)
    info["x-commit-reference"] = get_git_commit_hash()
    _write_api_index(updated_index)
    return existing_swagger


def _sanitize_json_filename(file_path: str) -> str:
    return f"{file_path.replace(os.sep, '_q_')}.json"


def _ensure_function_index(directory_path: str) -> Dict[str, List[Dict[str, object]]]:
    global _FUNCTION_INDEX_CACHE
    global _FUNCTION_INDEX_CACHE_ROOT

    if _FUNCTION_INDEX_CACHE and _FUNCTION_INDEX_CACHE_ROOT == directory_path:
        return _FUNCTION_INDEX_CACHE

    _FUNCTION_INDEX_CACHE = {}
    _FUNCTION_INDEX_CACHE_ROOT = directory_path
    metadata_dir = _METADATA_DIR
    if not metadata_dir or not os.path.exists(metadata_dir):
        return _FUNCTION_INDEX_CACHE

    for entry in os.scandir(metadata_dir):
        if not entry.is_file() or not entry.name.endswith(".json"):
            continue
        try:
            with open(entry.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        elements = data.get("elements", {})
        functions = elements.get("functions", [])
        for func in functions:
            name = func.get("name")
            start_line = func.get("start_line")
            end_line = func.get("end_line")
            file_name = data.get("filename") or func.get("file_path")
            if (
                not name
                or not isinstance(start_line, int)
                or not isinstance(end_line, int)
                or not file_name
            ):
                continue
            _FUNCTION_INDEX_CACHE.setdefault(name, []).append(
                {
                    "file_path": file_name,
                    "start_line": start_line,
                    "end_line": end_line,
                }
            )
    return _FUNCTION_INDEX_CACHE


def _find_function_definition(
    directory_path: str,
    function_name: str,
    preferred_file: Optional[str] = None,
    route_file: Optional[str] = None,
) -> Optional[Dict[str, object]]:
    index = _ensure_function_index(directory_path)
    entries = index.get(function_name, [])
    if not entries:
        return None
    if preferred_file:
        for entry in entries:
            if entry.get("file_path") == preferred_file:
                return entry
    if route_file:
        route_path = Path(route_file)
        route_stem = route_path.stem
        tokens: List[str] = []
        if route_stem.endswith("_route"):
            tokens.append(route_stem[: -len("_route")])
            tokens.append(route_stem[: -len("_route")] + "_controller")
        tokens.append(route_stem.replace("route", "controller"))
        best_entry = None
        best_score = -1
        for entry in entries:
            score = 0
            file_path = entry.get("file_path") or ""
            if "controller" in file_path:
                score += 5
            for token in tokens:
                if token and token in file_path:
                    score += 10
            if score > best_score:
                best_score = score
                best_entry = entry
        if best_entry:
            return best_entry
    return entries[0]


def _hydrate_method_info(
    directory_path: str, method_info: Dict[str, object]
) -> Optional[Dict[str, object]]:
    if method_info.get("start_line") and method_info.get("end_line") and method_info.get("file_path" ):
        return method_info

    handler_name = method_info.get("handler_name") or method_info.get("name")
    if not handler_name:
        return None

    preferred_file = method_info.get("file_path")
    definition = _find_function_definition(
        directory_path, handler_name, preferred_file, method_info.get("route_file")
    )
    if not definition:
        return None

    method_info = method_info.copy()
    method_info["file_path"] = definition.get("file_path")
    method_info["start_line"] = definition.get("start_line")
    method_info["end_line"] = definition.get("end_line")
    return method_info


def _read_file_lines(file_path: str) -> Optional[List[str]]:
    cached = _FILE_CONTENT_CACHE.get(file_path)
    if cached is not None:
        return cached
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
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
        usage_lines = item.get("usage_lines", [])
        if not usage_lines:
            continue
        for usage in usage_lines:
            if start_line <= usage <= end_line:
                imported_functions.append(item)
                break
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

    metadata_dir = _METADATA_DIR
    if not metadata_dir:
        return code_blocks
    for imp in imported_functions:
        origin = imp.get("origin")
        if not origin:
            continue
        if os.path.isdir(origin):
            candidates = [
                os.path.join(origin, name)
                for name in os.listdir(origin)
                if name.endswith(".go")
            ]
        else:
            candidates = [origin] if origin.endswith(".go") else []
        for candidate in candidates:
            json_file = os.path.join(
                metadata_dir, _sanitize_json_filename(candidate)
            )
            if not os.path.exists(json_file):
                continue
            try:
                with open(json_file, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            elements = data.get("elements", {})
            for func in elements.get("functions", []):
                if func.get("name") == imp.get("imported_name"):
                    origin_lines = _read_file_lines(candidate) or []
                    snippet = origin_lines[
                        func.get("start_line", 1) - 1 : func.get("end_line", 1)
                    ]
                    if snippet:
                        code_blocks.append(snippet)
                    break
    return code_blocks


def _extract_header_names(method_lines: List[str]) -> List[str]:
    text = "".join(method_lines)
    headers: List[str] = []
    for match in _HEADER_PATTERN.finditer(text):
        name = match.group(1) or match.group(2)
        if name:
            headers.append(name)
    return sorted(set(headers))


def _build_header_hint_block(method_lines: List[str]) -> Optional[List[str]]:
    header_names = _extract_header_names(method_lines)
    if not header_names:
        return None
    block = [
        "# Request headers referenced directly in this handler.\n",
        "# Document each of these headers as required parameters when applicable.\n",
    ]
    for name in header_names:
        block.append(f"# header: {name}\n")
    return block


def _load_types_from_origin(
    origin: str, alias: Optional[str], per_alias_limit: int
) -> List[List[str]]:
    metadata_dir = _METADATA_DIR
    if not metadata_dir:
        return []
    file_candidates: List[str] = []
    if os.path.isdir(origin):
        for entry in os.scandir(origin):
            if entry.is_file() and entry.name.endswith(".go"):
                file_candidates.append(entry.path)
    elif origin.endswith(".go"):
        file_candidates.append(origin)
    blocks: List[List[str]] = []
    collected = 0
    for candidate in file_candidates:
        json_file = os.path.join(metadata_dir, _sanitize_json_filename(candidate))
        if not os.path.exists(json_file):
            continue
        try:
            with open(json_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        type_entries = data.get("elements", {}).get("types", [])
        if not type_entries:
            continue
        for type_entry in type_entries:
            start = type_entry.get("start_line")
            end = type_entry.get("end_line")
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            lines = _read_file_lines(candidate)
            if lines is None:
                continue
            qualifier = f"{alias}." if alias else ""
            header = [f"# Type {qualifier}{type_entry.get('name')} from {candidate}\n"]
            blocks.append(header + lines[start - 1 : end])
            collected += 1
            if per_alias_limit and collected >= per_alias_limit:
                return blocks
    return blocks


def _collect_import_type_blocks(
    imports: List[Dict], per_alias_limit: int = 3
) -> List[List[str]]:
    if not imports:
        return []
    blocks: List[List[str]] = []
    seen: set = set()
    for import_entry in imports:
        if not import_entry.get("path_exists"):
            continue
        origin = import_entry.get("origin")
        if not origin:
            continue
        alias = import_entry.get("alias") or import_entry.get("imported_name")
        key = (alias, origin)
        if key in seen:
            continue
        seen.add(key)
        type_blocks = _load_types_from_origin(origin, alias, per_alias_limit)
        blocks.extend(type_blocks)
    return blocks




def provide_context_codeblock(directory_path: str, method_info: Dict):
    file_name = method_info["file_path"]
    lines = _read_file_lines(file_name) or []
    start_line = method_info.get("start_line", 1)
    end_line = method_info.get("end_line", start_line)
    method_definition_code_block = lines[start_line - 1 : end_line]

    metadata_dir = _METADATA_DIR
    data = {"elements": {"functions": [], "function_calls": []}, "imports": []}
    if metadata_dir:
        json_file = os.path.join(metadata_dir, _sanitize_json_filename(file_name))
        try:
            with open(json_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            data = {"elements": {"functions": [], "function_calls": []}, "imports": []}

    in_file_dependency_functions, imported_functions = get_dependencies(
        data, start_line, end_line, file_name
    )
    context_code_blocks = get_code_blocks(
        in_file_dependency_functions, imported_functions, file_name, directory_path
    )
    header_block = _build_header_hint_block(method_definition_code_block)
    type_blocks = _collect_import_type_blocks(data.get("imports", []))
    prefix_blocks: List[List[str]] = []
    if header_block:
        prefix_blocks.append(header_block)
    prefix_blocks.extend(type_blocks)
    context_code_blocks = prefix_blocks + context_code_blocks
    return context_code_blocks, method_definition_code_block


def run_swagger_generation(host: str) -> Optional[Dict]:
    directory_path = get_repo_path()
    repo_name = get_repo_name()
    global _METADATA_DIR
    metadata_dir = tempfile.mkdtemp(prefix="qodex_go_file_info_")
    _METADATA_DIR = metadata_dir

    try:
        for root, _, files in os.walk(directory_path):
            for filename in files:
                file_path = os.path.join(root, filename)
                if (
                    os.path.exists(file_path)
                    and should_process_directory(file_path, directory_path)
                    and file_path.endswith(".go")
                ):
                    try:
                        file_info = process_file(file_path, directory_path)
                    except Exception:
                        continue
                    json_file_name = os.path.join(
                        metadata_dir, _sanitize_json_filename(file_path)
                    )
                    with open(json_file_name, "w", encoding="utf-8") as handle:
                        json.dump(file_info, handle, indent=4)

        api_files = find_api_definition_files(directory_path)
        endpoints: List[Dict] = []
        for file in api_files:
            endpoints.extend(find_api_endpoints(Path(file), directory_path))

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
        for endpoint in endpoints:
            hydrated = _hydrate_method_info(directory_path, endpoint)
            if not hydrated:
                continue
            endpoint_jobs.append(hydrated)

        # Nothing was extracted: hand back None so the caller falls back instead
        # of publishing an empty spec (or, worse, incrementally deleting one).
        if not endpoint_jobs:
            print(
                "apimesh: golang parser found 0 endpoints, "
                "falling back to generic extraction"
            )
            return None

        incremental_swagger = _maybe_incremental_update(directory_path, endpoint_jobs)
        if incremental_swagger is not None:
            return incremental_swagger

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

        return swagger
    finally:
        if metadata_dir and os.path.exists(metadata_dir):
            shutil.rmtree(metadata_dir, ignore_errors=True)
        _METADATA_DIR = None

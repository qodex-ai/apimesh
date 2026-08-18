import copy
import json
import os
import re
import shutil
import time
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import Configurations
from utils import (
    get_git_commit_hash,
    get_github_repo_url,
    get_repo_path,
    get_repo_name,
    get_output_filepath,
    get_changed_files_since,
    num_tokens_from_string,
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
    find_api_endpoints,
)

config = Configurations()


_CLASS_INDEX_CACHE: Dict[str, Dict[str, object]] = {}
_CLASS_INDEX_CACHE_ROOT: Optional[str] = None
_CLASS_CODE_BLOCK_CACHE: Dict[str, List[str]] = {}
_FILE_CONTENT_CACHE: Dict[str, List[str]] = {}
_FUNCTION_INDEX_CACHE: Dict[str, List[Dict[str, object]]] = {}

_PARAM_PATTERN = re.compile(r"params\[(?::|['\"])([A-Za-z0-9_]+)['\"]?\]")
_PARAM_HINT_FUNCTIONS = {"apply_filters"}
_ROUTE_PARAM_PATTERN = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")

# Keys of an OpenAPI path item that hold an operation body.
HTTP_VERB_KEYS = {"get", "post", "put", "delete", "patch", "options", "head", "trace"}

# One LLM call documents at most this many endpoints of the same controller.
MAX_ENDPOINTS_PER_BATCH = 10

# Combined token cap for the shared context plus every handler body in one prompt.
CONTEXT_TOKEN_BUDGET = 6000

# Headroom for the separators joined between sections and blocks, so the
# budget holds for the final assembled prompt, not just the parts.
_EFFECTIVE_CONTEXT_BUDGET = CONTEXT_TOKEN_BUDGET - 64

# A single handler body longer than this is cut down before the budget is applied.
MAX_HANDLER_TOKENS = 2000

TRUNCATION_MARKER = "\n... truncated"

# Operation keys older prompts asked for, mapped to their OpenAPI 3.0 compliant form.
_LEGACY_OPERATION_FIELDS = {
    "api_description": "description",
    "authorization_tag": "x-authorization-tag",
    "module_tag": "x-module-tag",
    "auth_tag": "x-auth-tag",
    "sensitive_information": "x-sensitive-information",
}

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
    output_dir = os.path.dirname(get_output_filepath())
    os.makedirs(output_dir, exist_ok=True)
    return os.path.join(output_dir, "api_index.json")


def _load_file_metadata(directory_path: str, file_path: str):
    json_dir_path = os.path.join(directory_path, "qodex_file_information")
    json_file_name = _sanitize_json_filename(str(file_path))
    json_path = os.path.join(json_dir_path, json_file_name)
    if not os.path.exists(json_path):
        return None
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


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
    method_value = (method or "UNKNOWN").upper()
    route_value = _normalize_route(route)
    return f"{method_value} {route_value}".strip()


def _select_operation(path_item):
    """
    Pick the operation body out of one path item. A path item may legally hold
    non-operation keys such as `parameters` or vendor extensions, so only an
    HTTP verb with a dict body counts. A path item without one contributes no
    operation, which is what keeps a vendor-extension-only fragment out of the
    spec.
    """
    if not isinstance(path_item, dict):
        return None, None
    for key, payload in path_item.items():
        if key.lower() in HTTP_VERB_KEYS and isinstance(payload, dict):
            return key.lower(), payload
    return None, None


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


def _rekey_fragment(fragment, route, http_method) -> Optional[Dict]:
    """
    Validate an LLM swagger fragment and re-key it under the route the extractor
    found. The model normalizes paths its own way, so its keys are discarded and
    only the first operation body is kept. Returns None when the fragment is
    unusable.
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
        verb, payload = _select_operation(path_item)
        if payload is None:
            continue
        method_key = (http_method or verb or "get").lower()
        return {"paths": {route_key: {method_key: _normalize_operation_fields(payload)}}}
    return None


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


def _resolve_imported_definitions(import_item, directory_path: str, route):
    origin = import_item.get("origin")
    imported_name = import_item.get("imported_name")
    if not origin or not imported_name:
        return []
    metadata = _load_file_metadata(directory_path, origin)
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


def _build_api_index(directory_path: str, endpoints: list) -> dict:
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
            metadata = _load_file_metadata(directory_path, abs_file_path)
            if metadata:
                in_file, imported = get_dependencies(
                    metadata, start_line, end_line, abs_file_path
                )
                imports.extend(_normalize_in_file_dependencies(in_file, route, abs_file_path))
                for item in imported:
                    imports.extend(_resolve_imported_definitions(item, directory_path, route))
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
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(api_index, f, indent=2)
    except Exception:
        return


def _load_existing_swagger():
    swagger_path = get_output_filepath()
    if not os.path.exists(swagger_path):
        return None
    try:
        with open(swagger_path, "r", encoding="utf-8") as f:
            return _migrate_legacy_spec(json.load(f))
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
        swagger["paths"] = _canonicalize_path_keys(paths)
    return swagger


def _canonicalize_path_keys(paths: Dict) -> Dict:
    """Re-key a spec written before routes were canonicalized.

    A stored /users/:id reads as removed next to the /users/{id} the extractor
    now emits, which regenerates the whole spec on the first run after the
    upgrade. A key already in the canonical spelling wins; its legacy-spelled
    twin only fills the verbs the canonical one is missing.
    """
    canonical: Dict = {}
    legacy: List = []
    for path_key, path_item in paths.items():
        normalized = _normalize_route(path_key) or path_key
        if normalized == path_key:
            canonical[path_key] = path_item
        else:
            legacy.append((normalized, path_item))
    for normalized, path_item in legacy:
        existing = canonical.get(normalized)
        if existing is None:
            canonical[normalized] = path_item
        elif isinstance(existing, dict) and isinstance(path_item, dict):
            for operation_name, operation in path_item.items():
                existing.setdefault(operation_name, operation)
    return canonical


def _load_existing_api_index():
    api_index_path = _api_index_output_path()
    if not os.path.exists(api_index_path):
        return None
    try:
        with open(api_index_path, "r", encoding="utf-8") as f:
            return _canonicalize_index_keys(json.load(f))
    except Exception:
        return None


def _canonicalize_index_keys(api_index):
    """Same upgrade for the api_index: an index written before routes were
    canonicalized holds the rails spelling, which reads as removed while the
    freshly extracted key reads as added. The canonical key wins."""
    if not isinstance(api_index, dict):
        return api_index
    canonical: Dict = {}
    legacy: List = []
    for key, entry in api_index.items():
        method, route = _split_endpoint_key(key)
        normalized = _endpoint_key(route, method) if route else key
        if normalized == key:
            canonical[key] = entry
        else:
            legacy.append((normalized, entry))
    for normalized, entry in legacy:
        canonical.setdefault(normalized, entry)
    return canonical


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


def _handler_lines(method_info: Dict) -> List[str]:
    """The handler's own source lines, read for pricing its batch section."""
    lines = _read_file_lines(method_info.get("file_path") or "") or []
    start_line = method_info.get("start_line") or 1
    end_line = method_info.get("end_line") or start_line
    return lines[start_line - 1 : end_line]


def _batch_section_tokens(method_info: Dict) -> int:
    """What this endpoint's section costs the batch."""
    section, _ = _handler_section(
        f"{_endpoint_label(method_info)}:", "".join(_handler_lines(method_info))
    )
    return num_tokens_from_string(section)


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
    by_file: Dict[str, List[Dict]] = {}
    for method_info in endpoint_jobs:
        by_file.setdefault(method_info.get("file_path") or "", []).append(method_info)

    batches: List[List[Tuple[Dict, List[Dict]]]] = []
    for jobs in by_file.values():
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
        current: List[Tuple[Dict, List[Dict]]] = []
        used = 0
        for entry in entries:
            cost = _batch_section_tokens(entry[0])
            if current and (
                len(current) >= MAX_ENDPOINTS_PER_BATCH
                or used + cost > _EFFECTIVE_CONTEXT_BUDGET
            ):
                batches.append(current)
                current = []
                used = 0
            current.append(entry)
            used += cost
        if current:
            batches.append(current)
    return batches


def _block_text(block) -> str:
    """A code block arrives either as a list of source lines or as plain text."""
    if isinstance(block, str):
        return block
    return "".join(block)


def _truncate_to_tokens(text: str, max_tokens: int) -> Tuple[str, bool]:
    """
    Cut text down to its first max_tokens tokens. The character position is
    binary searched because only the token count is exposed, not the encoder.
    """
    if num_tokens_from_string(text) <= max_tokens:
        return text, False
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if num_tokens_from_string(text[:middle]) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low] + TRUNCATION_MARKER, True


def _handler_section(header: str, body: str) -> Tuple[str, bool]:
    """One endpoint's prompt section, with its handler capped."""
    body, was_truncated = _truncate_to_tokens(body, MAX_HANDLER_TOKENS)
    return (f"{header}\n{body}" if header else body), was_truncated


def _apply_context_budget(
    handler_sections: List[Tuple[str, str]], shared_blocks: List, file_label: str
) -> Tuple[List[str], List[str]]:
    """
    Fit the handler bodies and the shared context inside CONTEXT_TOKEN_BUDGET.
    Handler bodies are kept first, each capped at MAX_HANDLER_TOKENS; the deduped
    shared blocks then fill whatever is left and are dropped from the end. The
    batches are packed so their sections already fit, so what is left here is
    whatever room the shared blocks get.
    Returns (kept_shared_blocks, handler_sections) as plain text.
    """
    truncated = False
    sections: List[str] = []
    used = 0
    for header, body in handler_sections:
        section, was_truncated = _handler_section(header, body)
        truncated = truncated or was_truncated
        sections.append(section)
        used += num_tokens_from_string(section)

    seen = set()
    unique_blocks: List[str] = []
    for block in shared_blocks:
        text = _block_text(block)
        if not text.strip() or text in seen:
            continue
        seen.add(text)
        unique_blocks.append(text)

    kept_blocks: List[str] = []
    dropped = 0
    for index, text in enumerate(unique_blocks):
        cost = num_tokens_from_string(text) + 2
        if used + cost > _EFFECTIVE_CONTEXT_BUDGET:
            dropped = len(unique_blocks) - index
            break
        kept_blocks.append(text)
        used += cost

    if dropped or truncated:
        print(f"apimesh: context truncated for {file_label} ({dropped} blocks dropped)")
    return kept_blocks, sections


def _endpoint_label(method_info: Dict) -> str:
    return _endpoint_key(method_info.get("route"), method_info.get("http_method"))


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
        usable_entries.append(entry)
        endpoint_lines.append(label)
        handler_sections.append((f"{label}:", _block_text(method_definition)))
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


def _batch_response_is_usable(response) -> bool:
    return isinstance(response, dict) and isinstance(response.get("paths"), dict)


def _generate_endpoint_fragment(directory_path: str, method_info: Dict) -> Dict:
    """The per endpoint call, with the same dedupe and token budget as a batch."""
    context_blocks, method_definition = provide_context_codeblock(
        directory_path, method_info
    )
    http_method = method_info.get("http_method")
    if http_method:
        context_blocks = [[f"HTTP_METHOD: {http_method}\n"]] + context_blocks
    mirrored_from = method_info.get("mirrored_from")
    if mirrored_from:
        context_blocks = [[f"MIRRORED_FROM: {mirrored_from}\n"]] + context_blocks
    kept_blocks, sections = _apply_context_budget(
        [("", _block_text(method_definition))],
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

    response = None
    for _ in range(2):
        try:
            response = get_batch_definition_swagger(
                endpoints_list, shared_context, sections
            )
        except Exception:
            response = None
        if _batch_response_is_usable(response):
            break
        response = None

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


def _apply_host(swagger, host):
    """The host this run was given wins over the one the stored spec carries,
    otherwise --api-host is silently ignored on every incremental run."""
    if swagger is not None and host:
        swagger["servers"] = [{"url": host}]
    return swagger


def _maybe_incremental_update(
    directory_path: str, endpoint_jobs: list, host: Optional[str] = None
):
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
        return _apply_host(existing_swagger, host)
    changed_keys = set()
    for key in existing_keys & new_keys:
        if _endpoint_has_changed(existing_index.get(key), endpoint_map.get(key), changed_files):
            changed_keys.add(key)

    keys_to_update = added_keys | changed_keys
    updated_index = dict(existing_index)

    for key in removed_keys:
        updated_index.pop(key, None)
        _remove_endpoint_from_swagger(existing_swagger, key)

    # Every dirty endpoint goes through the batch path in one pass: generating
    # them key by key put the endpoints of one changed controller in a call each
    # and left a dirty PATCH and PUT unable to share the one generated body.
    jobs_to_update = []
    for key in keys_to_update:
        jobs_to_update.extend(endpoint_map.get(key, []))
    succeeded, failed = _update_swagger_for_endpoints(
        existing_swagger, directory_path, jobs_to_update
    )
    # A failed endpoint has to stay dirty: refreshing its index entry would
    # make the next run see no change and never retry it. Leaving the entry
    # stale (or absent for a new endpoint) is what schedules the retry, and a
    # key is only refreshed when every endpoint behind it made it.
    failed_keys = {_endpoint_label(method_info) for method_info in failed}
    # A failed key's stale entry is dropped, not kept: once the commit
    # reference advances, a kept entry would hide the failure forever, while
    # an absent key reads as newly added and is retried on the next run.
    for failed_key in failed_keys:
        updated_index.pop(failed_key, None)

    for entry_key, entry_value in _build_api_index(directory_path, succeeded).items():
        if entry_key in failed_keys:
            continue
        updated_index[entry_key] = entry_value

    info = existing_swagger.setdefault("info", {})
    info.pop("commit_reference", None)
    info["x-commit-reference"] = get_git_commit_hash()
    _write_api_index(updated_index)
    return _apply_host(existing_swagger, host)


def _sanitize_json_filename(file_path: str) -> str:
    """
    Convert a filesystem path into a deterministic filename that can be used
    to persist metadata in the staging directory.
    """
    normalized = file_path.replace(os.sep, "_q_")
    return f"{normalized}.json"


def run_swagger_generation(host: str) -> Optional[Dict]:
    directory_path = get_repo_path()
    repo_name = get_repo_name()
    new_dir_name = "qodex_file_information"
    new_dir_path = os.path.join(directory_path, new_dir_name)
    os.makedirs(new_dir_path, exist_ok=True)

    try:
        for root, _, files in os.walk(directory_path):
            for filename in files:
                file_path = os.path.join(root, filename)
                if (
                    os.path.exists(file_path)
                    and should_process_directory(str(file_path), directory_path)
                    and file_path.endswith(".rb")
                ):
                    try:
                        file_info = process_file(file_path, directory_path)
                    except Exception:
                        # Skip files that fail to parse; we still want best-effort coverage.
                        continue

                    json_file_name = _sanitize_json_filename(str(file_path))
                    json_file_path = os.path.join(new_dir_path, json_file_name)
                    with open(json_file_path, "w", encoding="utf-8") as f:
                        json.dump(file_info, f, indent=4)

        api_definition_files = find_api_definition_files(directory_path)
        all_endpoints_dict: Dict[str, List[Dict]] = {}
        route_map: Dict[str, List[Dict]] = {}
        controller_files: List[Path] = []

        for file in api_definition_files:
            ruby_file = Path(file)
            if ruby_file.as_posix().endswith("config/routes.rb"):
                find_api_endpoints(ruby_file, directory_path, route_map)
            else:
                controller_files.append(ruby_file)

        for controller_file in controller_files:
            endpoints = find_api_endpoints(controller_file, directory_path, route_map)
            if endpoints:
                all_endpoints_dict[str(controller_file)] = endpoints

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
        _write_api_index(_build_api_index(directory_path, generated))

        return swagger
    finally:
        if os.path.exists(new_dir_path):
            shutil.rmtree(new_dir_path, ignore_errors=True)


def _merge_paths(target: Dict, source: Dict) -> None:
    """
    Merge the path map from the LLM response into the aggregated swagger document.
    """
    paths = source.get("paths", {})
    for path_key, methods in paths.items():
        target.setdefault("paths", {})
        target["paths"].setdefault(path_key, {})
        for method, payload in methods.items():
            target["paths"][path_key][method] = payload


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

    json_dir_path = os.path.join(directory_path, "qodex_file_information")
    if not os.path.exists(json_dir_path):
        return _CLASS_INDEX_CACHE

    for entry in os.scandir(json_dir_path):
        if not entry.is_file() or not entry.name.endswith(".json"):
            continue
        try:
            with open(entry.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        source_file = data.get("filename")
        if not source_file:
            continue

        elements = data.get("elements", {})
        classes = elements.get("classes", [])
        functions = elements.get("functions", [])

        for klass in classes:
            name = klass.get("name")
            if not name or name in _CLASS_INDEX_CACHE:
                continue
            class_start = klass.get("start_line")
            class_end = klass.get("end_line")
            method_map: Dict[str, Dict[str, int]] = {}
            if isinstance(class_start, int) and isinstance(class_end, int):
                for func in functions:
                    method_name = func.get("name")
                    start_line = func.get("start_line")
                    end_line = func.get("end_line")
                    if (
                        method_name
                        and isinstance(start_line, int)
                        and isinstance(end_line, int)
                        and class_start <= start_line <= class_end
                    ):
                        method_map[method_name] = {
                            "start_line": start_line,
                            "end_line": end_line,
                        }
            _CLASS_INDEX_CACHE[name] = {
                "file_path": source_file,
                "superclass": klass.get("superclass"),
                "start_line": klass.get("start_line"),
                "end_line": klass.get("end_line"),
                "methods": method_map,
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
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    _FILE_CONTENT_CACHE[file_path] = lines
    return lines


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


def get_dependencies(
    data: Dict, start_line: int, end_line: int, file_path: str
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
        with open(file_name, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []

    for block in in_file_dependency_functions:
        if lines:
            start = max(block.get("function_start_line", 1) - 1, 0)
            end = block.get("function_end_line", start + 1)
            code_blocks.append(lines[start:end])

    for func in imported_functions:
        json_dir_path = os.path.join(directory_path, "qodex_file_information")
        origin = func.get("origin")
        if not origin:
            continue
        json_file = _sanitize_json_filename(str(origin))
        complete_json_file_path = os.path.join(json_dir_path, json_file)
        if not os.path.exists(complete_json_file_path):
            continue

        try:
            with open(complete_json_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        origin_file_name = origin
        try:
            with open(origin_file_name, "r", encoding="utf-8") as f:
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
        with open(file_name, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []

    method_definition_code_block = lines[
        method_info["start_line"] - 1 : method_info["end_line"]
    ]

    json_dir_path = os.path.join(directory_path, "qodex_file_information")
    json_file = _sanitize_json_filename(str(file_name))
    complete_json_file_path = os.path.join(json_dir_path, json_file)
    try:
        with open(complete_json_file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {"elements": {"functions": [], "function_calls": []}, "imports": []}

    in_file_dependency_functions, imported_functions = get_dependencies(
        data,
        method_info["start_line"],
        method_info["end_line"],
        method_info["file_path"],
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

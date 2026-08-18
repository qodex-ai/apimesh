import os, json, re
import hashlib
import shutil
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
from config import Configurations
from nodejs_pipeline.definition_swagger_generator import (
    get_batch_definition_swagger,
    get_function_definition_swagger,
)
from nodejs_pipeline.constants import (
    SUPPORTED_NODE_FILE_EXTENSIONS,
    METADATA_DIR_NAME,
)
from utils import (
    get_git_commit_hash,
    get_github_repo_url,
    get_repo_path,
    get_repo_name,
    get_output_filepath,
    get_changed_files_since,
    num_tokens_from_string,
)

config = Configurations()

# Keys of an OpenAPI path item that hold an operation body.
HTTP_VERB_KEYS = {"get", "post", "put", "delete", "patch", "options", "head", "trace"}

# An express route parameter name: ':name'. The inline regex constraint and the
# optional marker that may follow it are not part of the name, and the
# constraint can nest parentheses, so it is consumed separately.
EXPRESS_PARAM_PATTERN = re.compile(r":([A-Za-z_$][A-Za-z0-9_$]*)")

# One LLM call documents at most this many endpoints of the same source file.
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


def _metadata_dir_path(directory_path: str) -> str:
    return os.path.join(directory_path, METADATA_DIR_NAME)


def _metadata_file_name(file_path: str) -> str:
    sanitized = str(file_path).replace("/", "_q_").replace("\\", "_q_")
    name, _ = os.path.splitext(sanitized)
    return f"{name}.json"


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
    output_dir = os.path.dirname(get_output_filepath())
    os.makedirs(output_dir, exist_ok=True)
    return os.path.join(output_dir, "api_index.json")


def _load_file_metadata(directory_path: str, file_path: str):
    json_dir = _metadata_dir_path(directory_path)
    json_file = _metadata_file_name(file_path)
    json_path = os.path.join(json_dir, json_file)
    if not os.path.exists(json_path):
        return None
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _endpoint_key(route, method):
    method_value = (method or "UNKNOWN").upper()
    # Keys must use the same normalized route the swagger paths are keyed by,
    # otherwise removals of deleted endpoints never match.
    route_value = _normalize_route(route) or ""
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


def _resolve_imported_definitions(import_item, directory_path: str, route):
    origin = import_item.get("origin")
    imported_name = import_item.get("imported_name")
    if not origin or not imported_name or origin == "<node_builtin_or_external>":
        return []
    metadata = _load_file_metadata(directory_path, origin)
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
        method = endpoint.get("method") or endpoint.get("http_method")
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
        index_entry = api_index.setdefault(key, {"files": []})
        # The hash of the prompt this endpoint was generated from, so the next
        # run can tell an untouched endpoint from one whose context moved.
        context_hash = endpoint.get("context_hash")
        if context_hash:
            index_entry["context_hash"] = context_hash
        _merge_file_entry(index_entry["files"], entry)
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


def _canonicalize_path_keys(paths: dict) -> dict:
    """Re-key a spec written before routes were canonicalized.

    A stored /users/:id reads as removed next to the /users/{id} the extractor
    now emits, which regenerates the whole spec on the first run after the
    upgrade. A key already in the canonical spelling wins; its legacy-spelled
    twin only fills the verbs the canonical one is missing.
    """
    canonical = {}
    legacy = []
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
    canonicalized holds the express spelling, which reads as removed while the
    freshly extracted key reads as added. The canonical key wins."""
    if not isinstance(api_index, dict):
        return api_index
    canonical = {}
    legacy = []
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
        key = _endpoint_key(endpoint.get("route"), endpoint.get("method") or endpoint.get("http_method"))
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
    section, _ = _handler_section(
        f"{_endpoint_label(method_info)}:", "".join(_handler_lines(method_info))
    )
    return num_tokens_from_string(section)


def _batch_endpoint_jobs(endpoint_jobs: list) -> list:
    """
    One batch per source file, packed so the handler sections of a batch stay
    inside CONTEXT_TOKEN_BUDGET: capping each handler on its own still let ten
    of them add up to far more than the prompt can carry. A batch is closed as
    soon as the next section would push it past the budget, with
    MAX_ENDPOINTS_PER_BATCH as the secondary limit.
    """
    by_file = {}
    for method_info in endpoint_jobs:
        by_file.setdefault(method_info.get("file_path") or "", []).append(method_info)
    batches = []
    for jobs in by_file.values():
        current = []
        used = 0
        for method_info in jobs:
            cost = _batch_section_tokens(method_info)
            if current and (
                len(current) >= MAX_ENDPOINTS_PER_BATCH
                or used + cost > _EFFECTIVE_CONTEXT_BUDGET
            ):
                batches.append(current)
                current = []
                used = 0
            current.append(method_info)
            used += cost
        if current:
            batches.append(current)
    return batches


def _block_text(block) -> str:
    """A code block arrives either as a list of source lines or as plain text."""
    if isinstance(block, str):
        return block
    return "".join(block)


def _truncate_to_tokens(text: str, max_tokens: int):
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


def _handler_section(header: str, body: str):
    """One endpoint's prompt section, with its handler capped."""
    body, was_truncated = _truncate_to_tokens(body, MAX_HANDLER_TOKENS)
    return (f"{header}\n{body}" if header else body), was_truncated


def _apply_context_budget(handler_sections: list, shared_blocks: list, file_label: str):
    """
    Fit the handler bodies and the shared context inside CONTEXT_TOKEN_BUDGET.
    Handler bodies are kept first, each capped at MAX_HANDLER_TOKENS; the deduped
    shared blocks then fill whatever is left and are dropped from the end. The
    batches are packed so their sections already fit, so what is left here is
    whatever room the shared blocks get.
    Returns (kept_shared_blocks, handler_sections) as plain text.
    """
    truncated = False
    sections = []
    used = 0
    for header, body in handler_sections:
        section, was_truncated = _handler_section(header, body)
        truncated = truncated or was_truncated
        sections.append(section)
        used += num_tokens_from_string(section)

    seen = set()
    unique_blocks = []
    for block in shared_blocks:
        text = _block_text(block)
        if not text.strip() or text in seen:
            continue
        seen.add(text)
        unique_blocks.append(text)

    kept_blocks = []
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


def _context_hash(context_code_blocks, method_definition_code_block) -> str:
    """
    Fingerprint of the prompt text one endpoint carries: its capped handler body
    followed by its deduped context blocks, in the order the prompt sends them.
    The endpoint label and the rest of the batch are deliberately left out, so
    the value can be recomputed for a single endpoint before the batches are
    packed, which is what the incremental skip does.
    """
    body, _ = _truncate_to_tokens(_block_text(method_definition_code_block), MAX_HANDLER_TOKENS)
    seen = set()
    blocks = []
    for block in context_code_blocks:
        text = _block_text(block)
        if not text.strip() or text in seen:
            continue
        seen.add(text)
        blocks.append(text)
    return hashlib.sha256("\n\n".join([body] + blocks).encode("utf-8")).hexdigest()


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
        handler_sections.append((f"{label}:", _block_text(method_definition_code_block)))
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


def _batch_response_is_usable(response) -> bool:
    return isinstance(response, dict) and isinstance(response.get("paths"), dict)


def _generate_endpoint_fragment(directory_path: str, method_info: dict):
    """The per endpoint call, with the same dedupe and token budget as a batch."""
    context_code_blocks, method_definition_code_block = provide_context_codeblock(
        directory_path, method_info
    )
    method_info["context_hash"] = _context_hash(
        context_code_blocks, method_definition_code_block
    )
    kept_blocks, sections = _apply_context_budget(
        [("", _block_text(method_definition_code_block))],
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

    response = None
    for _ in range(2):
        try:
            response = get_batch_definition_swagger(endpoints_list, shared_context, sections)
        except Exception:
            response = None
        if _batch_response_is_usable(response):
            break
        response = None

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


def _apply_host(swagger, host):
    """The host this run was given wins over the one the stored spec carries,
    otherwise --api-host is silently ignored on every incremental run."""
    if swagger is not None and host:
        swagger["servers"] = [{"url": host}]
    return swagger


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


def _rebuild_unchanged_index_entries(
    directory_path: str, keys, endpoint_map, existing_index, updated_index
) -> None:
    """Rebuild the index entries of the endpoints that skipped on their hash.

    Their spec operation is right, but the dependency edges the old entry stores
    can be stale: a helper that moved into a file with identical text leaves the
    prompt text, and so the hash, untouched while the import moved with it.
    Keeping the old entry would point the next run's dependency hop at the file
    the helper left, and an edit to the file it moved to would never mark the
    endpoint dirty again. The entry is taken from the current extraction, and
    carries over the hash that matched, since nothing was regenerated.
    """
    for key in keys:
        rebuilt = _build_api_index(directory_path, endpoint_map.get(key, [])).get(key)
        if not rebuilt:
            continue
        existing_entry = existing_index.get(key)
        stored = existing_entry.get("context_hash") if isinstance(existing_entry, dict) else None
        if stored:
            rebuilt["context_hash"] = stored
        updated_index[key] = rebuilt


def _maybe_incremental_update(directory_path: str, endpoint_jobs: list, host=None):
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
    # Past the halfway mark the incremental pass is doing the whole run's work
    # while carrying a spec and an index it has to patch around, so the caller
    # regenerates from scratch instead.
    if new_keys and len(keys_to_update) * 2 > len(new_keys):
        print(
            f"apimesh: {len(keys_to_update)} of {len(new_keys)} endpoints affected, "
            "running a full regeneration"
        )
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
    _rebuild_unchanged_index_entries(
        directory_path, unchanged_keys, endpoint_map, existing_index, updated_index
    )
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

    _post_process_swagger(existing_swagger)
    info = existing_swagger.setdefault("info", {})
    info.pop("commit_reference", None)
    info["x-commit-reference"] = get_git_commit_hash()
    _write_api_index(updated_index)
    return _apply_host(existing_swagger, host)

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
    new_dir_path = _metadata_dir_path(directory_path)
    os.makedirs(new_dir_path, exist_ok=True)
    try:
        for root, dirs, files in os.walk(directory_path):
            for file in files:
                file_path = os.path.join(root, file)
                suffix = Path(file_path).suffix.lower()
                if (
                    os.path.exists(file_path)
                    and should_process_directory(str(file_path), directory_path)
                    and suffix in SUPPORTED_NODE_FILE_EXTENSIONS
                ):
                    try:
                        file_info = process_file(file_path, directory_path)
                    except Exception as ex:
                        continue
                    json_file_name = os.path.join(new_dir_path, _metadata_file_name(file_path))
                    with open(json_file_name, "w") as f:
                        json.dump(file_info, f, indent=4)
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
        if not endpoint_jobs:
            print("apimesh: nodejs parser found 0 endpoints, falling back to generic extraction")
            return None
        incremental_swagger = _maybe_incremental_update(directory_path, endpoint_jobs, host)
        if incremental_swagger is not None:
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
        _write_api_index(_build_api_index(directory_path, generated))
        return swagger
    finally:
        if os.path.exists(new_dir_path):
            shutil.rmtree(new_dir_path, ignore_errors=True)


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
        json_dir_path = _metadata_dir_path(directory_path)
        complete_json_file_path = os.path.join(json_dir_path, _metadata_file_name(origin_file_name))
        if not os.path.exists(complete_json_file_path):
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
    json_dir_path = _metadata_dir_path(directory_path)
    json_file = _metadata_file_name(file_name)
    complete_json_file_path = os.path.join(json_dir_path, json_file)

    if not os.path.exists(complete_json_file_path):
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


def _first_operation(source):
    """Return the first operation body in an LLM fragment, or None if it is unusable."""
    if not isinstance(source, dict):
        return None
    paths = source.get("paths")
    if not isinstance(paths, dict) or not paths:
        return None
    for path_item in paths.values():
        _, payload = _select_operation(path_item)
        if payload is not None:
            return payload
    return None


def _normalize_operation_fields(operation):
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


def _merge_paths(target, source, method_info):
    """
    Merge one generated fragment. The model routinely rewrites the path and the
    method, so the operation is re-keyed under the route we extracted; that is
    also the key the api index and incremental removals use.
    """
    operation = _first_operation(source)
    if operation is None:
        return False
    route = _normalize_route(method_info.get("route"))
    method = (method_info.get("method") or method_info.get("http_method") or "").lower()
    if not route or not method:
        return False
    target.setdefault("paths", {}).setdefault(route, {})[method] = _normalize_operation_fields(operation)
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

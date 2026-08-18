import os, json
import hashlib
import re
import shutil
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
from config import Configurations
from python_pipeline.definition_swagger_generator import (
    CONTEXT_TOKEN_BUDGET,
    build_endpoint_section,
    get_batch_definition_swagger,
    get_function_definition_swagger,
    section_token_cost,
)

# Headroom for the separators joined between sections and blocks, so the
# budget holds for the final assembled prompt, not just the parts.
_EFFECTIVE_CONTEXT_BUDGET = CONTEXT_TOKEN_BUDGET - 64
from utils import (
    get_git_commit_hash,
    get_github_repo_url,
    get_repo_path,
    get_repo_name,
    get_output_filepath,
    get_changed_files_since,
)

config = Configurations()

OPENAI_RETRY_DELAYS = (1, 4)
# One LLM call documents a whole file. Past ten endpoints the shared context
# budget leaves too little room per endpoint, so a file is chunked.
MAX_BATCH_ENDPOINTS = 10

# Flask writes params as <int:pk> or <name>, OpenAPI wants {pk} / {name}.
_ROUTE_PARAM_PATTERN = re.compile(r"<(?:[^<>:]+:)?([^<>]+)>")
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


def _first_operation(path_item):
    """
    The (verb, operation) pair of a path item. A path item legally carries
    members that are not operations ("parameters", vendor extensions), so only
    an HTTP verb with a dict body counts. A path item without one contributes no
    operation, which is what keeps a vendor-extension-only fragment out of the
    spec.
    """
    if not isinstance(path_item, dict):
        return None
    for name, value in path_item.items():
        if str(name).lower() in _HTTP_OPERATIONS and isinstance(value, dict):
            return name, value
    return None


def _api_index_output_path() -> str:
    output_dir = os.path.dirname(get_output_filepath())
    os.makedirs(output_dir, exist_ok=True)
    return os.path.join(output_dir, "api_index.json")


# Per file metadata is cached under the output directory, never inside the
# scanned repo: a run must not create or delete anything in the user's tree.
METADATA_CACHE_PIPELINE = "python"

# Bumped when the shape of a metadata entry changes. A missing or stale marker
# wipes this pipeline's cache before anything reads it.
METADATA_CACHE_VERSION = "1"
_METADATA_CACHE_VERSION_FILE = "cache_version"

# File path -> content hash, and file path -> the cache entry holding its
# metadata. Both are per run: two runs in one process are two checkouts.
_CONTENT_HASHES = {}
_METADATA_ENTRIES = {}


def _metadata_cache_dir() -> str:
    return os.path.join(
        os.path.dirname(get_output_filepath()), "metadata_cache", METADATA_CACHE_PIPELINE
    )


def _content_hash(file_path: str):
    """First 16 hex chars of the file's sha256, read once per run.

    The path is hashed along with the content because an entry records the file
    it came from and the import origins resolved around it, so two files that
    read identically in different directories are not interchangeable.
    """
    key = os.path.abspath(file_path)
    if key not in _CONTENT_HASHES:
        try:
            with open(key, "rb") as f:
                content = f.read()
        except OSError:
            _CONTENT_HASHES[key] = None
        else:
            digest = hashlib.sha256(key.encode("utf-8") + b"\0" + content)
            _CONTENT_HASHES[key] = digest.hexdigest()[:16]
    return _CONTENT_HASHES[key]


def _metadata_cache_filename(file_path: str, content_hash: str) -> str:
    """One cache entry name: the file's basename plus the hash of its content.

    A single path component can fill NAME_MAX on its own, so the readable half
    is capped and the hash is what keeps the name unique.
    """
    stem = os.path.splitext(os.path.basename(file_path))[0]
    stem = stem.encode("utf-8")[:200].decode("utf-8", "ignore")
    return f"{stem}_{content_hash}.json"


def _metadata_cache_path(file_path: str):
    """Where this file's metadata sits for the content it holds right now."""
    content_hash = _content_hash(file_path)
    if not content_hash:
        return None
    return os.path.join(
        _metadata_cache_dir(), _metadata_cache_filename(file_path, content_hash)
    )


def _prepare_metadata_cache() -> str:
    """The cache directory, emptied first when another schema wrote it."""
    cache_dir = _metadata_cache_dir()
    marker_path = os.path.join(cache_dir, _METADATA_CACHE_VERSION_FILE)
    try:
        with open(marker_path, "r", encoding="utf-8") as f:
            stored = f.read().strip()
    except OSError:
        stored = None
    if stored != METADATA_CACHE_VERSION:
        shutil.rmtree(cache_dir, ignore_errors=True)
    os.makedirs(cache_dir, exist_ok=True)
    with open(marker_path, "w", encoding="utf-8") as f:
        f.write(METADATA_CACHE_VERSION)
    return cache_dir


def _cache_file_metadata(file_path: str, directory_path: str) -> None:
    """Extract one file's metadata, unless the cache already holds its content."""
    cache_path = _metadata_cache_path(file_path)
    if not cache_path:
        return
    key = os.path.abspath(file_path)
    if os.path.exists(cache_path):
        # Byte for byte what a previous run parsed, so its metadata still holds.
        _METADATA_ENTRIES[key] = cache_path
        return
    # One unreadable file must not cost the run every other file's metadata.
    try:
        file_info = process_file(file_path, directory_path)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(file_info, f, indent=4)
    except Exception as exc:
        print(f"apimesh: skipping metadata for {file_path}: {exc}")
        return
    _METADATA_ENTRIES[key] = cache_path


def _build_metadata_cache(directory_path: str) -> None:
    """One cache entry per python file below the repo root."""
    _prepare_metadata_cache()
    for root, dirs, files in os.walk(directory_path):
        for file in files:
            file_path = os.path.join(root, file)
            if (
                os.path.exists(file_path)
                and should_process_directory(str(file_path), directory_path)
                and file_path.endswith(".py")
            ):
                _cache_file_metadata(file_path, directory_path)


def _prune_metadata_cache() -> None:
    """Drop entries for content the repo no longer holds, so the cache stays bounded."""
    live = set(_METADATA_ENTRIES.values())
    try:
        entries = list(os.scandir(_metadata_cache_dir()))
    except OSError:
        return
    for entry in entries:
        if not entry.is_file() or not entry.name.endswith(".json"):
            continue
        if entry.path in live:
            continue
        try:
            os.remove(entry.path)
        except OSError:
            pass


def _reset_metadata_state() -> None:
    """Two runs in one process see two checkouts of the same paths."""
    _CONTENT_HASHES.clear()
    _METADATA_ENTRIES.clear()


def _load_file_metadata(file_path: str):
    json_file_path = _metadata_cache_path(file_path)
    if not json_file_path or not os.path.exists(json_file_path):
        return None
    try:
        with open(json_file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _endpoint_key(route, method):
    method_value = (method or "UNKNOWN").upper()
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
    """The api_index entries for the endpoints this run generated.

    One entry per endpoint key:
    ``{"files": [{"file_path", "imports": [...]}], "context_hash": "<sha256>"}``.
    The hash covers the exact prompt text the endpoint was generated from, so
    the next run can tell an endpoint whose source context is byte for byte
    unchanged from one that has to be documented again. It is absent for an
    endpoint indexed before hashing existed, and such an endpoint always
    regenerates.
    """
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
        context_hash = endpoint.get("context_hash")
        if context_hash:
            api_index[key]["context_hash"] = context_hash
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


def _canonicalize_path_keys(paths: dict) -> dict:
    """Re-key a spec written before routes were canonicalized.

    A stored /users/<int:pk> reads as removed next to the /users/{pk} the
    extractor now emits, which regenerates the whole spec on the first run
    after the upgrade. A key already in the canonical spelling wins; its
    legacy-spelled twin only fills the verbs the canonical one is missing.
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
    canonicalized holds the framework's spelling, which reads as removed while
    the freshly extracted key reads as added. The canonical key wins."""
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
    """Whether an edit reached this endpoint, one dependency hop included.

    A handler is documented from the helpers and classes it pulls in, so
    editing one of those files makes the endpoint stale even though its own
    file was never touched. The hop stops there: the context hash decides
    whether the edit actually changed anything the model would see.
    """
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
    # An index written before routes were canonicalized still holds <id> keys.
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


def _merge_paths(target: dict, source: dict) -> None:
    for path_key, methods in source.get("paths", {}).items():
        target.setdefault("paths", {})
        target["paths"].setdefault(path_key, {})
        for method, payload in methods.items():
            target["paths"][path_key][method] = payload


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


def _rekey_fragment(fragment, route, method):
    """Re-key a model fragment under the route and method the extractor found.

    The model routinely rewrites the path it is given, which leaves the spec
    keyed on something the api_index can never match. Only the first operation
    body is kept, since one fragment describes one endpoint.
    """
    normalized_route = _normalize_route(route)
    if not normalized_route or not isinstance(fragment, dict):
        return None
    paths = fragment.get("paths")
    if not isinstance(paths, dict) or not paths:
        return None
    for path_item in paths.values():
        operation = _first_operation(path_item)
        if operation is None:
            continue
        model_method, payload = operation
        # The extractor cannot read the method off a bare Flask @app.route, so
        # the model's method is the only one available there.
        method_key = method.lower() if method else str(model_method).lower()
        return {"paths": {normalized_route: {method_key: _normalize_operation_fields(payload)}}}
    return None


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
        _batch_label(method_info), method_definition_code_block, context_code_blocks
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
    by_file = {}
    for job in endpoint_jobs:
        by_file.setdefault(job.get("file_path") or "", []).append(job)
    batches = []
    for jobs in by_file.values():
        current = []
        used = 0
        for job in jobs:
            cost = _batch_section_tokens(job)
            if current and (
                len(current) >= MAX_BATCH_ENDPOINTS or used + cost > _EFFECTIVE_CONTEXT_BUDGET
            ):
                batches.append(current)
                current = []
                used = 0
            current.append(job)
            used += cost
        if current:
            batches.append(current)
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


def _context_hash(label, body, context_blocks) -> str:
    """sha256 over the exact text this endpoint puts in front of the model.

    A batch's shared context is the union of its endpoints' blocks, and which
    endpoints share a batch depends on which ones happen to be dirty, so
    hashing that union would never match twice. What is hashed instead is the
    endpoint's own section plus the blocks it contributes, joined the way the
    prompt joins them.
    """
    section, _ = build_endpoint_section(label, body)
    parts = [section]
    for block in context_blocks or []:
        text = block if isinstance(block, str) else "".join(str(line) for line in block or [])
        if text.strip():
            parts.append(text)
    return hashlib.sha256("\n\n".join(parts).encode("utf-8")).hexdigest()


def _endpoint_context_hash(directory_path: str, method_info):
    """This endpoint's context hash, or None when its source cannot be read."""
    try:
        label, body, blocks = _batch_entry(directory_path, method_info)
    except Exception:
        return None
    return _context_hash(label, body, blocks)


def _stored_context_hash(entry):
    if not isinstance(entry, dict):
        return None
    stored = entry.get("context_hash")
    return stored if isinstance(stored, str) and stored else None


def _generate_batch_payload(directory_path: str, batch: list):
    """One LLM call for a file's endpoints. Returns the model's raw payload."""
    entries = []
    context_blocks = []
    for method_info in batch:
        label, body, blocks = _batch_entry(directory_path, method_info)
        method_info["context_hash"] = _context_hash(label, body, blocks)
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
            if not isinstance(value, dict) or str(name).lower() not in _HTTP_OPERATIONS:
                continue
            # Without a method on the decorator the model's own verb is the only
            # one available, which is what the per-endpoint path does too.
            if method and str(name).lower() != method.lower():
                continue
            method_key = method.lower() if method else str(name).lower()
            return {"paths": {normalized_route: {method_key: _normalize_operation_fields(value)}}}
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


def _apply_host(swagger, host):
    """The host this run was given wins over the one the stored spec carries,
    otherwise --api-host is silently ignored on every incremental run."""
    if swagger is not None and host:
        swagger["servers"] = [{"url": host}]
    return swagger


def _record_coverage(swagger, extracted, generated, skipped, failed, dropped=None):
    """An honest completeness block, so a consuming agent can tell a complete
    spec from a lower bound without re-running anything."""
    coverage = {
        "endpoints_extracted": extracted,
        "generated": generated,
        "skipped_unchanged": skipped,
        "failed": failed,
    }
    if dropped is not None:
        coverage["dropped_routes"] = dropped
    swagger.setdefault("info", {})["x-apimesh-coverage"] = coverage
    print(
        f"apimesh coverage: {generated} generated, {skipped} unchanged, "
        f"{failed} failed of {extracted} extracted"
    )
    return swagger


def _unchanged_context_keys(directory_path: str, keys, endpoint_map, existing_index) -> set:
    """The dirty keys whose prompt text is what they were generated from.

    Their stored spec operation and index entry are already correct, so they
    cost no LLM call. This is what makes the dependency hop cheap: an edit in
    an imported file that never reaches the endpoint's own context lands here.
    """
    unchanged = set()
    for key in keys:
        stored = _stored_context_hash(existing_index.get(key))
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
        rebuilt = _build_api_index(endpoint_map.get(key, [])).get(key)
        if not rebuilt:
            continue
        stored = _stored_context_hash(existing_index.get(key))
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
        _record_coverage(existing_swagger, len(endpoint_jobs), 0, len(endpoint_jobs), 0)
        return _apply_host(existing_swagger, host)
    changed_keys = set()
    for key in existing_keys & new_keys:
        if _endpoint_has_changed(existing_index.get(key), endpoint_map.get(key), changed_files):
            changed_keys.add(key)

    keys_to_update = added_keys | changed_keys
    # Past half the endpoints a surgical pass buys nothing: it costs the same
    # LLM calls one file at a time, while the full run writes a fresh index
    # with a hash for every endpoint.
    if keys_to_update and len(keys_to_update) * 2 > len(new_keys):
        print(
            f"apimesh: {len(keys_to_update)} of {len(new_keys)} endpoints affected, "
            "running a full regeneration"
        )
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

    _rebuild_unchanged_index_entries(
        directory_path, unchanged_keys, endpoint_map, existing_index, updated_index
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
    # A failed key's stale entry is dropped, not kept: once the commit
    # reference advances, a kept entry would hide the failure forever, while
    # an absent key reads as newly added and is retried on the next run.
    for failed_key in failed_keys:
        updated_index.pop(failed_key, None)

    for entry_key, entry_value in _build_api_index(generated).items():
        if entry_key in failed_keys:
            continue
        updated_index[entry_key] = entry_value

    # The incremental pass never raises: the existing spec is still valid, and
    # persisting the index below (failed keys dropped) is what schedules the
    # retry. Raising here would discard both and launch the slow fallback.
    total = len(generated) + len(failed)
    if total:
        print(f"generated {len(generated)} of {total} endpoints ({len(failed)} failed)")
    if failed and not generated:
        print("apimesh: every changed endpoint failed; keeping the previous spec, they will retry next run")

    info = existing_swagger.setdefault("info", {})
    info.pop("commit_reference", None)
    info["x-commit-reference"] = get_git_commit_hash()
    _write_api_index(updated_index)
    _record_coverage(
        existing_swagger,
        len(endpoint_jobs),
        len(generated),
        max(len(endpoint_jobs) - len(generated) - len(failed), 0),
        len(failed),
    )
    return _apply_host(existing_swagger, host)

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
        _record_coverage(swagger, len(endpoint_jobs), len(generated), 0, len(failed))
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

"""The machinery every language pipeline shares.

The four pipelines document endpoints the same way and used to carry four
copies of this layer, which is how they kept drifting apart. What lives here is
the part that is genuinely identical. Everything a language decides for itself
(route syntax, how source context is read, which prompt is sent) stays in its
own pipeline and is handed in as an argument, so the per language route
normalizers never leave their pipeline.
"""

import hashlib
import json
import os
import shutil

from utils import num_tokens_from_string


# Keys of an OpenAPI path item that hold an operation body.
HTTP_VERB_KEYS = {"get", "post", "put", "delete", "patch", "options", "head", "trace"}

# Backoff before each retry of a failed OpenAI call.
API_RETRY_DELAYS = (1, 4)

# Combined token cap for the shared context plus every handler body in one prompt.
CONTEXT_TOKEN_BUDGET = 6000

# Headroom for the separators joined between sections and blocks, so the
# budget holds for the final assembled prompt, not just the parts.
SEPARATOR_TOKEN_RESERVE = 64
EFFECTIVE_CONTEXT_BUDGET = CONTEXT_TOKEN_BUDGET - SEPARATOR_TOKEN_RESERVE

# A single handler body longer than this is cut down before the budget is applied.
MAX_HANDLER_TOKENS = 2000

TRUNCATION_MARKER = "\n... truncated"

# One LLM call documents a whole file. Past ten endpoints the shared context
# budget leaves too little room per endpoint, so a file is chunked.
MAX_ENDPOINTS_PER_BATCH = 10

# Operation keys older prompts asked for, mapped to their OpenAPI 3.0 compliant form.
LEGACY_OPERATION_FIELDS = {
    "api_description": "description",
    "authorization_tag": "x-authorization-tag",
    "module_tag": "x-module-tag",
    "auth_tag": "x-auth-tag",
    "sensitive_information": "x-sensitive-information",
}

# Info keys a pre-x-extension spec still carries, mapped to their new spelling.
LEGACY_INFO_FIELDS = {
    "generated_at": "x-generated-at",
    "commit_reference": "x-commit-reference",
    "github_repo_url": "x-github-repo-url",
}

# The marker a pipeline stamps its own cache version into. A missing or stale
# marker wipes that pipeline's cache before anything reads it. The version
# itself stays per pipeline: the metadata shapes are not the same.
METADATA_CACHE_VERSION_FILE = "cache_version"


# --------------------------------------------------------------------------- #
# Operation handling
# --------------------------------------------------------------------------- #


def normalize_operation_fields(operation):
    """
    Rename the legacy operation keys an older model reply may still carry to
    their OpenAPI compliant form. A value already under the new name wins, so a
    reply holding both does not end up with duplicated content.
    """
    if not isinstance(operation, dict):
        return operation
    for legacy_key, new_key in LEGACY_OPERATION_FIELDS.items():
        if legacy_key not in operation:
            continue
        operation.setdefault(new_key, operation.pop(legacy_key))
    return operation


def select_operation(path_item):
    """
    The (verb, operation) pair of one path item, verb lowercased. A path item
    may legally hold non-operation keys such as `parameters` or vendor
    extensions, so only an HTTP verb with a dict body counts. A path item
    without one contributes no operation, which is what keeps a
    vendor-extension-only fragment out of the spec.
    """
    if not isinstance(path_item, dict):
        return None, None
    for key, payload in path_item.items():
        if str(key).lower() in HTTP_VERB_KEYS and isinstance(payload, dict):
            return str(key).lower(), payload
    return None, None


def first_operation(fragment):
    """The first (verb, operation) an LLM fragment carries, or (None, None)."""
    if not isinstance(fragment, dict):
        return None, None
    paths = fragment.get("paths")
    if not isinstance(paths, dict) or not paths:
        return None, None
    for path_item in paths.values():
        verb, payload = select_operation(path_item)
        if payload is not None:
            return verb, payload
    return None, None


def merge_paths(target, source):
    """Merge the path map of one generated fragment into the aggregated spec."""
    for path_key, methods in source.get("paths", {}).items():
        target.setdefault("paths", {})
        target["paths"].setdefault(path_key, {})
        for method, payload in methods.items():
            target["paths"][path_key][method] = payload


# --------------------------------------------------------------------------- #
# Endpoint keys
# --------------------------------------------------------------------------- #


def endpoint_key(route, method, normalize_route):
    """The ``METHOD /path`` key the spec, the api_index and removals all use."""
    method_value = (method or "UNKNOWN").upper()
    route_value = normalize_route(route) or ""
    return f"{method_value} {route_value}".strip()


def split_endpoint_key(key):
    if not key:
        return "UNKNOWN", ""
    parts = key.split(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


# --------------------------------------------------------------------------- #
# Spec migration and coverage
# --------------------------------------------------------------------------- #


def migrate_legacy_spec(swagger, normalize_route):
    """Upgrade a pre-x-extension spec in place so incremental runs never write
    the legacy spellings back out. New keys win when both exist."""
    if not isinstance(swagger, dict):
        return swagger
    info = swagger.get("info")
    if isinstance(info, dict):
        for old_key, new_key in LEGACY_INFO_FIELDS.items():
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
                    normalize_operation_fields(operation)
        swagger["paths"] = canonicalize_path_keys(paths, normalize_route)
    return swagger


def canonicalize_path_keys(paths, normalize_route):
    """Re-key a spec written before routes were canonicalized.

    A stored /users/:id reads as removed next to the /users/{id} the extractor
    now emits, which regenerates the whole spec on the first run after the
    upgrade. A key already in the canonical spelling wins; its legacy-spelled
    twin only fills the verbs the canonical one is missing.
    """
    canonical = {}
    legacy = []
    for path_key, path_item in paths.items():
        normalized = normalize_route(path_key) or path_key
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


def canonicalize_index_keys(api_index, endpoint_key_of):
    """Same upgrade for the api_index: an index written before routes were
    canonicalized holds the framework's spelling, which reads as removed while
    the freshly extracted key reads as added. The canonical key wins."""
    if not isinstance(api_index, dict):
        return api_index
    canonical = {}
    legacy = []
    for key, entry in api_index.items():
        method, route = split_endpoint_key(key)
        normalized = endpoint_key_of(route, method) if route else key
        if normalized == key:
            canonical[key] = entry
        else:
            legacy.append((normalized, entry))
    for normalized, entry in legacy:
        canonical.setdefault(normalized, entry)
    return canonical


def load_existing_swagger(swagger_path, normalize_route):
    if not os.path.exists(swagger_path):
        return None
    try:
        with open(swagger_path, "r", encoding="utf-8") as f:
            return migrate_legacy_spec(json.load(f), normalize_route)
    except Exception:
        return None


def remove_endpoint_from_swagger(swagger, key, normalize_route):
    method, route = split_endpoint_key(key)
    # An index written before routes were canonicalized still holds :id keys.
    route = normalize_route(route)
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


def apply_host(swagger, host):
    """The host this run was given wins over the one the stored spec carries,
    otherwise --api-host is silently ignored on every incremental run."""
    if swagger is not None and host:
        swagger["servers"] = [{"url": host}]
    return swagger


def record_coverage(swagger, extracted, generated, skipped, failed, dropped=None):
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


def base_commit_of(swagger):
    """The commit an incremental run diffs against, or None when there is none."""
    info = swagger.get("info", {})
    # Specs written before the extension rename still carry the bare key.
    return info.get("x-commit-reference") or info.get("commit_reference")


def stamp_commit_reference(swagger, commit_hash):
    """Point the stored spec at the commit this run documented."""
    info = swagger.setdefault("info", {})
    info.pop("commit_reference", None)
    info["x-commit-reference"] = commit_hash


# --------------------------------------------------------------------------- #
# api_index
# --------------------------------------------------------------------------- #


def api_index_output_path(output_filepath):
    output_dir = os.path.dirname(output_filepath)
    os.makedirs(output_dir, exist_ok=True)
    return os.path.join(output_dir, "api_index.json")


def write_api_index(api_index, output_path):
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(api_index, f, indent=2)
    except Exception:
        return


def load_existing_api_index(api_index_path, endpoint_key_of):
    if not os.path.exists(api_index_path):
        return None
    try:
        with open(api_index_path, "r", encoding="utf-8") as f:
            return canonicalize_index_keys(json.load(f), endpoint_key_of)
    except Exception:
        return None


def normalize_in_file_dependencies(deps, route, file_path):
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


def dedupe_imports(imports):
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


def merge_file_entry(files, entry):
    for existing in files:
        if existing.get("file_path") == entry.get("file_path"):
            merged = existing.get("imports", []) + entry.get("imports", [])
            existing["imports"] = dedupe_imports(merged)
            return
    files.append(entry)


def endpoint_imports(
    endpoint, abs_file_path, route, load_metadata, get_dependencies, resolve_imported
):
    """The dependency edges one endpoint's index entry records, one hop deep.

    The next run marks the endpoint dirty when any of these files is touched,
    so an endpoint whose lines the extractor could not place, or whose file has
    no metadata, records none and is only ever dirtied by its own file.
    """
    start_line = endpoint.get("start_line")
    end_line = endpoint.get("end_line")
    if not isinstance(start_line, int) or not isinstance(end_line, int):
        return []
    metadata = load_metadata(abs_file_path)
    if not metadata:
        return []
    in_file, imported = get_dependencies(metadata, start_line, end_line, abs_file_path)
    imports = normalize_in_file_dependencies(in_file, route, abs_file_path)
    for item in imported:
        imports.extend(resolve_imported(item, route))
    return imports


def build_api_index(endpoints, endpoint_key_of, method_of, collect_imports):
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
        key = endpoint_key_of(route, method_of(endpoint))
        file_path = endpoint.get("file_path")
        if not file_path:
            continue
        abs_file_path = os.path.abspath(file_path)
        entry = {
            "file_path": abs_file_path,
            "imports": dedupe_imports(collect_imports(endpoint, abs_file_path, route)),
        }
        index_entry = api_index.setdefault(key, {"files": []})
        context_hash_value = endpoint.get("context_hash")
        if context_hash_value:
            index_entry["context_hash"] = context_hash_value
        merge_file_entry(index_entry["files"], entry)
    return api_index


def apply_generated_index_entries(updated_index, generated_index, failed_keys):
    """Refresh the entries of the endpoints that made it, drop the ones that did not.

    A failed key's stale entry is dropped, not kept: once the commit reference
    advances, a kept entry would hide the failure forever, while an absent key
    reads as newly added and is retried on the next run.
    """
    for failed_key in failed_keys:
        updated_index.pop(failed_key, None)
    for entry_key, entry_value in generated_index.items():
        if entry_key in failed_keys:
            continue
        updated_index[entry_key] = entry_value


# --------------------------------------------------------------------------- #
# Per file metadata cache
# --------------------------------------------------------------------------- #


def metadata_cache_dir(output_filepath, pipeline_name):
    # Per file metadata is cached under the output directory, never inside the
    # scanned repo: a run must not create or delete anything in the user's tree.
    return os.path.join(os.path.dirname(output_filepath), "metadata_cache", pipeline_name)


def content_hash(file_path, cache):
    """First 16 hex chars of the file's sha256, read once per run.

    The path is hashed along with the content because an entry records the file
    it came from and the import origins resolved around it, so two files that
    read identically in different directories are not interchangeable.
    """
    key = os.path.abspath(file_path)
    if key not in cache:
        try:
            with open(key, "rb") as f:
                content = f.read()
        except OSError:
            cache[key] = None
        else:
            digest = hashlib.sha256(key.encode("utf-8") + b"\0" + content)
            cache[key] = digest.hexdigest()[:16]
    return cache[key]


def metadata_cache_filename(file_path, content_hash_value):
    """One cache entry name: the file's basename plus the hash of its content.

    A single path component can fill NAME_MAX on its own, so the readable half
    is capped and the hash is what keeps the name unique.
    """
    stem = os.path.splitext(os.path.basename(file_path))[0]
    stem = stem.encode("utf-8")[:200].decode("utf-8", "ignore")
    return f"{stem}_{content_hash_value}.json"


def metadata_cache_path(file_path, cache_dir_of, content_hash_value):
    """Where this file's metadata sits for the content it holds right now.

    The cache directory is only resolved once the file actually hashed, because
    a file that cannot be read has no entry and the output path may not be set.
    """
    if not content_hash_value:
        return None
    return os.path.join(cache_dir_of(), metadata_cache_filename(file_path, content_hash_value))


def prepare_metadata_cache(cache_dir, version):
    """The cache directory, emptied first when another schema wrote it."""
    marker_path = os.path.join(cache_dir, METADATA_CACHE_VERSION_FILE)
    try:
        with open(marker_path, "r", encoding="utf-8") as f:
            stored = f.read().strip()
    except OSError:
        stored = None
    if stored != version:
        shutil.rmtree(cache_dir, ignore_errors=True)
    os.makedirs(cache_dir, exist_ok=True)
    with open(marker_path, "w", encoding="utf-8") as f:
        f.write(version)
    return cache_dir


def index_paths_exist(api_index):
    """True when every file the index references still exists.

    After a repository moves, keys still match but the stored absolute paths
    do not; the no-change fast return must fall through so the hash-skip loop
    rebuilds entries with fresh paths (at zero LLM cost)."""
    if not isinstance(api_index, dict):
        return True
    for entry in api_index.values():
        if not isinstance(entry, dict):
            continue
        for file_entry in entry.get("files", []):
            file_path = file_entry.get("file_path")
            if file_path and not os.path.exists(file_path):
                return False
            for imported in file_entry.get("imports", []):
                import_path = imported.get("file_path")
                if import_path and not os.path.exists(import_path):
                    return False
    return True


def cache_file_metadata(
    file_path, directory_path, cache_path_of, process_file, entries, on_error=None
):
    """Extract one file's metadata, unless the cache already holds its content."""
    cache_path = cache_path_of(file_path)
    if not cache_path:
        return
    key = os.path.abspath(file_path)
    if os.path.exists(cache_path):
        # Byte for byte what a previous run parsed, so its metadata still
        # holds, provided the entry itself is intact. A truncated write must
        # not become a permanent live entry.
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                json.load(f)
            entries[key] = cache_path
            return
        except (OSError, ValueError):
            try:
                os.remove(cache_path)
            except OSError:
                pass
    # One unreadable file must not cost the run every other file's metadata.
    try:
        file_info = process_file(file_path, directory_path)
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(file_info, f, indent=4)
        os.replace(tmp_path, cache_path)
    except Exception as exc:
        if on_error is not None:
            on_error(file_path, exc)
        return
    entries[key] = cache_path


def build_metadata_cache(
    directory_path, prepare, should_process, is_supported_file, cache_file
):
    """One cache entry per source file of this language below the repo root."""
    prepare()
    for root, _, files in os.walk(directory_path):
        for filename in files:
            file_path = os.path.join(root, filename)
            if (
                os.path.exists(file_path)
                and should_process(str(file_path), directory_path)
                and is_supported_file(file_path)
            ):
                cache_file(file_path, directory_path)


def prune_metadata_cache(cache_dir, entries):
    """Drop entries for content the repo no longer holds, so the cache stays bounded."""
    live = set(entries.values())
    try:
        scanned = list(os.scandir(cache_dir))
    except OSError:
        return
    for entry in scanned:
        if not entry.is_file() or not entry.name.endswith(".json"):
            continue
        if entry.path in live:
            continue
        try:
            os.remove(entry.path)
        except OSError:
            pass


def load_file_metadata(cache_path):
    if not cache_path or not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Prompt budget
# --------------------------------------------------------------------------- #


def block_text(block):
    """A code block arrives either as a list of source lines or as plain text."""
    if isinstance(block, str):
        return block
    return "".join(str(line) for line in block or [])


def truncate_to_tokens(text, max_tokens, marker=TRUNCATION_MARKER):
    """
    Cut text down to its first max_tokens tokens, and say whether anything was
    cut. The character position is binary searched because only the token count
    is exposed, not the encoder.
    """
    if not text or num_tokens_from_string(text) <= max_tokens:
        return text, False
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if num_tokens_from_string(text[:middle]) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low] + marker, True


def dedupe_blocks(blocks):
    """The same helper body is attached to every endpoint of a file, so an
    undeduped batch prompt repeats it once per endpoint."""
    seen = set()
    unique = []
    for block in blocks or []:
        text = block_text(block)
        if not text.strip() or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def fit_context(blocks, budget):
    """Deduped context blocks that fit the budget, and how many were dropped.

    Handler bodies are the part the model cannot document without, so they are
    priced first and whatever context does not fit is dropped from the end.
    """
    kept = []
    used = 0
    unique = dedupe_blocks(blocks)
    for index, text in enumerate(unique):
        cost = num_tokens_from_string(text) + 2
        if used + cost > budget:
            return kept, len(unique) - index
        kept.append(text)
        used += cost
    return kept, 0


def report_context_trim(source, dropped, truncated):
    if not dropped and not truncated:
        return
    print(f"apimesh: context truncated for {source} ({dropped} blocks dropped)")


def handler_section(header, body, max_tokens, marker=TRUNCATION_MARKER):
    """One endpoint's prompt section, with its handler capped."""
    body_text, was_truncated = truncate_to_tokens(block_text(body), max_tokens, marker)
    return (f"{header}\n{body_text}" if header else body_text), was_truncated


def section_token_cost(header, body, max_tokens, marker=TRUNCATION_MARKER):
    """What one endpoint's section costs against the batch budget.

    The caller packs batches with this, so it has to price exactly the section
    the prompt ends up carrying, truncation marker included.
    """
    section, _ = handler_section(header, body, max_tokens, marker)
    return num_tokens_from_string(section)


def apply_context_budget(
    handler_sections, shared_blocks, file_label, max_handler_tokens, budget,
    marker=TRUNCATION_MARKER,
):
    """
    Fit the handler bodies and the shared context inside the context budget.
    Handler bodies are kept first, each capped at max_handler_tokens; the
    deduped shared blocks then fill whatever is left and are dropped from the
    end. The batches are packed so their sections already fit, so what is left
    here is whatever room the shared blocks get.
    Returns (kept_shared_blocks, handler_sections) as plain text.
    """
    truncated = False
    sections = []
    used = 0
    for header, body in handler_sections:
        section, was_truncated = handler_section(header, body, max_handler_tokens, marker)
        truncated = truncated or was_truncated
        sections.append(section)
        used += num_tokens_from_string(section)
    kept_blocks, dropped = fit_context(shared_blocks, max(budget - used, 0))
    report_context_trim(file_label, dropped, truncated)
    return kept_blocks, sections


def group_jobs_by_file(endpoint_jobs):
    """Endpoint jobs bucketed by the source file they were extracted from."""
    by_file = {}
    for job in endpoint_jobs:
        by_file.setdefault(job.get("file_path") or "", []).append(job)
    return by_file


def pack_batches(items, section_tokens, max_items, budget):
    """One file's endpoints split into batches that fit the context budget.

    Capping each handler on its own still let ten of them add up to far more
    than the whole prompt can carry, so a batch is closed as soon as the next
    section would push its sections past the budget. max_items stays the
    secondary limit.
    """
    batches = []
    current = []
    used = 0
    for item in items:
        cost = section_tokens(item)
        if current and (len(current) >= max_items or used + cost > budget):
            batches.append(current)
            current = []
            used = 0
        current.append(item)
        used += cost
    if current:
        batches.append(current)
    return batches


def batch_response_is_usable(response):
    return isinstance(response, dict) and isinstance(response.get("paths"), dict)


def retry_batch_call(call, attempts=2):
    """A usable batch reply, or None once the attempts are spent.

    A second unusable reply is not worth a third batch call: the caller spends
    per-endpoint calls on those endpoints instead.
    """
    for _ in range(attempts):
        try:
            response = call()
        except Exception:
            response = None
        if batch_response_is_usable(response):
            return response
    return None


# --------------------------------------------------------------------------- #
# Context hashing and the incremental skeleton
# --------------------------------------------------------------------------- #


def context_hash(context_blocks, method_definition, max_handler_tokens=MAX_HANDLER_TOKENS):
    """
    Fingerprint of the prompt text one endpoint carries: its capped handler body
    followed by its deduped context blocks, in the order the prompt sends them.
    The endpoint label, the verb and the rest of the batch are deliberately left
    out, so the value can be recomputed for a single endpoint before the batches
    are packed and a mirrored rails PUT lands on exactly its PATCH's hash.
    """
    # One recipe for all four pipelines, unified on the node/rails variant. Go
    # and python stored a label-prefixed, undeduped hash, so their first
    # incremental run after this change matches nothing and regenerates once.
    body, _ = truncate_to_tokens(block_text(method_definition), max_handler_tokens)
    blocks = dedupe_blocks(context_blocks)
    return hashlib.sha256("\n\n".join([body] + blocks).encode("utf-8")).hexdigest()


def stored_context_hash(entry):
    if not isinstance(entry, dict):
        return None
    stored = entry.get("context_hash")
    return stored if isinstance(stored, str) and stored else None


def group_endpoints(endpoints, endpoint_key_of, method_of):
    grouped = {}
    for endpoint in endpoints:
        key = endpoint_key_of(endpoint.get("route"), method_of(endpoint))
        grouped.setdefault(key, []).append(endpoint)
    return grouped


def endpoint_has_changed(existing_entry, endpoints_for_key, changed_files):
    """Whether an edit reached this endpoint, one dependency hop included.

    A handler is documented from the helpers and types it pulls in, so editing
    one of those files makes the endpoint stale even though its own file was
    never touched. The hop stops there: the context hash decides whether the
    edit actually changed anything the model would see.
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


def should_regenerate_fully(keys_to_update, new_keys):
    """Whether the surgical pass should hand the run back to a full regeneration.

    Past half the endpoints a surgical pass buys nothing: it costs the same LLM
    calls while carrying a spec and an index it has to patch around, while the
    full run writes a fresh index with a hash for every endpoint.
    """
    if len(keys_to_update) * 2 <= len(new_keys):
        return False
    print(
        f"apimesh: {len(keys_to_update)} of {len(new_keys)} endpoints affected, "
        "running a full regeneration"
    )
    return True


def rebuild_unchanged_index_entries(
    keys, endpoint_map, existing_index, updated_index, build_index
):
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
        rebuilt = build_index(endpoint_map.get(key, [])).get(key)
        if not rebuilt:
            continue
        stored = stored_context_hash(existing_index.get(key))
        if stored:
            rebuilt["context_hash"] = stored
        updated_index[key] = rebuilt


# ---------------------------------------------------------------------------
# Contract lane integration, shared by every pipeline
# ---------------------------------------------------------------------------

def base_swagger(repo_name, host, commit_hash, repo_url, generated_at):
    """The empty document a contract-only run starts from."""
    return {
        "openapi": "3.0.0",
        "info": {
            "title": repo_name,
            "version": "1.0.0",
            "description": "This Swagger file was generated using OpenAI GPT.",
            "x-generated-at": generated_at,
            "x-commit-reference": commit_hash,
            "x-github-repo-url": repo_url,
        },
        "servers": [{"url": host}],
        "paths": {},
    }


def strip_contract_operations(swagger):
    """Remove every spec-sourced operation and the contract components.

    Used when the lane is disabled: an incremental fast path would otherwise
    hand back contract operations from a previous run as current.
    """
    if not isinstance(swagger, dict):
        return swagger
    paths = swagger.get("paths") or {}
    for route in list(paths):
        item = paths[route]
        if not isinstance(item, dict):
            continue
        for verb in list(item):
            operation = item[verb]
            if isinstance(operation, dict) and any(
                str(source).startswith("spec:")
                for source in operation.get("x-apimesh-source") or []
            ):
                del item[verb]
        if not item:
            del paths[route]
    swagger.pop("components", None)
    coverage = (swagger.get("info") or {}).get("x-apimesh-coverage")
    if isinstance(coverage, dict):
        coverage["contract"] = {"disabled": True}
    return swagger


def integrate_contract_lane(directory_path, endpoint_jobs, method_of, normalize_route):
    """Run the contract lane and supersede code jobs the contracts cover.

    Returns (lane_result, reconciled, filtered_jobs). lane_result is None when
    the lane is disabled; filtered_jobs is then the input unchanged. A code
    route a served contract also declares is documented from the contract, so
    generating it again would double-spend and collide.
    """
    from contract_lane.lane import run_lane
    from contract_lane.reconcile import reconcile

    lane_result = run_lane(directory_path)
    if lane_result is None:
        return None, None, endpoint_jobs
    code_ops = [
        {
            "method": (method_of(job) or "").upper(),
            "route": normalize_route(job.get("route")),
            "source_id": str(position),
            # Routing conditions ride onto the contract operation that
            # supersedes this handler, instead of vanishing with it.
            "conditions": job.get("conditions"),
        }
        for position, job in enumerate(endpoint_jobs)
    ]
    reconciled = reconcile(lane_result["rows"], code_ops, directory_path)
    allowed = {int(op["source_id"]) for op in reconciled["code_to_generate"]}
    filtered = [job for position, job in enumerate(endpoint_jobs) if position in allowed]
    return lane_result, reconciled, filtered


def write_repo_profile(report, reconciled, output_filepath):
    """The run's full account of the contract lane, for humans and agents.

    A generated report, never an input: the next run re-derives everything
    from the repository. Best-effort, a profile that cannot be written must
    not fail the spec.
    """
    profile = {
        "contract_lane": report,
        "conflicts": reconciled["conflicts"],
        "superseded_code_endpoints": len(reconciled["superseded_code"]),
    }
    path = os.path.join(os.path.dirname(output_filepath), "repo_profile.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w") as handle:
            json.dump(profile, handle, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except OSError as ex:
        print(f"apimesh: could not write {path}: {ex}")


def finish_with_contract(swagger, reconciled, report, output_filepath):
    """Merge the contract lane's operations into the code lane's document.

    Spec-sourced operations from a previous run are stripped first, so a
    contract operation that disappeared from its spec leaves the document:
    the contract portion is rebuilt from scratch on every run.
    """
    paths = swagger.setdefault("paths", {})
    for route in list(paths):
        item = paths[route]
        if not isinstance(item, dict):
            continue
        for verb in list(item):
            operation = item[verb]
            if isinstance(operation, dict) and any(
                str(source).startswith("spec:")
                for source in operation.get("x-apimesh-source") or []
            ):
                del item[verb]
        if not item:
            del paths[route]
    for route, item in reconciled["paths"].items():
        paths.setdefault(route, {}).update(item)
    # Components are a contract-lane artifact, so the merged set replaces the
    # previous one wholesale; anything else keeps renamed or deleted schemas
    # around forever.
    if reconciled["components"]:
        swagger["components"] = reconciled["components"]
    else:
        swagger.pop("components", None)

    contract_operations = sum(len(item) for item in reconciled["paths"].values())
    summary = {
        "specs_found": report["specs_found"],
        "specs_served": len(report["served"]),
        "specs_excluded": len(report["excluded"]),
        "specs_candidate": len(report["candidates"]),
        "operations": contract_operations,
        "unresolved_operations": len(report["unresolved_operations"]),
        "conflicts": len(reconciled["conflicts"]),
        "rewrite_failures": len(reconciled.get("rewrite_failures") or []),
        "superseded_code_endpoints": len(reconciled["superseded_code"]),
        "truncated": report["truncated"],
    }
    info = swagger.setdefault("info", {})
    coverage = info.get("x-apimesh-coverage")
    if isinstance(coverage, dict):
        coverage["contract"] = summary
    else:
        info["x-apimesh-coverage"] = {"contract": summary}
    # A repo with no contracts gets the coverage line but no profile file:
    # dropping an empty report into every plain repo is noise. An existing
    # profile is always rewritten, so a repo whose last contract disappeared
    # does not keep claiming it is served.
    profile_path = os.path.join(
        os.path.dirname(output_filepath), "repo_profile.json"
    )
    if report["specs_found"] or os.path.exists(profile_path):
        write_repo_profile(report, reconciled, output_filepath)
        print(
            f"apimesh: contract lane served {summary['specs_served']} specs "
            f"({contract_operations} operations), excluded {summary['specs_excluded']}, "
            f"candidates {summary['specs_candidate']}"
        )
    return swagger

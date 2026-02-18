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
    get_function_definition_swagger,
)
from golang_pipeline.find_api_definition_files import find_api_definition_files
from golang_pipeline.generate_file_information import process_file
from golang_pipeline.identify_api_functions import find_api_endpoints
from utils import get_git_commit_hash, get_github_repo_url, get_repo_path, get_repo_name, get_api_index_filepath

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


def should_process_directory(dir_path: str) -> bool:
    path_parts = dir_path.split(os.sep)
    return not any(part in config.ignored_dirs for part in path_parts)


def _api_index_output_path(directory_path: str) -> str:
    output_path = get_api_index_filepath()
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    return output_path


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


def _endpoint_key(route, method):
    method_value = (method or "UNKNOWN").upper()
    route_value = route or ""
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


def _write_api_index(directory_path: str, api_index: dict) -> None:
    output_path = _api_index_output_path(directory_path)
    try:
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(api_index, handle, indent=2)
    except Exception:
        return


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


def run_swagger_generation(host: str) -> Dict:
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
                    and should_process_directory(file_path)
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
                "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
                "commit_reference": get_git_commit_hash(),
                "github_repo_url": get_github_repo_url(),
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

        api_index = _build_api_index(endpoint_jobs)
        _write_api_index(directory_path, api_index)

        if not endpoint_jobs:
            return swagger

        def _generate_swagger_fragment(method_info: Dict) -> Dict:
            context_blocks, method_definition = provide_context_codeblock(
                directory_path, method_info
            )
            http_method = method_info.get("http_method") or "GET"
            if http_method:
                context_blocks = [[f"HTTP_METHOD: {http_method}\\n"]] + context_blocks
            handler_metadata = method_info.get("handler_selector") or method_info.get("name")
            if handler_metadata:
                context_blocks = [[f"HANDLER: {handler_metadata}\\n"]] + context_blocks
            return get_function_definition_swagger(
                method_definition, context_blocks, method_info["route"], http_method
            )

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(_generate_swagger_fragment, job)
                for job in endpoint_jobs
            ]
            for future in as_completed(futures):
                swagger_fragment = future.result()
                for path_key, methods in swagger_fragment.get("paths", {}).items():
                    swagger.setdefault("paths", {}).setdefault(path_key, {})
                    for method, payload in methods.items():
                        swagger["paths"][path_key][method] = payload

        return swagger
    finally:
        if metadata_dir and os.path.exists(metadata_dir):
            shutil.rmtree(metadata_dir, ignore_errors=True)
        _METADATA_DIR = None

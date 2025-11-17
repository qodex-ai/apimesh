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
from utils import get_git_commit_hash, get_github_repo_url
from rails_pipeline.definition_swagger_generator import (
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


def should_process_directory(dir_path: str) -> bool:
    """
    Check if a directory should be processed or ignored.
    Mirrors the logic used by the Node.js and Python generators.
    """
    path_parts = dir_path.split(os.sep)
    return not any(part in config.ignored_dirs for part in path_parts)


def _sanitize_json_filename(file_path: str) -> str:
    """
    Convert a filesystem path into a deterministic filename that can be used
    to persist metadata in the staging directory.
    """
    normalized = file_path.replace(os.sep, "_q_")
    return f"{normalized}.json"


def run_swagger_generation(directory_path: str, host: str, repo_name: str) -> Dict:
    new_dir_name = "qodex_file_information"
    new_dir_path = os.path.join(directory_path, new_dir_name)
    os.makedirs(new_dir_path, exist_ok=True)

    try:
        for root, _, files in os.walk(directory_path):
            for filename in files:
                file_path = os.path.join(root, filename)
                if (
                    os.path.exists(file_path)
                    and should_process_directory(str(file_path))
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
                "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
                "commit_reference": get_git_commit_hash(directory_path),
                "github_repo_url": get_github_repo_url(directory_path),
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

        if not endpoint_jobs:
            return swagger

        def _generate_swagger_fragment(method_info: Dict) -> Dict:
            context_blocks, method_definition = provide_context_codeblock(
                directory_path, method_info
            )
            http_method = method_info.get("http_method")
            if http_method:
                context_blocks = [[f"HTTP_METHOD: {http_method}\n"]] + context_blocks
            mirrored_from = method_info.get("mirrored_from")
            if mirrored_from:
                context_blocks = [
                    [f"MIRRORED_FROM: {mirrored_from}\n"]
                ] + context_blocks
            return get_function_definition_swagger(
                method_definition,
                context_blocks,
                method_info["route"],
                http_method=http_method,
            )

        with ThreadPoolExecutor(max_workers=5) as executor:
            completed = 0
            start_time = time.time()
            latest_message = ""
            futures = [executor.submit(_generate_swagger_fragment, method) for method in endpoint_jobs]
            for future in as_completed(futures):
                swagger_for_def = future.result()
                _merge_paths(swagger, swagger_for_def)
                completed += 1
                end_time = time.time()
                latest_message = (
                    f"Completed generating endpoint related information for {completed} endpoints in "
                    f"{int(end_time - start_time)} seconds"
                )
                print(latest_message, end="\r", flush=True)
            if completed:
                print(latest_message)

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

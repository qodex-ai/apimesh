import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

from config import Configurations
from ruby_on_rails_swagger_generation.definition_swagger_generator import (
    get_function_definition_swagger,
)
from ruby_on_rails_swagger_generation.generate_file_information import (
    process_file,
)
from ruby_on_rails_swagger_generation.find_api_definition_files import (
    find_api_definition_files,
)
from ruby_on_rails_swagger_generation.identify_api_functions import (
    find_api_endpoints,
)

config = Configurations()


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
    return context_code_blocks, method_definition_code_block

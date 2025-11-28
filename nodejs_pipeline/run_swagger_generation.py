import os, json, re
import shutil
import datetime
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from nodejs_pipeline.generate_file_information import process_file
from nodejs_pipeline.find_api_definition_files import find_api_definition_files
from nodejs_pipeline.identify_api_functions import find_api_endpoints_js
from config import Configurations
from nodejs_pipeline.definition_swagger_generator import get_function_definition_swagger
from nodejs_pipeline.constants import (
    SUPPORTED_NODE_FILE_EXTENSIONS,
    METADATA_DIR_NAME,
)
from utils import get_git_commit_hash, get_github_repo_url, get_repo_path, get_repo_name

config = Configurations()


def _metadata_dir_path(directory_path: str) -> str:
    return os.path.join(directory_path, METADATA_DIR_NAME)


def _metadata_file_name(file_path: str) -> str:
    sanitized = str(file_path).replace("/", "_q_").replace("\\", "_q_")
    name, _ = os.path.splitext(sanitized)
    return f"{name}.json"


def should_process_directory(dir_path: str) -> bool:
    """
    Check if a directory should be processed or ignored
    """
    path_parts = dir_path.split(os.sep)
    return not any(part in config.ignored_dirs for part in path_parts)

def _normalize_route(route: str):
    if not route:
        return route
    # Convert Express-style :param to OpenAPI {param}
    return re.sub(r":([A-Za-z_][\w-]*)", r"{\1}", route)


def _extract_brace_block(lines, start_idx: int):
    brace_depth = 0
    collected = []
    started = False
    for line in lines[start_idx:]:
        collected.append(line)
        brace_depth += line.count("{") - line.count("}")
        if "{" in line:
            started = True
        if started and brace_depth <= 0:
            break
    return collected


def _find_use_block(lines, pattern: str):
    matcher = re.compile(pattern)
    for idx, line in enumerate(lines):
        if matcher.search(line):
            return _extract_brace_block(lines, idx)
    return None


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
                    and should_process_directory(str(file_path))
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
        all_endpoints_dict = dict()
        for file in api_definition_files:
            all_endpoints = []
            py_file = Path(file)
            eps = find_api_endpoints_js(py_file)
            if eps:
                all_endpoints.extend(eps)
                all_endpoints_dict[file] = all_endpoints
        swagger = {
                "openapi": "3.0.0",
                "info": {
                    "title": repo_name,
                    "version": "1.0.0",
                    "description": "This Swagger file was generated using OpenAI GPT.",
                    "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
                    "commit_reference": get_git_commit_hash(),
                    "github_repo_url": get_github_repo_url()
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
            return swagger

        def _generate_swagger_fragment(method_info):
            context_code_blocks, method_definition_code_block = provide_context_codeblock(directory_path, method_info)
            return get_function_definition_swagger(method_definition_code_block, context_code_blocks, method_info['route'])

        max_workers = min(5, len(endpoint_jobs))
        start_time = time.time()
        completed = 0
        latest_message = ""
        with ThreadPoolExecutor(max_workers=max_workers or 1) as executor:
            futures = [executor.submit(_generate_swagger_fragment, method) for method in endpoint_jobs]
            for future in as_completed(futures):
                swagger_for_def = future.result()
                _merge_paths(swagger, swagger_for_def)
                completed += 1
                latest_message = (
                    f"Completed generating endpoint related information for {completed} endpoints in "
                    f"{int(time.time() - start_time)} seconds"
                )
                print(latest_message, end="\r", flush=True)
        if completed:
            print(latest_message)
        _post_process_swagger(swagger)
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
    # Include catch-all responder middleware (e.g., app.use('/:name', ...)) to give the LLM response semantics.
    responder_block = _find_use_block(lines, pattern=r"\.use\s*\(\s*['\"]/:")
    if responder_block:
        context_code_blocks.append(responder_block)
    return context_code_blocks, method_definition_code_block


def _merge_paths(target, source):
    paths = source.get("paths", {})
    for path_key, methods in paths.items():
        target.setdefault("paths", {})
        normalized_path = _normalize_route(path_key)
        target["paths"].setdefault(normalized_path, {})
        for method, payload in methods.items():
            target["paths"][normalized_path][method] = payload


def _post_process_swagger(swagger):
    """
    Clean up generated swagger to better align with tinyhttp behavior:
    - drop wildcard /* CORS path
    - normalize any lingering :param segments
    - adjust /{name} POST to return 201 and optional body
    - remove spurious 400 from GET /{name}
    - allow string|array for _dependent in DELETE /{name}/{id}
    """
    paths = swagger.get("paths", {})
    # drop wildcard CORS catch-all if present
    paths.pop("/*", None)
    paths.pop("*", None)

    # Re-key any lingering express-style paths
    for original in list(paths.keys()):
        normalized = _normalize_route(original)
        if normalized != original:
            existing = paths.pop(original)
            if normalized not in paths:
                paths[normalized] = existing
            else:
                paths[normalized].update(existing)

    # Fix POST /{name}
    post_name = paths.get("/{name}", {}).get("post")
    if post_name:
        # body optional
        if "requestBody" in post_name:
            post_name["requestBody"]["required"] = False
        # prefer 201, reuse existing schema if available
        responses = post_name.setdefault("responses", {})
        schema = None
        for code in ("201", "200"):
            resp = responses.get(code)
            if not resp:
                continue
            content = resp.get("content", {})
            app_json = content.get("application/json", {})
            schema = app_json.get("schema")
            if schema:
                break
        if not schema:
            schema = {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "additionalProperties": True,
            }
        responses.clear()
        responses["201"] = {
            "description": "Resource created successfully.",
            "content": {
                "application/json": {
                    "schema": schema
                }
            }
        }
        responses["404"] = {"description": "Collection not found."}

    # Clean GET /{name} errors
    get_name = paths.get("/{name}", {}).get("get")
    if get_name:
        get_responses = get_name.get("responses", {})
        get_responses.pop("400", None)

    # Fix _dependent param schema on DELETE /{name}/{id}
    delete_item = paths.get("/{name}/{id}", {}).get("delete")
    if delete_item:
        for param in delete_item.get("parameters", []):
            if param.get("name") == "_dependent":
                param["schema"] = {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ]
                }
                break

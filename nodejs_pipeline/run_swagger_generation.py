import os, json
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
from utils import get_git_commit_hash, get_github_repo_url, get_repo_path, get_repo_name

config = Configurations()


def should_process_directory(dir_path: str) -> bool:
    """
    Check if a directory should be processed or ignored
    """
    path_parts = dir_path.split(os.sep)
    return not any(part in config.ignored_dirs for part in path_parts)

def run_swagger_generation(host):
    directory_path = get_repo_path()
    repo_name = get_repo_name()
    new_dir_name = "qodex_file_information"
    new_dir_path = os.path.join(directory_path, new_dir_name)
    os.makedirs(new_dir_path, exist_ok=True)
    try:
        for root, dirs, files in os.walk(directory_path):
            for file in files:
                file_path = os.path.join(root, file)
                if os.path.exists(file_path) and should_process_directory(str(file_path)) and file_path.endswith(".js"):
                    try:
                        file_info = process_file(file_path, directory_path)
                    except Exception:
                        continue
                    json_file_name = new_dir_path +"/"+ str(file_path).replace("/", "_q_").strip(".js") + ".json"
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
        origin_file_name = func['origin']
        json_dir_path = directory_path + "/" + "qodex_file_information"
        json_file = str(origin_file_name).replace("/", "_q_").strip(".js") + ".json"
        complete_json_file_path = json_dir_path + "/" + json_file
        with open(complete_json_file_path, "r") as f:
            data = json.load(f)
            f.close()
        for item in data['elements']['classes']:
            if item['name'] == func['imported_name']:
                visited = True
                with open(origin_file_name, "r") as f:
                    lines = f.readlines()
                    f.close()
                code_blocks.append(lines[item['start_line']-1: item['end_line']])
                break
        if not visited:
            for item in data['elements']['functions']:
                if item['name'] == func['imported_name']:
                    visited = True
                    with open(origin_file_name, "r") as f:
                        lines = f.readlines()
                        f.close()
                    code_blocks.append(lines[item['start_line'] - 1: item['end_line']])
                    break
        if not visited:
            for item in data['elements']['variables']:
                if item['name'] == func['imported_name']:
                    with open(origin_file_name, "r") as f:
                        lines = f.readlines()
                        f.close()
                    code_blocks.append(lines[item['start_line'] - 1: item['end_line']])
                    break
    return code_blocks


def provide_context_codeblock(directory_path, method_info):
    file_name = method_info['file_path']
    with open(method_info['file_path'], "r") as f:
        lines = f.readlines()
    method_definition_code_block = lines[method_info["start_line"]-1: method_info["end_line"]]
    json_dir_path = directory_path + "/" + "qodex_file_information"
    json_file = str(file_name).replace("/", "_q_").strip(".js") + ".json"
    complete_json_file_path = json_dir_path + "/" + json_file
    with open(complete_json_file_path, "r") as f:
        data = json.load(f)
    in_file_dependency_functions, imported_functions = get_dependencies(data, method_info["start_line"], method_info["end_line"], method_info['file_path'])
    context_code_blocks = get_code_blocks(in_file_dependency_functions, imported_functions, file_name, directory_path)
    return context_code_blocks, method_definition_code_block


def _merge_paths(target, source):
    paths = source.get("paths", {})
    for path_key, methods in paths.items():
        target.setdefault("paths", {})
        target["paths"].setdefault(path_key, {})
        for method, payload in methods.items():
            target["paths"][path_key][method] = payload

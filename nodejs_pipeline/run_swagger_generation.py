import os, json
import shutil
import datetime
from pathlib import Path
from nodejs_pipeline.generate_file_information import process_file
from nodejs_pipeline.find_api_definition_files import find_api_definition_files
from nodejs_pipeline.identify_api_functions import find_api_endpoints_js
from config import Configurations
from nodejs_pipeline.definition_swagger_generator import get_function_definition_swagger
from utils import get_git_commit_hash, get_github_repo_url

config = Configurations()


def should_process_directory(dir_path: str) -> bool:
    """
    Check if a directory should be processed or ignored
    """
    path_parts = dir_path.split(os.sep)
    return not any(part in config.ignored_dirs for part in path_parts)

def run_swagger_generation(directory_path, host, repo_name):
    new_dir_name = "qodex_file_information"
    new_dir_path = os.path.join(directory_path, new_dir_name)
    os.makedirs(new_dir_path, exist_ok=True)
    for root, dirs, files in os.walk(directory_path):
        for file in files:
            file_path = os.path.join(root, file)
            if os.path.exists(file_path) and should_process_directory(str(file_path)) and file_path.endswith(".js"):
                file_info = process_file(file_path, directory_path)
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
                "commit_reference": get_git_commit_hash(directory_path),
                "github_repo_url": get_github_repo_url(directory_path)
            },
            "servers": [
                {
                    "url": host
                }
            ],
            "paths": {}
        }
    for key, value in all_endpoints_dict.items():
        for item in value:
            if item['type'] == 'class':
                if item['methods']:
                    for item1 in item['methods']:
                        context_code_blocks, method_definition_code_block = provide_context_codeblock(directory_path, item1)
                        swagger_for_def = get_function_definition_swagger(method_definition_code_block, context_code_blocks, item1['route'])
                        key = list(swagger_for_def['paths'].keys())[0]
                        if key not in swagger["paths"]:
                            swagger["paths"][key] = {}
                        _method_list = list(swagger_for_def['paths'][key].keys())
                        if not _method_list:
                            continue
                        _method = _method_list[0]
                        swagger["paths"][key][_method] = swagger_for_def['paths'][key][_method]
            else:
                context_code_blocks, method_definition_code_block = provide_context_codeblock(directory_path,item)
                swagger_for_def = get_function_definition_swagger(method_definition_code_block, context_code_blocks, item['route'])
                key = list(swagger_for_def['paths'].keys())[0]
                if key not in swagger["paths"]:
                    swagger["paths"][key] = {}
                _method_list = list(swagger_for_def['paths'][key].keys())
                if not _method_list:
                    continue
                _method = _method_list[0]
                swagger["paths"][key][_method] = swagger_for_def['paths'][key][_method]

    shutil.rmtree(new_dir_path)
    return swagger


def get_dependencies(data, start_line, end_line, file_path):
    existing_function_names = [item['name'] for item in data['elements']['functions'] if item['name'] not in ['get', 'post', 'put', 'delete', 'patch']]
    in_file_dependency_functions = []
    for item in data['elements']['function_calls']:
        if (item['name'] in existing_function_names) and item['start_line'] >= start_line and item['end_line'] <= end_line:
            item['file_path'] = file_path
            in_file_dependency_functions.append(item)
    imported_functions = []
    for item in data['elements']['imports']:
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

def get_code_blocks(in_file_dependency_functions, imported_functions, file_name, directory_path):
    code_blocks = []
    for block in in_file_dependency_functions:
        with open(file_name, "r") as f:
            lines = f.readlines()
            f.close()
        code_blocks.append(lines[block['function_start_line'] - 1 : block['function_start_line']])
    for func in imported_functions:
        visited = False
        file_name = func['origin']
        json_dir_path = directory_path + "/" + "qodex_file_information"
        json_file = str(file_name).replace("/", "_q_").strip(".js") + ".json"
        complete_json_file_path = json_dir_path + "/" + json_file
        with open(complete_json_file_path, "r") as f:
            data = json.load(f)
            f.close()
        for item in data['elements']['classes']:
            if item['name'] == func['imported_name']:
                visited = True
                with open(file_name, "r") as f:
                    lines = f.readlines()
                    f.close()
                code_blocks.append(lines[item['start_line']-1: item['end_line']])
                break
        if not visited:
            for item in data['elements']['functions']:
                if item['name'] == func['imported_name']:
                    visited = True
                    with open(file_name, "r") as f:
                        lines = f.readlines()
                        f.close()
                    code_blocks.append(lines[item['start_line'] - 1: item['end_line']])
                    break
        if not visited:
            for item in data['elements']['variables']:
                if item['name'] == func['imported_name']:
                    with open(file_name, "r") as f:
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



import os, json, ast
import shutil
import datetime
from pathlib import Path
from python_pipeline.generate_file_information import process_file
from python_pipeline.find_api_definition_files import find_api_definition_files
from python_pipeline.identify_api_functions import set_parents, find_api_endpoints
from config import Configurations
from python_pipeline.definition_swagger_generator import get_function_definition_swagger
from utils import get_git_commit_hash, get_github_repo_url, get_repo_path, get_repo_name, get_output_filepath

config = Configurations()


def should_process_directory(dir_path: str) -> bool:
    """
    Check if a directory should be processed or ignored
    """
    path_parts = dir_path.split(os.sep)
    return not any(part in config.ignored_dirs for part in path_parts)


def _api_index_output_path(directory_path: str) -> str:
    output_dir = os.path.dirname(get_output_filepath())
    os.makedirs(output_dir, exist_ok=True)
    return os.path.join(output_dir, "api_index.json")


def _metadata_file_path(directory_path: str, file_path: str) -> str:
    json_dir_path = os.path.join(directory_path, "qodex_file_information")
    sanitized = str(file_path).replace("/", "_q_").replace("\\", "_q_")
    json_file = sanitized.strip(".py") + ".json"
    return os.path.join(json_dir_path, json_file)


def _load_file_metadata(directory_path: str, file_path: str):
    json_file_path = _metadata_file_path(directory_path, file_path)
    if not os.path.exists(json_file_path):
        return None
    try:
        with open(json_file_path, "r", encoding="utf-8") as f:
            return json.load(f)
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
        api_index.setdefault(key, {"files": []})
        _merge_file_entry(api_index[key]["files"], entry)
    return api_index


def _write_api_index(directory_path: str, api_index: dict) -> None:
    output_path = _api_index_output_path(directory_path)
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(api_index, f, indent=2)
    except Exception:
        return

def run_swagger_generation(host):
    directory_path = get_repo_path()
    repo_name = get_repo_name()
    new_dir_name = "qodex_file_information"
    new_dir_path = os.path.join(directory_path, new_dir_name)
    os.makedirs(new_dir_path, exist_ok=True)
    for root, dirs, files in os.walk(directory_path):
        for file in files:
            file_path = os.path.join(root, file)
            if os.path.exists(file_path) and should_process_directory(str(file_path)) and file_path.endswith(".py"):
                file_info = process_file(file_path, directory_path)
                json_file_name = new_dir_path +"/"+ str(file_path).replace("/", "_q_").strip(".py") + ".json"
                with open(json_file_name, "w") as f:
                    json.dump(file_info, f, indent=4)
    api_definition_files = find_api_definition_files(directory_path)
    all_endpoints_dict = dict()
    for file in api_definition_files:
        all_endpoints = []
        py_file = Path(file)
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source)
        set_parents(tree)
        eps = find_api_endpoints(py_file)
        if eps:
            all_endpoints.extend(eps)
            all_endpoints_dict[file] = all_endpoints
    endpoint_jobs = []
    for value in all_endpoints_dict.values():
        for item in value:
            if item.get('type') == 'class':
                endpoint_jobs.extend(item.get('methods', []))
            else:
                endpoint_jobs.append(item)
    api_index = _build_api_index(directory_path, endpoint_jobs)
    _write_api_index(directory_path, api_index)
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
        json_file = str(file_name).replace("/", "_q_").strip(".py") + ".json"
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
    json_file = str(file_name).replace("/", "_q_").strip(".py") + ".json"
    complete_json_file_path = json_dir_path + "/" + json_file
    with open(complete_json_file_path, "r") as f:
        data = json.load(f)
    in_file_dependency_functions, imported_functions = get_dependencies(data, method_info["start_line"], method_info["end_line"], method_info['file_path'])
    context_code_blocks = get_code_blocks(in_file_dependency_functions, imported_functions, file_name, directory_path)
    return context_code_blocks, method_definition_code_block

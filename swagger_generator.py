from llm_client import OpenAiClient
from config import Configurations
import prompts
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
import time
import os, re
import datetime
from utils import get_git_commit_hash, get_github_repo_url, get_repo_path, get_repo_name, format_repo_name

config = Configurations()

# Keys of an OpenAPI path item that hold an operation body.
HTTP_VERB_KEYS = {"get", "post", "put", "delete", "patch", "options", "head", "trace"}

# Operation keys older prompts asked for, mapped to their OpenAPI 3.0 compliant form.
LEGACY_OPERATION_FIELDS = {
    "api_description": "description",
    "authorization_tag": "x-authorization-tag",
    "module_tag": "x-module-tag",
    "auth_tag": "x-auth-tag",
    "sensitive_information": "x-sensitive-information",
}


class SwaggerGeneration:
    def __init__(self):
        self.openai_client = OpenAiClient()


    def create_swagger_json(self, endpoints, authentication_information, framework, api_host):
        repo_path = get_repo_path()
        repo_name = get_repo_name()
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
                    "url": api_host
                }
            ],
            "paths": {}
        }
        print("\n***************************************************")
        print(f"\nstarted generating swagger for {len(endpoints)} endpoints")
        start_time = time.time()
        completed = 0

        def process_endpoint(endpoint):
            return self.generate_endpoint_swagger(endpoint, authentication_information, framework)

        with ThreadPoolExecutor(max_workers=8) as executor:
            future_to_endpoint = {executor.submit(process_endpoint, endpoint): endpoint
                                  for endpoint in endpoints}

            for future in as_completed(future_to_endpoint):
                endpoint = future_to_endpoint[future]
                path = endpoint.get("path", "")
                method = str(endpoint.get("method", "")).lower()
                try:
                    endpoint_swagger = future.result()
                    operation = self._extract_first_operation(endpoint_swagger)
                    if operation is None:
                        raise ValueError("no usable operation in the generated fragment")

                    if path not in swagger["paths"]:
                        swagger["paths"][path] = {}
                    swagger["paths"][path][method] = operation

                    completed += 1
                    end_time = time.time()
                    print(f"completed generating swagger for {completed} endpoints in {int(end_time - start_time)} seconds",
                          end="\r")
                except Exception as ex:
                    print(f"\nskipped {method.upper()} {path}: {ex}")

        total = len(endpoints)
        failed = total - completed
        print(f"\ngenerated {completed} of {total} endpoints ({failed} failed)")
        if total > 0 and completed == 0:
            raise RuntimeError("Swagger generation failed for every endpoint.")
        return swagger

    @staticmethod
    def _normalize_operation_fields(operation):
        """
        Rename the legacy operation keys an older model reply may still carry to
        their OpenAPI compliant form. A value already under the new name wins, so
        a reply holding both does not end up with duplicated content.
        """
        if not isinstance(operation, dict):
            return operation
        for legacy_key, new_key in LEGACY_OPERATION_FIELDS.items():
            if legacy_key not in operation:
                continue
            operation.setdefault(new_key, operation.pop(legacy_key))
        return operation

    @staticmethod
    def _extract_first_operation(endpoint_swagger):
        """
        Pull the first operation body out of a model generated fragment.

        The model's own path and method keys are ignored because it often rewrites them,
        the caller re-keys the operation under the endpoint we actually asked about.

        Returns:
            The operation dict, or None if the fragment is unusable.
        """
        if not isinstance(endpoint_swagger, dict):
            return None
        paths = endpoint_swagger.get("paths")
        if not isinstance(paths, dict) or not paths:
            return None
        for methods in paths.values():
            if not isinstance(methods, dict):
                continue
            # Only an HTTP verb key counts: a path item may legally carry
            # `parameters` or vendor extensions, which are not operations.
            for name, operation in methods.items():
                if str(name).lower() in HTTP_VERB_KEYS and isinstance(operation, dict):
                    return SwaggerGeneration._normalize_operation_fields(operation)
        return None



    def generate_endpoint_swagger(self, endpoint, authentication_information, framework):
        if framework == "ruby_on_rails":
            prompt = prompts.ruby_on_rails_swagger_generation_prompt.format(endpoint_info = endpoint['info'], endpoint_method = endpoint['method'], endpoint_path = endpoint['path'],
                                                                            authentication_information = authentication_information)
        else:
            prompt = prompts.generic_swagger_generation_prompt.format(endpoint_info = endpoint['info'], endpoint_method = endpoint['method'], endpoint_path = endpoint['path'],
                                                                            authentication_information = authentication_information)
        messages = [
            {"role": "system", "content": prompts.swagger_generation_system_prompt},
            {"role": "user", "content": prompt}
        ]
        response_content = self._call_chat_completion_with_retry(messages)
        try:
            start_index = response_content.find('{')
            end_index = response_content.rfind('}')
            swagger_json_block = response_content[start_index:end_index + 1]
            return json.loads(swagger_json_block)
        except Exception as ex:
            return {"paths": {endpoint['path']: {}}}


    def _call_chat_completion_with_retry(self, messages, retries=2):
        """
        Call the model, retrying transient API failures (rate limits, timeouts, 5xx).
        A malformed response body is not retried here, the caller parses it.
        """
        delays = [1, 4]
        for attempt in range(retries + 1):
            try:
                return self.openai_client.call_chat_completion(messages=messages)
            except Exception:
                if attempt == retries:
                    raise
                time.sleep(delays[attempt])


    @staticmethod
    def save_swagger_json(swagger, filename):
        """
        Saves the Swagger JSON to a file.

        Args:
            swagger (dict): The Swagger JSON dictionary.
            filename (str): The output file name.
        """
        swagger = SwaggerGeneration._sanitize_swagger(swagger)
        # Create directory if it doesn't exist
        directory = os.path.dirname(filename)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(filename, 'w', encoding='utf-8') as file:
            json.dump(swagger, file, indent=2)
        # Display relative path (remove /workspace prefix if present)
        display_path = filename
        if filename.startswith('/workspace/'):
            display_path = filename[len('/workspace/'):]
            if not display_path.startswith('./'):
                display_path = './' + display_path
        print(f"Swagger JSON saved to {display_path}.")
        # Generate HTML viewer file in the same directory
        if os.environ.get("APIMESH_SKIP_HTML", "").lower() in ("1", "true"):
            print("HTML viewer skipped because APIMESH_SKIP_HTML is set.")
            return
        SwaggerGeneration.generate_html_viewer(filename)

    @staticmethod
    def generate_html_viewer(swagger_json_path):
        """
        Generates an HTML viewer file in the same directory as the swagger.json file.
        Embeds the swagger.json data directly into the HTML to avoid CORS issues.

        Args:
            swagger_json_path (str): Path to the swagger.json file.
        """
        try:
            # Get the directory of the swagger.json file
            swagger_dir = os.path.dirname(swagger_json_path)
            if not swagger_dir:
                swagger_dir = '.'
            
            # Path to the HTML viewer template
            html_template_path = os.path.join(os.path.dirname(__file__), 'apimesh-docs.html')
            html_output_path = os.path.join(swagger_dir, 'apimesh-docs.html')
            
            # Read the swagger.json file
            swagger_data = None
            if os.path.exists(swagger_json_path):
                with open(swagger_json_path, 'r', encoding='utf-8') as f:
                    swagger_data = json.load(f)
            
            # Read the HTML template
            if os.path.exists(html_template_path):
                with open(html_template_path, 'r', encoding='utf-8') as f:
                    html_content = f.read()
                
                # Replace <repo_name> placeholder with formatted repo name from utils
                repo_name = get_repo_name()
                formatted_repo_name = html.escape(format_repo_name(repo_name))
                html_content = html_content.replace('<repo_name>', formatted_repo_name)
                
                # Embed the swagger data as a JavaScript variable
                if swagger_data:
                    # Escape the JSON for embedding in JavaScript
                    swagger_json_str = json.dumps(swagger_data, indent=2)
                    swagger_json_str = re.sub(r'</(script)', r'<\/\1', swagger_json_str, flags=re.IGNORECASE)
                    # Replace the placeholder or add the embedded data before the closing script tag
                    # We'll add it right after the script tag opens
                    embedded_data_script = f'''
        // Embedded Swagger data (to avoid CORS issues)
        const EMBEDDED_SWAGGER_DATA = {swagger_json_str};
'''
                    # Find the script tag and insert the embedded data right after it
                    script_start = html_content.find('<script>')
                    if script_start != -1:
                        insert_pos = script_start + len('<script>')
                        html_content = html_content[:insert_pos] + embedded_data_script + html_content[insert_pos:]
                
                # Write the modified HTML to the output directory
                with open(html_output_path, 'w', encoding='utf-8') as f:
                    f.write(html_content)
                
                # Display relative path (remove /workspace prefix if present)
                display_path = html_output_path
                if html_output_path.startswith('/workspace/'):
                    display_path = html_output_path[len('/workspace/'):]
                    if not display_path.startswith('./'):
                        display_path = './' + display_path
                
                # Print formatted success message
                print("\n==========================================")
                print("Swagger HTML Viewer Generated Successfully")
                print("==========================================\n")
                print("The HTML viewer has been generated at:")
                print(f"  Relative path:  {display_path} (in your mounted volume)\n")
                print("To view it:")
                print("  1. The file is in your mounted volume directory")
                print("  2. Open it directly in your browser from your host machine:")
                print("     Navigate to your repository directory and open:")
                print(f"     {display_path}\n")
                print("==========================================")
                
                return display_path
            else:
                print(f"Warning: HTML template not found at {html_template_path}")
                return None
        except Exception as ex:
            print(f"Warning: Could not generate HTML viewer: {ex}")
            return None

    @staticmethod
    def _sanitize_swagger(swagger: dict) -> dict:
        """
        Apply lightweight, framework-agnostic cleanup:
        - normalize Express-style segments (:param -> {param})
        - merge duplicate paths created by differing param syntax
        - drop wildcard /* or * paths that often come from generic middleware
        """
        paths = swagger.get("paths", {})
        if not isinstance(paths, dict):
            return swagger

        def normalize(path: str) -> str:
            # Flask/Django-style <converter:param> converts first, or the colon
            # rule below would eat the converter's colon and mangle the name.
            path = re.sub(r"<(?:[^<>:]+:)?([^<>]+)>", r"{\1}", path)
            return re.sub(r":([A-Za-z_][\w-]*)", r"{\1}", path)

        # Drop wildcard paths
        for wildcard in ("/*", "*"):
            paths.pop(wildcard, None)

        # Re-key normalized paths
        for original in list(paths.keys()):
            normalized = normalize(original)
            if normalized == original:
                continue
            methods = paths.pop(original)
            if normalized not in paths:
                paths[normalized] = methods
            else:
                # merge methods, favor normalized version
                paths[normalized].update(methods)

        swagger["paths"] = paths
        return swagger

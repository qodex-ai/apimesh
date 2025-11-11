from llm_client import OpenAiClient
from config import Configurations
import prompts
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import time

config = Configurations()

class SwaggerGeneration:
    def __init__(self):
        self.openai_client = OpenAiClient()


    def create_swagger_json(self, repo_name, endpoints, authentication_information, framework, api_host):
        swagger = {
            "openapi": "3.0.0",
            "info": {
                "title": repo_name,
                "version": "1.0.0",
                "description": "This Swagger file was generated using OpenAI GPT."
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
            endpoint_swagger = self.generate_endpoint_swagger(endpoint, authentication_information, framework)
            return endpoint["path"], endpoint["method"].lower(), endpoint_swagger

        with ThreadPoolExecutor(max_workers=8) as executor:
            future_to_endpoint = {executor.submit(process_endpoint, endpoint): endpoint
                                  for endpoint in endpoints}

            for future in as_completed(future_to_endpoint):
                path, method, endpoint_swagger = future.result()

                if path not in swagger["paths"]:
                    swagger["paths"][path] = {}

                key = list(endpoint_swagger['paths'].keys())[0]
                _method_list = list(endpoint_swagger['paths'][key].keys())
                if not _method_list:
                    continue
                _method = _method_list[0]
                swagger["paths"][path][_method] = endpoint_swagger['paths'][key][_method]

                completed += 1
                end_time = time.time()
                print(f"completed generating swagger for {completed} endpoints in {int(end_time - start_time)} seconds",
                      end="\r")
        return swagger



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
        response_content = self.openai_client.call_chat_completion(messages=messages)
        try:
            start_index = response_content.find('{')
            end_index = response_content.rfind('}')
            swagger_json_block = response_content[start_index:end_index + 1]
            return json.loads(swagger_json_block)
        except Exception as ex:
            return {"paths": {endpoint['path']: {}}}


    @staticmethod
    def save_swagger_json(swagger, filename):
        """
        Saves the Swagger JSON to a file.

        Args:
            swagger (dict): The Swagger JSON dictionary.
            filename (str): The output file name.
        """
        with open(filename, 'w', encoding='utf-8') as file:
            json.dump(swagger, file, indent=2)
        print(f"Swagger JSON saved to {filename}.")


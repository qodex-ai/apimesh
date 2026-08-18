import ast
import json
from llm_client import OpenAiClient
from config import Configurations
import prompts
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

config = Configurations()

class EndpointsExtractor:
    def __init__(self):
        self.openai_client = OpenAiClient()

    def extract_endpoints_with_gpt(self, file_path, framework):
        print("\n***************************************************")
        print(f"Started finding endpoints for {file_path}")
        with open(file_path, 'r', encoding='utf-8') as file:
            file_content = file.read()
        if framework == "ruby_on_rails":
            content = prompts.ruby_on_rails_endpoint_extractor_prompt.format(file_content = file_content)
            messages = [
                {"role": "system", "content": prompts.ruby_on_rails_endpoint_extractor_system_prompt},
                {"role": "user", "content": content}
            ]
        elif framework == "express":
            content = prompts.express_endpoint_extractor_prompt.format(file_content = file_content)
            messages = [
                {"role": "system", "content": prompts.express_endpoint_extractor_system_prompt},
                {"role": "user", "content": content}
            ]
        elif framework == "django":
            content = prompts.django_endpoint_extractor_prompt.format(file_content = file_content)
            messages = [
                {"role": "system", "content": prompts.django_endpoint_extractor_system_prompt},
                {"role": "user", "content": content}
            ]

        elif framework == "flask":
            content = prompts.flask_endpoint_extractor_prompt.format(file_content = file_content)
            messages = [
                {"role": "system", "content": prompts.flask_endpoint_extractor_system_prompt},
                {"role": "user", "content": content}
            ]
        elif framework == "fastapi":
            content = prompts.fastapi_endpoint_extractor_prompt.format(file_content = file_content)
            messages = [
                {"role": "system", "content": prompts.fastapi_endpoint_extractor_system_prompt},
                {"role": "user", "content": content}
            ]

        elif framework == "golang":
            content = prompts.golang_endpoint_extractor_prompt.format(file_content=file_content)
            messages = [
                {"role": "system", "content": prompts.golang_endpoint_extractor_system_prompt},
                {"role": "user", "content": content}
            ]
        else:
            # Frameworks without a dedicated prompt (spring, laravel, nestjs fallback, ...)
            # go through a framework-agnostic extractor instead of crashing.
            content = prompts.generic_endpoint_extractor_prompt.format(file_content=file_content)
            messages = [
                {"role": "system", "content": prompts.generic_endpoint_extractor_system_prompt},
                {"role": "user", "content": content}
            ]
        # Call the OpenAI API
        response = self.openai_client.call_chat_completion(messages=messages, temperature=0)
        start = response.find('[')
        end = response.rfind(']') + 1
        json_like_string = response[start:end]

        # json.loads first: the prompts demand JSON, and literal_eval rejects
        # JSON-only tokens (true/false/null). literal_eval stays as a fallback
        # for python-flavored output (single quotes).
        try:
            parsed_list = json.loads(json_like_string)
        except (ValueError, TypeError):
            try:
                parsed_list = ast.literal_eval(json_like_string)
            except (ValueError, SyntaxError):
                print("Error parsing JSON-like string from GPT response")
                parsed_list = []
        if not isinstance(parsed_list, list):
            print("Unexpected non-list endpoint payload from GPT response")
            parsed_list = []
        # The model occasionally emits null or malformed entries; only dicts
        # with a string method and path survive, everything downstream
        # subscripts both keys.
        parsed_list = [
            entry for entry in parsed_list
            if isinstance(entry, dict)
            and isinstance(entry.get("method"), str)
            and isinstance(entry.get("path"), str)
        ]

        print(f"Completed finding endpoints for {file_path}")
        return parsed_list

    @staticmethod
    def get_endpoint_related_information(faiss_vector_db, endpoints):
        print("\n***************************************************")
        print(f"Started generating endpoint related information for {len(endpoints)} endpoints")
        start_time = time.time()
        completed = 0

        def process_endpoint(endpoint):
            query = f"This is the Method: {endpoint['method']} and this is the Endpoint Path: {endpoint['path']} fetch the controller information for the endpoint."
            docs = faiss_vector_db.similarity_search(str(query), k=4)
            content_list = [doc.page_content.strip() for doc in docs]
            return {'method': endpoint['method'], 'path': endpoint['path'], 'info': content_list}

        endpoint_related_content = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            future_to_endpoint = {executor.submit(process_endpoint, endpoint): endpoint
                                  for endpoint in endpoints}

            for future in as_completed(future_to_endpoint):
                endpoint_related_content.append(future.result())
                completed += 1
                end_time = time.time()
                print(
                    f"Completed generating endpoint related information for {completed} endpoints in {int(end_time - start_time)} seconds",
                    end="\r")

        return endpoint_related_content

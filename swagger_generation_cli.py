import traceback
import os

from user_config import UserConfigurations
from swagger_generator import SwaggerGeneration
from file_scanner import FileScanner
from framework_identifier import FrameworkIdentifier
from endpoints_extractor import EndpointsExtractor
from faiss_index_generator import GenerateFaissIndex
from nodejs_pipeline.run_swagger_generation import run_swagger_generation as nodejs_swagger_generator
from python_pipeline.run_swagger_generation import run_swagger_generation as python_swagger_generator
from rails_pipeline.run_swagger_generation import run_swagger_generation as ruby_on_rails_swagger_generator
from golang_pipeline.run_swagger_generation import run_swagger_generation as golang_swagger_generator
from utils import get_output_filepath
import requests, json
import sys

class RunSwagger:
    def __init__(self, project_api_key, openai_api_key, ai_chat_id, is_mcp):
        self.ai_chat_id = ai_chat_id
        self.user_configurations = UserConfigurations(project_api_key, openai_api_key, ai_chat_id, is_mcp)
        self.user_config = self.user_configurations.load_user_config()
        self.framework_identifier = FrameworkIdentifier()
        self.file_scanner = FileScanner()
        self.endpoints_extractor = EndpointsExtractor()
        self.faiss_index = GenerateFaissIndex()
        self.swagger_generator = SwaggerGeneration()


    def run_python_nodejs_ruby(self, framework):
        swagger = None
        try:
            if framework == "django" or framework == "flask" or framework == "fastapi":
                swagger = python_swagger_generator(self.user_config['api_host'])
            elif framework == "express":
                swagger = nodejs_swagger_generator(self.user_config['api_host'])
            elif framework == "ruby_on_rails":
                swagger = ruby_on_rails_swagger_generator(self.user_config['api_host'])
            elif framework == "golang":
                swagger = golang_swagger_generator(self.user_config['api_host'])
        except Exception as ex:
            traceback.print_exc()
            print("Fallback to old procedure")
        return swagger

    def _resolve_ai_chat_id(self, ai_chat_id):
        candidate = (ai_chat_id or "").strip()
        if candidate and candidate.lower() != "null":
            return candidate
        return self.user_config.get("ai_chat_id", "null")

    def run(self, ai_chat_id=None):
        resolved_ai_chat_id = self._resolve_ai_chat_id(ai_chat_id if ai_chat_id is not None else self.ai_chat_id)
        try:
            file_paths = self.file_scanner.get_all_file_paths()
            print("\n***************************************************")
            if self.user_config.get('framework', None):
                print(f"Using Existing Framework - {self.user_config['framework']}")
                framework =  self.user_config.get('framework', "")
            else:
                print("Started framework identification")
                framework = self.framework_identifier.get_framework(file_paths)['framework']
                self.user_config['framework'] = framework
                self.user_configurations.save_user_config(self.user_config)
        except Exception as ex:
            msg = str(ex)
            lowered = msg.lower()
            if "insufficient_quota" in lowered or "quota" in lowered:
                print("OpenAI quota exceeded. Please check your plan/billing and retry after adding credits.")
            else:
                print("We do not support this framework currently. Please contact QodexAI support.")
            exit()
        print(f"completed framework identification - {framework}")
        print("\n***************************************************")
        print("Started finding files related to API information")
        try:
            swagger = self.run_python_nodejs_ruby(framework)
            if swagger:
                output_filepath = get_output_filepath()
                self.swagger_generator.save_swagger_json(swagger, output_filepath)
                #self.upload_swagger_to_qodex(resolved_ai_chat_id)
                exit()
            api_files = self.file_scanner.find_api_files(file_paths, framework)
            print("Completed finding files related to API information")
            all_endpoints = []
            for filePath in api_files:
                endpoints = self.endpoints_extractor.extract_endpoints_with_gpt(filePath, framework)
                all_endpoints.extend(endpoints)
            print("\n***************************************************")
            print("Started creating faiss index for all files")
            faiss_vector = self.faiss_index.create_faiss_index(file_paths, framework)
            print("Completed creating faiss index for all files")
            print("Fetching authentication related information")
            authentication_information = self.faiss_index.get_authentication_related_information(faiss_vector)
            print("Completed Fetching authentication related information")
            endpoint_related_information = self.endpoints_extractor.get_endpoint_related_information(faiss_vector, all_endpoints)
            swagger = self.swagger_generator.create_swagger_json(endpoint_related_information, authentication_information, framework, self.user_config['api_host'])
        except Exception as ex:
            traceback.print_exc()
            print("Oops! looks like we encountered an issue. Please try after some time.")
            exit()
        try:
            output_filepath = get_output_filepath()
            self.swagger_generator.save_swagger_json(swagger, output_filepath)
        except Exception as ex:
            print("Swagger was not able to be saved. Please check your project api key and try again.")
        #self.upload_swagger_to_qodex(resolved_ai_chat_id)
        return


    def upload_swagger_to_qodex(self, ai_chat_id):
        qodex_api_key = self.user_config['qodex_api_key']
        if qodex_api_key:
            print("Uploading swagger to Qodex.AI")
            url = "https://api.app.qodex.ai/api/v1/collection_imports/create_with_json"
            output_filepath = get_output_filepath()
            with open(output_filepath, "r") as file:
                swagger_doc = json.load(file)
            payload = {
                "api_key": qodex_api_key,
                "swagger_doc": swagger_doc,
                "ai_chat_id": ai_chat_id
            }
            response = requests.post(url, json=payload)

            # Check the response
            if response.status_code == 200 or response.status_code == 201:
                print("Success:", response.json())  # Or response.text for plain text responses
                print("Swagger successfully uploaded to Qodex AI. Please refresh your tab.")
                print("We highly recommend you to review the apis before generating test scenarios.")
                if str(ai_chat_id) != 'null':
                    print("Open the following link in your browser or refresh the existing open page to continue further")
                    print(f"https://app.qodex.ai/ai-agent?chatId={ai_chat_id}")
            else:
                print(f"Failed with status code {response.status_code}: {response.text}")
        return


openai_api_key = sys.argv[1] if len(sys.argv) > 1 else ""
project_api_key = sys.argv[2] if len(sys.argv) > 2 else ""
ai_chat_id = sys.argv[3] if len(sys.argv) > 3 else ""
is_mcp = sys.argv[4] if len(sys.argv) > 4 else False

RunSwagger(project_api_key, openai_api_key, ai_chat_id, is_mcp).run(ai_chat_id)

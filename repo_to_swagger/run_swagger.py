from repo_to_swagger.user_config import UserConfigurations
from repo_to_swagger.swagger_generator import SwaggerGeneration
from repo_to_swagger.file_scanner import FileScanner
from repo_to_swagger.framework_identifier import FrameworkIdentifier
from repo_to_swagger.endpoints_extractor import EndpointsExtractor
from repo_to_swagger.faiss_index_generator import GenerateFaissIndex
import requests, json
import sys

class RunSwagger:
    def __init__(self, project_api_key, openai_api_key):
        self.user_config = UserConfigurations(project_api_key, openai_api_key).load_user_config()
        self.framework_identifier = FrameworkIdentifier()
        self.file_scanner = FileScanner()
        self.endpoints_extractor = EndpointsExtractor()
        self.faiss_index = GenerateFaissIndex()
        self.swagger_generator = SwaggerGeneration()

    def run(self, ai_chat_id):
        try:
            file_paths = self.file_scanner.get_all_file_paths(self.user_config['repo_path'])
            print("\n***************************************************")
            print("Started framework identification")
            framework = self.framework_identifier.get_framework(file_paths)['framework']
        except Exception as ex:
            print("We do not support this framework currently. Please contact QodexAI support.")
            exit()
        print(f"completed framework identification - {framework}")
        print("\n***************************************************")
        print("Started finding files related to API information")
        try:
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
            swagger = self.swagger_generator.create_swagger_json(self.user_config['repo_name'],endpoint_related_information, authentication_information, framework, self.user_config['api_host'])
        except Exception as ex:
            print("Oops! looks like we encountered an issue. Please try after some time.")
            exit()
        try:
            self.swagger_generator.save_swagger_json(swagger, self.user_config['output_filepath'])
        except Exception as ex:
            print("Swagger was not able to be uploaded to server. Please check your project api key and try again.")
        print("Swagger Generated Successfully")
        self.upload_swagger_to_qodex(ai_chat_id)
        return


    def upload_swagger_to_qodex(self, ai_chat_id):
        qodex_api_key = self.user_config['qodex_api_key']
        if qodex_api_key:
            print("Uploading swagger to Qodex.AI")
            url = "https://api.app.qodex.ai/api/v1/collection_imports/create_with_json"
            with open(self.user_config['output_filepath'], "r") as file:
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
                if str(ai_chat_id) != 'null'
                    print(f"Open the following link in your browser or refresh the existing open page to continue further")
                    print(f"https://www.staging.app.qodex.ai/ai-agent?chatId={ai_chat_id}")
            else:
                print(f"Failed with status code {response.status_code}: {response.text}")
        return

project_api_key = sys.argv[1]
openai_api_key = sys.argv[2]
ai_chat_id = sys.argv[3]

RunSwagger(project_api_key, openai_api_key).run(ai_chat_id)
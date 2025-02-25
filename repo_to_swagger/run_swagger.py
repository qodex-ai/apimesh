from repo_to_swagger.user_config import UserConfigurations
from repo_to_swagger.swagger_generator import SwaggerGeneration
from repo_to_swagger.file_scanner import FileScanner
from repo_to_swagger.framework_identifier import FrameworkIdentifier
from repo_to_swagger.endpoints_extractor import EndpointsExtractor
from repo_to_swagger.faiss_index_generator import GenerateFaissIndex
import requests, json


user_configurations = UserConfigurations()
swagger_generator = SwaggerGeneration()
file_scanner = FileScanner()
framework_identifier = FrameworkIdentifier()
endpoints_extractor = EndpointsExtractor()
faiss_index = GenerateFaissIndex()

class RunSwagger:
    def __init__(self):
        self.user_config = UserConfigurations.load_user_config()

    def run(self):
        file_paths = file_scanner.get_all_file_paths(self.user_config['repo_path'])
        print("\n***************************************************")
        print("Started framework identification")
        framework = framework_identifier.get_framework(file_paths)['framework']
        print(f"completed framework identification - {framework}")
        print("\n***************************************************")
        print("Started finding files related to API information")
        api_files = file_scanner.find_api_files(file_paths, framework)
        print("Completed finding files related to API information")
        all_endpoints = []
        for filePath in api_files:
            endpoints = endpoints_extractor.extract_endpoints_with_gpt(filePath, framework)
            all_endpoints.extend(endpoints)
        print("\n***************************************************")
        print("Started creating faiss index for all files")
        faiss_vector = faiss_index.create_faiss_index(file_paths, framework)
        print("Completed creating faiss index for all files")
        print("Fetching authentication related information")
        authentication_information = faiss_index.get_authentication_related_information(faiss_vector)
        print("Completed Fetching authentication related information")
        endpoint_related_information = endpoints_extractor.get_endpoint_related_information(faiss_vector, all_endpoints)
        swagger = swagger_generator.create_swagger_json(endpoint_related_information, authentication_information, framework, self.user_config['api_host'])
        swagger_generator.save_swagger_json(swagger, self.user_config['output_filepath'])
        print("Swagger Generated Successfully")
        self.upload_swagger_to_qodex()
        return


    def upload_swagger_to_qodex(self):
        qodex_api_key = self.user_config['qodex_api_key']
        if qodex_api_key:
            print("Uploading swagger to Qodex.AI")
            url = "https://api.app.qodex.ai/api/v1/collection_imports/create_with_json"
            with open(self.user_config['output_filepath'], "r") as file:
                swagger_doc = json.load(file)
            payload = {
                "api_key": qodex_api_key,
                "swagger_doc": swagger_doc
            }
            response = requests.post(url, json=payload)

            # Check the response
            if response.status_code == 200 or response.status_code == 201:
                print("Success:", response.json())  # Or response.text for plain text responses
            else:
                print(f"Failed with status code {response.status_code}: {response.text}")
        return

RunSwagger().run()



import os, json
from repo_to_swagger.config import Configurations

configurations = Configurations()
config_dir = configurations.user_config_file_dir
config_file = os.path.join(config_dir, "config.json")
os.makedirs(config_dir, exist_ok=True)

class UserConfigurations:
    def __init__(self):
        self.add_user_configs()

    @staticmethod
    def load_user_config():
        if os.path.exists(config_file):
            with open(config_file, "r") as file:
                return json.load(file)
        return {}
    @staticmethod
    def save_user_config(config):
        with open(config_file, "w") as file:
            json.dump(config, file, indent=4)

    def add_user_configs(self):
        user_config = self.load_user_config()
        print("***************************************************")
        default_repo_path = user_config.get("repo_path", os.getcwd())
        repo_path = input(
            f"Please enter the project repository path (default: {default_repo_path}): ") or default_repo_path
        user_config["repo_path"] = repo_path
        self.save_user_config(user_config)
        # Check if the user entered something
        if not repo_path.strip():
            print("No path provided. Exiting...")
            exit(1)
        # Optionally check if the path exists
        if os.path.isdir(repo_path):
            print("The directory exists.")
        else:
            print("The directory does not exist.")
            exit(1)
        print("***************************************************")
        default_output_filepath = user_config.get("output_filepath", f"{os.getcwd()}/swagger.json")
        output_filepath = input(
            f"Please enter the output file path (default: {default_output_filepath}): ") or default_output_filepath
        user_config["output_filepath"] = output_filepath
        self.save_user_config(user_config)

        print("***************************************************")
        default_openai_api_key = user_config.get("openai_api_key")
        openai_api_key = input(
            f"Please enter openai api key (default: {default_openai_api_key}): ") or default_openai_api_key
        user_config["openai_api_key"] = openai_api_key
        self.save_user_config(user_config)

        print("***************************************************")
        default_openai_model = user_config.get("openai_model", "gpt-4o")
        openai_model = input(
            f"Please enter openai api model (default: {default_openai_model}): choices: gpt-4o/gpt-4o-mini ") or default_openai_model
        user_config["openai_model"] = openai_model
        self.save_user_config(user_config)

        print("***************************************************")
        default_api_host = user_config.get("api_host", "")
        api_host = input(f"Please enter api host (default: {default_api_host}): ") or default_api_host
        user_config["api_host"] = api_host
        self.save_user_config(user_config)
        # Check if the user entered something
        if not api_host.strip():
            print("No api host provided. Exiting...")
            exit(1)

        print("***************************************************")
        default_qodex_api_key = user_config.get("qodex_api_key")
        qodex_api_key = input(
            f"Please enter qodex api key (default: {default_qodex_api_key}) (press enter to skip this): ") or default_qodex_api_key
        user_config["qodex_api_key"] = qodex_api_key
        self.save_user_config(user_config)

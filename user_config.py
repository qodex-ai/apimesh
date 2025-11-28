"""
Utility helpers for capturing and persisting user-specific configuration
for the Swagger Generator CLI.
"""

import os, json
from config import Configurations
from utils import get_repo_name, get_repo_path
configurations = Configurations()

# Get JSON config file path from environment variable
config_file = os.environ.get("APIMESH_USER_CONFIG_PATH")
if config_file is None:
    raise ValueError(
        "APIMESH_USER_CONFIG_PATH environment variable is not set. "
        "Please set it to the path of your config.json file."
    )

# Ensure the directory exists
config_dir = os.path.dirname(config_file)
os.makedirs(config_dir, exist_ok=True)

class UserConfigurations:
    def __init__(self, project_api_key, openai_api_key, ai_chat_id, is_mcp):
        self.is_mcp = is_mcp
        self.ai_chat_id = ai_chat_id
        self.add_user_configs(project_api_key, openai_api_key)

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

    @staticmethod
    def _sanitize_cli_value(value):
        if value is None:
            return ""
        if isinstance(value, str):
            cleaned_value = value.strip()
        else:
            cleaned_value = str(value).strip()
        return cleaned_value if cleaned_value and cleaned_value.lower() != "null" else ""

    @staticmethod
    def _print_section_header(title):
        line = "=" * max(len(title) + 10, 50)
        print(f"\n{line}\n{title}\n{line}")

    def add_user_configs(self, project_api_key, openai_api_key):
        user_config = self.load_user_config()
        self._print_section_header("OpenAI Credentials")
        stored_openai_api_key = user_config.get("openai_api_key", "")
        sanitized_openai_api_key = self._sanitize_cli_value(openai_api_key)
        if sanitized_openai_api_key:
            resolved_openai_api_key = sanitized_openai_api_key
        elif not stored_openai_api_key and not self.is_mcp:
            resolved_openai_api_key = input(
                f"Please enter openai api key (default: {stored_openai_api_key}): ") or stored_openai_api_key
        else:
            resolved_openai_api_key = stored_openai_api_key
        user_config["openai_api_key"] = resolved_openai_api_key
        self.save_user_config(user_config)
        print(f"  ✓ API Key: {resolved_openai_api_key}")

        self._print_section_header("Model Selection")
        default_openai_model = user_config.get("openai_model", "gpt-5.1-codex")
        openai_model = default_openai_model
        user_config["openai_model"] = openai_model
        self.save_user_config(user_config)
        print(f"  ✓ AI Model: {openai_model}")

        self._print_section_header("API Host Configuration")
        default_api_host = user_config.get("api_host", "https://api.example.com")
        api_host = default_api_host
        user_config["api_host"] = api_host
        self.save_user_config(user_config)
        print(f"  ✓ API Host: {api_host}")
        # Check if the user entered something
        if not api_host.strip():
            print("  ✗ No api host provided. Exiting...")
            exit(1)

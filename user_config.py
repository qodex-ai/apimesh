"""
Utility helpers for capturing and persisting user-specific configuration
for the Swagger Generator CLI.
"""

import os, json, sys
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
    PLACEHOLDER_API_HOST = "https://api.example.com"
    DEFAULT_OPENAI_MODEL = "gpt-5.6-terra"

    def __init__(self, project_api_key, openai_api_key, ai_chat_id, is_mcp, api_host=None, openai_model=None):
        self.is_mcp = is_mcp
        self.ai_chat_id = ai_chat_id
        self.api_host_override = api_host
        self.openai_model_override = openai_model
        self.add_user_configs(openai_api_key)

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
    def _mask_secret(value):
        cleaned_value = (value or "").strip()
        if not cleaned_value:
            return "not set"
        if len(cleaned_value) <= 10:
            return "*" * len(cleaned_value)
        return f"{cleaned_value[:6]}...{cleaned_value[-4:]}"

    @staticmethod
    def _ensure_config_gitignored():
        """
        Keep the stored OpenAI key out of git by gitignoring config.json where it lives.
        """
        entry = os.path.basename(config_file)
        gitignore_path = os.path.join(config_dir or ".", ".gitignore")
        try:
            existing = ""
            if os.path.exists(gitignore_path):
                with open(gitignore_path, "r") as file:
                    existing = file.read()
                if entry in [line.strip() for line in existing.splitlines()]:
                    return gitignore_path
            separator = "" if not existing or existing.endswith("\n") else "\n"
            with open(gitignore_path, "a") as file:
                file.write(f"{separator}{entry}\n")
            return gitignore_path
        except OSError as ex:
            print(f"  ! Could not write {gitignore_path}: {ex}")
            return None

    @staticmethod
    def _print_section_header(title):
        line = "=" * max(len(title) + 10, 50)
        print(f"\n{line}\n{title}\n{line}")

    @staticmethod
    def clear_cached_framework():
        """
        Drop the cached framework so the next run detects it again. Returns True when
        something was actually cleared.
        """
        user_config = UserConfigurations.load_user_config()
        if user_config.pop("framework", None) is None:
            return False
        UserConfigurations.save_user_config(user_config)
        return True

    def _can_prompt(self):
        """Only ask a human a question when there is a human on the other end."""
        if self.is_mcp:
            return False
        try:
            return bool(sys.stdin.isatty())
        except (AttributeError, ValueError):
            return False

    def _resolve_openai_model(self, user_config):
        """--model beats APIMESH_OPENAI_MODEL, which beats the stored value. Never prompted."""
        return (
            self._sanitize_cli_value(self.openai_model_override)
            or self._sanitize_cli_value(os.getenv("APIMESH_OPENAI_MODEL"))
            or self._sanitize_cli_value(user_config.get("openai_model"))
            or self.DEFAULT_OPENAI_MODEL
        )

    def _resolve_api_host(self, user_config):
        """--api-host beats APIMESH_API_HOST, which beats the stored value."""
        api_host = (
            self._sanitize_cli_value(self.api_host_override)
            or self._sanitize_cli_value(os.getenv("APIMESH_API_HOST"))
            or self._sanitize_cli_value(user_config.get("api_host"))
        )
        if api_host and api_host != self.PLACEHOLDER_API_HOST:
            return api_host
        if self._can_prompt():
            try:
                entered = input(f"Please enter your API host [{self.PLACEHOLDER_API_HOST}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                # A tty that cannot actually be read from, fall through to the placeholder.
                print("")
                entered = ""
            if entered:
                return entered
        return self.PLACEHOLDER_API_HOST

    @staticmethod
    def _warn_placeholder_api_host(api_host):
        line = "!" * 50
        print(f"\n{line}")
        print(f"  ! No API host was provided, using the placeholder {api_host}.")
        print("  ! servers[0].url in the generated spec will point at that placeholder.")
        print("  ! Set the real one with --api-host <url> or APIMESH_API_HOST=<url>.")
        print(line)

    def add_user_configs(self, openai_api_key):
        user_config = self.load_user_config()
        self._print_section_header("OpenAI Credentials")
        stored_openai_api_key = self._sanitize_cli_value(user_config.get("openai_api_key", ""))
        sanitized_openai_api_key = (
            self._sanitize_cli_value(openai_api_key)
            or self._sanitize_cli_value(os.getenv("OPENAI_API_KEY"))
        )
        if sanitized_openai_api_key:
            resolved_openai_api_key = sanitized_openai_api_key
        elif not stored_openai_api_key and self._can_prompt():
            try:
                resolved_openai_api_key = input("Please enter your openai api key: ").strip()
            except (EOFError, KeyboardInterrupt):
                resolved_openai_api_key = ""
        else:
            resolved_openai_api_key = stored_openai_api_key
        if not resolved_openai_api_key:
            print("  ✗ No OpenAI API key provided.")
            print("    Set the OPENAI_API_KEY environment variable, pass the key as the first CLI")
            print("    argument (--openai-api-key when using run.sh or docker), or run interactively.")
            sys.exit(1)
        user_config["openai_api_key"] = resolved_openai_api_key
        self.save_user_config(user_config)
        print(f"  ✓ API Key: {self._mask_secret(resolved_openai_api_key)}")
        gitignore_path = self._ensure_config_gitignored()
        if gitignore_path:
            print(f"  ✓ Key stored in {config_file} and gitignored via {gitignore_path}")

        self._print_section_header("Model Selection")
        openai_model = self._resolve_openai_model(user_config)
        user_config["openai_model"] = openai_model
        self.save_user_config(user_config)
        print(f"  ✓ AI Model: {openai_model}")

        self._print_section_header("API Host Configuration")
        api_host = self._resolve_api_host(user_config)
        user_config["api_host"] = api_host
        self.save_user_config(user_config)
        print(f"  ✓ API Host: {api_host}")
        # A placeholder host still produces a usable spec, so warn loudly and carry on.
        if api_host == self.PLACEHOLDER_API_HOST:
            self._warn_placeholder_api_host(api_host)

import os
import yaml


class Configurations:
    def __init__(self, config_path=None):
        # Set default config path if not provided
        if config_path is None:
            config_path = os.path.join(os.getcwd(), "repo_to_swagger/config.yml")

        # Load YAML configurations
        self.config = self._load_config(config_path)

        # Assign values from the YAML file
        self.ignored_dirs = set(self.config.get("ignored_dirs", []))
        self.routing_patters_map = self.config.get("routing_patterns_map", {})
        self.gpt_4o_model_name = self.config.get("gpt_4o_model_name", "gpt-4o")
        self.user_config_file_dir = os.path.join(os.getcwd(), self.config.get("user_config_file_dir", ".qodexai"))

    def _load_config(self, config_path):
        """Loads configuration from a YAML file."""
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Configuration file '{config_path}' not found.")

        with open(config_path, "r", encoding="utf-8") as file:
            return yaml.safe_load(file)



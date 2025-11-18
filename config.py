import os
import yaml


class Configurations:
    def __init__(self):
        # Get config path from environment variable
        config_path = os.environ.get("APIMESH_CONFIG_PATH")
        if config_path is None:
            raise ValueError(
                "APIMESH_CONFIG_PATH environment variable is not set. "
                "Please set it to the path of your config.yml file."
            )
        
        # Load YAML configurations
        self.config = self._load_config(config_path)

        # Assign values from the YAML file
        self.ignored_dirs = set(self.config.get("ignored_dirs", []))
        self.routing_patters_map = self.config.get("routing_patterns_map", {})
        self.gpt_4o_model_name = self.config.get("gpt_4o_model_name", "gpt-4o")

    def _load_config(self, config_path):
        """Loads configuration from a YAML file."""
        with open(config_path, "r", encoding="utf-8") as file:
            config = yaml.safe_load(file)
            return config if config is not None else {}


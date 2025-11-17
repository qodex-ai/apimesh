import os
import yaml


class Configurations:
    def __init__(self, config_path=None):
        # Set default config path if not provided
        if config_path is None:
            config_path = os.path.join(os.getcwd(), "config.yml")

        # Load YAML configurations
        self.config = self._load_config(config_path)

        # Assign values from the YAML file
        self.ignored_dirs = set(self.config.get("ignored_dirs", []))
        self.routing_patters_map = self.config.get("routing_patterns_map", {})
        self.gpt_4o_model_name = self.config.get("gpt_4o_model_name", "gpt-4o")

        config_dir_name = self.config.get("user_config_file_dir", "apimesh")
        workspace_override = os.environ.get("APIMESH_PARENT_DIR")

        if workspace_override:
            self.user_config_file_dir = os.path.abspath(workspace_override)
        else:
            base_dir = "/workspace" if os.path.exists("/workspace") and os.path.isdir("/workspace") else os.getcwd()
            if os.path.isabs(config_dir_name):
                resolved_dir = config_dir_name
            else:
                resolved_dir = os.path.abspath(os.path.join(base_dir, config_dir_name))

            current_dir = os.getcwd()
            current_name = os.path.basename(current_dir.rstrip(os.sep))
            parent_dir = os.path.dirname(current_dir)
            parent_name = os.path.basename(parent_dir.rstrip(os.sep))
            if current_name and current_name == parent_name:
                resolved_dir = parent_dir

            self.user_config_file_dir = resolved_dir

    def _load_config(self, config_path):
        """Loads configuration from a YAML file."""
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Configuration file '{config_path}' not found.")

        with open(config_path, "r", encoding="utf-8") as file:
            return yaml.safe_load(file)


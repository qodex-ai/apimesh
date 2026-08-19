import json
import os
from config import Configurations
from prompts import framework_identifier_prompt, framework_identifier_system_prompt
from llm_client import OpenAiClient
from utils import get_repo_path

# Enough paths to identify any framework; a monorepo's full listing can blow
# the context window and adds nothing past this point.
MAX_PATHS_IN_PROMPT = 800


class FrameworkIdentifier:
    def __init__(self):
        self.config = Configurations()
        self.openai_client = OpenAiClient()

    @staticmethod
    def _paths_for_prompt(file_paths):
        """Repo-relative paths, capped, with a note when the list was cut."""
        repo_path = get_repo_path()
        relative_paths = []
        for file_path in file_paths:
            try:
                relative_paths.append(os.path.relpath(str(file_path), repo_path))
            except ValueError:
                relative_paths.append(str(file_path))
        shown = relative_paths[:MAX_PATHS_IN_PROMPT]
        listing = "\n".join(shown)
        hidden = len(relative_paths) - len(shown)
        if hidden > 0:
            listing += f"\n... and {hidden} more files not listed"
        return listing

    def get_framework(self, file_paths):
        prompt = framework_identifier_prompt.format(
            file_paths=self._paths_for_prompt(file_paths),
            frameworks=str(list(self.config.routing_patterns_map.keys())),
        )
        messages = [
            {"role": "system", "content": framework_identifier_system_prompt},
            {"role": "user", "content": prompt}
        ]
        response_content = self.openai_client.call_chat_completion(messages=messages)
        start_index = response_content.find('{')
        end_index = response_content.rfind('}')
        if start_index == -1 or end_index == -1:
            raise ValueError(
                f"Framework detection returned no JSON object: {response_content[:200]!r}"
            )
        try:
            return json.loads(response_content[start_index:end_index + 1])
        except json.JSONDecodeError as ex:
            raise ValueError(
                f"Framework detection returned unparseable JSON: {ex}"
            ) from ex

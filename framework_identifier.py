import json
from config import Configurations
from prompts import framework_identifier_prompt, framework_identifier_system_prompt
from llm_client import OpenAiClient


class FrameworkIdentifier:
    def __init__(self):
        self.config = Configurations()
        self.openai_client = OpenAiClient()


    def get_framework(self, file_paths):
        prompt = framework_identifier_prompt.format(file_paths = file_paths, frameworks = str(list(self.config.routing_patters_map.keys())))
        messages = [
            {"role": "system", "content": framework_identifier_system_prompt},
            {"role": "user", "content": prompt}
        ]
        response_content = self.openai_client.call_chat_completion(messages=messages)
        start_index = response_content.find('{')
        end_index = response_content.rfind('}')
        swagger_json_block = response_content[start_index:end_index + 1]
        return json.loads(swagger_json_block)

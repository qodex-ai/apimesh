from openai import OpenAI
from langchain_openai import OpenAIEmbeddings
from config import Configurations
import json, os

config = Configurations()

class OpenAiClient:
    def __init__(self):
        self.openai_api_key = self.load_openai_api_key()
        self.client = OpenAI(
            api_key=self.openai_api_key)
        self.embeddings = OpenAIEmbeddings(model="text-embedding-ada-002", openai_api_key=self.openai_api_key)

    def call_chat_completion(self, messages, temperature=0.5):
        model = self.load_openai_model()
        # The Responses API is required for Codex models (chat.completions is unsupported).
        effective_temperature = 1 if model.startswith("gpt-5") else temperature
        response = self.client.responses.create(
            model=model,
            input=messages,
            temperature=effective_temperature,
        )
        return response.output_text

    @staticmethod
    def load_openai_api_key():
        config_file = os.environ.get("APIMESH_USER_CONFIG_PATH")
        if config_file is None:
            raise ValueError(
                "APIMESH_USER_CONFIG_PATH environment variable is not set. "
                "Please set it to the path of your config.json file."
            )
        with open(config_file, "r") as file:
            user_config_data = json.load(file)
        return user_config_data['openai_api_key']

    def load_openai_model(self):
        config_file = os.environ.get("APIMESH_USER_CONFIG_PATH")
        if config_file is None:
            raise ValueError(
                "APIMESH_USER_CONFIG_PATH environment variable is not set. "
                "Please set it to the path of your config.json file."
            )
        with open(config_file, "r") as file:
            user_config_data = json.load(file)
        return user_config_data['openai_model']

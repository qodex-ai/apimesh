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
        if model.startswith("gpt-5"):
            response = self.client.chat.completions.create(model=model, messages=messages, temperature=1)
        else:
            response = self.client.chat.completions.create(model=model, messages=messages, temperature=temperature)
        return response.choices[0].message.content

    @staticmethod
    def load_openai_api_key():
        config_dir = config.user_config_file_dir
        config_file = os.path.join(config_dir, "config.json")
        with open(config_file , "r") as file:
            user_config_data = json.load(file)
        return user_config_data['openai_api_key']

    def load_openai_model(self):
        config_dir = config.user_config_file_dir
        config_file = os.path.join(config_dir, "config.json")
        with open(config_file, "r") as file:
            user_config_data = json.load(file)
        return user_config_data['openai_model']

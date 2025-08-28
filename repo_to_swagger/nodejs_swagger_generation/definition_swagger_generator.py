import json
from repo_to_swagger.prompts import node_js_prompt
from repo_to_swagger.llm_client import OpenAiClient



def get_function_definition_swagger(function_definition, context, route):
    openai_ai_client = OpenAiClient()
    messages = [{
        "role": "user",
        "content": node_js_prompt.fomat(route, function_definition, context)
    }]
    response = openai_ai_client.call_chat_completion(messages=messages, temperature=1)
    formatted_response = response.choices[0].message.content
    start_index = formatted_response.find('{')
    end_index = formatted_response.rfind('}')
    swagger_json_block = formatted_response[start_index:end_index + 1]
    return json.loads(swagger_json_block)

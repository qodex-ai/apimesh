import json
import time
from openai import APIError
from prompts import (
    batch_swagger_generation_prompt,
    batch_swagger_generation_system_prompt,
    node_js_prompt,
)
from llm_client import OpenAiClient


# Backoff before each retry of a failed OpenAI call.
RETRY_BACKOFF_SECONDS = (1, 4)

# Framework wording handed to the shared batch prompt.
BATCH_FRAMEWORK_LABEL = "Node.js (Express or NestJS)"
BATCH_FRAMEWORK_NOTES = ""


def _call_with_retries(messages):
    """Run one chat completion, backing off on API errors. Returns the raw text."""
    openai_ai_client = OpenAiClient()
    for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
        try:
            return openai_ai_client.call_chat_completion(messages=messages, temperature=1)
        except APIError:
            if attempt == len(RETRY_BACKOFF_SECONDS):
                raise
            time.sleep(RETRY_BACKOFF_SECONDS[attempt])
    return None


def _parse_json_object(response):
    if not response:
        return None
    start_index = response.find('{')
    end_index = response.rfind('}')
    if start_index == -1 or end_index <= start_index:
        return None
    try:
        return json.loads(response[start_index:end_index + 1])
    except ValueError:
        return None


def get_function_definition_swagger(function_definition, context, route, http_method=None):
    content = node_js_prompt.format(route = route, http_method = (http_method or 'not provided'), function_definition = function_definition, context=context)
    messages = [{
        "role": "user",
        "content": content
    }]
    return _parse_json_object(_call_with_retries(messages))


def get_batch_definition_swagger(endpoints_list, shared_context, per_endpoint_sections):
    """
    One call covering every endpoint of a batch. Returns the parsed response, or
    None when the reply carried no JSON object; the caller decides whether the
    payload is usable and when to fall back to the per endpoint calls.
    """
    content = batch_swagger_generation_prompt.format(
        framework_label=BATCH_FRAMEWORK_LABEL,
        framework_notes=BATCH_FRAMEWORK_NOTES,
        endpoints_list=endpoints_list,
        shared_context=shared_context,
        per_endpoint_sections=per_endpoint_sections,
    )
    messages = [
        {"role": "system", "content": batch_swagger_generation_system_prompt},
        {"role": "user", "content": content},
    ]
    return _parse_json_object(_call_with_retries(messages))

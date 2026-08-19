import json
import re
import time
from typing import List, Optional

import pipeline_common
from llm_client import OpenAiClient
from prompts import (
    batch_swagger_generation_prompt,
    batch_swagger_generation_system_prompt,
    ruby_on_rails_swagger_generation_prompt,
)


_SYSTEM_PROMPT = (
    "You are a meticulous API documentation assistant. "
    "Respond with a single valid JSON object that matches the requested schema. "
    "Do not include any surrounding prose, markdown, or code fences."
)

_RETRY_BACKOFF_SECONDS = pipeline_common.API_RETRY_DELAYS

# Framework wording handed to the shared batch prompt.
BATCH_FRAMEWORK_LABEL = "Ruby on Rails"
BATCH_FRAMEWORK_NOTES = "Paths use OpenAPI {param} templating."


def _extract_json_block(raw_text: str) -> Optional[str]:
    """
    Extract the JSON payload from a raw LLM response, handling code fences and
    other wrapping text the model might emit.
    """
    if not raw_text:
        return None

    fence_match = re.search(
        r"```(?:json)?\s*(\{[\s\S]*\})\s*```", raw_text, flags=re.IGNORECASE
    )
    if fence_match:
        return fence_match.group(1).strip()

    start_index = raw_text.find("{")
    end_index = raw_text.rfind("}")
    if start_index == -1 or end_index == -1 or end_index <= start_index:
        return None
    return raw_text[start_index : end_index + 1].strip()


def get_function_definition_swagger(
    function_definition: List[str],
    context: List[List[str]],
    route: str,
    http_method: Optional[str] = None,
) -> dict:
    """
    Delegate the heavy lifting of producing a Swagger snippet for a single
    Rails endpoint to the LLM, mirroring the behaviour of the Node and Python
    generators.
    """
    openai_ai_client = OpenAiClient()
    function_definition_text = "".join(function_definition)
    # The context used to be sent twice, once folded into the endpoint info and
    # once as the authentication information, which doubled the prompt for no gain.
    context_text = "\n\n".join("".join(block) for block in context) if context else ""

    prompt = ruby_on_rails_swagger_generation_prompt.format(
        endpoint_info=function_definition_text,
        endpoint_method=http_method or "GET",
        endpoint_path=route,
        authentication_information=context_text,
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    last_error: Optional[Exception] = None
    attempts = len(_RETRY_BACKOFF_SECONDS) + 1
    for attempt in range(attempts):
        try:
            response = openai_ai_client.call_chat_completion(
                messages=messages, temperature=0
            )
        except Exception as exc:
            last_error = exc
            if attempt < len(_RETRY_BACKOFF_SECONDS):
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
            continue
        swagger_json_block = _extract_json_block(response)
        if not swagger_json_block:
            last_error = ValueError("LLM response did not contain JSON payload.")
            continue
        try:
            return json.loads(swagger_json_block)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue

    error_message = (
        f"Failed to get Swagger JSON from the LLM after {attempts} attempts"
    )
    if last_error:
        raise ValueError(f"{error_message}: {last_error}") from last_error
    raise ValueError(f"{error_message}.")


def get_batch_definition_swagger(
    endpoints_list: str, shared_context: str, per_endpoint_sections: str
) -> Optional[dict]:
    """
    One call covering every endpoint of a batch. Returns the parsed response, or
    None when no reply carried a JSON payload; the caller decides whether the
    payload is usable and when to fall back to the per endpoint calls.
    """
    openai_ai_client = OpenAiClient()
    prompt = batch_swagger_generation_prompt.format(
        framework_label=BATCH_FRAMEWORK_LABEL,
        framework_notes=BATCH_FRAMEWORK_NOTES,
        endpoints_list=endpoints_list,
        shared_context=shared_context,
        per_endpoint_sections=per_endpoint_sections,
    )
    messages = [
        {"role": "system", "content": batch_swagger_generation_system_prompt},
        {"role": "user", "content": prompt},
    ]

    for attempt in range(len(_RETRY_BACKOFF_SECONDS) + 1):
        try:
            response = openai_ai_client.call_chat_completion(
                messages=messages, temperature=0
            )
        except Exception:
            if attempt < len(_RETRY_BACKOFF_SECONDS):
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
            continue
        swagger_json_block = _extract_json_block(response)
        if not swagger_json_block:
            continue
        try:
            return json.loads(swagger_json_block)
        except json.JSONDecodeError:
            continue
    return None

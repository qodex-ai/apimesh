import json
import re
from typing import List, Optional

from llm_client import OpenAiClient
from prompts import ruby_on_rails_swagger_generation_prompt


_SYSTEM_PROMPT = (
    "You are a meticulous API documentation assistant. "
    "Respond with a single valid JSON object that matches the requested schema. "
    "Do not include any surrounding prose, markdown, or code fences."
)


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
    context_text = "\n\n".join("".join(block) for block in context) if context else ""
    endpoint_info_text = (
        f"{function_definition_text}\n\n{context_text}"
        if context_text
        else function_definition_text
    )

    prompt = ruby_on_rails_swagger_generation_prompt.format(
        endpoint_info=endpoint_info_text,
        endpoint_method=http_method or "GET",
        endpoint_path=route,
        authentication_information=context_text,
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    last_error: Optional[Exception] = None
    for _ in range(3):
        response = openai_ai_client.call_chat_completion(
            messages=messages, temperature=0
        )
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
        "Failed to parse Swagger JSON from LLM response after multiple attempts."
    )
    if last_error:
        raise ValueError(error_message) from last_error
    raise ValueError(error_message)

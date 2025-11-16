import json
from typing import List, Optional

from llm_client import OpenAiClient
from prompts import (
    golang_swagger_generation_prompt,
    swagger_generation_system_prompt,
)


def _extract_json_block(raw_text: str) -> Optional[str]:
    if not raw_text:
        return None
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return raw_text[start : end + 1]


def _cleanup_swagger_payload(payload: dict) -> dict:
    paths = payload.get("paths", {})
    for path_data in paths.values():
        for method_data in path_data.values():
            auth_tag = method_data.get("auth_tag")
            if auth_tag is None or str(auth_tag).strip() == "":
                method_data.pop("auth_tag", None)
    return payload


def get_function_definition_swagger(
    function_definition: List[str],
    context: List[List[str]],
    route: str,
    http_method: Optional[str] = None,
) -> dict:
    client = OpenAiClient()
    function_text = "".join(function_definition)
    context_text = "\n\n".join("".join(block) for block in context) if context else ""

    prompt = golang_swagger_generation_prompt.format(
        endpoint_method=http_method or "GET",
        endpoint_path=route,
        endpoint_method_lower=(http_method or "GET").lower(),
        endpoint_info=function_text,
        authentication_information=context_text,
    )

    messages = [
        {"role": "system", "content": swagger_generation_system_prompt},
        {"role": "user", "content": prompt},
    ]

    last_error: Optional[Exception] = None
    for _ in range(3):
        response = client.call_chat_completion(messages=messages, temperature=0)
        payload = _extract_json_block(response)
        if not payload:
            last_error = ValueError("LLM response was missing JSON payload.")
            continue
        try:
            return _cleanup_swagger_payload(json.loads(payload))
        except json.JSONDecodeError as exc:
            last_error = exc
    raise ValueError("Unable to parse Swagger JSON response.") from last_error

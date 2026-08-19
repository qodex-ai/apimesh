import json
import time
from typing import List, Optional, Sequence, Tuple

import pipeline_common
from llm_client import OpenAiClient
from prompts import (
    batch_swagger_generation_prompt,
    batch_swagger_generation_system_prompt,
    golang_swagger_generation_prompt,
    swagger_generation_system_prompt,
)
from utils import num_tokens_from_string


_API_RETRY_DELAYS = pipeline_common.API_RETRY_DELAYS
CONTEXT_TOKEN_BUDGET = pipeline_common.CONTEXT_TOKEN_BUDGET
_EFFECTIVE_CONTEXT_BUDGET = pipeline_common.EFFECTIVE_CONTEXT_BUDGET
HANDLER_TOKEN_BUDGET = pipeline_common.MAX_HANDLER_TOKENS
_TRUNCATION_MARKER = "\n... truncated\n"
_FRAMEWORK_LABEL = "Go"
_FRAMEWORK_NOTES = "6. Path parameters use OpenAPI {param} templating."


def _call_with_retry(client: OpenAiClient, messages: List[dict]) -> str:
    # Transient API failures get two retries; a persistent one propagates so the
    # caller can drop this endpoint instead of the whole run.
    for delay in _API_RETRY_DELAYS:
        try:
            return client.call_chat_completion(messages=messages, temperature=0)
        except Exception:
            time.sleep(delay)
    return client.call_chat_completion(messages=messages, temperature=0)


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
    if not isinstance(paths, dict):
        return payload
    for path_data in paths.values():
        if not isinstance(path_data, dict):
            continue
        for method_data in path_data.values():
            # A path item can hold non-operation values such as "parameters": [].
            if not isinstance(method_data, dict):
                continue
            # The sample output shows an empty auth tag, and older replies still
            # use the pre-extension key name.
            for auth_tag_key in ("x-auth-tag", "auth_tag"):
                auth_tag = method_data.get(auth_tag_key)
                if auth_tag is None or str(auth_tag).strip() == "":
                    method_data.pop(auth_tag_key, None)
    return payload


def build_endpoint_section(label: str, body) -> Tuple[str, bool]:
    """One endpoint's prompt section, with its handler capped."""
    return pipeline_common.handler_section(
        f"{label}:", body, HANDLER_TOKEN_BUDGET, _TRUNCATION_MARKER
    )


def section_token_cost(label: str, body) -> int:
    """What one endpoint's section costs against the batch budget.

    The caller packs batches with this, so it has to price exactly the section
    the prompt ends up carrying, truncation marker included.
    """
    section, _ = build_endpoint_section(label, body)
    return num_tokens_from_string(section)


def build_batch_prompt(
    endpoint_entries: Sequence[Tuple[str, str]],
    context_blocks,
    source_file: str,
) -> str:
    """One prompt covering a file's endpoints, inside the context budget."""
    sections: List[str] = []
    handler_tokens = 0
    truncated = False
    for label, body in endpoint_entries:
        section, was_truncated = build_endpoint_section(label, body)
        truncated = truncated or was_truncated
        handler_tokens += num_tokens_from_string(section)
        sections.append(section)
    kept, dropped = pipeline_common.fit_context(
        context_blocks, max(_EFFECTIVE_CONTEXT_BUDGET - handler_tokens, 0)
    )
    pipeline_common.report_context_trim(source_file, dropped, truncated)
    return batch_swagger_generation_prompt.format(
        framework_label=_FRAMEWORK_LABEL,
        framework_notes=_FRAMEWORK_NOTES,
        endpoints_list="\n".join(label for label, _ in endpoint_entries),
        shared_context="\n\n".join(kept),
        per_endpoint_sections="\n\n".join(sections),
    )


def get_batch_definition_swagger(
    endpoint_entries: Sequence[Tuple[str, str]],
    context_blocks,
    source_file: str,
) -> Optional[dict]:
    """The model's paths payload for one file's endpoints, None when unusable.

    A second unusable reply is not worth a third batch call: the caller spends
    per-endpoint calls on those endpoints instead.
    """
    client = OpenAiClient()
    messages = [
        {"role": "system", "content": batch_swagger_generation_system_prompt},
        {
            "role": "user",
            "content": build_batch_prompt(endpoint_entries, context_blocks, source_file),
        },
    ]
    for _ in range(2):
        response = _call_with_retry(client, messages)
        payload = _extract_json_block(response)
        if not payload:
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("paths"), dict):
            return _cleanup_swagger_payload(parsed)
    return None


def get_function_definition_swagger(
    function_definition: List[str],
    context: List[List[str]],
    route: str,
    http_method: Optional[str] = None,
    source_file: Optional[str] = None,
) -> dict:
    client = OpenAiClient()
    function_text, truncated = pipeline_common.truncate_to_tokens(
        "".join(function_definition), HANDLER_TOKEN_BUDGET, _TRUNCATION_MARKER
    )
    kept, dropped = pipeline_common.fit_context(
        context,
        max(_EFFECTIVE_CONTEXT_BUDGET - num_tokens_from_string(function_text), 0),
    )
    pipeline_common.report_context_trim(source_file or route, dropped, truncated)
    context_text = "\n\n".join(kept)

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
        response = _call_with_retry(client, messages)
        payload = _extract_json_block(response)
        if not payload:
            last_error = ValueError("LLM response was missing JSON payload.")
            continue
        try:
            return _cleanup_swagger_payload(json.loads(payload))
        except json.JSONDecodeError as exc:
            last_error = exc
    raise ValueError("Unable to parse Swagger JSON response.") from last_error

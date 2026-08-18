import json
import time
from typing import List, Optional, Sequence, Tuple

from llm_client import OpenAiClient
from prompts import (
    batch_swagger_generation_prompt,
    batch_swagger_generation_system_prompt,
    golang_swagger_generation_prompt,
    swagger_generation_system_prompt,
)
from utils import num_tokens_from_string


_API_RETRY_DELAYS = (1, 4)
# A batch prompt carries one file's endpoints, so the source context has to stay
# inside something the model can actually attend to.
CONTEXT_TOKEN_BUDGET = 6000

# Headroom for the separators joined between sections and blocks, so the
# budget holds for the final assembled prompt, not just the parts.
_EFFECTIVE_CONTEXT_BUDGET = CONTEXT_TOKEN_BUDGET - 64
HANDLER_TOKEN_BUDGET = 2000
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


def _block_text(block) -> str:
    if isinstance(block, str):
        return block
    return "".join(str(line) for line in block or [])


def _truncate_to_tokens(text: str, limit: int) -> Tuple[str, bool]:
    """The first `limit` tokens of `text`, and whether anything was cut."""
    if not text or num_tokens_from_string(text) <= limit:
        return text, False
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if num_tokens_from_string(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low] + _TRUNCATION_MARKER, True


def _dedupe_blocks(blocks) -> List[str]:
    """The same helper body is attached to every endpoint of a file, so an
    undeduped batch prompt repeats it once per endpoint."""
    seen = set()
    unique: List[str] = []
    for block in blocks or []:
        text = _block_text(block)
        if not text.strip() or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _fit_context(blocks, budget: int) -> Tuple[List[str], int]:
    """Deduped context blocks that fit the budget, and how many were dropped.

    Handler bodies are the part the model cannot document without, so they are
    priced first and whatever context does not fit is dropped from the end.
    """
    kept: List[str] = []
    used = 0
    unique = _dedupe_blocks(blocks)
    for index, text in enumerate(unique):
        cost = num_tokens_from_string(text) + 2
        if used + cost > budget:
            return kept, len(unique) - index
        kept.append(text)
        used += cost
    return kept, 0


def _report_context_trim(source: str, dropped: int, truncated: bool) -> None:
    if not dropped and not truncated:
        return
    print(f"apimesh: context truncated for {source} ({dropped} blocks dropped)")


def build_endpoint_section(label: str, body) -> Tuple[str, bool]:
    """One endpoint's prompt section, with its handler capped."""
    body_text, was_truncated = _truncate_to_tokens(
        _block_text(body), HANDLER_TOKEN_BUDGET
    )
    return f"{label}:\n{body_text}", was_truncated


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
    kept, dropped = _fit_context(
        context_blocks, max(_EFFECTIVE_CONTEXT_BUDGET - handler_tokens, 0)
    )
    _report_context_trim(source_file, dropped, truncated)
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
    function_text, truncated = _truncate_to_tokens(
        "".join(function_definition), HANDLER_TOKEN_BUDGET
    )
    kept, dropped = _fit_context(
        context,
        max(_EFFECTIVE_CONTEXT_BUDGET - num_tokens_from_string(function_text), 0),
    )
    _report_context_trim(source_file or route, dropped, truncated)
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

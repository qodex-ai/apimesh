import json
from prompts import (
    batch_swagger_generation_prompt,
    batch_swagger_generation_system_prompt,
    python_swagger_prompt,
)
import pipeline_common
from llm_client import OpenAiClient
from utils import num_tokens_from_string


CONTEXT_TOKEN_BUDGET = pipeline_common.CONTEXT_TOKEN_BUDGET
_EFFECTIVE_CONTEXT_BUDGET = pipeline_common.EFFECTIVE_CONTEXT_BUDGET
HANDLER_TOKEN_BUDGET = pipeline_common.MAX_HANDLER_TOKENS
_TRUNCATION_MARKER = "\n... truncated\n"
_FRAMEWORK_LABEL = "Python (Flask, FastAPI, or Django)"
_FRAMEWORK_NOTES = (
    "6. Path parameters use OpenAPI {param} templating, including Flask "
    "converters written as <int:pk>."
)


def parse_swagger_response(response):
    """Pull the swagger object out of a model reply. None means unusable."""
    if not response:
        return None
    start_index = response.find('{')
    end_index = response.rfind('}')
    if start_index == -1 or end_index < start_index:
        return None
    try:
        return json.loads(response[start_index:end_index + 1])
    except ValueError:
        return None


def build_endpoint_section(label, body):
    """One endpoint's prompt section, with its handler capped."""
    return pipeline_common.handler_section(
        f"{label}:", body, HANDLER_TOKEN_BUDGET, _TRUNCATION_MARKER
    )


def section_token_cost(label, body):
    """What one endpoint's section costs against the batch budget.

    The caller packs batches with this, so it has to price exactly the section
    the prompt ends up carrying, truncation marker included.
    """
    section, _ = build_endpoint_section(label, body)
    return num_tokens_from_string(section)


def build_batch_prompt(endpoint_entries, context_blocks, source_file):
    """One prompt covering a file's endpoints, inside the context budget."""
    sections = []
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


def get_batch_definition_swagger(endpoint_entries, context_blocks, source_file):
    """One LLM call for a file's endpoints. None means the reply is unusable."""
    openai_ai_client = OpenAiClient()
    messages = [
        {"role": "system", "content": batch_swagger_generation_system_prompt},
        {
            "role": "user",
            "content": build_batch_prompt(endpoint_entries, context_blocks, source_file),
        },
    ]
    response = openai_ai_client.call_chat_completion(messages=messages, temperature=0)
    payload = parse_swagger_response(response)
    if isinstance(payload, dict) and isinstance(payload.get("paths"), dict):
        return payload
    return None


def get_function_definition_swagger(function_definition, context, route, http_method=None, source_file=None):
    openai_ai_client = OpenAiClient()
    function_text, truncated = pipeline_common.truncate_to_tokens(
        pipeline_common.block_text(function_definition),
        HANDLER_TOKEN_BUDGET,
        _TRUNCATION_MARKER,
    )
    kept, dropped = pipeline_common.fit_context(
        context, max(_EFFECTIVE_CONTEXT_BUDGET - num_tokens_from_string(function_text), 0)
    )
    pipeline_common.report_context_trim(source_file or route, dropped, truncated)
    messages = [{
        "role": "user",
        "content": python_swagger_prompt.format(route = route, http_method = (http_method or "not provided"), function_definition = function_text, context = "\n\n".join(kept))
    }]
    response = openai_ai_client.call_chat_completion(messages=messages, temperature=1)
    return parse_swagger_response(response)

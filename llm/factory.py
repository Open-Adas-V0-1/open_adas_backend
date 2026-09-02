from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import PydanticOutputParser

from app.config import Settings, get_node_llm_config, get_settings
from app.logging import get_logger
from llm.base import build_retry_decorator
from llm.providers.claude import build_claude
from llm.providers.gemini import build_gemini
from llm.providers.openai_compatible import build_openai_compatible

logger = get_logger(__name__)

# gpt, ollama and capgemini all speak the OpenAI-compatible API; only base_url differs.
OPENAI_COMPATIBLE_BACKENDS = {"gpt", "ollama", "capgemini"}

# Backends whose gateway was found (Layer-1 rebuild, Step 6 full integration run) NOT to
# honor LangChain's default with_structured_output() mechanism against gpt-4o-class
# models: the strict json_schema response_format is silently ignored (the model just
# free-texts a fenced, differently-shaped JSON blob instead), and method="function_calling"
# 500s outright (the gateway rejects a targeted tool_choice, only accepting "none"/"auto").
# method="json_mode" (response_format={"type": "json_object"} + the schema folded into the
# prompt, same mechanism every OpenAI-compatible gateway is expected to support) verified
# working end-to-end against this gateway. Scoped to the backend(s) this was actually
# observed against, not applied speculatively to gpt/ollama.
_JSON_MODE_ONLY_BACKENDS = {"capgemini"}

_client_cache: dict[tuple[str, str], BaseChatModel] = {}


def _resolve_backend_model(settings: Settings, node_name: str | None) -> tuple[str, str]:
    backend = settings.llm_backend
    model_id = settings.llm_model_id

    if node_name:
        override = get_node_llm_config(node_name)
        if override:
            backend = override.get("backend") or backend
            model_id = override.get("model_id") or model_id

    return backend, model_id


def _build_client(settings: Settings, backend: str, model_id: str) -> BaseChatModel:
    if backend in OPENAI_COMPATIBLE_BACKENDS:
        return build_openai_compatible(
            model_id=model_id,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout,
        )
    if backend == "gemini":
        api_key = settings.gemini_api_key or settings.llm_api_key
        return build_gemini(model_id=model_id, api_key=api_key, timeout=settings.llm_timeout)
    if backend == "claude":
        api_key = settings.anthropic_api_key or settings.llm_api_key
        return build_claude(model_id=model_id, api_key=api_key, timeout=settings.llm_timeout)

    raise ValueError(
        f"Unsupported LLM backend '{backend}'. Expected one of: "
        "gpt, ollama, gemini, claude, capgemini"
    )


def get_llm(node_name: str | None = None) -> BaseChatModel:
    """Resolve (backend, model_id) from env — honoring a per-node override — and
    return a cached, retry-wrapped LangChain chat model.
    """
    settings = get_settings()
    backend, model_id = _resolve_backend_model(settings, node_name)

    if not model_id:
        raise ValueError(
            f"No model_id resolved for node={node_name!r} backend={backend!r}. "
            "Set LLM_MODEL_ID (or LLM_OVERRIDE_{NODE}_MODEL_ID)."
        )

    cache_key = (backend, model_id)
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    logger.info("llm.resolve", node=node_name, backend=backend, model_id=model_id)

    client = _build_client(settings, backend, model_id)

    retry_decorator = build_retry_decorator(settings)
    # LangChain chat models are pydantic models: instance attribute assignment
    # on class methods is blocked by BaseModel.__setattr__, so bypass it deliberately
    # to shadow invoke/ainvoke with their retry-wrapped versions.
    object.__setattr__(client, "invoke", retry_decorator(client.invoke))
    object.__setattr__(client, "ainvoke", retry_decorator(client.ainvoke))

    if backend in _JSON_MODE_ONLY_BACKENDS:
        original_with_structured_output = client.with_structured_output

        def _with_structured_output(schema, **kwargs):
            kwargs.setdefault("method", "json_mode")
            structured = original_with_structured_output(schema, **kwargs)
            if kwargs["method"] == "json_mode":
                # json_mode (unlike the strict json_schema/function_calling methods) is
                # response_format={"type": "json_object"} ONLY -- LangChain does not
                # auto-inject the target schema into the prompt for it (that's the
                # caller's documented responsibility). Every prompt in this repo was
                # written assuming the API itself enforces required fields/types, so
                # without this the model silently drops fields (observed: PlanDecision's
                # required tasks[].description). Append the schema's own format
                # instructions to whatever string prompt each node already builds --
                # same fix, in the one place json_mode is turned on, not per node.
                format_instructions = PydanticOutputParser(pydantic_object=schema).get_format_instructions()
                original_ainvoke = structured.ainvoke
                original_invoke = structured.invoke

                def _prepend(prompt):
                    if isinstance(prompt, str):
                        return f"{prompt}\n\n{format_instructions}"
                    return prompt

                async def _ainvoke(prompt, *args, **kw):
                    return await original_ainvoke(_prepend(prompt), *args, **kw)

                def _invoke(prompt, *args, **kw):
                    return original_invoke(_prepend(prompt), *args, **kw)

                object.__setattr__(structured, "ainvoke", _ainvoke)
                object.__setattr__(structured, "invoke", _invoke)
            return structured

        object.__setattr__(client, "with_structured_output", _with_structured_output)

    _client_cache[cache_key] = client
    return client

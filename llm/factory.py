from langchain_core.language_models import BaseChatModel

from app.config import Settings, get_node_llm_config, get_settings
from app.logging import get_logger
from llm.base import build_retry_decorator
from llm.providers.claude import build_claude
from llm.providers.gemini import build_gemini
from llm.providers.openai_compatible import build_openai_compatible

logger = get_logger(__name__)

# gpt, ollama and capgemini all speak the OpenAI-compatible API; only base_url differs.
OPENAI_COMPATIBLE_BACKENDS = {"gpt", "ollama", "capgemini"}

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

    _client_cache[cache_key] = client
    return client

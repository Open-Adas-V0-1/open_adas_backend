from langchain_openai import ChatOpenAI


def build_openai_compatible(
    model_id: str, api_key: str | None, base_url: str | None, timeout: float
) -> ChatOpenAI:
    return ChatOpenAI(
        model=model_id,
        api_key=api_key or "not-needed",
        base_url=base_url,
        timeout=timeout,
    )

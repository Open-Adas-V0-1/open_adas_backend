from langchain_anthropic import ChatAnthropic


def build_claude(model_id: str, api_key: str | None, timeout: float) -> ChatAnthropic:
    return ChatAnthropic(
        model=model_id,
        api_key=api_key,
        timeout=timeout,
    )

from langchain_google_genai import ChatGoogleGenerativeAI


def build_gemini(model_id: str, api_key: str | None, timeout: float) -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model=model_id,
        google_api_key=api_key,
        timeout=timeout,
    )

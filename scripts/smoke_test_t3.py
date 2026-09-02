"""Smoke test for T3: LLM factory end-to-end.

Run: python -m scripts.smoke_test_t3
"""
from app.config import get_settings
from llm.factory import get_llm


def main() -> None:
    settings = get_settings()
    print(f"Configured LLM_BACKEND={settings.llm_backend} LLM_MODEL_ID={settings.llm_model_id}")

    llm = get_llm()
    response = llm.invoke("Say hello in exactly five words.")
    print(f"\n[{settings.llm_backend}/{settings.llm_model_id}] response:")
    print(response.content)


if __name__ == "__main__":
    main()

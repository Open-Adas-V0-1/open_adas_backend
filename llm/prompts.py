from pathlib import Path

_PROMPTS_ROOT = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(relative_path: str, **variables: object) -> str:
    """Load a .md prompt file from prompts/ at runtime and substitute {{var}} tokens."""
    text = (_PROMPTS_ROOT / relative_path).read_text(encoding="utf-8")
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text

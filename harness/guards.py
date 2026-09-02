from app.config import get_settings


def guard_breached(visits: int, max_visits: int) -> bool:
    """True when a supervisor node has been visited more times than allowed this run.
    Shared by the top-level and middle-layer supervisors so the fail-open threshold
    check is identical everywhere.
    """
    return visits > max_visits


def checkpoint_durability() -> str:
    """Env-driven durability mode ('sync' | 'async' | 'exit') passed to .ainvoke() at
    every layer that owns or forwards the production checkpointer's config.
    """
    return get_settings().checkpoint_durability

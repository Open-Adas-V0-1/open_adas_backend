"""Deterministic (router-as-code) helpers for the Step 5 memory-optimization gate --
NO LLM calls here, same spirit as plan_ops.py. Decides whether the turn's SHORT-TERM
context is near full enough to warrant routing through memory_optimization before
finalize_turn, vs going straight to finalize_turn -> END.

Short-term only: this NEVER looks at, counts, or touches approved artifacts (those are
already finalized in Postgres via RequirementRepo/DiagramRepo) -- only the in-flight
conversational state (user_input, response, messages, plan_state) is ever a candidate.
"""

import json

from app.config import get_settings
from supervisor.state import SupervisorState

# Rough chars-per-token heuristic (~4 chars/token for English text) -- good enough to
# drive a near-full GATE without pulling in a real tokenizer dependency. Precision
# doesn't matter here; only the ratio crossing the env-driven threshold does.
_CHARS_PER_TOKEN_ESTIMATE = 4


def estimate_short_term_tokens(state: SupervisorState) -> int:
    """Deterministic estimate of how many tokens the turn's short-term context is
    currently consuming: user_input, the in-flight response, any prior turn messages,
    and the plan_state's tasks (descriptions, results refs -- still light refs, never
    full artifact content).
    """
    parts: list[str] = []
    if state.get("user_input"):
        parts.append(state["user_input"])
    if state.get("response"):
        parts.append(state["response"])
    parts.extend(state.get("messages") or [])
    plan_state = state.get("plan_state")
    if plan_state:
        parts.append(json.dumps(plan_state, default=str))

    total_chars = sum(len(p) for p in parts)
    return total_chars // _CHARS_PER_TOKEN_ESTIMATE


def is_context_near_full(state: SupervisorState) -> bool:
    """True once the estimated short-term token usage reaches
    MEMORY_OPT_THRESHOLD_RATIO (default 0.8) of MEMORY_SHORT_TERM_BUDGET_TOKENS. Both
    env-driven (app.config.Settings), read fresh every call so tests can force either
    scenario via os.environ + get_settings.cache_clear().
    """
    settings = get_settings()
    budget = settings.memory_short_term_budget_tokens
    if budget <= 0:
        return False
    ratio = estimate_short_term_tokens(state) / budget
    return ratio >= settings.memory_opt_threshold_ratio

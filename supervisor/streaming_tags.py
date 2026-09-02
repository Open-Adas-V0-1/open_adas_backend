"""Shared constant for the chat SSE layer's token attribution (T6b Step 3a).

Kept in its own tiny module so supervisor/router.py (a graph node -- the ONLY
permitted "graph touch" for this step, a tag) and app/chat/* (the API layer) both
reference the SAME literal tag string. No graph logic lives here.
"""

# Attached via .with_config(tags=[...]) to the ONE LLM call whose streamed output is
# safe to forward to the user as `token` SSE events: top_level_supervisor's hub
# classification/response call. Covers BOTH simple_response and unclear (both produce
# genuine user-facing text via the SAME call/field, HubDecision.response) --
# needs_execution's response is always None, so nothing streams for it. Every other
# LLM call anywhere in the graph (planning, generation, verification, confirm-
# question phrasing, contextual answers, ...) is left UNTAGGED and is therefore never
# forwarded -- this is an allow-list by construction, never a deny-list.
TOKEN_STREAM_TAG = "hub_answer"

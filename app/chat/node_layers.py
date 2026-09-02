"""Node-name -> layer attribution for the chat SSE `status` event (T6b Step 3a).

Verified empirically against the REAL compiled graph (astream_events on a live run),
not assumed from LangGraph docs -- see the two facts below that make a naive
node-name lookup alone wrong:

1. Layer-1's OWN plan_node (supervisor/graph.py) and Layer-3's OWN plan_node
   (agents/sysml/graph.py) are registered under the EXACT SAME string name in their
   separately-compiled graphs.
2. Layer-2 and Layer-3 share the SAME checkpoint_ns prefix. Layer-3 is invoked from
   sysml_processing (a PLAIN Layer-2 node function that calls another compiled
   graph's .ainvoke() directly) -- NOT LangGraph's native subgraph-as-node
   composition -- so it adds no additional namespace segment of its own; both
   surface under "sysml_middle_node:<run-id>".

Disambiguation is therefore two-tier: whether checkpoint_ns is set AT ALL (empty/
None -> genuinely top-level, i.e. Layer 1) separates Layer-1's plan_node from
Layer-3's; node-name set membership then separates Layer-2 from Layer-3 within a
non-empty namespace (those two node-name sets never collide with each other).
"""

_LAYER_1_NODES = {
    "top_level_supervisor",
    "plan_node",
    "plan_review",
    "sysml_middle_node",
    "memory_optimization",
    "finalize_turn",
}
_LAYER_2_NODES = {
    "middle_supervisor",
    "validate_inputs",
    "resolve_level",
    "build_structured_format",
    "user_confirm_inputs",
    "sysml_processing",
}
_LAYER_3_NODES = {
    "sysml_supervisor",
    "plan_node",
    "generate_node",
    "verify_node",
    "requirement_review",
    "contextual_answer",
    "finalize",
}


def attribute_layer(node_name: str | None, checkpoint_ns: str | None) -> int | None:
    """Returns 1, 2, or 3 -- or None if unrecognized. Fails SAFE: an unrecognized
    node/namespace combination emits no status event at all, rather than guessing.
    """
    if not node_name:
        return None
    if not checkpoint_ns:
        return 1 if node_name in _LAYER_1_NODES else None
    if node_name in _LAYER_2_NODES:
        return 2
    if node_name in _LAYER_3_NODES:
        return 3
    return None

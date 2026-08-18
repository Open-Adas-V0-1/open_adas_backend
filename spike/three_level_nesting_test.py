# Tested against langgraph==0.6.3
#
# Purpose: before building the real middle (SysML) layer, validate two things about a
# THREE-level nested graph (L1 parent -> L2 middle -> L3 innermost processing):
#   1) Does interrupt() raised at L3 bubble up TWO levels to L1 and resume there via
#      Command(resume=...), without re-running work done before the interrupt?
#   2) Can each L3 invocation get its OWN independent thread_id (so we can return to a
#      specific processing later), while nested interrupt/resume still works? Which
#      L2->L3 wiring pattern (explicit wrapper vs. direct node-add) allows this?
#
# No LLM, no DB, no FastAPI. MemorySaver only, in one place: the L1 (parent) compile.
from __future__ import annotations

from importlib.metadata import version
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

print(f"langgraph version: {version('langgraph')}")

EXECUTION_LOG: list[str] = []


def log(msg: str) -> None:
    EXECUTION_LOG.append(msg)
    print(msg)


# ---------------------------------------------------------------------------
# LEVEL 3 — single processing graph. Own schema, own vocabulary, no checkpointer.
# ---------------------------------------------------------------------------
class L3State(TypedDict):
    l3_input: str
    generated: str
    decision: str


def generate_node(state: L3State) -> dict:
    log("[L3] generate_node EXECUTED")
    return {"generated": "draft"}


def review_node(state: L3State) -> dict:
    log("[L3] review BEFORE interrupt")
    decision = interrupt({"level": "L3", "artifact": state["generated"]})
    log(f"[L3] review AFTER interrupt got: {decision}")
    return {"decision": decision}


def finalize_node(state: L3State) -> dict:
    log("[L3] finalize_node EXECUTED")
    return {}


l3_builder = StateGraph(L3State)
l3_builder.add_node("generate_node", generate_node)
l3_builder.add_node("review_node", review_node)
l3_builder.add_node("finalize_node", finalize_node)
l3_builder.add_edge(START, "generate_node")
l3_builder.add_edge("generate_node", "review_node")
l3_builder.add_edge("review_node", "finalize_node")
l3_builder.add_edge("finalize_node", END)
l3_graph = l3_builder.compile()  # no own checkpointer


# ---------------------------------------------------------------------------
# LEVEL 2 — middle SysML subgraph. Own schema, no checkpointer.
# ---------------------------------------------------------------------------
class L2State(TypedDict):
    l2_input: str
    l2_result: str
    proc_id: str


def middle_supervisor(state: L2State) -> dict:
    log("[L2] middle_supervisor EXECUTED")
    return {}


def processing_node_variant_a(state: L2State, config: RunnableConfig) -> dict:
    """Variant A: explicit wrapper. Maps L2 -> L3, invokes with an INDEPENDENT
    per-processing thread_id derived deterministically from state (not random,
    since this node re-runs from the top on resume).
    """
    proc_id = state.get("proc_id", "p1")
    child_config = {
        **config,
        "configurable": {**config["configurable"], "thread_id": f"proc-{proc_id}"},
    }
    l3_input = {"l3_input": state["l2_input"], "generated": "", "decision": ""}
    l3_output = l3_graph.invoke(l3_input, child_config)
    log("[L2] processing_node (variant A) EXECUTED")
    return {"l2_result": l3_output.get("decision", "")}


l2_builder_a = StateGraph(L2State)
l2_builder_a.add_node("middle_supervisor", middle_supervisor)
l2_builder_a.add_node("processing_node", processing_node_variant_a)
l2_builder_a.add_edge(START, "middle_supervisor")
l2_builder_a.add_edge("middle_supervisor", "processing_node")
l2_builder_a.add_edge("processing_node", END)
l2_graph_variant_a = l2_builder_a.compile()  # no own checkpointer


# Variant B: add the L3 graph DIRECTLY as a node (no wrapper, no config override).
l2_builder_b = StateGraph(L2State)
l2_builder_b.add_node("middle_supervisor", middle_supervisor)
l2_builder_b.add_node("processing_node", l3_graph)
l2_builder_b.add_edge(START, "middle_supervisor")
l2_builder_b.add_edge("middle_supervisor", "processing_node")
l2_builder_b.add_edge("processing_node", END)
l2_graph_variant_b = l2_builder_b.compile()  # no own checkpointer


# ---------------------------------------------------------------------------
# LEVEL 1 — parent. Own schema. The ONLY checkpointer in the whole stack.
# ---------------------------------------------------------------------------
class L1State(TypedDict):
    messages: str
    result: str


def make_top_supervisor():
    def top_supervisor(state: L1State) -> dict:
        log("[L1] top_supervisor EXECUTED")
        return {}

    return top_supervisor


def make_sysml_middle_node(l2_graph):
    def sysml_middle_node(state: L1State, config: RunnableConfig) -> dict:
        l2_input = {"l2_input": state["messages"], "l2_result": "", "proc_id": "1"}
        l2_output = l2_graph.invoke(l2_input, config)
        log("[L1] sysml_middle_node EXECUTED")
        return {"result": l2_output.get("l2_result", "")}

    return sysml_middle_node


def build_l1_graph(l2_graph, checkpointer):
    builder = StateGraph(L1State)
    builder.add_node("top_supervisor", make_top_supervisor())
    builder.add_node("sysml_middle_node", make_sysml_middle_node(l2_graph))
    builder.add_edge(START, "top_supervisor")
    builder.add_edge("top_supervisor", "sysml_middle_node")
    builder.add_edge("sysml_middle_node", END)
    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def run_variant(name: str, l2_graph, thread_id: str) -> None:
    print(f"\n{'=' * 70}\nVARIANT: {name}\n{'=' * 70}")

    start_index = len(EXECUTION_LOG)
    checkpointer = MemorySaver()
    l1_graph = build_l1_graph(l2_graph, checkpointer)
    config = {"configurable": {"thread_id": thread_id}}

    print(f"\n--- RUN 1 (expect pause), thread_id={thread_id!r} ---")
    try:
        result_1 = l1_graph.invoke({"messages": "hello", "result": ""}, config)
    except Exception as exc:  # noqa: BLE001 - this IS the thing we're probing for
        print(f"RUN 1 RAISED: {type(exc).__name__}: {exc}")
        print(f"\n=== VERDICT: {name} ===")
        print("Direct node-add with mismatched parent/child schemas failed outright.")
        print("=> independent per-processing thread_id: N/A (never reached L3 at all)")
        return

    print(f"RUN 1 raw result: {result_1}")
    interrupted = bool(result_1.get("__interrupt__"))
    print(f"Interrupt surfaced to L1 (2 levels up from L3): {result_1.get('__interrupt__')}")

    # Inspect which thread/namespace the interrupt actually lived under.
    parent_state = l1_graph.get_state(config)
    print(f"L1 get_state(config).tasks: {parent_state.tasks}")
    if parent_state.tasks:
        for task in parent_state.tasks:
            print(f"  task={task.name!r} state={task.state!r}")

    print(f"\n--- RUN 2 (resume), thread_id={thread_id!r} ---")
    result_2 = l1_graph.invoke(Command(resume="approve"), config)
    print(f"RUN 2 raw result: {result_2}")

    scoped_log = EXECUTION_LOG[start_index:]
    generate_count = scoped_log.count("[L3] generate_node EXECUTED")
    after_interrupt = [ln for ln in scoped_log if ln.startswith("[L3] review AFTER interrupt")]
    finalize_count = scoped_log.count("[L3] finalize_node EXECUTED")
    final_result = result_2.get("result")

    # Ground truth for thread isolation: inspect the checkpointer's OWN storage
    # directly, rather than trust what a wrapper node claimed to pass in.
    thread_ids_with_checkpoints = sorted(str(tid) for tid in checkpointer.storage.keys())

    print(f"\n=== VERDICT: {name} ===")
    print(f"Interrupt reached L1 on RUN 1: {'PASS' if interrupted else 'FAIL'}")
    print(f"generate_node executions: {generate_count} "
          f"({'PASS - ran once' if generate_count == 1 else 'FAIL - re-executed'})")
    print(f"review AFTER interrupt occurrences: {len(after_interrupt)} -> {after_interrupt}")
    print(f"finalize_node executions: {finalize_count} "
          f"({'PASS - ran once' if finalize_count == 1 else 'FAIL'})")
    print(f"parent result crossed 2 boundaries correctly: {final_result!r} "
          f"({'PASS' if final_result == 'approve' else 'FAIL'})")
    print(f"thread_ids actually present in the checkpointer's storage: {thread_ids_with_checkpoints}")
    if len(thread_ids_with_checkpoints) > 1 or (thread_ids_with_checkpoints and thread_ids_with_checkpoints != [thread_id]):
        print(f"=> independent per-processing thread_id: ACHIEVED (proc thread distinct from {thread_id!r})")
    else:
        print(f"=> independent per-processing thread_id: NOT achieved — everything lives under "
              f"the single parent thread_id {thread_id!r}, regardless of what the wrapper passed in")


def main() -> None:
    run_variant("A - explicit wrapper (override thread_id per processing)", l2_graph_variant_a, "parent-thread-1")
    run_variant("B - direct node-add (compiled L3 graph as an L2 node)", l2_graph_variant_b, "parent-thread-2")

    print(f"\n{'=' * 70}\nFULL EXECUTION LOG\n{'=' * 70}")
    for line in EXECUTION_LOG:
        print(line)


if __name__ == "__main__":
    main()

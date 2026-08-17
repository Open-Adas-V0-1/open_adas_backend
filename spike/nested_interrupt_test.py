# Tested against langgraph==0.6.3
#
# Purpose: validate that when interrupt() is called inside a SUBGRAPH invoked as a
# NODE inside a PARENT graph, the whole graph pauses correctly and RESUMES from the
# interrupt point via Command(resume=...) WITHOUT re-executing work that already ran
# before the interrupt (e.g. the child's generate_node).
from __future__ import annotations

from importlib.metadata import version
from typing import TypedDict

import langgraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

print(f"langgraph version: {version('langgraph')}")

# Capture every labeled print so we can count executions programmatically
# instead of eyeballing stdout.
EXECUTION_LOG: list[str] = []


def log(msg: str) -> None:
    EXECUTION_LOG.append(msg)
    print(msg)


# --------------------------------------------------------------------------
# CHILD subgraph ("ADAS" agent) — its own state schema, no own checkpointer.
# --------------------------------------------------------------------------
class ChildState(TypedDict):
    child_input: str
    generated: str
    decision: str


def generate_node(state: ChildState) -> dict:
    log("[CHILD] generate_node EXECUTED")
    return {"generated": "draft-artifact"}


def review_node(state: ChildState) -> dict:
    log("[CHILD] review_node BEFORE interrupt")
    decision = interrupt({"artifact": state["generated"], "msg": "please review"})
    log(f"[CHILD] review_node AFTER interrupt, got: {decision}")
    return {"decision": decision}


def finalize_node(state: ChildState) -> dict:
    log("[CHILD] finalize_node EXECUTED")
    return {}


child_builder = StateGraph(ChildState)
child_builder.add_node("generate_node", generate_node)
child_builder.add_node("review_node", review_node)
child_builder.add_node("finalize_node", finalize_node)
child_builder.add_edge(START, "generate_node")
child_builder.add_edge("generate_node", "review_node")
child_builder.add_edge("review_node", "finalize_node")
child_builder.add_edge("finalize_node", END)
# No checkpointer here on purpose — it must inherit the parent's checkpointer.
child_graph = child_builder.compile()


# --------------------------------------------------------------------------
# PARENT graph ("supervisor") — different state schema.
# --------------------------------------------------------------------------
class ParentState(TypedDict):
    messages: str
    result: str


def supervisor_node(state: ParentState) -> dict:
    log("[PARENT] supervisor_node EXECUTED")
    return {}


def adas_subgraph_node(state: ParentState, config) -> dict:
    child_input = {"child_input": state["messages"], "generated": "", "decision": ""}
    child_output = child_graph.invoke(child_input, config)
    log("[PARENT] adas_subgraph_node EXECUTED")
    return {"result": child_output.get("decision", "")}


parent_builder = StateGraph(ParentState)
parent_builder.add_node("supervisor_node", supervisor_node)
parent_builder.add_node("adas_subgraph_node", adas_subgraph_node)
parent_builder.add_edge(START, "supervisor_node")
parent_builder.add_edge("supervisor_node", "adas_subgraph_node")
parent_builder.add_edge("adas_subgraph_node", END)

checkpointer = MemorySaver()
parent_graph = parent_builder.compile(checkpointer=checkpointer)


def main() -> None:
    config = {"configurable": {"thread_id": "spike-thread-1"}}

    print("\n=== RUN 1 (expect pause at interrupt) ===")
    result_1 = parent_graph.invoke({"messages": "hello", "result": ""}, config)
    print(f"RUN 1 raw result: {result_1}")

    interrupts = result_1.get("__interrupt__")
    if interrupts:
        print(f"Interrupt surfaced to parent: {interrupts}")
    else:
        state_snapshot = parent_graph.get_state(config)
        print(f"Interrupt surfaced via get_state.tasks: {state_snapshot.tasks}")

    print("\n=== RUN 2 (resume) ===")
    result_2 = parent_graph.invoke(Command(resume="approve"), config)
    print(f"RUN 2 raw result: {result_2}")

    print("\n=== VERDICT ===")
    generate_count = EXECUTION_LOG.count("[CHILD] generate_node EXECUTED")
    after_interrupt_lines = [
        line for line in EXECUTION_LOG if line.startswith("[CHILD] review_node AFTER interrupt")
    ]
    finalize_count = EXECUTION_LOG.count("[CHILD] finalize_node EXECUTED")
    final_result = result_2.get("result")

    print(f"generate_node executions: {generate_count} "
          f"({'PASS - ran once' if generate_count == 1 else 'FAIL - re-executed'})")
    print(f"review_node AFTER interrupt occurrences: {len(after_interrupt_lines)} "
          f"({'PASS - resumed' if after_interrupt_lines else 'FAIL - never resumed'}) "
          f"-> {after_interrupt_lines}")
    print(f"finalize_node executions: {finalize_count} "
          f"({'PASS - ran once' if finalize_count == 1 else 'FAIL - unexpected count'})")
    print(f"parent result: {final_result!r} "
          f"({'PASS - state translated across boundary' if final_result == 'approve' else 'FAIL'})")

    overall_pass = (
        generate_count == 1
        and bool(after_interrupt_lines)
        and finalize_count == 1
        and final_result == "approve"
    )
    print(f"\nOVERALL: {'PASS' if overall_pass else 'FAIL'}")


if __name__ == "__main__":
    main()

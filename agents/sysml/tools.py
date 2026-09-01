"""Thin adapter: the SysML v2 tooling lives in tools/sysml_v2 (reusable outside this
agent); this module just re-exports what Layer-3's nodes need.
"""
from tools.sysml_v2.diagnostics import Diagnostic
from tools.sysml_v2.lsp_client import validate
from tools.sysml_v2.mcp_client import to_mermaid

__all__ = ["Diagnostic", "validate", "to_mermaid"]

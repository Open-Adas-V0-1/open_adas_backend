from pathlib import Path

from app.config import get_settings

_PACKAGE_ROOT = Path(__file__).resolve().parent  # tools/sysml_v2
_DEFAULT_SERVER_DIR = _PACKAGE_ROOT / "node_modules" / "sysml-v2-lsp" / "dist" / "server"


def resolve_node_bin() -> str:
    return get_settings().sysml_node_bin


def resolve_lsp_server_path() -> Path:
    configured = get_settings().sysml_lsp_server_path
    path = Path(configured) if configured else _DEFAULT_SERVER_DIR / "server.js"
    if not path.exists():
        raise FileNotFoundError(
            f"SysML LSP server not found at {path}. Run 'npm install' in tools/sysml_v2/, "
            "or set SYSML_LSP_SERVER_PATH."
        )
    return path


def resolve_mcp_server_path() -> Path:
    configured = get_settings().sysml_mcp_server_path
    path = Path(configured) if configured else _DEFAULT_SERVER_DIR / "mcpServer.js"
    if not path.exists():
        raise FileNotFoundError(
            f"SysML MCP server not found at {path}. Run 'npm install' in tools/sysml_v2/, "
            "or set SYSML_MCP_SERVER_PATH."
        )
    return path


def resolve_timeout() -> float:
    return get_settings().sysml_tooling_timeout

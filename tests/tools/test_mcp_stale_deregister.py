from unittest.mock import patch

from tools.mcp_tool import MCPServerTask
from tools.registry import registry
import tools.mcp_tool as mcp


def test_stale_server_cannot_deregister_replacement_tools():
    name = "ownership-regression"
    tool_name = f"mcp_{name}_tool"
    stale = MCPServerTask(name)
    replacement = MCPServerTask(name)
    stale._registered_tool_names = [tool_name]
    replacement._registered_tool_names = [tool_name]

    with registry.mutation_transaction():
        registry.register(
            name=tool_name,
            toolset=f"mcp-{name}",
            schema={"name": tool_name, "description": "test", "parameters": {"type": "object", "properties": {}}},
            handler=lambda _args: "ok",
        )
    try:
        with patch.dict(mcp._servers, {name: replacement}):
            stale._deregister_tools()
        assert registry.get_entry(tool_name) is not None
    finally:
        registry.deregister(tool_name)
        mcp._servers.pop(name, None)

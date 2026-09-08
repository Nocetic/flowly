"""Local, deterministic stdio peer for isolated-profile setup acceptance."""
from mcp.server.mcpserver import MCPServer

server = MCPServer("profile-setup-fixture")


@server.tool()
def read_fixture() -> str:
    """Read a fixed test value without accessing files or services."""
    return "fixture"


@server.tool()
def other_fixture() -> str:
    """Second tool used to verify explicitly selected permissions."""
    return "other"


if __name__ == "__main__":
    server.run()

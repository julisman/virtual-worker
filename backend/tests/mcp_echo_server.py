"""A trivial stdio MCP server used by the MCP client tests. Exposes one tool."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the input text back."""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run()  # stdio transport by default

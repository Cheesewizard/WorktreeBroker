"""Use the worktree broker from a chat that has not reloaded its MCP tools."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from broker import HERE, SYNC_TOOLS, UI_TOOLS, call_ui_tool, capabilities, check_development_branch, load_route
from mcp import types


async def call(root, route, tool, arguments):
    if tool in UI_TOOLS:
        # In-process, under the same main-editor lock as the MCP server: skips starting a second broker process.
        if route.get("development_branch"):
            check_development_branch(route)
        try:
            content = await call_ui_tool(tool, arguments, root, route["name"])
        except Exception as error:  # report like the MCP server does
            content, failed = [types.TextContent(type="text", text=str(error))], True
        else:
            failed = False
        print(types.CallToolResult(content=content, isError=failed).model_dump_json(indent=2))
        return 1 if failed else 0
    if tool in ("integrate", "integrate_list"):
        import integrate
        print(json.dumps(integrate.run(**arguments) if tool == "integrate" else integrate.list_integrations(),
                         indent=2, default=str))
        return 0
    if tool in SYNC_TOOLS:
        print(json.dumps(SYNC_TOOLS[tool](root, **arguments), indent=2))
        return 0
    if tool == "worktree_capabilities":
        if arguments:
            raise ValueError("worktree_capabilities takes no arguments")
        print(json.dumps(capabilities(root, route), indent=2))
        return 0
    server = StdioServerParameters(
        command=sys.executable, args=[str(HERE / "broker.py"), "--worktree", str(root)],
        cwd=str(root),
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if tool == "list":
                result = await session.list_tools()
                print(result.model_dump_json(indent=2))
                return 0
            result = await session.call_tool(tool, arguments)
            print(result.model_dump_json(indent=2))
            return 1 if result.isError else 0


if __name__ == "__main__":
    # Unity MCP's tool descriptions contain non-cp1252 characters (e.g. arrows); print UTF-8 on Windows consoles.
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", type=Path, default=Path.cwd())
    parser.add_argument("tool")
    parser.add_argument("--arguments", type=json.loads, default={})
    args = parser.parse_args()
    root, route = load_route(args.worktree)
    raise SystemExit(asyncio.run(call(root, route, args.tool, args.arguments)))

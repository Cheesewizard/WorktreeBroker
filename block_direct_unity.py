"""Claude Code PreToolUse hook: refuse direct Unity MCP calls (mcp__<unity_mcp.direct_server>__*, default unityMCP) in the project.

The project's editor is shared by several chats; only the broker locks it, names who holds it and cuts off hung calls.
A chat that meets a busy lock may otherwise switch to direct Unity MCP and work around every other chat. The Claude
desktop app gives every project a direct `unityMCP` server, so this hook (in the project's .claude/settings.json,
written by `install.py claude`) is what keeps the project's chats on the broker. Codex already has unityMCP
switched off in .codex/config.toml (install_worktree_defaults.py).
"""

import json
import sys

from broker_state import DIRECT_UNITY_MCP, MCP_SERVER, PROJECT_NAME

REASON = (
    f"{PROJECT_NAME} forbids direct Unity MCP: use the broker's tool of the same name (mcp__{MCP_SERVER}__<tool>), "
    "which shares the editor safely with other chats. If the broker says the editor is busy or a call timed out, wait "
    "and retry once; if it still fails, stop and tell the user exactly what the broker said. Never work around the broker."
)


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    if not str(payload.get("tool_name", "")).startswith(f"mcp__{DIRECT_UNITY_MCP}__"):
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": REASON,
    }}))


if __name__ == "__main__":
    main()

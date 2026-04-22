# K8gentS MCP Server Integration

## What This Adds

This adds an MCP (Model Context Protocol) server to K8gentS, exposing your
existing diagnostic brain as tools that any AI agent can call on demand.

```
┌──────────────────────────────────────────────────────────────┐
│                      K8gentS Core                            │
│                                                              │
│   Kubernetes API  →  Log Collection  →  Sanitisation  →  LLM │
│                                                              │
├────────────────────┬─────────────────────────────────────────┤
│   main.py          │   mcp_server.py                         │
│   (always-on)      │   (on-demand)                           │
│                    │                                         │
│   Watches cluster  │   Waits for agent requests              │
│   Pushes to Slack  │   Returns structured diagnostics        │
│   Auto-detects     │   Any AI client can query:              │
│   failures         │   Claude, Cursor, Gemini CLI, etc.      │
└────────────────────┴─────────────────────────────────────────┘
```

## File Placement

Drop `mcp_server.py` into your existing `src/` directory:

```
K8gentS/
├── src/
│   ├── main.py           # existing — Slack watcher (unchanged)
│   └── mcp_server.py     # NEW — MCP server entry point
├── deploy/
├── prompts/
├── Dockerfile
└── README.md
```

## MCP Tools Exposed

| Tool                  | What it does                                          |
|-----------------------|-------------------------------------------------------|
| `diagnose_pod`        | Full RCA on a specific pod (logs + events + LLM)      |
| `get_failing_pods`    | Scan for CrashLoopBackOff, OOMKilled, etc.            |
| `get_cluster_health`  | Node status, resource pressure, pod phase summary     |
| `get_namespace_events`| Recent events with optional Warning filter            |
| `check_pod_resources` | Resource requests/limits for OOM/throttle diagnosis   |

## Quick Start

### 1. Install the MCP SDK

```bash
pip install "mcp[cli]" kubernetes google-generativeai
```

### 2. Run Locally (stdio mode, for Claude Desktop)

```bash
export AI_API_KEY="your-gemini-key"
python src/mcp_server.py
```

### 3. Connect to Claude Desktop

Add to your Claude Desktop config (`~/.claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "k8gents": {
      "command": "python",
      "args": ["src/mcp_server.py"],
      "env": {
        "AI_API_KEY": "your-gemini-key",
        "KUBECONFIG": "/path/to/your/kubeconfig"
      }
    }
  }
}
```

### 4. Test with MCP Inspector

```bash
mcp dev src/mcp_server.py
```

Then open the Inspector UI and try calling `get_cluster_health`.

## What You Can Do With It

Once connected, you can ask any MCP-compatible AI assistant things like:

- "Are there any failing pods in the staging namespace?"
- "Run a root cause analysis on pod web-api-7b9f4 in production"
- "Show me the cluster health — I'm about to deploy"
- "What Warning events happened in kube-system in the last hour?"

The agent calls your MCP tools, gets structured data back, and reasons
over it to give you a natural-language answer.

## How This Helps Your Resume

This turns K8gentS from a standalone Slack bot into an **MCP-compatible
Kubernetes diagnostic service** — the protocol created by Anthropic and
now hosted by the Linux Foundation. After adding this:

1. Publish the MCP server to the community servers list at
   `modelcontextprotocol/servers` on GitHub
2. Mention it in your K8gentS README
3. You now have a contribution in Anthropic's core open-source ecosystem

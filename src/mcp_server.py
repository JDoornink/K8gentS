"""
K8gentS MCP Server
==================
Exposes the K8gentS root-cause-analysis brain as MCP tools that any
AI agent (Claude Desktop, Cursor, Gemini CLI, etc.) can call on demand.

Drop this file into your K8gentS  src/  directory alongside main.py.
Both entry points share the same underlying modules — main.py is the
always-on Slack watcher; this file is the on-demand diagnostic API.

Prerequisites:
    pip install "mcp[cli]" kubernetes google-generativeai

Run locally (stdio, for Claude Desktop / MCP Inspector):
    python src/mcp_server.py

Run as HTTP (for remote agents):
    python src/mcp_server.py --transport streamable-http --port 8080
"""

import os
import re
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from mcp.server.fastmcp import FastMCP
from kubernetes import client, config

# ---------------------------------------------------------------------------
# Initialise MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="k8gents",
    instructions=(
        "K8gentS is a Kubernetes root-cause-analysis agent. "
        "Use these tools to diagnose pod failures, inspect cluster health, "
        "and get AI-powered resolution recommendations."
    ),
)

# ---------------------------------------------------------------------------
# Kubernetes client (in-cluster or local kubeconfig)
# ---------------------------------------------------------------------------
try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

core_v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()

# ---------------------------------------------------------------------------
# AI / LLM configuration  (reuses your existing AI_API_KEY secret)
# ---------------------------------------------------------------------------
AI_MODEL = os.getenv("AI_MODEL", "gemini-2.5-pro")

# ---------------------------------------------------------------------------
# Log sanitisation  (mirrors your existing regex sweeper in main.py)
# ---------------------------------------------------------------------------
def sanitize(text: str) -> str:
    """Strips sensitive data (PII, secrets, tokens, internal IPs) before sending to LLM."""
    if not text:
        return ""
    text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[REDACTED_IP]', text)
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b', '[REDACTED_EMAIL]', text)
    text = re.sub(r'eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*', '[REDACTED_JWT]', text)
    text = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9\-\._~+]+', r'\1[REDACTED_TOKEN]', text)
    text = re.sub(r'(?i)(api[_\-]?key)["\':= ]+[A-Za-z0-9\-\._~+]+', r'\1=[REDACTED_API_KEY]', text)
    text = re.sub(r'(?i)(password)["\':= ]+[^\s,;]+', r'\1=[REDACTED_PASSWORD]', text)
    return text


# ---------------------------------------------------------------------------
# Helper: collect diagnostic context for a single pod
# ---------------------------------------------------------------------------
def _collect_pod_context(namespace: str, pod_name: str) -> dict:
    """Gather logs, events, and status for a pod — the same context
    your main.py watcher collects before calling the LLM."""

    ctx: dict = {"pod": pod_name, "namespace": namespace}

    # --- Pod status & container statuses ---
    try:
        pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        ctx["phase"] = pod.status.phase
        ctx["conditions"] = [
            {"type": c.type, "status": c.status, "reason": c.reason}
            for c in (pod.status.conditions or [])
        ]
        ctx["container_statuses"] = []
        for cs in pod.status.container_statuses or []:
            entry = {
                "name": cs.name,
                "ready": cs.ready,
                "restart_count": cs.restart_count,
            }
            if cs.state.waiting:
                entry["state"] = "waiting"
                entry["reason"] = cs.state.waiting.reason
                entry["message"] = cs.state.waiting.message
            elif cs.state.terminated:
                entry["state"] = "terminated"
                entry["reason"] = cs.state.terminated.reason
                entry["exit_code"] = cs.state.terminated.exit_code
            else:
                entry["state"] = "running"
            ctx["container_statuses"].append(entry)
    except client.ApiException as exc:
        ctx["error"] = f"Failed to read pod: {exc.status} {exc.reason}"
        return ctx

    # --- Recent logs (last 100 lines, sanitised) ---
    try:
        logs = core_v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=100,
            timestamps=True,
        )
        ctx["logs"] = sanitize(logs)
    except client.ApiException:
        ctx["logs"] = "(unable to retrieve logs)"

    # --- Events ---
    try:
        events = core_v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name}",
        )
        ctx["events"] = [
            {
                "reason": e.reason,
                "message": sanitize(e.message or ""),
                "count": e.count,
                "last_seen": e.last_timestamp.isoformat() if e.last_timestamp else None,
            }
            for e in events.items[-20:]  # last 20 events
        ]
    except client.ApiException:
        ctx["events"] = []

    return ctx


# ---------------------------------------------------------------------------
# Helper: call the AI reasoning engine  (same flow as your main.py)
# ---------------------------------------------------------------------------
async def _run_rca(context: dict) -> str:
    """Send collected context to the LLM and return a root-cause report."""
    try:
        from google import genai

        client = genai.Client(api_key=os.environ["AI_API_KEY"])

        prompt = (
            "You are K8gentS, an expert Kubernetes SRE assistant with "
            "CKA/CKS-level knowledge. Analyse the following diagnostic "
            "context and provide:\n"
            "1. A concise summary of the problem.\n"
            "2. Top 3 most likely root causes with confidence scores.\n"
            "3. Step-by-step resolution instructions for each cause.\n\n"
            f"Diagnostic context:\n{json.dumps(context, indent=2, default=str)}"
        )
        response = client.models.generate_content(
            model=AI_MODEL,
            contents=prompt,
        )
        return response.text
    except Exception as exc:
        return f"AI analysis unavailable: {exc}"


# ===================================================================
#  MCP TOOLS — these are the capabilities agents will discover & call
# ===================================================================


@mcp.tool()
async def diagnose_pod(namespace: str, pod_name: str) -> str:
    """Run a full root-cause analysis on a specific Kubernetes pod.

    Collects logs, events, and container status, sanitises secrets,
    then sends the context to an LLM for expert diagnosis.

    Args:
        namespace: The Kubernetes namespace the pod lives in.
        pod_name:  The exact name of the pod to diagnose.
    """
    ctx = _collect_pod_context(namespace, pod_name)
    if "error" in ctx:
        return json.dumps(ctx, indent=2)
    rca = await _run_rca(ctx)
    return rca


@mcp.tool()
async def get_failing_pods(namespace: Optional[str] = None) -> str:
    """List all pods that are currently in a failure state.

    Scans for CrashLoopBackOff, OOMKilled, ImagePullBackOff, Error,
    and other non-Running states across the cluster or a single namespace.

    Args:
        namespace: Optional namespace to scope the search.
                   Omit to scan all namespaces.
    """
    FAILURE_REASONS = {
        "CrashLoopBackOff", "OOMKilled", "Error", "ImagePullBackOff",
        "ErrImagePull", "CreateContainerConfigError", "RunContainerError",
        "InvalidImageName", "ContainerCannotRun",
    }
    failing = []

    if namespace:
        pods = core_v1.list_namespaced_pod(namespace=namespace)
    else:
        pods = core_v1.list_pod_for_all_namespaces()

    for pod in pods.items:
        issues = []
        for cs in pod.status.container_statuses or []:
            if cs.state.waiting and cs.state.waiting.reason in FAILURE_REASONS:
                issues.append(f"{cs.name}: {cs.state.waiting.reason}")
            elif cs.state.terminated and cs.state.terminated.reason in FAILURE_REASONS:
                issues.append(f"{cs.name}: {cs.state.terminated.reason} (exit {cs.state.terminated.exit_code})")
            elif cs.restart_count and cs.restart_count > 5:
                issues.append(f"{cs.name}: high restart count ({cs.restart_count})")

        if issues:
            failing.append({
                "namespace": pod.metadata.namespace,
                "pod": pod.metadata.name,
                "issues": issues,
            })

    if not failing:
        scope = namespace or "all namespaces"
        return f"No failing pods found in {scope}."
    return json.dumps(failing, indent=2)


@mcp.tool()
async def get_cluster_health() -> str:
    """Get a high-level health snapshot of the Kubernetes cluster.

    Returns node status, resource pressure conditions, and a summary
    of pod states across all namespaces.
    """
    # --- Node health ---
    nodes = core_v1.list_node()
    node_info = []
    for node in nodes.items:
        conditions = {
            c.type: c.status for c in node.status.conditions or []
        }
        node_info.append({
            "name": node.metadata.name,
            "ready": conditions.get("Ready", "Unknown"),
            "memory_pressure": conditions.get("MemoryPressure", "Unknown"),
            "disk_pressure": conditions.get("DiskPressure", "Unknown"),
            "pid_pressure": conditions.get("PIDPressure", "Unknown"),
        })

    # --- Pod summary ---
    all_pods = core_v1.list_pod_for_all_namespaces()
    phase_counts: dict[str, int] = {}
    for pod in all_pods.items:
        phase = pod.status.phase or "Unknown"
        phase_counts[phase] = phase_counts.get(phase, 0) + 1

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "nodes": node_info,
        "pod_summary": phase_counts,
        "total_pods": len(all_pods.items),
    }
    return json.dumps(report, indent=2)


@mcp.tool()
async def get_namespace_events(
    namespace: str,
    event_type: Optional[str] = None,
) -> str:
    """Fetch recent Kubernetes events for a namespace.

    Useful for spotting scheduling failures, image pull errors,
    DNS issues, and other cluster-level problems.

    Args:
        namespace:  The namespace to query events from.
        event_type: Optional filter — 'Warning' or 'Normal'.
    """
    events = core_v1.list_namespaced_event(namespace=namespace)
    filtered = []
    for e in events.items[-50:]:  # last 50 events
        if event_type and e.type != event_type:
            continue
        filtered.append({
            "type": e.type,
            "reason": e.reason,
            "object": f"{e.involved_object.kind}/{e.involved_object.name}",
            "message": sanitize(e.message or ""),
            "count": e.count,
            "last_seen": e.last_timestamp.isoformat() if e.last_timestamp else None,
        })

    if not filtered:
        return f"No {'Warning ' if event_type == 'Warning' else ''}events in {namespace}."
    return json.dumps(filtered, indent=2)


@mcp.tool()
async def check_pod_resources(namespace: str, pod_name: str) -> str:
    """Check resource requests, limits, and actual usage for a pod.

    Helps diagnose OOMKilled, CPU throttling, and resource-quota issues.

    Args:
        namespace: The Kubernetes namespace.
        pod_name:  The pod name to inspect.
    """
    try:
        pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
    except client.ApiException as exc:
        return f"Error reading pod: {exc.status} {exc.reason}"

    containers = []
    for c in pod.spec.containers:
        containers.append({
            "name": c.name,
            "image": c.image,
            "requests": dict(c.resources.requests) if c.resources and c.resources.requests else {},
            "limits": dict(c.resources.limits) if c.resources and c.resources.limits else {},
        })
    return json.dumps({"pod": pod_name, "namespace": namespace, "containers": containers}, indent=2)


# ===================================================================
#  MCP RESOURCES — read-only context an agent can pull into its window
# ===================================================================

@mcp.resource("k8gents://prompts/rca-template")
def rca_prompt_template() -> str:
    """The standard RCA prompt template used by K8gentS."""
    return (
        "You are K8gentS, an expert Kubernetes SRE assistant with "
        "CKA/CKS-level knowledge. Analyse the following diagnostic "
        "context and provide:\n"
        "1. A concise summary of the problem.\n"
        "2. Top 3 most likely root causes with confidence scores.\n"
        "3. Step-by-step resolution instructions for each cause."
    )


# ===================================================================
#  Entry point
# ===================================================================
if __name__ == "__main__":
    mcp.run()

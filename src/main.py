import os
import logging
import re
import json
import uuid
import subprocess
from datetime import datetime, timedelta
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from google import genai
from google.genai import types
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import threading

# Secure in-memory store: maps incident_id → full rca_data dict.
# Decouples LLM generation from asynchronous human approval.
pending_fixes = {}
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("K8gentRCA")

# Module-level reference so Slack action handlers can call agent methods.
# Set to the RCAAgent instance in __main__ before starting the Slack handler.
agent_instance = None


def _load_allowed_approvers():
    """Return the set of approved Slack user IDs, or an empty set if no allowlist is configured.

    Resolution order:
    1. ALLOWED_APPROVERS_FILE env var → path to a local file (one user ID per line).
       Use this for local dev so the list never touches source control.
    2. ALLOWED_APPROVERS env var → comma-separated user IDs.
       Set via Kubernetes Secret for in-cluster deployments.
    3. Neither set → no allowlist (any channel member may approve).

    File format (approvers.local):
        # Lines starting with # are ignored
        U06ABC1234
        U09XYZ5678
    """
    file_path = os.environ.get("ALLOWED_APPROVERS_FILE", "")
    if file_path:
        try:
            with open(file_path, "r") as f:
                ids = {
                    line.strip()
                    for line in f
                    if line.strip() and not line.strip().startswith("#")
                }
            return ids
        except OSError as e:
            logger.warning(f"Could not read ALLOWED_APPROVERS_FILE '{file_path}': {e}")

    raw = os.environ.get("ALLOWED_APPROVERS", "")
    if raw:
        return {u.strip() for u in raw.split(",") if u.strip()}

    return set()

# Initialize Slack App
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

@app.action("approve_fix")
def handle_approve_fix(ack, body, logger):
    ack()
    user = body["user"]["id"]
    incident_id = body["actions"][0]["value"]
    channel_id = body["channel"]["id"]
    message_ts = body["message"]["ts"]

    # --- APPROVER ALLOWLIST ---
    allowed = _load_allowed_approvers()
    if allowed and user not in allowed:
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=f"⛔ <@{user}> is not authorized to approve remediations. Contact your on-call team."
        )
        return

    rca_data = pending_fixes.get(incident_id)
    if not rca_data:
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=f"❌ No fix data found for incident `{incident_id}`. It may have already been acted on."
        )
        return

    # Lock the action buttons immediately to prevent double-execution
    try:
        original_blocks = body["message"]["blocks"]
        updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
        updated_blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"🚀 *Execution approved and locked by <@{user}>.*"}]
        })
        app.client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=body["message"].get("text", "K8s Alert update"),
            blocks=updated_blocks
        )
    except Exception as e:
        logger.warning(f"Could not update message blocks: {e}")

    remediation_mode = os.environ.get("REMEDIATION_MODE", "api")
    kubectl_command = rca_data.get("kubectl_command", "")
    display_action = kubectl_command or f"{rca_data.get('remediation_action')} → {rca_data.get('remediation_target_name')}"

    app.client.chat_postMessage(
        channel=channel_id,
        thread_ts=message_ts,
        text=f"<@{user}> approved execution. Agent executing: `{display_action}`"
    )

    try:
        if remediation_mode == "subprocess":
            # Local dev fallback: shell out to kubectl using the active local kubeconfig.
            # NOT safe for in-cluster use (no binary, no kubeconfig, readOnlyRootFilesystem).
            if not kubectl_command:
                raise ValueError("No kubectl_command available for subprocess mode.")
            process = subprocess.run(
                kubectl_command,
                shell=True,
                check=True,
                capture_output=True,
                text=True
            )
            result_text = process.stdout.strip() or "Command completed with no output."
        else:
            # Default (api) mode: use the Kubernetes Python client directly.
            # Works in-cluster via ServiceAccount token. No binary or kubeconfig needed.
            result_text = agent_instance.execute_remediation_api(rca_data)

        # Consume the fix entry to prevent replay
        pending_fixes.pop(incident_id, None)

        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=f"✅ Fix applied successfully! 🛠️\n*Result:*\n```\n{result_text}\n```"
        )
    except subprocess.CalledProcessError as e:
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=f"❌ kubectl execution failed.\n*Error:*\n```\n{e.stderr.strip()}\n```"
        )
    except ApiException as e:
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=f"❌ Kubernetes API rejected the action (HTTP {e.status}): {e.reason}"
        )
    except Exception as e:
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=f"❌ Remediation failed unexpectedly: {e}"
        )

@app.action("forward_message")
def handle_forward_message(ack, body, logger):
    ack()
    trigger_id = body["trigger_id"]

    try:
        alert_text = body["message"]["blocks"][0]["text"]["text"]
    except (KeyError, IndexError):
        alert_text = "Kubernetes Alert (Content could not be parsed)"

    meta = json.dumps({
        "channel_id": body["channel"]["id"],
        "message_ts": body["message"]["ts"],
        "incident_id": body["actions"][0]["value"],
        "alert_text": alert_text
    })

    app.client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": "forward_modal_submit",
            "private_metadata": meta,
            "title": {"type": "plain_text", "text": "Forward Alert"},
            "submit": {"type": "plain_text", "text": "Forward"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "user_selection_block",
                    "element": {
                        "type": "multi_users_select",
                        "action_id": "selected_users_action",
                        "placeholder": {"type": "plain_text", "text": "Select colleagues..."}
                    },
                    "label": {"type": "plain_text", "text": "Forward to users:"}
                }
            ]
        }
    )

@app.view("forward_modal_submit")
def handle_forward_submit(ack, body, view, logger):
    ack()
    user_who_forwarded = body["user"]["id"]
    selected_users = view["state"]["values"]["user_selection_block"]["selected_users_action"]["selected_users"]
    meta = json.loads(view["private_metadata"])

    try:
        original_alert_text = meta.get("alert_text", "Kubernetes Alert")
        sent_to = []
        failed_to = []

        for target_user in selected_users:
            try:
                app.client.chat_postMessage(
                    channel=target_user,
                    text=f"<@{user_who_forwarded}> forwarded an Alert",
                    blocks=[{
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*Forwarded by* <@{user_who_forwarded}>:\n\n{original_alert_text}"}
                    }]
                )
                sent_to.append(f"<@{target_user}>")
            except Exception as user_e:
                logger.error(f"Failed to forward message to {target_user}: {user_e}")
                failed_to.append(f"<@{target_user}>")

        report_text = f"✅ <@{user_who_forwarded}> forwarded this alert to: {', '.join(sent_to)}"
        if failed_to:
            report_text += f"\n⚠️ *Failed to reach:* {', '.join(failed_to)}"

        app.client.chat_postMessage(
            channel=meta["channel_id"],
            thread_ts=meta["message_ts"],
            text=report_text
        )
    except Exception as e:
        logger.error(f"Failed to process forward request: {e}")

@app.action("disregard_alert")
def handle_disregard_alert(ack, body, logger):
    ack()
    user = body["user"]["id"]

    try:
        original_blocks = body["message"]["blocks"]
        updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
        updated_blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"🛑 *Alert officially disregarded by <@{user}>.*"}]
        })
        app.client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=body["message"].get("text", "K8s Alert update"),
            blocks=updated_blocks
        )
    except Exception as e:
        logger.warning(f"Could not update message blocks to remove buttons: {e}")

    app.client.chat_postMessage(
        channel=body["channel"]["id"],
        thread_ts=body["message"]["ts"],
        text=f"<@{user}> Disregarded the alert. Agent will stand down."
    )


class RCAAgent:
    def __init__(self):
        try:
            config.load_incluster_config()
            logger.info("Loaded in-cluster K8s configuration.")
        except config.config_exception.ConfigException:
            config.load_kube_config()
            logger.info("Loaded local kubeconfig file.")

        self.v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()

        self.ai_client = genai.Client(api_key=os.environ.get("AI_API_KEY"))
        self.ai_model = os.environ.get("AI_MODEL", "gemini-2.5-pro")

        # Rate Limiting & Debouncing Cache: Prevents the Agent from bankrupting your token budget.
        # Maps "Namespace/Kind/Name:Reason" to Timestamp
        self.alert_cache = {}
        self.hourly_alerts = 0
        self.hourly_reset_time = datetime.now() + timedelta(hours=1)

        slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
        slack_app_token = os.environ.get("SLACK_APP_TOKEN")
        if not slack_bot_token or not slack_app_token:
            logger.warning("Missing SLACK_BOT_TOKEN or SLACK_APP_TOKEN in environment variables.")

    def run_watcher(self):
        watch_namespace = os.environ.get("WATCH_NAMESPACE")

        if watch_namespace:
            logger.info(f"Starting K8gent-S Watcher (Restricted to namespace: {watch_namespace})...")
            stream = watch.Watch().stream(self.v1.list_namespaced_event, namespace=watch_namespace)
        else:
            logger.info("Starting K8gent-S Watcher (Global Scope)...")
            stream = watch.Watch().stream(self.v1.list_event_for_all_namespaces)

        for event in stream:
            event_obj = event['object']
            if event_obj.type == "Warning":
                self.handle_error_event(event_obj)

    def run(self):
        watcher_thread = threading.Thread(target=self.run_watcher)
        watcher_thread.daemon = True
        watcher_thread.start()

        logger.info("Starting Slack Socket Mode...")
        SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()

    def handle_error_event(self, event_obj):
        reason = event_obj.reason
        message = event_obj.message
        namespace = event_obj.metadata.namespace
        involved_object = event_obj.involved_object

        if reason in ["FailedScheduling", "Unhealthy"]:
            return

        cache_key = f"{namespace}/{involved_object.kind}/{involved_object.name}:{reason}"

        now = datetime.now()

        if now > self.hourly_reset_time:
            self.hourly_alerts = 0
            self.hourly_reset_time = now + timedelta(hours=1)

        if self.hourly_alerts >= 10:
            logger.warning("Global K8gent RCA rate limit hit (10/hr). Dropping event to save tokens.")
            return

        if cache_key in self.alert_cache:
            last_alerted = self.alert_cache[cache_key]
            if now < last_alerted + timedelta(minutes=15):
                logger.debug(f"Event {cache_key} debounced. Skipping LLM call.")
                return

        self.alert_cache[cache_key] = now
        self.hourly_alerts += 1

        logger.info(f"Anomaly detected! Reason: {reason}, Object: {involved_object.name}")

        context = ""
        if involved_object.kind == "Pod":
            context = self.gather_pod_context(involved_object.name, namespace)

        rca_result = self.generate_rca(reason, message, context, involved_object)
        self.send_slack_notification(rca_result, involved_object)

    def _get_owner_deployment(self, pod_name, namespace):
        """Traverse Pod → ReplicaSet → Deployment to find the managing Deployment name.
        Returns the Deployment name string, or None if not found."""
        try:
            pod = self.v1.read_namespaced_pod(name=pod_name, namespace=namespace)
            for ref in (pod.metadata.owner_references or []):
                if ref.kind == "ReplicaSet":
                    rs = self.apps_v1.read_namespaced_replica_set(name=ref.name, namespace=namespace)
                    for rs_ref in (rs.metadata.owner_references or []):
                        if rs_ref.kind == "Deployment":
                            return rs_ref.name
        except ApiException:
            pass
        return None

    def gather_pod_context(self, pod_name, namespace):
        context = []
        try:
            pod = self.v1.read_namespaced_pod(name=pod_name, namespace=namespace)
            container_names = [c.name for c in pod.spec.containers]
            context.append(f"--- POD CONTAINER NAMES ---\n{', '.join(container_names)}")
        except ApiException as e:
            context.append(f"Failed to fetch pod spec: {e.reason}")

        # Traverse ownership chain so the LLM knows the managing Deployment name
        # (needed for set_image and rollout_restart remediation actions)
        deployment_name = self._get_owner_deployment(pod_name, namespace)
        if deployment_name:
            context.append(f"--- OWNER DEPLOYMENT ---\n{deployment_name}")

        try:
            logs = self.v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, tail_lines=50)
            if not logs.strip():
                try:
                    logs = self.v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, tail_lines=50, previous=True)
                except ApiException:
                    pass
            sanitized_logs = self.sanitize_logs(logs)
            context.append(f"--- POD LOGS (tail 50) ---\n{sanitized_logs}")
        except ApiException as e:
            context.append(f"Failed to fetch logs: {e.reason}")

        return "\n".join(context)

    def sanitize_logs(self, raw_logs):
        """Strips sensitive data (PII, secrets, tokens, internal IPs) before sending to LLM."""
        if not raw_logs:
            return ""
        logs = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[REDACTED_IP]', raw_logs)
        logs = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b', '[REDACTED_EMAIL]', logs)
        logs = re.sub(r'eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*', '[REDACTED_JWT]', logs)
        logs = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9\-\._~+]+', r'\1[REDACTED_TOKEN]', logs)
        logs = re.sub(r'(?i)(api[_\-]?key)["\':= ]+[A-Za-z0-9\-\._~+]+', r'\1=[REDACTED_API_KEY]', logs)
        logs = re.sub(r'(?i)(password)["\':= ]+[^\s,;]+', r'\1=[REDACTED_PASSWORD]', logs)
        return logs

    def generate_rca(self, reason, message, context, involved_object):
        truncated_context = context[:5000] if len(context) > 5000 else context

        prompt = f"""
You are a Senior Site Reliability Engineer (SRE) with Kubernetes credentials (CKA, CKS) specialized in Automated Root Cause Analysis (RCA). Your task is to analyze raw system logs, identify the underlying failure, and provide a remediation plan.

Input Context:
Reason: {reason}
Message: {message}
Involved Object: {involved_object.kind}/{involved_object.name}
Namespace: {involved_object.namespace}
Logs/Events:
{truncated_context}

Environment: Kubernetes Cluster (Production)

Analysis Requirements:
Categorization: Classify the error (e.g., OOMKilled, CrashLoopBackOff, Connectivity/DNS, Database Deadlock, Secret/Config Missing).
Root Cause: Explain why the error occurred based strictly on the log patterns. IMPORTANT: Wrap any `kubectl` commands, object names, or variables in backticks so they are highlighted in Slack.
Suggested Fix: Provide a step-by-step technical resolution. IMPORTANT: Wrap any inline `kubectl` commands, file paths, or variable names in backticks for readability.
Kubectl Command: If applicable, formulate a single exact, executable shell `kubectl` command that cleanly remedies the issue. CRITICAL RULES: (1) You MUST always include the `-n {involved_object.namespace}` namespace flag. (2) ALWAYS prefer simple high-level subcommands: use `kubectl set image` for image issues, `kubectl delete pod` for crash loops, `kubectl rollout restart` for config reloads. (3) NEVER use `kubectl patch` with JSON arrays or --type=json. (4) If the fix requires ANY manual human steps first (creating secrets, editing YAML, typing passwords), leave this field completely empty.
Confidence Score: Assign a percentage (0-100) indicating your certainty in the fix.
Crucial: If your Confidence Score is less than 75, you must set escalation_required to true.

Structured Remediation Fields (for automated API execution — fill these in alongside the kubectl_command):
- remediation_action: Choose exactly one: "delete_pod" (for CrashLoopBackOff — delete the pod and let K8s recreate it), "set_image" (for ErrImagePull or wrong image tag), "rollout_restart" (for config/env reload), or "none" (if the fix requires manual human steps or you have low confidence).
- remediation_target_name: The exact name of the resource to act on. Use the pod name for "delete_pod". Use the OWNER DEPLOYMENT name from context (not the pod name) for "set_image" and "rollout_restart".
- remediation_target_namespace: Must always be "{involved_object.namespace}".
- remediation_container_name: The container name to update (from POD CONTAINER NAMES in context). Only for "set_image"; empty string otherwise.
- remediation_new_image: The corrected image:tag to apply. Only for "set_image"; empty string otherwise. Choose a stable public tag (e.g., "nginx:stable").

Output Format (Strict JSON):
{{
  "incident_id": "RCA-XXXX",
  "category": "String",
  "root_cause": "String",
  "suggested_fix": "String",
  "kubectl_command": "String",
  "remediation_action": "delete_pod | set_image | rollout_restart | none",
  "remediation_target_name": "String",
  "remediation_target_namespace": "String",
  "remediation_container_name": "String",
  "remediation_new_image": "String",
  "confidence_score": 0,
  "escalation_required": false
}}
"""

        try:
            response = self.ai_client.models.generate_content(
                model=self.ai_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction="You are an expert Kubernetes AI assistant. Always return ONLY valid JSON.",
                    temperature=0.2,
                    response_mime_type="application/json",
                )
            )
            ai_text = response.text

            json_str = ai_text.strip()
            if json_str.startswith("```json"):
                json_str = json_str[7:]
            elif json_str.startswith("```"):
                json_str = json_str[3:]
            if json_str.endswith("```"):
                json_str = json_str[:-3]
            json_str = json_str.strip()

            rca_data = json.loads(json_str)
        except Exception as e:
            logger.error(f"Failed to query AI or parse JSON: {e}")
            rca_data = {
                "incident_id": "UNKNOWN",
                "category": "Error",
                "root_cause": f"AI Diagnostic failed. Exception: {e}",
                "suggested_fix": "Investigate manually.",
                "kubectl_command": "",
                "remediation_action": "none",
                "remediation_target_name": "",
                "remediation_target_namespace": involved_object.namespace,
                "remediation_container_name": "",
                "remediation_new_image": "",
                "confidence_score": 0,
                "escalation_required": True
            }

        return {
            "rca_data": rca_data,
            "object_ref": f"{involved_object.kind}/{involved_object.name} in {involved_object.namespace}",
        }

    def execute_remediation_api(self, rca_data):
        """Execute remediation using the Kubernetes Python client.
        Safe for in-cluster use — no subprocess, no shell, no kubeconfig file needed."""
        action = rca_data.get("remediation_action", "none")
        target_name = rca_data.get("remediation_target_name", "")
        target_ns = rca_data.get("remediation_target_namespace", "default")

        if not target_name:
            raise ValueError(f"No remediation_target_name provided for action '{action}'.")

        if action == "delete_pod":
            self.v1.delete_namespaced_pod(name=target_name, namespace=target_ns)
            return f"Deleted pod `{target_name}` in `{target_ns}`. Kubernetes will recreate it from its controller."

        elif action == "set_image":
            container_name = rca_data.get("remediation_container_name", "")
            new_image = rca_data.get("remediation_new_image", "")
            if not container_name or not new_image:
                raise ValueError("set_image requires both remediation_container_name and remediation_new_image.")
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            # Strategic merge patch: merges by container name, leaving other containers untouched
                            "containers": [{"name": container_name, "image": new_image}]
                        }
                    }
                }
            }
            self.apps_v1.patch_namespaced_deployment(name=target_name, namespace=target_ns, body=patch)
            return f"Updated container `{container_name}` in deployment `{target_name}` to image `{new_image}`."

        elif action == "rollout_restart":
            # Mirrors what `kubectl rollout restart` does internally
            now = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
            patch = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": now
                            }
                        }
                    }
                }
            }
            self.apps_v1.patch_namespaced_deployment(name=target_name, namespace=target_ns, body=patch)
            return f"Triggered rolling restart of deployment `{target_name}` in `{target_ns}`."

        else:
            raise ValueError(f"Unsupported remediation action: '{action}'. Manual intervention required.")

    def send_slack_notification(self, rca_result, involved_object):
        if not os.environ.get("SLACK_CHANNEL_ID"):
            return

        rca_data = rca_result.get("rca_data", {})
        object_ref = rca_result["object_ref"]
        confidence = rca_data.get("confidence_score", 0)
        escalate = rca_data.get("escalation_required", True)

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*⚠️ K8s Alert:* `{object_ref}`\n"
                        f"*Category:* {rca_data.get('category', 'Unknown')}\n"
                        f"*Confidence Score:* {confidence}%\n\n"
                        f"*Root Cause:*\n{rca_data.get('root_cause', 'N/A')}\n\n"
                        f"*Suggested Fix:*\n{rca_data.get('suggested_fix', 'N/A')}"
                    )
                }
            }
        ]

        if confidence < 75 or escalate:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*🚨 ACTION_REQUIRED: ESCALATE_TO_HUMAN*\nConfidence score is too low for auto-remediation. Human-in-the-loop required."
                }
            })

        # Clean up kubectl_command markdown wrappers if present
        cmd = rca_data.get("kubectl_command", "")
        if cmd.startswith("```bash"):
            cmd = cmd.split("```bash")[1].split("```")[0].strip()
        elif cmd.startswith("```"):
            cmd = cmd.split("```")[1].split("```")[0].strip()
        rca_data["kubectl_command"] = cmd

        # Ensure namespace is always present as a fallback for the handler
        rca_data.setdefault("remediation_target_namespace", involved_object.namespace)

        # Store the full rca_data dict so handle_approve_fix can dispatch by mode
        unique_incident_id = str(uuid.uuid4())
        pending_fixes[unique_incident_id] = rca_data

        # Determine if the agent can offer automated remediation
        remediation_mode = os.environ.get("REMEDIATION_MODE", "api")
        has_api_action = rca_data.get("remediation_action", "none") not in ("none", "", None)
        has_subprocess_cmd = bool(cmd)
        can_remediate = (remediation_mode == "api" and has_api_action) or \
                        (remediation_mode == "subprocess" and has_subprocess_cmd)

        action_elements = []
        if can_remediate:
            action_elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": "Approve the LLM to fix it"},
                "style": "danger",
                "value": unique_incident_id,
                "action_id": "approve_fix"
            })
        else:
            blocks.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": "🛡️ *Automated Remediation Disabled:* This fix requires secure human configuration."
                }]
            })

        action_elements.extend([
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Forward message to another slack user"},
                "value": unique_incident_id,
                "action_id": "forward_message"
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Disregard"},
                "value": "cancel",
                "action_id": "disregard_alert"
            }
        ])

        blocks.append({"type": "actions", "elements": action_elements})

        try:
            app.client.chat_postMessage(
                channel=os.environ.get("SLACK_CHANNEL_ID"),
                text=f"Kubernetes Alert: {object_ref}",
                blocks=blocks
            )
            logger.info("Sent interactive Slack notification.")
        except Exception as e:
            logger.error(f"Failed to post via Slack: {e}")


if __name__ == "__main__":
    agent_instance = RCAAgent()
    agent_instance.run()

import os
import logging
import re
import json
import uuid
import subprocess
from datetime import datetime, timedelta
import time
from string import Template
from dashboard import init_db, start_dashboard, log_activity, get_setting
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from google import genai
from google.genai import types
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import threading

# The namespace this agent is deployed into — used when spawning executor Jobs.
_AGENT_NAMESPACE = os.environ.get("AGENT_NAMESPACE", "k8gent-system")

# Secure in-memory store: maps incident_id → full rca_data dict.
# Decouples LLM generation from asynchronous human approval.
pending_fixes = {}

# Maps incident_id → rollback_data dict for actions that can be reversed.
# Populated after a successful remediation; consumed when the user clicks Undo.
pending_rollbacks = {}
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


def _get_slack_display_name(user_id: str) -> str:
    """Resolve a Slack user ID to a human-readable display name.
    Falls back to the raw user ID if the API call fails (e.g. missing users:read scope)."""
    try:
        info = app.client.users_info(user=user_id)
        profile = info["user"]["profile"]
        return profile.get("display_name") or profile.get("real_name") or user_id
    except Exception:
        return user_id


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
            rollback_data = None  # subprocess mode has no structured rollback
        else:
            # Default (api) mode: use the Kubernetes Python client directly.
            # Works in-cluster via ServiceAccount token. No binary or kubeconfig needed.
            result_text, rollback_data = agent_instance.execute_remediation_api(rca_data)

        # Consume the fix entry to prevent replay
        pending_fixes.pop(incident_id, None)

        log_activity(
            incident_id=rca_data.get("incident_id"),
            category=rca_data.get("category"),
            object_ref=rca_data.get("_object_ref") or rca_data.get("remediation_target_name"),
            action=rca_data.get("remediation_action", "subprocess"),
            approved_by=_get_slack_display_name(user),
            result="Fix Applied",
            confidence_score=rca_data.get("confidence_score", 0),
            detail=result_text,
        )

        success_blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"✅ Fix applied successfully! 🛠️\n*Result:*\n```\n{result_text}\n```"
                }
            }
        ]

        if rollback_data:
            # Store rollback data and surface an Undo button in the success message
            pending_rollbacks[incident_id] = rollback_data
            rb = rollback_data
            success_blocks.append({
                "type": "actions",
                "elements": [{
                    "type": "button",
                    "text": {"type": "plain_text", "text": "↩️ Undo this change"},
                    "style": "danger",
                    "value": incident_id,
                    "action_id": "rollback_fix",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Undo the fix?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": f"This will revert `{rb['container_name']}` in `{rb['target_name']}` back to `{rb['previous_image']}`."
                        },
                        "confirm": {"type": "plain_text", "text": "Yes, undo it"},
                        "deny": {"type": "plain_text", "text": "Keep the fix"}
                    }
                }]
            })

        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text="✅ Fix applied successfully!",
            blocks=success_blocks
        )
    except subprocess.CalledProcessError as e:
        log_activity(
            incident_id=rca_data.get("incident_id"),
            category=rca_data.get("category"),
            object_ref=rca_data.get("remediation_target_name"),
            action=rca_data.get("remediation_action", "subprocess"),
            approved_by=user,
            result="Failed",
            detail=e.stderr.strip(),
        )
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=f"❌ kubectl execution failed.\n*Error:*\n```\n{e.stderr.strip()}\n```"
        )
    except ApiException as e:
        log_activity(
            incident_id=rca_data.get("incident_id"),
            category=rca_data.get("category"),
            object_ref=rca_data.get("remediation_target_name"),
            action=rca_data.get("remediation_action"),
            approved_by=user,
            result="Failed",
            detail=f"HTTP {e.status}: {e.reason}",
        )
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=f"❌ Kubernetes API rejected the action (HTTP {e.status}): {e.reason}"
        )
    except Exception as e:
        log_activity(
            incident_id=rca_data.get("incident_id") if rca_data else None,
            category=rca_data.get("category") if rca_data else None,
            object_ref=None,
            action="unknown",
            approved_by=user,
            result="Failed",
            detail=str(e),
        )
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=f"❌ Remediation failed unexpectedly: {e}"
        )

@app.action("rollback_fix")
def handle_rollback_fix(ack, body, logger):
    ack()
    user = body["user"]["id"]
    incident_id = body["actions"][0]["value"]
    channel_id = body["channel"]["id"]
    message_ts = body["message"]["ts"]

    # Same allowlist gate as approve
    allowed = _load_allowed_approvers()
    if allowed and user not in allowed:
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=f"⛔ <@{user}> is not authorized to perform rollbacks."
        )
        return

    rollback_data = pending_rollbacks.get(incident_id)
    if not rollback_data:
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text="❌ No rollback data found. This change may have already been rolled back."
        )
        return

    # Lock the undo button immediately to prevent double-execution
    try:
        original_blocks = body["message"]["blocks"]
        updated_blocks = [b for b in original_blocks if b.get("type") != "actions"]
        updated_blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"↩️ *Rollback initiated by <@{user}>.*"}]
        })
        app.client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=body["message"].get("text", "K8s rollback"),
            blocks=updated_blocks
        )
    except Exception as e:
        logger.warning(f"Could not lock rollback button: {e}")

    action = rollback_data.get("action")
    target_name = rollback_data.get("target_name")
    target_ns = rollback_data.get("target_namespace")
    container_name = rollback_data.get("container_name")
    previous_image = rollback_data.get("previous_image")

    app.client.chat_postMessage(
        channel=channel_id,
        thread_ts=message_ts,
        text=f"↩️ <@{user}> initiated rollback. Reverting `{container_name}` to `{previous_image}`..."
    )

    try:
        # Spawn an executor Job for the rollback — same security model as the original fix.
        # The main agent SA never holds write permissions directly.
        agent_instance.execute_remediation_api({
            "incident_id": f"{incident_id}-rollback",
            "remediation_action": "set_image",
            "remediation_target_name": target_name,
            "remediation_target_namespace": target_ns,
            "remediation_container_name": container_name,
            "remediation_new_image": previous_image,
        })

        pending_rollbacks.pop(incident_id, None)

        log_activity(
            incident_id=incident_id,
            category="Rollback",
            object_ref=f"Deployment/{target_name} in {target_ns}",
            action="set_image_rollback",
            approved_by=_get_slack_display_name(user),
            result="Rolled Back",
            detail=f"Reverted `{container_name}` to `{previous_image}`",
        )
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=f"✅ Rollback complete. `{container_name}` in `{target_name}` reverted to `{previous_image}`."
        )
    except ApiException as e:
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=f"❌ Rollback failed (HTTP {e.status}): {e.reason}"
        )
    except Exception as e:
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=message_ts,
            text=f"❌ Rollback failed unexpectedly: {e}"
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

        # Look up the originating rca_data if still in memory
        unique_id = meta.get("incident_id", "")
        rca_data = pending_fixes.get(unique_id, {})
        log_activity(
            incident_id=rca_data.get("incident_id", unique_id),
            category=rca_data.get("category"),
            object_ref=rca_data.get("_object_ref") or rca_data.get("remediation_target_name"),
            action="forward",
            approved_by=_get_slack_display_name(user_who_forwarded),
            result="Forwarded",
            confidence_score=rca_data.get("confidence_score", 0),
            detail=report_text,
        )
    except Exception as e:
        logger.error(f"Failed to process forward request: {e}")

@app.action("disregard_alert")
def handle_disregard_alert(ack, body, logger):
    ack()
    user = body["user"]["id"]
    incident_id = body["actions"][0]["value"]
    display_name = _get_slack_display_name(user)

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

    # Consume the pending fix so it can't be approved after disregard
    rca_data = pending_fixes.pop(incident_id, {})

    app.client.chat_postMessage(
        channel=body["channel"]["id"],
        thread_ts=body["message"]["ts"],
        text=f"<@{user}> Disregarded the alert. Agent will stand down."
    )

    log_activity(
        incident_id=rca_data.get("incident_id", incident_id),
        category=rca_data.get("category"),
        object_ref=rca_data.get("_object_ref") or rca_data.get("remediation_target_name"),
        action="disregard",
        approved_by=display_name,
        result="Disregarded",
        confidence_score=rca_data.get("confidence_score", 0),
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
        self.batch_v1 = client.BatchV1Api()

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
        init_db()
        start_dashboard(agent=self)

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

        # For Deployment-managed pods, key on the Deployment name rather than the
        # individual pod name. This prevents duplicate alerts when a crashlooping pod
        # is replaced by Kubernetes with a new generated name (e.g. my-app-xk2p9 →
        # my-app-mn4r1) — both belong to the same root cause and should share the
        # same debounce window.
        if involved_object.kind == "Pod":
            deployment_name = self._get_owner_deployment(involved_object.name, namespace)
            if deployment_name:
                cache_key = f"{namespace}/Deployment/{deployment_name}:{reason}"
            else:
                cache_key = f"{namespace}/Pod/{involved_object.name}:{reason}"
        else:
            cache_key = f"{namespace}/{involved_object.kind}/{involved_object.name}:{reason}"

        now = datetime.now()

        if now > self.hourly_reset_time:
            self.hourly_alerts = 0
            self.hourly_reset_time = now + timedelta(hours=1)

        hourly_limit = get_setting("hourly_alert_limit", 10)
        if self.hourly_alerts >= hourly_limit:
            logger.warning(f"Global K8gent RCA rate limit hit ({hourly_limit}/hr). Dropping event to save tokens.")
            return

        debounce_mins = get_setting("debounce_minutes", 15)
        if cache_key in self.alert_cache:
            last_alerted = self.alert_cache[cache_key]
            if now < last_alerted + timedelta(minutes=debounce_mins):
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

        rca_data = rca_result.get("rca_data", {})
        log_activity(
            incident_id=rca_data.get("incident_id"),
            category=rca_data.get("category"),
            object_ref=rca_result.get("object_ref"),
            action="RCA Generated",
            result="Escalated" if rca_data.get("escalation_required") else "Pending",
            confidence_score=rca_data.get("confidence_score", 0),
        )

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

        _prompt_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "prompts", "rca_analysis.txt"
        )
        with open(_prompt_path, encoding="utf-8") as f:
            prompt = Template(f.read()).substitute(
                reason=reason,
                message=message,
                kind=involved_object.kind,
                name=involved_object.name,
                namespace=involved_object.namespace,
                context=truncated_context,
            )

        try:
            response = self.ai_client.models.generate_content(
                model=get_setting("ai_model", self.ai_model),
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
        """Spawn a short-lived Kubernetes Job using k8gent-executor-sa to apply the fix.

        Security model:
          - The main agent SA (k8gent-sa) is permanently read-only.
          - Write permissions (delete/patch) are held ONLY by k8gent-executor-sa,
            which runs exclusively inside ephemeral Job pods.
          - Each Job lives for ~5-30 seconds, then exits. Kubernetes auto-deletes
            it 2 minutes later via ttlSecondsAfterFinished.
          - backoff_limit=0 prevents silent retries; a failed remediation requires
            a fresh human approval cycle.
        """
        action = rca_data.get("remediation_action", "none")
        target_name = rca_data.get("remediation_target_name", "")
        target_ns = rca_data.get("remediation_target_namespace", "default")
        incident_id = rca_data.get("incident_id", uuid.uuid4().hex)

        if not target_name:
            raise ValueError(f"No remediation_target_name provided for action '{action}'.")

        rollback_data = None

        if action == "delete_pod":
            command = ["kubectl", "delete", "pod", target_name, "-n", target_ns]
            result_text = (
                f"Deleted pod `{target_name}` in `{target_ns}`. "
                f"Kubernetes will recreate it from its controller."
            )

        elif action == "set_image":
            container_name = rca_data.get("remediation_container_name", "")
            new_image = rca_data.get("remediation_new_image", "")
            if not container_name or not new_image:
                raise ValueError(
                    "set_image requires both remediation_container_name and remediation_new_image."
                )

            # Capture current image for rollback using read-only access — before the Job mutates anything.
            previous_image = None
            try:
                dep = self.apps_v1.read_namespaced_deployment(name=target_name, namespace=target_ns)
                for c in (dep.spec.template.spec.containers or []):
                    if c.name == container_name:
                        previous_image = c.image
                        break
            except ApiException:
                pass  # Best-effort; rollback button simply won't appear if this fails

            command = [
                "kubectl", "set", "image",
                f"deployment/{target_name}",
                f"{container_name}={new_image}",
                "-n", target_ns,
            ]
            result_text = (
                f"Updated container `{container_name}` in deployment "
                f"`{target_name}` to image `{new_image}`."
            )
            if previous_image:
                rollback_data = {
                    "action": "set_image",
                    "target_name": target_name,
                    "target_namespace": target_ns,
                    "container_name": container_name,
                    "previous_image": previous_image,
                }

        elif action == "rollout_restart":
            command = [
                "kubectl", "rollout", "restart",
                f"deployment/{target_name}",
                "-n", target_ns,
            ]
            result_text = (
                f"Triggered rolling restart of deployment `{target_name}` in `{target_ns}`."
            )

        else:
            raise ValueError(
                f"Unsupported remediation action: '{action}'. Manual intervention required."
            )

        self._run_executor_job(command, incident_id)
        return result_text, rollback_data

    def _run_executor_job(self, command, incident_id, timeout=90):
        """Create a short-lived Job using k8gent-executor-sa and block until it finishes.

        The Job pod is the ONLY principal that ever holds write verbs against the cluster.
        It auto-deletes 120 seconds after completion via ttlSecondsAfterFinished.
        """
        # Build a DNS-safe job name from the incident ID
        safe_id = re.sub(r'[^a-z0-9]', '-', incident_id.lower())[:20].strip('-')
        job_name = f"k8gent-fix-{safe_id}-{int(time.time())}"

        job = client.V1Job(
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=_AGENT_NAMESPACE,
                labels={"app": "k8gent", "incident-id": safe_id},
            ),
            spec=client.V1JobSpec(
                ttl_seconds_after_finished=120,
                backoff_limit=0,  # No retries — failed remediations require fresh human approval
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={"app": "k8gent-executor"}
                    ),
                    spec=client.V1PodSpec(
                        service_account_name="k8gent-executor-sa",
                        restart_policy="Never",
                        security_context=client.V1PodSecurityContext(
                            run_as_non_root=True,
                            run_as_user=1000,
                            run_as_group=3000,
                        ),
                        containers=[
                            client.V1Container(
                                name="executor",
                                image="bitnami/kubectl:latest",
                                command=command,
                                security_context=client.V1SecurityContext(
                                    allow_privilege_escalation=False,
                                    read_only_root_filesystem=True,
                                    capabilities=client.V1Capabilities(drop=["ALL"]),
                                ),
                            )
                        ],
                    ),
                ),
            ),
        )

        self.batch_v1.create_namespaced_job(namespace=_AGENT_NAMESPACE, body=job)
        logger.info(f"Spawned remediation Job '{job_name}' for incident '{incident_id}'")

        # Poll until complete or timeout
        interval, elapsed = 3, 0
        while elapsed < timeout:
            status = self.batch_v1.read_namespaced_job(
                name=job_name, namespace=_AGENT_NAMESPACE
            ).status
            if status.succeeded:
                logger.info(f"Remediation Job '{job_name}' completed successfully.")
                return
            if status.failed:
                raise RuntimeError(
                    f"Remediation Job '{job_name}' failed. "
                    f"Inspect logs: kubectl logs -n {_AGENT_NAMESPACE} -l incident-id={safe_id}"
                )
            time.sleep(interval)
            elapsed += interval

        raise TimeoutError(
            f"Remediation Job '{job_name}' did not complete within {timeout}s."
        )

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
        # Inject the human-readable object ref so action handlers can log it correctly.
        # remediation_target_name is often empty for non-remediable events (e.g. config issues).
        rca_data["_object_ref"] = object_ref
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
                "value": unique_incident_id,
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

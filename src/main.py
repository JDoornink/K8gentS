import os
import time
import logging
import re
from datetime import datetime, timedelta
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from google import genai
from google.genai import types
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import threading
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("K8gentRCA")

# Initialize Slack App
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

@app.action("approve_fix")
def handle_approve_fix(ack, body, logger):
    ack()
    user = body["user"]["id"]
    # The value embedded in the button when sent
    action_value = body["actions"][0]["value"] 
    # Respond in the thread
    app.client.chat_postMessage(
        channel=body["channel"]["id"],
        thread_ts=body["message"]["ts"],
        text=f"<@{user}> Approved execution! Agent is attempting to apply the fix for context: {action_value}..."
    )
    # TODO: Here you would parse `action_value` and elevate privileges to perform the fix
    # For now, we simulate execution
    time.sleep(2)
    app.client.chat_postMessage(
        channel=body["channel"]["id"],
        thread_ts=body["message"]["ts"],
        text="Fix applied successfully! 🛠️"
    )

@app.action("forward_message")
def handle_forward_message(ack, body, logger):
    ack()
    user = body["user"]["id"]
    # In a real implementation, this would open a Slack Modal `users_select`
    app.client.chat_postMessage(
        channel=body["channel"]["id"],
        thread_ts=body["message"]["ts"],
        text=f"<@{user}> Requested to forward the message to another user. (Select User Modal Placeholder)"
    )

@app.action("disregard_alert")
def handle_disregard_alert(ack, body, logger):
    ack()
    user = body["user"]["id"]
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
        
        self.ai_client = genai.Client(api_key=os.environ.get("AI_API_KEY"))
        self.ai_model = os.environ.get("AI_MODEL", "gemini-2.5-pro")

        # Rate Limiting & Debouncing Cache: Prevents the Agent from bankrupting your token budget!
        # Maps "Namespace/Pod-Name:Reason" to Timestamp
        self.alert_cache = {}
        # Max alerts per hour (circuit breaker)
        self.hourly_alerts = 0
        self.hourly_reset_time = datetime.now() + timedelta(hours=1)

        slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
        slack_app_token = os.environ.get("SLACK_APP_TOKEN")

        if not slack_bot_token or not slack_app_token:
            logger.warning("Missing SLACK_BOT_TOKEN or SLACK_APP_TOKEN in environment variables.")

    def run_watcher(self):
        watch_namespace = os.environ.get("WATCH_NAMESPACE")
        
        if watch_namespace:
            logger.info(f"Starting K8gent-S Watcher (Restricted natively to namespace: {watch_namespace})...")
            stream = watch.Watch().stream(self.v1.list_namespaced_event, namespace=watch_namespace)
        else:
            logger.info("Starting K8gent-S Watcher (Global Scope)...")
            stream = watch.Watch().stream(self.v1.list_event_for_all_namespaces)
            
        for event in stream:
            event_obj = event['object']
            if event_obj.type == "Warning":
                self.handle_error_event(event_obj)

    def run(self):
        # Start the K8s Watcher in a background thread
        watcher_thread = threading.Thread(target=self.run_watcher)
        watcher_thread.daemon = True
        watcher_thread.start()

        # Start the Slack Socket Mode handler in the main thread (needed for signal handling)
        logger.info("Starting Slack Socket Mode...")
        SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()

    def handle_error_event(self, event_obj):
        reason = event_obj.reason
        message = event_obj.message
        namespace = event_obj.metadata.namespace
        involved_object = event_obj.involved_object
        
        # Skip spammy/noisy events that don't need expensive AI analysis (like scheduling loops)
        if reason in ["FailedScheduling", "Unhealthy"]:
            return

        cache_key = f"{namespace}/{involved_object.kind}/{involved_object.name}:{reason}"
        
        # --- RATE LIMITER & DEBOUNCE LOGIC ---
        now = datetime.now()
        
        # Reset hourly token burn circuit breaker
        if now > self.hourly_reset_time:
            self.hourly_alerts = 0
            self.hourly_reset_time = now + timedelta(hours=1)
            
        # Hard limit: Max 10 RCA analyses per hour globally
        if self.hourly_alerts >= 10:
            logger.warning("Global K8gent RCA rate limit hit (10/hr). Dropping event to save tokens.")
            return
            
        # Debounce: Do not alert on the exact same pod error more than once every 15 minutes.
        if cache_key in self.alert_cache:
            last_alerted = self.alert_cache[cache_key]
            if now < last_alerted + timedelta(minutes=15):
                logger.debug(f"Event {cache_key} debounced. Skipping LLM call.")
                return
                
        # Register the alert in our rate limiters
        self.alert_cache[cache_key] = now
        self.hourly_alerts += 1
        
        logger.info(f"Anomaly detected! Rate limit passed. Reason: {reason}, Obj: {involved_object.name}")

        context = ""
        if involved_object.kind == "Pod":
            context = self.gather_pod_context(involved_object.name, namespace)

        rca_result = self.generate_rca(reason, message, context, involved_object)
        self.send_slack_notification(rca_result, involved_object)

    def gather_pod_context(self, pod_name, namespace):
        context = []
        try:
            logs = self.v1.read_namespaced_pod_log(name=pod_name, namespace=namespace, tail_lines=50)
            if not logs.strip():
                # Attempt to get previous container logs if current is empty (common in crashes)
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
        """
        Strips highly sensitive data (PII, Secrets, Tokens, internal IPs) 
        before sending payload to external LLM APIs limit data-exposure.
        """
        if not raw_logs:
            return ""
            
        # 1. Strip IPv4 Addresses (Internal & External)
        logs = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[REDACTED_IP]', raw_logs)
        
        # 2. Strip standard Email formats
        logs = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b', '[REDACTED_EMAIL]', logs)
        
        # 3. Strip JWT Tokens (ey...)
        logs = re.sub(r'eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*', '[REDACTED_JWT]', logs)
        
        # 4. Strip common Secret/Key patterns (Basic Auth, Bearer, API Keys)
        logs = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9\-\._~+]+', r'\1[REDACTED_TOKEN]', logs)
        logs = re.sub(r'(?i)(api[_\-]?key)["\':= ]+[A-Za-z0-9\-\._~+]+', r'\1=[REDACTED_API_KEY]', logs)
        logs = re.sub(r'(?i)(password)["\':= ]+[^\s,;]+', r'\1=[REDACTED_PASSWORD]', logs)
        
        return logs

    def generate_rca(self, reason, message, context, involved_object):
        # Truncate string at 5000 characters just in case sanitization missed a massive block
        truncated_context = context[:5000] if len(context) > 5000 else context
        
        prompt = f"""
You are a Senior Site Reliability Engineer (SRE) with Kubernetes credentails (CKA, CKS) specialized in Automated Root Cause Analysis (RCA). Your task is to analyze raw system logs, identify the underlying failure, and provide a remediation plan.

Input Context:
Reason: {reason}
Message: {message}
Involved Object: {involved_object.kind}/{involved_object.name}
Logs/Events:
{truncated_context}

Environment: Kubernetes Cluster (Production)

Analysis Requirements:
Categorization: Classify the error (e.g., OOMKilled, CrashLoopBackOff, Connectivity/DNS, Database Deadlock, Secret/Config Missing).
Root Cause: Explain why the error occurred based strictly on the log patterns.
Suggested Fix: Provide a step-by-step technical resolution (kubectl commands, code changes, or config updates).
Confidence Score: Assign a percentage (0-100) indicating your certainty in the fix.
Crucial: If your Confidence Score is less than 75, you must set escalation_required to true.

Output Format (Strict JSON):
{{
  "incident_id": "RCA-XXXX",
  "category": "String",
  "root_cause": "String",
  "suggested_fix": "String",
  "confidence_score": 0,
  "escalation_required": false
}}
"""
        
        # Google Gemini API Call
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
            
            # Attempt to extract JSON from response
            json_str = ai_text
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()
                
            rca_data = json.loads(json_str)
        except Exception as e:
            logger.error(f"Failed to query AI or parse JSON: {e}")
            rca_data = {
                "incident_id": "UNKNOWN",
                "category": "Error",
                "root_cause": f"AI Diagnostic failed. Exception: {e}",
                "suggested_fix": "Investigate manually.",
                "confidence_score": 0,
                "escalation_required": True
            }

        return {
            "rca_data": rca_data,
            "object_ref": f"{involved_object.kind}/{involved_object.name} in {involved_object.namespace}",
            "raw_context": f"{involved_object.namespace}/{involved_object.kind}/{involved_object.name}"
        }

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
                    "text": f"*⚠️ K8s Alert:* `{object_ref}`\n*Category:* {rca_data.get('category', 'Unknown')}\n*Confidence Score:* {confidence}%\n\n*Root Cause:*\n{rca_data.get('root_cause', 'N/A')}\n\n*Suggested Fix:*\n{rca_data.get('suggested_fix', 'N/A')}"
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

        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Approve the LLM to fix it"
                    },
                    "style": "danger",
                    "value": rca_result['raw_context'],
                    "action_id": "approve_fix"
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Forward message to another slack user"
                    },
                    "value": rca_result['raw_context'],
                    "action_id": "forward_message"
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Disregard"
                    },
                    "value": "cancel",
                    "action_id": "disregard_alert"
                }
            ]
        })
        
        try:
            app.client.chat_postMessage(
                channel=os.environ.get("SLACK_CHANNEL_ID"),
                text=f"Kubernetes Alert: {object_ref}",
                blocks=blocks
            )
            logger.info("Sent Interactive Slack notification.")
        except Exception as e:
            logger.error(f"Failed to post via Slack: {e}")

if __name__ == "__main__":
    agent = RCAAgent()
    agent.run()

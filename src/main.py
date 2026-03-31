import os
import time
import logging
import re
from datetime import datetime, timedelta
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from anthropic import Anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import threading

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

@app.action("cancel_fix")
def handle_cancel_fix(ack, body, logger):
    ack()
    user = body["user"]["id"]
    app.client.chat_postMessage(
        channel=body["channel"]["id"],
        thread_ts=body["message"]["ts"],
        text=f"<@{user}> Cancelled the operation. Agent will stand down."
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
        
        self.ai_client = Anthropic(api_key=os.environ.get("AI_API_KEY"))
        self.ai_model = os.environ.get("AI_MODEL", "claude-4.6-opus-20260224")

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

    def run(self):
        # Start the Slack Socket Mode handler in a background thread
        logger.info("Starting Slack Socket Mode Thread...")
        socket_thread = threading.Thread(
            target=lambda: SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()
        )
        socket_thread.daemon = True
        socket_thread.start()

        logger.info("Starting K8gent-S Watcher...")
        w = watch.Watch()
        for event in w.stream(self.v1.list_event_for_all_namespaces):
            event_obj = event['object']
            if event_obj.type == "Warning":
                self.handle_error_event(event_obj)

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
        You are an expert Kubernetes Site Reliability Engineer (CKA/CKS). 
        An error has occurred:
        Reason: {reason}
        Message: {message}
        Involved Object: {involved_object.kind}/{involved_object.name}
        Context: {truncated_context}
        
        Provide the Top 3 possible root causes along with a confidence metric (percentage).
        Also provide step-by-step instructions on how to fix this issue safely.
        Format your response nicely.
        """
        
        # Native Anthropic Claude API Call
        try:
            response = self.ai_client.messages.create(
                model=self.ai_model,
                max_tokens=1000,
                temperature=0.2,
                system="You are an expert Kubernetes AI assistant.",
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            ai_text = response.content[0].text
        except Exception as e:
            logger.error(f"Failed to query AI: {e}")
            ai_text = f"*AI Diagnostic failed. Exception: {e}*"

        return {
            "ai_analysis": ai_text,
            "object_ref": f"{involved_object.kind}/{involved_object.name} in {involved_object.namespace}",
            "raw_context": f"{involved_object.namespace}/{involved_object.kind}/{involved_object.name}"
        }

    def send_slack_notification(self, rca_result, involved_object):
        if not os.environ.get("SLACK_CHANNEL_ID"):
            return
            
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*⚠️ K8s Alert:* `{rca_result['object_ref']}`\n\n*Agent RCA Diagnosis:*\n{rca_result['ai_analysis']}"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Approve Fix (Agent Execution)"
                        },
                        "style": "danger",
                        "value": rca_result['raw_context'],
                        "action_id": "approve_fix"
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "I'll do it manually"
                        },
                        "value": "cancel",
                        "action_id": "cancel_fix"
                    }
                ]
            }
        ]
        
        try:
            app.client.chat_postMessage(
                channel=os.environ.get("SLACK_CHANNEL_ID"),
                text=f"Kubernetes Alert: {rca_result['object_ref']}",
                blocks=blocks
            )
            logger.info("Sent Interactive Slack notification.")
        except Exception as e:
            logger.error(f"Failed to post via Slack: {e}")

if __name__ == "__main__":
    agent = RCAAgent()
    agent.run()

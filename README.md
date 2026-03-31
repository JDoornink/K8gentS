<div align="center">
  <h1>K8gentS ☸️🤖</h1>
  <p><strong>An Autonomous AI-Driven Root Cause Analysis Agent for Kubernetes</strong></p>
</div>

---

## 📖 Overview
**K8gentS** acts as a dedicated observer within your Kubernetes cluster. Designed as an SRE assistant equipped with Deep Kubernetes Knowledge (CKA/CKS level), it leverages Large Language Models (LLMs) like **Claude 4.6 Opus** and robust system telemetry to automatically diagnose pod failures, resource exhaustion, and network bottlenecks. Its goal is to drastically reduce operational mean time to repair (MTTR).

## ✨ Core Responsibilities
1. **Continuous Monitoring:** Watches the cluster for error events, crashed pods, `CrashLoopBackOff` states, `OOMKilled` events, and other failure conditions such as `Connectivity/DNS`, `Database Deadlock`, or `Secret/Config Missing`.
2. **Automated Root Cause Analysis (RCA):** Upon detecting an anomaly, it securely fetches relevant context (recent logs, pod descriptions, event history), sanitizes it of secrets/PII, and sends this context to an LLM-based reasoning engine.
3. **Notification & Confidence Scoring:** Notifies your designated Slack communication channel via Socket Mode with:
   - A descriptive summary of the error.
   - The top 3 possible root causes, each with an associated confidence metric.
   - Step-by-step resolution instructions.
4. **Interactive Remediation (Opt-in):** Prompts the user directly in Slack with interactive buttons: *"Approve Fix"* or *"I'll do it manually"*.
   - **Default Posture - Read Only:** The agent is strictly **READ-ONLY**, making it exceptionally secure by default.

---

## 🛠️ Architecture & Security
- **Air-Tight Execution:** The Agent's Pod utilizes a highly restrictive `securityContext` (`runAsNonRoot`, read-only filesystem, dropping all capabilities).
- **Rate-Limiting & Economy:** Built-in hourly circuit breakers and smart event debouncing ensure noisy namespaces don't bankrupt your AI API token budget.
- **Log Sanitization:** A robust regex sweeper strictly redacts IP addresses, internal domains, API Keys, and JWTs from logs *before* passing telemetry to the LLM.
- **Ingress-Free Interaction:** Uses Slack Socket Mode to maintain interactive bidirectional chat functionality without requiring any exposed endpoints or Kubernetes Ingress rules.

---

## 🚀 Installation & Connection Guide

### Prerequisites
- A Kubernetes cluster (compatible with v1.20+)
- `kubectl` configured and authenticated to the target cluster.
- AI Provider API Key (e.g., Anthropic Claude API Key).
- A Docker Registry to push the agent's image to.

### 1. Configure Slack (Socket Mode)
We highly recommend **Slack with Socket Mode** to receive interactive button clicks securely.

1. Open a browser and create a Slack App at `api.slack.com`.
2. Enable **Socket Mode** (this generates an App-Level Token starting with `xapp-`).
3. Enable **Interactive Components**.
4. Request `chat:write` and `chat:write.public` scopes under Oauth & Permissions (this generates a Bot Token starting with `xoxb-`).

### 2. Build and Push the Agent Image
Before deploying, you must build the Python agent and push it to your container registry (e.g., Docker Hub, AWS ECR, or a local K8s registry).

```bash
docker build -t your-registry/k8gent:latest .
docker push your-registry/k8gent:latest
```
*(Note: Be sure to update `deploy/deployment.yaml` line 20 with your actual image repository link!)*

### 3. Prepare Secrets
Create the secrets necessary for the Agent to securely interact with the AI engine and Slack.

```bash
kubectl create namespace k8gent-system

kubectl create secret generic k8gent-secrets \
  --namespace=k8gent-system \
  --from-literal=AI_API_KEY="your-api-key" \
  --from-literal=SLACK_BOT_TOKEN="xoxb-your-bot-token" \
  --from-literal=SLACK_APP_TOKEN="xapp-your-app-token" \
  --from-literal=SLACK_CHANNEL_ID="C12345678"
```

### 4. Deploy the Manifests
Apply the provided RBAC rules and Deployment manifests. 
The RBAC explicitly limits the Agent to `get`, `list`, and `watch` verbs across the cluster.

```bash
kubectl apply -f deploy/rbac.yaml
kubectl apply -f deploy/deployment.yaml
```

### 5. Verify Installation
Check the agent pod logs to ensure it successfully connected to the cluster and initialized the AI models.

```bash
kubectl logs -l app=k8gent -n k8gent-system -f
```

---

## 🔮 Future Customization
To define what specific namespaces or events to watch, or to utilize different LLM APIs (e.g., local open-weight models), simply modify the `WATCH_NAMESPACES` and `AI_MODEL` environment variables defined in the `deployment.yaml`.

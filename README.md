# K8gentS ☸️🤖

**An Autonomous AI-Driven Root Cause Analysis Agent for Kubernetes**

---

## 📖 Overview

**K8gentS** is a continuously running observer inside your Kubernetes cluster. It watches the event stream for all `Warning`-class events — pod failures, node pressure, storage mount errors, config problems — and routes them through a Gemini-powered reasoning engine to produce root cause analysis and resolution steps. Its goal is to reduce MTTR and surface failure context that would otherwise require manual `kubectl` investigation.

---

## 🧠 The Hard Problem

Building the Kubernetes side of this is straightforward. The real challenge is making a diagnostic layer trustworthy when the engine behind it is fundamentally non-deterministic.

Traditional observability is built on guarantees. Alerts fire on known thresholds. Dashboards show reproducible numbers. Logs return consistent answers to the same query. SRE success depends on that predictability — it's what makes incident response repeatable and on-call sustainable.

An LLM-driven diagnostic layer breaks that contract. The same pod failure can produce three different plausible explanations across three different runs. Each may be coherent. Each may even be correct under different assumptions. But "plausible" is not the same as "right," and for infrastructure, the gap between them is where outages live.

Some of the specific problems I've been working through while building K8gentS:

**1. Confidence scoring with an unbounded output space.**
The agent returns top-3 root causes with confidence metrics, but confidence in what, exactly? The model is not selecting from a fixed set of known failure modes — it's generating free-form hypotheses. A calibrated confidence score needs a reference distribution, and the distribution here is whatever the model happened to produce this run.

**2. When to trust reasoning vs. fall back to deterministic checks. - THE ART**
Some failures (CrashLoopBackOff, OOMKilled) have well-traveled diagnostic paths and a deterministic check will be right every time. Others benefit from the model's ability to interpolate across signals. Drawing that line — and doing it at runtime — is non-trivial and where the art really lies.

**3. Evaluating an agent that's supposed to find failures you didn't anticipate.**
The standard ML evaluation approach assumes you know what "correct" looks like. For a diagnostic agent, part of the value is catching novel failure modes — by definition, failures you couldn't pre-enumerate. So how do you decide what is wrong and what is right?

**4. The "Tool in the Cluster" problem.**
How do you monitor the monitor? Currently the service is set to run as a service in the cluster, but what if the service itself causes resource exhaustion, or is experiencing failures itself? How can you identify if the service itself is the cause of your issue?

**5. Determining the right model for this problem.**
With so many other types of Machine Learning models out there, is a non-deterministic large language model really the right choice or is another model better suited for infrastructure type problems?

These are the questions I'm actively working on. If you've solved any of them — or have a sharper framing than I've got — I'd like to hear it.

---

## ✨ Core Responsibilities

1. **Continuous Monitoring:** Watches the cluster for error events, crashed pods, `CrashLoopBackOff` states, `OOMKilled` events, and other failure conditions such as `Connectivity/DNS`, `Database Deadlock`, or `Secret/Config Missing`.
2. **Automated Root Cause Analysis (RCA):** Upon detecting an anomaly, it securely fetches relevant context (recent logs, pod descriptions, event history), sanitizes it of secrets/PII, and sends this context to an LLM-based reasoning engine.
3. **Notification & Confidence Scoring:** Notifies your designated Slack communication channel via Socket Mode with:
   * A descriptive summary of the error.
   * The top 3 possible root causes, each with an associated confidence metric.
   * Step-by-step resolution instructions.
4. **Interactive Remediation (Opt-in):** Prompts the user directly in Slack with interactive buttons: *"Approve Fix"* or *"I'll do it manually"*.
   * **Default Posture - Read Only:** The agent is strictly **READ-ONLY**, making it exceptionally secure by default.

---

## 🛠️ Architecture & Security

The agent is designed so that each layer independently limits blast radius — not as redundancy for its own sake, but because no single control is sufficient when the reasoning engine is non-deterministic.

| Layer | Mechanism | What it prevents |
|---|---|---|
| **Pod security** | `runAsNonRoot`, read-only filesystem, all Linux capabilities dropped | Container escape, privilege escalation |
| **RBAC** | Agent pod is strictly read-only; write verbs live only on `k8gent-executor-sa` | Agent compromise → cluster mutation |
| **Ephemeral executor** | Short-lived Jobs via `k8gent-executor-sa`; `ttlSecondsAfterFinished=120` | Persistent foothold after remediation |
| **OPA Gatekeeper** | Rego policy enforced at the API server admission layer | Executor escaping its scope, even if RBAC is misconfigured |
| **Log sanitization** | Regex sweeper strips IPs, JWTs, API keys, emails before LLM call | Secrets exfiltration via LLM prompt |
| **Rate limiting** | Hourly circuit breakers and event debouncing | Noise-driven API budget exhaustion |
| **Ingress-free comms** | Slack Socket Mode; no exposed endpoints or Ingress rules | Inbound attack surface |

The OPA Gatekeeper policy (defined in `deploy/helm/k8gents/templates/opa-gatekeeper/`) explicitly blocks the executor service account from modifying `serviceAccountName`, enabling `hostNetwork` or `hostPID`, operating inside `kube-system`, or mutating any resource kind other than pods and deployments — enforced directly at the Kubernetes API admission layer, independent of RBAC.

---

## 🚀 Deployment

K8gentS ships as a Helm chart. OPA Gatekeeper is a declared chart dependency — the security sandbox installs automatically alongside the agent.

### Prerequisites

* Kubernetes v1.20+, Helm 3
* `kubectl` authenticated to the target cluster
* A Google Gemini API key (`AI_API_KEY`)
* A Slack app with Socket Mode enabled (generates `SLACK_BOT_TOKEN` starting `xoxb-` and `SLACK_APP_TOKEN` starting `xapp-`)
* Slack channel ID (`SLACK_CHANNEL_ID`) and a comma-separated list of approver Slack user IDs (`ALLOWED_APPROVERS`)

### 1. Configure Slack

1. Create a Slack App at `api.slack.com`.
2. Enable **Socket Mode** → generates an App-Level Token (`xapp-...`).
3. Enable **Interactive Components**.
4. Add `chat:write` and `chat:write.public` OAuth scopes → generates a Bot Token (`xoxb-...`).
5. Invite the bot to your alert channel and copy the Channel ID from channel settings.

### 2. Build and Push the Agent Image

```bash
docker build -t your-registry/k8gent:latest .
docker push your-registry/k8gent:latest
```

Update `image.repository` in `deploy/helm/k8gents/values.yaml` to match your registry path.

### 3. Install via Helm

```bash
# Fetch chart dependencies (downloads OPA Gatekeeper)
helm dependency update deploy/helm/k8gents

# Install — secrets are injected at deploy time, never stored in source
helm install k8gents deploy/helm/k8gents \
  --namespace k8gent-system \
  --create-namespace \
  --set secrets.aiApiKey="YOUR_GEMINI_KEY" \
  --set secrets.slackBotToken="xoxb-..." \
  --set secrets.slackAppToken="xapp-..." \
  --set secrets.slackChannelId="C12345678" \
  --set secrets.allowedApprovers="U123456,U789012"
```

To disable the OPA sandbox (if your cluster already runs Gatekeeper with its own policies):
```bash
--set sandbox.enabled=false --set gatekeeper.enabled=false
```

### 4. Verify Installation

```bash
kubectl logs -l app=k8gents -n k8gent-system -f
```

You should see the watcher connect to the cluster API and the Slack Socket Mode connection initialize.

---

## ⚙️ Configuration

Key environment variables (set via `--set agent.*` in Helm, or directly if running locally):

| Variable | Default | Description |
|---|---|---|
| `WATCH_NAMESPACES` | `all` | Comma-separated namespaces to watch, or `all` for cluster-wide |
| `AI_MODEL` | `gemini-2.5-pro` | Any model name supported by the Google GenAI SDK |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `REMEDIATION_MODE` | `api` | `api` (Kubernetes client, safe in-cluster) or `subprocess` (kubectl, local dev only) |

Changing `AI_MODEL` requires no code changes — the agent routes all LLM calls through the configured model name dynamically.

---

## 🔮 What's Next

What's implemented and working:
* Watch → diagnose → Slack notification with confidence scoring
* Human-gated remediation via Slack interactive buttons
* Ephemeral Job executor with OPA Gatekeeper admission sandbox
* MCP server for on-demand diagnostics from AI clients
* Helm chart with Gatekeeper as a hard dependency

What's genuinely unsolved:
* **Confidence calibration** — the current scoring reflects the model's self-reported certainty, which doesn't reliably correlate with empirical accuracy.
* **Deterministic routing** — canonically diagnosable failures (CrashLoopBackOff, OOMKilled) shouldn't route through the LLM at all. Building a reliable runtime classifier for "known answer" vs. "needs reasoning" is the next structural change.
* **Evaluation** — regression testing an agent designed to catch novel failures requires a framework that doesn't yet fully exist for this problem domain. Synthetic failure injection (chaos engineering) is the most promising direction, but coverage is inherently limited.
* **Post-remediation verification** — after executing a fix, monitor the target namespace for 60s and post a follow-up Slack thread confirming recovery or flagging that the crash state persists.

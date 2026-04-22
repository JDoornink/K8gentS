# K8gentS - Autonomous Kubernetes RCA Agent

## Project Goal
To build a Python-based autonomous Root Cause Analysis (RCA) agent for Kubernetes clusters. The agent leverages Large Language Models (LLM) and system telemetry to automatically diagnose pod failures, resource exhaustion, and network bottlenecks, aiming to drastically reduce operational MTTR.

## Core Responsibilities & Workflow
1. **Ingestion & Monitoring:** 
   - Watch the cluster for `ERROR` or `CRITICAL` events (e.g., `OOMKilled`, `CrashLoopBackOff`).
   - Fetch relevant context (recent logs, pod descriptions, event history).
2. **Sanitization:** 
   - Scrub logs of secrets, PII, API keys, and internal IP addresses before sending context to the LLM.
3. **LLM Analysis (The "SRE Twist"):**
   - Use the specific system prompt to classify the error, identify the root cause, suggest a fix, and assign a confidence score (0-100%).
   - Expect a strict JSON output containing incident details, category, root cause, suggested fix, and an escalation flag.
4. **The Logic Gate / Escalation:**
   - If `confidence_score < 75`: Append `ACTION_REQUIRED: ESCALATE_TO_HUMAN` and notify via Slack (Socket Mode).
   - If `confidence_score >= 75`: Provide the validated fix to the user via Slack with an interactive "Approve Fix" button for human-in-the-loop remediation.
5. **Security Posture:** 
   - The agent operates in a **READ-ONLY** mode by default.
   - Any write/remediation actions require human-in-the-loop authorization via an approver allowlist.

## Technology Stack
- **Language:** Python 3
- **Libraries:** `kubernetes` (client-python), `slack_bolt` & `slack_sdk` (Socket Mode), `google-genai`, `flask` (dashboard).
- **Deployment:** Docker, Kubernetes Deployment, RBAC configured in `deploy/rbac.yaml`.
- **Dashboard:** Flask + Bootstrap 5 + SQLite, running as a daemon thread on port 8080.

## AI Agent Instructions (When assisting with this repo)
When asked to continue or work on this project, the AI should:
1. Refer to `src/main.py` for the core monitoring, LLM integration, and Slack notification loops.
2. Refer to `src/dashboard.py` for the Flask web UI, SQLite activity log, and settings management.
3. Adhere to the secure-by-default (read-only) RBAC policy in `deploy/rbac.yaml`. The opt-in remediation role (`k8gent-remediation-role`) grants only `delete` on pods and `patch` on deployments.
4. Remediation executes via the Kubernetes Python client (`execute_remediation_api`) when `REMEDIATION_MODE=api`, or via `subprocess`/`kubectl` for local dev when `REMEDIATION_MODE=subprocess`.
5. The approver allowlist is loaded from `ALLOWED_APPROVERS_FILE` (local file) or `ALLOWED_APPROVERS` env var (Kubernetes Secret for in-cluster).
6. Settings (AI model, rate limits, debounce window, remediation mode) are persisted to `data/settings.json` and applied live without restart. `watch_namespace` requires a restart.
7. Ensure all newly written code includes appropriate error handling, logging, and strictly parses JSON responses from the LLM.

---

## ✅ Completed Phases

### Phase 1 — End-to-End RCA Monitoring
- Cluster event watcher with namespace scoping and rate limiting/debouncing.
- LLM-powered RCA via Gemini (`google-genai` SDK), strict JSON output with confidence scoring.
- Interactive Slack notifications via Socket Mode (Approve Fix, Forward, Disregard buttons).
- Log sanitization (IPs, emails, JWTs, tokens, passwords redacted before LLM call).

### Phase 2 — Automated Remediation
- Kubernetes Python client execution (`delete_pod`, `set_image`, `rollout_restart`) replacing subprocess shell calls for in-cluster safety.
- Subprocess/kubectl fallback for local dev via `REMEDIATION_MODE=subprocess`.
- Approver allowlist: file-based for local dev (`approvers.local`), Kubernetes Secret for in-cluster.
- Narrowly scoped opt-in RBAC role for write permissions (separate from read-only role).
- Pod → ReplicaSet → Deployment owner traversal so LLM receives correct deployment name for structured remediation.
- Owner deployment name included in LLM context; structured remediation fields added to JSON schema.

### Phase 2.5 — Web Dashboard
- Flask dashboard (port 8080) running as a daemon thread alongside the agent.
- **Activity Log** (`/`): SQLite-backed event table with live cluster status resolution (Pending → Resolved) and 30-second auto-refresh.
- **Cluster Info** (`/cluster`): active context, connection health, watch scope, pod counts by namespace.
- **Settings** (`/settings`): live editor for AI model, remediation mode, rate limits, debounce window. Persists to `data/settings.json`.
- **Debounce deduplication (pod replacement):** Debounce cache now keys on the owning Deployment name (not the ephemeral pod name) for Deployment-managed pods. Prevents duplicate alerts when Kubernetes replaces a crashlooping pod with a new generated name.

---

## 🔮 Phase 3: Post-Remediation Verification & Rollback
Once the core execution hooks are stable, implement a closed-loop validation workflow:
1. **Verification Loop:** After executing a fix, automatically monitor the target namespace for 30-60 seconds to verify the pod transitions to `Running/Ready`.
2. **Follow-Up Slack Message:** Post to the thread confirming success or reporting that the crash state persists.
3. **Interactive Rollback:** Add a "Rollback Fix" button to the Slack payload for agent-applied changes.
4. **Execution Reversal:** On rollback, programmatically reverse changes (e.g., `kubectl rollout undo` via `apps_v1.patch_namespaced_deployment` with previous revision).

---

## 🔮 Phase 4: Expanded Monitoring Coverage
Currently the agent only processes pod-level `Warning` events. Expand to cover:

### Pod Lifecycle (already partially covered — improve handling)
- `CrashLoopBackOff` — repeated container crashes; bad entrypoint, missing config, OOM.
- `OOMKilled` — container exceeded memory limit; needs resource tuning or leak investigation.
- `ErrImagePull` / `ImagePullBackOff` — bad image tag, missing registry credentials, network issue.
- `CreateContainerConfigError` — missing Secret or ConfigMap the pod depends on.

### Scheduling & Resource Failures (not yet monitored)
- `Insufficient CPU/Memory` (`FailedScheduling`) — no node has capacity; cluster needs scaling. Currently skipped in `handle_error_event`.
- `Unschedulable` — taints/tolerations mismatch, node selectors, PVC not binding.
- `Evicted` — node disk/memory pressure caused pod eviction.

### Storage Failures (not yet monitored)
- `FailedMount` — PVC cannot attach or find a PV; common on node failures with EBS/NFS.
- `VolumeNotFound` — underlying storage deleted while pod still referenced it.

### Network & DNS Failures (not yet monitored)
- `NetworkPlugin not ready` — CNI plugin crashed or misconfigured.
- DNS resolution failures — CoreDNS pod issues breaking service discovery.
- Service connectivity failures — selector not matching any pod labels.

### Node-Level Failures (not yet monitored — requires node watcher)
- `NotReady` node — kubelet stopped, disk/memory pressure, or kernel panic.
- `DiskPressure` / `MemoryPressure` — node resource exhaustion leading to cascading evictions.
- Proactive threshold alerting (e.g., alert when node hits 85% memory before evictions occur).

### RBAC & Auth Failures (not yet monitored)
- `Forbidden` on API calls — ServiceAccount missing Role/ClusterRole binding.
- `Unauthorized` — expired or missing credentials for external services.

---

## 🔮 Phase 5: Security Hardening

### OPA / Kyverno Admission Policy Layer (Priority: High)
Even with the short-lived Job executor pattern in place, the `k8gent-executor-sa` service account holds
cluster-wide `delete` (pods) and `patch` (deployments) verbs. An OPA Gatekeeper or Kyverno admission
policy layer would enforce *what* can actually be mutated — independent of RBAC.

Goals:
- Allow only `patch` on `deployments`, never on `daemonsets`, `statefulsets`, or `namespaces`.
- Restrict `delete` to pods only, never to controllers or cluster-scoped resources.
- Scope allowed mutations to a configurable set of namespaces (e.g. exclude `kube-system`).
- Block any mutation that would change `serviceAccountName` or add `hostPID`/`hostNetwork`.
- Log all policy decisions to a dedicated audit sink.

This layer sits in front of the Kubernetes API server and enforces constraints even if RBAC is
misconfigured or overly broad. No Python code changes required — deploy a `ConstraintTemplate`
(OPA) or `ClusterPolicy` (Kyverno) manifest.

### Remaining Duplicate-Alert Gaps (Priority: Medium)
The following deduplication gaps still exist after the pod-replacement fix:

| Gap | Root Cause | Suggested Fix |
|---|---|---|
| Agent restart clears cache | `alert_cache` is in-memory only | Re-hydrate from recent SQLite activity log on startup |
| Hourly rate limiter also in-memory | `hourly_alerts` resets on restart | Persist count + reset timestamp to `data/settings.json` or SQLite |
| No idempotency key in Slack | Two calls to `send_slack_notification` produce two unlinked messages | Pass `incident_id` as Slack metadata; use `chat_update` if a message for that ID already exists |
| Cache not thread-safe | Plain `dict` with no lock; safe under CPython GIL but not guaranteed | Wrap `alert_cache` reads/writes with `threading.Lock` |
| `pending_fixes` has no TTL | Approvals clicked hours after an alert still execute the original fix | Store a `created_at` timestamp with each entry; reject approvals older than a configurable window (e.g. 30 min) |

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
   - Any write/remediation actions require human-in-the-loop authorization.

## Technology Stack
- **Language:** Python 3
- **Libraries:** `kubernetes` (client-python), `slack_bolt` & `slack_sdk` (Socket Mode), LLM SDK (OpenAI/Anthropic/Google).
- **Deployment:** Docker, Kubernetes Deployment, RBAC configured in `deploy/rbac.yaml` (strictly limited to `get`, `list`, `watch`).

## AI Agent Instructions (When assisting with this repo)
When asked to continue or work on this project, the AI should:
1. Refer to `src/main.py` for the core implementation of the monitoring, LLM integration, and Slack notification loops.
2. Adhere to the defined secure-by-default (read-only) RBAC policy located in `deploy/rbac.yaml`.
3. Utilize the LLM prompt outline (originally from `Temp.txt`) to structure any new prompt engineering tasks.
4. Ensure all newly written code includes appropriate error handling, logging, and strictly parses JSON responses from the LLM.
5. Ensure Slack Socket Mode is configured for any interactive elements.

---

## 🟢 Checkpoint / Current Status (Ready for Next Session)
During the last session, we successfully completed **Phase 1: End-to-End RCA Monitoring**. 
- The agent successfully detected cluster failures natively using a `WATCH_NAMESPACE` isolator.
- The underlying AI architecture was migrated to the `google-genai` SDK using `gemini-2.5-pro`.
- A strict JSON Mime-Type generation guard was placed on the `GenerateContentConfig` to ensure parser safety.
- The Logic Engine successfully dispatched the parsed JSON diagnostic via Slack Socket Mode.

### 📝 Next Action Items (Start Here Tomorrow):
1. **Automated Remediation Work:** The agent currently sends the Interactive Slack payload flawlessly, but clicking the "Approve Fix" button simply fires a simulated `time.sleep(2)` log.
2. **Bind the Execution Hooks:** Navigate to `src/main.py` and replace the simulated button action handlers with genuine Kubernetes API commands. Define how the Agent should actually apply the `"suggested_fix"` safely into the target namespace upon human approval.
3. Start creating some unit tests to verify the agent's functionality.

### 🔮 Phase 3: Post-Remediation Verification & Rollback
Once the core execution hooks are bound, implement a closed-loop validation workflow:
1. **Verification Loop:** After the LLM executes a fix, the Agent should automatically monitor the target namespace for 30-60 seconds to verify if the pod successfully transitions to a `Running/Ready` state.
2. **Follow-Up Slack Webhook:** Send a follow-up message to the thread indicating whether the fix was successful or if the crash state persists.
3. **Interactive Rollback:** If the fix fails (or if the user identifies a regression), provide a "Rollback Fix" interactive button on the Slack payload.
4. **Execution Reversal:** When "Rollback Fix" is clicked, the Agent must programmatically reverse the changes (e.g., reverting to the previous deployment generation via `kubectl rollout undo` or deleting the strictly newly-applied API patch).

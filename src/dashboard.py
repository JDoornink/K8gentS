"""
K8gentS Dashboard
-----------------
Flask web UI providing activity logging, cluster info, and live settings management.
Runs as a daemon thread inside the main agent process — no separate service needed.

Pages:
  /          Activity log (SQLite-backed, survives restarts)
  /cluster   Cluster connection info and pod counts
  /settings  Live settings editor (persists to data/settings.json)
"""
import os
import json
import sqlite3
import threading
import logging
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for

logger = logging.getLogger("K8gentDashboard")

# ─── Paths ────────────────────────────────────────────────────────────────────
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_DB_FILE = os.path.join(_DATA_DIR, "k8gent.db")
_SETTINGS_FILE = os.path.join(_DATA_DIR, "settings.json")

# ─── Settings ─────────────────────────────────────────────────────────────────
SETTINGS_DEFAULTS = {
    "ai_model": "gemini-2.5-flash",
    "remediation_mode": "api",
    "watch_namespace": "",
    "hourly_alert_limit": 10,
    "debounce_minutes": 15,
}

_settings: dict = {}
_settings_lock = threading.Lock()


def load_settings():
    """Load settings from file, falling back to env vars then defaults."""
    global _settings
    os.makedirs(_DATA_DIR, exist_ok=True)

    base = SETTINGS_DEFAULTS.copy()
    # Env vars seed the defaults so the first run picks up the shell environment
    if os.environ.get("AI_MODEL"):
        base["ai_model"] = os.environ["AI_MODEL"]
    if os.environ.get("REMEDIATION_MODE"):
        base["remediation_mode"] = os.environ["REMEDIATION_MODE"]
    if os.environ.get("WATCH_NAMESPACE"):
        base["watch_namespace"] = os.environ["WATCH_NAMESPACE"]

    if os.path.exists(_SETTINGS_FILE):
        try:
            with open(_SETTINGS_FILE, "r") as f:
                saved = json.load(f)
            base.update(saved)
        except Exception as e:
            logger.warning(f"Could not read settings file, using defaults: {e}")

    with _settings_lock:
        _settings = base


def save_settings(updates: dict):
    """Merge updates into the live settings dict and persist to disk."""
    with _settings_lock:
        _settings.update(updates)
        try:
            with open(_SETTINGS_FILE, "w") as f:
                json.dump(_settings, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save settings: {e}")


def get_setting(key, fallback=None):
    """Read a single setting. Safe to call from any thread at any time."""
    with _settings_lock:
        return _settings.get(key, fallback if fallback is not None else SETTINGS_DEFAULTS.get(key))


# ─── Database ─────────────────────────────────────────────────────────────────
def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables and load settings. Call once at agent startup."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    load_settings()
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            incident_id     TEXT,
            category        TEXT,
            object_ref      TEXT,
            action          TEXT,
            approved_by     TEXT,
            result          TEXT,
            confidence_score INTEGER,
            detail          TEXT
        )
    """)
    conn.commit()
    conn.close()
    logger.info(f"Dashboard database ready at {_DB_FILE}")


def log_activity(incident_id, category, object_ref, action,
                 approved_by=None, result=None, confidence_score=0, detail=None):
    """Insert one row into the activity log. Fire-and-forget safe."""
    try:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO activity_log
               (timestamp, incident_id, category, object_ref, action,
                approved_by, result, confidence_score, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                incident_id, category, object_ref, action,
                approved_by, result, confidence_score, detail,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to log activity: {e}")


def get_activity_log(limit: int = 200):
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _update_result(row_id: int, result: str):
    try:
        conn = _get_conn()
        conn.execute("UPDATE activity_log SET result = ? WHERE id = ?", (result, row_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to update activity log row {row_id}: {e}")


def _resolve_pending_incidents():
    """On each page load, check whether pods behind Pending/Escalated rows have
    recovered in the cluster, and flip their result to Resolved if so.

    Parse strategy: object_ref is stored as "Kind/name in namespace".
    For pods owned by a Deployment we check the Deployment's available
    replicas rather than the specific (possibly replaced) pod name.
    """
    if _agent_ref is None:
        return

    pending_rows = [
        r for r in get_activity_log(limit=200)
        if r.get("result") in ("Pending", "Escalated")
    ]
    if not pending_rows:
        return

    for row in pending_rows:
        obj_ref = row.get("object_ref") or ""
        if " in " not in obj_ref:
            continue

        kind_name, namespace = obj_ref.rsplit(" in ", 1)
        kind, _, name = kind_name.partition("/")
        namespace = namespace.strip()
        name = name.strip()

        resolved = False
        try:
            if kind == "Pod":
                # Prefer checking the owning Deployment (pod name changes after a fix)
                deployment_name = _agent_ref._get_owner_deployment(name, namespace)
                if deployment_name:
                    dep = _agent_ref.apps_v1.read_namespaced_deployment(
                        name=deployment_name, namespace=namespace
                    )
                    desired = dep.spec.replicas or 1
                    available = dep.status.available_replicas or 0
                    resolved = available >= desired
                else:
                    # Bare pod with no Deployment parent — check phase directly
                    pod = _agent_ref.v1.read_namespaced_pod(name=name, namespace=namespace)
                    resolved = pod.status.phase == "Running"
            elif kind == "Deployment":
                dep = _agent_ref.apps_v1.read_namespaced_deployment(
                    name=name, namespace=namespace
                )
                desired = dep.spec.replicas or 1
                available = dep.status.available_replicas or 0
                resolved = available >= desired
        except Exception:
            pass  # Resource may be gone or unreachable — leave as Pending

        if resolved:
            _update_result(row["id"], "Resolved")


# ─── Flask App ────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)
_agent_ref = None  # set via start_dashboard(agent=...)


# ─── HTML ─────────────────────────────────────────────────────────────────────
_BASE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>K8gentS</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
  <style>
    body { background: #f4f6f9; }
    .navbar-brand { font-weight: 700; letter-spacing: .4px; }
    .stat-card  { border-left: 4px solid #0d6efd; }
    .card-ok    { border-left: 4px solid #198754; }
    .card-warn  { border-left: 4px solid #dc3545; }
    .card-scope { border-left: 4px solid #6f42c1; }
    .ok       { color: #198754; font-weight: 600; }
    .resolved { color: #0d6efd; font-weight: 600; }
    .fail     { color: #dc3545; font-weight: 600; }
    .esc      { color: #6f42c1; font-weight: 600; }
    .pend     { color: #fd7e14; font-weight: 600; }
    .disregard { color: #6c757d; font-weight: 600; }
    .fwd      { color: #0dcaf0; font-weight: 600; }
    .table td, .table th { vertical-align: middle; }
  </style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark mb-4 shadow-sm">
  <div class="container-fluid">
    <a class="navbar-brand" href="/">&#9096;&#65039; K8gentS</a>
    <div class="navbar-nav ms-3">
      <a class="nav-link {{ 'active fw-semibold' if page=='activity' }}" href="/">
        <i class="bi bi-activity"></i> Activity Log</a>
      <a class="nav-link {{ 'active fw-semibold' if page=='cluster' }}" href="/cluster">
        <i class="bi bi-hdd-network"></i> Cluster</a>
      <a class="nav-link {{ 'active fw-semibold' if page=='settings' }}" href="/settings">
        <i class="bi bi-gear"></i> Settings</a>
    </div>
  </div>
</nav>
<div class="container-fluid px-4 pb-5">
  {% block content %}{% endblock %}
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>"""

_ACTIVITY = _BASE.replace("{% block content %}{% endblock %}", """
<div class="d-flex justify-content-between align-items-center mb-3">
  <h5 class="mb-0"><i class="bi bi-activity text-primary"></i> Activity Log</h5>
  <div class="d-flex align-items-center gap-3">
    <span class="text-muted small" id="refresh-countdown"></span>
    <button class="btn btn-sm btn-outline-secondary" onclick="location.reload()">
      <i class="bi bi-arrow-clockwise"></i> Refresh
    </button>
    <span class="text-muted small">{{ rows|length }} events</span>
  </div>
</div>
<div class="card shadow-sm">
  <div class="card-body p-0">
    <div class="table-responsive">
      <table class="table table-hover table-sm mb-0">
        <thead class="table-dark">
          <tr>
            <th>Timestamp</th><th>Incident</th><th>Category</th>
            <th>Object</th><th>Action</th><th>Approved By</th>
            <th>Result</th><th>Confidence</th>
          </tr>
        </thead>
        <tbody>
        {% if rows %}
          {% for r in rows %}
          <tr>
            <td class="text-muted small text-nowrap">{{ r.timestamp }}</td>
            <td><code class="small">{{ r.incident_id or '—' }}</code></td>
            <td>
              {% if r.category %}
              <span class="badge bg-secondary">{{ r.category }}</span>
              {% else %}—{% endif %}
            </td>
            <td class="small">{{ r.object_ref or '—' }}</td>
            <td class="small">{{ r.action or '—' }}</td>
            <td class="small">{{ r.approved_by or '—' }}</td>
            <td class="text-nowrap">
              {% if r.result == 'Fix Applied' %}
                <span class="ok"><i class="bi bi-check-circle-fill"></i> Fix Applied</span>
              {% elif r.result == 'Success' %}
                <span class="ok"><i class="bi bi-check-circle"></i> Success</span>
              {% elif r.result == 'Rolled Back' %}
                <span class="resolved"><i class="bi bi-arrow-counterclockwise"></i> Rolled Back</span>
              {% elif r.result == 'Disregarded' %}
                <span class="disregard"><i class="bi bi-slash-circle"></i> Disregarded</span>
              {% elif r.result == 'Forwarded' %}
                <span class="fwd"><i class="bi bi-forward-fill"></i> Forwarded</span>
              {% elif r.result == 'Failed' %}
                <span class="fail"><i class="bi bi-x-circle"></i> Failed</span>
              {% elif r.result == 'Escalated' %}
                <span class="esc"><i class="bi bi-exclamation-triangle"></i> Escalated</span>
              {% elif r.result == 'Resolved' %}
                <span class="resolved"><i class="bi bi-check2-circle"></i> Resolved</span>
              {% elif r.result == 'Pending' %}
                <span class="pend"><i class="bi bi-hourglass-split"></i> Pending</span>
              {% elif r.result %}
                <span class="text-muted">{{ r.result }}</span>
              {% else %}—{% endif %}
            </td>
            <td style="min-width:80px">
              {% if r.confidence_score is not none %}
              <div class="progress" style="height:16px">
                <div class="progress-bar
                  {% if r.confidence_score >= 75 %}bg-success
                  {% elif r.confidence_score >= 50 %}bg-warning text-dark
                  {% else %}bg-danger{% endif %}"
                  style="width:{{ r.confidence_score }}%;font-size:.7rem">
                  {{ r.confidence_score }}%
                </div>
              </div>
              {% else %}—{% endif %}
            </td>
          </tr>
          {% endfor %}
        {% else %}
          <tr><td colspan="8" class="text-center text-muted py-5">
            No activity yet. The agent will log events here as it detects them.
          </td></tr>
        {% endif %}
        </tbody>
      </table>
    </div>
  </div>
</div>
<script>
  var _secs = 30;
  var _el = document.getElementById('refresh-countdown');
  setInterval(function() {
    _secs--;
    if (_secs <= 0) { location.reload(); }
    else if (_el) { _el.textContent = 'Refreshing in ' + _secs + 's'; }
  }, 1000);
  if (_el) { _el.textContent = 'Refreshing in ' + _secs + 's'; }
</script>
""")

_CLUSTER = _BASE.replace("{% block content %}{% endblock %}", """
<div class="d-flex justify-content-between align-items-center mb-3">
  <h5 class="mb-0"><i class="bi bi-hdd-network text-primary"></i> Cluster</h5>
  <button class="btn btn-sm btn-outline-secondary" onclick="location.reload()">
    <i class="bi bi-arrow-clockwise"></i> Refresh
  </button>
</div>
<div class="row g-3 mb-4">
  <div class="col-sm-4">
    <div class="card shadow-sm stat-card h-100">
      <div class="card-body">
        <p class="text-muted small mb-1"><i class="bi bi-diagram-3"></i> Active Context</p>
        <h5 class="mb-0 text-truncate" title="{{ context_name }}">{{ context_name }}</h5>
      </div>
    </div>
  </div>
  <div class="col-sm-4">
    <div class="card shadow-sm h-100 {{ 'card-ok' if healthy else 'card-warn' }}">
      <div class="card-body">
        <p class="text-muted small mb-1"><i class="bi bi-heart-pulse"></i> Connection Health</p>
        <h5 class="mb-0 {{ 'ok' if healthy else 'fail' }}">
          <i class="bi bi-{{ 'wifi' if healthy else 'wifi-off' }}"></i>
          {{ 'Connected' if healthy else 'Unreachable' }}
        </h5>
        <small class="text-muted">Last event: {{ last_event }}</small>
      </div>
    </div>
  </div>
  <div class="col-sm-4">
    <div class="card shadow-sm h-100 card-scope">
      <div class="card-body">
        <p class="text-muted small mb-1"><i class="bi bi-eye"></i> Watch Scope</p>
        <h5 class="mb-0">{{ watch_scope }}</h5>
      </div>
    </div>
  </div>
</div>
<div class="card shadow-sm">
  <div class="card-header bg-dark text-white">
    <i class="bi bi-boxes"></i> Pod Counts by Namespace
  </div>
  <div class="card-body p-0">
    <div class="table-responsive">
      <table class="table table-hover table-sm mb-0">
        <thead class="table-light">
          <tr><th>Namespace</th><th>Running</th><th>Pending</th><th>Failed / Unknown</th><th>Total</th></tr>
        </thead>
        <tbody>
        {% if namespace_stats %}
          {% for ns in namespace_stats %}
          <tr>
            <td><code>{{ ns.name }}</code></td>
            <td class="ok">{{ ns.running }}</td>
            <td class="pend">{{ ns.pending }}</td>
            <td class="fail">{{ ns.failed }}</td>
            <td><strong>{{ ns.total }}</strong></td>
          </tr>
          {% endfor %}
        {% else %}
          <tr><td colspan="5" class="text-center text-muted py-4">
            {{ error or 'No data available.' }}
          </td></tr>
        {% endif %}
        </tbody>
      </table>
    </div>
  </div>
</div>
""")

_SETTINGS = _BASE.replace("{% block content %}{% endblock %}", """
<div class="d-flex justify-content-between align-items-center mb-3">
  <h5 class="mb-0"><i class="bi bi-gear text-primary"></i> Settings</h5>
</div>
{% if saved %}
<div class="alert alert-success alert-dismissible fade show shadow-sm" role="alert">
  <i class="bi bi-check-circle-fill"></i> Settings saved and applied immediately.
  <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
</div>
{% endif %}
<div class="card shadow-sm">
  <div class="card-body">
    <form method="POST">
      <div class="row g-4">
        <div class="col-md-6">
          <h6 class="text-muted text-uppercase small fw-semibold mb-3">AI & Execution</h6>
          <div class="mb-3">
            <label class="form-label fw-semibold">AI Model</label>
            <input type="text" class="form-control" name="ai_model"
                   value="{{ s.ai_model }}" placeholder="e.g. gemini-2.5-flash">
            <div class="form-text">Takes effect on the next RCA analysis.</div>
          </div>
          <div class="mb-3">
            <label class="form-label fw-semibold">Remediation Mode</label>
            <select class="form-select" name="remediation_mode">
              <option value="api" {{ 'selected' if s.remediation_mode == 'api' }}>
                api — Kubernetes Python client (in-cluster, default)
              </option>
              <option value="subprocess" {{ 'selected' if s.remediation_mode == 'subprocess' }}>
                subprocess — kubectl shell (local dev only)
              </option>
            </select>
          </div>
          <div class="mb-3">
            <label class="form-label fw-semibold">Watch Namespace</label>
            <input type="text" class="form-control" name="watch_namespace"
                   value="{{ s.watch_namespace }}" placeholder="Leave empty to watch all namespaces">
            <div class="form-text text-warning">
              <i class="bi bi-exclamation-triangle"></i> Requires agent restart to take effect.
            </div>
          </div>
        </div>
        <div class="col-md-6">
          <h6 class="text-muted text-uppercase small fw-semibold mb-3">Rate Limiting</h6>
          <div class="mb-3">
            <label class="form-label fw-semibold">Hourly Alert Limit</label>
            <input type="number" class="form-control" name="hourly_alert_limit"
                   value="{{ s.hourly_alert_limit }}" min="1" max="100">
            <div class="form-text">
              Circuit breaker: max RCA analyses per hour. Takes effect immediately.
            </div>
          </div>
          <div class="mb-3">
            <label class="form-label fw-semibold">Debounce Window (minutes)</label>
            <input type="number" class="form-control" name="debounce_minutes"
                   value="{{ s.debounce_minutes }}" min="1" max="120">
            <div class="form-text">
              Suppress duplicate alerts for the same pod within this window.
            </div>
          </div>
        </div>
      </div>
      <hr class="my-3">
      <button type="submit" class="btn btn-primary px-4">
        <i class="bi bi-floppy"></i> Save &amp; Apply
      </button>
      <a href="/settings" class="btn btn-outline-secondary ms-2">Reset</a>
    </form>
  </div>
</div>
""")


# ─── Routes ───────────────────────────────────────────────────────────────────
@flask_app.route("/")
def activity_log_page():
    _resolve_pending_incidents()
    rows = get_activity_log()
    return render_template_string(_ACTIVITY, page="activity", rows=rows)


@flask_app.route("/cluster")
def cluster_page():
    context_name = "Unknown"
    healthy = False
    namespace_stats = []
    error = None

    recent = get_activity_log(limit=1)
    last_event = recent[0]["timestamp"] if recent else "No events recorded yet"

    if _agent_ref is None:
        error = "Agent not yet initialized."
    else:
        # Resolve cluster context name
        try:
            from kubernetes import config as k8s_config
            try:
                _, active = k8s_config.list_kube_config_contexts()
                context_name = active["name"]
            except Exception:
                context_name = "in-cluster"
        except Exception:
            context_name = "Unknown"

        # Connectivity check + pod counts
        try:
            pods = _agent_ref.v1.list_pod_for_all_namespaces(limit=500)
            healthy = True
            counts: dict = {}
            for pod in pods.items:
                ns = pod.metadata.namespace
                phase = pod.status.phase or "Unknown"
                if ns not in counts:
                    counts[ns] = {"running": 0, "pending": 0, "failed": 0, "total": 0}
                counts[ns]["total"] += 1
                if phase == "Running":
                    counts[ns]["running"] += 1
                elif phase == "Pending":
                    counts[ns]["pending"] += 1
                elif phase in ("Failed", "Unknown"):
                    counts[ns]["failed"] += 1
            namespace_stats = [
                {"name": ns, **stats} for ns, stats in sorted(counts.items())
            ]
        except Exception as e:
            error = f"Could not reach cluster API: {e}"

    watch_scope = get_setting("watch_namespace") or "All namespaces"

    return render_template_string(
        _CLUSTER,
        page="cluster",
        context_name=context_name,
        healthy=healthy,
        last_event=last_event,
        watch_scope=watch_scope,
        namespace_stats=namespace_stats,
        error=error,
    )


@flask_app.route("/settings", methods=["GET", "POST"])
def settings_page():
    saved = False
    if request.method == "POST":
        save_settings({
            "ai_model":           request.form.get("ai_model", "").strip(),
            "remediation_mode":   request.form.get("remediation_mode", "api"),
            "watch_namespace":    request.form.get("watch_namespace", "").strip(),
            "hourly_alert_limit": int(request.form.get("hourly_alert_limit", 10)),
            "debounce_minutes":   int(request.form.get("debounce_minutes", 15)),
        })
        saved = True

    s = {k: get_setting(k) for k in SETTINGS_DEFAULTS}
    return render_template_string(_SETTINGS, page="settings", s=s, saved=saved)


# ─── Startup ──────────────────────────────────────────────────────────────────
def start_dashboard(agent, port: int = 8080):
    """Initialise the agent reference and start Flask in a daemon thread."""
    global _agent_ref
    _agent_ref = agent

    # Silence Flask/Werkzeug request logs so they don't clutter the agent output
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    t = threading.Thread(
        target=lambda: flask_app.run(
            host="0.0.0.0", port=port, debug=False, use_reloader=False
        ),
        daemon=True,
        name="K8gentDashboard",
    )
    t.start()
    logger.info(f"Dashboard available at http://localhost:{port}")

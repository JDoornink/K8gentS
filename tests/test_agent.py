import sys
import os
import pytest
from unittest.mock import patch, MagicMock

# Mock required environment variables before importing main module
os.environ["SLACK_BOT_TOKEN"] = "xoxb-dummy-token"
os.environ["SLACK_APP_TOKEN"] = "xapp-dummy-token"
os.environ["AI_API_KEY"] = "dummy-key"

import sys
slack_bolt_mock = MagicMock()
sys.modules['slack_bolt'] = slack_bolt_mock
sys.modules['slack_bolt.adapter.socket_mode'] = MagicMock()
os.environ["AI_API_KEY"] = "dummy-key"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))
from main import RCAAgent

@pytest.fixture
def agent():
    # Mocking kubernetes config load and genai client so unit tests can run offline
    with patch('main.config.load_incluster_config'), \
         patch('main.config.load_kube_config'), \
         patch('main.client.CoreV1Api'), \
         patch('main.client.AppsV1Api'), \
         patch('main.genai.Client'):
        return RCAAgent()

def test_sanitize_logs_ip_redaction(agent):
    logs = "Connection closed by 192.168.1.100 unexpectedly."
    sanitized = agent.sanitize_logs(logs)
    assert "192.168.1.100" not in sanitized
    assert "[REDACTED_IP]" in sanitized

def test_sanitize_logs_token_redaction(agent):
    logs = "Bearer token1234abcd unauthorized"
    sanitized = agent.sanitize_logs(logs)
    assert "token1234abcd" not in sanitized
    assert "Bearer [REDACTED_TOKEN]" in sanitized

def test_sanitize_logs_email_redaction(agent):
    logs = "User admin@company.com failed login"
    sanitized = agent.sanitize_logs(logs)
    assert "admin@company.com" not in sanitized
    assert "[REDACTED_EMAIL]" in sanitized

def test_sanitize_logs_password_redaction(agent):
    logs = "DB_PASSWORD=supersecret123 starting db"
    sanitized = agent.sanitize_logs(logs)
    assert "supersecret123" not in sanitized
    assert "DB_PASSWORD=[REDACTED_PASSWORD]" in sanitized

def test_sanitize_logs_empty(agent):
    assert agent.sanitize_logs(None) == ""
    assert agent.sanitize_logs("") == ""

def test_rate_limiter_global(agent):
    # Spoof the state to simulate hitting the rate limit
    agent.hourly_alerts = 10
    
    # Create mock event object
    event_obj = MagicMock()
    event_obj.reason = "CrashLoopBackOff"
    event_obj.metadata.namespace = "default"
    event_obj.involved_object.kind = "Pod"
    event_obj.involved_object.name = "test-pod"

    # Capture logs to verify it drops the event
    with patch('main.logger.warning') as mock_warning:
        agent.handle_error_event(event_obj)
        mock_warning.assert_called_with("Global K8gent RCA rate limit hit (10/hr). Dropping event to save tokens.")

import os
import sys
import pytest
import asyncio
from unittest.mock import patch, MagicMock

os.environ["AI_API_KEY"] = "dummy-key"

import kubernetes
with patch('kubernetes.config.load_incluster_config', side_effect=kubernetes.config.config_exception.ConfigException("Not in cluster")), \
     patch('kubernetes.config.load_kube_config'), \
     patch('kubernetes.client.CoreV1Api'), \
     patch('kubernetes.client.AppsV1Api'):

    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))
    import mcp_server

def test_sanitize_ip_redaction():
    logs = "Connection reset by 192.168.1.100."
    sanitized = mcp_server.sanitize(logs)
    assert "192.168.1.100" not in sanitized
    assert "[REDACTED_IP]" in sanitized

def test_sanitize_jwt_redaction():
    logs = "Token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6y"
    sanitized = mcp_server.sanitize(logs)
    assert "eyJ" not in sanitized
    assert "[REDACTED_JWT]" in sanitized

def test_sanitize_password_redaction():
    logs = "DB_PASSWORD=supersecret"
    sanitized = mcp_server.sanitize(logs)
    assert "supersecret" not in sanitized
    assert "DB_PASSWORD=[REDACTED_PASSWORD]" in sanitized

def test_run_rca_success():
    with patch('google.genai.Client') as mock_genai_client_class:
        # Setup mock
        mock_client = MagicMock()
        mock_model_response = MagicMock()
        mock_model_response.text = "This is a mock RCA report."
        mock_client.models.generate_content.return_value = mock_model_response
        mock_genai_client_class.return_value = mock_client
        
        context = {"pod": "test-pod", "logs": "error"}
        
        report = asyncio.run(mcp_server._run_rca(context))
        
        assert report == "This is a mock RCA report."
        mock_client.models.generate_content.assert_called_once()
        args, kwargs = mock_client.models.generate_content.call_args
        assert "test-pod" in kwargs['contents']

def test_run_rca_exception():
    with patch('google.genai.Client') as mock_genai_client_class:
        mock_genai_client_class.side_effect = Exception("API down")
        
        context = {"pod": "test-pod"}
        report = asyncio.run(mcp_server._run_rca(context))
        
        assert "AI analysis unavailable: API down" in report

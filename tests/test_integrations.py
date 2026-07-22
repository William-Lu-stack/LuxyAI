import asyncio
import base64
import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException, Request

from backend.app import main as server
from cloud.adapters import cloud_adapters_payload
from mcp_servers import k8s_mcp_server


class CollaborationIntegrationTests(unittest.TestCase):
    @staticmethod
    def _fixture_credential(label: str) -> str:
        return "-".join(("fixture", label, "value"))

    @staticmethod
    def _request(method: str, path: str) -> Request:
        return Request({
            "type": "http", "method": method, "path": path,
            "headers": [], "query_string": b"", "server": ("test", 80),
            "client": ("127.0.0.1", 12345), "scheme": "http",
        })

    def test_slack_requires_complete_credentials(self):
        credential = self._fixture_credential("slack")
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": credential}, clear=True):
            self.assertFalse(server._collaboration_configured("slack"))

        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": credential, "SLACK_CHANNEL": "#sre"}, clear=True):
            self.assertTrue(server._collaboration_configured("slack"))

    def test_telegram_requires_token_and_chat(self):
        credential = self._fixture_credential("telegram")
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": credential}, clear=True):
            self.assertFalse(server._collaboration_configured("telegram"))

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": credential, "TELEGRAM_CHAT_ID": "42"}, clear=True):
            self.assertTrue(server._collaboration_configured("telegram"))

    def test_unconfigured_channel_is_rejected_before_network(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HTTPException) as context:
                asyncio.run(server._send_collaboration_notification("webhook", "test"))
        self.assertEqual(context.exception.status_code, 409)

    def test_cloud_adapter_registry_includes_generic_storage_and_virtualization_platform(self):
        payload = cloud_adapters_payload()
        ids = {item["id"] for item in payload["available"]}
        self.assertIn("generic-storage", ids)
        self.assertIn("virtualization-platform", ids)

    def test_admin_mode_requires_secret_injected_basic_identity(self):
        password = self._fixture_credential("admin")
        encoded = base64.b64encode(f"admin:{password}".encode("utf-8")).decode("ascii")
        request = Request({
            "type": "http", "method": "GET", "path": "/api/session",
            "headers": [(b"authorization", f"Basic {encoded}".encode("ascii"))],
            "client": ("127.0.0.1", 12345),
        })
        with patch.dict(os.environ, {
            "CONSOLE_ADMIN_MODE": "true",
            "CONSOLE_ADMIN_USERS": "admin",
            "CONSOLE_BASIC_AUTH_USERNAME": "admin",
            "CONSOLE_BASIC_AUTH_PASSWORD": password,
        }, clear=True):
            self.assertTrue(server._request_is_admin(request))
        with patch.dict(os.environ, {
            "CONSOLE_ADMIN_MODE": "false",
            "CONSOLE_ADMIN_USERS": "admin",
            "CONSOLE_BASIC_AUTH_USERNAME": "admin",
            "CONSOLE_BASIC_AUTH_PASSWORD": password,
        }, clear=True):
            self.assertFalse(server._request_is_admin(request))

    def test_admin_guard_allows_read_only_knowledge_and_skill_inference(self):
        self.assertFalse(server._admin_write_route(self._request("POST", "/api/knowledge/ask")))
        self.assertFalse(server._admin_write_route(self._request("POST", "/api/ops/skills/match")))
        self.assertTrue(server._admin_write_route(self._request("POST", "/api/knowledge/upload")))
        self.assertTrue(server._admin_write_route(self._request("POST", "/api/ops/skills/import")))
        self.assertTrue(server._admin_write_route(self._request("POST", "/api/model-registry/active")))

    def test_kubernetes_tools_report_unconfigured_access_without_crashing(self):
        with patch.object(k8s_mcp_server, "_HOST", ""), patch.object(
            k8s_mcp_server, "_ACCESS_MODE", "unconfigured"
        ):
            self.assertEqual(
                k8s_mcp_server.kubernetes_access_status(),
                {"configured": False, "mode": "unconfigured", "host": ""},
            )
            result = k8s_mcp_server.list_pods("default")
        self.assertIn("Kubernetes access is not configured", result["error"])


if __name__ == "__main__":
    unittest.main()

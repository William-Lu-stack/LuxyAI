import unittest

from backend.app.services.external_traffic import build_external_traffic_payload


class ExternalTrafficTests(unittest.TestCase):
    def test_service_and_ingress_generate_boundary_flows(self):
        resources = [{
            "cluster": {"id": "c-prod", "name": "prod"},
            "pods": [{
                "metadata": {
                    "name": "api-abc",
                    "namespace": "app",
                    "labels": {"app": "api"},
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "api-75fd8"}],
                },
                "spec": {"containers": [{"name": "api", "env": []}]},
                "status": {"podIP": "192.0.2.20"},
            }],
            "services": [
                {
                    "metadata": {"name": "api", "namespace": "app"},
                    "spec": {"selector": {"app": "api"}, "ports": [{"port": 8080, "protocol": "TCP"}]},
                },
                {
                    "metadata": {"name": "partner", "namespace": "app"},
                    "spec": {"type": "ExternalName", "externalName": "partner.example.com", "ports": [{"port": 443}]},
                },
            ],
            "endpoints": [],
            "endpoint_slices": [],
            "ingresses": [{
                "metadata": {"name": "api-public", "namespace": "app"},
                "spec": {"rules": [{"host": "api.example.com", "http": {"paths": [{"backend": {"service": {"name": "api"}}}]}}]},
            }],
        }]
        payload = build_external_traffic_payload(resources, scope={"cluster": "all", "namespace": "app"})
        directions = {flow["direction"] for flow in payload["flows"]}
        self.assertIn("ingress", directions)
        self.assertIn("egress", directions)
        self.assertGreaterEqual(payload["summary"]["total"], 2)

    def test_pod_env_url_is_detected_and_redacted(self):
        credential = "user:" + "test-credential"
        resources = [{
            "cluster": {"id": "c-prod", "name": "prod"},
            "pods": [{
                "metadata": {
                    "name": "api-abc",
                    "namespace": "app",
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "api-75fd8"}],
                },
                "spec": {
                    "containers": [{
                        "name": "api",
                        "env": [{"name": "DATABASE_URL", "value": f"postgres://{credential}@db.external.example:5432/app"}],
                    }]
                },
                "status": {"podIP": "192.0.2.20"},
            }],
            "services": [],
            "endpoints": [],
            "endpoint_slices": [],
            "ingresses": [],
        }]
        payload = build_external_traffic_payload(resources, scope={"cluster": "all", "namespace": "app"})
        self.assertEqual(payload["summary"]["egress"], 1)
        flow = payload["flows"][0]
        self.assertEqual(flow["destination"]["address"], "db.external.example")
        self.assertNotIn("secret", " ".join(flow["evidence"]))


if __name__ == "__main__":
    unittest.main()

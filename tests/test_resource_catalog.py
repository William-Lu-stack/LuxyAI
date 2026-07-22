import unittest

from backend.app.services.resource_catalog import build_resource_catalog


class UnifiedResourceCatalogTests(unittest.TestCase):
    def test_merges_kubernetes_and_external_resources_with_filters(self):
        kubernetes = {
            "status": "ok",
            "inventory": [{
                "cluster": {"id": "c-prod", "name": "prod"},
                "namespaces": [{"name": "orders", "status": "Active"}],
                "nodes": [{"name": "node-1", "ready": True, "problems": []}],
                "workloads": [{"name": "orders-api", "kind": "Deployment", "namespace": "orders", "replicas": 2, "ready_replicas": 2}],
                "pods": [{"name": "orders-api-abc", "namespace": "orders", "phase": "Running", "ready": True, "restart_count": 0}],
            }],
        }
        infrastructure = {
            "status": "ok",
            "resources": [{"id": "orders-db", "name": "orders-db", "type": "database", "provider": "oracle", "cluster": "db-prod"}],
        }
        result = build_resource_catalog(kubernetes, infrastructure, cluster="prod", namespace="orders")
        self.assertEqual(result["contract"], "flawless.resource.v1")
        self.assertEqual(result["summary"]["by_type"]["pod"], 1)
        self.assertTrue(all(item["cluster"] == "prod" for item in result["items"]))
        self.assertTrue(all(item["namespace"] == "orders" for item in result["items"]))

        database = build_resource_catalog(kubernetes, infrastructure, resource_type="database")
        self.assertEqual(database["pagination"]["total"], 1)
        self.assertEqual(database["items"][0]["name"], "orders-db")

    def test_pagination_is_stable_and_bounded(self):
        kubernetes = {
            "status": "ok",
            "inventory": [{
                "cluster": {"id": "c1", "name": "cluster-1"},
                "namespaces": [], "nodes": [], "workloads": [],
                "pods": [{"name": f"pod-{index:03d}", "namespace": "default", "phase": "Running", "ready": True} for index in range(8)],
            }],
        }
        first = build_resource_catalog(kubernetes, {"status": "ok", "resources": []}, limit=3)
        second = build_resource_catalog(kubernetes, {"status": "ok", "resources": []}, limit=3, cursor=first["pagination"]["next_cursor"])
        self.assertEqual(len(first["items"]), 3)
        self.assertEqual(len(second["items"]), 3)
        self.assertFalse({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})


if __name__ == "__main__":
    unittest.main()

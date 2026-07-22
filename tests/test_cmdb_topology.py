import unittest

from cmdb.local_cmdb import _collect_cluster_topology


def metadata(name, namespace="production", labels=None, owners=None):
    value = {"name": name, "namespace": namespace, "labels": labels or {}}
    if owners:
        value["ownerReferences"] = owners
    return value


class CmdbTopologyTests(unittest.TestCase):
    def test_succeeded_one_shot_pod_is_not_risk(self):
        resources = {
            "/apis/apps/v1/deployments": {"items": [{
                "metadata": metadata("release-runner"),
                "spec": {"replicas": 1, "template": {"spec": {"containers": []}}},
                "status": {"readyReplicas": 1},
            }]},
            "/apis/apps/v1/statefulsets": {"items": []},
            "/apis/apps/v1/daemonsets": {"items": []},
            "/apis/apps/v1/replicasets": {"items": [{
                "metadata": metadata("release-runner-abc", owners=[{"kind": "Deployment", "name": "release-runner"}]),
            }]},
            "/api/v1/services": {"items": []},
            "/api/v1/pods": {"items": [{
                "metadata": metadata("release-job-1", owners=[{"kind": "ReplicaSet", "name": "release-runner-abc"}]),
                "status": {"phase": "Succeeded", "containerStatuses": [{"ready": False, "restartCount": 0, "state": {"terminated": {"reason": "Completed"}}}]},
            }]},
            "/apis/networking.k8s.io/v1/ingresses": {"items": []},
        }

        result = _collect_cluster_topology("cluster-a", "production-cluster", lambda path: resources[path], None)
        pod = next(node for node in result["nodes"] if node.get("kind") == "Pod")
        self.assertEqual(pod["risk"], "normal")
        self.assertEqual(pod["phase"], "Succeeded")
        self.assertTrue(pod["id"].startswith("cluster:cluster-a:"))
        self.assertTrue(any(edge["type"] == "owns" and edge["target"] == pod["id"] for edge in result["edges"]))


if __name__ == "__main__":
    unittest.main()

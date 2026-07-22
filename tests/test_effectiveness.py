import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import effectiveness


class EffectivenessPersistenceTests(unittest.TestCase):
    def test_records_survive_store_reload_and_keep_remediation_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "effectiveness.json"
            with effectiveness._STORE_LOCK:
                saved_inspections = list(effectiveness.INSPECTION_RUNS)
                saved_outcomes = list(effectiveness.REMEDIATION_OUTCOMES)
                saved_loaded = effectiveness._STORE_LOADED_FROM
                saved_active = effectiveness._STORE_ACTIVE_PATH
            try:
                with patch.dict(os.environ, {
                    "EFFECTIVENESS_STORE_PATH": str(store),
                    "EFFECTIVENESS_STORE_FALLBACK_PATH": str(store),
                }):
                    with effectiveness._STORE_LOCK:
                        effectiveness.INSPECTION_RUNS.clear()
                        effectiveness.REMEDIATION_OUTCOMES.clear()
                        effectiveness._STORE_LOADED_FROM = ""
                        effectiveness._STORE_ACTIVE_PATH = ""

                    effectiveness.record_inspection("prod", "orders", {
                        "source": "rancher",
                        "findings": [{"name": "orders-api-pod", "severity": "P1", "workload": {"kind": "Deployment", "name": "orders-api"}}],
                    }, model_id="deepseek-ops")
                    test_credential = "-".join(("redaction", "fixture", "value"))
                    effectiveness.record_remediation({
                        "id": "plan-2", "cluster": "prod", "namespace": "orders", "target": "Deployment/orders-api",
                        "changes": [{"type": "patch_workload", "workload_name": "orders-api", "api_key": test_credential}],
                    }, {
                        "status": "completed",
                        "results": [{"status": "completed", "change": {"type": "patch_workload", "workload_name": "orders-api"}, "result": {"status": "ok"}}],
                        "verification": {"recovered": True, "recovered_pods": ["orders-api-next"]},
                        "continuation_context": {
                            "lineage_id": "ops-root", "parent_job_id": "ops-parent", "attempt_count": 2,
                            "attempts": [{"attempt": 1, "strategy": "fsGroup", "status": "unresolved"}, {"attempt": 2, "strategy": "initContainer", "status": "completed"}],
                        },
                    }, model_id="deepseek-ops")

                    self.assertTrue(store.exists())
                    self.assertNotIn(test_credential, store.read_text(encoding="utf-8"))
                    with effectiveness._STORE_LOCK:
                        effectiveness.INSPECTION_RUNS.clear()
                        effectiveness.REMEDIATION_OUTCOMES.clear()
                        effectiveness._STORE_LOADED_FROM = ""
                        effectiveness._STORE_ACTIVE_PATH = ""
                    restored = effectiveness.summary()
                    self.assertEqual(restored["summary"]["inspection_runs"], 1)
                    self.assertEqual(restored["summary"]["pods_recovered"], 1)
                    record = restored["recent_remediations"][0]
                    self.assertEqual(record["lineage_id"], "ops-root")
                    self.assertEqual(record["lineage_attempt"], 2)
                    self.assertEqual(len(record["attempted_strategies"]), 2)
            finally:
                with effectiveness._STORE_LOCK:
                    effectiveness.INSPECTION_RUNS[:] = saved_inspections
                    effectiveness.REMEDIATION_OUTCOMES[:] = saved_outcomes
                    effectiveness._STORE_LOADED_FROM = saved_loaded
                    effectiveness._STORE_ACTIVE_PATH = saved_active


if __name__ == "__main__":
    unittest.main()

import os
import unittest

from agents.aiops_observability import (
    estimate_llm_cost_usd,
    quality_score_from_diagnosis,
    redact_sensitive,
    trace_hierarchy_schema,
)


class AIOpsObservabilityTests(unittest.TestCase):
    def test_redacts_nested_secrets_and_bearer_tokens(self):
        test_credential = "-".join(("redaction", "fixture", "value"))
        payload = {
            "headers": {"Authorization": "Bearer " + ("x" * 32)},
            "client_secret": test_credential,
            "safe": "client_" + f"secret={test_credential} token={test_credential}",
        }
        redacted = redact_sensitive(payload)
        self.assertEqual(redacted["client_secret"], "[REDACTED]")
        self.assertIn("[REDACTED]", redacted["headers"]["Authorization"])
        self.assertIn("client_secret=[REDACTED]", redacted["safe"])
        self.assertIn("token=[REDACTED]", redacted["safe"])

    def test_estimates_cost_from_env_rates(self):
        old_in = os.environ.get("LLM_COST_INPUT_PER_1K")
        old_out = os.environ.get("LLM_COST_OUTPUT_PER_1K")
        os.environ["LLM_COST_INPUT_PER_1K"] = "0.01"
        os.environ["LLM_COST_OUTPUT_PER_1K"] = "0.02"
        try:
            self.assertEqual(
                estimate_llm_cost_usd({"input_tokens": 1000, "output_tokens": 500}),
                0.02,
            )
        finally:
            if old_in is None:
                os.environ.pop("LLM_COST_INPUT_PER_1K", None)
            else:
                os.environ["LLM_COST_INPUT_PER_1K"] = old_in
            if old_out is None:
                os.environ.pop("LLM_COST_OUTPUT_PER_1K", None)
            else:
                os.environ["LLM_COST_OUTPUT_PER_1K"] = old_out

    def test_quality_score_rewards_evidence_and_actions(self):
        weak = quality_score_from_diagnosis({"root_cause": "unknown", "confidence": 0.2})
        strong = quality_score_from_diagnosis({
            "root_cause": "OOMKilled due to memory limit",
            "impact": "one workload degraded",
            "confidence": 0.9,
            "signals": [{"source": "events", "finding": "OOMKilled"}, {"source": "logs", "finding": "exit 137"}],
            "immediate_actions": ["read previous logs", "patch memory", "verify rollout"],
            "proposed_changes": [{"type": "patch_workload"}],
            "need_human_approval": True,
        }, {"logs": {"previous": "Killed"}})
        self.assertGreater(strong["overall"], weak["overall"])
        self.assertGreaterEqual(strong["safety_gate"], 0.9)

    def test_trace_schema_has_generation_and_tool_call(self):
        schema = trace_hierarchy_schema()
        names = {item["name"] for item in schema["observations"]}
        self.assertIn("llm_diagnosis", names)
        self.assertIn("healing_agent", names)


if __name__ == "__main__":
    unittest.main()

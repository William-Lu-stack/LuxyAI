"""告警扫描、AI 巡检、MCP 调用和受控运维任务接口。"""

from ._registry import build_feature_router


def build_router(runtime):
    return build_feature_router(runtime, [
        ("POST", "/api/alert", "proxy_alert"),
        ("POST", "/api/alert/scan", "scan_and_trigger_alert"),
        ("POST", "/api/inspection/run", "run_ai_inspection"),
        ("POST", "/api/inspection/preview", "preview_ai_inspection_finding"),
        ("GET", "/api/infrastructure/resources", "list_infrastructure_resources"),
        ("GET", "/api/infrastructure/providers", "infrastructure_providers"),
        ("POST", "/api/infrastructure/scan", "scan_infrastructure_resources"),
        ("POST", "/api/mcp/call", "mcp_call"),
        ("POST", "/api/ops/execute", "execute_ops_plan"),
        ("GET", "/api/ops/capabilities", "ops_capabilities"),
        ("GET", "/api/ops/skills", "list_ops_skills"),
        ("POST", "/api/ops/skills", "upsert_ops_skill"),
        ("POST", "/api/ops/skills/import", "import_ops_skill_package"),
        ("GET", "/api/ops/skills/{skill_id}/export", "export_ops_skill_package"),
        ("POST", "/api/ops/skills/match", "match_ops_skills"),
        ("POST", "/api/ops/skills/{skill_id}/delete", "delete_ops_skill"),
        ("DELETE", "/api/ops/skills/{skill_id}", "delete_ops_skill"),
        ("POST", "/api/ops/jobs", "create_ops_job"),
        ("GET", "/api/ops/jobs/{job_id}", "get_ops_job"),
        ("POST", "/api/ops/jobs/{job_id}/approve-step", "approve_ops_job_step"),
        ("POST", "/api/ops/jobs/{job_id}/cancel", "cancel_ops_job"),
    ], tag="AI 运维执行")

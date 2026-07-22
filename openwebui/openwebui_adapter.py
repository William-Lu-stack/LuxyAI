import traceback

from fastapi import FastAPI
from pydantic import BaseModel, Field

from agents.llm_client import get_llm, _token_cache
from agents.model_registry import select_model_profile
from agents.sre_graph import build_graph

app = FastAPI(title="Open WebUI Adapter")

sre_graph = build_graph()


@app.get("/health")
async def health():
    return {"status": "ok", "component": "openwebui-adapter"}


@app.get("/llm/health")
async def llm_health(profile_id: str = ""):
    try:
        profile = select_model_profile(profile_id or None)
        token_length = 0 if (profile.auth_type or "").lower() in {"none", "noauth", "anonymous"} else len(profile.api_key or profile.client_secret or "configured")
        response = get_llm(max_tokens=64, profile_id=profile.id).invoke("只返回两个字：成功")
        return {
            "status": "ok",
            "profile_id": profile.id,
            "model": profile.model,
            "token_length": token_length,
            "answer": response.content,
            "metadata": response.response_metadata,
        }
    except Exception as e:
        return {
            "status": "failed",
            "error": f"{type(e).__name__}: {e}",
            "trace": traceback.format_exc(limit=6),
        }


class ChatRequest(BaseModel):
    message: str
    model_profile_id: str = ""
    cluster: str = "all"
    cluster_id: str = "all"
    namespace: str = "default"
    deployment: str = ""
    workload_type: str = "Deployment"
    pod: str = ""
    target_id: str = ""
    severity: str = "P2"
    auto_healing_enabled: bool = False
    k8s_context: dict = {}
    operator_skills: list[dict] = Field(default_factory=list)
    model_profile_override: dict = {}


@app.post("/chat")
async def chat(req: ChatRequest):
    alert = {
        "alert_name": "ManualInvestigation",
        "cluster": req.cluster,
        "cluster_id": req.cluster_id,
        "namespace": req.namespace,
        "deployment": req.deployment,
        "workload_name": req.deployment,
        "workload_type": req.workload_type,
        "pod": req.pod,
        "target_id": req.target_id,
        "service": req.deployment,
        "severity": req.severity,
        "priority": "high",
        "summary": req.message,
        "auto_healing_enabled": req.auto_healing_enabled,
        "model_profile_id": req.model_profile_id,
    }

    try:
        initial_state = {"alert": alert, "model_profile_id": req.model_profile_id}
        if req.model_profile_override:
            initial_state["model_profile_override"] = req.model_profile_override
        if req.k8s_context:
            initial_state["k8s_context"] = req.k8s_context
        if req.operator_skills:
            initial_state["operator_skills"] = req.operator_skills
        result = await sre_graph.ainvoke(initial_state)
    except Exception as e:
        return {
            "answer": f"SRE 对话处理失败：{type(e).__name__}: {e}",
            "postmortem": None,
            "raw": {
                "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc(limit=6),
                "alert": alert,
            },
        }

    safe_result = dict(result)
    safe_result.pop("model_profile_override", None)
    return {
        "answer": safe_result.get("final_answer"),
        "postmortem": safe_result.get("postmortem"),
        "raw": safe_result,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8200)

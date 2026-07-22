"""
MCP HTTP Server — 将 k8s_mcp_server 的 tool 暴露为 HTTP 接口
本地开发可用，K8s 内作为 sidecar 或独立 pod 运行。
"""
import os
import secrets
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import traceback
from urllib.error import HTTPError, URLError
from fastapi.middleware.cors import CORSMiddleware
from mcp_servers.k8s_mcp_server import *


app = FastAPI(title="K8S MCP HTTP Gateway", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[item.strip() for item in os.getenv("MCP_CORS_ALLOW_ORIGINS", "").split(",") if item.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ToolCallRequest(BaseModel):
    tool: str
    arguments: dict = {}


@app.get("/health")
async def health():
    return {"status": "ok", "kubernetes": kubernetes_access_status()}


@app.post("/mcp/tools/call")
async def call_tool(req: ToolCallRequest, request: Request):
    internal_key = os.getenv("INTERNAL_API_KEY", "").strip()
    if internal_key:
        supplied = request.headers.get("X-Internal-API-Key", "")
        if not secrets.compare_digest(supplied, internal_key):
            raise HTTPException(status_code=401, detail="internal api key required")

    from mcp_servers.k8s_mcp_server import (
        list_pods,
        get_pod_events,
        get_pod_logs,
        get_pod_diagnostics,
        restart_deployment,
        scale_deployment,
        patch_workload,
        create_workload,
        patch_service,
        patch_service_account,
        create_configmap,
        patch_pdb,
        recreate_pod,
        evict_pod,
        patch_hpa,
        expand_pvc,
        create_pvc,
        create_persistent_volume,
        cordon_node,
        get_remediation_target_state,
        list_nodes,
        check_access,
        list_pod_metrics,
        # 新增聚合工具
        get_cluster_summary,
        list_all_pods,
        list_all_deployments,
        list_namespaces,
        get_resilience_topology,
        get_external_traffic_candidates,
    )

    tool_map = {
        "list_pods": list_pods,
        "get_pod_events": get_pod_events,
        "get_pod_logs": get_pod_logs,
        "get_pod_diagnostics": get_pod_diagnostics,
        "restart_deployment": restart_deployment,
        "scale_deployment": scale_deployment,
        "patch_workload": patch_workload,
        "create_workload": create_workload,
        "patch_service": patch_service,
        "patch_service_account": patch_service_account,
        "create_configmap": create_configmap,
        "patch_pdb": patch_pdb,
        "recreate_pod": recreate_pod,
        "evict_pod": evict_pod,
        "patch_hpa": patch_hpa,
        "expand_pvc": expand_pvc,
        "create_pvc": create_pvc,
        "create_persistent_volume": create_persistent_volume,
        "cordon_node": cordon_node,
        "get_remediation_target_state": get_remediation_target_state,
        "list_nodes": list_nodes,
        "check_access": check_access,
        "list_pod_metrics": list_pod_metrics,
        "get_cluster_summary": get_cluster_summary,
        "list_all_pods": list_all_pods,
        "list_all_deployments": list_all_deployments,
        "list_namespaces": list_namespaces,
        "get_resilience_topology": get_resilience_topology,
        "get_external_traffic_candidates": get_external_traffic_candidates,
    }

    fn = tool_map.get(req.tool)
    if fn is None:
        raise HTTPException(status_code=400, detail=f"Unknown tool: {req.tool}")

    try:
        result = fn(**req.arguments)
        return result
    except HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(e)
        detail = {
            "tool": req.tool,
            "http_status": e.code,
            "reason": e.reason,
            "body": raw[:4000],
            "message": "Kubernetes API rejected the operation; this is not an MCP server crash.",
        }
        print(f"Kubernetes HTTPError calling {req.tool}: {detail}")
        raise HTTPException(status_code=e.code if 400 <= e.code < 500 else 502, detail=detail)
    except URLError as e:
        detail = {
            "tool": req.tool,
            "reason": str(e.reason),
            "message": "Kubernetes API is unreachable from the MCP server.",
        }
        print(f"Kubernetes URLError calling {req.tool}: {detail}")
        raise HTTPException(status_code=503, detail=detail)
    except PermissionError as e:
        print(f"PermissionError calling {req.tool}: {e}")
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        print(f"ValueError calling {req.tool}: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"ERROR calling tool '{req.tool}':")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8105)

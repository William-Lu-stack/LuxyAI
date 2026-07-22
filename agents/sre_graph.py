"""
SRE Graph — 核心编排引擎 (本地化版)

变更：
  - 使用本地 LLM (Ollama/vLLM/LocalAI) 做 AI 诊断，替代规则匹配
  - 使用 LangFuse 自托管做全链路追踪
  - 通过 MCP client 调用 k8s-mcp-server（不再用 fake 数据）
  - 证书校验由 OUTBOUND_VERIFY_SSL 控制，生产建议开启并配置企业 CA
"""
import os
import json
import uuid
import traceback
from typing import TypedDict, Any, Literal

import httpx
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END

from agents.aiops_observability import (
    end_observation,
    estimate_llm_cost_usd,
    flush as flush_observability,
    langfuse_status,
    new_trace_id,
    observation_id,
    quality_score_from_diagnosis,
    redact_sensitive as _redact_sensitive,
    score_observation,
    start_generation,
    start_span,
    start_trace,
    update_trace,
)
from agents.llm_client import get_llm
from agents.remediation_engine import ACTION_CATALOG, build_remediation_plan, expert_steps_from_diagnosis

load_dotenv()

# ============================================================
# 内部服务地址
# ============================================================
HEALING_AGENT_URL = os.getenv("HEALING_AGENT_URL", "http://localhost:8101/a2a/tasks")
INCIDENT_AGENT_URL = os.getenv("INCIDENT_AGENT_URL", "http://localhost:8102/a2a/tasks")
POSTMORTEM_AGENT_URL = os.getenv("POSTMORTEM_AGENT_URL", "http://localhost:8103/a2a/tasks")

# MCP Server 地址（本地可以是 stdio，K8s 内可以是 SSE 端点）
# 这里 HTTP 模式方便 K8s Service 调用
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8105/mcp")

# ============================================================
# 私有网络 http 客户端（关闭证书验证，适配自签证书环境）
# ============================================================
def _http_client(timeout: int = 30) -> httpx.AsyncClient:
    verify = os.getenv("OUTBOUND_VERIFY_SSL", "true").lower() in {"1", "true", "yes", "on"}
    return httpx.AsyncClient(timeout=timeout, verify=verify)


def _mcp_tools_url() -> str:
    base = MCP_SERVER_URL.rstrip("/")
    if base.endswith("/mcp"):
        return base + "/tools/call"
    return base + "/mcp/tools/call"


# ============================================================
# State
# ============================================================
class SREState(TypedDict, total=False):
    alert: dict[str, Any]
    model_profile_id: str
    model_profile_override: dict[str, Any]
    k8s_context: dict[str, Any]
    operator_skills: list[dict[str, Any]]
    diagnosis: dict[str, Any]
    decision: dict[str, Any]
    remediation: dict[str, Any]
    incident: dict[str, Any]
    postmortem: dict[str, Any]
    observability: dict[str, Any]
    final_answer: str


SUPPORTED_ACTIONS = set(ACTION_CATALOG) | {"execute_plan", "investigate"}
_TRACE_CACHE: dict[str, Any] = {}


def _alert_scope(alert: dict[str, Any]) -> dict[str, str]:
    cluster_id = str(alert.get("cluster_id") or alert.get("cluster") or "local-cluster")
    cluster = str(alert.get("cluster") or cluster_id)
    namespace = str(alert.get("namespace") or "default")
    workload = str(alert.get("deployment") or alert.get("workload_name") or alert.get("pod") or "scope")
    workload_type = str(alert.get("workload_type") or "Workload")
    return {
        "cluster": cluster,
        "cluster_id": cluster_id,
        "namespace": namespace,
        "workload": workload,
        "workload_type": workload_type,
    }


def _pod_matches_workload(pod: dict[str, Any], workload_name: str) -> bool:
    workload_name = str(workload_name or "").strip()
    if not workload_name:
        return True
    workload = pod.get("workload") or {}
    return (
        workload_name in {str(pod.get("workload_name") or ""), str(workload.get("name") or "")}
        or str(pod.get("name") or "").startswith(f"{workload_name}-")
    )


def _pod_evidence_priority(pod: dict[str, Any]) -> tuple[int, int, str]:
    state_text = " ".join(
        " ".join([
            str(container.get("state", "")),
            str(container.get("reason", "")),
            str((container.get("state_detail") or {}).get("reason", "")),
            str((container.get("state_detail") or {}).get("message", "")),
        ])
        for container in pod.get("containers", [])
    ).lower()
    score = 0
    if any(term in state_text for term in ("crashloopbackoff", "oomkilled", "back-off", "error")):
        score += 500
    if any(term in state_text for term in ("imagepullbackoff", "errimagepull", "pull access denied")):
        score += 480
    if any(term in state_text for term in ("failedmount", "persistentvolumeclaim", "permission denied")):
        score += 460
    if str(pod.get("phase") or "") == "Pending":
        score += 240
    if not pod.get("ready"):
        score += 120
    restart_count = int(pod.get("restart_count") or 0)
    score += min(120, restart_count * 10)
    if pod.get("completed"):
        score -= 1000
    return score, restart_count, str(pod.get("name") or "")


def _select_evidence_pod(
    pods: list[dict[str, Any]],
    *,
    workload_name: str = "",
    requested_pod: str = "",
) -> dict[str, Any] | None:
    requested_pod = str(requested_pod or "").strip()
    if requested_pod:
        return next((pod for pod in pods if requested_pod == str(pod.get("name") or "")), None)
    matches = [pod for pod in pods if _pod_matches_workload(pod, workload_name)]
    if not matches:
        return None
    matches.sort(key=_pod_evidence_priority, reverse=True)
    return matches[0]


def _ensure_trace(state: SREState, stage: str) -> tuple[Any, dict[str, Any]]:
    alert = state.get("alert") or {}
    scope = _alert_scope(alert)
    observability = dict(state.get("observability") or {})
    trace_id = observability.get("trace_id") or new_trace_id("sre")
    session_id = observability.get("session_id") or (
        f"sre:{scope['cluster_id']}:{scope['namespace']}:{scope['workload']}:{alert.get('alert_name') or 'chat'}"
    )
    observability.update({
        "trace_id": trace_id,
        "session_id": session_id,
        "stage": stage,
        "langfuse": langfuse_status(),
    })
    trace = _TRACE_CACHE.get(trace_id)
    if trace is None:
        trace = start_trace(
            "flawless.sre.workflow",
            trace_id=trace_id,
            user_id=str(alert.get("user_id") or alert.get("operator") or "flawless-operator"),
            session_id=session_id,
            input={"alert": alert, "scope": scope},
            metadata={
                **scope,
                "alert_name": alert.get("alert_name"),
                "severity": alert.get("severity"),
                "model_profile_id": state.get("model_profile_id") or alert.get("model_profile_id"),
                "workflow": "sre_graph",
                "created_at": observability.get("created_at") or trace_id,
            },
            tags=["flawless", "aiops", "sre", str(alert.get("severity") or "P2")],
        )
        _TRACE_CACHE[trace_id] = trace
    return trace, observability


def _extract_json_object(content: str) -> dict[str, Any]:
    content = content.strip()
    if "```" in content:
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start : end + 1])
        raise


def _policy_action(alert: dict[str, Any], diagnosis: dict[str, Any]) -> tuple[str, str]:
    """Deterministic AIOps guardrail for remediable alert classes."""
    alert_name = str(alert.get("alert_name") or "").lower()
    summary = str(alert.get("summary") or alert.get("description") or "").lower()
    root_cause = str(diagnosis.get("root_cause") or "").lower()
    signal_text = " ".join(
        str(item.get("finding", item)) if isinstance(item, dict) else str(item)
        for item in diagnosis.get("signals", [])
    ).lower()
    corpus = " ".join([alert_name, summary, root_cause, signal_text])

    if any(k in corpus for k in ["oomkilled", "out of memory", "内存不足", "内存溢出"]):
        return "patch_workload", "policy: OOM evidence prefers resource patch over restart"

    if any(k in corpus for k in ["probe failed", "liveness", "readiness", "startup probe", "探针"]):
        return "patch_workload", "policy: probe evidence prefers workload probe tuning"

    if any(k in corpus for k in ["permission denied", "failedmount", "mountvolume", "read-only file system", "权限不足", "挂载失败"]):
        return "patch_workload", "policy: storage/config evidence prefers workload securityContext patch"

    if any(k in corpus for k in ["imagepullbackoff", "errimagepull", "pull access denied", "manifest unknown"]):
        return "observe", "policy: image pull failures need registry/tag/secret evidence before mutation"

    if any(k in corpus for k in ["crashloop", "crash looping", "容器反复崩溃", "反复重启"]):
        return "observe", "policy: generic crashloop requires evidence-specific plan before mutation"

    if any(k in corpus for k in ["highcpu", "cpu usage", "cpu 使用率", "高 cpu", "高cpu"]):
        return "scale_out", "policy: high cpu capacity alert"

    if any(k in corpus for k in ["pending", "无法调度", "insufficient", "node affinity", "taint"]):
        return "observe", "policy: scheduling issue requires investigation"

    return "observe", "policy: no safe deterministic remediation"


# ============================================================
# MCP Tool 调用（替代原来的 fake 函数）
# ============================================================
async def mcp_call_tool(tool_name: str, arguments: dict) -> dict:
    """
    通过 HTTP 调用本地 MCP Server 的 tool。
    如果 MCP Server 不可用，回退到直接调用 K8s API。
    """
    try:
        headers = {}
        internal_key = os.getenv("INTERNAL_API_KEY", "").strip()
        if internal_key:
            headers["X-Internal-API-Key"] = internal_key
        async with _http_client() as client:
            resp = await client.post(
                _mcp_tools_url(),
                json={"tool": tool_name, "arguments": arguments},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        # Fallback: 直接 import k8s_mcp_server 的 function（同进程调用）
        from mcp_servers.k8s_mcp_server import (
            list_pods,
            get_pod_events,
            get_pod_logs,
            get_pod_diagnostics,
        )

        tool_map = {
            "list_pods": list_pods,
            "get_pod_events": get_pod_events,
            "get_pod_logs": get_pod_logs,
            "get_pod_diagnostics": get_pod_diagnostics,
        }

        fn = tool_map.get(tool_name)
        if fn is None:
            raise ValueError(f"Unknown MCP tool: {tool_name}; http_error={e}")
        result = fn(**arguments)
        if isinstance(result, dict) and result.get("error"):
            result["mcp_http_error"] = str(e)
        return result


# ============================================================
# Graph Nodes
# ============================================================
async def collect_context(state: SREState) -> SREState:
    alert = state["alert"]
    trace, observability = _ensure_trace(state, "collect_context")
    node_span = start_span(
        trace,
        "01.collect_context",
        input={"alert": alert},
        metadata={"node": "collect_context", "purpose": "collect K8s/Rancher/MCP evidence"},
    )
    provided = state.get("k8s_context") or {}
    if provided.get("source") in {"rancher", "kubeconfig"} and (provided.get("pod") or provided.get("pods")):
        end_observation(node_span, output={"source": provided.get("source"), "context": provided})
        return {**state, "k8s_context": provided, "observability": observability}
    namespace = alert.get("namespace", "default")
    deployment = alert.get("deployment") or alert.get("workload_name") or ""
    requested_pod = alert.get("pod") or ""

    # Step 1: 获取 Pods
    span = start_span(
        trace,
        "tool.mcp.list_pods",
        input={"namespace": namespace},
        metadata={"tool": "list_pods", "stage": "evidence"},
    )
    pods_data = await mcp_call_tool("list_pods", {"namespace": namespace})

    end_observation(span, output=pods_data, metadata={"pods_count": len(pods_data.get("pods", []))})

    # Step 2: select the actual target and collect bounded deep evidence.
    context_data = {"pods": pods_data, "events": {}, "diagnostics": {}}

    selected_pod = _select_evidence_pod(
        pods_data.get("pods", []),
        workload_name=deployment,
        requested_pod=requested_pod,
    ) if (requested_pod or deployment) else None
    if selected_pod and selected_pod.get("name"):
        name = selected_pod["name"]
        span2 = start_span(
            trace,
            "tool.mcp.get_pod_diagnostics",
            input={"namespace": namespace, "pod_name": name, "tail_lines": 160},
            metadata={"tool": "get_pod_diagnostics", "target": name},
        )
        diagnostics = await mcp_call_tool(
            "get_pod_diagnostics",
            {"namespace": namespace, "pod_name": name, "tail_lines": 160},
        )
        end_observation(span2, output=diagnostics, metadata={"events_count": len(diagnostics.get("events", []))})
        context_data["pod"] = diagnostics.get("pod") or selected_pod
        context_data["events"] = {"events": diagnostics.get("events", [])}
        context_data["logs"] = diagnostics.get("logs", {})
        context_data["workload"] = diagnostics.get("workload", {})
        context_data["diagnostics"] = diagnostics
        context_data["matching_pods"] = [
            pod for pod in pods_data.get("pods", [])
            if _pod_matches_workload(pod, deployment)
        ][:8]

    # Alerts sometimes omit a workload. Use the first unhealthy-looking Pod as
    # an evidence target, never as an automatic mutation target by name guess.
    if not context_data.get("pod") and not (requested_pod or deployment):
        candidate = _select_evidence_pod(pods_data.get("pods", []))
        if candidate and candidate.get("name"):
            span3 = start_span(
                trace,
                "tool.mcp.get_pod_diagnostics.autotarget",
                input={"namespace": namespace, "pod_name": candidate["name"], "tail_lines": 160},
                metadata={"tool": "get_pod_diagnostics", "target_source": "unhealthy_candidate"},
            )
            diagnostics = await mcp_call_tool(
                "get_pod_diagnostics",
                {"namespace": namespace, "pod_name": candidate["name"], "tail_lines": 160},
            )
            end_observation(span3, output=diagnostics, metadata={"events_count": len(diagnostics.get("events", []))})
            context_data.update({
                "pod": diagnostics.get("pod") or candidate,
                "events": {"events": diagnostics.get("events", [])},
                "logs": diagnostics.get("logs", {}),
                "workload": diagnostics.get("workload", {}),
                "diagnostics": diagnostics,
            })
    elif not context_data.get("pod"):
        context_data["target_binding_error"] = (
            f"operator-selected pod/{requested_pod} was not found"
            if requested_pod else f"operator-selected workload/{deployment} was not found"
        )

    end_observation(
        node_span,
        output=context_data,
        metadata={
            "pods_count": len(pods_data.get("pods", [])),
            "target_pod": (context_data.get("pod") or {}).get("name"),
            "has_deep_evidence": bool(context_data.get("diagnostics")),
        },
    )
    update_trace(trace, metadata={"last_stage": "collect_context", "target_pod": (context_data.get("pod") or {}).get("name")})

    return {**state, "k8s_context": context_data, "observability": observability}


async def diagnose(state: SREState) -> SREState:
    """使用本地 LLM 做智能诊断，替代之前硬编码的规则匹配"""
    alert = state["alert"]
    context = state["k8s_context"]
    trace, observability = _ensure_trace(state, "diagnose")
    model_profile_id = state.get("model_profile_id") or alert.get("model_profile_id") or ""
    target_pod = context.get("pod") or {}
    scoped_pods = [target_pod] if target_pod and (alert.get("deployment") or alert.get("workload_name") or alert.get("pod")) else context.get("pods", {}).get("pods", [])[:5]
    llm = get_llm(
        temperature=0.1,
        max_tokens=int(os.getenv("LLM_DIAGNOSIS_MAX_TOKENS", "1400")),
        profile_id=model_profile_id or None,
        profile_override=state.get("model_profile_override") or None,
    )

    generation = start_generation(
        trace,
        "02.llm_diagnosis",
        model=getattr(llm, "model_name", os.getenv("LLM_MODEL", "qwen2.5:7b")),
        input={
            "alert": _redact_sensitive(alert),
            "context": {
                "pods": _redact_sensitive(scoped_pods),
                "events": _redact_sensitive(context.get("events", {}).get("events", [])[:10]),
            },
        },
        metadata={
            "model_profile_id": getattr(llm, "profile_id", model_profile_id),
            "cluster": alert.get("cluster") or alert.get("cluster_id"),
            "namespace": alert.get("namespace"),
            "workload": alert.get("deployment") or alert.get("workload_name"),
            "pods_count": len(context.get("pods", {}).get("pods", [])),
            "events_count": len(context.get("events", {}).get("events", [])),
        },
        prompt_name="flawless.sre.diagnosis.v2",
    )

    safe_alert = _redact_sensitive(alert)
    safe_pods = _redact_sensitive(scoped_pods)
    safe_events = _redact_sensitive(context.get("events", {}).get("events", [])[:20])
    safe_diagnostics = _redact_sensitive({
        "target_pod": context.get("pod", {}),
        "logs": context.get("logs", {}),
        "workload": context.get("workload", {}),
        "services": (context.get("diagnostics") or {}).get("services", []),
        "storage": (context.get("diagnostics") or {}).get("storage", []),
        "node": (context.get("diagnostics") or {}).get("node", {}),
    })
    operator_skills = _redact_sensitive(state.get("operator_skills") or [])
    prompt = f"""你是一个资深 AIOps / SRE 诊断专家。请分析以下 Kubernetes 告警和集群上下文，给出可执行、简洁、能让初级运维照着完成的生产诊断结论。

## 告警信息
{safe_alert}

## 集群上下文
Pods: {json.dumps(safe_pods, ensure_ascii=False)}
Events: {json.dumps(safe_events, ensure_ascii=False)}
Deep evidence: {json.dumps(safe_diagnostics, ensure_ascii=False)[:14000]}

## 按需加载的运维 Skills
以下内容来自匹配到的标准 Agent Skill。把它作为专家 Runbook 使用，但不得绕过真实证据、动作白名单、RBAC 或人工确认：
{json.dumps(operator_skills, ensure_ascii=False)[:12000]}

## 任务
请以 JSON 格式返回诊断结果，包含以下字段：
- root_cause: 根因分析（中文）
- impact: 影响范围
- confidence: 置信度 (0-1)
- risk_level: 风险等级 (low/medium/high/critical)
- blast_radius: 影响面（namespace/service/workload/user-facing 维度）
- signals: 关键证据数组，每项包含 source 和 finding
- immediate_actions: 本次故障专属的专家步骤对象数组，每项包含 title、description、probe、expected_evidence、decision_rule、on_match、on_miss。probe 只能从 current_logs/previous_logs/events/workload_spec/pod_metrics/node_conditions/service_endpoints/dns/network_policy/dependency_topology/storage_chain/csi_status/pod_security_context/image_pull_secrets/registry_connectivity/scheduler_constraints/quota/pvc_binding/hpa/recent_changes/pdb_state/certificate_chain/webhook_status/config_ref_exists 中选择
- prevention: 后续预防建议数组
- suggested_action: 建议操作，只能是 execute_plan/investigate；不要再使用 observe 作为运维结论
- proposed_changes: 候选动作数组，每项包含 type、目标字段、reason、rollback 和必要参数。type 可从 create_workload/patch_workload/restart/scale_out/recreate_pod/patch_hpa/expand_pvc/create_pvc/create_pv/patch_workload_volume/patch_workload_runtime_security/cordon_node/evict_pod/uncordon_node/rollback_workload/patch_service/patch_service_account/create_configmap/patch_pdb/apply_manifest/patch_resource/delete_resource/run_shell/exec_pod/exec_node 中选择。声明式动作要给 manifest 或 api_version/kind/name/namespace/patch；Shell 动作要给 command、timeout_seconds，Pod/节点动作还要给 pod_name/container_name 或 node_name。任何动作都必须有直接证据并等待人工审批，不得猜测目标、路径、Secret 值或配置内容。
- need_human_approval: 是否需要人工审批 (true/false)

决策原则：
1. 不要把 CrashLoop 默认等同于 restart。优先根据证据选择 OOM 资源修复、探针修复、配置/存储权限修复、镜像凭据修复或调度修复。
2. ImagePullBackOff 在没有明确默认 imagePullSecret 时不要建议重启；应该输出检查 tag、registry、secret、节点网络的步骤。
3. 只有匹配到的 Skill 明确允许 run_shell/exec_pod/exec_node 时才能生成命令；命令必须完整展示目标、风险、回滚和超时，并进入逐步人工审批。优先使用 Kubernetes API/YAML，不要把 kubectl 字符串伪装成声明式动作。
4. 如果用户问题与 Kubernetes/SRE 无关，root_cause 写“非运维问题”，suggested_action=investigate，immediate_actions 给出正常回答要点，不要生成变更。
5. immediate_actions 必须根据当前对象的真实证据动态生成，不得照抄固定流程。每一步写清“查什么、为何查、什么结果支持/排除哪个根因、下一步怎么走”；覆盖范围确认、并行取证、至少两个根因分支、变更门禁、最小变更、SLI/SLO 恢复验证和失败后的不同策略，通常为 6-10 项。
6. 必须区分症状与根因。没有 previous logs、Events 或真实配置证据时，不得把 restart 当作默认修复。
7. 对复杂问题应覆盖 rollout 回归、PDB 死锁、Service selector/Endpoint、Quota/LimitRange、DNS/CNI、CSI/PVC、证书/Webhook、节点压力和依赖故障等分支。
8. 命中运维 Skill 时，遵循其中的证据顺序和恢复判据；Skill 与实时证据冲突时以实时证据为准。
9. 如果日志不存在、Pod 已删除或 container 尚未产生日志，先用 Events/状态/Workload 模板判断是否有 PVC、镜像、ConfigMap、配额或调度阻断；没有模板级阻断时，可以提出 recreate_pod 作为诊断性重建，重建后必须重新采集 current/previous logs。

只返回 JSON，不要任何其他内容。"""

    try:
        response = llm.invoke(prompt)
        diagnosis = _extract_json_object(response.content)

        # 安全获取 token 用量
        try:
            um = getattr(response, "usage_metadata", {}) or {}
            tm = getattr(response, "response_metadata", {}).get("token_usage", {})
            in_tok = int(um.get("input_tokens", tm.get("input_tokens", 0)))
            out_tok = int(um.get("output_tokens", tm.get("output_tokens", 0)))
        except Exception:
            in_tok = out_tok = 0

        cost_usd = estimate_llm_cost_usd(
            {"input_tokens": in_tok, "output_tokens": out_tok, "total_tokens": in_tok + out_tok},
            model_profile_id=getattr(llm, "profile_id", model_profile_id) or model_profile_id,
            model=getattr(llm, "model_name", os.getenv("LLM_MODEL", "qwen2.5:7b")),
        )
        end_observation(
            generation,
            output=_redact_sensitive(diagnosis),
            usage={"input_tokens": in_tok, "output_tokens": out_tok},
            metadata={"estimated_cost_usd": cost_usd},
        )
        quality_scores = quality_score_from_diagnosis(diagnosis, context)
        for score_name, score_value in quality_scores.items():
            score_observation(
                trace,
                name=f"sre_diagnosis.{score_name}",
                value=score_value,
                comment=f"Flawless diagnostic quality score: {score_name}",
                metadata={"stage": "diagnose", "model_profile_id": getattr(llm, "profile_id", model_profile_id)},
            )
        diagnosis["diagnosis_metadata"] = {
            "source": "llm",
            "model": getattr(llm, "model_name", os.getenv("LLM_MODEL", "qwen2.5:7b")),
            "model_profile_id": getattr(llm, "profile_id", model_profile_id),
            "token_usage": {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "total_tokens": in_tok + out_tok,
            },
            "estimated_cost_usd": cost_usd,
            "quality_scores": quality_scores,
            "langfuse_trace_id": observability.get("trace_id"),
            "langfuse_session_id": observability.get("session_id"),
        }

    except Exception as e:
        diagnosis = _fallback_diagnosis(alert, context)
        diagnosis["llm_error"] = {
            "type": type(e).__name__,
            "message": str(e),
            "trace": traceback.format_exc(limit=4),
        }

        end_observation(
            generation,
            output=_redact_sensitive(diagnosis),
            status_message=f"fallback: LLM failed, using rule-based diagnosis: {e}",
            level="ERROR",
        )
        quality_scores = quality_score_from_diagnosis(diagnosis, context)
        for score_name, score_value in quality_scores.items():
            score_observation(trace, name=f"sre_diagnosis.{score_name}", value=score_value, comment="fallback diagnosis score")
        diagnosis["diagnosis_metadata"] = {
            "source": "fallback",
            "model": getattr(llm, "model_name", os.getenv("LLM_MODEL", "qwen2.5:7b")),
            "model_profile_id": getattr(llm, "profile_id", model_profile_id),
            "token_usage": {},
            "quality_scores": quality_scores,
            "langfuse_trace_id": observability.get("trace_id"),
            "langfuse_session_id": observability.get("session_id"),
        }

    diagnosis["evidence"] = {
        "pods": context.get("pods", {}).get("pods", [])[:5],
        "events": context.get("events", {}).get("events", [])[:10],
    }
    diagnosis.setdefault("risk_level", "medium")
    diagnosis.setdefault("blast_radius", diagnosis.get("impact", "待评估"))
    diagnosis.setdefault("signals", [])
    diagnosis.setdefault("immediate_actions", [])
    diagnosis.setdefault("prevention", [])
    diagnosis.setdefault("proposed_changes", [])

    remediation_plan = build_remediation_plan(alert, diagnosis, context)
    top_hypothesis = (remediation_plan.get("hypotheses") or [{}])[0]
    root_text = str(diagnosis.get("root_cause") or "").lower()
    if top_hypothesis and any(term in root_text for term in ("未知", "unknown", "证据不足", "人工介入")):
        confidence = float(top_hypothesis.get("confidence") or 0.0)
        if confidence >= 0.62:
            matched = top_hypothesis.get("matched_evidence") or []
            evidence_sources = ", ".join(sorted({str(item.get("source") or "") for item in matched if item.get("source")})[:4])
            diagnosis["root_cause"] = (
                f"{top_hypothesis.get('title') or top_hypothesis.get('id')}。"
                f"证据来源：{evidence_sources or 'runtime evidence'}；"
                f"置信度 {confidence:.2f}。"
            )
    engine_changes = remediation_plan.get("changes") or []
    llm_changes = []
    for raw_change in diagnosis.get("proposed_changes") or []:
        if not isinstance(raw_change, dict):
            continue
        candidate = dict(raw_change)
        candidate.setdefault("type", "patch_workload" if candidate.get("patch") else "")
        if candidate.get("type") in ACTION_CATALOG:
            llm_changes.append(candidate)
    diagnosis["proposed_changes"] = llm_changes
    if llm_changes and diagnosis.get("diagnosis_metadata", {}).get("source") == "llm":
        # 模型基于本轮真实证据给出的结构化方案是主计划；确定性引擎只负责
        # 动作目录、风险和审批门禁，不能再用固定 runbook 覆盖模型方案。
        adopted = []
        for candidate in llm_changes:
            action = str(candidate.get("type") or "")
            policy = ACTION_CATALOG[action]
            adopted.append({
                **candidate,
                "risk": "high" if action in {"apply_manifest", "patch_resource", "delete_resource", "run_shell", "exec_pod", "exec_node"} else candidate.get("risk") or policy["risk"],
                "auto_allowed": False,
                "rollback": candidate.get("rollback") or policy["rollback"],
                "selection_source": "llm_evidence_plan",
            })
        remediation_plan["changes"] = adopted
        remediation_plan["decision"] = "ready_for_approval"
        remediation_plan["reason"] = "AI 已基于实时证据生成结构化方案，等待动态 Skill 校验和人工审批。"
        remediation_plan["change_source"] = "llm"
    elif not llm_changes and engine_changes:
        diagnosis["proposed_changes"] = engine_changes
    expert_steps = expert_steps_from_diagnosis(diagnosis)
    if diagnosis.get("diagnosis_metadata", {}).get("source") == "llm" and len(expert_steps) >= 4:
        remediation_plan["steps"] = expert_steps
        remediation_plan["planning_engine"] = "LLMEvidenceExpertPlanner/v1 + EvidenceRunbookEngine guardrails"
        remediation_plan["step_source"] = "llm_evidence_expert"
    else:
        remediation_plan["planning_engine"] = "EvidenceRunbookEngine/v1 fallback"
        remediation_plan["step_source"] = "deterministic_fallback"
    diagnosis["remediation_plan"] = remediation_plan
    diagnosis["root_cause_hypotheses"] = remediation_plan.get("hypotheses", [])
    diagnosis["immediate_actions"] = remediation_plan.get("steps") or diagnosis.get("immediate_actions", [])

    metadata = diagnosis.setdefault("diagnosis_metadata", {})
    if isinstance(metadata, dict):
        metadata.setdefault("quality_scores", quality_score_from_diagnosis(diagnosis, context))
        metadata.setdefault("langfuse_trace_id", observability.get("trace_id"))
        metadata.setdefault("langfuse_session_id", observability.get("session_id"))
    observability["quality_scores"] = metadata.get("quality_scores", {})
    observability["model_profile_id"] = metadata.get("model_profile_id")
    observability["estimated_cost_usd"] = metadata.get("estimated_cost_usd", 0)
    update_trace(trace, output=diagnosis, metadata={"last_stage": "diagnose", "quality_scores": observability.get("quality_scores")})

    return {**state, "diagnosis": diagnosis, "observability": observability}


def _fallback_diagnosis(alert: dict, context: dict) -> dict:
    """规则匹配降级方案 — LLM 不可用时的保底逻辑"""
    alert_name = alert.get("alert_name", "")
    pods = context.get("pods", {}).get("pods", [])
    evidence_text = json.dumps(_redact_sensitive({
        "pod": context.get("pod", {}),
        "events": context.get("events", {}),
        "logs": context.get("logs", {}),
        "diagnostics": context.get("diagnostics", {}),
    }), ensure_ascii=False).lower()

    crash_states = ["CrashLoopBackOff", "Error", "ImagePullBackOff"]
    has_crash = any(
        any(s.lower() in " ".join([
            str(c.get("state", "")),
            str(c.get("reason", "")),
            str((c.get("state_detail") or {}).get("reason", "")),
            str((c.get("state_detail") or {}).get("message", "")),
        ]).lower() for s in crash_states)
        for p in pods
        for c in p.get("containers", [])
    ) or any(term in evidence_text for term in ("crashloopbackoff", "back-off restarting", "imagepullbackoff", "errimagepull"))

    if any(term in evidence_text for term in ("permission denied", "can't create directory", "cannot create directory", "read-only file system")):
        return {
            "root_cause": "容器日志显示写入目录权限不足，优先怀疑挂载卷属主/属组、运行用户或底层存储目录权限不匹配。",
            "impact": "目标 Pod 无法完成启动或业务初始化，Workload 可用副本可能不足。",
            "confidence": 0.86,
            "risk_level": "high",
            "blast_radius": "目标 Workload 及依赖其提供服务的上游调用会受影响。",
            "signals": [{"source": "logs", "finding": "命中 permission denied / mkdir 目录创建失败"}],
            "immediate_actions": [
                {"title": "读取 previous logs", "description": "确认失败发生在启动初始化阶段还是业务运行阶段。", "probe": "previous_logs"},
                {"title": "核对运行用户和挂载点", "description": "检查 runAsUser/runAsGroup、volumeMount 和 PVC/PV。", "probe": "pod_security_context"},
                {"title": "动态匹配权限恢复 Skill", "description": "比较失败路径、运行用户、initContainer、挂载与存储后端，再生成实际 YAML 或受审批命令，不预设固定修复。", "probe": "storage_chain"},
            ],
            "prevention": ["发布前校验镜像运行用户与存储目录权限", "核心有状态服务沉淀 storage permission Skill"],
            "suggested_action": "execute_plan",
            "need_human_approval": True,
        }

    if has_crash or alert_name == "KubePodCrashLooping":
        return {
            "root_cause": "容器反复崩溃，可能原因：OOM、启动失败、配置错误",
            "impact": "服务不可用或部分可用",
            "confidence": 0.85,
            "risk_level": "high",
            "blast_radius": "目标 Deployment 的部分或全部副本可能不可用，若无冗余会影响用户请求",
            "signals": [{"source": "pod_status", "finding": "检测到 CrashLoopBackOff/Error/ImagePullBackOff 或对应告警"}],
            "immediate_actions": [
                "采集证据：查看 Pod Events、current/previous logs、退出码、最近变更",
                "分支判断：OOM 走资源修复，探针失败走 startupProbe/readiness 修复，挂载/权限失败由实时证据与动态 Skill 决定实际配置或存储侧修复，镜像失败走 registry/secret 修复",
                "变更门禁：确认影响范围、副本冗余、回滚路径和人工审批",
                "执行验证：变更后观察新 Pod Ready、restart_count、Events 和业务探活",
            ],
            "prevention": ["补充启动探针和资源限制告警", "为核心服务配置多副本和 PDB", "沉淀 CrashLoop runbook"],
            "suggested_action": "investigate",
            "need_human_approval": True,
        }

    if alert_name == "HighCPUUsage":
        return {
            "root_cause": "Deployment CPU 使用率持续超过阈值",
            "impact": "延迟上升，请求处理变慢",
            "confidence": 0.82,
            "risk_level": "medium",
            "blast_radius": "目标服务吞吐下降，可能扩散到上游调用链",
            "signals": [{"source": "alert", "finding": "CPU 使用率持续超过阈值"}],
            "immediate_actions": ["确认 HPA 状态", "检查是否存在异常流量", "临时扩容副本"],
            "prevention": ["补充 HPA 策略", "建立容量基线", "为突发流量配置限流和降级"],
            "suggested_action": "scale_out",
            "need_human_approval": True,
        }

    if alert_name == "KubePodPending":
        return {
            "root_cause": "Pod 无法调度，可能因资源不足或节点亲和性不匹配",
            "impact": "新副本无法启动",
            "confidence": 0.88,
            "risk_level": "medium",
            "blast_radius": "发布或扩容流程受阻，现有副本不足时会影响服务容量",
            "signals": [{"source": "scheduler", "finding": "Pod 长时间处于 Pending"}],
            "immediate_actions": ["查看调度事件", "检查节点资源和 taint/toleration", "检查 PVC 或镜像拉取状态"],
            "prevention": ["增加资源水位告警", "完善节点池容量策略", "发布前做资源配额校验"],
            "suggested_action": "investigate",
            "need_human_approval": False,
        }

    return {
        "root_cause": "未知异常，需人工介入",
        "impact": "待评估",
        "confidence": 0.3,
        "risk_level": "medium",
        "blast_radius": "未知，需要结合上下文继续排查",
        "signals": [],
        "immediate_actions": ["查看 Pod Events", "查看应用日志", "确认近期变更"],
        "prevention": ["补齐告警标签", "补充服务拓扑和 runbook"],
        "suggested_action": "investigate",
        "need_human_approval": True,
    }


async def decide_action(state: SREState) -> SREState:
    diagnosis = state["diagnosis"]
    alert = state["alert"]
    trace, observability = _ensure_trace(state, "decide_action")
    span = start_span(
        trace,
        "03.decision_gate",
        input={
            "risk_level": diagnosis.get("risk_level"),
            "confidence": diagnosis.get("confidence"),
            "proposed_changes": diagnosis.get("proposed_changes", []),
        },
        metadata={"node": "decide_action", "gate": "human_approval_and_action_catalog"},
    )

    remediation_plan = diagnosis.get("remediation_plan") or {}
    proposed_changes = diagnosis.get("proposed_changes") or remediation_plan.get("changes") or []
    target = remediation_plan.get("target") or {}
    workload_name = alert.get("deployment") or alert.get("workload_name") or target.get("workload_name") or ""
    action = "execute_plan" if proposed_changes else "investigate"
    decision_source = "evidence_runbook_engine"
    need_approval = diagnosis.get("need_human_approval", True)

    requested_auto_healing = bool(alert.get("auto_healing_enabled", False))
    decision = {
        "action": action,
        # SRE Graph is a diagnosis/planning surface only. Real mutations are
        # exclusively executed by the OpsJob state machine with per-step approval.
        "require_human_approval": True if proposed_changes else bool(need_approval),
        "dry_run": True,
        "auto_healing_enabled": False,
        "requested_auto_healing": requested_auto_healing,
        "source": decision_source,
        "reason": diagnosis["root_cause"],
        "proposed_changes": proposed_changes,
        "diagnostic_actions": remediation_plan.get("diagnostic_actions", []),
        "runbook_id": remediation_plan.get("runbook_id", "unknown"),
        "success_criteria": remediation_plan.get("success_criteria", []),
        "target": {
            "namespace": alert.get("namespace", "default"),
            "workload_type": alert.get("workload_type") or target.get("workload_type") or "Deployment",
            "workload_name": workload_name or "unknown",
            "pod_name": target.get("pod_name") or alert.get("pod") or "",
        },
    }

    score_observation(
        trace,
        name="sre_decision.has_executable_plan",
        value=1.0 if proposed_changes else 0.0,
        comment=decision.get("reason", ""),
        metadata={"runbook_id": decision.get("runbook_id"), "dry_run": decision.get("dry_run")},
    )
    score_observation(
        trace,
        name="sre_decision.requires_human_approval",
        value=1.0 if decision.get("require_human_approval") else 0.0,
        metadata={"auto_healing_requested": requested_auto_healing, "forced_dry_run": True},
    )
    end_observation(span, output=decision)
    update_trace(trace, metadata={"last_stage": "decide_action", "runbook_id": decision.get("runbook_id")})

    return {**state, "decision": decision, "observability": observability}


async def call_healing_agent(state: SREState) -> SREState:
    decision = state["decision"]
    trace, observability = _ensure_trace(state, "healing")
    span = start_span(
        trace,
        "04.healing_agent",
        input={"decision": decision},
        metadata={"node": "call_healing_agent", "action": decision.get("action")},
    )

    if decision["action"] == "investigate":
        remediation = {
            "executed": False,
            "message": "已生成可执行的深度诊断计划；完成取证后将重新评分根因并生成受控变更。",
            "reason": decision.get("reason"),
            "decision_source": decision.get("source"),
            "diagnostic_actions": decision.get("diagnostic_actions", []),
        }
        end_observation(span, output=remediation, metadata={"executed": False, "reason": "investigate_only"})
        return {
            **state,
            "remediation": remediation,
            "observability": observability,
        }

    # Do not call the Healing Agent from a chat/alert graph. That path used to
    # let auto_healing_enabled turn an LLM decision into a write before the
    # canonical Skill gate and operator approval. The UI/API now submits the
    # returned plan to OpsJob, which owns approval, execution and verification.
    if decision.get("proposed_changes"):
        remediation = {
            "executed": False,
            "status": "awaiting_ops_job_approval",
            "message": "已生成受控修复计划；真实变更必须提交统一运维任务并逐项人工审批。",
            "reason": decision.get("reason"),
            "changes": decision.get("proposed_changes") or [],
            "target": decision.get("target") or {},
        }
        end_observation(span, output=remediation, metadata={"executed": False, "reason": "ops_job_approval_required"})
        return {**state, "remediation": remediation, "observability": observability}

    payload = {
        "id": str(uuid.uuid4()),
        "source_agent": "agentic-sre",
        "target_agent": "healing-agent",
        "task_type": "healing.execute",
        "priority": "high",
        "payload": {
            "namespace": decision["target"]["namespace"],
            "workload_type": decision["target"]["workload_type"],
            "workload_name": decision["target"]["workload_name"],
            "action": decision["action"],
            "reason": decision["reason"],
            "dry_run": decision["dry_run"],
            "patch": (decision.get("proposed_changes") or [{}])[0].get("patch") if decision.get("proposed_changes") else None,
            "changes": decision.get("proposed_changes") or [],
        },
    }

    try:
        async with _http_client() as client:
            resp = await client.post(HEALING_AGENT_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        error_payload = {
            "executed": False,
            "status": "failed",
            "error": f"healing-agent request failed: {type(e).__name__}: {e}",
        }
        end_observation(span, output=error_payload, status_message=error_payload["error"], level="ERROR")
        return {
            **state,
            "remediation": error_payload,
            "observability": observability,
        }

    remediation = data.get("result", data)
    end_observation(span, output=remediation, metadata={"executed": bool((remediation or {}).get("executed")) if isinstance(remediation, dict) else None})
    score_observation(
        trace,
        name="sre_healing.executed",
        value=1.0 if isinstance(remediation, dict) and remediation.get("executed") else 0.0,
        metadata={"action": decision.get("action"), "dry_run": decision.get("dry_run")},
    )
    update_trace(trace, metadata={"last_stage": "healing", "healing_status": (remediation or {}).get("status") if isinstance(remediation, dict) else ""})
    return {**state, "remediation": remediation, "observability": observability}


async def call_incident_agent(state: SREState) -> SREState:
    alert = state["alert"]
    diagnosis = state["diagnosis"]
    trace, observability = _ensure_trace(state, "incident")

    payload = {
        "id": str(uuid.uuid4()),
        "source_agent": "agentic-sre",
        "target_agent": "incident-agent",
        "task_type": "incident.create_or_update",
        "priority": alert.get("priority", "high"),
        "payload": {
            "title": f"K8S Alert: {alert.get('alert_name')}",
            "severity": alert.get("severity", "P2"),
            "namespace": alert.get("namespace", "default"),
            "service": alert.get("service", alert.get("deployment")),
            "summary": diagnosis["root_cause"],
            "status": "open",
        },
    }
    span = start_span(
        trace,
        "05.incident_agent",
        input=payload,
        metadata={"node": "call_incident_agent", "severity": payload["payload"].get("severity")},
    )

    try:
        async with _http_client() as client:
            resp = await client.post(INCIDENT_AGENT_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        incident = {
            "incident_id": f"INC-{uuid.uuid4().hex[:8]}",
            "title": payload["payload"]["title"],
            "severity": payload["payload"]["severity"],
            "namespace": payload["payload"]["namespace"],
            "service": payload["payload"]["service"],
            "summary": payload["payload"]["summary"],
            "status": "open",
            "agent_error": f"incident-agent request failed: {type(e).__name__}: {e}",
        }
        end_observation(span, output=incident, status_message=incident["agent_error"], level="ERROR")
        return {**state, "incident": incident, "observability": observability}

    incident = data.get("result", data)
    end_observation(span, output=incident)
    update_trace(trace, metadata={"last_stage": "incident", "incident_id": (incident or {}).get("incident_id") if isinstance(incident, dict) else ""})
    return {**state, "incident": incident, "observability": observability}


async def call_postmortem_agent(state: SREState) -> SREState:
    trace, observability = _ensure_trace(state, "postmortem")
    payload = {
        "id": str(uuid.uuid4()),
        "source_agent": "agentic-sre",
        "target_agent": "postmortem-agent",
        "task_type": "postmortem.generate",
        "priority": "medium",
        "payload": {
            "incident": state.get("incident"),
            "diagnosis": state.get("diagnosis"),
            "remediation": state.get("remediation"),
        },
    }
    span = start_span(
        trace,
        "06.postmortem_agent",
        input=payload,
        metadata={"node": "call_postmortem_agent", "incident_id": (state.get("incident") or {}).get("incident_id")},
    )

    try:
        async with _http_client() as client:
            resp = await client.post(POSTMORTEM_AGENT_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        postmortem = {
            "report": f"# Post-Mortem generation failed\n\n{type(e).__name__}: {e}",
            "agent_error": f"postmortem-agent request failed: {type(e).__name__}: {e}",
        }
        end_observation(span, output=postmortem, status_message=postmortem["agent_error"], level="ERROR")
        return {
            **state,
            "postmortem": postmortem,
            "observability": observability,
        }

    postmortem = data.get("result", data)
    end_observation(span, output=postmortem)
    update_trace(trace, metadata={"last_stage": "postmortem"})
    return {**state, "postmortem": postmortem, "observability": observability}


async def summarize(state: SREState) -> SREState:
    alert = state["alert"]
    diagnosis = state["diagnosis"]
    decision = state["decision"]
    remediation = state.get("remediation")
    incident = state.get("incident")
    trace, observability = _ensure_trace(state, "summarize")
    span = start_span(
        trace,
        "07.summarize",
        input={
            "diagnosis": diagnosis,
            "decision": decision,
            "remediation": remediation,
            "incident": incident,
        },
        metadata={"node": "summarize"},
    )

    def _lines(items, limit: int = 5) -> str:
        if not items:
            return "- 暂无明确证据"
        rows = []
        for item in items[:limit]:
            if isinstance(item, dict):
                finding = item.get("finding") or item.get("message") or item.get("reason") or json.dumps(item, ensure_ascii=False)
                source = item.get("source")
                rows.append(f"- {source + ': ' if source else ''}{finding}")
            else:
                rows.append(f"- {item}")
        return "\n".join(rows)

    action = decision.get("action") or diagnosis.get("suggested_action") or "investigate"
    dry_run = decision.get("dry_run")
    approval = decision.get("require_human_approval")
    executed = remediation.get("executed") if isinstance(remediation, dict) else False
    remediation_msg = ""
    if isinstance(remediation, dict):
        remediation_msg = remediation.get("message") or remediation.get("error") or remediation.get("reason") or ""
    incident_id = incident.get("incident_id") if isinstance(incident, dict) else ""

    final_answer = f"""
## 结论
{diagnosis.get("root_cause") or "还没有足够证据定位根因。"}

## 影响
- 范围：{diagnosis.get("impact") or diagnosis.get("blast_radius") or "待确认"}
- 风险：{diagnosis.get("risk_level") or "medium"}
- 置信度：{diagnosis.get("confidence", "n/a")}

## 证据轨迹
{_lines(diagnosis.get("signals") or [])}

## 下一步
{_lines(diagnosis.get("immediate_actions") or [])}

## 执行状态
- 建议动作：{action}
- 当前模式：{"诊断/待确认" if dry_run else "允许执行"}
- 人工确认：{"需要" if approval else "不需要"}
- Healing Agent：{"已执行" if executed else "未执行"}{f"；{remediation_msg}" if remediation_msg else ""}
{f"- 事件编号：{incident_id}" if incident_id else ""}

## 预防建议
{_lines(diagnosis.get("prevention") or [], 4)}
"""
    observability.update({
        "trace_id": observation_id(trace, observability.get("trace_id", "")),
        "quality_scores": (diagnosis.get("diagnosis_metadata") or {}).get("quality_scores", {}),
        "estimated_cost_usd": (diagnosis.get("diagnosis_metadata") or {}).get("estimated_cost_usd", 0),
        "token_usage": (diagnosis.get("diagnosis_metadata") or {}).get("token_usage", {}),
        "model_profile_id": (diagnosis.get("diagnosis_metadata") or {}).get("model_profile_id", ""),
        "completed": True,
    })
    end_observation(span, output={"final_answer": final_answer, "observability": observability})
    update_trace(
        trace,
        output={"final_answer": final_answer, "diagnosis": diagnosis, "decision": decision, "remediation": remediation},
        metadata={
            "last_stage": "summarize",
            "quality_scores": observability.get("quality_scores", {}),
            "estimated_cost_usd": observability.get("estimated_cost_usd", 0),
        },
    )
    flush_observability()
    _TRACE_CACHE.pop(observability.get("trace_id", ""), None)
    return {**state, "observability": observability, "final_answer": final_answer}


def should_generate_postmortem(state: SREState) -> Literal["postmortem", "end"]:
    severity = state["alert"].get("severity", "P2")
    return "postmortem" if severity in ["P0", "P1", "P2"] else "end"


# ============================================================
# Graph Build
# ============================================================
def build_graph():
    graph = StateGraph(SREState)

    graph.add_node("collect_context", collect_context)
    graph.add_node("diagnose", diagnose)
    graph.add_node("decide_action", decide_action)
    graph.add_node("healing", call_healing_agent)
    graph.add_node("incident", call_incident_agent)
    graph.add_node("postmortem", call_postmortem_agent)
    graph.add_node("summarize", summarize)

    graph.set_entry_point("collect_context")
    graph.add_edge("collect_context", "diagnose")
    graph.add_edge("diagnose", "decide_action")
    graph.add_edge("decide_action", "healing")
    graph.add_edge("healing", "incident")

    graph.add_conditional_edges(
        "incident",
        should_generate_postmortem,
        {"postmortem": "postmortem", "end": "summarize"},
    )

    graph.add_edge("postmortem", "summarize")
    graph.add_edge("summarize", END)

    return graph.compile()


# ============================================================
# Standalone entry point
# ============================================================
async def run_standalone():
    app = build_graph()

    alert = {
        "alert_name": "KubePodCrashLooping",
        "namespace": "default",
        "deployment": "your-workload",
        "service": "your-service",
        "severity": "P1",
        "priority": "critical",
    }

    result = await app.ainvoke({"alert": alert})
    print(result["final_answer"])

    if "postmortem" in result:
        print(result["postmortem"]["report"])


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_standalone())

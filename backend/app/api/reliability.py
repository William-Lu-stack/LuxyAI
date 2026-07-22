"""SLO、错误预算和应用发布治理接口。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import re
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request
import httpx
import yaml

from backend.app.domain.slo import evaluate_error_budget
from backend.app.schemas.reliability import ApprovalRequest, ObjectiveRequest, ReleaseRequest
from backend.app.services.reliability_store import ReliabilityStore


GateEvaluator = Callable[..., dict[str, Any]]
ReleaseSubmitter = Callable[[dict[str, Any], str], Awaitable[dict[str, Any]]]


@dataclass
class ReliabilityDependencies:
    store: ReliabilityStore
    gate_evaluator: GateEvaluator
    submit_release: ReleaseSubmitter


ALLOWED_RELEASE_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}
MUTABLE_IMAGE_TAGS = {"", "latest", "main", "master", "dev", "snapshot"}


def _image_is_immutable(image: str) -> bool:
    image = str(image or "").strip()
    if not image:
        return False
    if "@sha256:" in image:
        return True
    tail = image.rsplit("/", 1)[-1]
    tag = tail.rsplit(":", 1)[-1].lower() if ":" in tail else ""
    return tag not in MUTABLE_IMAGE_TAGS


def _validate_release_manifest(raw_yaml: str, request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        documents = [item for item in yaml.safe_load_all(raw_yaml) if item is not None]
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=422, detail=f"YAML 解析失败：{exc}") from exc
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise HTTPException(status_code=422, detail="每次发布必须提交且只提交一个 Kubernetes Workload YAML。")
    manifest = documents[0]
    kind = str(manifest.get("kind") or "")
    api_version = str(manifest.get("apiVersion") or "")
    metadata = manifest.get("metadata") or {}
    spec = manifest.get("spec") or {}
    if kind not in ALLOWED_RELEASE_KINDS or api_version != "apps/v1":
        raise HTTPException(status_code=422, detail="发布治理当前只接受 apps/v1 Deployment、StatefulSet、DaemonSet。")
    name = str(metadata.get("name") or "")
    namespace = str(metadata.get("namespace") or request.get("namespace") or "default")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", name or ""):
        raise HTTPException(status_code=422, detail="YAML metadata.name 不是合法的 Kubernetes 名称。")
    if namespace != request.get("namespace"):
        raise HTTPException(status_code=422, detail="YAML namespace 必须与发布范围选择一致。")
    if request.get("release_mode") == "existing":
        if kind != request.get("workload_kind") or name != request.get("workload_name"):
            raise HTTPException(status_code=422, detail="YAML kind/name 必须与已选择的 Workload 完全一致。")
    pod_spec = (((spec.get("template") or {}).get("spec")) or {})
    forbidden = [key for key in ("hostNetwork", "hostPID", "hostIPC") if pod_spec.get(key)]
    if forbidden:
        raise HTTPException(status_code=422, detail=f"生产发布禁止启用：{', '.join(forbidden)}")
    if pod_spec.get("serviceAccountName") and pod_spec.get("automountServiceAccountToken", True):
        raise HTTPException(status_code=422, detail="Workload 使用 ServiceAccount 时必须显式设置 automountServiceAccountToken=false，或走安全例外审批。")
    for volume in pod_spec.get("volumes") or []:
        if isinstance(volume, dict) and volume.get("hostPath"):
            raise HTTPException(status_code=422, detail="生产发布禁止 hostPath volume。")
    containers = list(pod_spec.get("containers") or []) + list(pod_spec.get("initContainers") or [])
    if not containers:
        raise HTTPException(status_code=422, detail="YAML 至少需要一个 container。")
    for container in containers:
        if not isinstance(container, dict) or not container.get("name") or not container.get("image"):
            raise HTTPException(status_code=422, detail="每个 container 必须包含 name 和 image。")
        if not _image_is_immutable(str(container.get("image"))):
            raise HTTPException(status_code=422, detail=f"容器 {container.get('name')} 必须使用不可变镜像 tag 或 sha256 digest。")
        security = container.get("securityContext") or {}
        if security.get("privileged") or security.get("allowPrivilegeEscalation") is True:
            raise HTTPException(status_code=422, detail=f"容器 {container.get('name')} 禁止 privileged/allowPrivilegeEscalation。")
        add_caps = set(((security.get("capabilities") or {}).get("add")) or [])
        if add_caps & {"SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE", "ALL"}:
            raise HTTPException(status_code=422, detail=f"容器 {container.get('name')} 请求了禁止的 Linux capability。")
    safe_manifest = {
        "apiVersion": "apps/v1",
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": metadata.get("labels") or {},
            "annotations": metadata.get("annotations") or {},
        },
        "spec": spec,
    }
    validation = {
        "policy": "ProductionWorkloadManifest/v1",
        "kind": kind,
        "name": name,
        "namespace": namespace,
        "containers": len(containers),
        "immutable_images": True,
        "privileged": False,
        "host_path": False,
        "digest": hashlib.sha256(yaml.safe_dump(safe_manifest, sort_keys=True).encode()).hexdigest()[:16],
    }
    return safe_manifest, validation


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _percent(value: Any, digits: int = 1) -> str:
    return f"{_safe_float(value) * 100:.{digits}f}%"


def _risk_label(score: Any) -> str:
    value = _safe_float(score)
    if value >= 0.82:
        return "极高"
    if value >= 0.62:
        return "高"
    if value >= 0.38:
        return "中"
    return "低"


def _release_strategy_text(gate: dict[str, Any]) -> str:
    strategy = gate.get("selected_strategy") or {}
    if strategy:
        return (
            f"建议灰度首批 {_percent(strategy.get('first_ratio'))}，每批最多增加 {_percent(strategy.get('step_ratio'))}，"
            f"最高不超过 {_percent(strategy.get('max_ratio'))}；每批观察 {strategy.get('observation_window_min', 20)} 分钟，"
            f"预计 {strategy.get('batches', 1)} 批完成。"
        )
    envelope = gate.get("safety_envelope") or {}
    return (
        "当前没有候选灰度策略落入安全包络。建议先只做 1% 以内人工灰度或影子验证，"
        f"并把首批比例压到 {_percent(envelope.get('first_ratio_limit', 0.01))} 以下，"
        "待错误率、P99、重启和下游错误稳定后再重新判定。"
    )


def _blast_text(gate: dict[str, Any]) -> str:
    blast = gate.get("blast_radius") or {}
    radius = blast.get("blast_radius") or {}
    services = len(radius.get("impacted_services") or [])
    pods = len(radius.get("impacted_pods") or [])
    dependencies = len(radius.get("related_dependencies") or [])
    paths = len(radius.get("critical_paths") or [])
    return (
        f"影响等级 {blast.get('impact_level', 'unknown')}，放大系数 {blast.get('amplification_factor', 0)}，"
        f"关键路径 {paths} 条；预计影响服务 {services} 个、Pod {pods} 个、共享依赖 {dependencies} 个。"
    )


def _release_images(payload: dict[str, Any], manifest: dict[str, Any] | None) -> list[str]:
    images: list[str] = []
    if payload.get("image"):
        images.append(str(payload["image"]))
    pod_spec = (((manifest or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    for container in list(pod_spec.get("containers") or []) + list(pod_spec.get("initContainers") or []):
        if isinstance(container, dict) and container.get("image"):
            images.append(str(container["image"]))
    seen = set()
    return [image for image in images if not (image in seen or seen.add(image))]


def _scanner_risk_level(scan: dict[str, Any]) -> str:
    critical = int(_safe_float(scan.get("critical") or scan.get("critical_count") or scan.get("critical_vulnerabilities")))
    high = int(_safe_float(scan.get("high") or scan.get("high_count") or scan.get("high_vulnerabilities")))
    if critical > 0:
        return "critical"
    if high > 0:
        return "high"
    return str(scan.get("risk_level") or scan.get("severity") or "low").lower()


async def _scan_release_images(images: list[str]) -> dict[str, Any]:
    if not images:
        return {"status": "not_applicable", "summary": "本次未发现镜像变更。", "images": []}
    scanner_url = os.getenv("IMAGE_SECURITY_SCAN_URL", "").strip()
    if not scanner_url:
        return {
            "status": "not_configured",
            "summary": "未配置镜像扫描服务；已完成不可变 tag/digest 与 Workload 安全策略检查，但未做漏洞库扫描。",
            "images": images,
            "risk_level": "unknown",
        }
    try:
        verify = os.getenv("IMAGE_SECURITY_SCAN_VERIFY_SSL", "true").lower() not in {"0", "false", "no"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0), verify=verify) as client:
            response = await client.post(scanner_url, json={"images": images})
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return {
            "status": "degraded",
            "summary": f"镜像扫描服务暂时不可用：{type(exc).__name__}: {exc}。本次只保留离线安全校验结果。",
            "images": images,
            "risk_level": "unknown",
        }
    if isinstance(data, dict):
        risk_level = _scanner_risk_level(data)
        return {
            "status": str(data.get("status") or "ok"),
            "summary": str(data.get("summary") or data.get("message") or f"镜像扫描完成，风险等级 {risk_level}。"),
            "images": data.get("images") or images,
            "risk_level": risk_level,
            "critical": data.get("critical") or data.get("critical_count") or 0,
            "high": data.get("high") or data.get("high_count") or 0,
            "raw": data,
        }
    return {"status": "ok", "summary": "镜像扫描完成。", "images": images, "risk_level": "unknown", "raw": data}


def _image_report(payload: dict[str, Any], manifest_validation: dict[str, Any] | None, image_scan: dict[str, Any] | None) -> str:
    scan = image_scan or {}
    scan_summary = scan.get("summary")
    if payload.get("change_channel") == "emergency_recovery" and payload.get("emergency_action") == "restart_component":
        return "本次是受控重启，不变更镜像；执行前会记录当前镜像和 generation。"
    image = payload.get("image")
    if manifest_validation:
        return (
            f"YAML 已通过生产安全校验，包含 {manifest_validation.get('containers', 0)} 个容器，"
            f"镜像均为不可变版本，未发现 privileged、hostPath 或高危 capability；校验指纹 {manifest_validation.get('digest')}。"
            f"{' ' + scan_summary if scan_summary else ''}"
        )
    if image:
        return (
            f"镜像 {image} 已通过不可变版本检查。未提交完整 YAML 时，平台只能校验镜像格式，"
            "建议补充期望状态 YAML 以检查探针、资源限制、ServiceAccount 和安全上下文。"
            f"{' ' + scan_summary if scan_summary else ''}"
        )
    return "未发现镜像变更；本次以配置或 Workload 期望状态变更为主。"


def _build_release_report(
    payload: dict[str, Any],
    gate: dict[str, Any],
    budget: dict[str, Any],
    manifest_validation: dict[str, Any] | None,
    image_scan: dict[str, Any] | None,
) -> dict[str, Any]:
    """生成给运维人员阅读的发布风险摘要，不输出代码片段。"""
    risk = gate.get("risk") or {}
    envelope = gate.get("safety_envelope") or {}
    verdict = str(gate.get("verdict") or "manual_review")
    is_emergency = payload.get("change_channel") == "emergency_recovery"
    budget_state = budget.get("state", "unknown")
    diff_risk = _safe_float(risk.get("diff_risk"))
    amp = _safe_float(risk.get("amplification_factor"))
    decision_map = {
        "pass": "允许按建议灰度推进",
        "hold": "暂停扩大灰度，继续观察",
        "rollback": "触发回滚边界",
        "blocked": "阻断常规发布",
        "manual_approval": "需要人工复核后极小比例灰度",
        "manual_review": "需要人工复核",
        "emergency_review": "紧急修复需人工复核后执行",
    }
    short_risks = [
        f"短期可能出现 rollout 卡住、Ready 副本不足、错误率上升或 P99 延迟抬升；当前差分风险 {_risk_label(diff_risk)}（{diff_risk:.2f}）。",
        f"拓扑放大系数 {amp:.2f}，若关键路径上存在共享中间件或上游流量，故障会被放大到依赖链路。",
    ]
    if budget.get("freeze_changes") and not is_emergency:
        short_risks.insert(0, "错误预算已耗尽，常规发布会被直接冻结。")
    if is_emergency:
        short_risks.insert(0, "紧急修复只用于恢复稳定性；若修复失败，应立即停止并进入回退或人工接管。")
    long_risks = [
        "如果不补充完整 YAML、观测窗口和回滚证据，后续很难复盘是哪一项配置导致风险变化。",
        "如果持续使用可变镜像 tag、缺少资源边界或缺少 readinessProbe，未来发布会更容易出现不可预测故障。",
    ]
    recommendations = [
        _release_strategy_text(gate),
        "推进期间重点观察：错误率、P95/P99、Pod 重启、Ready 副本、CPU throttling、下游错误和 Kafka/DB 等共享依赖。",
        "若任一核心指标越过暂停阈值，停止扩大灰度；若越过回滚阈值，执行回滚或紧急修复通道。",
    ]
    if verdict in {"blocked", "manual_approval", "manual_review"}:
        recommendations.insert(0, "不要直接全量发布；先补齐证据或只做人工确认的小流量验证。")
    if is_emergency:
        recommendations.insert(0, "执行前确认影响范围、回退条件和验证口径；执行后必须核对恢复判据。")
    evidence = [
        f"SLO 目标 {budget.get('target_percent', 99.9)}%，错误预算状态 {budget_state}，剩余预算 {_percent(budget.get('remaining_ratio'))}，燃烧率 {budget.get('burn_rate', 0)}x。",
        f"门禁算法 {((gate.get('algorithm') or {}).get('name') or 'SemanticGrayReleaseGate')}，候选灰度策略 {len(gate.get('candidate_strategies') or [])} 个，安全包络预算上限 {_percent(envelope.get('budget_cost_limit'))}。",
        _blast_text(gate),
        _image_report(payload, manifest_validation, image_scan),
        gate.get("reason") or "门禁未返回详细原因，已按人工复核处理。",
    ]
    return {
        "headline": (
            f"{decision_map.get(verdict, '需要人工复核')}：{payload.get('service')} "
            f"{payload.get('workload_kind')}/{payload.get('workload_name')}"
        ),
        "risk_decision": (
            f"{decision_map.get(verdict, '需要人工复核')}。风险等级 {_risk_label(max(diff_risk, amp / 3.0))}；"
            f"依据是错误预算、灰度候选策略、拓扑爆炸半径、历史风险和当前观测差分。"
        ),
        "allowed_scope": _release_strategy_text(gate),
        "blast_radius": _blast_text(gate),
        "image_check": _image_report(payload, manifest_validation, image_scan),
        "image_scan": image_scan or {},
        "short_term_risks": short_risks,
        "long_term_risks": long_risks,
        "recommendations": recommendations,
        "evidence": evidence,
    }


def _actor(request: Request) -> str:
    return str(request.headers.get("x-auth-request-user") or request.headers.get("x-forwarded-user") or (request.client.host if request.client else "unknown"))[:120]


def build_reliability_router(deps: ReliabilityDependencies) -> APIRouter:
    router = APIRouter(tags=["SRE reliability governance"])

    @router.get("/api/reliability/summary")
    async def reliability_summary():
        objectives = deps.store.objectives()
        budgets = [item["budget"] for item in objectives]
        return {
            "status": "ok",
            "objectives": objectives,
            "summary": {
                "total": len(budgets),
                "healthy": sum(1 for item in budgets if item["state"] == "healthy"),
                "at_risk": sum(1 for item in budgets if item["state"] == "at_risk"),
                "exhausted": sum(1 for item in budgets if item["state"] == "exhausted"),
                "changes_frozen": sum(1 for item in budgets if item["freeze_changes"]),
            },
            "policy": "预算耗尽即冻结新功能和常规发布，只允许恢复稳定性的紧急变更。",
            "audit_storage": deps.store.storage_status(),
        }

    @router.post("/api/reliability/objectives")
    async def upsert_objective(req: ObjectiveRequest):
        try:
            return {"status": "ok", "objective": deps.store.upsert_objective(req.model_dump())}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/releases")
    async def list_releases():
        return {"status": "ok", "releases": deps.store.releases()}

    @router.post("/api/releases")
    async def create_release(req: ReleaseRequest, request: Request):
        payload = req.model_dump()
        is_emergency = payload["change_channel"] == "emergency_recovery"
        if is_emergency:
            if payload["release_mode"] != "existing":
                raise HTTPException(status_code=422, detail="紧急修复通道只能操作现有 Workload，不能用于发布新应用。")
            if payload["emergency_action"] not in {"rollback", "restore_config", "restart_component"}:
                raise HTTPException(status_code=422, detail="请选择回滚稳定版本、恢复配置或重启故障组件。")
            if len(payload["emergency_reason"].strip()) < 8:
                raise HTTPException(status_code=422, detail="紧急修复必须填写故障现象、业务影响或恢复理由，至少 8 个字符。")
        manifest = None
        manifest_validation = None
        if payload["manifest_yaml"].strip():
            manifest, manifest_validation = _validate_release_manifest(payload.pop("manifest_yaml"), payload)
            payload["workload_kind"] = manifest["kind"]
            payload["workload_name"] = manifest["metadata"]["name"]
            payload["namespace"] = manifest["metadata"]["namespace"]
            if payload["release_mode"] == "existing":
                payload["patch"] = {"spec": manifest["spec"]}
        elif payload["release_mode"] == "new":
            raise HTTPException(status_code=422, detail="新建 Workload 必须提交完整 YAML。")
        elif not payload["workload_name"]:
            raise HTTPException(status_code=422, detail="请选择现有 Workload，或切换为新建并提交 YAML。")
        elif not payload["image"] and not payload["patch"] and not (is_emergency and payload["emergency_action"] == "restart_component"):
            raise HTTPException(status_code=422, detail="现有 Workload 发布必须填写不可变镜像，或提交完整期望状态 YAML 形成可审计变更。")
        if is_emergency and payload["emergency_action"] == "rollback" and not payload["image"]:
            raise HTTPException(status_code=422, detail="回滚必须填写已验证的上一稳定镜像版本或 digest。")
        if is_emergency and payload["emergency_action"] == "restore_config" and not (manifest or payload["patch"]):
            raise HTTPException(status_code=422, detail="恢复误删配置必须提交经过校验的期望状态 YAML。")
        if is_emergency and payload["emergency_action"] == "restart_component":
            payload["image"] = ""
            payload["patch"] = {}
        if payload["image"]:
            if not _image_is_immutable(payload["image"]):
                raise HTTPException(status_code=422, detail="生产发布必须使用不可变镜像版本，禁止 latest/main/master/dev。")
            if not payload["container_name"]:
                raise HTTPException(status_code=422, detail="镜像发布必须指定 container_name。")
        image_scan = await _scan_release_images(_release_images(payload, manifest))
        objective = deps.store.objective_for(payload["service"], payload["cluster"], payload["namespace"])
        budget = evaluate_error_budget(objective)
        runtime = {
            "remaining_budget": budget["remaining_ratio"],
            "budget_burn_rate": budget["burn_rate"],
            "runtime_pressure": min(1.0, budget["burn_rate"] / 4.0),
            "release_state": "frozen" if budget["freeze_changes"] else "pending",
        }
        change = {
            "target": f"{payload['workload_kind']}/{payload['workload_name']}",
            "kind": payload["workload_kind"],
            "summary": payload["change_summary"],
            "operator": (
                f"emergency_{payload['emergency_action']}"
                if is_emergency else
                "version_replace" if payload["image"] else "config_replace"
            ),
            "selected": {
                "id": f"{payload['cluster']}:{payload['namespace']}:{payload['workload_kind']}:{payload['workload_name']}",
                "type": "workload",
                "title": f"{payload['workload_kind']}/{payload['workload_name']}",
                "category": "application",
            },
        }
        try:
            gate = deps.gate_evaluator(change, payload["graph"], runtime, payload["history"], payload["candidates"], payload["observation"])
        except Exception as exc:
            gate = {
                "status": "degraded",
                "verdict": "manual_review",
                "action": "human_approval",
                "reason": f"风险门禁降级：{type(exc).__name__}: {exc}。请人工核对拓扑、SLO 预算和回滚方案后再批准。",
                "risk": {"risk_score": 0.62, "risk_level": "medium"},
                "algorithm": {"name": "SemanticGrayReleaseGate", "fallback": True},
            }
        if budget["freeze_changes"] and not is_emergency:
            gate = {
                **gate,
                "verdict": "blocked",
                "action": "freeze_change",
                "reason": budget["freeze_reason"],
            }
        elif _scanner_risk_level(image_scan) in {"critical", "high"} and not is_emergency:
            gate = {
                **gate,
                "verdict": "blocked",
                "action": "block_image_risk",
                "reason": f"镜像安全扫描未通过：{image_scan.get('summary') or image_scan.get('risk_level')}",
            }
        elif is_emergency:
            gate = {
                **gate,
                "verdict": "emergency_review",
                "action": "break_glass_approval",
                "reason": (
                    "紧急修复通道仅豁免错误预算冻结，不豁免 YAML 安全校验、人工审批、审计和恢复验证。"
                    f" 动作：{payload['emergency_action']}；理由：{payload['emergency_reason'].strip()}"
                ),
                "emergency": True,
            }
        status = "blocked" if gate.get("verdict") in {"blocked", "rollback"} else "awaiting_approval"
        release_report = _build_release_report(payload, gate, budget, manifest_validation, image_scan)
        try:
            release = deps.store.add_release({
                **payload,
                "manifest": manifest,
                "manifest_validation": manifest_validation,
                "status": status,
                "gate": gate,
                "report": release_report,
                "error_budget": budget,
                "submitted_by": _actor(request),
                "emergency_audit": {
                    "enabled": is_emergency,
                    "action": payload["emergency_action"],
                    "reason": payload["emergency_reason"].strip(),
                    "budget_freeze_bypassed": bool(is_emergency and budget["freeze_changes"]),
                },
            })
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "发布审计状态无法写入。请检查 RELIABILITY_STORE_PATH 是否挂载为可写目录，"
                    f"当前错误：{type(exc).__name__}: {exc}"
                ),
            ) from exc
        return {"status": "ok", "release": release}

    @router.post("/api/releases/{release_id}/approve")
    async def approve_release(release_id: str, req: ApprovalRequest, request: Request):
        release = deps.store.release(release_id)
        if not release:
            raise HTTPException(status_code=404, detail="发布申请不存在")
        if not req.confirm:
            raise HTTPException(status_code=409, detail="必须明确确认风险后才能批准")
        is_emergency = release.get("change_channel") == "emergency_recovery"
        if (release.get("error_budget") or {}).get("freeze_changes") and not is_emergency:
            raise HTTPException(status_code=409, detail="错误预算已耗尽，发布冻结；请先恢复稳定性或走独立紧急变更流程。")
        if (release.get("gate") or {}).get("verdict") in {"blocked", "rollback"} and not is_emergency:
            raise HTTPException(status_code=409, detail="发布门禁已阻断，不能批准")
        if is_emergency and len(req.comment.strip()) < 8:
            raise HTTPException(status_code=422, detail="紧急修复审批必须填写至少 8 个字符的复核意见。")
        updated = deps.store.update_release(
            release_id,
            status="approved",
            approved_by=_actor(request),
            approval_comment=req.comment,
            break_glass_approved=is_emergency,
        )
        return {"status": "ok", "release": updated}

    @router.post("/api/releases/{release_id}/execute")
    async def execute_release(release_id: str, request: Request):
        release = deps.store.release(release_id)
        if not release:
            raise HTTPException(status_code=404, detail="发布申请不存在")
        if release.get("status") != "approved":
            raise HTTPException(status_code=409, detail="发布必须先通过门禁并由人工批准")
        if (release.get("error_budget") or {}).get("freeze_changes") and release.get("change_channel") != "emergency_recovery":
            raise HTTPException(status_code=409, detail="错误预算已耗尽，禁止执行发布")
        job = await deps.submit_release(release, _actor(request))
        updated = deps.store.update_release(release_id, status="executing", ops_job_id=job.get("id"), execution=job)
        return {"status": "accepted", "release": updated, "job": job}

    return router

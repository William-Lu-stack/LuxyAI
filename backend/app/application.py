"""Flawless 控制面的兼容运行时与应用装配模块。

历史接口实现暂时保留在这里，以保证生产行为和测试契约不因目录迁移而改变。
HTTP 路由、请求模型和新增业务必须分别放入 ``api/features``、``schemas`` 和
``services``；本文件只接受从旧实现向独立服务迁出的兼容改动。
"""
from fastapi import FastAPI, Request, HTTPException, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import httpx
import asyncio
import base64
import copy
import hashlib
import json
import os
import re
import secrets
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse
import sys
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
load_dotenv(ROOT_DIR / ".env", override=False)

from agents.aiops_algorithms import analyze_blast_radius, evaluate_release_gate, prioritize_inspection_findings
from agents.aiops_observability import (
    end_observation,
    estimate_llm_cost_usd,
    flush as flush_observability,
    langfuse_status,
    new_trace_id,
    score_observation,
    start_generation,
    start_trace,
    trace_hierarchy_schema,
    update_trace,
)
from agents.effectiveness import record_inspection, record_remediation, summary as effectiveness_summary
from agents.model_registry import (
    get_active_model_profile_id,
    registry_payload,
    select_model_profile,
    set_active_model_profile,
    upsert_model_profile,
    delete_model_profile,
)
from agents.remediation_engine import ACTION_CATALOG, action_catalog_payload, build_remediation_plan, validate_change
from agents.runtime_resilience import AsyncBulkhead, BulkheadRejected, TTLCache, bounded_append, build_self_heal_decision
from cloud.adapters import cloud_adapters_payload
from backend.app.services.knowledge_files import extract_knowledge_file
from backend.app.services.infrastructure_providers import (
    providers_payload as infrastructure_providers_payload,
    redact_sensitive as redact_infrastructure_sensitive,
    scan_resources as scan_infrastructure_provider_resources,
)
from backend.app.services.external_traffic import build_external_traffic_payload
from backend.app.services.ebpf_flows import normalize_observed_flow_payload
from backend.app.services.resource_catalog import build_resource_catalog
from backend.app.services.cluster_registry import ClusterRegistry
from cmdb.local_cmdb import _collect_cluster_topology
from backend.app.services.ops_execution import StageTimeoutError, run_with_heartbeat
from backend.app.services.ops_skill_registry import OpsSkillRegistry, approved_script_catalog, skill_option_catalog
from backend.app.services.agent_skill_packages import AgentSkillPackageError, MAX_PACKAGE_BYTES
from backend.app.api.reliability import ReliabilityDependencies, build_reliability_router
from backend.app.domain.slo import evaluate_error_budget
from backend.app.services.reliability_store import ReliabilityStore
from backend.app.services.release_execution import submit_release_job
from backend.app.schemas.chat import ChatRequest, ChatResponse, ChatRiskRankRequest
from backend.app.schemas.integrations import CollaborationNotificationRequest
from backend.app.schemas.knowledge import KnowledgeAskRequest, KnowledgeDocumentRequest, KnowledgeReindexRequest
from backend.app.schemas.models import ModelBenchmarkRequest, ModelProfileActiveRequest, ModelProfileUpsertRequest
from backend.app.schemas.operations import (
    AlertScanRequest,
    ExternalTrafficFlowRequest,
    InfrastructureScanRequest,
    InspectionPreviewRequest,
    InspectionRequest,
    MCPToolRequest,
    OpsExecuteRequest,
    OpsJobCreateRequest,
    OpsStepApprovalRequest,
    OpsSkillDefinition,
    OpsSkillMatchRequest,
    ReleaseGateRequest,
    TopologyImpactRequest,
    KubeconfigContextsRequest,
    ManagedClusterTokenRefreshRequest,
    ManagedClusterUpsertRequest,
)

# ============================================================
# App Setup
# ============================================================
STATIC_DIR = Path(os.getenv("FRONTEND_STATIC_DIR", ROOT_DIR / "frontend"))
MODERN_DIST_DIR = STATIC_DIR / "dist"
CLUSTER_REGISTRY = ClusterRegistry()

def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


def _csv_env(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


app = FastAPI(
    title="flawless Control Plane API",
    version="2.0",
    docs_url=None if _env_bool("DISABLE_OPENAPI_DOCS", "true") else "/docs",
    redoc_url=None if _env_bool("DISABLE_OPENAPI_DOCS", "true") else "/redoc",
    openapi_url=None if _env_bool("DISABLE_OPENAPI_DOCS", "true") else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_csv_env("CORS_ALLOW_ORIGINS", ""),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/assets", StaticFiles(directory=MODERN_DIST_DIR / "assets", check_dir=False), name="modern-assets")

REQUEST_BULKHEAD = AsyncBulkhead(
    int(os.getenv("MAX_INFLIGHT_REQUESTS", "256")),
    float(os.getenv("REQUEST_BULKHEAD_TIMEOUT_SECONDS", "1.5")),
)
OUTBOUND_BULKHEAD = AsyncBulkhead(
    int(os.getenv("MAX_OUTBOUND_CONCURRENCY", "96")),
    float(os.getenv("OUTBOUND_BULKHEAD_TIMEOUT_SECONDS", "2.0")),
)
RANCHER_CACHE = TTLCache(
    int(os.getenv("CACHE_TTL_RANCHER_CLUSTERS", "45")),
    int(os.getenv("CACHE_MAX_ITEMS", "256")),
)
RANCHER_INVENTORY_CACHE = TTLCache(
    int(os.getenv("CACHE_TTL_RANCHER_INVENTORY", "15")),
    int(os.getenv("CACHE_MAX_ITEMS", "256")),
)
CMDB_TOPOLOGY_CACHE = TTLCache(
    int(os.getenv("CACHE_TTL_CMDB_TOPOLOGY", "30")),
    int(os.getenv("CACHE_MAX_ITEMS", "256")),
)
PROMETHEUS_SUMMARY_CACHE = TTLCache(
    int(os.getenv("CACHE_TTL_PROMETHEUS_SUMMARY", "8")),
    int(os.getenv("CACHE_MAX_ITEMS", "256")),
)
EXTERNAL_TRAFFIC_CACHE = TTLCache(
    int(os.getenv("CACHE_TTL_EXTERNAL_TRAFFIC", "20")),
    int(os.getenv("CACHE_MAX_ITEMS", "256")),
)
HTTP_LIMITS = httpx.Limits(
    max_connections=int(os.getenv("HTTP_MAX_CONNECTIONS", "256")),
    max_keepalive_connections=int(os.getenv("HTTP_MAX_KEEPALIVE_CONNECTIONS", "64")),
)
STORE_LIMIT = int(os.getenv("IN_MEMORY_STORE_LIMIT", "1000"))
KNOWLEDGE_STORE_PATH = Path(os.getenv("KNOWLEDGE_STORE_PATH", "/tmp/flawless-knowledge-base.json"))
OPS_SKILL_ROOT = Path(os.getenv("OPS_SKILL_ROOT", "/tmp/flawless/ops-skills"))
OPS_SKILL_STORE_PATH = Path(os.getenv("OPS_SKILL_STORE_PATH", "/tmp/flawless-ops-skills.json"))
KNOWLEDGE_RUNTIME_WRITE_ENABLED = _env_bool("KNOWLEDGE_RUNTIME_WRITE_ENABLED", "true")
KNOWLEDGE_EMBEDDING_ENABLED = _env_bool("KNOWLEDGE_EMBEDDING_ENABLED", "true")
KNOWLEDGE_CHUNK_CHARS = max(300, int(os.getenv("KNOWLEDGE_CHUNK_CHARS", "900")))
KNOWLEDGE_CHUNK_OVERLAP = max(0, int(os.getenv("KNOWLEDGE_CHUNK_OVERLAP", "120")))
KNOWLEDGE_LOCK = threading.RLock()
PLATFORM_LAST_SELF_HEAL_AT = 0.0
APP_BUILD_VERSION = os.getenv("APP_BUILD_VERSION", "3.2.2")
APP_CODE_SIGNATURE = "production-hardening-v8"
MAX_REQUEST_BODY_BYTES = int(os.getenv("MAX_REQUEST_BODY_BYTES", str(2 * 1024 * 1024)))
KNOWLEDGE_MAX_UPLOAD_BYTES = int(os.getenv("KNOWLEDGE_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
KNOWLEDGE_MAX_EXTRACTED_BYTES = int(os.getenv("KNOWLEDGE_MAX_EXTRACTED_BYTES", str(8 * 1024 * 1024)))
SECURITY_HEADERS_ENABLED = _env_bool("SECURITY_HEADERS_ENABLED", "true")
OUTBOUND_VERIFY_SSL = _env_bool("OUTBOUND_VERIFY_SSL", "true")
RANCHER_HTTP_CLIENT: httpx.AsyncClient | None = None
OUTBOUND_HTTP_CLIENTS: dict[int, httpx.AsyncClient] = {}


@app.on_event("startup")
async def startup_build_banner():
    global RANCHER_HTTP_CLIENT
    RANCHER_HTTP_CLIENT = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=5.0, pool=3.0),
        verify=_rancher_verify_ssl(),
        limits=HTTP_LIMITS,
        headers={"Accept": "application/json"},
    )
    for timeout_seconds in (3, 8, 10, 12, 30, 60, 90, 120):
        OUTBOUND_HTTP_CLIENTS[timeout_seconds] = httpx.AsyncClient(
            timeout=httpx.Timeout(float(timeout_seconds), connect=5.0, pool=2.0),
            verify=OUTBOUND_VERIFY_SSL,
            limits=HTTP_LIMITS,
            headers={"Accept": "application/json"},
        )
    print(f"FLAWLESS_BACKEND_BUILD version={APP_BUILD_VERSION} signature={APP_CODE_SIGNATURE}", flush=True)


@app.on_event("shutdown")
async def shutdown_http_clients():
    global RANCHER_HTTP_CLIENT
    if RANCHER_HTTP_CLIENT is not None:
        await RANCHER_HTTP_CLIENT.aclose()
        RANCHER_HTTP_CLIENT = None
    await asyncio.gather(*(client.aclose() for client in OUTBOUND_HTTP_CLIENTS.values()), return_exceptions=True)
    OUTBOUND_HTTP_CLIENTS.clear()


async def build_info():
    return {
        "status": "ok",
        "version": APP_BUILD_VERSION,
        "signature": APP_CODE_SIGNATURE,
        "server_module": "backend.app.main",
        "self_heal_run_signature": "request-json-body",
    }


def _basic_auth_credentials() -> tuple[str, str] | None:
    user = os.getenv("CONSOLE_BASIC_AUTH_USERNAME", "").strip()
    password = os.getenv("CONSOLE_BASIC_AUTH_PASSWORD", "").strip()
    if not user or not password:
        return None
    return user, password


def _authorized_basic(request: Request) -> bool:
    credentials = _basic_auth_credentials()
    if credentials is None:
        return True
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return False
    expected_user, expected_password = credentials
    return secrets.compare_digest(username, expected_user) and secrets.compare_digest(password, expected_password)


def _authorized_console(request: Request) -> bool:
    trusted_header = os.getenv("CONSOLE_TRUSTED_IDENTITY_HEADER", "x-auth-request-user").strip().lower()
    if not _env_bool("CONSOLE_AUTH_REQUIRED", "false"):
        return True
    if trusted_header and request.headers.get(trusted_header, "").strip():
        return True
    return _authorized_basic(request)


def _request_is_admin(request: Request) -> bool:
    """管理员能力默认关闭；开启后仍必须通过 Secret 注入的身份校验。"""
    if not _env_bool("CONSOLE_ADMIN_MODE", "false"):
        return False
    admin_users = set(_csv_env("CONSOLE_ADMIN_USERS", "admin"))
    credentials = _basic_auth_credentials()
    if credentials is not None and _authorized_basic(request):
        return credentials[0] in admin_users
    if _env_bool("CONSOLE_ADMIN_TRUSTED_IDENTITY", "false"):
        trusted_header = os.getenv("CONSOLE_TRUSTED_IDENTITY_HEADER", "x-auth-request-user").strip().lower()
        trusted_user = request.headers.get(trusted_header, "").strip() if trusted_header else ""
        return bool(trusted_user and trusted_user in admin_users)
    return False


def _admin_write_route(request: Request) -> bool:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    path = request.url.path.rstrip("/")
    if path == "/api/model-registry" or path == "/api/model-registry/active" or path.startswith("/api/model-registry/"):
        return True
    if path in {"/api/knowledge/documents", "/api/knowledge/upload", "/api/knowledge/reindex"}:
        return True
    if request.method == "DELETE" and path.startswith("/api/knowledge/documents/"):
        return True
    if path in {"/api/ops/skills", "/api/ops/skills/import"}:
        return True
    if path.startswith("/api/ops/skills/") and (request.method == "DELETE" or path.endswith("/delete")):
        return True
    return False


async def console_session(request: Request):
    is_admin = _request_is_admin(request)
    return {
        "status": "ok",
        "actor": _request_actor(request),
        "role": "admin" if is_admin else "viewer",
        "admin_mode": _env_bool("CONSOLE_ADMIN_MODE", "false"),
        "capabilities": {
            "read": True,
            "operate": True,
            "configure_models": is_admin,
            "upload_knowledge": is_admin,
            "manage_skills": is_admin,
        },
        "message": "管理员配置能力已启用" if is_admin else "当前为只读配置视图；运维审批能力按原有门禁执行",
    }


def _request_actor(request: Request) -> str:
    trusted = request.headers.get("x-auth-request-user") or request.headers.get("x-forwarded-user")
    if trusted:
        return str(trusted)[:120]
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            return base64.b64decode(header.split(" ", 1)[1]).decode("utf-8").split(":", 1)[0][:120]
        except Exception:
            pass
    return (request.client.host if request.client else "unknown")[:120]


def _rate_limit_allowed(request: Request) -> bool:
    if request.method == "GET" or request.url.path in {"/", "/health", "/api/build"}:
        return True
    limit = max(1, int(os.getenv("WRITE_RATE_LIMIT_PER_MINUTE", "120")))
    key = _request_actor(request)
    now = time.monotonic()
    start, count = RATE_LIMIT_WINDOWS.get(key, (now, 0))
    if now - start >= 60:
        start, count = now, 0
    count += 1
    RATE_LIMIT_WINDOWS[key] = (start, count)
    if len(RATE_LIMIT_WINDOWS) > 5000:
        for actor, (window_start, _) in list(RATE_LIMIT_WINDOWS.items()):
            if now - window_start >= 120:
                RATE_LIMIT_WINDOWS.pop(actor, None)
    return count <= limit


def _audit_event(action: str, actor: str, target: str, outcome: str, **details):
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "security_audit",
        "action": action,
        "actor": str(actor)[:120],
        "target": str(target)[:240],
        "outcome": outcome,
        "details": _redact_sensitive(details),
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def _safe_audit_event(audit_action: str, actor: str, audit_target: str, outcome: str, **details) -> dict | None:
    """审计失败不能阻断人工确认或变更执行，只把失败原因回填到任务事件。"""
    try:
        reserved = {
            "action": "detail_action",
            "target": "detail_target",
            "actor": "detail_actor",
            "outcome": "detail_outcome",
        }
        details = {reserved.get(key, key): value for key, value in details.items()}
        _audit_event(audit_action, actor, audit_target, outcome, **details)
        return None
    except Exception as exc:
        return {
            "type": type(exc).__name__,
            "message": _redact_text(str(exc)),
        }


def _security_headers(response):
    if not SECURITY_HEADERS_ENABLED:
        return response
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cache-Control", "no-store")
    csp = os.getenv(
        "CONTENT_SECURITY_POLICY",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
    )
    if csp:
        response.headers.setdefault("Content-Security-Policy", csp)
    return response


@app.middleware("http")
async def request_bulkhead_middleware(request: Request, call_next):
    try:
        request_started = time.perf_counter()
        request_id = request.headers.get("x-request-id") or f"req-{uuid.uuid4().hex[:16]}"
        request_body_limit = KNOWLEDGE_MAX_UPLOAD_BYTES + 1024 * 1024 if request.url.path == "/api/knowledge/upload" else MAX_REQUEST_BODY_BYTES
        if int(request.headers.get("content-length") or 0) > request_body_limit:
            return _security_headers(JSONResponse(
                status_code=413,
                content={"status": "rejected", "error": "request body too large"},
            ))
        if request.url.path != "/health" and not _authorized_console(request):
            return _security_headers(JSONResponse(
                status_code=401,
                content={"status": "unauthorized", "error": "console authentication required"},
                headers={"WWW-Authenticate": 'Basic realm="flawless SRE Console"'},
            ))
        if _admin_write_route(request) and not _request_is_admin(request):
            mode_enabled = _env_bool("CONSOLE_ADMIN_MODE", "false")
            return _security_headers(JSONResponse(
                status_code=401 if mode_enabled else 403,
                content={
                    "status": "admin_required",
                    "error": (
                        "管理员身份校验失败"
                        if mode_enabled else
                        "CONSOLE_ADMIN_MODE=false，模型、知识库和 Skill 写入已关闭"
                    ),
                    "admin_mode": mode_enabled,
                },
                headers={"WWW-Authenticate": 'Basic realm="Flawless SRE Admin"'} if mode_enabled else {},
            ))
        if not _rate_limit_allowed(request):
            return _security_headers(JSONResponse(
                status_code=429,
                content={"status": "rate_limited", "error": "write request rate limit exceeded", "request_id": request_id},
                headers={"Retry-After": "60"},
            ))
        async with REQUEST_BULKHEAD.slot():
            response = await call_next(request)
            response.headers.setdefault("X-Request-ID", request_id)
            response.headers.setdefault("Server-Timing", f"app;dur={(time.perf_counter() - request_started) * 1000:.1f}")
            return _security_headers(response)
    except BulkheadRejected as exc:
        return _security_headers(JSONResponse(
            status_code=503,
            content={
                "status": "busy",
                "error": str(exc),
                "message": "平台当前并发已达到保护阈值，请稍后重试。",
                "runtime": {"requests": REQUEST_BULKHEAD.snapshot()},
            },
        ))

# ============================================================
# Service Registry
# ============================================================
SERVICES = {
    "observability": os.getenv("OBSERVABILITY_URL", "http://localhost:8100"),
    "healing": os.getenv("HEALING_AGENT_URL", "http://localhost:8101/a2a/tasks"),
    "incident": os.getenv("INCIDENT_AGENT_URL", "http://localhost:8102/a2a/tasks"),
    "postmortem": os.getenv("POSTMORTEM_AGENT_URL", "http://flawless.k8s-agent.svc.cluster.local:8103/a2a/tasks"),
    "adapter": os.getenv("ADAPTER_URL", "http://localhost:8200"),
    "mcp": os.getenv("MCP_SERVER_URL", "http://localhost:8105/mcp"),
    "cmdb": os.getenv("CMDB_URL", ""),
    "prometheus": os.getenv("PROMETHEUS_URL", ""),
    "loki": os.getenv("LOKI_URL", ""),
    "tempo": os.getenv("TEMPO_URL", ""),
    "grafana": os.getenv("GRAFANA_URL", ""),
}

# In-memory store for incidents / postmortems (mirrors what backend agents produce)
INCIDENTS_STORE: list[dict] = []
POSTMORTEMS_STORE: list[dict] = []
ALERT_HISTORY: list[dict] = []
A2A_TRACES: list[dict] = []
LLM_OBSERVABILITY_STORE: list[dict] = []
ALGORITHM_DECISIONS_STORE: list[dict] = []
MODEL_BENCHMARK_STORE: list[dict] = []
LAST_INSPECTION_PAYLOAD: dict = {}
OPS_JOBS: dict[str, dict] = {}
OPS_JOB_TASKS: dict[str, asyncio.Task] = {}
OPS_JOB_CANCEL_EVENTS: dict[str, asyncio.Event] = {}
OPS_JOB_STEP_APPROVAL_EVENTS: dict[str, asyncio.Event] = {}
OPS_JOBS_LOCK = asyncio.Lock()
OPS_JOB_STORE_PATH = Path(os.getenv("OPS_JOB_STORE_PATH", "").strip()) if os.getenv("OPS_JOB_STORE_PATH", "").strip() else None
OPS_JOB_STORE_ERROR = ""
RATE_LIMIT_WINDOWS: dict[str, tuple[float, int]] = {}
RELIABILITY_STORE = ReliabilityStore()
OPS_SKILL_REGISTRY = OpsSkillRegistry(OPS_SKILL_ROOT, legacy_path=OPS_SKILL_STORE_PATH)


# ============================================================
# HTTP Client
# ============================================================
@asynccontextmanager
async def _client(timeout: int = 60):
    timeout_key = int(timeout)
    client = OUTBOUND_HTTP_CLIENTS.get(timeout_key)
    if client is not None:
        yield client
        return
    fallback = httpx.AsyncClient(
        timeout=httpx.Timeout(float(timeout_key), connect=5.0, pool=2.0),
        verify=OUTBOUND_VERIFY_SSL,
        limits=HTTP_LIMITS,
    )
    try:
        yield fallback
    finally:
        await fallback.aclose()


def _internal_headers() -> dict[str, str]:
    key = os.getenv("INTERNAL_API_KEY", "").strip()
    return {"X-Internal-API-Key": key} if key else {}


def _service_health_url(name: str, url: str) -> str:
    if name == "mcp" and url.endswith("/mcp"):
        return url.removesuffix("/mcp") + "/health"
    if url.rstrip("/").endswith("/a2a/tasks"):
        return url.rstrip("/").removesuffix("/a2a/tasks") + "/health"
    return url.rstrip("/") + "/health"


def _service_health_candidates(name: str, url: str) -> list[str]:
    primary = _service_health_url(name, url)
    base = url.rstrip("/")
    if base.endswith("/a2a/tasks"):
        base = base.removesuffix("/a2a/tasks")
    if name == "mcp" and base.endswith("/mcp"):
        base = base.removesuffix("/mcp")
    candidates = [primary, f"{base}/health"]
    unique = []
    for item in candidates:
        if item and item not in unique:
            unique.append(item)
    return unique


def _mcp_tools_url() -> str:
    base = SERVICES["mcp"].rstrip("/")
    if base.endswith("/mcp"):
        return base + "/tools/call"
    return base + "/mcp/tools/call"


def _append_unique(store: list[dict], item: dict, key: str):
    if not item:
        return
    item_id = item.get(key)
    if item_id and any(existing.get(key) == item_id for existing in store):
        return
    bounded_append(store, item, STORE_LIMIT)


def _remember_graph_result(raw: dict | None):
    if not isinstance(raw, dict):
        return

    incident = raw.get("incident")
    if isinstance(incident, dict) and incident.get("incident_id"):
        _append_unique(INCIDENTS_STORE, incident, "incident_id")

    postmortem = raw.get("postmortem")
    if isinstance(postmortem, dict) and postmortem.get("report"):
        pm = {
            "id": str(uuid.uuid4())[:8],
            "incident_id": (incident or {}).get("incident_id", "?"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "report": postmortem.get("report", ""),
        }
        bounded_append(POSTMORTEMS_STORE, pm, STORE_LIMIT)


SENSITIVE_KEY_RE = re.compile(r"(token|secret|password|passwd|authorization|cookie|client_secret|api[_-]?key|access[_-]?key)", re.I)
SENSITIVE_VALUE_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I),
    re.compile(r"(client_secret=)[^&\s]+", re.I),
    re.compile(r"(password=)[^&\s]+", re.I),
    re.compile(r"(token=)[^&\s]+", re.I),
    re.compile(r"(sk-lf-)[A-Za-z0-9-]+", re.I),
    re.compile(r"(pk-lf-)[A-Za-z0-9-]+", re.I),
]


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in SENSITIVE_VALUE_PATTERNS:
        redacted = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", redacted)
    return redacted


def _redact_sensitive(value, depth: int = 0):
    if depth > 8:
        return "[REDACTED:MAX_DEPTH]"
    if isinstance(value, dict):
        secret_object = str(value.get("kind") or "").lower() == "secret"
        result = {}
        for key, item in value.items():
            if SENSITIVE_KEY_RE.search(str(key)) or (secret_object and str(key) in {"data", "stringData"}):
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact_sensitive(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_redact_sensitive(item, depth + 1) for item in value[:200]]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _compact_dict(value, limit: int = 2000):
    try:
        value = _redact_sensitive(value)
        text = json.dumps(value, ensure_ascii=False)
        if len(text) <= limit:
            return value
        return {"_truncated": True, "preview": text[:limit]}
    except Exception:
        text = _redact_text(str(value))
        return text[:limit]


def _persist_ops_jobs() -> None:
    """Persist public, redacted job lineage; live asyncio events remain process-local."""
    global OPS_JOB_STORE_ERROR
    if OPS_JOB_STORE_PATH is None:
        return
    try:
        OPS_JOB_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "jobs": [
                _redact_sensitive(job)
                for job in list(OPS_JOBS.values())[-int(os.getenv("OPS_JOB_STORE_LIMIT", "200")):]
            ],
        }
        temporary = OPS_JOB_STORE_PATH.with_suffix(f"{OPS_JOB_STORE_PATH.suffix}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(temporary, OPS_JOB_STORE_PATH)
        OPS_JOB_STORE_ERROR = ""
    except Exception as exc:
        OPS_JOB_STORE_ERROR = f"{type(exc).__name__}: {_redact_text(str(exc))}"


def _restore_ops_jobs() -> None:
    """Restore audit/history and queue unfinished incidents for safe revalidation."""
    global OPS_JOB_STORE_ERROR
    if OPS_JOB_STORE_PATH is None or not OPS_JOB_STORE_PATH.is_file():
        return
    try:
        raw = json.loads(OPS_JOB_STORE_PATH.read_text(encoding="utf-8"))
        restored = raw.get("jobs") if isinstance(raw, dict) else []
        for item in restored or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            job = item
            if job.get("status") in {"queued", "running", "awaiting_approval", "cancelling"}:
                now = datetime.now(timezone.utc).isoformat()
                job.update({
                    "status": "resume_pending",
                    "stage": "worker_restarted",
                    "message": "API Worker 在任务未闭环时重启；旧审批已失效，启动后将自动重新取证并继续故障链。",
                    "pending_approval": None,
                    "approved_change_index": 0,
                    "last_consumed_approval": None,
                    "updated_at": now,
                })
                job.setdefault("events", []).append({
                    "timestamp": now,
                    "stage": "worker_restarted",
                    "level": "warning",
                    "message": "系统不会沿用重启前审批；将从实时集群状态恢复持续诊断，任何新变更都重新逐项审批。",
                })
                prior_result = job.get("result") if isinstance(job.get("result"), dict) else {}
                job["result"] = {
                    **prior_result,
                    "status": "resume_pending",
                    "executed": bool(prior_result.get("executed")),
                    "verification": {
                        **(prior_result.get("verification") or {}),
                        "status": "unknown",
                        "recovered": None,
                        "message": "Worker 重启后必须重新读取目标状态，不能沿用重启前恢复结论。",
                    },
                    "blocked_reason": "执行 Worker 重启，内存中的执行租约和旧审批已失效；任务将自动恢复只读取证。",
                    "operator_steps": [
                        "刷新目标 Workload、Pod、Events、日志和变更回执，确认重启前动作是否已被 API 接受。",
                        "等待系统自动恢复同一故障链并生成新方案，无需重新创建任务。",
                        "重新核对新方案指纹并逐步审批；旧 approval_id 永不复用。",
                    ],
                }
            OPS_JOBS[str(job["id"])] = job
        OPS_JOB_STORE_ERROR = ""
    except Exception as exc:
        OPS_JOB_STORE_ERROR = f"{type(exc).__name__}: {_redact_text(str(exc))}"


_restore_ops_jobs()


def _record_llm_observation(source: str, request_payload: dict, response_payload: dict, started_at: datetime, error: str = ""):
    request_payload = request_payload if isinstance(request_payload, dict) else {}
    response_payload = response_payload if isinstance(response_payload, dict) else {}
    raw = response_payload.get("raw") or {}
    raw = raw if isinstance(raw, dict) else {"raw": raw}
    diagnosis = raw.get("diagnosis") or {}
    decision = raw.get("decision") or {}
    observability = raw.get("observability") or {}
    alert = raw.get("alert") or request_payload or {}
    llm_meta = diagnosis.get("diagnosis_metadata") or {}
    quality_scores = llm_meta.get("quality_scores") or observability.get("quality_scores") or {}
    metadata = {
        "cluster": request_payload.get("cluster") or alert.get("cluster") or "all",
        "cluster_id": request_payload.get("cluster_id") or alert.get("cluster_id") or "all",
        "namespace": request_payload.get("namespace") or alert.get("namespace") or "",
        "workload": request_payload.get("deployment") or alert.get("deployment") or alert.get("workload_name") or "",
        "severity": request_payload.get("severity") or alert.get("severity") or "",
        "model_profile_id": request_payload.get("model_profile_id") or ((diagnosis.get("diagnosis_metadata") or {}).get("model_profile_id")) or get_active_model_profile_id(),
        "langfuse_trace_id": observability.get("trace_id") or llm_meta.get("langfuse_trace_id", ""),
        "langfuse_session_id": observability.get("session_id") or llm_meta.get("langfuse_session_id", ""),
    }
    token_usage = {}
    try:
        token_usage = (((diagnosis.get("diagnosis_metadata") or {}).get("token_usage")) or {})
    except Exception:
        token_usage = {}
    item = {
        "id": str(uuid.uuid4())[:12],
        "trace_id": metadata["langfuse_trace_id"],
        "session_id": metadata["langfuse_session_id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "status": "failed" if error or response_payload.get("backend_error") or raw.get("error") else "ok",
        "latency_ms": int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000),
        "llm": {
            "model": llm_meta.get("model") or os.getenv("LLM_MODEL", ""),
            "model_profile_id": metadata["model_profile_id"],
            "gateway": os.getenv("LLM_GATEWAY_BASE") or os.getenv("LLM_API_BASE", ""),
            "langfuse_enabled": os.getenv("LANGFUSE_ENABLED", "true").lower() in {"1", "true", "yes", "on"},
            "langfuse_host": os.getenv("LANGFUSE_HOST", ""),
            "langfuse_trace_id": metadata["langfuse_trace_id"],
            "langfuse_session_id": metadata["langfuse_session_id"],
            "token_usage": token_usage,
            "estimated_cost_usd": llm_meta.get("estimated_cost_usd") or observability.get("estimated_cost_usd") or 0,
            "quality_scores": quality_scores,
        },
        "metadata": metadata,
        "data_flow": [
            {"stage": "user", "name": "Operator / UI", "detail": source},
            {"stage": "context", "name": "Kubernetes / Rancher / CMDB / Prometheus", "detail": f"{metadata.get('cluster')} / {metadata.get('namespace')}"},
            {"stage": "llm", "name": llm_meta.get("model") or os.getenv("LLM_MODEL", "LLM"), "detail": metadata["model_profile_id"]},
            {"stage": "action", "name": decision.get("action") or diagnosis.get("suggested_action") or "answer", "detail": "human approval required" if decision.get("require_human_approval") else "no mutation or approved path"},
        ],
        "input": _compact_dict(request_payload),
        "output": {
            "preview": _redact_text(str(response_payload.get("answer") or response_payload.get("result") or ""))[:1200],
            "answer_preview": _redact_text(str(response_payload.get("answer") or ""))[:1200],
            "root_cause": _redact_sensitive(diagnosis.get("root_cause", "")),
            "impact": _redact_sensitive(diagnosis.get("impact", "")),
            "confidence": diagnosis.get("confidence", ""),
            "risk_level": diagnosis.get("risk_level", ""),
            "action": decision.get("action", diagnosis.get("suggested_action", "")),
            "decision_source": decision.get("source", ""),
            "dry_run": decision.get("dry_run"),
            "signals": _redact_sensitive(diagnosis.get("signals", [])[:8] if isinstance(diagnosis.get("signals", []), list) else []),
            "immediate_actions": _redact_sensitive(diagnosis.get("immediate_actions", [])[:8] if isinstance(diagnosis.get("immediate_actions", []), list) else []),
            "quality_scores": quality_scores,
            "llm_error": _redact_text(str(diagnosis.get("llm_error") or raw.get("error") or error or response_payload.get("backend_error", ""))),
        },
        "chain": [
            {"step": "collect_context", "name": "采集上下文", "status": "ok" if raw.get("k8s_context") else "unknown"},
            {"step": "llm_diagnosis", "name": "LLM 诊断", "status": "fallback" if diagnosis.get("llm_error") else ("ok" if diagnosis else "unknown")},
            {"step": "decision", "name": "决策编排", "status": "ok" if decision else "unknown", "action": decision.get("action", "")},
            {"step": "healing", "name": "修复执行", "status": "ok" if raw.get("remediation") else "unknown"},
            {"step": "incident", "name": "事件沉淀", "status": "ok" if raw.get("incident") else "unknown"},
            {"step": "postmortem", "name": "复盘生成", "status": "ok" if raw.get("postmortem") else "skipped"},
        ],
        "raw": _compact_dict(raw, 6000),
    }
    bounded_append(LLM_OBSERVABILITY_STORE, item, STORE_LIMIT)


def _record_algorithm_decision(
    algorithm: str,
    used_by: str,
    output: dict,
    input_summary: dict | None = None,
    action_effect: str = "",
):
    item = {
        "id": str(uuid.uuid4())[:12],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "algorithm": algorithm,
        "used_by": used_by,
        "input_summary": _redact_sensitive(input_summary or {}),
        "output": _redact_sensitive(output),
        "action_effect": _redact_text(action_effect),
    }
    bounded_append(ALGORITHM_DECISIONS_STORE, item, STORE_LIMIT)
    return item


def _normalize_alertmanager_body(body: dict) -> tuple[dict, dict]:
    if "alerts" in body:
        alerts = body.get("alerts") or []
        first = alerts[0] if alerts else {}
        labels = first.get("labels", {})
        annotations = first.get("annotations", {})
        normalized = {
            "receiver": body.get("receiver", "sre-agent"),
            "status": body.get("status", "firing"),
            "auto_healing_enabled": bool(body.get("auto_healing_enabled", False)),
            "alerts": alerts,
        }
        meta = {
            "alert_name": labels.get("alertname", "ManualAlert"),
            "cluster": labels.get("cluster", ""),
            "cluster_id": labels.get("cluster_id", ""),
            "namespace": labels.get("namespace", "default"),
            "deployment": labels.get("deployment", ""),
            "severity": labels.get("severity", "P2"),
            "message": annotations.get("summary") or annotations.get("description") or "",
        }
        return normalized, meta

    severity = body.get("severity", "P2")
    alert_name = body.get("alert_name", "ManualAlert")
    namespace = body.get("namespace", "default")
    cluster = body.get("cluster", "")
    cluster_id = body.get("cluster_id", cluster)
    deployment = body.get("deployment", "")
    message = body.get("message", "")
    normalized = {
        "receiver": "sre-agent",
        "status": "firing",
        "auto_healing_enabled": bool(body.get("auto_healing_enabled", False)),
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": alert_name,
                "cluster": cluster,
                "cluster_id": cluster_id,
                "namespace": namespace,
                "deployment": deployment,
                "service": deployment,
                "severity": severity,
                "priority": "critical" if severity in ("P0", "P1") else "high",
                "auto_healing_enabled": str(bool(body.get("auto_healing_enabled", False))).lower(),
            },
            "annotations": {
                "summary": message,
                "description": message,
            },
        }],
    }
    return normalized, {
        "alert_name": alert_name,
        "cluster": cluster,
        "cluster_id": cluster_id,
        "namespace": namespace,
        "deployment": deployment,
        "severity": severity,
        "message": message,
    }


# ============================================================
# Static files
# ============================================================
async def index():
    modern_index = MODERN_DIST_DIR / "index.html"
    if modern_index.exists():
        return FileResponse(modern_index)
    return JSONResponse(
        status_code=503,
        content={"status": "frontend_build_missing", "error": "frontend/dist/index.html not found; rebuild the React classic console"},
    )


async def legacy_index():
    return RedirectResponse(url="/", status_code=307)


async def favicon():
    icon = MODERN_DIST_DIR / "favicon.svg"
    if icon.exists():
        return FileResponse(icon, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="favicon not found")


async def process_health():
    return {"status": "ok", "version": APP_BUILD_VERSION, "signature": APP_CODE_SIGNATURE}


# ============================================================
# Health
# ============================================================
async def health():
    """健康检查：每个核心后端必须从明确的健康端点返回 2xx。"""
    import asyncio

    async def check_one(name: str, url: str) -> dict:
        errors = []
        async with OUTBOUND_BULKHEAD.slot():
            async with _client(3) as c:
                for candidate in _service_health_candidates(name, url):
                    try:
                        resp = await c.get(candidate)
                        if 200 <= resp.status_code < 300:
                            return {
                                "status": "up",
                                "code": resp.status_code,
                                "url": candidate,
                                "configured_url": url,
                                "error": "" if resp.status_code < 400 else resp.text[:240],
                            }
                        errors.append(f"{candidate} -> {resp.status_code}: {resp.text[:160]}")
                    except Exception as e:
                        errors.append(f"{candidate} -> {type(e).__name__}: {e}")
        return {
            "status": "down",
            "code": None,
            "url": _service_health_url(name, url),
            "configured_url": url,
            "error": " | ".join(errors)[-800:],
        }

    # 后端 Agent 是核心健康面；CMDB/Prometheus 是可选数据源，未配置时不应拖垮主状态。
    optional_sources = {"cmdb", "prometheus", "loki", "tempo", "grafana"}
    health_urls = {
        name: url
        for name, url in SERVICES.items()
        if name not in optional_sources and url
    }

    names = list(health_urls.keys())
    checked = await asyncio.gather(
        *[check_one(name, health_urls[name]) for name in names],
        return_exceptions=True,
    )
    results = {}
    for name, item in zip(names, checked):
        if isinstance(item, Exception):
            results[name] = {
                "status": "down",
                "code": None,
                "url": _service_health_url(name, health_urls[name]),
                "configured_url": health_urls[name],
                "error": f"{type(item).__name__}: {item}",
            }
        else:
            results[name] = item
    for name in optional_sources:
        url = SERVICES.get(name, "")
        results[name] = {"status": "configured" if url else "disabled", "code": None}
    all_up = all(v["status"] == "up" for name, v in results.items() if name not in optional_sources)
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "services": results, "all_healthy": all_up}


# ============================================================
# Chat / Diagnosis
# 请求模型统一位于 backend/app/schemas，避免功能实现与 HTTP 契约混杂。
# ============================================================


def _chat_user_text(req: ChatRequest) -> str:
    return (req.original_message or req.message or "").strip()


def _chat_scope(req: ChatRequest) -> dict:
    cluster = (req.cluster_id or req.cluster or "").strip()
    cluster_name = (req.cluster or req.cluster_id or "").strip()
    namespace = (req.namespace or "").strip()
    workload = (req.deployment or "").strip()
    pod = (req.pod or "").strip()
    cluster_selected = bool(cluster and cluster.lower() not in {"all", "*", "所有", "所有集群"})
    namespace_selected = bool(namespace and namespace.lower() not in {"all", "*", "所有", "所有namespace"})
    workload_selected = bool(workload or pod)
    return {
        "cluster": cluster_name,
        "cluster_id": cluster,
        "namespace": namespace,
        "workload_type": (req.workload_type or "Workload").strip(),
        "workload": workload,
        "pod": pod,
        "cluster_selected": cluster_selected,
        "namespace_selected": namespace_selected,
        "workload_selected": workload_selected,
        "ops_scope_selected": workload_selected or cluster_selected or (cluster_selected and namespace_selected),
    }


def _pod_matches_workload(pod: dict, workload_name: str, workload_type: str = "") -> bool:
    """判断 Pod 是否属于用户选择的 Workload，优先使用 owner 信息，名称前缀只做兜底。"""
    workload_name = str(workload_name or "").strip()
    if not workload_name:
        return True
    workload_type = str(workload_type or "").strip().lower()
    pod_workload = pod.get("workload") or {}
    candidates = {
        str(pod.get("workload_name") or ""),
        str(pod_workload.get("name") or ""),
    }
    kinds = {
        str(pod.get("workload_kind") or "").lower(),
        str(pod_workload.get("kind") or "").lower(),
    }
    if workload_name in candidates and (not workload_type or workload_type.lower() in {"workload", *kinds}):
        return True
    # StatefulSet/DaemonSet/ReplicaSet 兜底：名称一般以 workload- 开头。
    return str(pod.get("name") or "").startswith(f"{workload_name}-")


def _pod_evidence_priority(pod: dict) -> tuple[int, int, str]:
    """给一个 Workload 下的多个 Pod 排序，优先读取最能解释故障的 Pod。"""
    category, severity, _ = _classify_pod_issue(pod, [])
    severity_score = {"P0": 500, "P1": 400, "P2": 260, "P3": 120}.get(str(severity or ""), 0)
    category_score = {
        "crashloop": 120,
        "image_pull": 110,
        "storage_config": 105,
        "scheduling": 90,
        "network": 70,
        "not_ready": 50,
    }.get(str(category or ""), 0)
    phase = str(pod.get("phase") or "")
    phase_score = {"Failed": 80, "Unknown": 70, "Pending": 60, "Running": 10}.get(phase, 0)
    ready_penalty = 0 if pod.get("ready") else 45
    restart_score = min(80, int(pod.get("restart_count") or 0) * 8)
    completed_penalty = -1000 if _pod_completed_successfully(pod) else 0
    return (
        severity_score + category_score + phase_score + ready_penalty + restart_score + completed_penalty,
        int(pod.get("restart_count") or 0),
        str(pod.get("name") or ""),
    )


def _select_representative_pod(
    pods: list[dict],
    *,
    workload_name: str = "",
    workload_type: str = "",
    requested_pod: str = "",
) -> tuple[dict | None, list[dict]]:
    """从 Workload/Pod 范围内选择最值得下钻日志和事件的 Pod。"""
    requested_pod = str(requested_pod or "").strip()
    if requested_pod:
        selected = next((pod for pod in pods if requested_pod == str(pod.get("name") or "")), None)
        return selected, [selected] if selected else []
    matches = [
        pod for pod in pods
        if _pod_matches_workload(pod, workload_name, workload_type)
    ]
    if not matches:
        return None, []
    matches.sort(key=_pod_evidence_priority, reverse=True)
    return matches[0], matches


def _select_chat_target_pod(pods: list[dict], req: ChatRequest) -> dict | None:
    """Resolve exactly one selected target; ranking order is never a target selector."""
    requested_pod = (req.pod or "").strip()
    requested_workload = (req.deployment or "").strip()
    selected, _ = _select_representative_pod(
        pods,
        workload_name=requested_workload,
        workload_type=req.workload_type,
        requested_pod=requested_pod,
    )
    return selected


def _enforce_chat_target_binding(req: ChatRequest, data: dict) -> dict:
    """Keep diagnosis and every executable action on the operator-selected target."""
    if not isinstance(data, dict):
        return data
    requested_workload = (req.deployment or "").strip()
    requested_pod = (req.pod or "").strip()
    if not requested_workload and not requested_pod:
        return data

    raw = data.setdefault("raw", {})
    if not isinstance(raw, dict):
        return data
    alert = raw.setdefault("alert", {})
    if isinstance(alert, dict):
        alert.update({
            "cluster": req.cluster,
            "cluster_id": req.cluster_id,
            "namespace": req.namespace,
            "deployment": requested_workload,
            "workload_name": requested_workload,
            "workload_type": req.workload_type,
            "pod": requested_pod,
            "target_id": req.target_id,
        })

    rejected: list[dict] = []
    workload_actions = {
        "restart", "patch_workload", "patch_workload_volume", "scale_out", "rollback_workload",
        "create_workload",
    }
    pod_actions = {"recreate_pod", "evict_pod"}

    def bind_changes(changes) -> list[dict]:
        bound: list[dict] = []
        for raw_change in changes or []:
            if not isinstance(raw_change, dict):
                continue
            change = dict(raw_change)
            action = str(change.get("type") or change.get("action") or "")
            if requested_workload and action in workload_actions:
                existing = str(change.get("workload_name") or "").strip()
                if existing and existing != requested_workload:
                    rejected.append({"action": action, "target": existing, "reason": "与操作员选择的 Workload 不一致"})
                    continue
                change.update({
                    "namespace": req.namespace,
                    "workload_type": req.workload_type,
                    "workload_name": requested_workload,
                })
            if requested_pod and action in pod_actions:
                existing_pod = str(change.get("pod_name") or "").strip()
                if existing_pod and existing_pod != requested_pod:
                    rejected.append({"action": action, "target": existing_pod, "reason": "与操作员选择的 Pod 不一致"})
                    continue
                change.update({"namespace": req.namespace, "pod_name": requested_pod})
            bound.append(change)
        return bound

    diagnosis = raw.get("diagnosis") or {}
    if isinstance(diagnosis, dict):
        plan = diagnosis.get("remediation_plan")
        if isinstance(plan, dict):
            plan.update({
                "cluster": req.cluster,
                "cluster_id": req.cluster_id,
                "namespace": req.namespace,
                "target": (
                    f"{req.workload_type}/{requested_workload}"
                    if requested_workload else f"Pod/{requested_pod}"
                ),
                "pod_name": requested_pod or plan.get("pod_name", ""),
                "target_binding": "operator_selected",
            })
            plan["changes"] = bind_changes(plan.get("changes") or [])
            if rejected:
                plan["evidence_gap"] = "已拒绝模型生成的跨目标动作；请基于当前指定对象重新取证和规划。"
        diagnosis["proposed_changes"] = bind_changes(diagnosis.get("proposed_changes") or [])
    decision = raw.get("decision") or {}
    if isinstance(decision, dict):
        decision["target"] = {
            "namespace": req.namespace,
            "workload_type": req.workload_type,
            "workload_name": requested_workload,
            "pod_name": requested_pod,
        }
        decision["proposed_changes"] = bind_changes(decision.get("proposed_changes") or [])
    raw["target_binding"] = {
        "source": "operator_selected",
        "target_id": req.target_id,
        "cluster": req.cluster,
        "cluster_id": req.cluster_id,
        "namespace": req.namespace,
        "workload_type": req.workload_type,
        "workload_name": requested_workload,
        "pod_name": requested_pod,
        "rejected_cross_target_actions": rejected,
    }
    return data


def _is_likely_sre_question(text: str) -> bool:
    lowered = text.lower()
    keywords = [
        "k8s", "kubernetes", "pod", "pods", "deployment", "statefulset", "daemonset", "namespace",
        "node", "ingress", "service", "svc", "container", "crashloop", "crashloopbackoff",
        "imagepull", "oom", "pending", "prometheus", "alert", "告警", "巡检", "运维", "集群",
        "节点", "容器", "日志", "事件", "修复", "拓扑", "rbac", "rancher", "helm", "yaml",
        "cpu", "memory", "重启", "异常", "服务不可用", "发布", "回滚", "灰度", "变更",
        "扩容", "缩容", "容量", "可用性", "稳定性", "链路", "调用", "依赖", "流量", "网关",
        "延迟", "超时", "丢包", "dns", "证书", "tls", "ingress", "负载均衡", "熔断", "限流",
        "队列", "kafka", "redis", "mysql", "elasticsearch", "elk", "日志链路", "监控", "指标",
        "trace", "tracing", "链路追踪", "apm", "slo", "sla", "错误率", "成功率", "排查",
        "定位", "根因", "故障", "宕机", "不可用", "慢查询", "资源不足", "权限", "secret",
        "configmap", "pvc", "pv", "存储卷", "镜像", "探针", "readiness", "liveness", "startup",
        "工作负载", "中间件", "云", "私有云", "公有云", "阿里云", "rds", "slb", "ack",
    ]
    return any(k in lowered for k in keywords)


def _is_clear_general_chat(text: str) -> bool:
    lowered = text.lower().strip()
    if not lowered:
        return False
    general_markers = [
        "讲个笑话", "写首诗", "写一首诗", "写小说", "写作文", "翻译成", "帮我翻译", "润色这段",
        "生成海报", "画一张", "菜谱", "减肥", "健身计划", "旅游攻略", "历史故事", "数学题",
        "英语作文", "自我介绍", "情书", "歌词", "电影推荐", "今天吃什么", "天气怎么样",
        "欢迎语", "宣传语", "广告语", "文案", "帮我总结这段", "改写这段", "续写",
    ]
    greetings = {"你好", "hi", "hello", "在吗", "你是谁", "介绍一下你自己", "谢谢", "ok", "好的"}
    if lowered in greetings:
        return True
    return any(item in lowered for item in general_markers)


def _fallback_chat_intent(req: ChatRequest, reason: str = "rule_fallback") -> dict:
    text = _chat_user_text(req)
    scope = _chat_scope(req)
    likely_sre = _is_likely_sre_question(text)
    clear_general = _is_clear_general_chat(text)
    vague_ops = ["看看", "检查", "排查", "分析", "修复", "为什么", "不行", "异常", "失败", "慢", "卡", "定位", "影响", "风险", "建议"]
    if likely_sre:
        mode, confidence, route_reason = "sre", 0.88, "命中运维/稳定性/云原生相关语义"
    elif scope["ops_scope_selected"] and not clear_general:
        mode, confidence, route_reason = "sre", 0.76, "已选择集群/命名空间/工作负载上下文，按运维意图处理"
    elif scope["namespace_selected"] and any(item in text for item in vague_ops) and not clear_general:
        mode, confidence, route_reason = "sre", 0.72, "已选择 namespace 且问题包含排查/分析/修复类表达"
    else:
        mode, confidence, route_reason = "general", 0.78 if clear_general else 0.62, "未发现运维意图，按通用问答处理"
    return {
        "mode": mode,
        "confidence": confidence,
        "reason": f"{reason}: {route_reason}",
        "source": "rules",
        "scope": scope,
        "signals": {
            "likely_sre_keywords": likely_sre,
            "clear_general_chat": clear_general,
            "ops_scope_selected": scope["ops_scope_selected"],
        },
    }


def _extract_json_object(text: str) -> dict:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I | re.S).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if not match:
        raise ValueError("LLM intent router did not return JSON")
    return json.loads(match.group(0))


async def _route_chat_intent(req: ChatRequest) -> dict:
    text = _chat_user_text(req)
    if not text:
        return _fallback_chat_intent(req, "empty_message")
    if not _env_bool("LLM_INTENT_ROUTER_ENABLED", "true"):
        return _fallback_chat_intent(req, "llm_intent_router_disabled")

    scope = _chat_scope(req)
    prompt = f"""你是 flawless SRE Copilot 的意图路由器。请判断用户输入应该进入哪条链路：

1. sre：任何可能和运维、SRE、DevOps、云、Kubernetes、Rancher、容器、应用稳定性、故障排查、发布变更、容量、性能、网络、存储、安全、日志、监控、告警、拓扑影响、中间件、数据库、云资源有关的问题。
2. general：明确和运维无关的普通聊天、写作、翻译、生活、创意类问题。

重要规则：
- 不要只依赖关键词。用户可能说“帮我看看为什么不行”“这个服务很慢”“昨天发布后有问题”，这些都应视为 sre。
- 如果用户已经选择了 cluster / namespace / workload，除非问题明显是闲聊或写作，否则默认视为 sre。
- 模糊问题宁可判为 sre，因为 SRE 链路会继续采集证据；只有非常明确无关时才判 general。
- 只返回 JSON，不要解释，不要 Markdown。

可用上下文：
{json.dumps(scope, ensure_ascii=False)}

用户输入：
{text}

返回格式：
{{"mode":"sre|general","confidence":0.0到1.0,"reason":"一句中文原因","signals":["简短信号1","简短信号2"]}}
"""
    started_at = datetime.now(timezone.utc)
    trace = start_trace(
        "flawless.chat.intent_router",
        trace_id=new_trace_id("intent"),
        user_id="flawless-ui",
        session_id=f"chat-intent:{req.cluster_id or req.cluster}:{req.namespace}",
        input={"message": text, "scope": scope},
        metadata={"cluster": req.cluster, "cluster_id": req.cluster_id, "namespace": req.namespace, "model_profile_id": req.model_profile_id},
        tags=["flawless", "intent-router", "llm"],
    )
    generation = start_generation(
        trace,
        "llm_intent_router",
        model=req.model_profile_id or os.getenv("LLM_MODEL", ""),
        input={"message": text, "scope": scope},
        metadata={"purpose": "route chat to SRE or general"},
        prompt_name="flawless.chat.intent_router.v1",
    )
    try:
        def _call_llm() -> tuple[str, dict]:
            from agents.llm_client import get_llm
            resp = get_llm(temperature=0.0, max_tokens=220, profile_id=req.model_profile_id or None).invoke(prompt)
            return (getattr(resp, "content", "") or ""), ((getattr(resp, "response_metadata", {}) or {}).get("token_usage") or {})

        timeout = float(os.getenv("LLM_INTENT_ROUTER_TIMEOUT_SECONDS", "4.0"))
        content, usage = await asyncio.wait_for(asyncio.to_thread(_call_llm), timeout=timeout)
        parsed = _extract_json_object(content)
        mode = str(parsed.get("mode") or "").strip().lower()
        if mode in {"ops", "operation", "operations", "k8s", "kubernetes"}:
            mode = "sre"
        if mode not in {"sre", "general"}:
            raise ValueError(f"unsupported mode {mode!r}")
        confidence = float(parsed.get("confidence", 0.0) or 0.0)
        intent = {
            "mode": mode,
            "confidence": max(0.0, min(1.0, confidence)),
            "reason": str(parsed.get("reason") or "LLM 意图路由完成")[:300],
            "source": "llm",
            "scope": scope,
            "signals": parsed.get("signals") if isinstance(parsed.get("signals"), list) else [],
            "token_usage": usage,
            "latency_ms": int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000),
            "langfuse_trace_id": getattr(trace, "id", ""),
            "estimated_cost_usd": estimate_llm_cost_usd(usage, model_profile_id=req.model_profile_id),
        }
        # Low-confidence general classifications are risky in an SRE console; fall back toward SRE when scope exists.
        if intent["mode"] == "general" and scope["ops_scope_selected"] and intent["confidence"] < 0.82 and not _is_clear_general_chat(text):
            intent["mode"] = "sre"
            intent["reason"] = f"LLM 低置信度判为通用，但已选择运维范围，升级为 SRE：{intent['reason']}"
        end_observation(generation, output=intent, usage=usage, metadata={"estimated_cost_usd": intent["estimated_cost_usd"]})
        score_observation(trace, name="intent_router.confidence", value=intent["confidence"], comment=intent["reason"])
        update_trace(trace, output=intent, metadata={"mode": intent["mode"], "confidence": intent["confidence"]})
        flush_observability()
        return intent
    except Exception as exc:
        intent = _fallback_chat_intent(req, f"llm_intent_router_failed:{type(exc).__name__}")
        intent["error"] = str(exc)[:300]
        end_observation(generation, output=intent, status_message=f"{type(exc).__name__}: {exc}", level="ERROR")
        update_trace(trace, output=intent, metadata={"mode": intent.get("mode"), "fallback": True})
        flush_observability()
        return intent


def _is_general_question(req: ChatRequest) -> bool:
    return _fallback_chat_intent(req).get("mode") == "general"


async def _general_chat_response(req: ChatRequest, intent: dict | None = None) -> dict:
    user_text = _chat_user_text(req)
    trace = start_trace(
        "flawless.chat.general_answer",
        trace_id=new_trace_id("general"),
        user_id="flawless-ui",
        session_id=f"chat-general:{req.cluster_id or req.cluster}:{req.namespace}",
        input={"message": user_text, "intent": intent or {}},
        metadata={"cluster": req.cluster, "cluster_id": req.cluster_id, "namespace": req.namespace, "model_profile_id": req.model_profile_id},
        tags=["flawless", "general-chat", "llm"],
    )
    generation = start_generation(
        trace,
        "llm_general_answer",
        model=req.model_profile_id or os.getenv("LLM_MODEL", ""),
        input={"message": user_text},
        metadata={"intent_router": intent or {}},
        prompt_name="flawless.chat.general.v1",
    )
    prompt = f"""你是 flawless SRE Copilot。用户的问题不一定和 Kubernetes 运维有关。
如果问题和 K8S/SRE 无关，请直接用自然、清晰、专业的方式回答，不要强行生成故障诊断、修复流程或 Kubernetes 操作。
如果问题需要澄清，可以简短说明。

用户问题：
{user_text}
"""
    try:
        def _call_llm():
            from agents.llm_client import get_llm
            return get_llm(temperature=0.2, max_tokens=1600, profile_id=req.model_profile_id or None).invoke(prompt)

        response = await asyncio.wait_for(asyncio.to_thread(_call_llm), timeout=float(os.getenv("GENERAL_CHAT_TIMEOUT_SECONDS", "35")))
        usage = ((getattr(response, "response_metadata", {}) or {}).get("token_usage") or {})
        answer = response.content or ""
        source = "llm"
        estimated_cost = estimate_llm_cost_usd(usage, model_profile_id=req.model_profile_id)
        end_observation(generation, output={"answer": answer}, usage=usage, metadata={"estimated_cost_usd": estimated_cost})
        update_trace(trace, output={"answer": answer}, metadata={"source": source, "estimated_cost_usd": estimated_cost})
        flush_observability()
    except Exception as exc:
        usage = {}
        answer = f"这个问题看起来不是 Kubernetes/SRE 运维问题，我可以直接回答。但当前 LLM 网关调用失败：{type(exc).__name__}: {exc}"
        source = "fallback"
        estimated_cost = 0
        end_observation(generation, output={"answer": answer}, status_message=f"{type(exc).__name__}: {exc}", level="ERROR")
        update_trace(trace, output={"answer": answer}, metadata={"source": source, "fallback": True})
        flush_observability()
    return {
        "answer": answer,
        "postmortem": None,
        "raw": {
            "mode": "general_chat",
            "alert": {
                "alert_name": "GeneralConversation",
                "cluster": req.cluster,
                "cluster_id": req.cluster_id,
                "namespace": req.namespace,
                "summary": user_text,
            },
            "diagnosis": {
                "root_cause": "通用对话问题，不触发 Kubernetes 故障诊断。",
                "impact": "无集群变更影响",
                "confidence": 1,
                "risk_level": "none",
                "suggested_action": "answer",
                "immediate_actions": [],
                "proposed_changes": [],
                "diagnosis_metadata": {
                    "source": source,
                    "model": os.getenv("LLM_MODEL", ""),
                    "model_profile_id": req.model_profile_id or get_active_model_profile_id(),
                    "token_usage": usage,
                    "intent_router": intent or {},
                    "estimated_cost_usd": estimated_cost,
                    "langfuse_trace_id": getattr(trace, "id", ""),
                    "langfuse_session_id": f"chat-general:{req.cluster_id or req.cluster}:{req.namespace}",
                },
            },
            "decision": {
                "action": "answer",
                "dry_run": True,
                "require_human_approval": False,
                "source": "general_chat_router",
            },
        },
    }


def _attach_chat_intent(data: dict, intent: dict) -> dict:
    if not isinstance(data, dict):
        return data
    raw = data.setdefault("raw", {})
    if isinstance(raw, dict):
        raw["chat_intent"] = intent
        diagnosis = raw.setdefault("diagnosis", {})
        if isinstance(diagnosis, dict):
            metadata = diagnosis.setdefault("diagnosis_metadata", {})
            if isinstance(metadata, dict):
                metadata["intent_router"] = intent
    return data


async def _chat_response_data(req: ChatRequest, intent: dict | None = None) -> dict:
    intent = intent or await _route_chat_intent(req)
    if intent.get("mode") == "general":
        data = await _general_chat_response(req, intent)
    else:
        try:
            request_payload = req.model_dump()
            if req.model_profile_id:
                try:
                    request_payload["model_profile_override"] = select_model_profile(req.model_profile_id).to_dict(redact=False)
                except Exception as profile_exc:
                    request_payload["model_profile_error"] = f"{type(profile_exc).__name__}: {_redact_text(str(profile_exc))}"
            scope = _chat_scope(req)
            managed_cluster = next(
                (
                    item for item in CLUSTER_REGISTRY.list()
                    if (req.cluster_id or req.cluster) in {item.get("id"), item.get("name")}
                ),
                None,
            )
            if managed_cluster and scope.get("cluster_selected"):
                try:
                    raw_inventory = await asyncio.to_thread(CLUSTER_REGISTRY.inventory, managed_cluster["id"])
                    replica_owner: dict[str, tuple[str, str]] = {}
                    for replica in raw_inventory.get("replicasets") or []:
                        metadata = replica.get("metadata") or {}
                        owners = metadata.get("ownerReferences") or []
                        if owners:
                            replica_owner[str(metadata.get("name") or "")] = (
                                str(owners[0].get("kind") or "Deployment"),
                                str(owners[0].get("name") or ""),
                            )
                    pods = []
                    for item in raw_inventory.get("pods") or []:
                        pod = _normalize_k8s_pod(item, replica_owner)
                        pod.update({"cluster": managed_cluster["name"], "cluster_id": managed_cluster["id"]})
                        pods.append(pod)
                    selected = _select_chat_target_pod(pods, req)
                    if selected:
                        context_plan = {
                            "cluster": managed_cluster["name"],
                            "cluster_id": managed_cluster["id"],
                            "source": "kubeconfig",
                            "namespace": selected.get("namespace") or req.namespace,
                            "pod_name": selected.get("name"),
                            "target": f"{selected.get('workload_kind') or req.workload_type}/{selected.get('workload_name') or req.deployment}",
                            "evidence": {"pod": selected},
                            "changes": [],
                        }
                        deep = await _collect_plan_deep_evidence(context_plan)
                        request_payload["k8s_context"] = {
                            "source": "kubeconfig",
                            "cluster": managed_cluster["name"],
                            "cluster_id": managed_cluster["id"],
                            "pods": {"pods": pods[:40]},
                            "pod": deep.get("pod") or selected,
                            "events": {"events": deep.get("events", [])},
                            "logs": deep.get("logs", {}),
                            "workload": deep.get("workload", {}),
                            "diagnostics": deep,
                            "target_binding": {
                                "source": "operator_selected" if req.deployment or req.pod else "unhealthy_autotarget",
                                "target_id": req.target_id,
                                "workload_name": req.deployment,
                                "pod_name": req.pod,
                            },
                        }
                    elif req.deployment or req.pod:
                        request_payload["k8s_context"] = {
                            "source": "kubeconfig",
                            "cluster": managed_cluster["name"],
                            "cluster_id": managed_cluster["id"],
                            "pods": {"pods": []},
                            "target_binding_error": (
                                f"指定目标不存在或当前身份不可见：{req.workload_type}/{req.deployment}"
                                if req.deployment else f"指定 Pod 不存在或当前身份不可见：{req.pod}"
                            ),
                        }
                except Exception as context_exc:
                    request_payload["k8s_context_error"] = f"{type(context_exc).__name__}: {_redact_text(str(context_exc))}"
            elif _rancher_enabled() and scope.get("cluster_selected"):
                try:
                    pods, clusters, errors = await _rancher_pods_for_alert_scan(req.cluster_id or req.cluster, req.namespace or "all")
                    selected = _select_chat_target_pod(pods, req)
                    if selected:
                        context_plan = {
                            "cluster": selected.get("cluster") or req.cluster,
                            "cluster_id": selected.get("cluster_id") or req.cluster_id,
                            "source": "rancher",
                            "namespace": selected.get("namespace") or req.namespace,
                            "pod_name": selected.get("name"),
                            "target": f"{selected.get('workload_kind') or req.workload_type}/{selected.get('workload_name') or req.deployment}",
                            "evidence": {"pod": selected},
                            "changes": [],
                        }
                        deep = await _collect_plan_deep_evidence(context_plan)
                        request_payload["k8s_context"] = {
                            "source": "rancher",
                            "cluster": context_plan["cluster"],
                            "cluster_id": context_plan["cluster_id"],
                            "pods": {"pods": pods[:40]},
                            "pod": deep.get("pod") or selected,
                            "events": {"events": deep.get("events", [])},
                            "logs": deep.get("logs", {}),
                            "workload": deep.get("workload", {}),
                            "diagnostics": deep,
                            "collection_errors": errors,
                            "target_binding": {
                                "source": "operator_selected" if req.deployment or req.pod else "unhealthy_autotarget",
                                "target_id": req.target_id,
                                "workload_name": req.deployment,
                                "pod_name": req.pod,
                            },
                        }
                    elif req.deployment or req.pod:
                        request_payload["k8s_context"] = {
                            "source": "rancher",
                            "cluster": req.cluster,
                            "cluster_id": req.cluster_id,
                            "pods": {"pods": []},
                            "target_binding_error": (
                                f"指定目标不存在或当前身份不可见：{req.workload_type}/{req.deployment}"
                                if req.deployment else f"指定 Pod 不存在或当前身份不可见：{req.pod}"
                            ),
                            "collection_errors": errors,
                        }
                except Exception as context_exc:
                    request_payload["k8s_context_error"] = f"{type(context_exc).__name__}: {_redact_text(str(context_exc))}"
            elif _rancher_enabled():
                # “所有集群”不能回退到 API Pod 所在的本地集群。先跨 Rancher
                # 盘点，再把最需要下钻的真实 Pod 和它所属集群精确绑定到证据链。
                try:
                    pods, clusters, errors = await _rancher_pods_for_alert_scan("all", req.namespace or "all")
                    selected = _select_chat_target_pod(pods, req)
                    context: dict = {
                        "source": "rancher",
                        "pods": {"pods": pods[:40]},
                        "clusters_scanned": [
                            {"id": item.get("id"), "name": item.get("name")}
                            for item in clusters[:40]
                        ],
                        "collection_errors": errors,
                        "requested_scope": {"cluster": "all", "namespace": req.namespace or "all"},
                        "target_binding": {"source": "cross_cluster_unhealthy_autotarget"},
                    }
                    if selected:
                        context_plan = {
                            "cluster": selected.get("cluster"),
                            "cluster_id": selected.get("cluster_id"),
                            "source": "rancher",
                            "namespace": selected.get("namespace") or req.namespace,
                            "pod_name": selected.get("name"),
                            "target": f"{selected.get('workload_kind') or req.workload_type}/{selected.get('workload_name') or ''}",
                            "evidence": {"pod": selected},
                            "changes": [],
                        }
                        deep = await _collect_plan_deep_evidence(context_plan)
                        context.update({
                            "cluster": context_plan["cluster"],
                            "cluster_id": context_plan["cluster_id"],
                            "pod": deep.get("pod") or selected,
                            "events": {"events": deep.get("events", [])},
                            "logs": deep.get("logs", {}),
                            "workload": deep.get("workload", {}),
                            "diagnostics": deep,
                            "target_binding": {
                                "source": "cross_cluster_unhealthy_autotarget",
                                "cluster_id": context_plan["cluster_id"],
                                "namespace": context_plan["namespace"],
                                "pod_name": context_plan["pod_name"],
                                "workload_name": selected.get("workload_name") or "",
                            },
                        })
                        # Mutations generated from the evidence must target this
                        # concrete cluster, never the symbolic value "all".
                        request_payload["cluster"] = context_plan["cluster"]
                        request_payload["cluster_id"] = context_plan["cluster_id"]
                        request_payload["namespace"] = context_plan["namespace"]
                        request_payload["workload_type"] = selected.get("workload_kind") or req.workload_type
                    request_payload["k8s_context"] = context
                except Exception as context_exc:
                    request_payload["k8s_context"] = {
                        "source": "rancher",
                        "pods": {"pods": []},
                        "requested_scope": {"cluster": "all", "namespace": req.namespace or "all"},
                        "target_binding_error": "跨集群证据采集失败；不会回退到本地集群或生成变更。",
                    }
                    request_payload["k8s_context_error"] = f"{type(context_exc).__name__}: {_redact_text(str(context_exc))}"
            # 只扫描目录包元数据，命中后才加载对应 SKILL.md；匹配输入同时包含已采集的真实证据。
            request_payload["operator_skills"] = OPS_SKILL_REGISTRY.agent_context({
                "question": req.message,
                "cluster": req.cluster,
                "cluster_id": req.cluster_id,
                "namespace": req.namespace,
                "workload": req.deployment,
                "workload_type": req.workload_type,
                "evidence": request_payload.get("k8s_context") or {},
            })
            async with _client(120) as c:
                resp = await c.post(f"{SERVICES['adapter']}/chat", json=request_payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            # 依赖不可用时只返回可执行的取证计划，绝不编造根因或修复成功。
            data = _fallback_diagnosis_response(req)
            data["backend_error"] = str(e)
    data = _enforce_chat_target_binding(req, data)
    return _attach_operator_skills_to_chat(req, _attach_chat_intent(data, intent))


def _remember_chat_result(req: ChatRequest, data: dict, started_at: datetime):
    bounded_append(ALERT_HISTORY, {
        "id": str(uuid.uuid4())[:8],
        "type": "chat",
        "namespace": req.namespace,
        "deployment": req.deployment,
        "severity": req.severity,
        "message": req.message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "result": data.get("raw", {}) if isinstance(data, dict) else {},
    }, STORE_LIMIT)

    if isinstance(data, dict):
        _remember_graph_result(data.get("raw", {}))
        _record_llm_observation("sre_chat", req.model_dump(), data, started_at)


async def proxy_chat(req: ChatRequest):
    """代理到 Open WebUI Adapter（最终调用 SRE Graph）"""
    started_at = datetime.now(timezone.utc)
    intent = await _route_chat_intent(req)
    data = await _chat_response_data(req, intent)
    _remember_chat_result(req, data, started_at)
    return data


def _chat_stream_event(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


def _chat_answer_presentation_prompt(req: ChatRequest, data: dict) -> str:
    raw = data.get("raw") or {}
    diagnosis = raw.get("diagnosis") or {}
    decision = raw.get("decision") or {}
    plan = diagnosis.get("remediation_plan") or {}
    target = (raw.get("target_binding") or {}) if isinstance(raw, dict) else {}
    payload = {
        "user_question": _chat_user_text(req),
        "target": target or {
            "cluster": req.cluster,
            "namespace": req.namespace,
            "workload_type": req.workload_type,
            "workload_name": req.deployment,
            "pod_name": req.pod,
        },
        "root_cause": diagnosis.get("root_cause"),
        "impact": diagnosis.get("impact") or diagnosis.get("blast_radius"),
        "risk_level": diagnosis.get("risk_level"),
        "confidence": diagnosis.get("confidence"),
        "signals": diagnosis.get("signals") or [],
        "immediate_actions": diagnosis.get("immediate_actions") or [],
        "prevention": diagnosis.get("prevention") or [],
        "decision": {
            "action": decision.get("action"),
            "require_human_approval": decision.get("require_human_approval"),
            "reason": decision.get("reason"),
        },
        "plan": {
            "summary": plan.get("summary") or plan.get("reason"),
            "evidence_gap": plan.get("evidence_gap"),
            "steps": plan.get("steps") or [],
            "changes": plan.get("changes") or [],
            "success_criteria": plan.get("success_criteria") or [],
            "operator_skills": plan.get("operator_skills") or diagnosis.get("operator_skills") or [],
        },
    }
    return f"""你是企业生产环境的首席 SRE。请基于下面的真实诊断结果直接回答用户。

要求：
- 先用一两句话说清结论和当前指定目标，不要寒暄，不要复述问题。
- 只使用输入中的证据；证据不足时明确写“尚不能确认”，不得编造日志、指标或执行结果。
- 用自然、简洁的中文 Markdown。优先使用短段落和少量列表，不要输出 JSON，不要机械套用固定六段报告。
- 清楚区分“已经观察到”“推断”“建议执行”；不要声称尚未执行的动作已经完成。
- 有可执行计划时，简洁说明为什么选择它、会改什么、如何验证；具体审批按钮由界面承载，无需重复长篇安全声明。
- 不展示隐藏思维链。可以说明证据来源和判断依据。

诊断数据：
{json.dumps(_redact_sensitive(payload), ensure_ascii=False, default=str)[:18000]}
"""


async def proxy_chat_stream(req: ChatRequest):
    async def event_iter():
        started_at = datetime.now(timezone.utc)
        yield _chat_stream_event({"type": "status", "stage": "understanding", "message": "理解问题并锁定操作目标"})
        intent = await _route_chat_intent(req)
        route_label = (
            f"LLM 意图路由：{'SRE 运维诊断' if intent.get('mode') == 'sre' else '通用问答'}"
            f"（{intent.get('source', 'router')}，置信度 {float(intent.get('confidence') or 0):.2f}）"
        )
        yield _chat_stream_event({"type": "status", "stage": "routed", "message": route_label})
        yield _chat_stream_event({
            "type": "status",
            "stage": "collecting" if intent.get("mode") == "sre" else "answering",
            "message": "读取指定对象的日志、Events、配置与依赖证据" if intent.get("mode") == "sre" else "组织回答",
        })
        try:
            data = await _chat_response_data(req, intent)
            yield _chat_stream_event({
                "type": "status",
                "stage": "evidence_ready",
                "message": "证据与候选方案已完成校验，正在生成回答",
            })
            collected: list[str] = []
            try:
                from agents.llm_client import stream_chat_text
                async for token in stream_chat_text(
                    _chat_answer_presentation_prompt(req, data),
                    profile_id=req.model_profile_id or None,
                    temperature=0.1,
                    max_tokens=int(os.getenv("LLM_CHAT_PRESENTATION_MAX_TOKENS", "1600")),
                ):
                    collected.append(token)
                    yield _chat_stream_event({"type": "delta", "text": token})
            except Exception as stream_exc:
                fallback = data.get("answer") or json.dumps(data, ensure_ascii=False)
                if not collected:
                    collected.append(fallback)
                    yield _chat_stream_event({"type": "delta", "text": fallback})
                else:
                    suffix = "\n\n> 流式连接提前结束，已保留当前已生成内容；执行计划仍以页面下方结构化数据为准。"
                    collected.append(suffix)
                    yield _chat_stream_event({"type": "delta", "text": suffix})
                raw = data.setdefault("raw", {})
                if isinstance(raw, dict):
                    raw["presentation_stream_error"] = f"{type(stream_exc).__name__}: {_redact_text(str(stream_exc))}"
            if collected:
                data["answer"] = "".join(collected)
            raw = data.setdefault("raw", {})
            if isinstance(raw, dict):
                raw["streaming"] = {"mode": "upstream_token_stream", "target_bound": bool(req.deployment or req.pod)}
            _remember_chat_result(req, data, started_at)
            yield _chat_stream_event({"type": "final", "data": data})
        except Exception as exc:
            yield _chat_stream_event({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(event_iter(), media_type="application/x-ndjson")


async def rank_chat_risks(req: ChatRiskRankRequest):
    """Re-rank the visible risk queue without turning ranking into a chat turn."""
    risks = []
    for raw in (req.risks or [])[:12]:
        if not isinstance(raw, dict) or not str(raw.get("key") or "").strip():
            continue
        risks.append({
            "key": str(raw.get("key"))[:240],
            "type": str(raw.get("type") or "workload")[:40],
            "cluster": str(raw.get("cluster") or req.cluster)[:160],
            "namespace": str(raw.get("namespace") or req.namespace)[:160],
            "kind": str(raw.get("kind") or "Workload")[:60],
            "name": str(raw.get("name") or "")[:240],
            "severity": str(raw.get("severity") or "P2")[:20],
            "ready_replicas": raw.get("ready_replicas"),
            "replicas": raw.get("replicas"),
            "restart_count": int(raw.get("restart_count") or 0),
            "reasons": [str(item)[:400] for item in (raw.get("reasons") or [])[:5]],
            "pods": [str(item)[:240] for item in (raw.get("pods") or [])[:8]],
            "baseline_score": float(raw.get("score") or 0),
        })
    if not risks:
        return {"status": "ok", "ordered_keys": [], "rationales": {}, "source": "empty"}

    baseline = sorted(risks, key=lambda item: (-item["baseline_score"], item["name"]))
    source = "deterministic_fallback"
    rationales = {
        item["key"]: "依据严重级别、不可用副本、重启次数和现有异常证据排序。"
        for item in baseline
    }
    ordered_keys = [item["key"] for item in baseline]
    error = ""
    try:
        def call_ranker() -> dict:
            from agents.llm_client import get_llm
            prompt = (
                "你是生产值班 SRE 的风险队列排序器。只对输入中的真实对象重新排序，不得增加、删除或改名。"
                "排序优先级依次考虑：当前业务不可用程度、影响副本比例、P0/P1、持续重启/调度失败、存储网络等阻断性根因、"
                "潜在爆炸半径；单纯高重启次数不能压过完全不可用的核心 Workload。"
                "只返回 JSON：{ordered_keys:[...],rationales:{key:'一句运维人员能看懂的理由'}}。\n"
                f"范围={req.cluster}/{req.namespace}\n风险对象={json.dumps(_redact_sensitive(risks), ensure_ascii=False)[:14000]}"
            )
            response = get_llm(temperature=0.0, max_tokens=900, profile_id=req.model_profile_id or None).invoke(prompt)
            return _extract_json_object(getattr(response, "content", str(response)))

        ranked = await asyncio.wait_for(
            asyncio.to_thread(call_ranker),
            timeout=float(os.getenv("CHAT_RISK_RANK_TIMEOUT_SECONDS", "15")),
        )
        valid = {item["key"] for item in risks}
        candidate = [str(key) for key in ranked.get("ordered_keys") or [] if str(key) in valid]
        ordered_keys = list(dict.fromkeys(candidate + [item["key"] for item in baseline]))
        returned_reasons = ranked.get("rationales") or {}
        if isinstance(returned_reasons, dict):
            for key in valid:
                if key in returned_reasons:
                    rationales[key] = _clip_text(str(returned_reasons[key]), 320)
        source = "llm_constrained_ranking"
    except Exception as exc:
        error = f"{type(exc).__name__}: {_redact_text(str(exc))}"
    return {
        "status": "ok",
        "ordered_keys": ordered_keys,
        "rationales": rationales,
        "source": source,
        "error": error or None,
        "criteria": ["业务不可用程度", "影响副本比例", "严重级别", "持续性", "阻断性根因", "爆炸半径"],
        "ranked_at": datetime.now(timezone.utc).isoformat(),
    }


APP_KNOWLEDGE_DOCS = [
    {
        "id": "sre-chat",
        "title": "SRE 对话",
        "content": "SRE 对话是核心入口。用户描述现象后，系统会先判断是否为运维问题，再采集 Rancher/Kubernetes 上下文、Pod 日志、Events、Workload 模板、Service/Endpoint、PVC/PV、节点状态和 CMDB 拓扑，最终输出诊断结论、证据、可确认执行计划。非运维问题会切换到普通 LLM 回答。",
        "tags": ["chat", "sre", "stream", "diagnosis"],
    },
    {
        "id": "inspection",
        "title": "AI 巡检",
        "content": "AI 巡检可按所有集群、指定集群或 namespace 扫描问题 Pod/Workload。生产模式会额外扫描健康对象中的未来风险，例如单副本、资源边界缺失、探针缺失、可变镜像 tag、高权限 securityContext 和 hostNetwork。",
        "tags": ["inspection", "production", "risk", "rancher"],
    },
    {
        "id": "topology",
        "title": "拓扑影响",
        "content": "拓扑影响模块从 CMDB、Rancher、Service、Pod、Workload、Kafka/ELK 等关系形成数据流图，用 ChangeSensitiveBlastRadius 算法计算影响等级、关键路径、放大系数和变更门禁依据。3D 拓扑用于看清跨集群数据流和依赖传播。",
        "tags": ["topology", "cmdb", "blast-radius", "kafka"],
    },
    {
        "id": "effectiveness",
        "title": "运维成效",
        "content": "运维成效模块记录每次巡检、模型测评和运维执行结果，展示模型成功率、变更次数、恢复 Pod、风险降低率和审计记录，用于量化不同模型的真实运维能力。",
        "tags": ["effectiveness", "model", "audit"],
    },
]


OPS_KNOWLEDGE_DOCS = [
    {
        "id": "crashloop",
        "title": "CrashLoop/OOM/启动失败",
        "content": "先看 previous logs、当前 logs、Events、lastState、退出码、OOMKilled、探针失败和最近配置变更。OOM 可调整 resources；慢启动/探针失败可加 startupProbe；配置/Secret/PVC 错误要修模板；代码异常不自动改业务镜像。",
        "tags": ["crashloop", "oom", "probe", "logs"],
    },
    {
        "id": "image-pull",
        "title": "镜像拉取失败",
        "content": "检查 ErrImagePull/ImagePullBackOff Events、镜像 tag、registry DNS/网络、仓库鉴权、imagePullSecret。只有配置了 DEFAULT_IMAGE_PULL_SECRET 且证据显示鉴权失败时，系统才自动生成 imagePullSecrets patch。",
        "tags": ["image", "registry", "secret"],
    },
    {
        "id": "storage",
        "title": "存储和配置挂载",
        "content": "检查 FailedMount、FailedAttachVolume、PVC/PV/StorageClass、CSI、ConfigMap/Secret、实际失败路径、运行用户、volumeMount 和最近模板变更。目录权限类问题必须动态匹配 Skill 决定 YAML/命令；底层 NFS/Ceph/宿主机权限由平台侧处理。",
        "tags": ["storage", "pvc", "configmap", "secret"],
    },
    {
        "id": "network",
        "title": "网络和依赖",
        "content": "检查 Service selector、EndpointSlice、DNS、NetworkPolicy、Ingress、Service Mesh、Kafka/Redis/MySQL 等依赖链路。没有充分证据时不自动改网络策略，只给验证步骤和审批建议。",
        "tags": ["network", "service", "dns", "kafka"],
    },
    {
        "id": "production-readiness",
        "title": "生产模式风险",
        "content": "健康对象也要查未来风险：单副本、缺 requests/limits、缺探针、latest 镜像、高权限 securityContext、hostNetwork、缺 PDB/HPA 或拓扑分散策略。可安全自动 patch 的项进入确认流程，业务语义项需要人工补充参数。",
        "tags": ["production", "security", "resources", "risk"],
    },
]


def _knowledge_domain(value: str, title: str = "", content: str = "", tags: list[str] | None = None) -> str:
    requested = str(value or "").strip().lower()
    if requested in {"app", "guide", "assistant", "product"}:
        return "app"
    if requested in {"ops", "operation", "runbook", "sre"}:
        return "ops"
    text = " ".join([title or "", content or "", " ".join(tags or [])]).lower()
    ops_keywords = [
        "pod", "k8s", "kubernetes", "namespace", "workload", "deployment", "statefulset",
        "crashloop", "oom", "pvc", "probe", "ingress", "service", "node", "rbac", "prometheus",
        "rancher", "cmdb", "kafka", "elk", "运维", "巡检", "告警", "修复", "故障", "集群",
        "存储", "网络", "镜像", "探针", "权限",
    ]
    return "ops" if any(keyword in text for keyword in ops_keywords) else "app"


def _knowledge_tags(tags: list[str] | str | None, title: str, content: str) -> list[str]:
    if isinstance(tags, str):
        raw_tags = [item.strip() for item in re.split(r"[,，;；\s]+", tags) if item.strip()]
    elif isinstance(tags, list):
        raw_tags = [str(item).strip() for item in tags if str(item).strip()]
    else:
        raw_tags = []
    text = f"{title}\n{content}".lower()
    auto_terms = [
        "sre", "rancher", "prometheus", "cmdb", "kafka", "elk", "pod", "workload", "deployment",
        "statefulset", "crashloop", "oom", "pvc", "rbac", "security", "network", "storage",
        "巡检", "拓扑", "模型", "知识库", "自动运维", "修复", "权限", "告警",
    ]
    raw_tags.extend(term for term in auto_terms if term.lower() in text)
    unique: list[str] = []
    for item in raw_tags:
        cleaned = item[:40]
        if cleaned and cleaned not in unique:
            unique.append(cleaned)
    return unique[:18]


def _knowledge_store() -> dict:
    with KNOWLEDGE_LOCK:
        if not KNOWLEDGE_STORE_PATH.exists():
            return {"version": 1, "documents": []}
        try:
            data = json.loads(KNOWLEDGE_STORE_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"version": 1, "documents": []}
            docs = data.get("documents")
            if not isinstance(docs, list):
                data["documents"] = []
            return data
        except Exception:
            return {"version": 1, "documents": []}


def _write_knowledge_store(data: dict) -> None:
    if not KNOWLEDGE_RUNTIME_WRITE_ENABLED:
        raise RuntimeError("KNOWLEDGE_RUNTIME_WRITE_ENABLED=false")
    with KNOWLEDGE_LOCK:
        KNOWLEDGE_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, **(data or {})}
        tmp = KNOWLEDGE_STORE_PATH.with_suffix(KNOWLEDGE_STORE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(KNOWLEDGE_STORE_PATH)


def _split_knowledge_content(content: str) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", (content or "").strip())
    if not text:
        return []
    parts = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for part in parts or [text]:
        if len(part) > KNOWLEDGE_CHUNK_CHARS:
            for idx in range(0, len(part), max(1, KNOWLEDGE_CHUNK_CHARS - KNOWLEDGE_CHUNK_OVERLAP)):
                segment = part[idx:idx + KNOWLEDGE_CHUNK_CHARS].strip()
                if segment:
                    chunks.append(segment)
            continue
        if current and len(current) + len(part) + 2 > KNOWLEDGE_CHUNK_CHARS:
            chunks.append(current.strip())
            tail = current[-KNOWLEDGE_CHUNK_OVERLAP:].strip() if KNOWLEDGE_CHUNK_OVERLAP else ""
            current = f"{tail}\n{part}".strip() if tail else part
        else:
            current = f"{current}\n\n{part}".strip() if current else part
    if current:
        chunks.append(current.strip())
    return chunks[:80]


def _embedding_enabled() -> bool:
    return KNOWLEDGE_EMBEDDING_ENABLED and bool(os.getenv("EMBEDDING_GATEWAY_BASE") or os.getenv("EMBEDDING_API_BASE") or os.getenv("LLM_API_BASE"))


def _embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    from agents.llm_client import get_embeddings
    vectors = get_embeddings().embed_documents(texts)
    normalized: list[list[float]] = []
    for vector in vectors:
        normalized.append([float(item) for item in vector])
    return normalized


async def _embed_texts_async(texts: list[str]) -> list[list[float]]:
    return await asyncio.to_thread(_embed_texts, texts)


def _cosine_similarity(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _runtime_knowledge_documents(domain: str = "all") -> list[dict]:
    selected = str(domain or "all").lower()
    docs = []
    for doc in _knowledge_store().get("documents", []):
        if not isinstance(doc, dict):
            continue
        doc_domain = str(doc.get("domain") or "app").lower()
        if selected not in {"all", "*"} and doc_domain != _knowledge_domain(selected):
            continue
        clean = {key: value for key, value in doc.items() if key != "chunks"}
        clean["chunks"] = [
            {key: value for key, value in chunk.items() if key != "embedding"}
            for chunk in doc.get("chunks", []) if isinstance(chunk, dict)
        ]
        docs.append(clean)
    return sorted(docs, key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)


def _runtime_knowledge_chunks(domain: str) -> list[dict]:
    rows = []
    for doc in _knowledge_store().get("documents", []):
        if not isinstance(doc, dict):
            continue
        doc_domain = str(doc.get("domain") or "app").lower()
        selected = _knowledge_domain(domain)
        if doc_domain != selected:
            continue
        chunks = doc.get("chunks") if isinstance(doc.get("chunks"), list) else []
        if not chunks:
            chunks = [{"id": f"{doc.get('id')}:content", "text": doc.get("content", ""), "embedding": None, "embedding_status": doc.get("embedding_status", "none")}]
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            text = str(chunk.get("text") or "").strip()
            if not text:
                continue
            rows.append({
                "id": chunk.get("id") or f"{doc.get('id')}:{len(rows)}",
                "document_id": doc.get("id"),
                "title": doc.get("title") or "未命名知识",
                "content": text,
                "tags": doc.get("tags") or [],
                "domain": doc_domain,
                "source": doc.get("source") or "ui",
                "runtime": True,
                "embedding": chunk.get("embedding"),
                "embedding_status": chunk.get("embedding_status") or doc.get("embedding_status") or "none",
                "created_at": doc.get("created_at"),
                "updated_at": doc.get("updated_at"),
            })
    return rows


def _builtin_knowledge_docs(domain: str) -> list[dict]:
    selected = _knowledge_domain(domain)
    docs = APP_KNOWLEDGE_DOCS if selected == "app" else OPS_KNOWLEDGE_DOCS
    docs = [{**doc, "domain": selected, "source": "builtin", "runtime": False} for doc in docs]
    if selected == "ops":
        action_docs = [{
            "id": f"action-{item['id']}",
            "title": f"动作词表：{item['id']}",
            "content": f"{item.get('description', '')} 风险级别 {item.get('risk')}，自动允许 {item.get('auto_allowed')}，回滚方式：{item.get('rollback')}",
            "tags": ["action", item["id"], item.get("risk", "")],
            "domain": "ops",
            "source": "action_catalog",
            "runtime": False,
        } for item in action_catalog_payload()]
        docs = docs + action_docs
    return docs


def _knowledge_docs(domain: str) -> list[dict]:
    return _builtin_knowledge_docs(domain) + [
        {key: value for key, value in chunk.items() if key != "embedding"}
        for chunk in _runtime_knowledge_chunks(domain)
    ]


def _knowledge_keyword_scores(question: str, docs: list[dict], limit: int = 5) -> list[dict]:
    lowered_question = str(question or "").lower()
    tokens = [x for x in re.split(r"[\s,，。；;:/\\|()\[\]{}<>]+", lowered_question) if x]
    zh_terms = [
        "生产模式", "单副本", "巡检", "拓扑", "爆炸半径", "模型", "知识库", "自动运维",
        "修复", "权限", "rbac", "crashloop", "oom", "镜像", "探针", "存储", "pvc",
        "网络", "kafka", "资源", "安全", "部署", "workload", "pod",
    ]
    tokens.extend(term for term in zh_terms if term.lower() in lowered_question and term.lower() not in tokens)
    scored = []
    for doc in docs:
        haystack = " ".join([doc.get("title", ""), doc.get("content", ""), " ".join(doc.get("tags") or [])]).lower()
        score = sum(3 if token in str(doc.get("title", "")).lower() else 1 for token in tokens if token and token in haystack)
        if score or not tokens:
            clean = {key: value for key, value in doc.items() if key != "embedding"}
            clean["score"] = score
            clean["retrieval"] = "keyword"
            scored.append((score, clean))
    scored.sort(key=lambda item: (-item[0], item[1].get("id", "")))
    return [doc for _, doc in scored[:limit]]


async def _retrieve_knowledge(question: str, domain: str, limit: int = 5, use_vector: bool = True) -> tuple[list[dict], str]:
    builtin_docs = _builtin_knowledge_docs(domain)
    runtime_chunks = _runtime_knowledge_chunks(domain)
    all_docs = builtin_docs + runtime_chunks
    vector_docs: list[dict] = []
    if use_vector and _embedding_enabled() and runtime_chunks:
        try:
            query_vector = (await _embed_texts_async([question]))[0]
            for doc in runtime_chunks:
                similarity = _cosine_similarity(query_vector, doc.get("embedding"))
                if similarity > 0:
                    clean = {key: value for key, value in doc.items() if key != "embedding"}
                    clean["score"] = round(similarity, 4)
                    clean["retrieval"] = "vector"
                    vector_docs.append(clean)
            vector_docs.sort(key=lambda item: (-float(item.get("score") or 0), item.get("id", "")))
        except Exception as exc:
            keyword = _knowledge_keyword_scores(question, all_docs, limit)
            for item in keyword:
                item["embedding_error"] = f"{type(exc).__name__}: {_redact_text(str(exc))}"
            return keyword or _knowledge_docs(domain)[:limit], "keyword_fallback"
    keyword_docs = _knowledge_keyword_scores(question, all_docs, limit)
    merged: list[dict] = []
    seen: set[str] = set()
    for doc in vector_docs[:limit] + keyword_docs:
        doc_id = str(doc.get("id"))
        if doc_id and doc_id not in seen:
            seen.add(doc_id)
            merged.append(doc)
        if len(merged) >= limit:
            break
    if merged:
        return merged, "vector_rag" if vector_docs else "keyword_rag"
    return _knowledge_docs(domain)[:limit], "keyword_rag"


def _fallback_knowledge_answer(question: str, docs: list[dict], include_principle: bool) -> str:
    if not docs:
        return "我没有检索到相关知识。建议先说明你在哪个模块遇到问题，或描述 Pod/Workload 的 namespace、名称和错误现象。"
    lines = ["可以这样做："]
    for doc in docs[:3]:
        lines.append(f"- {doc['title']}：{doc['content']}")
    if include_principle:
        lines.append("原理简述：系统先用关键词/标签检索知识片段，再把片段和用户问题交给当前模型生成回答；模型不可用时直接返回检索摘要。")
    return "\n".join(lines)


async def knowledge_sources():
    runtime_docs = _runtime_knowledge_documents("all")
    runtime_chunks = sum(len(item.get("chunks") or []) for item in runtime_docs)
    return {
        "status": "ok",
        "embedding": {
            "enabled": _embedding_enabled(),
            "model": os.getenv("EMBEDDING_MODEL", ""),
            "base_url": os.getenv("EMBEDDING_GATEWAY_BASE") or os.getenv("EMBEDDING_API_BASE", ""),
            "store_path": str(KNOWLEDGE_STORE_PATH),
            "runtime_write_enabled": KNOWLEDGE_RUNTIME_WRITE_ENABLED,
            "runtime_documents": len(runtime_docs),
            "runtime_chunks": runtime_chunks,
        },
        "domains": [
            {"id": "app", "name": "应用使用知识库", "documents": len(APP_KNOWLEDGE_DOCS) + len([item for item in runtime_docs if item.get("domain") == "app"])},
            {"id": "ops", "name": "运维 Runbook 知识库", "documents": len(OPS_KNOWLEDGE_DOCS) + len(action_catalog_payload()) + len([item for item in runtime_docs if item.get("domain") == "ops"])},
        ],
    }


async def list_knowledge_documents(domain: str = "all"):
    return {
        "status": "ok",
        "documents": _runtime_knowledge_documents(domain),
        "store_path": str(KNOWLEDGE_STORE_PATH),
        "embedding_enabled": _embedding_enabled(),
    }


async def add_knowledge_document(req: KnowledgeDocumentRequest):
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    if len(content.encode("utf-8")) > KNOWLEDGE_MAX_EXTRACTED_BYTES:
        raise HTTPException(status_code=413, detail="knowledge document is too large")
    title = (req.title or "").strip() or content.splitlines()[0][:80] or "未命名知识"
    tags = _knowledge_tags(req.tags, title, content)
    domain = _knowledge_domain(req.domain, title, content, tags)
    chunks_text = _split_knowledge_content(content)
    if not chunks_text:
        raise HTTPException(status_code=400, detail="content has no indexable text")
    now = datetime.now(timezone.utc).isoformat()
    doc_id = hashlib.sha256(f"{domain}\n{title}\n{content}".encode("utf-8")).hexdigest()[:16]
    chunks = []
    embedding_status = "not_requested"
    embedding_error = ""
    if req.embed and _embedding_enabled():
        try:
            vectors = await _embed_texts_async(chunks_text)
            for index, (text, vector) in enumerate(zip(chunks_text, vectors)):
                chunks.append({
                    "id": f"{doc_id}:{index}",
                    "text": text,
                    "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                    "embedding": vector,
                    "embedding_status": "ready",
                })
            embedding_status = "ready"
        except Exception as exc:
            embedding_status = "failed"
            embedding_error = f"{type(exc).__name__}: {_redact_text(str(exc))}"
    if not chunks:
        chunks = [{
            "id": f"{doc_id}:{index}",
            "text": text,
            "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            "embedding_status": embedding_status,
        } for index, text in enumerate(chunks_text)]
    document = {
        "id": doc_id,
        "domain": domain,
        "title": title[:160],
        "content": content,
        "tags": tags,
        "source": (req.source or "ui")[:80],
        "document_type": (req.document_type or "text")[:40],
        "embedding_status": embedding_status,
        "embedding_error": embedding_error,
        "embedding_model": os.getenv("EMBEDDING_MODEL", ""),
        "chunk_count": len(chunks),
        "created_at": now,
        "updated_at": now,
        "chunks": chunks,
    }
    store = _knowledge_store()
    docs = [item for item in store.get("documents", []) if isinstance(item, dict) and item.get("id") != doc_id]
    docs.insert(0, document)
    store["documents"] = docs[:int(os.getenv("KNOWLEDGE_MAX_DOCUMENTS", "500"))]
    _write_knowledge_store(store)
    clean_doc = {key: value for key, value in document.items() if key != "chunks"}
    clean_doc["chunks"] = [{key: value for key, value in chunk.items() if key != "embedding"} for chunk in chunks]
    return {
        "status": "ok" if embedding_status in {"ready", "not_requested"} else "degraded",
        "document": clean_doc,
        "message": "知识已添加并完成向量化" if embedding_status == "ready" else "知识已添加，当前使用关键词兜底检索",
    }


async def upload_knowledge_document(
    file: UploadFile = File(...),
    domain: str = Form("auto"),
    title: str = Form(""),
    tags: str = Form(""),
    embed: bool = Form(True),
):
    filename = Path(file.filename or "uploaded-document").name
    data = await file.read(KNOWLEDGE_MAX_UPLOAD_BYTES + 1)
    await file.close()
    if len(data) > KNOWLEDGE_MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"文件超过 {KNOWLEDGE_MAX_UPLOAD_BYTES // 1024 // 1024} MiB 限制")
    content, document_type = await asyncio.to_thread(extract_knowledge_file, data, filename)
    result = await add_knowledge_document(KnowledgeDocumentRequest(
        title=(title or Path(filename).stem)[:160],
        content=content,
        domain=domain,
        tags=tags,
        source=f"upload:{filename}"[:80],
        document_type=document_type,
        embed=embed,
    ))
    result["filename"] = filename
    result["extracted_characters"] = len(content)
    result["message"] = f"{filename} 已抽取 {len(content)} 个字符；{result.get('message', '已加入知识库')}"
    return result


async def delete_knowledge_document(document_id: str):
    store = _knowledge_store()
    docs = store.get("documents", [])
    kept = [item for item in docs if not (isinstance(item, dict) and str(item.get("id")) == document_id)]
    if len(kept) == len(docs):
        raise HTTPException(status_code=404, detail="knowledge document not found")
    store["documents"] = kept
    _write_knowledge_store(store)
    return {"status": "ok", "deleted": document_id}


async def reindex_knowledge(req: KnowledgeReindexRequest):
    if not _embedding_enabled():
        raise HTTPException(status_code=400, detail="embedding gateway is not configured")
    store = _knowledge_store()
    changed = 0
    failed = 0
    for doc in store.get("documents", []):
        if not isinstance(doc, dict):
            continue
        if req.document_id and str(doc.get("id")) != req.document_id:
            continue
        if str(req.domain or "all").lower() not in {"all", "*"} and str(doc.get("domain")) != _knowledge_domain(req.domain):
            continue
        chunks = doc.get("chunks") if isinstance(doc.get("chunks"), list) else []
        needs = req.force or any(not isinstance(chunk, dict) or not chunk.get("embedding") for chunk in chunks)
        if not needs:
            continue
        texts = [str(chunk.get("text") or "").strip() for chunk in chunks if isinstance(chunk, dict) and str(chunk.get("text") or "").strip()]
        if not texts:
            continue
        try:
            vectors = await _embed_texts_async(texts)
            vector_index = 0
            for chunk in chunks:
                if not isinstance(chunk, dict) or not str(chunk.get("text") or "").strip():
                    continue
                chunk["embedding"] = vectors[vector_index]
                chunk["embedding_status"] = "ready"
                vector_index += 1
            doc["embedding_status"] = "ready"
            doc["embedding_error"] = ""
            doc["embedding_model"] = os.getenv("EMBEDDING_MODEL", "")
            doc["updated_at"] = datetime.now(timezone.utc).isoformat()
            changed += 1
        except Exception as exc:
            doc["embedding_status"] = "failed"
            doc["embedding_error"] = f"{type(exc).__name__}: {_redact_text(str(exc))}"
            failed += 1
    _write_knowledge_store(store)
    return {"status": "ok" if not failed else "degraded", "reindexed": changed, "failed": failed}


async def ask_knowledge(req: KnowledgeAskRequest):
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    docs, retrieval_source = await _retrieve_knowledge(question, req.domain, use_vector=req.use_vector)
    prompt = f"""你是 Flawless 的产品使用与运维知识库助手。请基于检索片段回答用户问题。
要求：
- 中文，简洁，直接告诉用户怎么做。
- 如果用户问原理，才解释原理；否则只给操作步骤。
- 不要编造项目不存在的按钮或权限。

用户问题：
{question}

检索片段：
{json.dumps(docs, ensure_ascii=False)}
"""
    try:
        def _call_llm():
            from agents.llm_client import get_llm
            return get_llm(temperature=0.15, max_tokens=1100, profile_id=req.model_profile_id or None).invoke(prompt)

        response = await asyncio.wait_for(asyncio.to_thread(_call_llm), timeout=18)
        answer = getattr(response, "content", str(response))
        source = retrieval_source
    except Exception as exc:
        answer = _fallback_knowledge_answer(question, docs, req.include_principle)
        source = f"{retrieval_source}_fallback"
        return {
            "status": "degraded",
            "source": source,
            "answer": answer,
            "citations": docs,
            "error": f"{type(exc).__name__}: {_redact_text(str(exc))}",
        }
    return {"status": "ok", "source": source, "answer": answer, "citations": docs}


async def proxy_llm_health(profile_id: str = ""):
    try:
        async with _client(90) as c:
            resp = await c.get(f"{SERVICES['adapter']}/llm/health", params={"profile_id": profile_id} if profile_id else None)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        return {"status": "failed", "error": f"{type(e).__name__}: {e}"}


async def aiops_status():
    health_data = await health()
    llm_data = await proxy_llm_health()
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "control_plane": {
            "all_agents_healthy": health_data.get("all_healthy", False),
            "services": health_data.get("services", {}),
            "llm": llm_data,
        },
        "operations": {
            "alerts_total": len(ALERT_HISTORY),
            "incidents_total": len(INCIDENTS_STORE),
            "incidents_open": sum(1 for i in INCIDENTS_STORE if i.get("status") == "open"),
            "postmortems_total": len(POSTMORTEMS_STORE),
        },
    }


async def platform_resilience():
    health_data = await health()
    decision = build_self_heal_decision(health_data, PLATFORM_LAST_SELF_HEAL_AT)
    return {
        "status": "ok",
        "runtime": {
            "request_bulkhead": REQUEST_BULKHEAD.snapshot(),
            "outbound_bulkhead": OUTBOUND_BULKHEAD.snapshot(),
            "rancher_cache": RANCHER_CACHE.snapshot(),
            "rancher_inventory_cache": RANCHER_INVENTORY_CACHE.snapshot(),
            "cmdb_topology_cache": CMDB_TOPOLOGY_CACHE.snapshot(),
            "prometheus_summary_cache": PROMETHEUS_SUMMARY_CACHE.snapshot(),
            "store_limit": STORE_LIMIT,
            "stores": {
                "alerts": len(ALERT_HISTORY),
                "incidents": len(INCIDENTS_STORE),
                "postmortems": len(POSTMORTEMS_STORE),
                "llm_observability": len(LLM_OBSERVABILITY_STORE),
            },
        },
        "health": health_data,
        "self_heal": decision,
    }


async def platform_self_heal_status():
    health_data = await health()
    return build_self_heal_decision(health_data, PLATFORM_LAST_SELF_HEAL_AT)


async def platform_self_heal_run(request: Request):
    global PLATFORM_LAST_SELF_HEAL_AT
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    confirm = bool((payload or {}).get("confirm", False))
    health_data = await health()
    decision = build_self_heal_decision(health_data, PLATFORM_LAST_SELF_HEAL_AT)
    if not decision.get("self_heal_enabled"):
        return {
            "status": "blocked",
            "message": "PLATFORM_SELF_HEAL_ENABLED is false; only a plan is returned.",
            "decision": decision,
        }
    if not confirm:
        return {
            "status": "requires_confirmation",
            "message": "Self-healing requires confirm=true.",
            "decision": decision,
        }
    if decision.get("status") not in {"repairable"}:
        return {
            "status": "skipped",
            "message": "No repairable platform fault or cooldown is active.",
            "decision": decision,
        }
    changes = []
    for action in decision.get("actions", []):
        action_type = action.get("type")
        if action_type == "restart":
            changes.append({
                "type": "restart",
                "namespace": action.get("namespace", "k8s-agent"),
                "workload_type": action.get("workload_type", "Deployment"),
                "workload_name": action.get("workload_name", "flawless"),
                "reason": action.get("reason", "平台自修复滚动重启"),
                "patch": {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "<now>"}}}}},
            })
        elif action_type in {"patch_workload", "patch", "scale_out"}:
            changes.append({
                "type": "patch_workload" if action_type in {"patch_workload", "patch"} else "scale_out",
                "namespace": action.get("namespace", "k8s-agent"),
                "workload_type": action.get("workload_type", "Deployment"),
                "workload_name": action.get("workload_name", "flawless"),
                "replicas": action.get("replicas"),
                "reason": action.get("reason", "平台自修复受控变更"),
                "patch": action.get("patch") or ({"spec": {"replicas": int(action.get("replicas") or 2)}} if action_type == "scale_out" else {}),
            })
    if not changes:
        return {"status": "skipped", "message": "No executable platform self-heal action generated.", "decision": decision}
    plan = {
        "id": f"platform-self-heal-{uuid.uuid4().hex[:8]}",
        "title": "平台自修复执行计划",
        "cluster": "local-cluster",
        "cluster_id": "local",
        "source": "mcp",
        "namespace": changes[0].get("namespace", "k8s-agent"),
        "target": f"{changes[0].get('workload_type', 'Deployment')}/{changes[0].get('workload_name', 'flawless')}",
        "steps": [
            {"title": "检查平台 Agent 健康", "description": "读取 /api/health 与服务探针结果，确认故障服务。", "status": "pending"},
            {"title": "变更风险门禁", "description": "评估本次平台自修复是否会影响控制面可用性。", "status": "pending"},
            {"title": "执行自修复变更", "description": "根据策略执行 restart/patch/scale，并验证服务恢复。", "status": "pending"},
        ],
        "changes": changes,
        "requires_confirmation": True,
        "summary": decision.get("message", "平台自修复"),
    }
    result = await _enqueue_ops_job(
        plan,
        str((payload or {}).get("approved_by") or "platform-operator"),
        autonomous=False,
        confirmed=True,
    )
    PLATFORM_LAST_SELF_HEAL_AT = time.time()
    return {
        "status": result.get("status", "queued"),
        "message": "平台自修复计划已进入统一运维队列；每项真实变更仍需操作员逐步确认。",
        "reason": (payload or {}).get("reason", ""),
        "decision": decision,
        "result": result,
    }


async def model_registry():
    return registry_payload()


async def save_model_profile(req: ModelProfileUpsertRequest):
    try:
        return {"status": "ok", "profile": upsert_model_profile(req.model_dump())}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {_redact_text(str(exc))}") from exc


async def activate_model_profile(req: ModelProfileActiveRequest):
    try:
        return {"status": "ok", "profile": set_active_model_profile(req.profile_id)}
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"{type(exc).__name__}: {_redact_text(str(exc))}") from exc


async def remove_model_profile(profile_id: str):
    try:
        return delete_model_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="model profile not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {_redact_text(str(exc))}") from exc


async def test_model_profile(profile_id: str):
    started = datetime.now(timezone.utc)
    try:
        from agents.llm_client import get_llm
        llm = get_llm(temperature=0.0, max_tokens=80, profile_id=profile_id)

        def _call():
            return llm.invoke("只返回一句中文：模型接入成功")

        resp = await asyncio.wait_for(asyncio.to_thread(_call), timeout=float(os.getenv("MODEL_PROFILE_TEST_TIMEOUT_SECONDS", "30")))
        metadata = getattr(resp, "response_metadata", {}) or {}
        return {
            "status": "ok",
            "profile_id": profile_id,
            "model": metadata.get("model") or getattr(llm, "model_name", ""),
            "latency_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            "answer": getattr(resp, "content", ""),
            "token_usage": metadata.get("token_usage") or {},
        }
    except Exception as exc:
        return {
            "status": "failed",
            "profile_id": profile_id,
            "latency_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            "error": f"{type(exc).__name__}: {_redact_text(str(exc))}",
        }


def _benchmark_context(req: ModelBenchmarkRequest) -> dict:
    findings = []
    if req.include_latest_findings and isinstance(LAST_INSPECTION_PAYLOAD, dict):
        for item in LAST_INSPECTION_PAYLOAD.get("findings") or []:
            if not isinstance(item, dict):
                continue
            if req.cluster not in {"", "all", "*"} and req.cluster not in {item.get("cluster"), item.get("cluster_id")}:
                continue
            if req.namespace not in {"", "all", "*"} and req.namespace != item.get("namespace"):
                continue
            findings.append({
                "title": item.get("title"),
                "summary": item.get("summary"),
                "severity": item.get("severity"),
                "category": item.get("category"),
                "cluster": item.get("cluster"),
                "namespace": item.get("namespace"),
                "name": item.get("name"),
                "evidence": _compact_dict(item.get("evidence") or {}, 1800),
                "ops_plan": _compact_dict(item.get("ops_plan") or {}, 1800),
            })
    return {
        "scope": {"cluster": req.cluster, "namespace": req.namespace},
        "prompt": req.prompt or "请基于这些巡检证据输出根因判断、优先级、可执行修复计划、风险门禁和验证方式。",
        "findings": findings[:10],
        "source": "latest-inspection" if findings else "synthetic-sre-case",
    }


def _score_benchmark_answer(answer: str, context: dict, latency_ms: int, usage: dict) -> dict:
    text = (answer or "").lower()
    findings = context.get("findings") or []
    token_total = int(usage.get("total_tokens") or usage.get("input_tokens", 0) + usage.get("output_tokens", 0) or 0)

    def group_hits(groups: dict[str, list[str]]) -> tuple[list[str], list[str]]:
        hits = [label for label, terms in groups.items() if any(term in text for term in terms)]
        return hits, [label for label in groups if label not in hits]

    named_evidence = []
    for finding in findings[:8]:
        values = [str(finding.get(key) or "").lower() for key in ("name", "namespace", "category", "title")]
        matched = next((value for value in values if len(value) >= 3 and value in text), "")
        if matched:
            named_evidence.append(matched[:80])

    evidence_hits, evidence_missing = group_hits({
        "容器日志/退出状态": ["previous log", "previous_logs", "日志", "exit code", "laststate", "oomkilled"],
        "Kubernetes Events": ["event", "事件", "failedscheduling", "failedmount", "imagepull"],
        "Workload 实际配置": ["workload", "deployment", "statefulset", "yaml", "spec", "配置"],
        "指标与趋势": ["cpu", "memory", "p95", "错误率", "延迟", "重启趋势", "prometheus"],
        "依赖与影响面": ["拓扑", "依赖", "上游", "下游", "blast", "影响半径", "kafka"],
    })
    evidence_score = min(100, len(evidence_hits) * 13 + len(named_evidence) * 9 + (12 if findings else 5))

    reasoning_hits, reasoning_missing = group_hits({
        "根因候选排序": ["根因", "假设", "优先级", "可能性", "候选"],
        "因果链": ["导致", "因此", "由于", "因果", "传播", "触发"],
        "置信度与未知项": ["置信", "证据不足", "待确认", "不确定", "需要补充"],
        "区分症状与根因": ["症状", "根因", "表象", "不是根因"],
    })
    reasoning_score = min(100, len(reasoning_hits) * 22 + (12 if len(answer) >= 260 else 0))

    action_hits, action_missing = group_hits({
        "Workload Patch": ["patch", "resources", "startupProbe", "readinessProbe", "securitycontext"],
        "容量与弹性": ["scale", "扩容", "hpa", "副本"],
        "发布回滚": ["rollback", "回滚", "revision", "上一版本"],
        "Pod/节点处置": ["recreate", "重建 pod", "evict", "cordon", "隔离节点"],
        "存储修复": ["pvc", "storageclass", "csi", "扩容卷", "fsgroup"],
        "网络与流量": ["service selector", "endpoint", "networkpolicy", "dns", "流量切换"],
        "配置与发布": ["yaml", "configmap", "imagepullsecret", "不可变镜像", "灰度"],
        "替代策略": ["替代策略", "如果未恢复", "下一策略", "二次取证"],
    })
    target_specificity = 15 if named_evidence else (8 if any(term in text for term in ["namespace", "deployment/", "pod/"]) else 0)
    action_score = min(100, len(action_hits) * 11 + target_specificity + (10 if "验证" in text else 0))

    safety_hits, safety_missing = group_hits({
        "人工审批": ["审批", "人工确认", "二次确认"],
        "预演与最小权限": ["dry-run", "预演", "最小权限", "rbac", "白名单"],
        "错误预算/SLO 门禁": ["错误预算", "slo", "门禁", "burn rate"],
        "回滚路径": ["回滚", "恢复原配置", "上一版本"],
        "影响半径": ["影响半径", "blast", "上游", "下游", "关键路径"],
    })
    safety_score = min(100, len(safety_hits) * 19 + (5 if "任意 shell" not in text else 0))

    verification_hits, verification_missing = group_hits({
        "Pod Ready/rollout": ["pod ready", "ready", "rollout", "observedgeneration"],
        "重启与事件消退": ["restart_count", "重启次数", "事件消失", "events"],
        "业务 SLI": ["错误率", "成功率", "p95", "延迟", "吞吐", "业务探活"],
        "观测窗口": ["观察窗口", "持续观察", "分钟", "连续"],
        "失败后策略升级": ["未恢复", "替代策略", "重新取证", "停止重复"],
    })
    verification_score = min(100, len(verification_hits) * 19)

    latency_score = max(20, min(100, 100 - max(0, latency_ms - 1500) // 250))
    token_score = 100 if token_total <= 2200 else max(35, 100 - (token_total - 2200) // 90)
    length_score = 100 if 260 <= len(answer) <= 2200 else max(40, 100 - abs(len(answer) - 1100) // 18)
    efficiency_score = round(latency_score * 0.45 + token_score * 0.35 + length_score * 0.2, 1)

    dimensions = [
        ("evidence_grounding", "证据落地", 22, evidence_score, evidence_hits + [f"命中对象：{value}" for value in named_evidence[:4]], evidence_missing),
        ("root_cause_reasoning", "根因推理", 20, reasoning_score, reasoning_hits, reasoning_missing),
        ("remediation_depth", "修复深度", 22, action_score, action_hits, action_missing),
        ("safety_change_control", "安全变更", 16, safety_score, safety_hits, safety_missing),
        ("recovery_verification", "恢复验证", 12, verification_score, verification_hits, verification_missing),
        ("operational_efficiency", "响应效率", 8, efficiency_score, [f"延迟 {latency_ms}ms", f"Token {token_total}", f"回答长度 {len(answer)}"], []),
    ]
    criteria = [{
        "id": key, "label": label, "weight": weight, "score": round(float(score), 1),
        "weighted_score": round(float(score) * weight / 100.0, 2),
        "evidence": evidence[:8], "missing": missing[:6],
    } for key, label, weight, score, evidence, missing in dimensions]
    total = round(sum(item["weighted_score"] for item in criteria), 2)
    grade = "S" if total >= 90 else "A" if total >= 80 else "B" if total >= 70 else "C" if total >= 60 else "D"
    strengths = [item["label"] for item in sorted(criteria, key=lambda item: item["score"], reverse=True)[:2] if item["score"] >= 65]
    weak = sorted(criteria, key=lambda item: item["score"])[:3]
    gaps = [f"{item['label']}：缺少{'、'.join(item['missing'][:3])}" for item in weak if item["missing"]]
    recommendations = [
        f"补强{item['label']}：在答案中明确给出{'、'.join(item['missing'][:2])}。"
        for item in weak if item["missing"]
    ]
    return {
        "total": total,
        "grade": grade,
        "criteria": criteria,
        "evidence": evidence_score,
        "reasoning": reasoning_score,
        "actionability": action_score,
        "safety": safety_score,
        "verification": verification_score,
        "efficiency": efficiency_score,
        "strengths": strengths,
        "gaps": gaps,
        "recommendations": recommendations,
        "methodology": {
            "name": "FrontierSRE-Production-Rubric",
            "version": "2026.1",
            "weights": {item["id"]: item["weight"] for item in criteria},
            "principle": "真实证据优先于语言流畅度；可执行修复必须同时具备安全门禁、回滚和恢复验证。",
        },
        "explain": "总分为六个生产 SRE 维度的加权和；每项均保留命中证据、缺失项与改进建议。",
    }


async def run_model_benchmark(req: ModelBenchmarkRequest):
    profile_ids = [item for item in req.model_profile_ids if item]
    if not profile_ids:
        profile_ids = [get_active_model_profile_id() or select_model_profile().id]
    context = _benchmark_context(req)
    results = []
    for profile_id in profile_ids[:6]:
        started = datetime.now(timezone.utc)
        try:
            profile = select_model_profile(profile_id)
            from agents.llm_client import get_llm
            from langchain_core.messages import HumanMessage, SystemMessage

            llm = get_llm(temperature=0.05, max_tokens=min(1800, profile.max_tokens), profile_id=profile.id)
            prompt = (
                "你是 Kubernetes/AIOps 模型测评对象。请不要闲聊，基于证据输出："
                "1. 根因排序；2. 可执行修复计划；3. 变更风险门禁；4. 恢复验证；5. 需要人工确认的项。"
                "不要输出 JSON。\n\n"
                f"{json.dumps(_redact_sensitive(context), ensure_ascii=False)[:12000]}"
            )

            def _call():
                return llm.invoke([
                    SystemMessage(content="你是顶级 Kubernetes SRE 专家，回答要简洁、证据驱动、可执行。"),
                    HumanMessage(content=prompt),
                ])

            response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=float(os.getenv("MODEL_BENCHMARK_TIMEOUT_SECONDS", "60")))
            usage = ((getattr(response, "response_metadata", {}) or {}).get("token_usage") or {})
            answer = getattr(response, "content", "") or ""
            latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            result = {
                "profile_id": profile.id,
                "provider": profile.provider,
                "model": profile.model,
                "status": "ok",
                "latency_ms": latency_ms,
                "token_usage": usage,
                "score": _score_benchmark_answer(answer, context, latency_ms, usage),
                "answer_preview": _redact_text(answer)[:2400],
            }
        except Exception as exc:
            latency_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            result = {
                "profile_id": profile_id,
                "status": "failed",
                "latency_ms": latency_ms,
                "score": {"total": 0, "explain": "模型调用失败，无法评分。"},
                "error": f"{type(exc).__name__}: {_redact_text(str(exc))}",
            }
        results.append(result)
    run = {
        "id": str(uuid.uuid4())[:12],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scope": {"cluster": req.cluster, "namespace": req.namespace},
        "context_source": context.get("source"),
        "findings": len(context.get("findings") or []),
        "rubric": {
            "name": "FrontierSRE-Production-Rubric",
            "version": "2026.1",
            "dimensions": ["证据落地", "根因推理", "修复深度", "安全变更", "恢复验证", "响应效率"],
            "weights": {
                "证据落地": 22, "根因推理": 20, "修复深度": 22,
                "安全变更": 16, "恢复验证": 12, "响应效率": 8,
            },
            "formula": "总分 = Σ(维度得分 × 维度权重)；S>=90，A>=80，B>=70，C>=60，否则 D。",
            "basis": [
                "Google SRE 的 SLO/错误预算与渐进式风险控制原则",
                "Kubernetes 声明式变更、最小权限、可回滚与 rollout 收敛验证",
                "DORA 变更失败率/恢复时间与 OpenTelemetry 可观测证据链",
            ],
            "scope": "这是公开、可审计的 frontier AI infrastructure SRE 基准，不冒充任何组织未公开的内部评分标准。",
        },
        "results": sorted(results, key=lambda x: (x.get("score") or {}).get("total", 0), reverse=True),
    }
    bounded_append(MODEL_BENCHMARK_STORE, run, STORE_LIMIT)
    return {"status": "ok", "run": run}


async def model_benchmark(limit: int = 20):
    runs = MODEL_BENCHMARK_STORE[-limit:]
    leaderboard: dict[str, dict] = {}
    for run in runs:
        for result in run.get("results") or []:
            key = result.get("profile_id") or "unknown"
            row = leaderboard.setdefault(key, {"profile_id": key, "runs": 0, "score_total": 0.0, "avg_latency_ms": 0, "_latency": 0, "failures": 0, "_dimensions": {}})
            row["runs"] += 1
            row["score_total"] += float((result.get("score") or {}).get("total") or 0)
            row["_latency"] += int(result.get("latency_ms") or 0)
            if result.get("status") != "ok":
                row["failures"] += 1
            for criterion in (result.get("score") or {}).get("criteria") or []:
                criterion_id = criterion.get("id")
                if criterion_id:
                    row["_dimensions"][criterion_id] = row["_dimensions"].get(criterion_id, 0.0) + float(criterion.get("score") or 0)
    rows = []
    for row in leaderboard.values():
        row["avg_score"] = round(row["score_total"] / row["runs"], 2) if row["runs"] else 0
        row["avg_latency_ms"] = int(row["_latency"] / row["runs"]) if row["runs"] else 0
        row["dimension_averages"] = {key: round(value / row["runs"], 1) for key, value in row["_dimensions"].items()} if row["runs"] else {}
        row.pop("_latency", None)
        row.pop("score_total", None)
        row.pop("_dimensions", None)
        rows.append(row)
    return {
        "status": "ok",
        "rubric": {
            "name": "FrontierSRE-Production-Rubric",
            "version": "2026.1",
            "weights": {"证据落地": 22, "根因推理": 20, "修复深度": 22, "安全变更": 16, "恢复验证": 12, "响应效率": 8},
            "formula": "总分 = Σ(维度得分 × 维度权重)",
            "basis": ["SLO/错误预算", "Kubernetes 安全变更", "恢复验证", "DORA 成效", "OpenTelemetry 证据链"],
            "scope": "公开可审计的生产 SRE 基准；离线答案评分与线上真实修复成效分开记录。",
        },
        "leaderboard": sorted(rows, key=lambda x: x["avg_score"], reverse=True),
        "runs": list(reversed(runs)),
    }


async def cloud_adapters():
    return cloud_adapters_payload()


async def ai_effectiveness():
    return effectiveness_summary()


async def algorithm_registry():
    return {
        "status": "ok",
        "positioning": "本项目的核心不是普通聊天，而是算法驱动的 AIOps 决策控制面。",
        "patent": {
            "title": "一种云原生灰度发布门禁控制方法、系统及存储介质",
            "mapped_modules": [
                "变更语义算子",
                "变更敏感因果传播图",
                "影响放大系数 Amp",
                "错误预算安全包络",
                "灰度-稳定差分观测",
                "滞回状态机",
                "审计反馈闭环",
            ],
        },
        "algorithms": [
            {
                "name": "ChangeSensitiveBlastRadius",
                "module": "agents.aiops_algorithms.analyze_blast_radius",
                "api": "/api/topology/impact",
                "used_in": ["拓扑影响分析", "修复前影响面门禁", "灰度发布门禁"],
                "patent_mapping": ["S3 变更敏感因果传播图", "S4 传播规则", "影响放大系数 Amp"],
                "formula": "Amp=sum(PathWeight*OperatorWeight*TrafficRatio*SLOWeight*BusinessWeight*RetryFactor*ResourcePressure*ContextRisk*EdgePropagationCoef*Decay)",
            },
            {
                "name": "SemanticGrayReleaseGate",
                "module": "agents.aiops_algorithms.evaluate_release_gate",
                "api": "/api/release-gate/evaluate",
                "used_in": ["变更风险分析", "灰度门禁", "发布暂停/回滚/人工审批"],
                "patent_mapping": ["S2 变更语义识别", "S5 历史风险", "S6 预算预测", "S7 安全包络", "S10 滞回状态机"],
                "formula": "Envelope={G|BudgetCost<=RemainingBudget*SafetyFactor,ViolationProb<=Threshold,RiskScore<=Threshold}",
            },
            {
                "name": "InspectionEvidencePriority",
                "module": "agents.aiops_algorithms.prioritize_inspection_findings",
                "api": "/api/inspection/run",
                "used_in": ["AI 巡检", "告警优先级", "自动运维排队"],
                "patent_mapping": ["多源数据采集", "运行上下文风险", "审计反馈"],
                "formula": "Priority=0.32*Severity+0.24*IssueType+0.18*Impact+0.16*RedundancyRisk+0.10*EvidenceConfidence",
            },
        ],
    }


def _finding_to_algorithm_graph(finding: dict) -> tuple[dict, dict, dict]:
    finding = finding or {}
    workload = finding.get("workload") or {}
    pod = ((finding.get("evidence") or {}).get("pod") or {})
    cluster = finding.get("cluster") or "local-cluster"
    namespace = finding.get("namespace") or "default"
    workload_kind = workload.get("kind") or pod.get("workload_kind") or "Workload"
    workload_name = workload.get("name") or pod.get("workload_name") or finding.get("name") or "unknown"
    pod_name = pod.get("name") or finding.get("name") or f"{workload_name}-pod"
    ns_id = f"ns:{cluster}:{namespace}"
    wl_id = f"workload:{cluster}:{namespace}:{workload_kind}:{workload_name}"
    pod_id = f"pod:{cluster}:{namespace}:{pod_name}"
    svc_id = f"service:{cluster}:{namespace}:{workload_name}"
    dep_id = f"dependency:{cluster}:observability:logs-metrics"
    risk = "critical" if finding.get("severity") in {"P0", "P1"} else "high" if finding.get("severity") == "P2" else "medium"
    graph = {
        "nodes": [
            {"id": ns_id, "type": "namespace", "title": namespace, "category": "namespace", "risk": "normal", "meta": {"cluster": cluster}},
            {"id": wl_id, "type": "workload", "title": f"{workload_kind}/{workload_name}", "category": workload.get("category") or "application", "risk": risk, "meta": {"cluster": cluster, "namespace": namespace, "business_weight": 1.1}},
            {"id": pod_id, "type": "pod", "title": pod_name, "category": workload.get("category") or "application", "risk": risk, "status": "err" if risk in {"critical", "high"} else "warn", "meta": {"restart_count": pod.get("restart_count", 0), "resource_pressure": 0.4 if finding.get("category") == "high_cpu" else 0.2}},
            {"id": svc_id, "type": "service", "title": f"svc/{workload_name}", "category": "application", "risk": risk, "meta": {"traffic_ratio": 0.7}},
            {"id": dep_id, "type": "dependency", "title": "logging/metrics pipeline", "category": "observability", "risk": "medium", "meta": {"criticality": "infrastructure"}},
        ],
        "edges": [
            {"from": ns_id, "to": wl_id, "type": "contains", "propagation_coef": 0.45},
            {"from": wl_id, "to": pod_id, "type": "owns", "propagation_coef": 0.9},
            {"from": pod_id, "to": svc_id, "type": "serves", "traffic_ratio": 0.8, "propagation_coef": 0.85},
            {"from": pod_id, "to": dep_id, "type": "emits_logs", "traffic_ratio": 0.35, "propagation_coef": 0.45},
        ],
    }
    selected = graph["nodes"][2]
    change = {
        "target": selected["id"],
        "kind": "pod",
        "category": finding.get("category"),
        "summary": finding.get("summary"),
        "operator": "pod_change",
    }
    return selected, graph, change


def _algorithm_live_cases() -> dict:
    findings = (LAST_INSPECTION_PAYLOAD.get("findings") or []) if isinstance(LAST_INSPECTION_PAYLOAD, dict) else []
    top_finding = findings[0] if findings else {}
    cases: list[dict] = []
    if top_finding:
        selected, graph, change = _finding_to_algorithm_graph(top_finding)
        blast = analyze_blast_radius(
            selected,
            graph,
            "single_pod_change",
            change,
            {"runtime_pressure": 0.45, "budget_burn_rate": 0.25, "max_depth": 4},
        )
        release = evaluate_release_gate(
            {"target": selected["id"], "kind": "pod", "operator": "pod_change", "selected": selected},
            graph,
            {
                "remaining_budget": 0.42,
                "budget_burn_rate": 0.35,
                "runtime_pressure": 0.45,
                "release_state": "pending",
            },
            [],
            [],
            {"diff_error_rate": 0.05, "diff_p99": 0.08, "diff_budget_burn": 0.12},
        )
        cases.extend([
            {
                "id": "live-blast-radius",
                "title": "拓扑影响分析 / 爆炸半径",
                "algorithm": "ChangeSensitiveBlastRadius",
                "where_used": "拓扑影响分析、SRE 对话影响判断、自动运维前审批门禁",
                "input": {"finding": top_finding.get("title"), "cluster": top_finding.get("cluster"), "namespace": top_finding.get("namespace")},
                "output": {
                    "impact_level": blast.get("impact_level"),
                    "impact_score": blast.get("impact_score"),
                    "amplification_factor": blast.get("amplification_factor"),
                    "critical_paths": (blast.get("blast_radius") or {}).get("critical_paths", [])[:4],
                },
                "action_effect": "影响分数 high/critical 时，修复动作必须先展示影响路径、dry-run 与人工确认。",
            },
            {
                "id": "live-release-gate",
                "title": "变更风险分析 / 灰度门禁",
                "algorithm": "SemanticGrayReleaseGate",
                "where_used": "修复配置、扩缩容、发布变更前的通过/暂停/回滚/人工审批判断",
                "input": {"change": "pod_change", "target": selected.get("title")},
                "output": {
                    "verdict": release.get("verdict"),
                    "action": release.get("action"),
                    "reason": release.get("reason"),
                    "risk": release.get("risk"),
                    "selected_strategy": release.get("selected_strategy"),
                },
                "action_effect": "如果不在安全包络内，系统不会直接执行扩大影响面的变更，而会要求人工审批或最小灰度。",
            },
        ])
    if LAST_INSPECTION_PAYLOAD:
        cases.append({
            "id": "live-inspection-priority",
            "title": "AI 巡检异常排序 / 自动运维排队",
            "algorithm": "InspectionEvidencePriority",
            "where_used": "AI 巡检结果排序、自动运维优先队列、告警去重",
            "input": {
                "findings": len(findings),
                "source": LAST_INSPECTION_PAYLOAD.get("source"),
                "scope": f"{LAST_INSPECTION_PAYLOAD.get('summary', {}).get('clusters', 1)} cluster(s)",
            },
            "output": {
                "algorithm": LAST_INSPECTION_PAYLOAD.get("inspection_algorithm"),
                "top_risks": (LAST_INSPECTION_PAYLOAD.get("summary") or {}).get("priority_top", []),
            },
            "action_effect": "分数最高的问题会排在最前，并且自动运维只对有可执行 plan 的对象发起动作。",
        })
    return {"cases": cases, "source": "latest-inspection" if LAST_INSPECTION_PAYLOAD else "waiting-for-runtime-data"}


async def algorithm_workbench():
    live = _algorithm_live_cases()
    return {
        "status": "ok",
        "positioning": "算法展示模块呈现算法在实际 AIOps 流程中的运行结果，而不是静态说明。",
        "runtime_source": live["source"],
        "cases": live["cases"],
        "recent_decisions": list(reversed(ALGORITHM_DECISIONS_STORE[-30:])),
        "module_map": [
            {
                "module": "拓扑影响分析",
                "algorithm": "ChangeSensitiveBlastRadius",
                "api": "/api/topology/impact",
                "effect": "计算单 Pod/Workload 变化对 Service、依赖、架构的爆炸半径和传播路径。",
            },
            {
                "module": "变更风险分析",
                "algorithm": "SemanticGrayReleaseGate",
                "api": "/api/release-gate/evaluate",
                "effect": "计算错误预算安全包络，决定通过、暂停、回滚或人工审批。",
            },
            {
                "module": "AI 巡检与自动运维",
                "algorithm": "InspectionEvidencePriority",
                "api": "/api/inspection/run",
                "effect": "根据严重级别、问题类型、影响面、冗余风险和证据置信度排序。",
            },
        ],
    }


# ============================================================
# Alert
# ============================================================
async def proxy_alert(request: Request):
    """模拟 Alertmanager Webhook，触发完整 SRE 流程"""
    started_at = datetime.now(timezone.utc)
    incoming = await request.json()
    body, meta = _normalize_alertmanager_body(incoming)

    try:
        async with _client(120) as c:
            resp = await c.post(f"{SERVICES['observability']}/alertmanager/webhook", json=body)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        data = {"status": "fallback", "error": str(e)}

    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        _record_llm_observation("alert_workflow", incoming, data if isinstance(data, dict) else {}, started_at, error=(data or {}).get("error", "") if isinstance(data, dict) else "")
    for item in results:
        if not isinstance(item, dict):
            item = {"result": item}
        _remember_graph_result(item.get("raw"))
        _record_llm_observation("alert_workflow", incoming, item if isinstance(item, dict) else {}, started_at)

    bounded_append(ALERT_HISTORY, {
        "id": str(uuid.uuid4())[:8],
        "type": "alert",
        "alert_name": meta["alert_name"],
        "namespace": meta["namespace"],
        "deployment": meta["deployment"],
        "severity": meta["severity"],
        "message": meta["message"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "result": data,
    }, STORE_LIMIT)

    return {
        **data,
        "incidents": INCIDENTS_STORE[-50:],
        "postmortems": POSTMORTEMS_STORE[-50:],
    }


# ============================================================
# MCP Proxy — 直接调用 K8s 工具
# ============================================================
async def _call_mcp_tool(tool: str, arguments: dict | None = None) -> dict:
    req = MCPToolRequest(tool=tool, arguments=arguments or {})
    return await mcp_call(req)


def _is_crash_pod(pod: dict) -> bool:
    state_text = " ".join(str(c.get("state", "")) for c in pod.get("containers", []))
    return (
        pod.get("restart_count", 0) > 5
        or any(s in state_text for s in ["CrashLoopBackOff", "ImagePullBackOff", "OOMKilled", "Error"])
    )


def _cpu_to_millicores(cpu: str | None) -> float:
    if not cpu:
        return 0.0
    if cpu.endswith("n"):
        return float(cpu[:-1]) / 1_000_000
    if cpu.endswith("u"):
        return float(cpu[:-1]) / 1_000
    if cpu.endswith("m"):
        return float(cpu[:-1])
    return float(cpu) * 1000


def _quantity_to_bytes(value: str | None) -> float:
    if not value:
        return 0.0
    text = str(value).strip()
    factors = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "Ti": 1024 ** 4,
        "K": 1000,
        "M": 1000 ** 2,
        "G": 1000 ** 3,
        "T": 1000 ** 4,
    }
    for suffix, factor in factors.items():
        if text.endswith(suffix):
            return float(text[:-len(suffix)]) * factor
    return float(text)


def _auto_severity(intent: str, findings: list[dict]) -> str:
    if not findings:
        return "P3"
    if intent == "crashloop":
        restarts = [int(x.get("restart_count") or 0) for x in findings]
        if len(findings) >= 3 or max(restarts or [0]) >= 20:
            return "P1"
        return "P2"
    if intent == "pending":
        return "P1" if len(findings) >= 5 else "P2"
    if intent == "highcpu":
        max_cpu = max([float(x.get("cpu_millicores") or 0) for x in findings] or [0])
        return "P1" if max_cpu >= 2000 or len(findings) >= 5 else "P2"
    return "P2"


def _pod_owner_name(pod: dict) -> str:
    if pod.get("workload_name"):
        return str(pod["workload_name"])
    workload = pod.get("workload") or {}
    if workload.get("name"):
        return str(workload["name"])
    owners = pod.get("owner_references") or []
    if owners:
        owner = owners[0]
        name = str(owner.get("name", ""))
        if owner.get("kind") == "ReplicaSet":
            return re.sub(r"-[a-f0-9]{8,12}$", "", name) or name
        return name
    return ""


def _node_index(graph: dict) -> dict:
    return {n.get("id"): n for n in graph.get("nodes", []) if n.get("id")}


def _walk_edges(graph: dict, start_id: str, direction: str = "downstream", max_depth: int = 4) -> list[dict]:
    nodes = _node_index(graph)
    edges = graph.get("edges", [])
    frontier = [(start_id, 0)]
    visited = {start_id}
    result: list[dict] = []
    while frontier:
        current, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        for edge in edges:
            if direction == "downstream":
                match = edge.get("from") == current
                next_id = edge.get("to")
            else:
                match = edge.get("to") == current
                next_id = edge.get("from")
            if not match or not next_id or next_id in visited:
                continue
            visited.add(next_id)
            next_node = nodes.get(next_id, {"id": next_id})
            result.append({
                "depth": depth + 1,
                "edge_risk": edge.get("risk", "normal"),
                "node": {
                    "id": next_id,
                    "type": next_node.get("type"),
                    "title": next_node.get("title"),
                    "category": next_node.get("category"),
                    "risk": next_node.get("risk"),
                    "status": next_node.get("status"),
                    "meta": next_node.get("meta"),
                },
            })
            frontier.append((next_id, depth + 1))
    return result


def _edge_default_traffic(edge_type: str) -> float:
    text = str(edge_type or "").lower()
    if "kafka_to_elk" in text or "dataflow" in text or "stream" in text:
        return 0.8
    if "route" in text or "service" in text or "calls" in text:
        return 0.62
    if "contains" in text:
        return 0.18
    if "logging" in text:
        return 0.28
    return 0.42


def _normalize_topology_graph(graph: dict, selected: dict | None = None) -> tuple[dict, dict]:
    """Normalize frontend/CMDB topology into the deterministic algorithm contract."""
    graph = graph or {}
    selected = selected or {}
    nodes: list[dict] = []
    seen_nodes: set[str] = set()
    for raw in graph.get("nodes") or []:
        if not isinstance(raw, dict):
            continue
        node_id = str(raw.get("id") or raw.get("uid") or raw.get("name") or raw.get("title") or "").strip()
        if not node_id or node_id in seen_nodes:
            continue
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
        detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else {}
        node = {
            **raw,
            "id": node_id,
            "title": raw.get("title") or raw.get("name") or node_id,
            "type": str(raw.get("type") or raw.get("kind") or "node").lower(),
            "category": raw.get("category") or raw.get("type") or raw.get("kind") or "application",
            "risk": raw.get("risk") or ("critical" if raw.get("status") == "err" else "high" if raw.get("status") == "warn" else "normal"),
            "status": raw.get("status") or "ok",
            "meta": {
                **meta,
                "namespace": meta.get("namespace") or detail.get("namespace") or raw.get("namespace") or "",
                "cluster": meta.get("cluster") or detail.get("cluster") or raw.get("cluster") or "",
                "cluster_id": meta.get("cluster_id") or detail.get("cluster_id") or raw.get("cluster_id") or "",
                "business_weight": meta.get("business_weight") or (1.25 if str(raw.get("category") or "").lower() in {"data", "middleware", "infrastructure"} else 1.0),
            },
        }
        nodes.append(node)
        seen_nodes.add(node_id)

    edges: list[dict] = []
    for raw in graph.get("edges") or []:
        if not isinstance(raw, dict):
            continue
        src = str(raw.get("from") or raw.get("source") or raw.get("src") or raw.get("caller") or "").strip()
        dst = str(raw.get("to") or raw.get("target") or raw.get("dst") or raw.get("callee") or "").strip()
        if not src or not dst or src == dst:
            continue
        edge_type = raw.get("type") or raw.get("protocol") or "dependency"
        traffic = raw.get("traffic_ratio", raw.get("traffic", raw.get("weight")))
        propagation = raw.get("propagation_coef", raw.get("propagation"))
        try:
            traffic_value = float(traffic)
        except Exception:
            traffic_value = _edge_default_traffic(str(edge_type))
        try:
            propagation_value = float(propagation)
        except Exception:
            propagation_value = 0.72 if traffic_value >= 0.6 else 0.48
        edges.append({
            **raw,
            "from": src,
            "to": dst,
            "type": edge_type,
            "risk": raw.get("risk") or ("high" if any(k in str(edge_type).lower() for k in ["kafka", "data", "storage", "external"]) else "normal"),
            "traffic_ratio": max(0.05, min(1.0, traffic_value)),
            "propagation_coef": max(0.15, min(1.6, propagation_value)),
        })

    normalized = {**graph, "nodes": nodes, "edges": edges}
    selected_id = str(selected.get("id") or "").strip()
    normalized_selected = dict(selected)
    if selected_id not in seen_nodes:
        selected_title = str(selected.get("title") or selected.get("name") or "").lower()
        matched = next(
            (
                n for n in nodes
                if selected_title and selected_title in str(n.get("title") or n.get("name") or "").lower()
            ),
            None,
        )
        if matched:
            normalized_selected = {**matched, **selected, "id": matched["id"]}
    elif selected_id:
        graph_node = next((n for n in nodes if n.get("id") == selected_id), {})
        normalized_selected = {**graph_node, **selected, "id": selected_id}
    return normalized, normalized_selected


def _risk_numeric(value: str | None) -> float:
    return {
        "critical": 1.0,
        "err": 0.9,
        "high": 0.74,
        "medium": 0.48,
        "warn": 0.46,
        "low": 0.28,
        "normal": 0.16,
        "ok": 0.12,
    }.get(str(value or "").lower(), 0.22)


def _impact_level(score: float) -> str:
    if score >= 0.82:
        return "critical"
    if score >= 0.58:
        return "high"
    if score >= 0.34:
        return "medium"
    return "low"


def _fallback_blast_radius_policy(req: TopologyImpactRequest, graph: dict, selected: dict, error: Exception) -> dict:
    selected_id = str(selected.get("id") or "")
    nodes = _node_index(graph)
    selected_node = nodes.get(selected_id, selected or {})
    upstream = _walk_edges(graph, selected_id, "upstream") if selected_id else []
    downstream = _walk_edges(graph, selected_id, "downstream") if selected_id else []
    impacted_services = [x["node"] for x in downstream if str((x.get("node") or {}).get("type")) in {"service", "ingress"}]
    impacted_pods = [x["node"] for x in downstream if str((x.get("node") or {}).get("type")) == "pod"]
    parent_workloads = [x["node"] for x in upstream if str((x.get("node") or {}).get("type")) == "workload"]
    related_dependencies = [
        x["node"] for x in downstream
        if str((x.get("node") or {}).get("type")) in {"dependency", "middleware", "observability"}
        or str((x.get("node") or {}).get("category")) in {"data", "middleware", "storage", "external"}
    ]
    critical_items = [
        x for x in upstream + downstream
        if str((x.get("node") or {}).get("risk")) in {"critical", "high", "err"}
        or str(x.get("edge_risk")) in {"critical", "high"}
    ]
    impact_score = min(
        1.0,
        0.18
        + _risk_numeric(selected_node.get("risk") or selected_node.get("status")) * 0.26
        + min(0.28, len(downstream) * 0.035)
        + min(0.16, len(upstream) * 0.025)
        + min(0.16, len(related_dependencies) * 0.055)
        + min(0.16, len(critical_items) * 0.045),
    )
    amplification_factor = round(
        1
        + sum((1 / max(1, int(x.get("depth") or 1))) * (0.25 + _risk_numeric(x.get("edge_risk"))) for x in downstream[:20])
        + len(related_dependencies) * 0.18,
        2,
    )
    critical_paths = []
    for item in critical_items[:8]:
        node = item.get("node") or {}
        critical_paths.append({
            "depth": item.get("depth"),
            "node": node.get("title") or node.get("id"),
            "type": node.get("type"),
            "risk": node.get("risk") or item.get("edge_risk"),
        })
    return {
        "source": "fallback",
        "algorithm": {"name": "ChangeSensitiveBlastRadius", "version": "graph-normalized-fallback", "semantic_operator": req.scenario},
        "selected": {
            "id": selected_node.get("id") or selected_id,
            "type": selected_node.get("type") or selected.get("type"),
            "title": selected_node.get("title") or selected.get("title") or selected.get("name"),
            "category": selected_node.get("category") or selected.get("category"),
            "risk": selected_node.get("risk") or selected.get("risk", "normal"),
            "status": selected_node.get("status") or selected.get("status", "ok"),
        },
        "impact_level": _impact_level(impact_score),
        "impact_score": round(impact_score, 3),
        "amplification_factor": amplification_factor,
        "context_risk": round(_risk_numeric(selected_node.get("risk") or selected_node.get("status")), 3),
        "summary": (
            f"已基于标准化拓扑执行降级影响分析。选中节点连接到 {len(upstream)} 个上游、"
            f"{len(downstream)} 个下游、{len(related_dependencies)} 个基础依赖。"
        ),
        "blast_radius": {
            "upstream": upstream,
            "downstream": downstream,
            "impacted_services": impacted_services,
            "impacted_pods": impacted_pods,
            "parent_workloads": parent_workloads,
            "related_dependencies": related_dependencies,
            "critical_paths": critical_paths,
        },
        "aiops_value": [
            "将 source/target 与 from/to 混用的 CMDB 边统一成算法输入，避免影响面归零。",
            "用图遍历、风险权重、依赖数量和传播深度形成可解释的变更审批基线。",
        ],
        "recommended_actions": [
            "优先验证关键路径上的 Service、Pod、Kafka/ELK 或数据依赖是否存在同向异常。",
            "如果影响等级为 high/critical，执行修复前应开启人工确认和变更门禁。",
        ],
        "error": f"{type(error).__name__}: {error}",
    }


def _deterministic_topology_impact(req: TopologyImpactRequest) -> dict:
    graph, selected = _normalize_topology_graph(req.graph or {}, req.selected or {})
    try:
        return analyze_blast_radius(selected or {}, graph or {}, req.scenario)
    except Exception as exc:
        return _fallback_blast_radius_policy(req, graph, selected, exc)


def _topology_prompt(policy: dict, req: TopologyImpactRequest) -> str:
    compact = _redact_sensitive({
        "selected": policy.get("selected"),
        "impact_level": policy.get("impact_level"),
        "summary": policy.get("summary"),
        "blast_radius": policy.get("blast_radius"),
        "aiops_value": policy.get("aiops_value"),
        "recommended_actions": policy.get("recommended_actions"),
        "selected_detail": (req.selected or {}).get("detail", {}),
    })
    return f"""你是一个资深 AIOps/SRE 架构专家。请基于下面的 Kubernetes 拓扑上下文，输出对运维有实际价值的影响分析。

要求：
1. 解释被选中节点在整体架构中的角色。
2. 说明单个 Pod 或 Workload 变动会如何影响应用、Service、调用链和整体集群可靠性。
3. 明确影响等级、影响路径、需要验证的证据、建议动作。
4. 不要编造拓扑中不存在的服务；证据不足时要明确说“需要补充证据”。
5. 输出简洁中文 Markdown，分为：架构角色、影响半径、风险判断、SRE 操作建议、AIOps 后续价值。

拓扑上下文 JSON：
{json.dumps(compact, ensure_ascii=False, indent=2)}
"""


async def analyze_topology_impact(req: TopologyImpactRequest):
    """Use topology context plus LLM to explain pod/workload blast radius."""
    policy = _deterministic_topology_impact(req)
    _record_algorithm_decision(
        "ChangeSensitiveBlastRadius",
        "拓扑影响分析 / /api/topology/impact",
        {
            "impact_level": policy.get("impact_level"),
            "impact_score": policy.get("impact_score"),
            "amplification_factor": policy.get("amplification_factor"),
            "critical_paths": (policy.get("blast_radius") or {}).get("critical_paths", [])[:5],
        },
        {"selected": (req.selected or {}).get("title") or (req.selected or {}).get("id"), "scenario": req.scenario},
        "用于决定影响面、关键路径、是否需要人工确认和后续变更门禁。",
    )
    try:
        def _call_llm() -> str:
            import sys
            sys.path.insert(0, str(ROOT_DIR))
            from agents.llm_client import get_llm
            from langchain_core.messages import HumanMessage, SystemMessage

            llm = get_llm(temperature=0.05, max_tokens=1800)
            result = llm.invoke([
                SystemMessage(content="你是企业级 Kubernetes AIOps 产品中的 SRE 拓扑影响分析专家。"),
                HumanMessage(content=_topology_prompt(policy, req)),
            ])
            return getattr(result, "content", str(result))

        analysis = await asyncio.wait_for(asyncio.to_thread(_call_llm), timeout=25)
        return {
            "status": "ok",
            "source": "llm",
            "analysis": analysis,
            "policy": policy,
        }
    except Exception as e:
        return {
            "status": "fallback",
            "source": "policy",
            "error": f"{type(e).__name__}: {e}",
            "analysis": "\n".join([
                "## 架构角色",
                policy["summary"],
                "",
                "## 影响半径",
                f"- 上游关联：{len(policy['blast_radius']['upstream'])} 个节点",
                f"- 下游关联：{len(policy['blast_radius']['downstream'])} 个节点",
                f"- 受影响 Service：{len(policy['blast_radius']['impacted_services'])} 个",
                f"- 关联基础依赖：{len(policy['blast_radius'].get('related_dependencies', []))} 个",
                "",
                "## SRE 操作建议",
                *[f"- {item}" for item in policy["recommended_actions"]],
                "",
                "## AIOps 后续价值",
                *[f"- {item}" for item in policy["aiops_value"]],
            ]),
            "policy": policy,
        }


async def evaluate_gray_release_gate(req: ReleaseGateRequest):
    """Evaluate a cloud-native gray-release gate with real safety-envelope algorithms."""
    try:
        result = evaluate_release_gate(
            change=req.change,
            graph=req.graph,
            runtime=req.runtime,
            history=req.history,
            candidates=req.candidates,
            observation=req.observation,
        )
        _record_algorithm_decision(
            "SemanticGrayReleaseGate",
            "变更风险分析 / /api/release-gate/evaluate",
            {
                "verdict": result.get("verdict"),
                "action": result.get("action"),
                "risk": result.get("risk"),
                "selected_strategy": result.get("selected_strategy"),
            },
            {"change": req.change, "runtime": req.runtime},
            "用于决定发布/修复变更是通过、暂停、回滚还是转人工审批。",
        )
        return result
    except Exception as e:
        return {
            "status": "failed",
            "error": f"{type(e).__name__}: {e}",
            "algorithm": {"name": "SemanticGrayReleaseGate", "version": "1.0"},
        }


def _normalize_cmdb_topology(raw) -> dict:
    if not raw:
        return {"nodes": [], "edges": []}
    if isinstance(raw, dict):
        if isinstance(raw.get("data"), dict):
            raw = raw["data"]
        nodes = raw.get("nodes") or raw.get("items") or raw.get("applications") or raw.get("apps") or []
        edges = raw.get("edges") or raw.get("links") or raw.get("relations") or raw.get("dependencies") or []
    elif isinstance(raw, list):
        nodes, edges = raw, []
    else:
        return {"nodes": [], "edges": []}

    norm_nodes = []
    for item in nodes if isinstance(nodes, list) else []:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or item.get("name") or item.get("app") or item.get("service") or "")
        if not node_id:
            continue
        norm_nodes.append({
            "id": node_id,
            "name": item.get("name") or item.get("app") or item.get("service") or node_id,
            "type": item.get("type") or item.get("kind") or item.get("category") or "application",
            "namespace": item.get("namespace") or item.get("ns") or "",
            "cluster": item.get("cluster") or item.get("cluster_name") or "",
            "cluster_id": item.get("cluster_id") or item.get("clusterId") or "",
            "kind": item.get("kind") or "",
            "owner": item.get("owner") or item.get("team") or "",
            "tier": item.get("tier") or item.get("layer") or "",
            "raw": item,
        })

    norm_edges = []
    for item in edges if isinstance(edges, list) else []:
        if not isinstance(item, dict):
            continue
        source = item.get("source") or item.get("from") or item.get("src") or item.get("caller")
        target = item.get("target") or item.get("to") or item.get("dst") or item.get("callee")
        if not source or not target:
            continue
        norm_edges.append({
            "source": str(source),
            "target": str(target),
            "type": item.get("type") or item.get("protocol") or "dependency",
            "traffic": item.get("traffic") or item.get("qps") or item.get("weight") or "",
            "raw": item,
        })
    return {"nodes": norm_nodes, "edges": norm_edges}


def _rancher_base() -> str:
    raw = os.getenv("RANCHER_URL", "").strip().rstrip("/")
    for marker in ("/dashboard", "/v3", "/v1", "/k8s/clusters"):
        if marker in raw:
            raw = raw.split(marker, 1)[0]
    return raw.rstrip("/")


def _rancher_token() -> str:
    return os.getenv("RANCHER_TOKEN", "").strip()


def _rancher_enabled() -> bool:
    return bool(_rancher_base() and _rancher_token())


def _rancher_verify_ssl() -> bool:
    return _env_bool("RANCHER_VERIFY_SSL", "true")


def _wanted_rancher_clusters() -> set[str] | None:
    raw = os.getenv("RANCHER_CLUSTER_IDS", "all")
    values = {item.strip() for item in raw.split(",") if item.strip()}
    lowered = {item.lower() for item in values}
    if not values or {"all", "*", "所有"} & lowered:
        return None
    return values


async def _rancher_get(path: str, timeout: int = 20):
    if not _rancher_enabled():
        raise RuntimeError("RANCHER_URL/RANCHER_TOKEN 未配置")
    url = path if str(path).startswith(("http://", "https://")) else f"{_rancher_base()}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {_rancher_token()}", "Accept": "application/json"}
    async with OUTBOUND_BULKHEAD.slot():
        client = RANCHER_HTTP_CLIENT
        if client is None:
            async with httpx.AsyncClient(timeout=timeout, verify=_rancher_verify_ssl(), limits=HTTP_LIMITS) as fallback:
                resp = await fallback.get(url, headers=headers)
        else:
            resp = await client.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        if "application/json" in resp.headers.get("content-type", ""):
            return resp.json()
        return resp.text


def _with_rancher_limit(path_or_url: str, limit: int = 1000) -> str:
    parsed = urlparse(path_or_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("limit", str(limit))
    return urlunparse(parsed._replace(query=urlencode(query)))


async def _rancher_collect(path_or_url: str, timeout: int = 20) -> tuple[list[dict], dict]:
    items: list[dict] = []
    page = _with_rancher_limit(path_or_url)
    pages = 0
    last_payload: dict = {}
    while page and pages < 50:
        payload = await _rancher_get(page, timeout=timeout)
        if not isinstance(payload, dict):
            break
        last_payload = payload
        data = payload.get("data") or []
        if isinstance(data, list):
            items.extend(x for x in data if isinstance(x, dict))
        pagination = payload.get("pagination") or {}
        links = payload.get("links") or {}
        next_page = pagination.get("next") or links.get("next")
        if not next_page or next_page == page:
            break
        page = next_page
        pages += 1
    return items, {
        "pages": pages + 1,
        "count": len(items),
        "base_type": last_payload.get("baseType", ""),
        "type": last_payload.get("type", ""),
    }


async def _rancher_k8s_get(cluster_id: str, path: str, timeout: int = 25):
    return await _rancher_get(f"/k8s/clusters/{quote(cluster_id, safe='')}{path}", timeout=timeout)


async def _rancher_k8s_patch(
    cluster_id: str,
    path: str,
    patch: dict,
    timeout: int = 30,
    content_type: str = "application/strategic-merge-patch+json",
):
    if not _rancher_enabled():
        raise RuntimeError("RANCHER_URL/RANCHER_TOKEN 未配置")
    url = f"{_rancher_base()}/k8s/clusters/{quote(cluster_id, safe='')}{path}"
    headers = {
        "Authorization": f"Bearer {_rancher_token()}",
        "Content-Type": content_type,
        "Accept": "application/json",
    }
    async with OUTBOUND_BULKHEAD.slot():
        client = RANCHER_HTTP_CLIENT
        if client is None:
            async with httpx.AsyncClient(timeout=timeout, verify=_rancher_verify_ssl(), limits=HTTP_LIMITS) as fallback:
                resp = await fallback.patch(url, headers=headers, json=patch)
        else:
            resp = await client.patch(url, headers=headers, json=patch, timeout=timeout)
        resp.raise_for_status()
        return resp.json() if resp.text else {}


async def _rancher_k8s_delete(cluster_id: str, path: str, body: dict | None = None, timeout: int = 30):
    if not _rancher_enabled():
        raise RuntimeError("RANCHER_URL/RANCHER_TOKEN 未配置")
    url = f"{_rancher_base()}/k8s/clusters/{quote(cluster_id, safe='')}{path}"
    headers = {"Authorization": f"Bearer {_rancher_token()}", "Accept": "application/json"}
    async with OUTBOUND_BULKHEAD.slot():
        client = RANCHER_HTTP_CLIENT
        if client is None:
            async with httpx.AsyncClient(timeout=timeout, verify=_rancher_verify_ssl(), limits=HTTP_LIMITS) as fallback:
                resp = await fallback.request("DELETE", url, headers=headers, json=body)
        else:
            resp = await client.request("DELETE", url, headers=headers, json=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json() if resp.text else {"status": "accepted"}


async def _rancher_k8s_post(cluster_id: str, path: str, body: dict, timeout: int = 30):
    if not _rancher_enabled():
        raise RuntimeError("RANCHER_URL/RANCHER_TOKEN 未配置")
    url = f"{_rancher_base()}/k8s/clusters/{quote(cluster_id, safe='')}{path}"
    headers = {"Authorization": f"Bearer {_rancher_token()}", "Accept": "application/json", "Content-Type": "application/json"}
    async with OUTBOUND_BULKHEAD.slot():
        client = RANCHER_HTTP_CLIENT
        if client is None:
            async with httpx.AsyncClient(timeout=timeout, verify=_rancher_verify_ssl(), limits=HTTP_LIMITS) as fallback:
                resp = await fallback.post(url, headers=headers, json=body)
        else:
            resp = await client.post(url, headers=headers, json=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json() if resp.text else {"status": "accepted"}


async def _rancher_execution_configuration(cluster_id: str):
    """Generate a short-lived in-memory kubeconfig for approved exec operations."""
    if not _rancher_enabled():
        raise RuntimeError("RANCHER_URL/RANCHER_TOKEN 未配置")
    url = f"{_rancher_base()}/v3/clusters/{quote(cluster_id, safe='')}?action=generateKubeconfig"
    headers = {
        "Authorization": f"Bearer {_rancher_token()}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with OUTBOUND_BULKHEAD.slot():
        client = RANCHER_HTTP_CLIENT
        if client is None:
            async with httpx.AsyncClient(timeout=30, verify=_rancher_verify_ssl(), limits=HTTP_LIMITS) as fallback:
                response = await fallback.post(url, headers=headers, json={})
        else:
            response = await client.post(url, headers=headers, json={}, timeout=30)
        response.raise_for_status()
        payload = response.json()
    content = str(payload.get("config") or payload.get("kubeconfig") or "")
    if not content:
        raise RuntimeError("Rancher 未返回临时 kubeconfig；当前 Token 可能缺少 generateKubeconfig 权限")
    return await asyncio.to_thread(CLUSTER_REGISTRY.configuration_from_content, content)


async def _rancher_dynamic_resource_paths(
    cluster_id: str,
    *,
    api_version: str,
    kind: str,
    namespace: str = "",
    name: str = "",
) -> tuple[str, str]:
    """Resolve a Kubernetes kind through live API discovery behind Rancher."""
    api_version = str(api_version or "").strip()
    kind = str(kind or "").strip()
    if not api_version or not kind:
        raise ValueError("api_version 和 kind 不能为空")
    if api_version == "v1":
        api_root = "/api/v1"
    elif "/" in api_version:
        group, version = api_version.split("/", 1)
        if not group or not version:
            raise ValueError("api_version 格式无效")
        api_root = f"/apis/{quote(group, safe='.')}/{quote(version, safe='')}"
    else:
        raise ValueError("api_version 格式无效")
    discovery = await _rancher_k8s_get(cluster_id, api_root, timeout=20)
    resource = next(
        (
            item for item in (discovery.get("resources") or [])
            if str(item.get("kind") or "") == kind and "/" not in str(item.get("name") or "")
        ),
        None,
    )
    if not resource:
        raise ValueError(f"目标集群 API discovery 未找到 {api_version}/{kind}")
    plural = quote(str(resource.get("name") or ""), safe="")
    if resource.get("namespaced"):
        if not namespace:
            namespace = "default"
        collection = f"{api_root}/namespaces/{quote(namespace, safe='')}/{plural}"
    else:
        collection = f"{api_root}/{plural}"
    item_path = f"{collection}/{quote(name, safe='')}" if name else ""
    return collection, item_path


async def _rancher_apply_manifest(cluster_id: str, manifest: dict, namespace: str = "") -> dict:
    metadata = manifest.get("metadata") or {}
    name = str(metadata.get("name") or "")
    collection, item_path = await _rancher_dynamic_resource_paths(
        cluster_id,
        api_version=str(manifest.get("apiVersion") or ""),
        kind=str(manifest.get("kind") or ""),
        namespace=str(metadata.get("namespace") or namespace),
        name=name,
    )
    if not item_path:
        raise ValueError("manifest 缺少 metadata.name")
    try:
        resource = await _rancher_k8s_patch(
            cluster_id,
            item_path,
            manifest,
            timeout=40,
            content_type="application/merge-patch+json",
        )
        return {"operation": "patched", "resource": resource}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
    resource = await _rancher_k8s_post(cluster_id, collection, manifest, timeout=40)
    return {"operation": "created", "resource": resource}


def _normalize_rancher_cluster(item: dict, source: str) -> dict | None:
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    raw_id = str(item.get("id") or meta.get("name") or item.get("name") or "")
    cid = str(status.get("clusterName") or raw_id)
    cname = str(
        item.get("displayName")
        or spec.get("displayName")
        or item.get("name")
        or meta.get("name")
        or cid
    )
    if not cid:
        return None
    return {
        "id": cid,
        "name": cname,
        "state": item.get("state") or status.get("phase") or item.get("transitioning") or "",
        "provider": item.get("provider") or item.get("driver") or spec.get("kubernetesVersion") or "",
        "source": source,
        "raw_id": raw_id,
    }


async def _rancher_clusters() -> list[dict]:
    cache_key = f"rancher:clusters:{_rancher_base()}:{os.getenv('RANCHER_CLUSTER_IDS', 'all')}"
    cached = await RANCHER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    endpoints: list[tuple[str, str]] = []
    try:
        root = await _rancher_get("/v3", timeout=20)
        cluster_link = ((root.get("links") or {}).get("clusters") if isinstance(root, dict) else "") or ""
        if cluster_link:
            endpoints.append((cluster_link, "v3/root.links.clusters"))
    except Exception:
        pass
    endpoints.extend([
        ("/v3/clusters", "v3/clusters"),
        ("/v1/management.cattle.io.clusters?limit=-1", "v1/management.cattle.io.clusters"),
        ("/v1/provisioning.cattle.io.clusters?limit=-1", "v1/provisioning.cattle.io.clusters"),
    ])
    wanted = _wanted_rancher_clusters()
    result_by_id: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for path, source in endpoints:
        try:
            clusters, meta = await _rancher_collect(path, timeout=20)
        except Exception as exc:
            errors[source] = f"{type(exc).__name__}: {exc}"
            continue
        for item in clusters:
            cluster = _normalize_rancher_cluster(item, source)
            if not cluster:
                continue
            if wanted and cluster["id"] not in wanted and cluster["name"] not in wanted:
                continue
            result_by_id.setdefault(cluster["id"], cluster)
    result = list(result_by_id.values())
    result.sort(key=lambda c: (c["name"] == "local", c["name"]))
    await RANCHER_CACHE.set(cache_key, result)
    return result


def _node_condition_summary(node: dict) -> list[dict]:
    conditions = (node.get("status") or {}).get("conditions") or []
    negative = {"DiskPressure", "MemoryPressure", "PIDPressure", "NetworkUnavailable"}
    result = []
    for c in conditions:
        ctype = c.get("type", "")
        status = c.get("status", "Unknown")
        if ctype == "Ready":
            healthy = status == "True"
            label = "Ready" if healthy else "NotReady"
        elif ctype in negative:
            healthy = status == "False"
            label = ctype if status == "True" else f"No {ctype}"
        else:
            healthy = status != "False"
            label = ctype
        result.append({
            "type": ctype,
            "status": status,
            "healthy": healthy,
            "active": status == "True",
            "label": label,
            "reason": c.get("reason", ""),
            "message": c.get("message", ""),
        })
    return result


def _normalize_k8s_workload(kind: str, raw: dict, cluster: dict) -> dict:
    meta = raw.get("metadata") or {}
    spec = raw.get("spec") or {}
    status = raw.get("status") or {}
    pod_spec = ((spec.get("template") or {}).get("spec") or {})
    replicas = spec.get("replicas", status.get("desiredNumberScheduled", status.get("replicas", 1)))
    ready = status.get("readyReplicas", status.get("numberReady", status.get("availableReplicas", 0)))
    return {
        "cluster": cluster["name"],
        "cluster_id": cluster["id"],
        "kind": kind,
        "name": meta.get("name", ""),
        "namespace": meta.get("namespace", "default"),
        "replicas": replicas or 0,
        "ready_replicas": ready or 0,
        "available_replicas": status.get("availableReplicas", ready or 0),
        "containers": pod_spec.get("containers") or [],
        "pod_spec": {
            "securityContext": pod_spec.get("securityContext") or {},
            "imagePullSecrets": pod_spec.get("imagePullSecrets") or [],
            "nodeSelector": pod_spec.get("nodeSelector") or {},
            "tolerations": pod_spec.get("tolerations") or [],
            "affinity": pod_spec.get("affinity") or {},
            "topologySpreadConstraints": pod_spec.get("topologySpreadConstraints") or [],
            "hostNetwork": pod_spec.get("hostNetwork", False),
            "hostPID": pod_spec.get("hostPID", False),
            "hostIPC": pod_spec.get("hostIPC", False),
        },
        "strategy": spec.get("strategy") or {},
        "labels": meta.get("labels") or {},
    }


def _pod_classification_from_context(pod: dict) -> dict:
    text = " ".join([
        pod.get("namespace", ""),
        pod.get("name", ""),
        pod.get("workload_name", ""),
        " ".join(c.get("image", "") for c in pod.get("containers", [])),
    ]).lower()
    if pod.get("namespace") in {"kube-system", "cattle-system", "fleet-system", "monitoring", "k8s-agent"}:
        return {"class": "infrastructure", "label": "基础服务", "reason": "系统或平台命名空间"}
    if any(x in text for x in ["kafka", "redis", "mysql", "postgres", "elastic", "zookeeper", "mongo"]):
        return {"class": "data", "label": "中间件/数据", "reason": "名称或镜像匹配数据服务关键词"}
    if any(x in text for x in ["job", "cron", "batch"]):
        return {"class": "batch", "label": "批任务", "reason": "名称匹配任务类关键词"}
    return {"class": "application", "label": "应用服务", "reason": "默认业务应用类"}


async def _rancher_inventory_cluster(cluster: dict) -> dict:
    cid = cluster["id"]
    errors: list[str] = []

    async def get_items(path: str, label: str) -> list[dict]:
        try:
            payload = await _rancher_k8s_get(cid, path, timeout=35)
            return payload.get("items", []) if isinstance(payload, dict) else []
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            return []

    nodes_raw, ns_raw, pods_raw, deps_raw, sts_raw, ds_raw, rs_raw = await asyncio.gather(
        get_items("/api/v1/nodes", "nodes"),
        get_items("/api/v1/namespaces", "namespaces"),
        get_items("/api/v1/pods", "pods"),
        get_items("/apis/apps/v1/deployments", "deployments"),
        get_items("/apis/apps/v1/statefulsets", "statefulsets"),
        get_items("/apis/apps/v1/daemonsets", "daemonsets"),
        get_items("/apis/apps/v1/replicasets", "replicasets"),
    )

    replica_owner: dict[str, tuple[str, str]] = {}
    for rs in rs_raw:
        owners = (rs.get("metadata") or {}).get("ownerReferences") or []
        if owners:
            owner = owners[0]
            replica_owner[(rs.get("metadata") or {}).get("name", "")] = (
                owner.get("kind", "Deployment"),
                owner.get("name", ""),
            )

    pods = []
    for raw in pods_raw:
        pod = _normalize_k8s_pod(raw, replica_owner)
        pod["cluster"] = cluster["name"]
        pod["cluster_id"] = cid
        pod["workload"] = {
            "kind": pod.get("workload_kind") or "Pod",
            "name": pod.get("workload_name") or pod.get("name"),
        }
        pod["classification"] = _pod_classification_from_context(pod)
        category, severity, reason = _classify_pod_issue(pod, [])
        pod["issue"] = {"category": category, "severity": severity, "reason": reason} if category else None
        pods.append(pod)

    workloads = [
        *[_normalize_k8s_workload("Deployment", x, cluster) for x in deps_raw],
        *[_normalize_k8s_workload("StatefulSet", x, cluster) for x in sts_raw],
        *[_normalize_k8s_workload("DaemonSet", x, cluster) for x in ds_raw],
    ]

    nodes = []
    for raw in nodes_raw:
        meta = raw.get("metadata") or {}
        status = raw.get("status") or {}
        condition_summary = _node_condition_summary(raw)
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in condition_summary)
        problems = [c["type"] for c in condition_summary if c["type"] != "Ready" and c["status"] == "True"]
        if not ready:
            problems.append("NotReady")
        nodes.append({
            "cluster": cluster["name"],
            "cluster_id": cid,
            "name": meta.get("name", ""),
            "ready": ready,
            "health": "healthy" if ready and not problems else "degraded",
            "problems": problems,
            "condition_summary": condition_summary,
            "allocatable": status.get("allocatable") or {},
        })

    namespaces = [{
        "cluster": cluster["name"],
        "cluster_id": cid,
        "name": (x.get("metadata") or {}).get("name", ""),
        "status": (x.get("status") or {}).get("phase", ""),
        "created_at": (x.get("metadata") or {}).get("creationTimestamp", ""),
    } for x in ns_raw]

    return {
        "cluster": cluster,
        "errors": errors,
        "nodes": nodes,
        "namespaces": namespaces,
        "pods": pods,
        "workloads": workloads,
    }


async def _managed_inventory_cluster(cluster: dict) -> dict:
    try:
        raw = await asyncio.to_thread(CLUSTER_REGISTRY.inventory, cluster["id"])
    except Exception as exc:
        return {"cluster": cluster, "errors": [f"kubeconfig: {type(exc).__name__}: {exc}"], "nodes": [], "namespaces": [], "pods": [], "workloads": []}
    replica_owner: dict[str, tuple[str, str]] = {}
    for replica in raw.get("replicasets") or []:
        metadata = replica.get("metadata") or {}
        owners = metadata.get("ownerReferences") or []
        if owners:
            replica_owner[str(metadata.get("name") or "")] = (
                str(owners[0].get("kind") or "Deployment"),
                str(owners[0].get("name") or ""),
            )
    pods = []
    for item in raw.get("pods") or []:
        pod = _normalize_k8s_pod(item, replica_owner)
        pod.update({"cluster": cluster["name"], "cluster_id": cluster["id"]})
        pod["workload"] = {"kind": pod.get("workload_kind") or "Pod", "name": pod.get("workload_name") or pod.get("name")}
        pod["classification"] = _pod_classification_from_context(pod)
        category, severity, reason = _classify_pod_issue(pod, [])
        pod["issue"] = {"category": category, "severity": severity, "reason": reason} if category else None
        pods.append(pod)
    workloads = [
        *[_normalize_k8s_workload("Deployment", item, cluster) for item in raw.get("deployments") or []],
        *[_normalize_k8s_workload("StatefulSet", item, cluster) for item in raw.get("statefulsets") or []],
        *[_normalize_k8s_workload("DaemonSet", item, cluster) for item in raw.get("daemonsets") or []],
    ]
    nodes = []
    for item in raw.get("nodes") or []:
        meta, status = item.get("metadata") or {}, item.get("status") or {}
        summary = _node_condition_summary(item)
        ready = any(condition["type"] == "Ready" and condition["status"] == "True" for condition in summary)
        problems = [condition["type"] for condition in summary if condition["type"] != "Ready" and condition["status"] == "True"]
        if not ready:
            problems.append("NotReady")
        nodes.append({"cluster": cluster["name"], "cluster_id": cluster["id"], "name": meta.get("name", ""), "ready": ready, "health": "healthy" if ready and not problems else "degraded", "problems": problems, "condition_summary": summary, "allocatable": status.get("allocatable") or {}})
    namespaces = [{"cluster": cluster["name"], "cluster_id": cluster["id"], "name": (item.get("metadata") or {}).get("name", ""), "status": (item.get("status") or {}).get("phase", ""), "created_at": (item.get("metadata") or {}).get("creationTimestamp", "")} for item in raw.get("namespaces") or []]
    return {"cluster": cluster, "errors": [], "nodes": nodes, "namespaces": namespaces, "pods": pods, "workloads": workloads}


def _managed_cluster_metadata() -> list[dict]:
    return [{"id": item["id"], "name": item["name"], "state": item["status"], "provider": item.get("version") or "Kubernetes", "source": "kubeconfig", "raw_id": item["id"]} for item in CLUSTER_REGISTRY.list()]


async def rancher_status():
    managed = _managed_cluster_metadata()
    if not _rancher_enabled():
        return {"enabled": bool(managed), "status": "ok" if managed else "disabled", "clusters": managed, "cluster_count": len(managed), "cluster_sources": ["kubeconfig"] if managed else [], "message": f"已通过 kubeconfig 纳管 {len(managed)} 个集群。" if managed else "Rancher 尚未配置，可在 Web 页面上传 kubeconfig 纳管集群。"}
    try:
        clusters = [*(await _rancher_clusters()), *managed]
        return {
            "enabled": True,
            "status": "ok",
            "base": _rancher_base(),
            "clusters": clusters,
            "cluster_count": len(clusters),
            "cluster_sources": sorted({c.get("source", "") for c in clusters if c.get("source")}),
            "cluster_ids": [c.get("id") for c in clusters],
            "message": f"Rancher 已返回 {len(clusters)} 个集群。",
        }
    except Exception as exc:
        return {
            "enabled": True,
            "status": "degraded" if managed else "failed",
            "base": _rancher_base(),
            "error": f"{type(exc).__name__}: {exc}",
            "clusters": managed,
            "cluster_count": len(managed),
            "cluster_sources": ["kubeconfig"] if managed else [],
            "message": f"Rancher 暂不可用；{len(managed)} 个 kubeconfig 集群仍可正常纳管。" if managed else "Rancher 连接失败。",
        }


async def managed_clusters():
    """Return metadata only; encrypted kubeconfig material never leaves the server."""
    return {"items": await asyncio.to_thread(CLUSTER_REGISTRY.list)}


async def managed_cluster_contexts(req: KubeconfigContextsRequest):
    try:
        return {"contexts": await asyncio.to_thread(CLUSTER_REGISTRY.contexts, req.kubeconfig)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def upsert_managed_cluster(req: ManagedClusterUpsertRequest):
    try:
        verified = await asyncio.to_thread(
            CLUSTER_REGISTRY.save_verified,
            content=req.kubeconfig,
            name=req.name,
            context_name=req.context,
            cluster_id=req.cluster_id,
            bearer_token=req.bearer_token.get_secret_value() if req.bearer_token else "",
        )
        if verified["status"] != "connected":
            raise HTTPException(status_code=422, detail={"message": "kubeconfig 连接验证失败，未覆盖已有凭据", "cluster": verified})
        await RANCHER_INVENTORY_CACHE.invalidate()
        return verified
    except HTTPException:
        raise
    except (KeyError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def verify_managed_cluster(cluster_id: str):
    try:
        return await asyncio.to_thread(CLUSTER_REGISTRY.probe, cluster_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="集群不存在") from exc


async def refresh_managed_cluster_token(cluster_id: str, req: ManagedClusterTokenRefreshRequest):
    try:
        verified = await asyncio.to_thread(
            CLUSTER_REGISTRY.refresh_token,
            cluster_id,
            req.bearer_token.get_secret_value(),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="集群不存在") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if verified["status"] != "connected":
        raise HTTPException(status_code=422, detail={"message": "新 Token 验证失败，未覆盖已有凭据", "cluster": verified})
    await RANCHER_INVENTORY_CACHE.invalidate()
    return verified


async def delete_managed_cluster(cluster_id: str):
    deleted = await asyncio.to_thread(CLUSTER_REGISTRY.delete, cluster_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="集群不存在")
    await RANCHER_INVENTORY_CACHE.invalidate()
    await RANCHER_CACHE.invalidate()
    return {"status": "deleted", "cluster_id": cluster_id}


async def rancher_inventory():
    managed = _managed_cluster_metadata()
    if not _rancher_enabled() and not managed:
        return {
            "status": "disabled",
            "source": "rancher",
            "clusters": [],
            "inventory": [],
            "summary": {"clusters": 0, "namespaces": 0, "pods": 0, "workloads": 0, "nodes": 0},
            "message": "Rancher 尚未配置；请在 Web 页面上传 kubeconfig 纳管集群。",
        }
    cache_key = f"cluster:inventory:{_rancher_base()}:{os.getenv('RANCHER_CLUSTER_IDS', 'all')}:{','.join(item['id'] for item in managed)}"
    cached = await RANCHER_INVENTORY_CACHE.get(cache_key)
    if cached is not None:
        return {**copy.deepcopy(cached), "cache": {"hit": True, "ttl_seconds": RANCHER_INVENTORY_CACHE.ttl_seconds}}
    rancher_clusters = await _rancher_clusters() if _rancher_enabled() else []
    clusters = [*rancher_clusters, *managed]
    results = await asyncio.gather(
        *[_rancher_inventory_cluster(cluster) for cluster in rancher_clusters],
        *[_managed_inventory_cluster(cluster) for cluster in managed],
        return_exceptions=True,
    )
    inventory = []
    errors: dict[str, str] = {}
    for cluster, result in zip(clusters, results):
        if isinstance(result, Exception):
            errors[cluster["name"]] = f"{type(result).__name__}: {result}"
            continue
        inventory.append(result)
        if result.get("errors"):
            errors[cluster["name"]] = "; ".join(result["errors"])

    pods = [p for item in inventory for p in item["pods"]]
    workloads = [w for item in inventory for w in item["workloads"]]
    nodes = [n for item in inventory for n in item["nodes"]]
    namespaces = [n for item in inventory for n in item["namespaces"]]
    summary = {
        "clusters": len(clusters),
        "namespaces": len(namespaces),
        "pods": len(pods),
        "running_pods": sum(1 for p in pods if p.get("phase") == "Running"),
        "pending_pods": sum(1 for p in pods if p.get("phase") == "Pending"),
        "failed_pods": sum(1 for p in pods if p.get("phase") == "Failed"),
        "problem_pods": sum(1 for p in pods if not _pod_completed_successfully(p) and (p.get("issue") or not p.get("ready"))),
        "workloads": len(workloads),
        "healthy_workloads": sum(1 for w in workloads if int(w.get("ready_replicas") or 0) >= int(w.get("replicas") or 0)),
        "nodes": len(nodes),
        "ready_nodes": sum(1 for n in nodes if n.get("ready")),
    }
    payload = {
        "status": "ok" if not errors else "degraded",
        "source": "rancher+kubeconfig" if rancher_clusters and managed else "rancher" if rancher_clusters else "kubeconfig",
        "clusters": clusters,
        "inventory": inventory,
        "summary": summary,
        "errors": errors,
        "node_condition_standard": "Ready=True 为节点就绪；DiskPressure/MemoryPressure/PIDPressure/NetworkUnavailable=True 才是异常，False 表示无对应压力。",
    }
    await RANCHER_INVENTORY_CACHE.set(cache_key, payload)
    return {**payload, "cache": {"hit": False, "ttl_seconds": RANCHER_INVENTORY_CACHE.ttl_seconds}}


async def unified_resources(
    resource_type: str = "all",
    cluster: str = "all",
    namespace: str = "all",
    limit: int = 500,
    cursor: str = "",
):
    """聚合 Kubernetes 与全栈基础设施资源，供资源浏览器和外部系统统一调用。"""
    kubernetes_payload, infrastructure_payload = await asyncio.gather(
        rancher_inventory(),
        asyncio.to_thread(infrastructure_providers_payload),
    )
    return build_resource_catalog(
        kubernetes_payload,
        infrastructure_payload,
        resource_type=resource_type,
        cluster=cluster,
        namespace=namespace,
        limit=limit,
        cursor=cursor,
    )


async def _managed_topology_payload() -> dict:
    clusters = CLUSTER_REGISTRY.list()
    if not clusters:
        return {"status": "disabled", "enabled": False, "nodes": [], "edges": []}
    raw_allowed = {item.strip() for item in os.getenv("CMDB_ALLOWED_NAMESPACES", "all").split(",") if item.strip()}
    allowed = None if not raw_allowed or {"all", "*"} & {item.lower() for item in raw_allowed} else raw_allowed

    def build(cluster: dict) -> dict:
        inventory = CLUSTER_REGISTRY.inventory(cluster["id"])
        resources = {
            "/apis/apps/v1/deployments": {"items": inventory.get("deployments") or []},
            "/apis/apps/v1/statefulsets": {"items": inventory.get("statefulsets") or []},
            "/apis/apps/v1/daemonsets": {"items": inventory.get("daemonsets") or []},
            "/apis/apps/v1/replicasets": {"items": inventory.get("replicasets") or []},
            "/api/v1/services": {"items": inventory.get("services") or []},
            "/api/v1/pods": {"items": inventory.get("pods") or []},
            "/apis/networking.k8s.io/v1/ingresses": {"items": inventory.get("ingresses") or []},
        }
        result = _collect_cluster_topology(
            cluster["id"],
            cluster["name"],
            lambda path: resources[path],
            allowed,
        )
        for node in result.get("nodes") or []:
            if node.get("type") == "cluster":
                node.update({"kind": "KubernetesCluster", "owner": "kubeconfig"})
        return result

    results = await asyncio.gather(
        *[asyncio.to_thread(build, cluster) for cluster in clusters],
        return_exceptions=True,
    )
    nodes: list[dict] = []
    edges: list[dict] = []
    diagnostics = {"mode": "encrypted-kubeconfig", "clusters": [], "errors": {}}
    for cluster, result in zip(clusters, results):
        if isinstance(result, Exception):
            diagnostics["errors"][cluster["name"]] = f"{type(result).__name__}: {_redact_text(str(result))}"
            continue
        nodes.extend(result.get("nodes") or [])
        edges.extend(result.get("edges") or [])
        diagnostics["clusters"].append(result.get("diagnostics") or {})
    node_ids = {str(item.get("id") or "") for item in nodes}
    unique_edges = {
        (str(item.get("source") or ""), str(item.get("target") or ""), str(item.get("type") or "dependency")): item
        for item in edges
        if str(item.get("source") or "") in node_ids and str(item.get("target") or "") in node_ids
    }
    return {
        "status": "degraded" if diagnostics["errors"] else "ok" if nodes else "empty",
        "enabled": True,
        "source": "encrypted-kubeconfig",
        "message": "拓扑由已纳管集群的实时 Kubernetes 对象构建。",
        "nodes": nodes,
        "edges": list(unique_edges.values()),
        "diagnostics": diagnostics,
        "summary": {
            "clusters": sum(1 for item in nodes if item.get("type") == "cluster"),
            "applications": sum(1 for item in nodes if item.get("type") == "application"),
            "infrastructure": sum(1 for item in nodes if item.get("type") == "infrastructure"),
            "data": sum(1 for item in nodes if item.get("type") == "data"),
            "services": sum(1 for item in nodes if item.get("type") == "service"),
            "relations": len(unique_edges),
        },
    }


def _merge_topology_payload(primary: dict, extra: dict) -> dict:
    if not extra.get("nodes"):
        return primary
    nodes_by_id = {
        str(item.get("id") or ""): item
        for item in [*(primary.get("nodes") or []), *(extra.get("nodes") or [])]
        if item.get("id")
    }
    edges_by_key = {
        (str(item.get("source") or ""), str(item.get("target") or ""), str(item.get("type") or "dependency")): item
        for item in [*(primary.get("edges") or []), *(extra.get("edges") or [])]
        if item.get("source") in nodes_by_id and item.get("target") in nodes_by_id
    }
    merged = {
        **primary,
        "nodes": list(nodes_by_id.values()),
        "edges": list(edges_by_key.values()),
        "source": f"{primary.get('source') or 'cmdb'}+{extra.get('source') or 'kubeconfig'}",
        "managed_cluster_topology": extra.get("summary") or {},
    }
    merged["summary"] = {
        **(primary.get("summary") or {}),
        "clusters": sum(1 for item in merged["nodes"] if item.get("type") == "cluster"),
        "applications": sum(1 for item in merged["nodes"] if item.get("type") == "application"),
        "infrastructure": sum(1 for item in merged["nodes"] if item.get("type") == "infrastructure"),
        "data": sum(1 for item in merged["nodes"] if item.get("type") == "data"),
        "services": sum(1 for item in merged["nodes"] if item.get("type") == "service"),
        "relations": len(merged["edges"]),
    }
    return merged


async def cmdb_topology():
    """Fetch application/data-flow topology from CMDB if CMDB_URL is configured."""
    base = SERVICES.get("cmdb", "").rstrip("/")
    managed_topology = await _managed_topology_payload()
    if not base:
        if managed_topology.get("nodes"):
            return managed_topology
        ebpf_only = await _observed_flow_only_topology("CMDB_URL 未配置")
        if ebpf_only:
            return ebpf_only
        return {"status": "disabled", "enabled": False, "nodes": [], "edges": [], "message": "CMDB_URL 未配置"}
    flow_cfg = _observed_flow_endpoint_config()
    managed_ids = ",".join(item["id"] for item in CLUSTER_REGISTRY.list())
    cache_key = f"cmdb:topology:{base}:managed:{managed_ids}:ebpf:{flow_cfg.get('url_env','')}:{flow_cfg.get('url','')}:fusion:{_env_bool('EBPF_TOPOLOGY_FUSION_ENABLED', 'true')}"
    cached = await CMDB_TOPOLOGY_CACHE.get(cache_key)
    if cached is not None:
        return {**copy.deepcopy(cached), "cache": {"hit": True, "ttl_seconds": CMDB_TOPOLOGY_CACHE.ttl_seconds}}
    urls = [f"{base}/topology", f"{base}/api/topology", base]
    last_error = ""
    for url in urls:
        try:
            async with _client(45) as c:
                resp = await c.get(url)
                if resp.status_code >= 400:
                    last_error = f"{resp.status_code}: {resp.text[:200]}"
                    continue
                raw = resp.json()
                normalized = _normalize_cmdb_topology(raw)
                upstream_status = raw.get("status", "ok") if isinstance(raw, dict) else "ok"
                payload = {
                    "status": upstream_status,
                    "enabled": True,
                    "source": url,
                    "message": raw.get("message", "") if isinstance(raw, dict) else "",
                    "summary": raw.get("summary", {}) if isinstance(raw, dict) else {},
                    "diagnostics": raw.get("diagnostics", {}) if isinstance(raw, dict) else {},
                    **normalized,
                }
                payload = _merge_topology_payload(payload, managed_topology)
                if _env_bool("EBPF_TOPOLOGY_FUSION_ENABLED", "true"):
                    flow_req = ExternalTrafficFlowRequest(
                        cluster="all",
                        cluster_id="all",
                        namespace="all",
                        workload="",
                        window=os.getenv("EBPF_TOPOLOGY_WINDOW", "30m"),
                        source="observed",
                        include_static_inference=False,
                        include_cmdb=False,
                    )
                    observed_flows, observed_status = await _fetch_configured_observed_flows(flow_req)
                    flow_payload = build_external_traffic_payload(
                        [],
                        cmdb_topology={},
                        observed_flows=observed_flows,
                        scope={"cluster": "all", "namespace": "all", "window": flow_req.window},
                        options={**_external_flow_options(), "include_internal_observed": True},
                    )
                    payload = _merge_observed_flow_topology(payload, flow_payload, observed_status)
                await CMDB_TOPOLOGY_CACHE.set(cache_key, payload)
                return {**payload, "cache": {"hit": False, "ttl_seconds": CMDB_TOPOLOGY_CACHE.ttl_seconds}}
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
    if managed_topology.get("nodes"):
        managed_topology["status"] = "degraded"
        managed_topology["message"] += f" CMDB 暂不可用：{_redact_text(last_error)}"
        return managed_topology
    ebpf_only = await _observed_flow_only_topology(last_error)
    if ebpf_only:
        return ebpf_only
    return {"status": "failed", "enabled": True, "nodes": [], "edges": [], "error": last_error}


def _k8s_list_path(api_base: str, resource: str, namespace: str) -> str:
    namespace = str(namespace or "all")
    if namespace.lower() in {"", "all", "*", "所有", "所有namespace"}:
        return f"{api_base}/{resource}"
    return f"{api_base}/namespaces/{quote(namespace, safe='')}/{resource}"


def _external_flow_cluster_matches(cluster: dict, req: ExternalTrafficFlowRequest) -> bool:
    selected = {str(req.cluster or ""), str(req.cluster_id or "")}
    selected = {item for item in selected if item and item.lower() not in {"all", "*", "所有"}}
    if not selected:
        return True
    return bool(selected & {str(cluster.get("id") or ""), str(cluster.get("name") or ""), str(cluster.get("raw_id") or "")})


def _external_flow_options() -> dict:
    def parse_json_list(name: str) -> list:
        raw = os.getenv(name, "").strip()
        if not raw:
            return []
        try:
            value = json.loads(raw)
            return value if isinstance(value, list) else []
        except Exception:
            return []

    internal_domains = _csv_env("CLUSTER_INTERNAL_DOMAINS", "svc,svc.cluster.local,cluster.local")
    return {
        "internal_domains": internal_domains,
        "cross_cluster_domains": parse_json_list("EXTERNAL_FLOW_CLUSTER_DOMAINS_JSON"),
    }


def _observed_flow_endpoint_config() -> dict:
    candidates: list[tuple[str, str, str]] = []
    cni_mode = (os.getenv("CNI_PLUGIN_MODE") or os.getenv("NETWORK_PLUGIN_MODE") or "auto").strip().lower()
    provider = os.getenv("EBPF_FLOW_PROVIDER", "auto").strip().lower()

    def add(source_system: str, url_env: str, token_env: str) -> None:
        if not any(existing[1] == url_env for existing in candidates):
            candidates.append((source_system, url_env, token_env))

    if _env_bool("EBPF_FLOW_ENABLED", "true"):
        generic_label = {
            "flannel": "ebpf_flannel",
            "canal": "ebpf_canal",
            "calico": "ebpf_calico",
            "cilium": "ebpf_hubble",
        }.get(cni_mode, "ebpf_generic")
        if provider in {"auto", "flannel"} or cni_mode == "flannel":
            add("ebpf_flannel", "FLANNEL_EBPF_FLOW_URL", "FLANNEL_EBPF_FLOW_TOKEN")
        if provider in {"auto", "canal"} or cni_mode == "canal":
            add("ebpf_canal", "CANAL_EBPF_FLOW_URL", "CANAL_EBPF_FLOW_TOKEN")
            add("ebpf_calico", "CALICO_GOLDMANE_FLOW_URL", "CALICO_FLOW_TOKEN")
            add("ebpf_calico", "CALICO_FLOW_URL", "CALICO_FLOW_TOKEN")
        if provider in {"auto", "calico", "goldmane", "tigera"} or cni_mode == "calico":
            add("ebpf_calico", "CALICO_GOLDMANE_FLOW_URL", "CALICO_FLOW_TOKEN")
            add("ebpf_calico", "CALICO_FLOW_URL", "CALICO_FLOW_TOKEN")
        add(generic_label, "EBPF_FLOW_URL", "EBPF_FLOW_TOKEN")
        add("ebpf_hubble", "HUBBLE_RELAY_HTTP_URL", "HUBBLE_FLOW_TOKEN")
        add("ebpf_hubble", "HUBBLE_FLOW_URL", "HUBBLE_FLOW_TOKEN")
    add("observed_flow", "FLOW_OBSERVATION_URL", "FLOW_OBSERVATION_TOKEN")
    add("kiali", "KIALI_FLOW_URL", "KIALI_TOKEN")
    for source_system, url_env, token_env in candidates:
        url = os.getenv(url_env, "").strip()
        if url:
            token = os.getenv(token_env, "").strip()
            return {
                "source_system": source_system,
                "cni_mode": cni_mode,
                "provider": provider,
                "url_env": url_env,
                "url": url,
                "token_env": token_env,
                "token": token,
            }
    return {}


def _observed_flow_verify_ssl(source_system: str) -> bool:
    if source_system == "ebpf_calico":
        return _env_bool("CALICO_FLOW_VERIFY_SSL", "true")
    if source_system == "ebpf_canal":
        return _env_bool("CANAL_EBPF_FLOW_VERIFY_SSL", str(_env_bool("EBPF_FLOW_VERIFY_SSL", "true")).lower())
    if source_system == "ebpf_flannel":
        return _env_bool("FLANNEL_EBPF_FLOW_VERIFY_SSL", str(_env_bool("EBPF_FLOW_VERIFY_SSL", "true")).lower())
    if source_system in {"ebpf_hubble", "ebpf_generic"}:
        return _env_bool("EBPF_FLOW_VERIFY_SSL", "true")
    if source_system == "kiali":
        return _env_bool("KIALI_VERIFY_SSL", "true")
    return _env_bool("FLOW_OBSERVATION_VERIFY_SSL", str(OUTBOUND_VERIFY_SSL).lower())


def _flow_window_seconds(raw_window: str | None, default_seconds: int = 300) -> int:
    value = str(raw_window or "").strip().lower()
    if not value:
        return default_seconds
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", value)
    if not match:
        return default_seconds
    amount = max(1, int(match.group(1)))
    unit = match.group(2) or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 1)
    return min(amount * multiplier, 24 * 3600)


_BEYLA_FLOW_TOKEN_RE = re.compile(r"([A-Za-z0-9_.-]+)=((?:\"[^\"]*\")|(?:'[^']*')|[^\s]+)")


def _parse_beyla_network_flow_line(line: str, *, cluster_hint: str = "") -> dict:
    labels: dict[str, str] = {}
    for key, value in _BEYLA_FLOW_TOKEN_RE.findall(line or ""):
        labels[key] = value.strip("\"'")
    src_owner = labels.get("k8s.src.owner.name") or labels.get("src.owner.name")
    src_owner_type = labels.get("k8s.src.owner.type") or labels.get("src.owner.type")
    dst_owner = labels.get("k8s.dst.owner.name") or labels.get("dst.owner.name")
    dst_owner_type = labels.get("k8s.dst.owner.type") or labels.get("dst.owner.type")
    src_name = labels.get("k8s.src.name") or labels.get("src.name") or labels.get("src.address")
    dst_name = labels.get("k8s.dst.name") or labels.get("dst.name") or labels.get("dst.address")
    src_namespace = labels.get("k8s.src.namespace") or labels.get("src.namespace")
    dst_namespace = labels.get("k8s.dst.namespace") or labels.get("dst.namespace")
    cluster = labels.get("k8s.cluster.name") or labels.get("cluster") or cluster_hint
    protocol = labels.get("transport") or labels.get("proto") or labels.get("protocol") or labels.get("l4.protocol") or "unknown"
    destination_is_internal = bool(dst_namespace or dst_owner or labels.get("k8s.dst.type"))
    return {
        "source": {
            "cluster": cluster,
            "cluster_id": cluster,
            "namespace": src_namespace,
            "kind": src_owner_type or labels.get("k8s.src.type") or ("Workload" if src_namespace else "External"),
            "name": src_owner or src_name,
            "pod": src_name if (labels.get("k8s.src.type") or "").lower() == "pod" else labels.get("src.name", ""),
            "ip": labels.get("src.address", ""),
        },
        "destination": {
            "cluster": cluster if destination_is_internal else "",
            "cluster_id": cluster if destination_is_internal else "",
            "namespace": dst_namespace,
            "kind": dst_owner_type or labels.get("k8s.dst.type") or ("Workload" if dst_namespace else "External"),
            "name": dst_owner or dst_name,
            "pod": dst_name if (labels.get("k8s.dst.type") or "").lower() == "pod" else labels.get("dst.name", ""),
            "ip": labels.get("dst.address", ""),
            "port": labels.get("dst.port") or labels.get("destination.port") or labels.get("dport"),
            "protocol": protocol,
            "type": "kubernetes" if destination_is_internal else "external_ip",
        },
        "protocol": protocol,
        "destination_port": labels.get("dst.port") or labels.get("destination.port") or labels.get("dport"),
        "bytes": labels.get("bytes") or labels.get("beyla.network.flow.bytes") or labels.get("value"),
        "direction": "internal" if destination_is_internal else "egress",
        "source_system": "beyla",
        "confidence": 0.98,
        "evidence": [line[:900]],
        "raw_labels": labels,
    }


async def _fetch_beyla_loki_flows(req: ExternalTrafficFlowRequest) -> tuple[list[dict], list[dict]]:
    if not _env_bool("BEYLA_LOKI_FLOW_ENABLED", "true") or req.source == "static":
        return [], []
    loki_url = SERVICES.get("loki", "").rstrip("/")
    if not loki_url:
        return [], [{
            "id": "ebpf_beyla",
            "status": "not_configured",
            "hint": "LOKI_URL 未配置。若使用 manifests/ebpf-beyla.yaml，请同时部署 grafana-observability.yaml，让 Alloy 收集 Beyla flow 日志到 Loki。",
        }]

    namespace = os.getenv("BEYLA_LOKI_NAMESPACE", "flawless-ebpf").strip() or "flawless-ebpf"
    pod_selector = os.getenv("BEYLA_LOKI_POD_SELECTOR", "flawless-beyla.*").strip() or "flawless-beyla.*"
    query = os.getenv("BEYLA_LOKI_QUERY", "").strip() or f'{{namespace="{namespace}",pod=~"{pod_selector}"}} |= "network_flow:"'
    limit = max(1, min(int(os.getenv("BEYLA_LOKI_FLOW_LIMIT", "500") or "500"), 5000))
    seconds = _flow_window_seconds(req.window or os.getenv("BEYLA_FLOW_QUERY_WINDOW", "5m"), 300)
    end_ns = int(time.time() * 1_000_000_000)
    start_ns = end_ns - seconds * 1_000_000_000
    params = {
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(limit),
        "direction": "BACKWARD",
    }
    try:
        async with _client(12) as c:
            resp = await c.get(f"{loki_url}/loki/api/v1/query_range", params=params)
            resp.raise_for_status()
            payload = resp.json()
        raw_flows: list[dict] = []
        for stream in (((payload or {}).get("data") or {}).get("result") or []):
            values = stream.get("values") if isinstance(stream, dict) else []
            for entry in values or []:
                if not isinstance(entry, list) or len(entry) < 2:
                    continue
                line = str(entry[1] or "")
                if "network_flow:" not in line:
                    continue
                raw_flows.append(_parse_beyla_network_flow_line(line, cluster_hint=req.cluster_id or req.cluster or ""))
        flows = normalize_observed_flow_payload(
            raw_flows,
            source_system="beyla",
            cluster_hint=req.cluster_id or req.cluster or "",
            default_namespace=req.namespace or "",
        )
        return flows, [{
            "id": "ebpf_beyla",
            "status": "connected" if flows else "empty",
            "mode": "loki_network_flow_logs",
            "namespace": namespace,
            "query": query,
            "window": f"{seconds}s",
            "lines": len(raw_flows),
            "flows": len(flows),
            "hint": "" if flows else "Beyla 已接入但最近窗口没有 network_flow 日志；可先访问几个业务接口，或检查 flawless-beyla DaemonSet 日志。",
        }]
    except Exception as exc:
        return [], [{
            "id": "ebpf_beyla",
            "status": "failed",
            "mode": "loki_network_flow_logs",
            "query": query,
            "error": f"{type(exc).__name__}: {exc}",
        }]


def _merge_observed_flow_topology(topology: dict, flow_payload: dict, source_status: list[dict]) -> dict:
    """Merge observed eBPF flow graph into CMDB topology without changing the UI contract."""
    if not isinstance(topology, dict) or not isinstance(flow_payload, dict):
        return topology
    observed_source = next((str(item.get("id") or "") for item in source_status if isinstance(item, dict) and item.get("id")), "observed_flow")
    graph = flow_payload.get("graph") if isinstance(flow_payload.get("graph"), dict) else {}
    flow_nodes = [item for item in (graph.get("nodes") or []) if isinstance(item, dict)]
    flow_edges = [item for item in (graph.get("edges") or []) if isinstance(item, dict)]
    if not flow_nodes and not flow_edges:
        diagnostics = dict(topology.get("diagnostics") or {})
        diagnostics["ebpf_flow_status"] = source_status
        topology["diagnostics"] = diagnostics
        return topology

    nodes_by_id: dict[str, dict] = {
        str(node.get("id")): dict(node)
        for node in (topology.get("nodes") or [])
        if isinstance(node, dict) and node.get("id")
    }
    for node in flow_nodes:
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        existing = nodes_by_id.get(node_id, {})
        nodes_by_id[node_id] = {
            **existing,
            "id": node_id,
            "name": existing.get("name") or node.get("title") or node.get("name") or node_id,
            "type": existing.get("type") or node.get("type") or "workload",
            "namespace": existing.get("namespace") or node.get("namespace") or "",
            "cluster": existing.get("cluster") or node.get("cluster") or "",
            "cluster_id": existing.get("cluster_id") or node.get("cluster_id") or node.get("cluster") or "",
            "kind": existing.get("kind") or node.get("kind") or node.get("type") or "",
            "source_system": existing.get("source_system") or observed_source,
            "observed": bool(existing.get("observed") or True),
            "raw": existing.get("raw") or node,
        }

    edge_keys = {
        (
            str(edge.get("source") or ""),
            str(edge.get("target") or ""),
            str(edge.get("type") or ""),
            str(edge.get("source_system") or ""),
        )
        for edge in (topology.get("edges") or [])
        if isinstance(edge, dict)
    }
    merged_edges = [edge for edge in (topology.get("edges") or []) if isinstance(edge, dict)]
    for edge in flow_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if not source or not target:
            continue
        key = (source, target, "observed_flow", str(edge.get("source_system") or observed_source))
        if key in edge_keys:
            continue
        edge_keys.add(key)
        merged_edges.append({
            "source": source,
            "target": target,
            "type": "observed_flow",
            "traffic": edge.get("rps") or edge.get("bytes") or "",
            "protocol": edge.get("protocol") or "",
            "port": edge.get("port"),
            "direction": edge.get("direction") or "",
            "source_system": edge.get("source_system") or observed_source,
            "observed": True,
            "raw": edge,
        })

    summary = dict(topology.get("summary") or {})
    summary["ebpf_observed_nodes"] = len(flow_nodes)
    summary["ebpf_observed_edges"] = len(flow_edges)
    summary["nodes"] = len(nodes_by_id)
    summary["edges"] = len(merged_edges)
    diagnostics = dict(topology.get("diagnostics") or {})
    diagnostics["ebpf_flow_status"] = source_status
    diagnostics["ebpf_fusion"] = {
        "enabled": True,
        "mode": flow_payload.get("mode"),
        "summary": flow_payload.get("summary") or {},
        "note": "Topology contains CMDB relations plus observed eBPF/CNI data-flow edges.",
    }
    data_sources = set(topology.get("data_sources") or [])
    if not data_sources and (topology.get("nodes") or topology.get("edges")):
        data_sources.add("cmdb")
    data_sources.add(observed_source)
    return {
        **topology,
        "nodes": list(nodes_by_id.values()),
        "edges": merged_edges,
        "summary": summary,
        "diagnostics": diagnostics,
        "data_sources": sorted(data_sources),
    }


async def _observed_flow_only_topology(cmdb_error: str = "") -> dict:
    cfg = _observed_flow_endpoint_config()
    if not cfg and not _env_bool("BEYLA_LOKI_FLOW_ENABLED", "true"):
        return {}
    flow_req = ExternalTrafficFlowRequest(
        cluster="all",
        cluster_id="all",
        namespace="all",
        workload="",
        window=os.getenv("EBPF_TOPOLOGY_WINDOW", "30m"),
        source="observed",
        include_static_inference=False,
        include_cmdb=False,
    )
    observed_flows, observed_status = await _fetch_configured_observed_flows(flow_req)
    flow_payload = build_external_traffic_payload(
        [],
        cmdb_topology={},
        observed_flows=observed_flows,
        scope={"cluster": "all", "namespace": "all", "window": flow_req.window},
        options={**_external_flow_options(), "include_internal_observed": True},
    )
    base_topology = {
        "status": "ok" if observed_flows else "degraded",
        "enabled": True,
        "source": cfg.get("url") or "beyla-loki",
        "message": "CMDB 不可用，已使用 eBPF/CNI 真实观测流量生成临时拓扑。" if cmdb_error else "已使用 eBPF/CNI 真实观测流量生成拓扑。",
        "summary": {},
        "diagnostics": {"cmdb_error": cmdb_error} if cmdb_error else {},
        "nodes": [],
        "edges": [],
        "data_sources": [str(cfg.get("source_system") or "ebpf_beyla")],
    }
    return _merge_observed_flow_topology(base_topology, flow_payload, observed_status)


async def _collect_rancher_external_flow_resources(req: ExternalTrafficFlowRequest) -> tuple[list[dict], dict[str, str]]:
    clusters = [cluster for cluster in await _rancher_clusters() if _external_flow_cluster_matches(cluster, req)]
    errors: dict[str, str] = {}

    async def collect_cluster(cluster: dict) -> dict:
        cid = cluster["id"]

        async def get_items(path: str, label: str) -> list[dict]:
            try:
                payload = await _rancher_k8s_get(cid, path, timeout=35)
                return payload.get("items", []) if isinstance(payload, dict) else []
            except Exception as exc:
                errors[f"{cluster.get('name') or cid}:{label}"] = f"{type(exc).__name__}: {exc}"
                return []

        namespace = req.namespace or "all"
        pods, services, endpoints, endpoint_slices, ingresses, network_policies = await asyncio.gather(
            get_items(_k8s_list_path("/api/v1", "pods", namespace), "pods"),
            get_items(_k8s_list_path("/api/v1", "services", namespace), "services"),
            get_items(_k8s_list_path("/api/v1", "endpoints", namespace), "endpoints"),
            get_items(_k8s_list_path("/apis/discovery.k8s.io/v1", "endpointslices", namespace), "endpointslices"),
            get_items(_k8s_list_path("/apis/networking.k8s.io/v1", "ingresses", namespace), "ingresses"),
            get_items(_k8s_list_path("/apis/networking.k8s.io/v1", "networkpolicies", namespace), "networkpolicies"),
        )
        return {
            "cluster": cluster,
            "pods": pods,
            "services": services,
            "endpoints": endpoints,
            "endpoint_slices": endpoint_slices,
            "ingresses": ingresses,
            "network_policies": network_policies,
            "source": "rancher",
        }

    results = await asyncio.gather(*(collect_cluster(cluster) for cluster in clusters), return_exceptions=True)
    resources = []
    for cluster, result in zip(clusters, results):
        if isinstance(result, Exception):
            errors[cluster.get("name") or cluster.get("id") or "cluster"] = f"{type(result).__name__}: {result}"
        else:
            resources.append(result)
    return resources, errors


async def _collect_mcp_external_flow_resources(req: ExternalTrafficFlowRequest) -> tuple[list[dict], dict[str, str]]:
    result = await _call_mcp_tool("get_external_traffic_candidates", {
        "namespace": req.namespace or "all",
    })
    if result.get("error"):
        return [], {"mcp": str(result.get("detail") or result.get("error"))}
    resources = result.get("resources") if isinstance(result, dict) else None
    if isinstance(resources, list):
        return resources, {}
    return [], {"mcp": "MCP did not return external traffic resources"}


async def _fetch_configured_observed_flows(req: ExternalTrafficFlowRequest) -> tuple[list[dict], list[dict]]:
    cfg = _observed_flow_endpoint_config()
    url = str(cfg.get("url") or "")
    if not url or req.source == "static":
        beyla_flows, beyla_status = await _fetch_beyla_loki_flows(req)
        if beyla_status:
            if beyla_flows or req.source != "static":
                return beyla_flows, beyla_status
        return [], [{
            "id": "observed_flow",
            "status": "not_configured",
            "hint": "配置 EBPF_FLOW_URL/FLANNEL_EBPF_FLOW_URL/CANAL_EBPF_FLOW_URL/CALICO_FLOW_URL/HUBBLE_FLOW_URL 后可接入真实网络流；也可以直接 kubectl apply -f manifests/ebpf-beyla.yaml，从 0 部署 Beyla eBPF Collector。",
        }]
    source_system = str(cfg.get("source_system") or "observed_flow")
    headers = {"Accept": "application/json"}
    token = str(cfg.get("token") or "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {
        "cluster": req.cluster_id or req.cluster or "all",
        "namespace": req.namespace or "all",
        "workload": req.workload or "",
        "window": req.window or "30m",
        "follow": "false",
    }
    try:
        verify_ssl = _observed_flow_verify_ssl(source_system)
        if verify_ssl == OUTBOUND_VERIFY_SSL:
            async with _client(12) as c:
                resp = await c.get(url, params=params, headers=headers)
                resp.raise_for_status()
                raw = resp.json()
        else:
            timeout = httpx.Timeout(12.0, connect=5.0, pool=2.0)
            async with httpx.AsyncClient(timeout=timeout, verify=verify_ssl, limits=HTTP_LIMITS) as c:
                resp = await c.get(url, params=params, headers=headers)
                resp.raise_for_status()
                raw = resp.json()
        flows = normalize_observed_flow_payload(
            raw,
            source_system=source_system,
            cluster_hint=req.cluster_id or req.cluster or "",
            default_namespace=req.namespace or "",
        )
        return flows, [{
            "id": source_system,
            "status": "connected",
            "url": url,
            "url_env": cfg.get("url_env"),
            "cni_mode": cfg.get("cni_mode"),
            "provider": cfg.get("provider"),
            "flows": len(flows),
            "verify_ssl": verify_ssl,
        }]
    except Exception as exc:
        return [], [{
            "id": source_system,
            "status": "failed",
            "url": url,
            "url_env": cfg.get("url_env"),
            "cni_mode": cfg.get("cni_mode"),
            "provider": cfg.get("provider"),
            "error": f"{type(exc).__name__}: {exc}",
        }]


async def external_traffic_flows(req: ExternalTrafficFlowRequest):
    """Return only traffic flows crossing the cluster boundary or cluster boundary."""
    cache_key = json.dumps({
        "cluster": req.cluster,
        "cluster_id": req.cluster_id,
        "namespace": req.namespace,
        "workload": req.workload,
        "window": req.window,
        "source": req.source,
        "static": req.include_static_inference,
        "cmdb": req.include_cmdb,
        "rancher": _rancher_base(),
        "flow_endpoint": _observed_flow_endpoint_config().get("url", ""),
        "flow_source": _observed_flow_endpoint_config().get("source_system", ""),
    }, ensure_ascii=False, sort_keys=True)
    cached = await EXTERNAL_TRAFFIC_CACHE.get(cache_key)
    if cached is not None:
        return {**copy.deepcopy(cached), "cache": {"hit": True, "ttl_seconds": EXTERNAL_TRAFFIC_CACHE.ttl_seconds}}

    collection_errors: dict[str, str] = {}
    resources: list[dict] = []
    if req.include_static_inference:
        if _rancher_enabled():
            resources, collection_errors = await _collect_rancher_external_flow_resources(req)
        else:
            resources, collection_errors = await _collect_mcp_external_flow_resources(req)

    cmdb_payload = await cmdb_topology() if req.include_cmdb else {}
    observed_flows, source_status = await _fetch_configured_observed_flows(req)
    scope = {
        "cluster": req.cluster_id or req.cluster or "all",
        "cluster_label": req.cluster or req.cluster_id or "all",
        "namespace": req.namespace or "all",
        "workload": req.workload or "",
        "window": req.window or "30m",
    }
    payload = build_external_traffic_payload(
        resources,
        cmdb_topology=cmdb_payload if isinstance(cmdb_payload, dict) else {},
        observed_flows=observed_flows,
        scope=scope,
        options={**_external_flow_options(), "include_internal_observed": False},
    )
    if collection_errors:
        payload["status"] = "degraded" if payload["flows"] else "failed"
    payload["collection_errors"] = collection_errors
    payload["data_source_status"] = [
        {"id": "rancher", "status": "connected" if _rancher_enabled() else "not_configured", "resources": sum(len(item.get("pods") or []) + len(item.get("services") or []) for item in resources)},
        {"id": "cmdb", "status": cmdb_payload.get("status", "disabled") if isinstance(cmdb_payload, dict) else "disabled", "nodes": len(cmdb_payload.get("nodes") or []) if isinstance(cmdb_payload, dict) else 0},
        *source_status,
    ]
    payload["message"] = (
        "已输出集群边界外的数据流。observed 表示 flannel/canal/calico/cilium 对应 eBPF Collector、Calico Flow 或 Kiali 等真实网络观测，inferred 表示 K8s/CMDB 配置推断。"
        if payload["summary"]["total"]
        else "当前范围没有发现集群外或跨集群数据流；flannel/canal/calico 环境如需真实字节级流量，请接入企业 eBPF Collector、Calico Flow/Goldmane、Kiali 或 Flow Observation。"
    )
    await EXTERNAL_TRAFFIC_CACHE.set(cache_key, payload)
    return {**payload, "cache": {"hit": False, "ttl_seconds": EXTERNAL_TRAFFIC_CACHE.ttl_seconds}}


async def _prom_query(expr: str) -> float | None:
    base = SERVICES.get("prometheus", "").rstrip("/")
    if not base:
        return None
    async with _client(8) as c:
        resp = await c.get(f"{base}/api/v1/query", params={"query": expr})
        resp.raise_for_status()
        payload = resp.json()
    result = (((payload.get("data") or {}).get("result") or []))
    if not result:
        return 0.0
    try:
        return float(result[0]["value"][1])
    except Exception:
        return 0.0


def _prometheus_cluster_labels() -> list[str]:
    raw = os.getenv("PROMETHEUS_CLUSTER_LABELS") or os.getenv("PROMETHEUS_CLUSTER_LABEL", "cluster")
    values = []
    for item in raw.split(","):
        item = item.strip()
        if item and item not in values:
            values.append(item)
    for fallback in ["cluster", "cluster_id", "rancher_cluster_id", "cluster_name"]:
        if fallback not in values:
            values.append(fallback)
    return values


def _metric_selector(extra: str = "", cluster_pattern: str = "", cluster_label: str | None = None) -> str:
    parts = [p for p in [extra] if p]
    if cluster_pattern:
        parts.append(f'{cluster_label or os.getenv("PROMETHEUS_CLUSTER_LABEL", "cluster")}=~"{cluster_pattern}"')
    return "{" + ",".join(parts) + "}"


async def _prometheus_cluster_pattern(cluster: str) -> str:
    if not cluster or cluster in {"all", "*", "所有"}:
        return ""
    candidates = {str(cluster)}
    if _rancher_enabled():
        try:
            for item in await _rancher_clusters():
                if cluster in {item.get("id"), item.get("name")}:
                    candidates.add(str(item.get("id", "")))
                    candidates.add(str(item.get("name", "")))
        except Exception:
            pass
    return "|".join(re.escape(x) for x in candidates if x)


def _prometheus_queries(cluster_pattern: str = "", cluster_label: str | None = None) -> dict[str, str]:
    container_sel = _metric_selector('container!="",pod!=""', cluster_pattern, cluster_label)
    pod_ready_sel = _metric_selector('condition="false"', cluster_pattern, cluster_label)
    return {
        "cpu_cores": f"sum(rate(container_cpu_usage_seconds_total{container_sel}[5m]))",
        "memory_bytes": f"sum(container_memory_working_set_bytes{container_sel})",
        "pod_restarts_1h": f"sum(increase(kube_pod_container_status_restarts_total{_metric_selector('', cluster_pattern, cluster_label)}[1h]))",
        "not_ready_pods": f"sum(kube_pod_status_ready{pod_ready_sel})",
    }


async def _run_prometheus_queries_with_errors(queries: dict[str, str]) -> tuple[dict[str, float | None], dict[str, str]]:
    values = {}
    errors: dict[str, str] = {}
    for key, expr in queries.items():
        try:
            values[key] = await _prom_query(expr)
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:260] if exc.response is not None else ""
            errors[key] = f"HTTP {exc.response.status_code if exc.response is not None else '?'}: {body}"
            values[key] = None
        except Exception as exc:
            errors[key] = f"{type(exc).__name__}: {exc}"
            values[key] = None
    return values, errors


async def _run_prometheus_queries(queries: dict[str, str]) -> dict[str, float | None]:
    values, _ = await _run_prometheus_queries_with_errors(queries)
    return values


async def _rancher_metrics_fallback(cluster: str = "all") -> dict:
    if not _rancher_enabled():
        return {"enabled": False, "reason": "Rancher not configured"}
    clusters = await _rancher_clusters()
    if cluster and cluster not in {"all", "*", "所有"}:
        clusters = [c for c in clusters if cluster in {c.get("id"), c.get("name")}]
    cpu_m = 0.0
    memory = 0.0
    restarts = 0
    not_ready = 0
    errors: dict[str, str] = {}
    for c in clusters:
        cid = c["id"]
        try:
            metrics = await _rancher_k8s_get(cid, "/apis/metrics.k8s.io/v1beta1/pods", timeout=20)
            for pod in metrics.get("items", []) if isinstance(metrics, dict) else []:
                for container in pod.get("containers", []):
                    usage = container.get("usage") or {}
                    cpu_m += _cpu_to_millicores(usage.get("cpu"))
                    memory += _quantity_to_bytes(usage.get("memory"))
        except Exception as exc:
            errors[c.get("name", cid)] = f"metrics: {type(exc).__name__}: {exc}"
        try:
            pods = await _rancher_k8s_get(cid, "/api/v1/pods", timeout=25)
            for raw in pods.get("items", []) if isinstance(pods, dict) else []:
                pod = _normalize_k8s_pod(raw, {})
                restarts += int(pod.get("restart_count") or 0)
                if not pod.get("ready"):
                    not_ready += 1
        except Exception as exc:
            errors[c.get("name", cid)] = (errors.get(c.get("name", cid), "") + f"; pods: {type(exc).__name__}: {exc}").strip("; ")
    return {
        "enabled": True,
        "source": "rancher-metrics-api",
        "clusters": clusters,
        "errors": errors,
        "values": {
            "cpu_cores": round(cpu_m / 1000, 4),
            "memory_bytes": memory,
            "pod_restarts_1h": restarts,
            "not_ready_pods": not_ready,
        },
    }


async def prometheus_summary(cluster: str = "all"):
    """Prometheus-backed dashboard summary. Disabled cleanly when PROMETHEUS_URL is absent."""
    cache_key = f"prometheus:summary:{SERVICES.get('prometheus', '')}:{cluster}:{os.getenv('PROMETHEUS_CLUSTER_LABEL', 'cluster')}"
    cached = await PROMETHEUS_SUMMARY_CACHE.get(cache_key)
    if cached is not None:
        return {**copy.deepcopy(cached), "cache": {"hit": True, "ttl_seconds": PROMETHEUS_SUMMARY_CACHE.ttl_seconds}}

    async def cached_response(payload: dict) -> dict:
        await PROMETHEUS_SUMMARY_CACHE.set(cache_key, payload)
        return {**payload, "cache": {"hit": False, "ttl_seconds": PROMETHEUS_SUMMARY_CACHE.ttl_seconds}}

    if not SERVICES.get("prometheus"):
        fallback = await _rancher_metrics_fallback(cluster)
        if fallback.get("enabled"):
            return await cached_response({"status": "ok", "enabled": True, "source": "rancher-metrics-api", "message": "PROMETHEUS_URL 未配置，已使用 Rancher metrics API 汇总。", **fallback})
        return await cached_response({"status": "disabled", "enabled": False, "message": "PROMETHEUS_URL 未配置"})
    cluster_pattern = await _prometheus_cluster_pattern(cluster)
    query_attempts = []
    labels = [""] if not cluster_pattern else _prometheus_cluster_labels()
    try:
        values = {}
        queries = {}
        query_errors = {}
        selected_label = os.getenv("PROMETHEUS_CLUSTER_LABEL", "cluster")
        for label in labels:
            queries = _prometheus_queries(cluster_pattern, label or None)
            values, query_errors = await _run_prometheus_queries_with_errors(queries)
            query_attempts.append({"cluster_label": label or "none", "queries": queries, "values": values, "errors": query_errors})
            if not cluster_pattern or any(float(v or 0) > 0 for v in values.values()):
                selected_label = label or ""
                break
        if not cluster_pattern and query_errors and all(values.get(k) is None for k in ("cpu_cores", "memory_bytes", "pod_restarts_1h", "not_ready_pods")):
            rancher_fallback = await _rancher_metrics_fallback(cluster)
            if rancher_fallback.get("enabled"):
                return await cached_response({
                    "status": "ok",
                    "enabled": True,
                    "cluster": cluster,
                    "source": "rancher-metrics-api",
                    "cluster_label": selected_label,
                    "cluster_pattern": cluster_pattern,
                    "fallback_to_rancher": True,
                    "message": "Prometheus 核心指标查询失败，已通过 Rancher metrics API 汇总。",
                    **rancher_fallback,
                    "query_attempts": query_attempts,
                })
        fallback_to_global = False
        fallback_queries = {}
        if cluster_pattern and all(float(values.get(k) or 0) == 0 for k in ("cpu_cores", "memory_bytes", "pod_restarts_1h", "not_ready_pods")):
            fallback_queries = _prometheus_queries("")
            fallback_values, fallback_errors = await _run_prometheus_queries_with_errors(fallback_queries)
            if fallback_errors:
                query_attempts.append({"cluster_label": "global", "queries": fallback_queries, "values": fallback_values, "errors": fallback_errors})
            if any(float(v or 0) > 0 for v in fallback_values.values()):
                values = fallback_values
                fallback_to_global = True
            else:
                rancher_fallback = await _rancher_metrics_fallback(cluster)
                if rancher_fallback.get("enabled"):
                    return await cached_response({
                        "status": "ok",
                        "enabled": True,
                        "cluster": cluster,
                        "source": "rancher-metrics-api",
                        "cluster_label": selected_label,
                        "cluster_pattern": cluster_pattern,
                        "fallback_to_rancher": True,
                        "message": "Prometheus 未匹配到多集群指标或查询失败，已通过 Rancher metrics API 汇总。",
                        **rancher_fallback,
                        "query_attempts": query_attempts,
                    })
        return await cached_response({
            "status": "ok",
            "enabled": True,
            "cluster": cluster,
            "source": "prometheus",
            "cluster_label": selected_label,
            "cluster_pattern": cluster_pattern,
            "fallback_to_global": fallback_to_global,
            "prometheus_errors": query_errors,
            "values": values,
            "queries": queries,
            "fallback_queries": fallback_queries,
            "query_attempts": query_attempts,
            "message": "Prometheus 指标未匹配到所选集群标签，已回退到未按集群切分的全局指标。" if fallback_to_global else "",
        })
    except Exception as e:
        rancher_fallback = await _rancher_metrics_fallback(cluster)
        if rancher_fallback.get("enabled"):
            return await cached_response({
                "status": "ok",
                "enabled": True,
                "cluster": cluster,
                "source": "rancher-metrics-api",
                "fallback_to_rancher": True,
                "message": "Prometheus 查询异常，已自动降级为 Rancher metrics API。",
                "prometheus_error": f"{type(e).__name__}: {e}",
                **rancher_fallback,
            })
        return await cached_response({"status": "failed", "enabled": True, "error": f"{type(e).__name__}: {e}", "values": {}})


def _finding_id(kind: str, namespace: str, name: str, reason: str) -> str:
    return f"{kind}:{namespace}:{name}:{reason}".lower()


def _increased_memory_quantity(value: str | None) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTE]i?)", text, re.I)
    if not match:
        return os.getenv("AUTO_OPS_DEFAULT_MEMORY_LIMIT", "1Gi")
    original = float(match.group(1))
    amount = original * float(os.getenv("AUTO_OPS_MEMORY_GROWTH_FACTOR", "1.5"))
    cap_gi = float(os.getenv("AUTO_OPS_MAX_MEMORY_GI", "8"))
    unit = match.group(2)
    if unit.lower() == "mi":
        amount = max(amount, original + float(os.getenv("AUTO_OPS_MIN_MEMORY_BUMP_MI", "256")))
    if unit.lower() == "gi":
        amount = min(amount, cap_gi)
    rounded = int(amount) if amount.is_integer() else round(amount, 1)
    return f"{rounded}{unit}"


def _probe_patch_from_container(container: dict) -> dict:
    """从已有探针复制 handler，生成 Kubernetes 可接受的探针调优 patch。"""
    handler_keys = ("httpGet", "tcpSocket", "exec", "grpc")
    source = (
        container.get("startupProbe")
        or container.get("startup_probe")
        or container.get("livenessProbe")
        or container.get("liveness_probe")
        or container.get("readinessProbe")
        or container.get("readiness_probe")
        or {}
    )
    if not isinstance(source, dict):
        source = {}
    handler = {key: copy.deepcopy(source[key]) for key in handler_keys if source.get(key)}
    if handler:
        probe = {
            **handler,
            "failureThreshold": max(30, int(source.get("failureThreshold") or source.get("failure_threshold") or 30)),
            "periodSeconds": max(1, int(source.get("periodSeconds") or source.get("period_seconds") or 10)),
        }
        if source.get("timeoutSeconds") is not None:
            probe["timeoutSeconds"] = source.get("timeoutSeconds")
        return {"startupProbe": probe}
    patch: dict[str, dict] = {}
    if container.get("livenessProbe") or container.get("liveness_probe"):
        patch["livenessProbe"] = {
            "initialDelaySeconds": max(60, int(os.getenv("AUTO_OPS_PROBE_INITIAL_DELAY_SECONDS", "60")))
        }
    if container.get("readinessProbe") or container.get("readiness_probe"):
        patch["readinessProbe"] = {
            "initialDelaySeconds": max(30, int(os.getenv("AUTO_OPS_READINESS_INITIAL_DELAY_SECONDS", "30")))
        }
    return patch


def _container_patch_base(container_name: str, container: dict) -> dict:
    """JSON merge patch 会替换 containers 列表，因此容器 patch 必须保留 image。"""
    patch = {"name": container_name}
    image = str((container or {}).get("image") or "").strip()
    if image:
        patch["image"] = image
    return patch


def _expert_playbook_catalog() -> list[dict]:
    return [
        {
            "category": "CrashLoop/OOM/启动失败",
            "diagnostics": ["previous logs", "Events", "退出码/OOMKilled", "探针", "资源限制", "配置/Secret/PVC"],
            "safe_mutations": ["resources request/limit 调整", "startupProbe 容错窗口", "滚动重启加载配置"],
            "human_required": ["应用代码缺陷", "镜像版本回退", "业务配置语义变更"],
        },
        {
            "category": "镜像拉取失败",
            "diagnostics": ["ErrImagePull Events", "registry DNS/网络", "imagePullSecret", "tag/manifest"],
            "safe_mutations": ["在明确默认凭据时 patch imagePullSecrets"],
            "human_required": ["新建镜像仓库凭据", "替换未知镜像 tag"],
        },
        {
            "category": "存储/配置",
            "diagnostics": ["FailedMount/Attach", "PVC/PV bound", "ConfigMap/Secret 是否存在", "volumeMount 权限"],
            "safe_mutations": ["基于实时证据和动态 Skill 的最小 YAML Patch", "startupProbe 延长等待"],
            "human_required": ["底层 NFS/Ceph/宿主机目录权限", "缺失配置内容补齐"],
        },
        {
            "category": "调度/容量/高 CPU",
            "diagnostics": ["scheduler Events", "节点压力", "配额", "HPA", "Pod metrics"],
            "safe_mutations": ["临时扩容 replicas", "资源 requests/limits 右调", "受控 tolerations/nodeSelector 变更"],
            "human_required": ["节点池扩容", "跨可用区调度策略调整"],
        },
        {
            "category": "网络/依赖",
            "diagnostics": ["Service/Endpoint", "DNS", "NetworkPolicy", "Ingress/Service Mesh", "Kafka/数据库连接"],
            "safe_mutations": ["无充分证据时不自动改网络策略，只生成验证清单"],
            "human_required": ["生产网络策略放通", "外部依赖账号/ACL/防火墙变更"],
        },
        {
            "category": "发布回归/PDB 死锁",
            "diagnostics": ["revision/镜像摘要", "故障时间线", "ReplicaSet rollout", "PDB disruptionsAllowed", "业务 SLI"],
            "safe_mutations": ["有上一不可变镜像证据时生成回滚", "经副本预算计算后调整 PDB"],
            "human_required": ["镜像回滚", "PDB 阈值变更", "新 Workload 创建"],
        },
        {
            "category": "控制面/准入/证书",
            "diagnostics": ["API Server latency", "admission webhook", "CABundle/SAN/有效期", "controller leader election", "etcd signals"],
            "safe_mutations": ["默认只读诊断、隔离故障范围、给出精确权限申请"],
            "human_required": ["Webhook failurePolicy", "证书轮换", "控制面组件变更"],
        },
        {
            "category": "节点恢复与容量治理",
            "diagnostics": ["Ready/Pressure 条件", "系统 Pod", "磁盘 inode", "CNI/CSI", "可驱逐工作负载"],
            "safe_mutations": ["证据充分时 cordon", "恢复验证后 uncordon", "PDB 约束下逐 Pod 驱逐"],
            "human_required": ["节点重启", "内核/磁盘修复", "节点池扩缩容"],
        },
    ]


def _workload_patch_change(namespace: str, workload_type: str, workload_name: str, patch: dict, reason: str) -> dict:
    return {
        "type": "patch_workload",
        "namespace": namespace,
        "workload_type": workload_type,
        "workload_name": workload_name,
        "reason": reason,
        "patch": patch,
    }


def _workload_patchable(kind: str) -> bool:
    return str(kind or "").lower() in {"deployment", "statefulset", "daemonset", "replicaset"}


def _pod_completed_successfully(pod: dict) -> bool:
    phase = str(pod.get("phase") or "")
    if phase in {"Succeeded", "Completed"}:
        return True
    containers = pod.get("containers") or []
    return bool(containers) and all(
        str((c.get("state_detail") or {}).get("terminated_reason") or c.get("reason") or "") == "Completed"
        for c in containers
    )


def _image_tag_is_mutable(image: str) -> bool:
    image = str(image or "").strip()
    if not image:
        return True
    tag = image.rsplit(":", 1)[-1] if ":" in image.rsplit("/", 1)[-1] else ""
    return not tag or tag.lower() in {"latest", "dev", "snapshot", "master", "main", "test"}


def _container_resources_incomplete(container: dict) -> bool:
    resources = container.get("resources") or {}
    requests = resources.get("requests") or {}
    limits = resources.get("limits") or {}
    return not (requests.get("cpu") and requests.get("memory") and limits.get("cpu") and limits.get("memory"))


def _default_resource_patch(container: dict) -> dict:
    resources = container.get("resources") or {}
    requests = resources.get("requests") or {}
    limits = resources.get("limits") or {}
    return {
        "name": container.get("name") or "app",
        "resources": {
            "requests": {
                "cpu": requests.get("cpu") or os.getenv("PRODUCTION_DEFAULT_CPU_REQUEST", "100m"),
                "memory": requests.get("memory") or os.getenv("PRODUCTION_DEFAULT_MEMORY_REQUEST", "256Mi"),
            },
            "limits": {
                "cpu": limits.get("cpu") or os.getenv("PRODUCTION_DEFAULT_CPU_LIMIT", "1"),
                "memory": limits.get("memory") or os.getenv("PRODUCTION_DEFAULT_MEMORY_LIMIT", "512Mi"),
            },
        },
    }


def _workload_production_risk_findings(workload: dict, *, cluster: dict | None = None, source: str = "rancher") -> list[dict]:
    kind = str(workload.get("kind") or "Deployment")
    name = str(workload.get("name") or "")
    namespace = str(workload.get("namespace") or "default")
    if not name or kind.lower() in {"job", "cronjob"}:
        return []

    cluster_name = str(workload.get("cluster") or (cluster or {}).get("name") or "local-cluster")
    cluster_id = str(workload.get("cluster_id") or (cluster or {}).get("id") or "local")
    containers = workload.get("containers") or []
    pod_spec = workload.get("pod_spec") or {}
    replicas = int(workload.get("replicas") or 0)
    labels = workload.get("labels") or {}
    category = _pod_classification_from_context({
        "namespace": namespace,
        "name": name,
        "workload_name": name,
        "containers": containers,
    }).get("class", "application")
    if category == "batch":
        return []

    risks: list[dict] = []
    patch_containers: list[dict] = []
    security_patches: list[dict] = []
    high = False

    for container in containers:
        cname = container.get("name") or "app"
        security_context = container.get("securityContext") or container.get("security_context") or {}
        if _container_resources_incomplete(container):
            risks.append({
                "code": "missing_resources",
                "severity": "P2",
                "title": f"容器 {cname} 缺少完整 requests/limits",
                "why_it_matters": "生产中缺少资源边界会导致调度不准、抢占不可控、HPA/容量判断失真。",
                "safe_action": "补齐保守的 cpu/memory requests 与 limits，执行后观察实际用量再微调。",
            })
            patch_containers.append(_default_resource_patch(container))
        has_readiness = bool(container.get("readinessProbe") or container.get("readiness_probe"))
        has_liveness = bool(container.get("livenessProbe") or container.get("liveness_probe"))
        has_startup = bool(container.get("startupProbe") or container.get("startup_probe"))
        if not (has_readiness and (has_liveness or has_startup)):
            risks.append({
                "code": "probe_policy_gap",
                "severity": "P2",
                "title": f"容器 {cname} 探针策略不完整",
                "why_it_matters": "缺少 readiness/liveness/startup 会让流量过早进入或故障实例长期留在服务后端。",
                "safe_action": "需要基于应用真实健康检查端口/路径生成探针；未确认路径前不自动写入伪探针。",
            })
        if _image_tag_is_mutable(container.get("image", "")):
            risks.append({
                "code": "mutable_image_tag",
                "severity": "P1",
                "title": f"容器 {cname} 使用可变镜像 tag",
                "why_it_matters": "latest/dev/snapshot 或无 tag 会让同一 YAML 在不同时间部署出不同镜像，回滚和审计都不可靠。",
                "safe_action": "改为不可变版本号或 digest；需要你确认目标镜像版本后才能自动 patch。",
            })
            high = True
        if security_context.get("privileged") is True or security_context.get("allowPrivilegeEscalation") is True:
            risks.append({
                "code": "privilege_escalation",
                "severity": "P1",
                "title": f"容器 {cname} 存在高权限运行风险",
                "why_it_matters": "privileged 或提权会扩大容器逃逸/横向移动影响面，生产默认应关闭。",
                "safe_action": "关闭 allowPrivilegeEscalation；privileged=false 需要先确认驱动/采集类组件例外。",
            })
            high = True
            security_patches.append({
                "name": cname,
                "securityContext": {"allowPrivilegeEscalation": False},
            })

    if replicas <= 1 and kind.lower() in {"deployment", "statefulset"} and labels.get("aiops.example.com/single-replica-ok") != "true":
        risks.append({
            "code": "single_replica",
            "severity": "P2",
            "title": "生产模式发现单副本工作负载",
            "why_it_matters": "单 Pod 故障、节点维护或滚动发布会直接影响该服务可用性。",
            "safe_action": "提升到 2 副本；若这是有状态单实例，请打 aiops.example.com/single-replica-ok=true 豁免。",
        })
    if pod_spec.get("hostNetwork") or pod_spec.get("hostPID") or pod_spec.get("hostIPC"):
        risks.append({
            "code": "host_namespace",
            "severity": "P1",
            "title": "Workload 使用宿主机网络/进程命名空间",
            "why_it_matters": "hostNetwork/hostPID/hostIPC 会显著扩大爆炸半径，只有网络/节点代理类组件应豁免。",
            "safe_action": "核对是否为平台组件；业务应用建议移除 host 级权限后灰度发布。",
        })
        high = True

    if not risks:
        return []

    changes: list[dict] = []
    if _workload_patchable(kind):
        if patch_containers:
            changes.append(_workload_patch_change(
                namespace,
                kind,
                name,
                {"spec": {"template": {"spec": {"containers": patch_containers}}}},
                "生产模式发现资源边界缺失；补齐保守 requests/limits，减少调度和容量风险。",
            ))
        if security_patches:
            changes.append(_workload_patch_change(
                namespace,
                kind,
                name,
                {"spec": {"template": {"spec": {"containers": security_patches}}}},
                "生产模式发现容器提权风险；关闭 allowPrivilegeEscalation，降低横向影响面。",
            ))
        if replicas <= 1 and kind.lower() in {"deployment", "statefulset"}:
            changes.append(_workload_patch_change(
                namespace,
                kind,
                name,
                {"spec": {"replicas": 2}},
                "生产模式发现单副本风险；提升到 2 副本以覆盖节点维护、滚动发布和单 Pod 故障。",
            ))

    severity = "P1" if high or any(r.get("severity") == "P1" for r in risks) else "P2"
    summary = "；".join(r["title"] for r in risks[:4])
    finding = {
        "id": _finding_id("production-risk", f"{cluster_id}:{namespace}", f"{kind}/{name}", summary),
        "source": source,
        "cluster": cluster_name,
        "cluster_id": cluster_id,
        "category": "production_risk",
        "severity": severity,
        "title": f"[{cluster_name}] {kind}/{name} 生产配置风险",
        "summary": summary,
        "namespace": namespace,
        "name": name,
        "workload": {
            **workload,
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "replicas": replicas,
            "ready_replicas": workload.get("ready_replicas", 0),
        },
        "evidence": {
            "risks": risks,
            "workload_spec": {
                "containers": containers,
                "pod_spec": pod_spec,
                "labels": labels,
            },
            "state_text": summary,
        },
        "proposed_changes": changes,
    }
    finding["ops_plan"] = _ops_plan_from_finding(finding)
    return [finding]


def _ops_plan_from_finding(finding: dict) -> dict:
    namespace = finding.get("namespace", "default")
    workload = finding.get("workload") or {}
    workload_name = workload.get("name") or finding.get("name", "")
    workload_type = workload.get("kind", "Deployment")
    category = finding.get("category")
    evidence = finding.get("evidence") or {}
    pod = evidence.get("pod") or {}
    evidence_text = " ".join([
        str(finding.get("title") or ""),
        str(finding.get("summary") or ""),
        str(evidence.get("state_text") or ""),
        " ".join(f"{e.get('reason', '')} {e.get('message', '')}" for e in evidence.get("events", []) or []),
    ]).lower()
    container = next((c for c in pod.get("containers", []) or [] if c.get("name")), {})
    container_name = container.get("name") or ""
    resources = container.get("resources") or {}
    current_requests = resources.get("requests") or {}
    current_limits = resources.get("limits") or {}
    patchable_workload = str(workload_type).lower() in {"deployment", "statefulset", "daemonset", "replicaset"}
    if category == "production_risk":
        steps = [
            {"title": "核对生产配置风险", "description": "检查资源边界、镜像 tag、探针、安全上下文、副本数和 host 级权限。", "status": "pending"},
            {"title": "区分可自动修复与需确认项", "description": "只对资源边界、提权关闭和副本数这类可审计 patch 生成候选动作；镜像版本和业务探针路径需要人工确认。", "status": "pending"},
            {"title": "执行变更门禁", "description": "计算影响面、风险级别和回滚方式，等待人工确认后再提交 Kubernetes patch。", "status": "pending"},
        ]
    elif category == "crashloop":
        steps = [
            {"title": "查看日志", "description": "读取 CrashLoop Pod 最近日志和上一次退出日志，确认启动失败/OOM/异常栈。", "status": "pending"},
            {"title": "检查配置和存储卷", "description": "核对 ConfigMap、Secret、PVC、挂载路径、启动参数和环境变量。", "status": "pending"},
            {"title": "检查探针和资源限制", "description": "检查 liveness/readiness、CPU/Memory limit、近期镜像或配置变更。", "status": "pending"},
            {"title": "选择差异化修复策略", "description": "优先按证据选择资源、探针、权限或配置修复；只有证据不足时才把滚动重启作为低风险加载动作。", "status": "pending"},
        ]
    elif category == "image_pull":
        steps = [
            {"title": "检查镜像拉取", "description": "确认镜像 tag、仓库地址、imagePullSecret、节点到镜像仓库网络连通性。", "status": "pending"},
            {"title": "查看 Pod Events", "description": "定位 ErrImagePull/ImagePullBackOff 的具体鉴权或网络错误。", "status": "pending"},
        ]
    elif category == "network":
        steps = [
            {"title": "检查网络事件", "description": "查看 Pod Events 中 DNS、Service、CNI、超时或连接拒绝线索。", "status": "pending"},
            {"title": "验证服务发现", "description": "检查 Service endpoints、selector、DNS 解析、NetworkPolicy 或 Service Mesh 路由。", "status": "pending"},
            {"title": "定位依赖链路", "description": "结合 CMDB/拓扑查看该 Pod 到 Kafka、数据库、上游服务的真实数据流路径。", "status": "pending"},
        ]
    elif category == "storage_config":
        steps = [
            {"title": "检查配置和存储卷", "description": "查看 ConfigMap/Secret/PVC 挂载失败、权限、路径和存储绑定状态。", "status": "pending"},
            {"title": "查看事件", "description": "确认 MountVolume、FailedAttachVolume、FailedScheduling 相关事件。", "status": "pending"},
        ]
    else:
        steps = [
            {"title": "查看日志", "description": "读取最近容器日志，确认错误栈、退出原因或启动失败信息。", "status": "pending"},
            {"title": "检查配置和存储卷", "description": "核对 ConfigMap、Secret、PVC、挂载路径和权限。", "status": "pending"},
            {"title": "检查运行参数", "description": "检查副本数、资源限制、探针、镜像版本和最近变更。", "status": "pending"},
        ]
    changes: list[dict] = []
    strategy_class = "evidence_only"
    if category == "production_risk" and finding.get("proposed_changes"):
        strategy_class = "production_readiness_hardening"
        changes.extend(finding.get("proposed_changes") or [])
    elif category == "crashloop" and "oomkilled" in evidence_text and workload_name and container_name and patchable_workload:
        strategy_class = "oom_resource_recovery"
        changes.append(_workload_patch_change(
            namespace,
            workload_type,
            workload_name,
            {"spec": {"template": {"spec": {"containers": [{
                **_container_patch_base(container_name, container),
                "resources": {
                    "requests": {
                        "memory": current_requests.get("memory") or "256Mi",
                        "cpu": current_requests.get("cpu") or "100m",
                    },
                    "limits": {
                        "memory": _increased_memory_quantity(current_limits.get("memory")),
                        "cpu": current_limits.get("cpu") or "1",
                    },
                },
            }]}}}},
            "检测到 OOMKilled 证据；提升容器内存 request/limit 并通过 rollout 验证，而不是重复重启。",
        ))
    elif category == "crashloop" and any(term in evidence_text for term in ["probe failed", "liveness", "readiness", "startup probe", "connection refused", "context deadline exceeded"]) and workload_name and container_name and patchable_workload:
        strategy_class = "probe_stabilization"
        probe_patch = _probe_patch_from_container(container)
        changes.append(_workload_patch_change(
            namespace,
            workload_type,
            workload_name,
            {"spec": {"template": {"spec": {"containers": [{
                **_container_patch_base(container_name, container),
                **probe_patch,
            }]}}}},
            "检测到启动慢/探针失败证据；增加 startupProbe 容错窗口，避免容器在真正启动前被反复杀死。",
        ))
    elif category == "storage_config" and any(term in evidence_text for term in ["permission denied", "operation not permitted", "read-only file system"]) and workload_name and patchable_workload:
        strategy_class = "dynamic_skill_required"
        # 权限故障不能被固定 fsGroup 模板抢答。先完成实时深取证，再由 AI 结合
        # 匹配 Skill 生成实际 YAML/命令和重新发布方案。
        changes = []
    elif category == "image_pull" and any(term in evidence_text for term in ["unauthorized", "authentication required", "pull access denied", "secret", "denied"]) and workload_name and patchable_workload and os.getenv("DEFAULT_IMAGE_PULL_SECRET", "").strip():
        strategy_class = "image_pull_secret_recovery"
        secret_name = os.getenv("DEFAULT_IMAGE_PULL_SECRET", "").strip()
        changes.append(_workload_patch_change(
            namespace,
            workload_type,
            workload_name,
            {"spec": {"template": {"spec": {"imagePullSecrets": [{"name": secret_name}]}}}},
            f"检测到镜像仓库鉴权失败，并已配置 DEFAULT_IMAGE_PULL_SECRET={secret_name}；为 Workload 注入 imagePullSecrets。",
        ))
    elif category == "not_ready" and any(term in evidence_text for term in ["probe failed", "liveness", "readiness", "startup probe"]) and workload_name and container_name and patchable_workload:
        strategy_class = "probe_stabilization"
        probe_patch = _probe_patch_from_container(container)
        changes.append(_workload_patch_change(
            namespace,
            workload_type,
            workload_name,
            {"spec": {"template": {"spec": {"containers": [{
                **_container_patch_base(container_name, container),
                **probe_patch,
            }]}}}},
            "检测到探针失败证据；增加启动容错窗口，避免慢启动容器被反复杀死。",
        ))
    elif category == "crashloop" and workload_name and patchable_workload:
        strategy_class = "controlled_rollout_restart"
        changes.append({
            "type": "restart",
            "namespace": namespace,
            "workload_type": workload_type,
            "workload_name": workload_name,
            "reason": "CrashLoop/高重启风险，确认日志和配置后执行滚动重启。",
            "patch": {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "<now>"}}}}},
        })
    elif category == "capacity" and workload_name and patchable_workload:
        strategy_class = "replica_capacity_recovery"
        changes.append(_workload_patch_change(
            namespace,
            workload_type,
            workload_name,
            {"spec": {"replicas": max(2, int(workload.get("replicas") or 1) + 1)}},
            "容量或可用副本不足，建议临时增加 replicas。",
        ))
    engine_context = {
        "pod": pod,
        "pods": [pod] if pod else [],
        "events": {"events": evidence.get("events", []) or []},
        "diagnostics": evidence,
    }
    engine_plan = build_remediation_plan(
        {
            "alert_name": finding.get("category", "inspection_finding"),
            "summary": finding.get("summary", ""),
            "namespace": namespace,
            "workload_type": workload_type,
            "workload_name": workload_name,
            "pod": pod.get("name", ""),
        },
        {"root_cause": finding.get("summary", ""), "signals": evidence.get("events", [])},
        engine_context,
    )
    if engine_plan.get("changes") and not (category == "storage_config" and any(term in evidence_text for term in ["permission denied", "operation not permitted", "read-only file system"])):
        changes = engine_plan["changes"]
        strategy_class = engine_plan.get("runbook_id") or strategy_class
    engine_steps = engine_plan.get("steps") or []
    engine_step_ids = {step.get("id") or step.get("title") for step in engine_steps}
    steps = engine_steps + [step for step in steps if (step.get("id") or step.get("title")) not in engine_step_ids]
    plan = {
        "id": finding.get("id") or str(uuid.uuid4())[:8],
        "title": finding.get("title", "AI 运维计划"),
        "cluster": finding.get("cluster", "local-cluster"),
        "cluster_id": finding.get("cluster_id", "local"),
        "source": finding.get("source", "mcp"),
        "namespace": namespace,
        "target": f"{workload_type}/{workload_name}" if workload_name else namespace,
        "pod_name": ((finding.get("evidence") or {}).get("pod") or {}).get("name", ""),
        "evidence": evidence,
        "steps": steps,
        "changes": changes,
        "requires_confirmation": bool(changes),
        "summary": finding.get("summary", ""),
        "reason": engine_plan.get("reason", ""),
        "evidence_gap": engine_plan.get("evidence_gap", ""),
        "strategy_class": strategy_class,
        "root_cause_hypotheses": engine_plan.get("hypotheses", []),
        "success_criteria": engine_plan.get("success_criteria", []),
        "diagnostic_actions": engine_plan.get("diagnostic_actions", []),
        "planning_engine": engine_plan.get("engine"),
        "action_catalog": engine_plan.get("action_catalog", []),
        "expert_playbook": _expert_playbook_catalog(),
    }
    return _attach_operator_skills_to_plan(plan, _skill_signal_payload(
        question=finding.get("summary", ""),
        alert={
            "category": finding.get("category"),
            "severity": finding.get("severity"),
            "cluster": finding.get("cluster"),
            "namespace": namespace,
            "workload_type": workload_type,
            "workload_name": workload_name,
            "pod": pod.get("name", ""),
        },
        diagnosis={"root_cause": finding.get("summary", ""), "signals": evidence.get("events", [])},
        evidence=evidence,
        plan=plan,
    ))


def _pod_state_text(pod: dict) -> str:
    parts = [str(pod.get("phase", ""))]
    for c in pod.get("containers", []):
        parts.extend([
            str(c.get("state", "")),
            str(c.get("reason", "")),
            str((c.get("state_detail") or {}).get("reason", "")),
            str((c.get("state_detail") or {}).get("message", "")),
        ])
    return " ".join(parts)


def _classify_pod_issue(pod: dict, events: list[dict] | None = None) -> tuple[str | None, str, str]:
    if _pod_completed_successfully(pod):
        return None, "", ""
    text = (_pod_state_text(pod) + " " + " ".join(
        f"{e.get('reason','')} {e.get('message','')}" for e in (events or [])
    )).lower()
    if any(k.lower() in text for k in ["ImagePullBackOff", "ErrImagePull", "pull access denied", "manifest unknown"]):
        return "image_pull", "P1", "Pod 镜像拉取失败，可能是镜像地址、凭据或仓库网络问题"
    if any(k.lower() in text for k in ["FailedMount", "FailedAttachVolume", "MountVolume", "configmap", "secret not found", "persistentvolumeclaim"]):
        return "storage_config", "P1", "Pod 存在配置或存储卷挂载异常"
    if any(k.lower() in text for k in ["dns", "cni", "network", "connection refused", "i/o timeout", "no route to host"]):
        return "network", "P2", "Pod 存在网络、DNS、CNI 或服务连通性异常线索"
    # 泛化的 rpc error / Error 不能抢在 ImagePull、挂载、网络等明确证据前面。
    if any(k.lower() in text for k in ["CrashLoopBackOff", "OOMKilled", "Back-off restarting failed container", "exit code 137"]):
        return "crashloop", "P1", "Pod 出现 CrashLoop/OOM/容器反复失败信号"
    if pod.get("phase") == "Pending":
        return "scheduling", "P2", "Pod Pending，可能是资源不足、亲和性、污点容忍或 PVC 绑定问题"
    if not pod.get("ready"):
        return "not_ready", "P2", "Pod 未 Ready，需要检查探针、日志和事件"
    if pod.get("restart_count", 0) > 5:
        return "crashloop", "P2", "Pod 重启次数过高，存在稳定性风险"
    return None, "", ""


def _owner_from_k8s_pod(pod: dict, replica_owner: dict[str, tuple[str, str]] | None = None) -> tuple[str, str]:
    owners = ((pod.get("metadata") or {}).get("ownerReferences") or [])
    if not owners:
        return "", ""
    owner = owners[0]
    kind = str(owner.get("kind") or "")
    name = str(owner.get("name") or "")
    if kind == "ReplicaSet":
        mapped = (replica_owner or {}).get(name)
        if mapped:
            return mapped
        guessed = re.sub(r"-[a-f0-9]{8,12}$", "", name)
        return "Deployment", guessed or name
    return kind, name


def _normalize_k8s_pod(raw: dict, replica_owner: dict[str, tuple[str, str]] | None = None) -> dict:
    meta = raw.get("metadata") or {}
    status = raw.get("status") or {}
    spec = raw.get("spec") or {}
    container_statuses = status.get("containerStatuses") or []
    init_container_statuses = status.get("initContainerStatuses") or []
    containers = []
    restart_count = 0
    ready = bool(container_statuses) and all(bool(c.get("ready")) for c in container_statuses)
    for c in [*container_statuses, *init_container_statuses]:
        state_obj = c.get("state") or {}
        last_state = c.get("lastState") or {}
        state_name = next(iter(state_obj.keys()), "")
        state_detail = state_obj.get(state_name) or {}
        reason = state_detail.get("reason") or (last_state.get("terminated") or {}).get("reason") or ""
        message = state_detail.get("message") or (last_state.get("terminated") or {}).get("message") or ""
        restart_count += int(c.get("restartCount") or 0)
        containers.append({
            "name": c.get("name", ""),
            "is_init": c in init_container_statuses,
            "ready": bool(c.get("ready")),
            "restart_count": int(c.get("restartCount") or 0),
            "state": state_name,
            "reason": reason,
            "state_detail": {"reason": reason, "message": message},
            "image": c.get("image", ""),
            "resources": {},
            "liveness_probe": None,
            "readiness_probe": None,
            "startup_probe": None,
            "security_context": {},
            "volume_mounts": [],
        })
    for spec_container in [*(spec.get("containers") or []), *(spec.get("initContainers") or [])]:
        for item in containers:
            if item.get("name") == spec_container.get("name"):
                item["security_context"] = spec_container.get("securityContext") or {}
                item["volume_mounts"] = spec_container.get("volumeMounts") or []
                item["resources"] = spec_container.get("resources") or {}
                item["env"] = spec_container.get("env") or []
                item["liveness_probe"] = spec_container.get("livenessProbe") or {}
                item["readiness_probe"] = spec_container.get("readinessProbe") or {}
                item["startup_probe"] = spec_container.get("startupProbe") or {}
    workload_kind, workload_name = _owner_from_k8s_pod(raw, replica_owner)
    completed = status.get("phase") in {"Succeeded", "Completed"} or (
        bool(containers) and all(str((c.get("state_detail") or {}).get("reason") or "") == "Completed" for c in containers)
    )
    return {
        "name": meta.get("name", ""),
        "namespace": meta.get("namespace", "default"),
        "labels": meta.get("labels") or {},
        "annotations": meta.get("annotations") or {},
        "node": spec.get("nodeName", ""),
        "phase": status.get("phase", ""),
        "ready": ready,
        "completed": completed,
        "restart_count": restart_count,
        "containers": containers,
        "owner_references": meta.get("ownerReferences") or [],
        "security_context": spec.get("securityContext") or {},
        "workload_kind": workload_kind,
        "workload_name": workload_name,
    }


def _normalize_k8s_events(raw_items: list[dict]) -> list[dict]:
    events = []
    for item in raw_items[-12:]:
        events.append({
            "type": item.get("type", ""),
            "reason": item.get("reason", ""),
            "message": item.get("message", ""),
            "count": item.get("count", item.get("series", {}).get("count", "")),
            "last_timestamp": item.get("lastTimestamp") or item.get("eventTime") or item.get("metadata", {}).get("creationTimestamp", ""),
        })
    return events


def _workload_api_path(kind: str, namespace: str, name: str) -> str:
    plural = {
        "deployment": "deployments",
        "statefulset": "statefulsets",
        "daemonset": "daemonsets",
        "replicaset": "replicasets",
    }.get(str(kind or "deployment").lower(), "deployments")
    return f"/apis/apps/v1/namespaces/{quote(namespace, safe='')}/{plural}/{quote(name, safe='')}"


def _workload_collection_api_path(kind: str, namespace: str) -> str:
    return _workload_api_path(kind, namespace, "").rstrip("/")


def _safe_workload_evidence(raw: dict) -> dict:
    metadata = raw.get("metadata") or {}
    spec = raw.get("spec") or {}
    pod_spec = ((spec.get("template") or {}).get("spec") or {})
    containers = []
    for container in pod_spec.get("containers", []) or []:
        containers.append({
            "name": container.get("name"), "image": container.get("image"),
            "resources": container.get("resources", {}), "livenessProbe": container.get("livenessProbe"),
            "readinessProbe": container.get("readinessProbe"), "startupProbe": container.get("startupProbe"),
            "securityContext": container.get("securityContext", {}), "volumeMounts": container.get("volumeMounts", []),
            "envReferences": [
                {"name": env.get("name"), "source": next(iter((env.get("valueFrom") or {}).keys()), "literal-present" if "value" in env else "")}
                for env in container.get("env", []) or []
            ],
        })
    return {
        "apiVersion": raw.get("apiVersion"), "kind": raw.get("kind"),
        "metadata": {"name": metadata.get("name"), "namespace": metadata.get("namespace"), "generation": metadata.get("generation")},
        "spec": {"replicas": spec.get("replicas"), "strategy": spec.get("strategy"), "template": {"spec": {
            "containers": containers, "volumes": pod_spec.get("volumes", []), "securityContext": pod_spec.get("securityContext", {}),
            "imagePullSecrets": pod_spec.get("imagePullSecrets", []), "nodeSelector": pod_spec.get("nodeSelector", {}),
            "tolerations": pod_spec.get("tolerations", []), "affinity": pod_spec.get("affinity", {}),
            "topologySpreadConstraints": pod_spec.get("topologySpreadConstraints", []),
        }}},
        "status": raw.get("status", {}),
    }


async def _rancher_scan_cluster(cluster: dict, namespace_filter: str = "all", production_mode: bool = False) -> dict:
    cluster_id = cluster["id"]
    cluster_name = cluster["name"]
    ns_path = "" if namespace_filter in {"", "all", "*"} else f"/namespaces/{quote(namespace_filter, safe='')}"
    findings: list[dict] = []
    namespaces: set[str] = set()
    errors: list[str] = []

    replica_owner: dict[str, tuple[str, str]] = {}
    try:
        rs_payload = await _rancher_k8s_get(cluster_id, f"/apis/apps/v1/replicasets", timeout=25)
        for rs in rs_payload.get("items", []) if isinstance(rs_payload, dict) else []:
            owners = (rs.get("metadata") or {}).get("ownerReferences") or []
            if owners:
                owner = owners[0]
                replica_owner[(rs.get("metadata") or {}).get("name", "")] = (
                    owner.get("kind", "Deployment"),
                    owner.get("name", ""),
                )
    except Exception as exc:
        errors.append(f"replicasets: {type(exc).__name__}: {exc}")

    try:
        pods_payload = await _rancher_k8s_get(cluster_id, f"/api/v1{ns_path}/pods", timeout=35)
        raw_pods = pods_payload.get("items", []) if isinstance(pods_payload, dict) else []
    except Exception as exc:
        return {
            "cluster": cluster,
            "findings": [],
            "namespaces": [],
            "errors": [f"pods: {type(exc).__name__}: {exc}"],
        }

    normalized_pods = [_normalize_k8s_pod(raw_pod, replica_owner) for raw_pod in raw_pods]
    suspicious: list[tuple[dict, str, str, str]] = []
    for pod in normalized_pods:
        ns = pod.get("namespace") or "default"
        namespaces.add(ns)
        category, severity, reason = _classify_pod_issue(pod, [])
        if category:
            suspicious.append((pod, category, severity, reason))

    evidence_limit = int(os.getenv("INSPECTION_MAX_EVENT_FETCHES_PER_CLUSTER", "40"))

    async def fetch_events(item: tuple[dict, str, str, str]) -> list[dict]:
        pod, _, _, _ = item
        ns = pod.get("namespace") or "default"
        try:
            selector = quote(f"involvedObject.name={pod.get('name','')}", safe="=,")
            events_payload = await _rancher_k8s_get(
                cluster_id,
                f"/api/v1/namespaces/{quote(ns, safe='')}/events?fieldSelector={selector}",
                timeout=12,
            )
            return _normalize_k8s_events(events_payload.get("items", []) if isinstance(events_payload, dict) else [])
        except Exception as exc:
            return [{"type": "Warning", "reason": "EventFetchFailed", "message": f"{type(exc).__name__}: {exc}"}]

    event_batches = await asyncio.gather(*(fetch_events(item) for item in suspicious[:evidence_limit]))
    events_by_pod = {
        item[0].get("name", ""): events
        for item, events in zip(suspicious[:evidence_limit], event_batches)
    }

    for pod, category, severity, reason in suspicious:
            ns = pod.get("namespace") or "default"
            events = events_by_pod.get(pod.get("name", ""), [])
            category, severity, reason = _classify_pod_issue(pod, events)
            workload = {
                "kind": pod.get("workload_kind") or "Pod",
                "name": pod.get("workload_name") or pod.get("name"),
                "replicas": 1,
                "ready_replicas": 0 if not pod.get("ready") else 1,
                "pods": [pod],
                "impact": {
                    "level": "high" if severity in {"P0", "P1"} else "medium",
                    "summary": reason,
                },
            }
            finding = {
                "id": _finding_id("pod", f"{cluster_id}:{ns}", pod.get("name", ""), category),
                "source": "rancher",
                "cluster": cluster_name,
                "cluster_id": cluster_id,
                "category": category,
                "severity": severity,
                "title": f"[{cluster_name}] Pod {pod.get('name')} {reason}",
                "summary": (
                    f"{reason}。所属 {workload['kind']}/{workload['name']}，"
                    f"namespace={ns}，重启 {pod.get('restart_count', 0)} 次，phase={pod.get('phase')}"
                ),
                "namespace": ns,
                "name": pod.get("name"),
                "workload": workload,
                "evidence": {
                    "pod": pod,
                    "events": events[:8],
                    "state_text": _pod_state_text(pod),
                },
            }
            finding["ops_plan"] = _ops_plan_from_finding(finding)
            findings.append(finding)

    if production_mode:
        async def get_workloads(path: str, kind: str) -> list[dict]:
            try:
                payload = await _rancher_k8s_get(cluster_id, path, timeout=30)
                return [
                    _normalize_k8s_workload(kind, raw, cluster)
                    for raw in (payload.get("items", []) if isinstance(payload, dict) else [])
                ]
            except Exception as exc:
                errors.append(f"{kind.lower()} production scan: {type(exc).__name__}: {exc}")
                return []

        deployments, statefulsets, daemonsets = await asyncio.gather(
            get_workloads(f"/apis/apps/v1{ns_path}/deployments", "Deployment"),
            get_workloads(f"/apis/apps/v1{ns_path}/statefulsets", "StatefulSet"),
            get_workloads(f"/apis/apps/v1{ns_path}/daemonsets", "DaemonSet"),
        )
        for workload in [*deployments, *statefulsets, *daemonsets]:
            namespaces.add(workload.get("namespace") or "default")
            findings.extend(_workload_production_risk_findings(workload, cluster=cluster, source="rancher"))

    return {
        "cluster": cluster,
        "findings": findings,
        "namespaces": sorted(namespaces),
        "errors": errors,
    }


async def _rancher_inspection(req: InspectionRequest) -> dict:
    clusters = await _rancher_clusters()
    selected_cluster = (req.cluster or "all").strip()
    if selected_cluster not in {"", "all", "*", "所有"}:
        clusters = [
            c for c in clusters
            if selected_cluster in {c["id"], c["name"]}
        ]
    scans = await asyncio.gather(
        *[_rancher_scan_cluster(c, req.namespace or "all", req.production_mode) for c in clusters],
        return_exceptions=True,
    )
    findings: list[dict] = []
    namespaces_by_cluster: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    for cluster, result in zip(clusters, scans):
        if isinstance(result, Exception):
            errors[cluster["name"]] = f"{type(result).__name__}: {result}"
            namespaces_by_cluster[cluster["name"]] = []
            continue
        findings.extend(result.get("findings", []))
        namespaces_by_cluster[cluster["name"]] = result.get("namespaces", [])
        if result.get("errors"):
            errors[cluster["name"]] = "; ".join(result["errors"])
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "ok" if not errors else "degraded",
        "source": "rancher",
        "clusters": clusters,
        "namespaces_by_cluster": namespaces_by_cluster,
        "findings": findings,
        "diagnostics": {"errors": errors},
        "summary": {
            "total": len(findings),
            "critical": sum(1 for f in findings if f.get("severity") in {"P0", "P1"}),
            "auto_ops": req.auto_ops,
            "production_mode": req.production_mode,
            "clusters": len(clusters),
        },
        "node_condition_standard": (
            "Rancher 多集群巡检：基于 Pod phase、container waiting/terminated reason、ready、restart_count "
            "和 Events 判断 CrashLoop、ImagePull、Pending、存储/配置、网络等风险。"
        ),
    }


async def _managed_inspection(req: InspectionRequest) -> dict:
    clusters = _managed_cluster_metadata()
    selected = (req.cluster or "all").strip()
    if selected not in {"", "all", "*", "所有"}:
        clusters = [cluster for cluster in clusters if selected in {cluster["id"], cluster["name"]}]
    scans = await asyncio.gather(*[_managed_inventory_cluster(cluster) for cluster in clusters])
    findings: list[dict] = []
    namespaces_by_cluster: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    for cluster, scan in zip(clusters, scans):
        allowed_pods = [pod for pod in scan.get("pods") or [] if req.namespace in {"", "all", "*", "所有", pod.get("namespace")}]
        namespaces_by_cluster[cluster["name"]] = sorted({str(pod.get("namespace") or "default") for pod in allowed_pods})
        if scan.get("errors"):
            errors[cluster["name"]] = "; ".join(scan["errors"])
        for pod in allowed_pods:
            category, severity, reason = _classify_pod_issue(pod, [])
            if not category:
                continue
            workload = {"kind": pod.get("workload_kind") or "Pod", "name": pod.get("workload_name") or pod.get("name"), "replicas": 1, "ready_replicas": 1 if pod.get("ready") else 0, "pods": [pod]}
            finding = {"id": _finding_id("pod", f"{cluster['id']}:{pod.get('namespace')}", pod.get("name", ""), category), "source": "kubeconfig", "cluster": cluster["name"], "cluster_id": cluster["id"], "category": category, "severity": severity, "title": f"[{cluster['name']}] Pod {pod.get('name')} {reason}", "summary": f"{reason}。所属 {workload['kind']}/{workload['name']}，namespace={pod.get('namespace')}，重启 {pod.get('restart_count', 0)} 次。", "namespace": pod.get("namespace") or "default", "name": pod.get("name"), "workload": workload, "evidence": {"pod": pod, "events": [], "state_text": _pod_state_text(pod)}}
            finding["ops_plan"] = _ops_plan_from_finding(finding)
            findings.append(finding)
        if req.production_mode:
            for workload in scan.get("workloads") or []:
                if req.namespace not in {"", "all", "*", "所有", workload.get("namespace")}:
                    continue
                findings.extend(_workload_production_risk_findings(workload, cluster=cluster, source="kubeconfig"))
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "status": "ok" if not errors else "degraded", "source": "kubeconfig", "clusters": clusters, "namespaces_by_cluster": namespaces_by_cluster, "findings": findings, "diagnostics": {"errors": errors}, "summary": {"total": len(findings), "critical": sum(1 for finding in findings if finding.get("severity") in {"P0", "P1"}), "auto_ops": req.auto_ops, "production_mode": req.production_mode, "clusters": len(clusters)}, "node_condition_standard": "通过加密 kubeconfig 直接读取真实 Kubernetes API；候选变更必须经动态 Skill 和人工审批。"}


def _clip_text(text: str | None, limit: int = 2200) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[-limit:]


def _materialize_patch(value):
    if isinstance(value, dict):
        return {k: _materialize_patch(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_materialize_patch(v) for v in value]
    if value == "<now>":
        return datetime.now(timezone.utc).isoformat()
    return value


def _target_pod_from_plan(plan: dict) -> str:
    evidence = plan.get("evidence") or {}
    pod = evidence.get("pod") if isinstance(evidence, dict) else {}
    return plan.get("pod_name") or (pod or {}).get("name", "")


def _permission_guidance(error_payload: dict | str, plan: dict, change: dict | None = None) -> dict | None:
    if isinstance(error_payload, dict):
        # Successful resources can legitimately be named ``permission-*``.
        # Only error-shaped fields are authorization evidence; scanning an
        # entire returned object would show a false RBAC warning after success.
        error_view = {
            key: error_payload.get(key)
            for key in ("error", "message", "reason", "code")
            if error_payload.get(key) not in (None, "", {}, [])
        }
        # Kubernetes Status errors use a scalar ``status: Failure``.  A normal
        # resource also has a nested ``status`` object, whose rollout messages
        # may repeat a workload name containing the word "permission".
        status_value = error_payload.get("status")
        if isinstance(status_value, (str, int, float, bool)):
            error_view["status"] = status_value
        if error_view and error_payload.get("details") not in (None, "", {}, []):
            error_view["details"] = error_payload.get("details")
        text = json.dumps(error_view, ensure_ascii=False, default=str)
    else:
        text = str(error_payload or "")
    lowered = text.lower()
    if not any(term in lowered for term in ["forbidden", "rbac", "403", "permission", "unauthorized", "not permitted"]):
        return None
    namespace = plan.get("namespace") or "目标 namespace"
    workload_type = plan.get("target") or plan.get("workload_type") or "目标 Workload"
    cluster = plan.get("cluster") or plan.get("cluster_id") or "目标集群"
    action = str((change or {}).get("type") or "kubernetes_change")
    requirements = {
        "recreate_pod": (["get", "delete"], ["pods"]),
        "restart": (["get", "patch"], ["deployments", "statefulsets", "daemonsets"]),
        "patch_workload": (["get", "patch"], ["deployments", "statefulsets", "daemonsets"]),
        "patch_workload_volume": (["get", "patch"], ["deployments", "statefulsets", "daemonsets", "persistentvolumeclaims"]),
        "patch_workload_runtime_security": (["get", "patch"], ["deployments", "statefulsets", "daemonsets", "pods/log", "events"]),
        "create_pvc": (["get", "create"], ["persistentvolumeclaims"]),
        "create_pv": (["get", "create"], ["persistentvolumes", "persistentvolumeclaims"]),
        "cordon_node": (["get", "patch"], ["nodes"]),
        "evict_pod": (["get", "create"], ["pods", "pods/eviction"]),
        "create_configmap": (["get", "create"], ["configmaps"]),
    }
    verbs, resources = requirements.get(action, (["get", "patch", "update"], ["目标 Kubernetes 资源"]))
    permission_owner = "Rancher Token" if plan.get("source") == "rancher" else "ServiceAccount k8s-agent/k8s-agent-sa"
    return {
        "summary": f"{permission_owner} 没有执行 {action} 所需的最小权限，本轮没有继续提交变更。",
        "do_this": [
            f"在集群 {cluster} 检查 {permission_owner} 对 namespace={namespace} 的授权范围。",
            f"本动作只需 verbs={','.join(verbs)}，resources={','.join(resources)}；不需要直接授予 cluster-admin。",
            "本地集群应用 manifests/rbac.yaml 和对应 namespace RoleBinding；跨 Rancher 集群则给 Rancher Token 的项目/集群角色补同等权限。",
            "重新部署后再执行同一个运维计划，不需要重新输入问题。",
        ],
        "minimal_verbs": verbs,
        "minimal_resources": resources,
        "action": action,
        "target": workload_type,
    }


ALLOWED_TEMPLATE_SPEC_KEYS = {"containers", "securityContext", "imagePullSecrets", "nodeSelector", "tolerations", "affinity"}
ALLOWED_CONTAINER_PATCH_KEYS = {
    "name",
    "image",
    "resources",
    "env",
    "livenessProbe",
    "readinessProbe",
    "startupProbe",
    "securityContext",
}
ALLOWED_POD_SECURITY_KEYS = {"fsGroup", "fsGroupChangePolicy", "runAsUser", "runAsGroup", "runAsNonRoot", "supplementalGroups"}
ALLOWED_CONTAINER_SECURITY_KEYS = {"runAsUser", "runAsGroup", "runAsNonRoot", "allowPrivilegeEscalation", "readOnlyRootFilesystem"}
ALLOWED_INIT_CONTAINER_KEYS = {"name", "image", "command", "args", "securityContext", "volumeMounts"}
ALLOWED_INIT_CONTAINER_SECURITY_KEYS = {
    "runAsUser",
    "runAsGroup",
    "runAsNonRoot",
    "allowPrivilegeEscalation",
    "readOnlyRootFilesystem",
    "capabilities",
}
ALLOWED_PROBE_KEYS = {
    "exec",
    "failureThreshold",
    "grpc",
    "httpGet",
    "initialDelaySeconds",
    "periodSeconds",
    "successThreshold",
    "tcpSocket",
    "terminationGracePeriodSeconds",
    "timeoutSeconds",
}
PROBE_HANDLER_KEYS = {"exec", "httpGet", "tcpSocket", "grpc"}


def _patch_touches_volumes(patch: dict) -> bool:
    try:
        pod_spec = (((patch.get("spec") or {}).get("template") or {}).get("spec") or {})
        return "volumes" in pod_spec
    except Exception:
        return False


def _patch_touches_init_containers(patch: dict) -> bool:
    try:
        pod_spec = (((patch.get("spec") or {}).get("template") or {}).get("spec") or {})
        return "initContainers" in pod_spec
    except Exception:
        return False


def _validate_probe_patch(probe: dict, field: str) -> tuple[bool, str]:
    if not isinstance(probe, dict):
        return False, f"{field} must be object"
    illegal = set(probe) - ALLOWED_PROBE_KEYS
    if illegal:
        return False, f"{field} contains unsupported fields: {sorted(illegal)}"
    if field == "startupProbe" and not (set(probe) & PROBE_HANDLER_KEYS):
        return False, "startupProbe must include one of exec/httpGet/tcpSocket/grpc"
    for key in ("failureThreshold", "initialDelaySeconds", "periodSeconds", "successThreshold", "timeoutSeconds"):
        if key in probe:
            try:
                value = int(probe[key])
            except Exception:
                return False, f"{field}.{key} must be integer"
            if value < 0 or value > 3600:
                return False, f"{field}.{key} must be between 0 and 3600"
    return True, ""


def _validate_init_container_patch(container: dict) -> tuple[bool, str]:
    if not isinstance(container, dict):
        return False, "initContainer patch item must be object"
    illegal = set(container) - ALLOWED_INIT_CONTAINER_KEYS
    if illegal:
        return False, f"unsupported initContainer fields: {sorted(illegal)}"
    if not container.get("name") or not container.get("image"):
        return False, "initContainer patch requires name and image"
    for field in ("command", "args"):
        values = container.get(field) or []
        if values and (not isinstance(values, list) or any(not isinstance(item, str) for item in values)):
            return False, f"initContainer.{field} must be a string list"
    command_text = " ".join((container.get("command") or []) + (container.get("args") or [])).lower()
    forbidden = [
        "curl ", "wget ", "nc ", "ncat ", "kubectl", "python", "perl", "ruby",
        "rm -rf", "mkfs", "dd if=", "mount ", "umount ", "ssh ", "scp ", "/dev/tcp",
    ]
    if any(term in command_text for term in forbidden):
        return False, "initContainer command contains unsupported network, shell or destructive operation"
    if command_text and not any(term in command_text for term in ("chown", "chmod", "mkdir", "test", "id", "ls")):
        return False, "initContainer command must be a bounded permission probe/fix command"
    sc = container.get("securityContext") or {}
    if sc:
        if not isinstance(sc, dict) or set(sc) - ALLOWED_INIT_CONTAINER_SECURITY_KEYS:
            return False, "initContainer.securityContext contains unsupported fields"
        capabilities = sc.get("capabilities") or {}
        if capabilities:
            if not isinstance(capabilities, dict) or set(capabilities) - {"drop", "add"}:
                return False, "initContainer.securityContext.capabilities only permits drop/add"
            for key in ("drop", "add"):
                values = capabilities.get(key) or []
                if values and (not isinstance(values, list) or any(not isinstance(item, str) for item in values)):
                    return False, f"initContainer.capabilities.{key} must be a string list"
    mounts = container.get("volumeMounts") or []
    if mounts:
        if not isinstance(mounts, list):
            return False, "initContainer.volumeMounts must be list"
        for mount in mounts:
            if not isinstance(mount, dict):
                return False, "initContainer.volumeMount item must be object"
            if set(mount) - {"name", "mountPath", "readOnly", "subPath"}:
                return False, "initContainer.volumeMount only permits name/mountPath/readOnly/subPath"
            if not mount.get("name") or not mount.get("mountPath"):
                return False, "initContainer.volumeMount requires name and mountPath"
    return True, ""


def _validate_workload_patch(
    patch: dict,
    *,
    allow_volume_patch: bool = False,
    allow_init_containers: bool = False,
) -> tuple[bool, str]:
    if not isinstance(patch, dict):
        return False, "patch must be a JSON object"
    spec = patch.get("spec")
    if not isinstance(spec, dict):
        return False, "patch must contain spec"
    allowed_spec_keys = {"replicas", "template"}
    illegal = set(spec) - allowed_spec_keys
    if illegal:
        return False, f"unsupported spec fields: {sorted(illegal)}"
    if "replicas" in spec:
        try:
            replicas = int(spec["replicas"])
        except Exception:
            return False, "spec.replicas must be integer"
        max_replicas = int(os.getenv("MAX_PATCH_REPLICAS", "20"))
        if replicas < 0 or replicas > max_replicas:
            return False, f"spec.replicas must be between 0 and {max_replicas}"
    template = spec.get("template")
    if template is None:
        return True, ""
    if not isinstance(template, dict):
        return False, "spec.template must be object"
    illegal_template = set(template) - {"metadata", "spec"}
    if illegal_template:
        return False, f"unsupported template fields: {sorted(illegal_template)}"
    metadata = template.get("metadata") or {}
    if metadata:
        if not isinstance(metadata, dict) or set(metadata) - {"annotations"}:
            return False, "only template.metadata.annotations can be patched"
        annotations = metadata.get("annotations") or {}
        if not isinstance(annotations, dict):
            return False, "template.metadata.annotations must be object"
    pod_spec = template.get("spec") or {}
    if pod_spec:
        if not isinstance(pod_spec, dict):
            return False, "template.spec must be object"
        allowed_pod_keys = set(ALLOWED_TEMPLATE_SPEC_KEYS)
        if allow_volume_patch:
            allowed_pod_keys.add("volumes")
        if allow_init_containers:
            allowed_pod_keys.add("initContainers")
        illegal_pod_spec = set(pod_spec) - allowed_pod_keys
        if illegal_pod_spec:
            return False, f"unsupported template.spec fields: {sorted(illegal_pod_spec)}"
        pod_sc = pod_spec.get("securityContext") or {}
        if pod_sc and (not isinstance(pod_sc, dict) or set(pod_sc) - ALLOWED_POD_SECURITY_KEYS):
            return False, "template.spec.securityContext contains unsupported fields"
        image_pull_secrets = pod_spec.get("imagePullSecrets") or []
        if image_pull_secrets:
            if not isinstance(image_pull_secrets, list) or any(not isinstance(x, dict) or set(x) - {"name"} or not x.get("name") for x in image_pull_secrets):
                return False, "template.spec.imagePullSecrets must be a list of {name}"
        node_selector = pod_spec.get("nodeSelector") or {}
        if node_selector and not isinstance(node_selector, dict):
            return False, "template.spec.nodeSelector must be object"
        tolerations = pod_spec.get("tolerations") or []
        if tolerations and not isinstance(tolerations, list):
            return False, "template.spec.tolerations must be list"
        if tolerations and any(not isinstance(item, dict) for item in tolerations):
            return False, "template.spec.tolerations entries must be objects"
        affinity = pod_spec.get("affinity") or {}
        if affinity and not isinstance(affinity, dict):
            return False, "template.spec.affinity must be object"
        volumes = pod_spec.get("volumes") or []
        if volumes:
            if not allow_volume_patch:
                return False, "template.spec.volumes requires patch_workload_volume high-risk action"
            if not isinstance(volumes, list):
                return False, "template.spec.volumes must be list"
            for volume in volumes:
                if not isinstance(volume, dict):
                    return False, "volume patch item must be object"
                if set(volume) - {"name", "persistentVolumeClaim"}:
                    return False, "volume patch only permits name and persistentVolumeClaim"
                claim = volume.get("persistentVolumeClaim") or {}
                if not volume.get("name") or not isinstance(claim, dict) or not claim.get("claimName"):
                    return False, "volume patch requires name and persistentVolumeClaim.claimName"
        containers = pod_spec.get("containers") or []
        if containers and not isinstance(containers, list):
            return False, "template.spec.containers must be list"
        for container in containers:
            if not isinstance(container, dict):
                return False, "container patch item must be object"
            illegal_container = set(container) - ALLOWED_CONTAINER_PATCH_KEYS
            if illegal_container:
                return False, f"unsupported container fields: {sorted(illegal_container)}"
            if not container.get("name"):
                return False, "container patch must include name"
            if "image" in container and not str(container.get("image") or "").strip():
                return False, "container image must be a non-empty immutable reference"
            csc = container.get("securityContext") or {}
            if csc and (not isinstance(csc, dict) or set(csc) - ALLOWED_CONTAINER_SECURITY_KEYS):
                return False, "container.securityContext contains unsupported fields"
            for probe_field in ("livenessProbe", "readinessProbe", "startupProbe"):
                if probe_field in container:
                    valid, reason = _validate_probe_patch(container.get(probe_field) or {}, probe_field)
                    if not valid:
                        return False, reason
        init_containers = pod_spec.get("initContainers") or []
        if init_containers:
            if not allow_init_containers:
                return False, "template.spec.initContainers requires explicit high-risk operator confirmation"
            if not isinstance(init_containers, list):
                return False, "template.spec.initContainers must be list"
            for init_container in init_containers:
                valid, reason = _validate_init_container_patch(init_container)
                if not valid:
                    return False, reason
    return True, ""


def _validate_storage_manifest(manifest: dict, kind: str, namespace: str = "") -> tuple[bool, str]:
    if not isinstance(manifest, dict) or manifest.get("apiVersion") != "v1" or manifest.get("kind") != kind:
        return False, f"manifest must be v1 {kind}"
    metadata = manifest.get("metadata") or {}
    name = str(metadata.get("name") or "")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", name or ""):
        return False, "metadata.name is not a valid Kubernetes name"
    spec = manifest.get("spec") or {}
    if kind == "PersistentVolumeClaim":
        if metadata.get("namespace") != namespace:
            return False, "PVC namespace must match the approved operation namespace"
        requests = ((spec.get("resources") or {}).get("requests") or {})
        if not requests.get("storage"):
            return False, "PVC spec.resources.requests.storage is required"
        if not isinstance(spec.get("accessModes") or [], list) or not spec.get("accessModes"):
            return False, "PVC spec.accessModes is required"
        return True, ""
    if kind == "PersistentVolume":
        if spec.get("hostPath"):
            return False, "PV hostPath is not allowed by the AIOps safety policy"
        capacity = (spec.get("capacity") or {}).get("storage")
        if not capacity:
            return False, "PV spec.capacity.storage is required"
        if not isinstance(spec.get("accessModes") or [], list) or not spec.get("accessModes"):
            return False, "PV spec.accessModes is required"
        allow_local = os.getenv("AUTO_OPS_ALLOW_LOCAL_STATIC_PV", "false").lower() in {"1", "true", "yes", "on"}
        allowed_sources = ("nfs", "csi", "fc", "iscsi", "rbd", "cephfs") + (("local",) if allow_local else ())
        if spec.get("local") and not allow_local:
            return False, "PV local source is only allowed when AUTO_OPS_ALLOW_LOCAL_STATIC_PV=true"
        if spec.get("local") and not spec.get("nodeAffinity"):
            return False, "PV local source requires nodeAffinity"
        if not any(spec.get(key) for key in allowed_sources):
            return False, "PV must use an approved network or CSI storage source; hostPath is forbidden"
        claim_ref = spec.get("claimRef") or {}
        if claim_ref and (not claim_ref.get("name") or not claim_ref.get("namespace")):
            return False, "PV claimRef must include namespace and name"
        return True, ""
    return False, f"unsupported storage kind: {kind}"


def _validate_configmap_manifest(manifest: dict, namespace: str = "") -> tuple[bool, str]:
    if not isinstance(manifest, dict) or manifest.get("apiVersion") != "v1" or manifest.get("kind") != "ConfigMap":
        return False, "manifest must be v1 ConfigMap"
    metadata = manifest.get("metadata") or {}
    name = str(metadata.get("name") or "")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", name or ""):
        return False, "metadata.name is not a valid Kubernetes name"
    if namespace and metadata.get("namespace") != namespace:
        return False, "ConfigMap namespace must match the approved operation namespace"
    allowed_top = {"apiVersion", "kind", "metadata", "data", "binaryData", "immutable"}
    illegal = set(manifest) - allowed_top
    if illegal:
        return False, f"unsupported ConfigMap fields: {sorted(illegal)}"
    data = manifest.get("data") or {}
    binary = manifest.get("binaryData") or {}
    if not isinstance(data, dict) or not isinstance(binary, dict):
        return False, "ConfigMap data and binaryData must be objects"
    if not data and not binary:
        return False, "ConfigMap requires data or binaryData from an approved template"
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in data.items()):
        return False, "ConfigMap data must be string key/value pairs"
    return True, ""


def _validate_service_account_patch(patch: dict) -> tuple[bool, str]:
    if not isinstance(patch, dict) or not patch:
        return False, "ServiceAccount patch must be a non-empty object"
    allowed = {"imagePullSecrets"}
    illegal = set(patch) - allowed
    if illegal:
        return False, f"unsupported ServiceAccount fields: {sorted(illegal)}"
    secrets = patch.get("imagePullSecrets") or []
    if not isinstance(secrets, list) or not secrets:
        return False, "imagePullSecrets must be a non-empty list"
    if any(not isinstance(item, dict) or set(item) - {"name"} or not item.get("name") for item in secrets):
        return False, "imagePullSecrets must be a list of {name}"
    return True, ""


async def _collect_plan_deep_evidence(plan: dict) -> dict:
    namespace = plan.get("namespace") or "default"
    pod_name = _target_pod_from_plan(plan)
    cluster_id = plan.get("cluster_id") or "local"
    use_managed = cluster_id in {item.get("id") for item in CLUSTER_REGISTRY.list()}
    use_rancher = plan.get("source") == "rancher" and cluster_id not in {"", "local", "local-cluster"}
    namespace, workload_type, workload_name = _workload_identity_from_plan(plan)
    matching_pods: list[dict] = []
    replica_owner: dict[str, tuple[str, str]] = {}
    if not pod_name:
        if use_managed:
            inventory = await asyncio.to_thread(CLUSTER_REGISTRY.inventory, cluster_id)
            replica_owner = {}
            for replica in inventory.get("replicasets") or []:
                metadata = replica.get("metadata") or {}
                owners = metadata.get("ownerReferences") or []
                if owners:
                    replica_owner[str(metadata.get("name") or "")] = (
                        str(owners[0].get("kind") or "Deployment"),
                        str(owners[0].get("name") or ""),
                    )
            candidates = [
                _normalize_k8s_pod(item, replica_owner)
                for item in inventory.get("pods") or []
                if (item.get("metadata") or {}).get("namespace") == namespace
            ]
        elif use_rancher:
            ns = quote(namespace, safe="")
            try:
                rs_payload = await _rancher_k8s_get(cluster_id, f"/apis/apps/v1/namespaces/{ns}/replicasets", timeout=18)
                for rs in rs_payload.get("items", []) if isinstance(rs_payload, dict) else []:
                    owners = (rs.get("metadata") or {}).get("ownerReferences") or []
                    if owners:
                        owner = owners[0]
                        replica_owner[(rs.get("metadata") or {}).get("name", "")] = (
                            owner.get("kind", "Deployment"),
                            owner.get("name", ""),
                        )
            except Exception:
                replica_owner = {}
            payload = await _rancher_k8s_get(cluster_id, f"/api/v1/namespaces/{ns}/pods", timeout=25)
            candidates = [_normalize_k8s_pod(item, replica_owner) for item in payload.get("items", [])] if isinstance(payload, dict) else []
        else:
            payload = await _call_mcp_tool("list_pods", {"namespace": namespace})
            candidates = payload.get("pods", []) if isinstance(payload, dict) else []
        selected, matching_pods = _select_representative_pod(
            candidates,
            workload_name=workload_name,
            workload_type=workload_type,
        )
        if not selected:
            return {
                "error": "target pod is not known",
                "namespace": namespace,
                "workload_type": workload_type,
                "workload_name": workload_name,
                "candidate_pods": len(candidates),
                "operator_hint": "没有找到属于该 Workload 的 Pod；请确认 Workload 类型/名称是否正确，或检查当前身份是否能 list pods。",
            }
        pod_name = selected.get("name")
        plan["pod_name"] = pod_name
        plan.setdefault("evidence", {})["pod"] = selected
        plan.setdefault("evidence", {})["matching_pods"] = matching_pods[:8]
    if use_managed:
        raw = await asyncio.to_thread(
            CLUSTER_REGISTRY.pod_diagnostics,
            cluster_id,
            namespace=namespace,
            pod_name=pod_name,
            tail_lines=180,
        )
        pod = _normalize_k8s_pod(raw.get("raw_pod") or {}, {})
        workload = _safe_workload_evidence(raw.get("workload") or {}) if raw.get("workload") else {}
        return _redact_sensitive({
            "namespace": namespace,
            "pod_name": pod_name,
            "pod": pod,
            "events": _normalize_k8s_events(raw.get("events") or []),
            "logs": raw.get("logs") or {},
            "workload": workload,
            "storage": raw.get("storage") or [],
            "services": raw.get("services") or [],
            "node": raw.get("node") or {},
            "credential_evidence": raw.get("credential_evidence") or [],
            "source": "kubeconfig",
            "matching_pods": matching_pods[:8],
        })
    if not use_rancher:
        result = await _call_mcp_tool("get_pod_diagnostics", {
            "namespace": namespace, "pod_name": pod_name, "tail_lines": 180,
        })
        return _redact_sensitive(result)

    ns = quote(namespace, safe="")
    pod_q = quote(pod_name, safe="")
    raw_pod = await _rancher_k8s_get(cluster_id, f"/api/v1/namespaces/{ns}/pods/{pod_q}", timeout=25)
    if not replica_owner:
        owners = (raw_pod.get("metadata") or {}).get("ownerReferences") or []
        rs_name = next((owner.get("name") for owner in owners if owner.get("kind") == "ReplicaSet"), "")
        if rs_name:
            try:
                rs = await _rancher_k8s_get(cluster_id, f"/apis/apps/v1/namespaces/{ns}/replicasets/{quote(rs_name, safe='')}", timeout=18)
                rs_owners = (rs.get("metadata") or {}).get("ownerReferences") or []
                if rs_owners:
                    owner = rs_owners[0]
                    replica_owner[rs_name] = (owner.get("kind", "Deployment"), owner.get("name", ""))
            except Exception:
                pass
    pod = _normalize_k8s_pod(raw_pod, replica_owner)
    selector = quote(f"involvedObject.name={pod_name}", safe="=,")
    event_payload = await _rancher_k8s_get(cluster_id, f"/api/v1/namespaces/{ns}/events?fieldSelector={selector}", timeout=20)
    events = _normalize_k8s_events(event_payload.get("items", []) if isinstance(event_payload, dict) else [])
    storage = []
    for volume in (raw_pod.get("spec") or {}).get("volumes", []) or []:
        claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
        if not claim:
            continue
        try:
            pvc = await _rancher_k8s_get(
                cluster_id,
                f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{quote(claim, safe='')}",
                timeout=18,
            )
            pv_name = (pvc.get("spec") or {}).get("volumeName")
            pv = {}
            if pv_name:
                try:
                    pv = await _rancher_k8s_get(cluster_id, f"/api/v1/persistentvolumes/{quote(pv_name, safe='')}", timeout=18)
                except Exception as exc:
                    pv = {"error": f"{type(exc).__name__}: {exc}"}
            storage.append({
                "volume": volume.get("name"),
                "pvc": claim,
                "pvc_phase": (pvc.get("status") or {}).get("phase"),
                "requested": (((pvc.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage"),
                "capacity": ((pvc.get("status") or {}).get("capacity") or {}).get("storage"),
                "storage_class": (pvc.get("spec") or {}).get("storageClassName"),
                "access_modes": (pvc.get("spec") or {}).get("accessModes") or [],
                "pv": pv_name,
                "pv_phase": (pv.get("status") or {}).get("phase"),
                "csi_driver": (((pv.get("spec") or {}).get("csi") or {}).get("driver")),
                "nfs": bool((pv.get("spec") or {}).get("nfs")),
                "pv_error": pv.get("error"),
            })
        except Exception as exc:
            storage.append({
                "volume": volume.get("name"),
                "pvc": claim,
                "missing": True,
                "error": f"{type(exc).__name__}: {exc}",
            })
    services = []
    pod_labels = (raw_pod.get("metadata") or {}).get("labels") or {}
    try:
        service_payload = await _rancher_k8s_get(
            cluster_id, f"/api/v1/namespaces/{ns}/services", timeout=20,
        )
        for service in service_payload.get("items", []) if isinstance(service_payload, dict) else []:
            service_spec = service.get("spec") or {}
            service_selector = service_spec.get("selector") or {}
            if not service_selector or not all(pod_labels.get(key) == value for key, value in service_selector.items()):
                continue
            service_name = str((service.get("metadata") or {}).get("name") or "")
            query = urlencode({"labelSelector": f"kubernetes.io/service-name={service_name}"})
            slices_payload = await _rancher_k8s_get(
                cluster_id,
                f"/apis/discovery.k8s.io/v1/namespaces/{ns}/endpointslices?{query}",
                timeout=20,
            )
            slices = slices_payload.get("items", []) if isinstance(slices_payload, dict) else []
            services.append({
                "name": service_name,
                "type": service_spec.get("type"),
                "selector": service_selector,
                "ports": service_spec.get("ports") or [],
                "ready_endpoints": sum(
                    1
                    for endpoint_slice in slices
                    for endpoint in endpoint_slice.get("endpoints") or []
                    if (endpoint.get("conditions") or {}).get("ready") is not False
                ),
                "endpoint_slices": len(slices),
            })
    except Exception as exc:
        services = [{"error": f"{type(exc).__name__}: {_redact_text(str(exc))}"}]
    node = {}
    node_name = str((raw_pod.get("spec") or {}).get("nodeName") or "")
    if node_name:
        try:
            raw_node = await _rancher_k8s_get(cluster_id, f"/api/v1/nodes/{quote(node_name, safe='')}", timeout=20)
            node = {
                "name": node_name,
                "unschedulable": bool((raw_node.get("spec") or {}).get("unschedulable")),
                "conditions": (raw_node.get("status") or {}).get("conditions") or [],
                "capacity": (raw_node.get("status") or {}).get("capacity") or {},
                "allocatable": (raw_node.get("status") or {}).get("allocatable") or {},
                "taints": (raw_node.get("spec") or {}).get("taints") or [],
            }
        except Exception as exc:
            node = {"name": node_name, "error": f"{type(exc).__name__}: {_redact_text(str(exc))}"}
    logs = {}
    for container in (pod.get("containers") or [])[:8]:
        name = container.get("name")
        if not name:
            continue
        query = urlencode({"tailLines": 180, "container": name})
        current = ""
        current_error = ""
        try:
            current_payload = await _rancher_k8s_get(cluster_id, f"/api/v1/namespaces/{ns}/pods/{pod_q}/log?{query}", timeout=25)
            current = current_payload if isinstance(current_payload, str) else json.dumps(current_payload, ensure_ascii=False)
        except Exception as exc:
            current_error = f"{type(exc).__name__}: {_redact_text(str(exc))}"
        previous = ""
        previous_error = ""
        if container.get("restart_count", 0):
            try:
                previous_payload = await _rancher_k8s_get(cluster_id, f"/api/v1/namespaces/{ns}/pods/{pod_q}/log?{query}&previous=true", timeout=25)
                previous = previous_payload if isinstance(previous_payload, str) else json.dumps(previous_payload, ensure_ascii=False)
            except Exception as exc:
                previous_error = f"{type(exc).__name__}: {_redact_text(str(exc))}"
        logs[name] = {
            "current": _clip_text(current, 5000),
            "current_error": current_error,
            "previous": _clip_text(previous, 5000),
            "previous_error": previous_error,
        }
    workload_type = pod.get("workload_kind") or workload_type or (plan.get("changes") or [{}])[0].get("workload_type") or "Deployment"
    workload_name = pod.get("workload_name") or workload_name
    workload = {}
    if workload_name and str(workload_type).lower() in {"deployment", "statefulset", "daemonset"}:
        try:
            workload = _safe_workload_evidence(
                await _rancher_k8s_get(cluster_id, _workload_api_path(workload_type, namespace, workload_name), timeout=25)
            )
        except Exception as exc:
            workload = {"error": f"{type(exc).__name__}: {exc}"}
    return _redact_sensitive({
        "namespace": namespace, "pod_name": pod_name, "pod": pod, "events": events,
        "logs": logs, "workload": workload, "storage": storage, "services": services,
        "node": node, "source": "rancher",
        "matching_pods": matching_pods[:8],
        "log_errors": {
            name: {key: value for key, value in (content or {}).items() if key.endswith("_error") and value}
            for name, content in logs.items()
            if any((content or {}).get(key) for key in ("current_error", "previous_error"))
        },
    })


async def _collect_ops_step(step: dict, plan: dict) -> dict:
    title = str(step.get("title", "运维步骤"))
    namespace = plan.get("namespace") or "default"
    pod_name = _target_pod_from_plan(plan)
    cluster_id = plan.get("cluster_id") or "local"
    use_rancher = plan.get("source") == "rancher" and cluster_id not in {"", "local", "local-cluster"}
    logs = [
        f"[{datetime.now(timezone.utc).isoformat()}] INIT {title}",
        f"[target] cluster={plan.get('cluster', 'local-cluster')} namespace={namespace} pod={pod_name or '-'} workload={plan.get('target', '-')}",
    ]
    artifacts: dict = {}
    status = "completed"

    probe_id = str(step.get("id") or "")
    deep = plan.get("_runtime_evidence") or {}
    if probe_id and deep and not deep.get("error"):
        artifacts["probe_id"] = probe_id
        if probe_id in {"current_logs", "previous_logs"}:
            key = "previous" if probe_id == "previous_logs" else "current"
            excerpts = {
                name: _clip_text((content or {}).get(key, ""), 2600)
                for name, content in (deep.get("logs") or {}).items()
            }
            errors = {
                name: (content or {}).get(f"{key}_error", "")
                for name, content in (deep.get("logs") or {}).items()
                if (content or {}).get(f"{key}_error", "")
            }
            artifacts[f"{key}_logs"] = excerpts
            if errors:
                artifacts[f"{key}_log_errors"] = errors
            for name, excerpt in excerpts.items():
                logs.append(f"[{key}-logs] container={name} chars={len(excerpt)}")
                logs.extend(f"[log] {line}" for line in excerpt.splitlines()[-10:] if line.strip())
            for name, error in errors.items():
                status = "warning"
                logs.append(f"[warn] {key} logs unavailable for container={name}: {error}")
        elif probe_id == "events":
            artifacts["events"] = (deep.get("events") or [])[-20:]
            logs.extend(
                f"[event] {event.get('type','?')} {event.get('reason','?')} - {event.get('message','')}"
                for event in artifacts["events"]
            )
        elif probe_id == "workload_spec":
            artifacts["workload"] = deep.get("workload") or {}
            logs.append("[workload] fetched live workload template and rollout status")
        elif probe_id in {"service_endpoints", "dns", "network_policy", "mesh_routes", "dependency_topology"}:
            artifacts["network"] = {"services": deep.get("services", []), "pod": deep.get("pod", {})}
            logs.append(f"[network] services/endpoints evidence count={len(deep.get('services') or [])}")
        elif probe_id in {"storage_chain", "node_storage", "csi_status", "pvc_binding", "pod_security_context"}:
            artifacts["storage"] = deep.get("storage", [])
            artifacts["pod_security_context"] = (deep.get("pod") or {}).get("security_context", {})
            logs.append(f"[storage] pvc/pv chain evidence count={len(deep.get('storage') or [])}")
        elif probe_id in {"node_conditions", "node_capacity", "node_pressure", "node_labels", "system_pods"}:
            artifacts["node"] = deep.get("node", {})
            logs.append(f"[node] collected node condition/capacity evidence for {(deep.get('node') or {}).get('name','-')}")
        else:
            artifacts["evidence"] = deep
            logs.append(f"[probe] {probe_id} evaluated against collected evidence bundle")
        return {
            **step, "status": status, "logs": _redact_sensitive(logs),
            "artifacts": _redact_sensitive(artifacts), "finished_at": datetime.now(timezone.utc).isoformat(),
        }

    try:
        if "日志" in title and pod_name and use_rancher:
            result = await _rancher_k8s_get(
                cluster_id,
                f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(pod_name, safe='')}/log?tailLines=120",
                timeout=25,
            )
            excerpt = _redact_text(_clip_text(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False), 2600))
            artifacts["logs_excerpt"] = excerpt
            logs.append(f"[rancher/logs] pulled {len(excerpt)} chars from pod/{pod_name}")
            logs.extend([f"[log] {line}" for line in excerpt.splitlines()[-12:] if line.strip()])
        elif any(key in title for key in ["事件", "镜像", "网络", "存储", "配置"]) and pod_name and use_rancher:
            selector = quote(f"involvedObject.name={pod_name}", safe="=,")
            result = await _rancher_k8s_get(
                cluster_id,
                f"/api/v1/namespaces/{quote(namespace, safe='')}/events?fieldSelector={selector}",
                timeout=20,
            )
            events = _redact_sensitive(_normalize_k8s_events(result.get("items", []) if isinstance(result, dict) else []))
            artifacts["events"] = events
            logs.append(f"[rancher/events] pulled {len(events)} events for pod/{pod_name}")
            logs.extend([
                f"[event] {e.get('type','?')} {e.get('reason','?')} x{e.get('count','?')} - {e.get('message','')}"
                for e in events
            ])
        elif "日志" in title and pod_name:
            result = await _call_mcp_tool("get_pod_logs", {
                "namespace": namespace,
                "pod_name": pod_name,
                "tail_lines": 120,
            })
            if result.get("error"):
                status = "warning"
                logs.append(f"[warn] get_pod_logs failed: {_redact_text(str(result.get('error')))}")
            else:
                excerpt = _redact_text(_clip_text(result.get("logs", ""), 2600))
                artifacts["logs_excerpt"] = excerpt
                logs.append(f"[logs] pulled {len(result.get('logs', ''))} bytes from pod/{pod_name}")
                logs.extend([f"[log] {line}" for line in excerpt.splitlines()[-12:] if line.strip()])
        elif any(key in title for key in ["事件", "镜像", "网络", "存储", "配置"]) and pod_name:
            result = await _call_mcp_tool("get_pod_events", {
                "namespace": namespace,
                "pod_name": pod_name,
            })
            if result.get("error"):
                status = "warning"
                logs.append(f"[warn] get_pod_events failed: {_redact_text(str(result.get('error')))}")
            else:
                events = _redact_sensitive(result.get("events", [])[-12:])
                artifacts["events"] = events
                logs.append(f"[events] pulled {len(result.get('events', []))} events for pod/{pod_name}")
                logs.extend([
                    f"[event] {e.get('type','?')} {e.get('reason','?')} x{e.get('count','?')} - {e.get('message','')}"
                    for e in events
                ])
        else:
            evidence = plan.get("evidence") or {}
            state_text = evidence.get("state_text") if isinstance(evidence, dict) else ""
            if state_text:
                artifacts["state_text"] = _redact_text(_clip_text(state_text, 1200))
                logs.append(f"[state] {_redact_text(_clip_text(state_text, 500))}")
            else:
                logs.append("[analysis] no direct pod artifact attached; using planned change context")
    except Exception as e:
        status = "warning"
        logs.append(f"[warn] step probe failed: {type(e).__name__}: {_redact_text(str(e))}")

    return {
        **step,
        "status": status,
        "logs": _redact_sensitive(logs),
        "artifacts": _redact_sensitive(artifacts),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


async def _execute_change(change: dict, plan: dict) -> dict:
    ctype = change.get("type")
    namespace = change.get("namespace", plan.get("namespace", "default"))
    workload_name = change.get("workload_name", "")
    workload_type = change.get("workload_type", "Deployment")
    cluster_id = plan.get("cluster_id") or change.get("cluster_id") or "local"
    managed_cluster_ids = {item.get("id") for item in CLUSTER_REGISTRY.list()}
    use_managed_cluster = cluster_id in managed_cluster_ids
    use_rancher = plan.get("source") == "rancher" and cluster_id not in {"", "local", "local-cluster"}
    workload_change_types = {
        "restart", "patch_workload", "patch_workload_volume", "patch_workload_runtime_security",
        "patch", "scale_out", "rollback_workload",
    }
    if ctype in workload_change_types and not workload_name:
        return {
            "change": _redact_sensitive(change),
            "status": "failed",
            "result": {"error": "workload_name is required before executing a Kubernetes change"},
        }

    registered_type = "patch_workload" if ctype == "patch" else ctype
    operator_confirmed = bool(
        plan.get("high_risk_confirmed")
        or plan.get("operator_force_execute")
        or change.get("human_approved")
        or change.get("operator_confirmed")
    )
    if operator_confirmed:
        change["human_approved"] = True
        change["operator_confirmed"] = True
    policy_change = {**change, "type": registered_type, "human_approved": operator_confirmed}
    valid_action, action_reason = validate_change(policy_change)
    if not valid_action:
        return {
            "change": _redact_sensitive(change),
            "status": "blocked",
            "result": {"error": action_reason, "requires_high_risk_confirmation": "high risk" in action_reason},
        }

    try:
        if ctype in {"run_shell", "exec_pod", "exec_node"}:
            command = str(change.get("command") or "").strip()
            timeout_seconds = max(1, min(int(change.get("timeout_seconds") or 120), 900))
            if not command or len(command) > 20000 or "\x00" in command:
                result = {"error": "Shell command is empty, too large, or contains invalid bytes"}
            elif ctype == "run_shell":
                result = await asyncio.to_thread(CLUSTER_REGISTRY.run_shell, command, timeout_seconds)
            elif ctype == "exec_pod":
                pod_name = str(change.get("pod_name") or "").strip()
                if not pod_name:
                    result = {"error": "exec_pod requires pod_name"}
                elif use_managed_cluster:
                    result = await asyncio.to_thread(
                        CLUSTER_REGISTRY.exec_pod,
                        cluster_id,
                        namespace=namespace,
                        pod_name=pod_name,
                        container_name=str(change.get("container_name") or ""),
                        command=command,
                        timeout_seconds=timeout_seconds,
                    )
                elif use_rancher:
                    configuration = await _rancher_execution_configuration(cluster_id)
                    result = await asyncio.to_thread(
                        CLUSTER_REGISTRY.exec_pod_with_configuration,
                        configuration,
                        namespace=namespace,
                        pod_name=pod_name,
                        container_name=str(change.get("container_name") or ""),
                        command=command,
                        timeout_seconds=timeout_seconds,
                    )
                else:
                    result = {"error": "exec_pod 要求目标集群通过 Rancher 或加密 kubeconfig 纳管"}
            else:
                node_name = str(change.get("node_name") or "").strip()
                if not node_name:
                    result = {"error": "exec_node requires node_name"}
                elif use_managed_cluster:
                    result = await asyncio.to_thread(
                        CLUSTER_REGISTRY.exec_node,
                        cluster_id,
                        node_name=node_name,
                        command=command,
                        timeout_seconds=timeout_seconds,
                        image=str(change.get("executor_image") or os.getenv("NODE_EXEC_IMAGE", "busybox:1.36")),
                    )
                elif use_rancher:
                    configuration = await _rancher_execution_configuration(cluster_id)
                    result = await asyncio.to_thread(
                        CLUSTER_REGISTRY.exec_node_with_configuration,
                        configuration,
                        node_name=node_name,
                        command=command,
                        timeout_seconds=timeout_seconds,
                        image=str(change.get("executor_image") or os.getenv("NODE_EXEC_IMAGE", "busybox:1.36")),
                    )
                else:
                    result = {"error": "exec_node 要求目标集群通过 Rancher 或加密 kubeconfig 纳管"}
            if isinstance(result, dict) and result.get("exit_code") not in {None, 0}:
                result["error"] = f"command exited with code {result.get('exit_code')}"
        elif use_managed_cluster and ctype == "apply_manifest":
            result = await asyncio.to_thread(CLUSTER_REGISTRY.apply_manifest, cluster_id, change.get("manifest") or {})
        elif use_managed_cluster and ctype == "patch_resource":
            result = await asyncio.to_thread(
                CLUSTER_REGISTRY.patch_resource,
                cluster_id,
                api_version=str(change.get("api_version") or change.get("apiVersion") or "v1"),
                kind=str(change.get("kind") or ""),
                name=str(change.get("name") or ""),
                namespace=str(change.get("namespace") or namespace),
                patch=change.get("patch") or {},
            )
        elif use_managed_cluster and ctype == "delete_resource":
            result = await asyncio.to_thread(
                CLUSTER_REGISTRY.delete_resource,
                cluster_id,
                api_version=str(change.get("api_version") or change.get("apiVersion") or "v1"),
                kind=str(change.get("kind") or ""),
                name=str(change.get("name") or ""),
                namespace=str(change.get("namespace") or namespace),
            )
        elif use_rancher and ctype == "apply_manifest":
            result = await _rancher_apply_manifest(
                cluster_id,
                change.get("manifest") or {},
                namespace=str(change.get("namespace") or namespace),
            )
        elif use_rancher and ctype == "patch_resource":
            _, item_path = await _rancher_dynamic_resource_paths(
                cluster_id,
                api_version=str(change.get("api_version") or change.get("apiVersion") or "v1"),
                kind=str(change.get("kind") or ""),
                namespace=str(change.get("namespace") or namespace),
                name=str(change.get("name") or ""),
            )
            result = await _rancher_k8s_patch(
                cluster_id,
                item_path,
                change.get("patch") or {},
                timeout=40,
                content_type="application/merge-patch+json",
            )
        elif use_rancher and ctype == "delete_resource":
            _, item_path = await _rancher_dynamic_resource_paths(
                cluster_id,
                api_version=str(change.get("api_version") or change.get("apiVersion") or "v1"),
                kind=str(change.get("kind") or ""),
                namespace=str(change.get("namespace") or namespace),
                name=str(change.get("name") or ""),
            )
            result = await _rancher_k8s_delete(
                cluster_id,
                item_path,
                {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Background"},
                timeout=40,
            )
        elif use_managed_cluster and ctype in workload_change_types:
            manifest_kind = str(workload_type or "Deployment")
            api_version = "apps/v1"
            patch = _materialize_patch(change.get("patch") or {})
            if ctype == "restart":
                patch = _materialize_patch({"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "<now>"}}}}})
            elif ctype == "scale_out":
                patch = {"spec": {"replicas": int(change.get("replicas") or 2)}}
            result = await asyncio.to_thread(
                CLUSTER_REGISTRY.patch_resource,
                cluster_id,
                api_version=api_version,
                kind=manifest_kind,
                name=workload_name,
                namespace=namespace,
                patch=patch,
            )
        elif _is_infrastructure_action(str(ctype)):
            result = await _execute_infrastructure_action(change, plan)
        elif use_rancher and ctype in workload_change_types:
            patch = _materialize_patch(change.get("patch") or {})
            if ctype == "restart":
                patch = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "<now>"}}}}}
                patch = _materialize_patch(patch)
            elif ctype == "scale_out":
                patch = {"spec": {"replicas": int(change.get("replicas") or 2)}}
            valid, reason = _validate_workload_patch(
                patch,
                allow_volume_patch=ctype == "patch_workload_volume",
                allow_init_containers=operator_confirmed,
            )
            if not valid:
                result = {"error": f"patch rejected by safety policy: {reason}"}
            else:
                result = await _rancher_k8s_patch(
                    cluster_id,
                    _workload_api_path(workload_type, namespace, workload_name),
                    patch,
                    timeout=35,
                )
        elif use_rancher and ctype == "create_workload":
            manifest = change.get("manifest") or {}
            result = await _rancher_k8s_post(
                cluster_id,
                _workload_collection_api_path(manifest.get("kind") or workload_type, namespace),
                manifest,
                timeout=40,
            )
        elif use_rancher and ctype == "patch_service":
            result = await _rancher_k8s_patch(
                cluster_id,
                f"/api/v1/namespaces/{quote(namespace, safe='')}/services/{quote(change.get('service_name',''), safe='')}",
                change.get("patch") or {},
                timeout=35,
            )
        elif use_rancher and ctype == "patch_service_account":
            patch = change.get("patch") or {"imagePullSecrets": [{"name": change.get("image_pull_secret", "")}]}
            valid, reason = _validate_service_account_patch(patch)
            if not valid:
                result = {"error": f"ServiceAccount patch rejected by safety policy: {reason}"}
            else:
                result = await _rancher_k8s_patch(
                    cluster_id,
                    f"/api/v1/namespaces/{quote(namespace, safe='')}/serviceaccounts/{quote(change.get('service_account','default'), safe='')}",
                    patch,
                    timeout=35,
                )
        elif use_rancher and ctype == "create_configmap":
            manifest = change.get("manifest") or {}
            valid, reason = _validate_configmap_manifest(manifest, namespace)
            if not valid:
                result = {"error": f"ConfigMap manifest rejected by safety policy: {reason}"}
            else:
                result = await _rancher_k8s_post(
                    cluster_id,
                    f"/api/v1/namespaces/{quote(namespace, safe='')}/configmaps",
                    manifest,
                    timeout=35,
                )
        elif use_rancher and ctype == "patch_pdb":
            result = await _rancher_k8s_patch(
                cluster_id,
                f"/apis/policy/v1/namespaces/{quote(namespace, safe='')}/poddisruptionbudgets/{quote(change.get('pdb_name',''), safe='')}",
                change.get("patch") or {},
                timeout=35,
            )
        elif use_rancher and ctype == "create_pvc":
            manifest = change.get("manifest") or {}
            valid, reason = _validate_storage_manifest(manifest, "PersistentVolumeClaim", namespace)
            if not valid:
                result = {"error": f"PVC manifest rejected by safety policy: {reason}"}
            else:
                result = await _rancher_k8s_post(
                    cluster_id,
                    f"/api/v1/namespaces/{quote(namespace, safe='')}/persistentvolumeclaims",
                    manifest,
                    timeout=35,
                )
        elif use_rancher and ctype == "create_pv":
            manifest = change.get("manifest") or {}
            valid, reason = _validate_storage_manifest(manifest, "PersistentVolume", namespace)
            if not valid:
                result = {"error": f"PV manifest rejected by safety policy: {reason}"}
            else:
                result = await _rancher_k8s_post(cluster_id, "/api/v1/persistentvolumes", manifest, timeout=35)
        elif use_rancher and ctype == "recreate_pod":
            pod_name = change.get("pod_name") or _target_pod_from_plan(plan)
            if not pod_name:
                result = {"error": "recreate_pod requires pod_name"}
            else:
                result = await _rancher_k8s_delete(
                    cluster_id,
                    f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(pod_name, safe='')}",
                    {"apiVersion": "v1", "kind": "DeleteOptions", "gracePeriodSeconds": max(0, min(int(change.get('grace_period_seconds') or 30), 120)), "propagationPolicy": "Background"},
                    timeout=35,
                )
        elif use_rancher and ctype == "evict_pod":
            pod_name = change.get("pod_name") or _target_pod_from_plan(plan)
            grace = max(0, min(int(change.get("grace_period_seconds") or 30), 120))
            body = {
                "apiVersion": "policy/v1", "kind": "Eviction",
                "metadata": {"name": pod_name, "namespace": namespace},
                "deleteOptions": {"gracePeriodSeconds": grace, "propagationPolicy": "Background"},
            }
            result = await _rancher_k8s_post(
                cluster_id,
                f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(pod_name, safe='')}/eviction",
                body,
                timeout=35,
            )
        elif use_rancher and ctype == "patch_hpa":
            hpa_name = change.get("hpa_name", "")
            patch = {"spec": {}}
            if change.get("min_replicas") is not None:
                patch["spec"]["minReplicas"] = int(change["min_replicas"])
            if change.get("max_replicas") is not None:
                patch["spec"]["maxReplicas"] = int(change["max_replicas"])
            result = await _rancher_k8s_patch(
                cluster_id,
                f"/apis/autoscaling/v2/namespaces/{quote(namespace, safe='')}/horizontalpodautoscalers/{quote(hpa_name, safe='')}",
                patch,
                timeout=35,
            )
        elif use_rancher and ctype == "expand_pvc":
            pvc_name = change.get("pvc_name", "")
            patch = {"spec": {"resources": {"requests": {"storage": change.get("storage")}}}}
            result = await _rancher_k8s_patch(
                cluster_id,
                f"/api/v1/namespaces/{quote(namespace, safe='')}/persistentvolumeclaims/{quote(pvc_name, safe='')}",
                patch,
                timeout=35,
            )
        elif use_rancher and ctype in {"cordon_node", "uncordon_node"}:
            result = await _rancher_k8s_patch(
                cluster_id,
                f"/api/v1/nodes/{quote(change.get('node_name', ''), safe='')}",
                {"spec": {"unschedulable": False if ctype == "uncordon_node" else bool(change.get("unschedulable", True))}},
                timeout=35,
            )
        elif ctype == "restart":
            if str(workload_type).lower() == "deployment":
                result = await _call_mcp_tool("restart_deployment", {
                    "namespace": namespace,
                    "deployment_name": workload_name,
                    "dry_run": False,
                })
            else:
                patch = _materialize_patch(change.get("patch") or {})
                valid, reason = _validate_workload_patch(patch, allow_init_containers=operator_confirmed)
                if not valid:
                    return {
                        "change": _redact_sensitive(change),
                        "status": "failed",
                        "result": {"error": f"patch rejected by safety policy: {reason}"},
                    }
                result = await _call_mcp_tool("patch_workload", {
                    "namespace": namespace,
                    "workload_type": workload_type,
                    "workload_name": workload_name,
                    "patch": patch,
                    "dry_run": False,
                })
        elif ctype in {"patch_workload", "patch_workload_volume", "patch_workload_runtime_security", "patch", "rollback_workload"}:
            patch = _materialize_patch(change.get("patch") or {})
            valid, reason = _validate_workload_patch(
                patch,
                allow_volume_patch=ctype == "patch_workload_volume",
                allow_init_containers=operator_confirmed,
            )
            if not valid:
                return {
                    "change": _redact_sensitive(change),
                    "status": "failed",
                    "result": {"error": f"patch rejected by safety policy: {reason}"},
                }
            result = await _call_mcp_tool("patch_workload", {
                "namespace": namespace,
                "workload_type": workload_type,
                "workload_name": workload_name,
                "patch": patch,
                "high_risk_volume_patch": ctype in {"patch_workload_volume", "patch_workload_runtime_security"},
                "dry_run": False,
            })
        elif ctype == "create_workload":
            result = await _call_mcp_tool("create_workload", {
                "manifest": change.get("manifest") or {},
                "dry_run": False,
            })
        elif ctype == "patch_service":
            result = await _call_mcp_tool("patch_service", {
                "namespace": namespace,
                "service_name": change.get("service_name", ""),
                "patch": change.get("patch") or {},
                "dry_run": False,
            })
        elif ctype == "patch_service_account":
            patch = change.get("patch") or {"imagePullSecrets": [{"name": change.get("image_pull_secret", "")}]}
            valid, reason = _validate_service_account_patch(patch)
            if not valid:
                result = {"error": f"ServiceAccount patch rejected by safety policy: {reason}"}
            else:
                result = await _call_mcp_tool("patch_service_account", {
                    "namespace": namespace,
                    "service_account": change.get("service_account", "default"),
                    "patch": patch,
                    "dry_run": False,
                })
        elif ctype == "create_configmap":
            manifest = change.get("manifest") or {}
            valid, reason = _validate_configmap_manifest(manifest, namespace)
            if not valid:
                result = {"error": f"ConfigMap manifest rejected by safety policy: {reason}"}
            else:
                result = await _call_mcp_tool("create_configmap", {
                    "namespace": namespace,
                    "manifest": manifest,
                    "dry_run": False,
                })
        elif ctype == "patch_pdb":
            result = await _call_mcp_tool("patch_pdb", {
                "namespace": namespace,
                "pdb_name": change.get("pdb_name", ""),
                "patch": change.get("patch") or {},
                "dry_run": False,
            })
        elif ctype == "scale_out":
            if str(workload_type).lower() == "deployment":
                result = await _call_mcp_tool("scale_deployment", {
                    "namespace": namespace,
                    "deployment_name": workload_name,
                    "replicas": int(change.get("replicas") or 2),
                    "dry_run": False,
                })
            else:
                patch = {"spec": {"replicas": int(change.get("replicas") or 2)}}
                valid, reason = _validate_workload_patch(patch, allow_init_containers=operator_confirmed)
                if not valid:
                    return {
                        "change": _redact_sensitive(change),
                        "status": "failed",
                        "result": {"error": f"patch rejected by safety policy: {reason}"},
                    }
                result = await _call_mcp_tool("patch_workload", {
                    "namespace": namespace,
                    "workload_type": workload_type,
                    "workload_name": workload_name,
                    "patch": patch,
                    "dry_run": False,
                })
        elif ctype == "recreate_pod":
            result = await _call_mcp_tool("recreate_pod", {
                "namespace": namespace,
                "pod_name": change.get("pod_name") or _target_pod_from_plan(plan),
                "grace_period_seconds": int(change.get("grace_period_seconds") or 30),
                "dry_run": False,
            })
        elif ctype == "evict_pod":
            result = await _call_mcp_tool("evict_pod", {
                "namespace": namespace,
                "pod_name": change.get("pod_name") or _target_pod_from_plan(plan),
                "grace_period_seconds": int(change.get("grace_period_seconds") or 30),
                "dry_run": False,
            })
        elif ctype == "patch_hpa":
            result = await _call_mcp_tool("patch_hpa", {
                "namespace": namespace,
                "hpa_name": change.get("hpa_name", ""),
                "min_replicas": change.get("min_replicas"),
                "max_replicas": change.get("max_replicas"),
                "dry_run": False,
            })
        elif ctype == "expand_pvc":
            result = await _call_mcp_tool("expand_pvc", {
                "namespace": namespace,
                "pvc_name": change.get("pvc_name", ""),
                "storage": change.get("storage", ""),
                "dry_run": False,
            })
        elif ctype == "create_pvc":
            manifest = change.get("manifest") or {}
            valid, reason = _validate_storage_manifest(manifest, "PersistentVolumeClaim", namespace)
            if not valid:
                result = {"error": f"PVC manifest rejected by safety policy: {reason}"}
            else:
                result = await _call_mcp_tool("create_pvc", {
                    "namespace": namespace,
                    "manifest": manifest,
                    "dry_run": False,
                })
        elif ctype == "create_pv":
            manifest = change.get("manifest") or {}
            valid, reason = _validate_storage_manifest(manifest, "PersistentVolume", namespace)
            if not valid:
                result = {"error": f"PV manifest rejected by safety policy: {reason}"}
            else:
                result = await _call_mcp_tool("create_persistent_volume", {
                    "manifest": manifest,
                    "dry_run": False,
                })
        elif ctype == "cordon_node":
            result = await _call_mcp_tool("cordon_node", {
                "node_name": change.get("node_name", ""),
                "unschedulable": bool(change.get("unschedulable", True)),
                "dry_run": False,
            })
        elif ctype == "uncordon_node":
            result = await _call_mcp_tool("cordon_node", {
                "node_name": change.get("node_name", ""),
                "unschedulable": False,
                "dry_run": False,
            })
        else:
            result = {"error": f"Unsupported change type: {ctype}"}
    except Exception as e:
        result = {
            "error": _redact_text(str(e)),
            "type": type(e).__name__,
            "trace": _redact_text(traceback.format_exc(limit=4)),
        }

    outcome = {
        "change": _redact_sensitive(change),
        "status": "failed" if isinstance(result, dict) and result.get("error") else "completed",
        "result": _redact_sensitive(result),
    }
    guidance = _permission_guidance(outcome["result"], plan, change)
    if guidance and isinstance(outcome["result"], dict):
        outcome["permission_guidance"] = guidance
        outcome["result"]["permission_guidance"] = guidance
        outcome["result"]["operator_steps"] = guidance.get("do_this") or []
    try:
        if ctype in {"apply_manifest", "patch_resource", "delete_resource"}:
            manifest = change.get("manifest") or {}
            audit_target = "/".join(filter(None, [
                str(change.get("kind") or manifest.get("kind") or "Resource"),
                str(change.get("name") or (manifest.get("metadata") or {}).get("name") or ""),
            ]))
        else:
            audit_target = f"{workload_type}/{workload_name}"
        _audit_event(
            "infrastructure.change" if _is_infrastructure_action(str(ctype)) else "kubernetes.workload.change",
            str(plan.get("_operator") or "unknown"),
            (
                f"{change.get('resource_type')}/{change.get('resource_id')}"
                if _is_infrastructure_action(str(ctype))
                else f"{cluster_id}/{namespace}/{audit_target}"
            ),
            outcome["status"],
            change_type=ctype,
            patch=change.get("patch") or {},
            result=outcome["result"],
        )
    except Exception as audit_exc:
        # 审计输出失败不能把真实运维结果伪装成 HTTP 500。失败信息仍回传给前端，
        # 便于运维人员修复日志采集或持久化目录权限。
        outcome.setdefault("result", {})["audit_warning"] = f"{type(audit_exc).__name__}: {_redact_text(str(audit_exc))}"
    return outcome


async def _probe_plan_recovery(plan: dict, results: list[dict]) -> dict:
    if any(r.get("status") in {"failed", "blocked"} for r in results):
        return {"status": "skipped", "recovered": False, "message": "存在变更 API 失败，跳过恢复验证并进入替代策略。"}

    first_change = (plan.get("changes") or [{}])[0]
    change_type = first_change.get("type")
    namespace, workload_type, workload_name = _workload_identity_from_plan(plan)
    workload_scoped = str(workload_type or "").lower() in {
        "deployment", "statefulset", "daemonset", "replicaset",
    } and bool(workload_name)
    resource_verification: dict | None = None
    if not plan.get("changes") and not workload_scoped:
        return {
            "status": "skipped",
            "recovered": None,
            "message": "本次只读诊断没有绑定可验证的 Workload，不能宣称已经恢复。",
        }
    if _is_infrastructure_action(str(change_type)):
        resource_type = first_change.get("resource_type") or plan.get("resource_type") or "all"
        resource_id = first_change.get("resource_id") or plan.get("resource_id") or ""
        if not resource_id:
            return {"status": "skipped", "recovered": None, "message": "基础设施变更缺少 resource_id，无法自动验证。"}
        try:
            scan = await scan_infrastructure_provider_resources(resource_type, resource_id, include_probe=True)
            recovered = int(scan.get("finding_count") or 0) == 0
            return {
                "status": "verified" if recovered else "needs_followup",
                "recovered": recovered,
                "message": "基础设施探测恢复正常。" if recovered else "基础设施探测仍有异常或证据不足，需要查看执行器回执和人工复核。",
                "resource_id": resource_id,
                "resource_type": resource_type,
                "scan_summary": scan.get("summary") or {},
            }
        except Exception as exc:
            return {
                "status": "skipped",
                "recovered": None,
                "message": f"基础设施恢复验证失败：{type(exc).__name__}: {_redact_text(str(exc))}",
            }
    if change_type in {"patch_hpa", "expand_pvc", "create_pvc", "create_pv", "cordon_node", "patch_service_account", "create_configmap"}:
        namespace = first_change.get("namespace") or plan.get("namespace") or "default"
        cluster_id = plan.get("cluster_id") or "local"
        use_rancher = plan.get("source") == "rancher" and cluster_id not in {"", "local", "local-cluster"}
        if change_type == "patch_hpa":
            kind, name = "HPA", first_change.get("hpa_name", "")
            path = f"/apis/autoscaling/v2/namespaces/{quote(namespace, safe='')}/horizontalpodautoscalers/{quote(name, safe='')}"
        elif change_type in {"expand_pvc", "create_pvc"}:
            kind, name = "PVC", first_change.get("pvc_name", "")
            path = f"/api/v1/namespaces/{quote(namespace, safe='')}/persistentvolumeclaims/{quote(name, safe='')}"
        elif change_type == "create_pv":
            kind = "PV"
            name = ((first_change.get("manifest") or {}).get("metadata") or {}).get("name", "")
            path = f"/api/v1/persistentvolumes/{quote(name, safe='')}"
        elif change_type == "create_configmap":
            kind = "ConfigMap"
            name = first_change.get("configmap_name") or (((first_change.get("manifest") or {}).get("metadata") or {}).get("name", ""))
            path = f"/api/v1/namespaces/{quote(namespace, safe='')}/configmaps/{quote(name, safe='')}"
        elif change_type == "patch_service_account":
            kind = "ServiceAccount"
            name = first_change.get("service_account", "default")
            path = f"/api/v1/namespaces/{quote(namespace, safe='')}/serviceaccounts/{quote(name, safe='')}"
        else:
            kind, name = "Node", first_change.get("node_name", "")
            path = f"/api/v1/nodes/{quote(name, safe='')}"
        try:
            if use_rancher:
                raw = await _rancher_k8s_get(cluster_id, path, timeout=25)
                if kind == "HPA":
                    state = {"spec": raw.get("spec") or {}, "status": raw.get("status") or {}}
                elif kind == "PVC":
                    state = {"spec": {"requested": ((((raw.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage"))}, "status": raw.get("status") or {}}
                elif kind == "PV":
                    state = {"spec": raw.get("spec") or {}, "status": raw.get("status") or {}}
                elif kind == "ConfigMap":
                    state = {
                        "kind": "ConfigMap",
                        "name": name,
                        "namespace": namespace,
                        "data_keys": sorted(((raw.get("data") or {}).keys()))[:80],
                        "immutable": bool(raw.get("immutable")),
                    }
                elif kind == "ServiceAccount":
                    state = {
                        "kind": "ServiceAccount",
                        "name": name,
                        "namespace": namespace,
                        "imagePullSecrets": raw.get("imagePullSecrets") or [],
                    }
                else:
                    state = {"spec": {"unschedulable": bool((raw.get("spec") or {}).get("unschedulable"))}, "status": raw.get("status") or {}}
            else:
                state = await _call_mcp_tool("get_remediation_target_state", {"kind": kind, "name": name, "namespace": namespace})
            if state.get("error"):
                raise RuntimeError(str(state["error"]))
            if kind == "HPA":
                expected = {k: first_change.get(k) for k in ("min_replicas", "max_replicas") if first_change.get(k) is not None}
                actual = {"min_replicas": (state.get("spec") or {}).get("minReplicas"), "max_replicas": (state.get("spec") or {}).get("maxReplicas")}
                recovered = all(actual.get(key) == int(value) for key, value in expected.items())
            elif kind == "PVC":
                expected = {"storage": first_change.get("storage") or ((((first_change.get("manifest") or {}).get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage")}
                actual = {"storage": (state.get("spec") or {}).get("requested")}
                recovered = actual["storage"] == expected["storage"] and (state.get("status") or {}).get("phase") == "Bound"
            elif kind == "PV":
                expected = {"claim": first_change.get("pvc_name")}
                actual = {"phase": (state.get("status") or {}).get("phase"), "claim": (((state.get("spec") or {}).get("claimRef") or {}).get("name"))}
                recovered = bool(actual["claim"] == expected["claim"] and actual["phase"] in {"Available", "Bound", "Released", None, ""})
            elif kind == "ConfigMap":
                expected = {"name": name, "data_keys": sorted((((first_change.get("manifest") or {}).get("data") or {}).keys()))[:80]}
                actual = {"name": state.get("name"), "data_keys": state.get("data_keys") or []}
                recovered = bool(actual["name"] == expected["name"] and set(expected["data_keys"]).issubset(set(actual["data_keys"])))
            elif kind == "ServiceAccount":
                expected = {"image_pull_secret": first_change.get("image_pull_secret") or (((first_change.get("patch") or {}).get("imagePullSecrets") or [{}])[0].get("name"))}
                actual = {"image_pull_secrets": [item.get("name") for item in (state.get("imagePullSecrets") or []) if isinstance(item, dict)]}
                recovered = bool(expected["image_pull_secret"] in actual["image_pull_secrets"])
            else:
                expected = {"unschedulable": bool(first_change.get("unschedulable", True))}
                actual = {"unschedulable": bool((state.get("spec") or {}).get("unschedulable"))}
                recovered = actual == expected
            resource_verification = {
                "status": "completed", "recovered": recovered, "target": f"{kind}/{name}",
                "message": "目标资源状态已与计划一致。" if recovered else "目标资源尚未收敛到计划状态。",
                "expected": expected, "actual": actual, "state": _redact_sensitive(state),
            }
            if not recovered:
                return resource_verification
            if not workload_scoped:
                return {
                    **resource_verification,
                    "status": "unknown",
                    "recovered": None,
                    "message": "目标资源状态已符合计划，但没有绑定受影响的 Workload/业务探针，不能据此宣称故障已经恢复。",
                }
        except Exception as exc:
            return {"status": "unknown", "recovered": None, "target": f"{kind}/{name}", "message": "无法读取目标资源状态。", "errors": [f"{type(exc).__name__}: {exc}"]}

    pod_name = _target_pod_from_plan(plan)
    cluster_id = plan.get("cluster_id") or "local"
    use_managed = cluster_id in {item.get("id") for item in CLUSTER_REGISTRY.list()}
    use_rancher = plan.get("source") == "rancher" and cluster_id not in {"", "local", "local-cluster"}
    matched: list[dict] = []
    errors: list[str] = []

    try:
        if use_managed:
            inventory = await asyncio.to_thread(CLUSTER_REGISTRY.inventory, cluster_id)
            replica_owner: dict[str, tuple[str, str]] = {}
            for replica in inventory.get("replicasets") or []:
                metadata = replica.get("metadata") or {}
                owners = metadata.get("ownerReferences") or []
                if owners:
                    replica_owner[str(metadata.get("name") or "")] = (
                        str(owners[0].get("kind") or "Deployment"),
                        str(owners[0].get("name") or ""),
                    )
            for raw in inventory.get("pods") or []:
                if (raw.get("metadata") or {}).get("namespace") != namespace:
                    continue
                pod = _normalize_k8s_pod(raw, replica_owner)
                if pod_name and pod.get("name") == pod_name:
                    matched.append(pod)
                elif workload_name and _pod_matches_workload(pod, workload_name, workload_type):
                    matched.append(pod)
        elif use_rancher:
            replica_owner: dict[str, tuple[str, str]] = {}
            try:
                rs_payload = await _rancher_k8s_get(
                    cluster_id,
                    f"/apis/apps/v1/namespaces/{quote(namespace, safe='')}/replicasets",
                    timeout=18,
                )
                for rs in rs_payload.get("items", []) if isinstance(rs_payload, dict) else []:
                    owners = (rs.get("metadata") or {}).get("ownerReferences") or []
                    if owners:
                        owner = owners[0]
                        replica_owner[(rs.get("metadata") or {}).get("name", "")] = (
                            owner.get("kind", "Deployment"),
                            owner.get("name", ""),
                        )
            except Exception:
                replica_owner = {}
            payload = await _rancher_k8s_get(
                cluster_id,
                f"/api/v1/namespaces/{quote(namespace, safe='')}/pods",
                timeout=25,
            )
            for raw in payload.get("items", []) if isinstance(payload, dict) else []:
                pod = _normalize_k8s_pod(raw, replica_owner)
                if pod_name and pod.get("name") == pod_name:
                    matched.append(pod)
                elif workload_name and _pod_matches_workload(pod, workload_name, workload_type):
                    matched.append(pod)
        else:
            payload = await _call_mcp_tool("list_pods", {"namespace": namespace})
            for pod in payload.get("pods", []) if isinstance(payload, dict) else []:
                if pod_name and pod.get("name") == pod_name:
                    matched.append(pod)
                elif workload_name and _pod_matches_workload(pod, workload_name, workload_type):
                    matched.append(pod)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")

    if not matched:
        verification = {
            "status": "unknown",
            "recovered": None,
            "message": "没有找到目标 Pod，无法证明修复是否成功。",
            "namespace": namespace,
            "target": f"{workload_type}/{workload_name}",
            "errors": errors,
        }
        if resource_verification:
            verification["resource_verification"] = resource_verification
        return verification

    # Workload 级变更会产生新 Pod；旧的 Failed/Succeeded Pod 不应让恢复验证一直卡住。
    active_matched = [pod for pod in matched if not _pod_completed_successfully(pod)]
    non_terminal_active = [
        pod for pod in active_matched
        if str(pod.get("phase") or "") not in {"Failed", "Unknown"}
    ]
    if workload_name and non_terminal_active:
        matched_for_health = non_terminal_active
    else:
        matched_for_health = active_matched or matched

    unresolved = []
    for pod in matched_for_health:
        category, severity, reason = _classify_pod_issue(pod, [])
        if category or not pod.get("ready"):
            unresolved.append({
                "name": pod.get("name"),
                "ready": pod.get("ready"),
                "phase": pod.get("phase"),
                "restart_count": pod.get("restart_count", 0),
                "category": category or "not_ready",
                "severity": severity or "P2",
                "reason": reason or "Pod 仍未 Ready",
            })
    recovered = not unresolved
    terminal_unresolved = [
        item for item in unresolved
        if item.get("phase") in {"Failed", "Unknown"}
        or item.get("category") in {"crashloop", "image_pull", "storage_config"}
    ]
    recovered_pods = [str(pod.get("name")) for pod in matched_for_health if pod.get("name")] if recovered else []
    verification = {
        "status": "completed",
        "recovered": recovered,
        "message": (
            "目标资源状态已收敛，且关联 Pod 已恢复 Ready。"
            if recovered and resource_verification
            else "目标 Pod 已恢复 Ready。"
            if recovered
            else "变更执行后目标 Pod 仍未恢复，系统将切换替代修复策略。"
        ),
        "namespace": namespace,
        "target": f"{workload_type}/{workload_name}",
        "pods_checked": len(matched_for_health),
        "pods_matched_total": len(matched),
        "recovered_pods": recovered_pods,
        "unresolved": unresolved[:8],
        "terminal_unresolved": terminal_unresolved[:8],
        "errors": errors,
    }
    if resource_verification:
        verification["resource_verification"] = resource_verification
    return verification


def _assess_recovery_criteria(plan: dict, verification: dict, evidence: dict) -> dict:
    """Evaluate the Skill/runbook criteria that can be proven from fresh evidence."""
    requested = [
        str(item.get("id") or item.get("name") or item.get("description") or "") if isinstance(item, dict) else str(item)
        for item in (plan.get("success_criteria") or [])
    ]
    requested = [item.strip() for item in requested if item.strip()]
    _namespace, workload_type, workload_name = _workload_identity_from_plan(plan)
    mandatory = ["pod_ready"] if workload_name else []
    if workload_name and str(workload_type or "").lower() in {
        "deployment", "statefulset", "daemonset", "replicaset",
    }:
        mandatory.append("rollout_complete")
    normalized_requested = {item.strip().lower().replace(" ", "_") for item in requested}
    requested = requested + [item for item in mandatory if item not in normalized_requested]
    pod = evidence.get("pod") or {}
    events = evidence.get("events") or []
    logs = evidence.get("logs") or {}
    workload = evidence.get("workload") or {}
    storage = evidence.get("storage") or []
    services = [item for item in (evidence.get("services") or []) if isinstance(item, dict) and not item.get("error")]
    business_probes = [item for item in (evidence.get("business_probes") or []) if isinstance(item, dict)]
    sli = evidence.get("sli") if isinstance(evidence.get("sli"), dict) else {}
    text = " ".join([
        _pod_state_text(pod),
        json.dumps(_redact_sensitive(events), ensure_ascii=False, default=str),
        json.dumps(_redact_sensitive(logs), ensure_ascii=False, default=str),
    ]).lower()
    baseline_pod = ((plan.get("evidence") or {}).get("pod") or {}) if isinstance(plan.get("evidence"), dict) else {}
    baseline_restarts = int(baseline_pod.get("restart_count") or 0)
    current_restarts = int(pod.get("restart_count") or 0)
    status = workload.get("status") or {}
    metadata = workload.get("metadata") or {}
    spec = workload.get("spec") or {}
    desired = int(spec.get("replicas") or 1)
    generation = int(metadata.get("generation") or 0)
    observed_generation = int(status.get("observedGeneration") or 0)
    rollout_complete = bool(
        workload
        and (not generation or observed_generation >= generation)
        and int(status.get("updatedReplicas") or status.get("currentReplicas") or desired) >= desired
        and int(status.get("readyReplicas") or status.get("numberReady") or 0) >= desired
    )
    known: dict[str, tuple[bool | None, str]] = {
        "pod_ready": (verification.get("recovered") is True and bool(pod and pod.get("ready")), "活动 Pod 均为 Ready"),
        "rollout_complete": (rollout_complete if workload else None, "Workload observedGeneration 与可用副本已收敛"),
        "restart_count_stable": (current_restarts <= max(1, baseline_restarts), f"restart_count={current_restarts}, baseline={baseline_restarts}"),
        "events_no_new_backoff": (not any(term in text for term in ("backoff", "back-off restarting", "failedmount", "errimagepull")), "新 Pod Events 无 BackOff/FailedMount/ImagePull"),
        "mount_events_absent": (not any(term in text for term in ("failedmount", "failedattachvolume", "mountvolume")), "新 Pod 无挂载失败事件"),
        "write_errors_absent": (not any(term in text for term in ("permission denied", "operation not permitted", "read-only file system", "mkdir:")), "新 Pod 日志无目录写入错误"),
        "oom_absent": ("oomkilled" not in text and "out of memory" not in text, "新 Pod 无 OOM 证据"),
        "probe_failures_absent": ("probe failed" not in text and "unhealthy" not in text, "新 Pod 无探针失败证据"),
        "image_pulled": (not any(term in text for term in ("imagepullbackoff", "errimagepull", "pull access denied")), "新 Pod 无镜像拉取失败"),
        "pvc_bound": (
            all(str(item.get("pvc_phase") or "") == "Bound" for item in storage if isinstance(item, dict) and item.get("pvc"))
            if any(isinstance(item, dict) and item.get("pvc") for item in storage) else None,
            "关联 PVC 均为 Bound",
        ),
        "config_ref_exists": (not any(term in text for term in ("configmap not found", "secret not found", "createcontainerconfigerror")), "新 Pod 无配置引用缺失"),
        "pod_scheduled": (bool(pod.get("node")) if pod else None, "Pod 已分配节点"),
        "endpoint_ready": (
            all(int(item.get("ready_endpoints") or 0) > 0 for item in services) if services else None,
            "匹配 Service 均存在 Ready EndpointSlice 地址",
        ),
        "dependency_reachable": (
            all(item.get("passed") is True for item in (evidence.get("dependency_checks") or []))
            if evidence.get("dependency_checks") else None,
            "已配置的关键依赖探测全部通过",
        ),
        "business_probe_ok": (
            all(item.get("passed") is True for item in business_probes) if business_probes else None,
            "已配置的业务健康探测全部通过",
        ),
        "error_rate_recovered": (
            bool(sli.get("error_rate_recovered")) if "error_rate_recovered" in sli else None,
            "错误率已回到 SLO 或变更前基线",
        ),
        "image_platform_matches_node": (
            verification.get("recovered") is True and "exec format error" not in text,
            "新 Pod 已运行且无镜像架构不匹配证据",
        ),
        "constraint_satisfied": (bool(pod.get("node")) if pod else None, "Pod 已满足调度约束并分配节点"),
        "admission_allowed": (bool(pod) and not any(term in text for term in ("exceeded quota", "limitrange", "admission webhook denied")), "新 Pod 无准入/配额拒绝"),
        "pod_sandbox_ready": (verification.get("recovered") is True and "failedcreatepodsandbox" not in text, "Pod sandbox 已建立"),
    }
    evaluations = []
    failed: list[str] = []
    not_evaluated: list[str] = []
    for criterion in requested:
        normalized = criterion.strip().lower().replace(" ", "_")
        value, proof = known.get(normalized, (None, "当前执行器未配置该业务/外部探针"))
        evaluations.append({
            "criterion": criterion,
            "passed": value,
            "proof": proof,
            "mandatory": normalized in mandatory,
        })
        if value is False:
            failed.append(criterion)
        elif value is None:
            not_evaluated.append(criterion)
    return {
        "requested": requested,
        "mandatory": mandatory,
        "evaluations": evaluations,
        "failed": failed,
        "not_evaluated": not_evaluated,
        "passed": not failed and not not_evaluated,
        "fresh_evidence": {
            "pod": pod.get("name"),
            "restart_count": current_restarts,
            "events": len(events),
            "log_streams": len(logs),
            "storage_objects": len(storage),
            "services": len(services),
            "business_probes": len(business_probes),
            "workload_generation": generation,
            "observed_generation": observed_generation,
        },
    }


async def _verify_plan_recovery(
    plan: dict,
    results: list[dict],
    cancel_event: asyncio.Event | None = None,
) -> dict:
    """Wait for rollout convergence and require evidence that the target recovered."""
    if not plan.get("changes") or any(r.get("status") == "failed" for r in results):
        return await _probe_plan_recovery(plan, results)

    timeout_seconds = max(0, int(os.getenv("OPS_VERIFY_TIMEOUT_SECONDS", "45")))
    interval_seconds = max(1, int(os.getenv("OPS_VERIFY_INTERVAL_SECONDS", "5")))
    initial_grace_seconds = max(0, int(os.getenv("OPS_VERIFY_INITIAL_GRACE_SECONDS", "15")))
    deadline = time.monotonic() + timeout_seconds
    attempts = 0
    if initial_grace_seconds:
        grace_deadline = time.monotonic() + min(initial_grace_seconds, timeout_seconds)
        while time.monotonic() < grace_deadline:
            if cancel_event and cancel_event.is_set():
                return {
                    "status": "cancelled",
                    "recovered": False,
                    "message": "运维任务已中断；系统停止恢复验证。",
                    "attempts": attempts,
                    "waited_seconds": round(time.monotonic() + initial_grace_seconds - grace_deadline, 1),
                }
            await asyncio.sleep(min(1.0, max(0.0, grace_deadline - time.monotonic())))
    last = await _probe_plan_recovery(plan, results)
    while last.get("recovered") is not True and time.monotonic() < deadline:
        if last.get("terminal_unresolved"):
            last = {
                **last,
                "status": "needs_followup",
                "message": "恢复验证发现目标 Pod 已进入明确失败状态，停止空等并进入替代策略。",
            }
            break
        if cancel_event and cancel_event.is_set():
            return {
                **last,
                "status": "cancelled",
                "recovered": False,
                "message": "运维任务已中断；系统停止后续验证与策略升级。当前变更可能已提交，请人工核对目标状态。",
                "attempts": attempts + 1,
            }
        attempts += 1
        await asyncio.sleep(min(interval_seconds, max(0.0, deadline - time.monotonic())))
        last = await _probe_plan_recovery(plan, results)
    if last.get("recovered") is True:
        try:
            verification_plan = copy.deepcopy(plan)
            recovered_pods = last.get("recovered_pods") or []
            if recovered_pods:
                verification_plan["pod_name"] = recovered_pods[0]
            fresh_evidence = await _collect_plan_deep_evidence(verification_plan)
            if fresh_evidence.get("error"):
                raise RuntimeError(str(fresh_evidence.get("error")))
            criteria = _assess_recovery_criteria(plan, last, fresh_evidence)
            last["criteria"] = criteria
            if criteria.get("failed"):
                last.update({
                    "status": "needs_followup",
                    "recovered": False,
                    "message": "Pod Ready 但 Skill 恢复判据仍未全部通过，继续进入差异化方案。",
                })
            elif criteria.get("not_evaluated"):
                last.update({
                    "status": "unknown",
                    "recovered": None,
                    "message": "Pod Ready，但仍有恢复判据缺少可验证证据；事件保持打开并继续补充诊断方案。",
                })
        except Exception as exc:
            last.update({
                "status": "unknown",
                "recovered": None,
                "message": "Pod 状态看似恢复，但无法取得执行后日志/Events/Workload 复合证据，故障不会关闭。",
                "criteria_error": f"{type(exc).__name__}: {_redact_text(str(exc))}",
            })
    return {
        **last,
        "attempts": attempts + 1,
        "initial_grace_seconds": initial_grace_seconds,
        "waited_seconds": min(timeout_seconds, initial_grace_seconds + attempts * interval_seconds),
        "proof": "Pod Ready=true 且未再匹配 CrashLoop/ImagePull/挂载/网络/Pending 等异常证据" if last.get("recovered") else "在验证窗口内未取得恢复证据",
    }


def _derive_alternative_plans(plan: dict, verification: dict, results: list[dict]) -> list[dict]:
    # 只有 recovered=true 才能关闭故障。无法读取、目标消失、验证超时等
    # recovered=null 同样必须继续给方案，不能被误报为成功。
    if verification.get("recovered") is True:
        return []
    namespace, workload_type, workload_name = _workload_identity_from_plan(plan)
    if not workload_name:
        return []
    tried = {str(c.get("type") or "") for c in plan.get("changes", [])}
    evidence = plan.get("evidence") or {}
    pod = evidence.get("pod") if isinstance(evidence, dict) else {}
    container_name = ""
    container: dict = {}
    for container in (pod or {}).get("containers", []) or []:
        if container.get("name"):
            container_name = container["name"]
            break
    unresolved_text = " ".join(
        f"{x.get('category','')} {x.get('reason','')}" for x in verification.get("unresolved", [])
    ).lower()
    summary_text = " ".join([unresolved_text, str(plan.get("summary") or ""), str(evidence.get("state_text") if isinstance(evidence, dict) else "")]).lower()
    storage_followups = _derive_followup_plans(plan, summary_text)
    if storage_followups:
        for item in storage_followups:
            item["title"] = "替代策略：" + item.get("title", "修复存储权限")
            item["previous_strategy"] = ", ".join(sorted(tried)) or "unknown"
        return storage_followups
    if "patch_workload" in tried or "patch" in tried:
        return [{
            "id": f"alternative-rollout-restart-{uuid.uuid4().hex[:8]}",
            "title": "替代策略：滚动重启加载新配置",
            "namespace": namespace,
            "target": f"{workload_type}/{workload_name}",
            "summary": "上一轮配置 patch 后 Pod 仍未恢复。系统不重复同一 patch，改为触发滚动重启加载配置，并重新读取日志与事件验证是否生效。",
            "steps": [
                {"title": "查看日志", "description": "读取新 Pod 和 previous container 日志，确认 patch 是否被应用。", "status": "pending"},
                {"title": "查看事件", "description": "检查调度、挂载、探针、镜像和权限事件。", "status": "pending"},
                {"title": "滚动重启", "description": "触发一次安全滚动重启，验证新配置是否生效。", "status": "pending"},
            ],
            "changes": [{
                "type": "restart",
                "namespace": namespace,
                "workload_type": workload_type,
                "workload_name": workload_name,
                "patch": {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "<now>"}}}}},
                "reason": "上一轮 patch 后验证失败，切换为滚动重启加载配置，而不是重复 patch。",
            }],
            "requires_confirmation": True,
            "source": "alternative_strategy_after_failed_patch",
            "previous_strategy": ", ".join(sorted(tried)) or "unknown",
            "verification_plan": _next_attempt_verification_plan(f"{workload_type}/{workload_name}"),
        }]

    if "restart" in tried:
        probe_patch = _probe_patch_from_container(container) if isinstance(container, dict) else {}
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            **_container_patch_base(container_name or "<container-name>", container if isinstance(container, dict) else {}),
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "1", "memory": "1Gi"},
                            },
                            **probe_patch,
                        }]
                    }
                }
            }
        }
        return [{
            "id": f"alternative-runtime-tuning-{uuid.uuid4().hex[:8]}",
            "title": "替代策略：调整资源与启动探针",
            "namespace": namespace,
            "target": f"{workload_type}/{workload_name}",
            "summary": "滚动重启后 Pod 仍未恢复，系统不再重复 restart，改为检查并调整资源限制/启动探针以处理 OOM、启动慢或探针过激导致的 CrashLoop。",
            "steps": [
                {"title": "复查失败证据", "description": "重新读取 Pod Logs、Events、上一次退出原因和重启计数。", "status": "pending"},
                {"title": "变更风险门禁", "description": "使用 SemanticGrayReleaseGate 判断资源/探针变更是否需要人工审批。", "status": "pending"},
                {"title": "Patch Workload 运行参数", "description": "调整 resources 与 startupProbe，避免继续重复无效重启。", "status": "pending"},
                {"title": "验证恢复", "description": "观察新 Pod Ready、restart_count 和事件是否恢复。", "status": "pending"},
            ],
            "changes": [{
                "type": "patch_workload",
                "namespace": namespace,
                "workload_type": workload_type,
                "workload_name": workload_name,
                "patch": patch,
                "reason": "上一轮 restart 后验证失败，切换为资源/探针修复策略。",
            }],
            "requires_confirmation": True,
            "source": "alternative_strategy_after_failed_restart",
            "previous_strategy": ", ".join(sorted(tried)) or "unknown",
            "risk_note": "容器名为占位时需要人工确认具体 container name；该动作会触发滚动更新。",
            "verification_plan": _next_attempt_verification_plan(f"{workload_type}/{workload_name}"),
        }]
    return [{
        "id": f"alternative-evidence-deep-dive-{uuid.uuid4().hex[:8]}",
        "title": "替代策略：证据加深后再变更",
        "namespace": namespace,
        "target": f"{workload_type}/{workload_name}",
        "summary": "上一轮执行后 Pod 仍未恢复。系统停止重复同一动作，改为加深日志、事件、配置、网络和存储证据，再生成新的变更计划。",
        "steps": [
            {"title": "查看日志", "description": "读取当前 Pod 和 previous container 日志。", "status": "pending"},
            {"title": "检查事件和配置", "description": "重新检查 Events、ConfigMap、Secret、PVC、Service/Endpoint。", "status": "pending"},
            {"title": "重新生成修复计划", "description": "基于新证据由 LLM 生成不同策略的 patch。", "status": "pending"},
        ],
        "changes": [],
        "requires_confirmation": False,
        "source": "alternative_strategy_evidence_first",
        "previous_strategy": ", ".join(sorted(tried)) or "unknown",
        "verification_plan": _next_attempt_verification_plan(f"{workload_type}/{workload_name}"),
    }]


def _ops_release_gate(plan: dict) -> dict:
    changes = plan.get("changes") or []
    if not changes:
        return {"status": "skipped", "reason": "本次计划没有 Kubernetes 变更。"}
    change = changes[0]
    selected = {
        "id": f"{change.get('workload_type','Workload')}/{change.get('workload_name','')}",
        "type": "workload",
        "title": f"{change.get('workload_type','Workload')}/{change.get('workload_name','')}",
        "risk": "high" if change.get("type") in {"patch_workload", "scale_out"} else "medium",
        "category": "application",
    }
    graph = {"nodes": [selected], "edges": []}
    service = str(plan.get("service") or change.get("workload_name") or "platform-default")
    objective = RELIABILITY_STORE.objective_for(
        service,
        str(plan.get("cluster_id") or plan.get("cluster") or "all"),
        str(plan.get("namespace") or "all"),
    )
    budget = evaluate_error_budget(objective)
    result = evaluate_release_gate(
        {
            "target": selected["id"],
            "kind": change.get("workload_type", "Workload"),
            "operator": change.get("operator") or ("resource_expand" if change.get("type") == "scale_out" else "config_replace"),
            "selected": selected,
        },
        graph,
        {
            "remaining_budget": budget["remaining_ratio"],
            "runtime_pressure": min(1.0, budget["burn_rate"] / 4.0),
            "budget_burn_rate": budget["burn_rate"],
            "release_state": "frozen" if budget["freeze_changes"] else "pending",
        },
        [],
        [],
        {},
    )
    change_class = str(plan.get("change_class") or "stability_repair")
    is_stability_repair = change_class in {"stability_repair", "incident_recovery", "rollback", "emergency_recovery"}
    result["error_budget"] = budget
    result["change_class"] = change_class
    result["allowed"] = not budget["freeze_changes"] or is_stability_repair
    if budget["freeze_changes"] and is_stability_repair:
        result["reason"] = "错误预算已耗尽；常规发布冻结，但本次为稳定性恢复动作，允许在人工确认后执行。"
        result["action"] = "stability_repair_only"
    elif budget["freeze_changes"]:
        result["verdict"] = "blocked"
        result["action"] = "freeze_change"
        result["reason"] = budget["freeze_reason"]
    _record_algorithm_decision(
        "SemanticGrayReleaseGate",
        "AI 运维执行 / /api/ops/execute",
        {"verdict": result.get("verdict"), "action": result.get("action"), "risk": result.get("risk")},
        {"target": selected["id"], "changes": len(changes)},
        "用于在执行修复前评估变更风险和人工确认必要性。",
    )
    return result


def _workload_identity_from_plan(plan: dict) -> tuple[str, str, str]:
    changes = plan.get("changes") or []
    first_change = changes[0] if changes else {}
    namespace = first_change.get("namespace") or plan.get("namespace") or "default"
    workload_type = first_change.get("workload_type") or "Deployment"
    workload_name = first_change.get("workload_name") or ""
    target = str(plan.get("target") or "")
    if not workload_name and "/" in target:
        left, right = target.split("/", 1)
        workload_type = left or workload_type
        workload_name = right or workload_name
    return namespace, workload_type, workload_name


async def _rancher_pods_for_alert_scan(cluster_filter: str, namespace: str) -> tuple[list[dict], list[dict], dict[str, str]]:
    clusters = await _rancher_clusters()
    selected = (cluster_filter or "all").strip()
    if selected not in {"", "all", "*", "所有"}:
        clusters = [c for c in clusters if selected in {c.get("id"), c.get("name")}]
    errors: dict[str, str] = {}
    all_pods: list[dict] = []
    for cluster in clusters:
        cid = cluster["id"]
        try:
            rs_payload = await _rancher_k8s_get(cid, "/apis/apps/v1/replicasets", timeout=25)
            replica_owner: dict[str, tuple[str, str]] = {}
            for rs in rs_payload.get("items", []) if isinstance(rs_payload, dict) else []:
                owners = (rs.get("metadata") or {}).get("ownerReferences") or []
                if owners:
                    owner = owners[0]
                    replica_owner[(rs.get("metadata") or {}).get("name", "")] = (
                        owner.get("kind", "Deployment"),
                        owner.get("name", ""),
                    )
            ns_path = "" if namespace in {"", "all", "*", "所有"} else f"/namespaces/{quote(namespace, safe='')}"
            pods_payload = await _rancher_k8s_get(cid, f"/api/v1{ns_path}/pods", timeout=35)
            for raw in pods_payload.get("items", []) if isinstance(pods_payload, dict) else []:
                pod = _normalize_k8s_pod(raw, replica_owner)
                pod["cluster"] = cluster["name"]
                pod["cluster_id"] = cid
                all_pods.append(pod)
        except Exception as exc:
            errors[cluster.get("name", cid)] = f"{type(exc).__name__}: {exc}"
    return all_pods, clusters, errors


def _next_attempt_verification_plan(target: str = "") -> list[str]:
    grace = max(0, int(os.getenv("OPS_VERIFY_INITIAL_GRACE_SECONDS", "15")))
    return [
        f"提交变更后先等待 {grace} 秒，让 Deployment/StatefulSet/DaemonSet 创建新 Pod 并进入真实启动阶段。",
        f"重新读取{target or '目标 Workload'}新 Pod 的 current/previous logs 与 Events，确认是否仍有同类错误。",
        "验证 Pod Ready=true、restart_count 不再增长，且不再出现 CrashLoop/ImagePull/FailedMount/Permission denied。",
        "若新 Pod 仍失败，立刻进入下一轮证据采集和差异化方案，不重复同一动作。",
    ]


def _manual_required_steps(plan: dict, verification: dict | None = None, reason: str = "") -> list[str]:
    namespace, workload_type, workload_name = _workload_identity_from_plan(plan)
    target = f"{workload_type}/{workload_name}" if workload_name else str(plan.get("target") or "目标对象")
    verification = verification or {}
    unresolved_text = "；".join(
        str(item.get("reason") or item.get("message") or item)
        for item in (verification.get("terminal_unresolved") or verification.get("unresolved") or [])[:3]
    )
    reason_text = reason or verification.get("blocked_reason") or unresolved_text or "平台没有拿到足以支持继续自动变更的强证据。"
    return [
        f"先不要重复执行同一动作；当前未恢复原因：{reason_text}",
        f"在 {namespace} 命名空间核对 {target} 的最新 Pod、previous logs、Events、PVC/PV、ConfigMap/Secret、Service/Endpoint 与 Node 调度状态。",
        "如果日志仍显示 Permission denied、FailedMount、ImagePull、Exec format error、探针失败或 OOM，请把对应证据重新交给 SRE 对话，系统会生成下一轮差异化方案。",
        "如果证据指向存储后端目录、网络插件、云平台版本、镜像架构或外部系统权限，需管理员先按页面文字步骤处理底层资源，再回到平台点击重新验证。",
        "处理完成后重新运行同一目标的 AI 运维，让平台确认 Pod Ready、restart_count 稳定且同类事件不再出现。",
    ]


def _manual_required_followup_plan(plan: dict, verification: dict, reason: str = "") -> dict:
    namespace, workload_type, workload_name = _workload_identity_from_plan(plan)
    target = f"{workload_type}/{workload_name}" if workload_name else str(plan.get("target") or "目标对象")
    steps = _manual_required_steps(plan, verification, reason)
    return {
        "id": f"manual-required-{uuid.uuid4().hex[:8]}",
        "title": "人工接管：补齐平台无法直接取得的底层证据",
        "namespace": namespace,
        "target": target,
        "summary": "自动变更没有取得恢复证据；为了避免重复无效动作，系统转为给出管理员可执行的文字方案。",
        "steps": [{"title": f"人工步骤 {index + 1}", "description": text, "status": "manual"} for index, text in enumerate(steps)],
        "operator_steps": steps,
        "changes": [],
        "requires_confirmation": False,
        "source": "manual_required_after_failed_recovery",
        "verification_plan": _next_attempt_verification_plan(target),
    }


def _ops_terminal_next_steps(
    plan: dict,
    verification: dict | None,
    alternative_plans: list[dict] | None = None,
    operator_steps: list[str] | None = None,
    failed_results: list[dict] | None = None,
) -> list[str]:
    verification = verification or {}
    alternative_plans = alternative_plans or []
    operator_steps = operator_steps or []
    failed_results = failed_results or []
    namespace, workload_type, workload_name = _workload_identity_from_plan(plan)
    target = f"{workload_type}/{workload_name}" if workload_name else str(plan.get("target") or "目标对象")
    if verification.get("recovered") is True:
        return [
            f"继续观察 {target} 5-10 分钟，确认 Ready、重启次数和关键业务指标稳定。",
            "把本次根因、变更内容、恢复证据写入运维成效和 Skill 沉淀，后续同类问题可自动复用。",
            "如果这是发布后故障，回到发布治理页补充灰度门禁规则，避免同类变更再次放大。",
        ]
    if failed_results:
        guidance: list[str] = []
        for item in failed_results[:2]:
            raw = item.get("result") if isinstance(item, dict) else {}
            if isinstance(raw, dict):
                for step in raw.get("operator_steps") or []:
                    text = str(step or "").strip()
                    if text and text not in guidance:
                        guidance.append(text)
        return (guidance or [
            "变更 API 返回失败；先查看变更回执里的 403/409/422/500 细节，确认是权限、资源版本冲突、参数非法还是执行器异常。",
            "修正 Rancher Token、ServiceAccount RBAC、目标对象 resourceVersion 或变更参数后，重新生成预演再执行。",
        ])[:6]
    actionable = [item for item in alternative_plans if (item.get("changes") or item.get("steps") or item.get("operator_steps"))]
    changeable = [item for item in actionable if item.get("changes")]
    if changeable:
        first = changeable[0]
        return [
            f"本轮没有恢复；系统已生成下一轮差异化策略：{first.get('title') or '替代修复'}。",
            "先核对下方变更目标、补丁内容、风险和回滚方式；确认后点击“确认并执行”。",
            "下一轮执行后会重新读取新 Pod 的 logs/events/workload/pvc/pv 证据，成功则闭环，失败则继续换策略。",
        ]
    if actionable:
        first = actionable[0]
        return [
            f"本轮没有恢复；下一步先执行只读加深诊断：{first.get('title') or '证据加深'}。",
            "平台会重新读取失败后新 Pod 的 current/previous logs、Events、配置、存储和网络证据。",
            "诊断完成后若能形成安全变更，会再次显示人工确认按钮；若必须管理员处理，会给出明确文字步骤。",
        ]
    if operator_steps:
        return operator_steps[:6]
    return _manual_required_steps(plan, verification)


def _storage_permission_text(plan: dict, summary_text: str = "") -> str:
    deep = plan.get("_runtime_evidence") or {}
    evidence = plan.get("evidence") or {}
    return " ".join([
        str(summary_text or ""),
        str(plan.get("summary") or ""),
        str(evidence.get("state_text") or ""),
        json.dumps(_redact_sensitive(deep.get("logs") or {}), ensure_ascii=False, default=str),
        json.dumps(_redact_sensitive(deep.get("events") or evidence.get("events") or []), ensure_ascii=False, default=str),
        json.dumps(_redact_sensitive(deep.get("storage") or []), ensure_ascii=False, default=str),
        json.dumps(_redact_sensitive(deep.get("workload") or {}), ensure_ascii=False, default=str),
    ]).lower()


def _storage_permission_detected(plan: dict, summary_text: str = "") -> bool:
    text = _storage_permission_text(plan, summary_text)
    storage_terms = ["存储", "存储卷", "卷", "volume", "mount", "failedmount", "mountvolume", "pvc", "persistentvolumeclaim"]
    permission_terms = ["目录权限", "权限不足", "permission denied", "operation not permitted", "read-only file system", "can't create directory", "cannot create directory", "mkdir:"]
    return any(term in text for term in storage_terms) and any(term in text for term in permission_terms)


def _storage_admin_boundary_detected(plan: dict) -> bool:
    text = _storage_permission_text(plan)
    boundary_terms = [
        "forbidden",
        "rbac",
        "podsecurity",
        "restricted",
        "violates podsecurity",
        "runasuser=0",
        "namespace not labeled",
        "permission denied: namespaces",
        "cannot patch namespace",
    ]
    return any(term in text for term in boundary_terms)


def _storage_admin_steps(plan: dict, reason: str = "") -> list[str]:
    namespace, workload_type, workload_name = _workload_identity_from_plan(plan)
    storage = (plan.get("_runtime_evidence") or {}).get("storage") or []
    pvc_names = [str(item.get("pvc")) for item in storage if isinstance(item, dict) and item.get("pvc")]
    pvc_label = "、".join(sorted(set(pvc_names))[:4]) or "目标 PVC"
    target = f"{workload_type}/{workload_name}" if workload_name else str(plan.get("target") or namespace)
    return [
        f"确认目标：namespace={namespace}，workload={target}，PVC={pvc_label}；保留失败路径、容器 UID/GID、挂载模式和最近 YAML 的证据。",
        "不要重复上一轮动作。先判定是错误的 initContainer/chmod、readOnly 挂载、应用路径、PodSecurity，还是 CSI/NFS/Ceph 后端目录权限。",
        "若证据指向 Kubernetes 模板，回到平台让动态 Skill 生成并审批精确 Patch；若指向存储后端，由存储管理员按实际导出路径和业务 UID/GID 修复，禁止猜测路径或属组。",
        "管理员执行后回到平台重新点击验证/重跑本计划，确认新 Pod 日志不再出现 Permission denied，Ready=true 且重启次数不再增长。",
        reason or "如果当前 Rancher Token/ServiceAccount 无法修改 namespace 安全标签或底层存储目录，这是权限边界，不是 AI 诊断卡死。",
    ]


def _derive_followup_plans(plan: dict, summary_text: str) -> list[dict]:
    if not _storage_permission_detected(plan, summary_text):
        return []

    namespace, workload_type, workload_name = _workload_identity_from_plan(plan)
    target = f"{workload_type}/{workload_name}" if workload_name else str(plan.get("target") or namespace)
    operator_steps = _storage_admin_steps(
        plan,
        "当前没有得到新的、可授权具体变更的证据；请补齐后重新触发动态 Skill 规划。",
    )
    return [{
        "id": f"followup-storage-evidence-{uuid.uuid4().hex[:8]}",
        "title": "重新取证并动态匹配权限恢复 Skill",
        "namespace": namespace,
        "target": target,
        "summary": "上一轮没有恢复。系统不会自动切换到固定 fsGroup/initContainer 链，而是要求基于失败后新 Pod 证据生成差异化方案。",
        "steps": [{"title": "补齐差异证据", "description": item, "status": "manual"} for item in operator_steps],
        "changes": [],
        "requires_confirmation": False,
        "source": "dynamic_skill_evidence_required",
        "operator_steps": operator_steps,
        "verification_plan": _next_attempt_verification_plan(target),
    }]


def _normalize_planner_change(raw: dict, plan: dict) -> tuple[dict | None, str]:
    if not isinstance(raw, dict):
        return None, "change must be an object"
    change = copy.deepcopy(raw)
    action = str(change.get("type") or "").strip()
    if action == "patch_workload" and _patch_touches_volumes(change.get("patch") or {}):
        action = "patch_workload_volume"
        change["type"] = action
    if action in {"patch_workload", "patch"} and _patch_touches_init_containers(change.get("patch") or {}):
        action = "patch_workload_runtime_security"
        change["type"] = action
    if action not in ACTION_CATALOG:
        return None, f"action {action or '<empty>'} is not registered"
    namespace, workload_type, workload_name = _workload_identity_from_plan(plan)
    change.setdefault("namespace", namespace)
    change.setdefault("workload_type", workload_type)
    change.setdefault("workload_name", workload_name)
    change["risk"] = ACTION_CATALOG[action]["risk"]
    change["auto_allowed"] = ACTION_CATALOG[action]["auto_allowed"]
    change["rollback"] = ACTION_CATALOG[action]["rollback"]
    change["human_approved"] = bool(
        plan.get("high_risk_confirmed")
        or plan.get("operator_force_execute")
        or change.get("human_approved")
        or change.get("operator_confirmed")
    )
    if action in {"run_shell", "exec_pod", "exec_node"}:
        allowed_by_skill = {
            str(item)
            for skill in plan.get("operator_skills") or []
            for item in (skill.get("allowed_actions") or [])
        }
        if action not in allowed_by_skill:
            return None, f"{action} requires a matched execution-ready Skill that explicitly allows it"
        command = str(change.get("command") or "").strip()
        if not command or len(command) > 20000 or "\x00" in command:
            return None, f"{action} requires a valid command"
        change["command"] = command
        change["timeout_seconds"] = max(1, min(int(change.get("timeout_seconds") or 120), 900))
        if action == "exec_pod":
            change.setdefault("pod_name", _target_pod_from_plan(plan))
            if not change.get("pod_name"):
                return None, "exec_pod requires pod_name"
        if action == "exec_node" and not change.get("node_name"):
            return None, "exec_node requires node_name"
        return change, ""
    if action == "apply_manifest":
        manifest = change.get("manifest") or {}
        metadata = manifest.get("metadata") or {} if isinstance(manifest, dict) else {}
        if not isinstance(manifest, dict) or not manifest.get("apiVersion") or not manifest.get("kind") or not metadata.get("name"):
            return None, "apply_manifest requires apiVersion, kind and metadata.name"
        change.setdefault("namespace", metadata.get("namespace") or namespace)
        return change, ""
    if action in {"patch_resource", "delete_resource"}:
        if not (change.get("api_version") or change.get("apiVersion")) or not change.get("kind") or not change.get("name"):
            return None, f"{action} requires api_version, kind and name"
        if action == "patch_resource" and not isinstance(change.get("patch"), dict):
            return None, "patch_resource requires patch object"
        return change, ""
    if _is_infrastructure_action(action):
        target = str(plan.get("target") or "")
        target_type, target_id = ("", "")
        if "/" in target:
            target_type, target_id = target.split("/", 1)
        change.setdefault("resource_type", plan.get("resource_type") or target_type or "external")
        change.setdefault("resource_id", plan.get("resource_id") or target_id)
        change.setdefault("resource_name", plan.get("resource_name") or change.get("resource_id"))
        change.setdefault("requires_external_executor", True)
        if not change.get("resource_id"):
            return None, f"{action} requires resource_id"
        valid, reason = validate_change({**change, "human_approved": ACTION_CATALOG[action]["risk"] != "high"})
        if not valid and ACTION_CATALOG[action]["risk"] != "high":
            return None, reason
        return change, ""
    if action in {"patch_workload", "patch_workload_volume", "patch_workload_runtime_security", "restart", "scale_out"} and not change.get("workload_name"):
        return None, f"{action} requires workload_name"
    if action in {"patch_workload", "patch_workload_volume", "patch_workload_runtime_security"}:
        valid, reason = _validate_workload_patch(
            change.get("patch") or {},
            allow_volume_patch=action == "patch_workload_volume",
            # A bounded runtime-security proposal must be renderable before an
            # operator can review it. Execution still requires high-risk
            # confirmation through validate_change/_execute_change.
            allow_init_containers=action == "patch_workload_runtime_security" or bool(change.get("human_approved")),
        )
        if not valid:
            return None, f"patch rejected: {reason}"
    if action == "restart":
        change["patch"] = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": "<now>"}}}}}
    if action == "scale_out":
        replicas = max(1, min(int(change.get("replicas") or 2), int(os.getenv("MAX_PATCH_REPLICAS", "20"))))
        change["replicas"] = replicas
        change["patch"] = {"spec": {"replicas": replicas}}
    if action in {"recreate_pod", "evict_pod"}:
        change.setdefault("pod_name", _target_pod_from_plan(plan))
        if not change.get("pod_name"):
            return None, f"{action} requires pod_name"
    if action == "patch_hpa" and not change.get("hpa_name"):
        return None, "patch_hpa requires hpa_name"
    if action == "patch_service_account":
        patch = change.get("patch") or {"imagePullSecrets": [{"name": change.get("image_pull_secret", "")}]}
        valid, reason = _validate_service_account_patch(patch)
        if not valid:
            return None, reason
        change["patch"] = patch
        change.setdefault("service_account", "default")
    if action == "create_configmap":
        manifest = change.get("manifest") or {}
        valid, reason = _validate_configmap_manifest(manifest, change.get("namespace") or namespace)
        if not valid:
            return None, reason
        change["configmap_name"] = change.get("configmap_name") or ((manifest.get("metadata") or {}).get("name"))
    if action == "expand_pvc" and (not change.get("pvc_name") or not change.get("storage")):
        return None, "expand_pvc requires pvc_name and storage"
    if action == "create_pvc" and (not change.get("manifest") or not change.get("pvc_name")):
        manifest = change.get("manifest") or {}
        metadata = manifest.get("metadata") or {}
        change["pvc_name"] = change.get("pvc_name") or metadata.get("name")
        if not change.get("pvc_name"):
            return None, "create_pvc requires pvc_name and manifest"
    if action == "create_pv" and not change.get("manifest"):
        return None, "create_pv requires manifest"
    if action == "cordon_node" and not change.get("node_name"):
        return None, "cordon_node requires node_name"
    valid, reason = validate_change({**change, "human_approved": ACTION_CATALOG[action]["risk"] != "high"})
    if not valid and ACTION_CATALOG[action]["risk"] != "high":
        return None, reason
    return change, ""


def _autonomous_plan_allowed(plan: dict) -> bool:
    changes = plan.get("changes") or []
    return bool(changes) and all(
        ACTION_CATALOG.get(str(change.get("type") or ""), {}).get("auto_allowed") is True
        and ACTION_CATALOG.get(str(change.get("type") or ""), {}).get("risk") in {"low", "medium"}
        for change in changes
    )


async def _evidence_based_replan(
    plan: dict,
    steps: list[dict],
    attempted_actions: set[str] | None = None,
    *,
    include_llm: bool = True,
) -> list[dict]:
    deep = plan.get("_runtime_evidence") or {}
    failed_context = plan.get("_last_failure") or {}
    blocked_change_fingerprints = {
        str(value) for value in (plan.get("_attempted_change_fingerprints") or []) if value
    }
    blocked_change_fingerprints.update(
        _change_item_fingerprint(change)
        for change in (plan.get("changes") or [])
        if isinstance(change, dict)
    )
    alert = {
        "alert_name": plan.get("strategy_class") or plan.get("runbook_id") or "runtime_incident",
        "summary": plan.get("summary", ""),
        "namespace": plan.get("namespace", "default"),
        "workload_type": _workload_identity_from_plan(plan)[1],
        "workload_name": _workload_identity_from_plan(plan)[2],
        "pod": _target_pod_from_plan(plan),
    }
    diagnosis = {"root_cause": plan.get("summary", ""), "signals": deep.get("events", [])}
    engine_plan = build_remediation_plan(alert, diagnosis, {
        **deep,
        "pod": deep.get("pod") or ((plan.get("evidence") or {}).get("pod") or {}),
        "pods": [deep.get("pod")] if deep.get("pod") else [],
        "events": {"events": deep.get("events", [])},
    })
    primary = (engine_plan.get("hypotheses") or [{}])[0]
    plan["_runtime_replan"] = {
        "runbook_id": engine_plan.get("runbook_id"),
        "confidence": float(primary.get("confidence") or 0.0),
        "reason": engine_plan.get("reason"),
        "evidence_gap": engine_plan.get("evidence_gap"),
        "success_criteria": engine_plan.get("success_criteria") or [],
    }
    candidates = list(engine_plan.get("changes") or [])
    planner_meta: dict = {"source": "EvidenceRunbookEngine", "hypotheses": engine_plan.get("hypotheses", [])}

    try:
        if not include_llm:
            raise RuntimeError("deterministic preflight")
        def call_planner() -> dict:
            from agents.llm_client import get_llm
            planner_max_tokens = max(512, min(int(os.getenv("OPS_LLM_PLANNER_MAX_TOKENS", "4096")), 8192))
            llm = get_llm(
                temperature=0.05,
                max_tokens=planner_max_tokens,
                profile_id=plan.get("model_profile_id") or None,
            )
            matched_skill_context = OPS_SKILL_REGISTRY.agent_context(_skill_signal_payload(
                question=str(plan.get("summary") or ""),
                alert=alert,
                diagnosis=diagnosis,
                evidence=deep,
                plan=plan,
            ), top_k=3)
            prompt = (
                "你是 Kubernetes 故障修复规划器。根据真实执行证据和动态匹配 Skill，从给定动作目录中选择至多两个结构化动作。"
                "证据不足时 changes=[]。高风险动作可以提出但必须标 risk=high，所有动作都必须人工审批。"
                "上一轮方案已经执行且恢复验证失败；不得只改写理由后重复相同动作、目标和参数。只有参数发生实质变化且新证据明确支持时，"
                "才允许继续使用同一动作类型，否则必须换根因假设、换动作，或明确进入管理员人工处理。"
                "不要为了显得积极而重启；必须解释证据如何支持根因。很多故障普通日志没有内容，必须优先使用 Events、"
                "container waiting/terminated reason、lastState、Workload 模板、PVC/PV、镜像平台、节点标签和最近发布证据。只返回 JSON："
                "如果 logs/current 或 logs/previous 不存在、Pod 已删除或 container 尚未产生日志，且 Events/YAML 没有证明 PVC、镜像、"
                "ConfigMap、配额或调度约束等模板级阻断，可以提出 recreate_pod 作为诊断性重建，然后重新采集日志；"
                "如果已命中模板级阻断，禁止用重启掩盖根因。"
                "对于 Permission denied/目录不可写，不得预设 fsGroup、initContainer 或重启就是答案；必须比较失败路径、镜像用户、"
                "Pod/容器 securityContext、挂载、PVC/PV、最近配置与匹配 Skill，再给出实际 YAML Patch、重新发布方式或受审批命令。"
                "只有匹配 Skill 允许时才可提出 run_shell/exec_pod/exec_node，并必须给 command、明确目标、timeout_seconds、reason、rollback。"
                "{root_cause,confidence,selected_runbook,reason,changes:[{type,namespace,workload_type,workload_name,pod_name,"
                "container_name,hpa_name,pvc_name,storage,node_name,service_account,configmap_name,api_version,kind,name,replicas,manifest,patch,command,timeout_seconds,reason,rollback}]}。\n"
                f"动作目录={json.dumps(action_catalog_payload(), ensure_ascii=False)}\n"
                f"动态匹配 Skills={json.dumps(_redact_sensitive(matched_skill_context), ensure_ascii=False)[:12000]}\n"
                f"已尝试动作={sorted(attempted_actions or set())}\n"
                f"已失败策略指纹={sorted(blocked_change_fingerprints)}\n"
                f"同一故障链历史={json.dumps(_redact_sensitive(plan.get('_prior_attempts') or []), ensure_ascii=False)[:6000]}\n"
                f"上一轮失败与验证结果={json.dumps(_redact_sensitive(failed_context), ensure_ascii=False)[:7000]}\n"
                f"目标与原计划={json.dumps(_redact_sensitive({k: v for k, v in plan.items() if not k.startswith('_')}), ensure_ascii=False)[:7000]}\n"
                f"真实证据={json.dumps(_redact_sensitive(deep), ensure_ascii=False)[:15000]}"
            )
            response = llm.invoke(prompt)
            return _extract_json_object(getattr(response, "content", str(response)))

        planner_timeout = max(5, min(int(os.getenv("OPS_LLM_PLANNER_TIMEOUT_SECONDS", "60")), 180))
        llm_plan = await asyncio.wait_for(asyncio.to_thread(call_planner), timeout=planner_timeout)
        planner_meta = {"source": "llm+EvidenceRunbookEngine", **_redact_sensitive(llm_plan)}
        if llm_plan.get("changes"):
            # AI+Skill 针对本轮证据的方案取代确定性模板；确定性引擎只在模型
            # 无可用方案时降级，不得再次覆盖模型选择。
            candidates = list(llm_plan.get("changes") or [])
    except Exception as exc:
        if include_llm:
            planner_meta["llm_error"] = f"{type(exc).__name__}: {_redact_text(str(exc))}"

    normalized: list[dict] = []
    rejected: list[str] = []
    seen = set()
    constrained_actions = {
        "storage_mount": {"create_pvc", "create_pv", "expand_pvc", "patch_workload_volume"},
        "storage_permission": {"patch_workload", "patch_workload_runtime_security", "patch_resource", "apply_manifest", "exec_pod", "exec_node"},
        "config_missing": {"create_configmap"},
        "image_auth": {"patch_workload", "patch_service_account", "rollback_workload"},
        "image_architecture": {"rollback_workload", "patch_workload"},
        "oom": {"patch_workload"},
        "probe": {"patch_workload"},
        "node_pressure": {"cordon_node", "evict_pod"},
    }.get(str(engine_plan.get("runbook_id") or "")) if float(primary.get("confidence") or 0.0) >= 0.72 else None
    for raw in candidates:
        if constrained_actions and str((raw or {}).get("type") or "") not in constrained_actions:
            rejected.append(
                f"{(raw or {}).get('type') or 'unknown'} conflicts with proven {engine_plan.get('runbook_id')} evidence"
            )
            continue
        change, reason = _normalize_planner_change(raw, plan)
        if not change:
            rejected.append(reason)
            continue
        fingerprint = _change_item_fingerprint(change)
        if fingerprint in blocked_change_fingerprints:
            rejected.append(f"{change.get('type') or 'unknown'} repeats a previously failed action/target/parameter set")
            continue
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        normalized.append(change)
    plan["_runtime_replan"]["planning"] = {
        "source": planner_meta.get("source"),
        "llm_error": planner_meta.get("llm_error", ""),
        "proposed_changes": len(planner_meta.get("changes") or []),
        "accepted_changes": len(normalized),
        "rejected_candidates": rejected[:8],
    }
    if not normalized:
        return []
    namespace, workload_type, workload_name = _workload_identity_from_plan(plan)
    replanned = {
        "id": f"evidence-replan-{uuid.uuid4().hex[:8]}",
        "title": f"证据重规划：{engine_plan.get('runbook_id', 'expert-runbook')}",
        "namespace": namespace,
        "target": f"{workload_type}/{workload_name}" if workload_name else plan.get("target", namespace),
        "pod_name": _target_pod_from_plan(plan),
        "cluster": plan.get("cluster"),
        "cluster_id": plan.get("cluster_id"),
        "source": plan.get("source"),
        "evidence": plan.get("evidence") or {},
        "summary": planner_meta.get("reason") or engine_plan.get("reason") or "根据新证据生成差异化修复策略。",
        "steps": engine_plan.get("steps") or [],
        "changes": normalized[:2],
        "requires_confirmation": True,
        "requires_high_risk_confirmation": any(change.get("risk") == "high" for change in normalized[:2]),
        "strategy_source": "evidence_replan",
        "planning": planner_meta,
        "root_cause_hypotheses": engine_plan.get("hypotheses", []),
        "success_criteria": engine_plan.get("success_criteria", []),
        "verification_plan": _next_attempt_verification_plan(f"{workload_type}/{workload_name}" if workload_name else plan.get("target", "")),
        "rejected_candidates": rejected,
    }
    replanned = _attach_operator_skills_to_plan(
        replanned,
        _skill_signal_payload(
            question=str(replanned.get("summary") or plan.get("summary") or ""),
            alert=alert,
            diagnosis=diagnosis,
            evidence=deep,
            plan=replanned,
        ),
    )
    return [replanned] if replanned.get("changes") else []


def _preflight_evidence_conflict(plan: dict) -> bool:
    """Return True when live evidence invalidates the already approved action."""
    meta = plan.get("_runtime_replan") or {}
    if float(meta.get("confidence") or 0.0) < 0.72:
        return False
    allowed = {
        "storage_mount": {"create_pvc", "create_pv", "expand_pvc", "patch_workload_volume"},
        "storage_permission": {"patch_workload", "patch_workload_runtime_security", "patch_resource", "apply_manifest", "exec_pod", "exec_node"},
        "config_missing": {"create_configmap"},
        "image_auth": {"patch_workload", "patch_service_account", "rollback_workload"},
        "image_architecture": {"rollback_workload", "patch_workload"},
        "oom": {"patch_workload"},
        "probe": {"patch_workload"},
        "node_pressure": {"cordon_node", "evict_pod"},
    }.get(str(meta.get("runbook_id") or ""))
    if not allowed:
        return False
    original_actions = {str(change.get("type") or "") for change in plan.get("changes") or []}
    return bool(original_actions and not original_actions.issubset(allowed))


async def _llm_ops_summary(plan: dict, steps: list[dict], results: list[dict]) -> dict:
    failed = [r for r in results if r.get("status") == "failed"]
    payload = {
        "plan": {
            "title": plan.get("title"),
            "target": plan.get("target"),
            "summary": plan.get("summary"),
            "changes": plan.get("changes", []),
        },
        "steps": [
            {
                "title": s.get("title"),
                "status": s.get("status"),
                "logs": s.get("logs", [])[-10:],
                "artifacts": s.get("artifacts", {}),
            }
            for s in steps
        ],
        "change_results": results,
    }

    def _fallback() -> str:
        if failed:
            reason = "; ".join(str((r.get("result") or {}).get("error", "unknown")) for r in failed)
            return f"AI 降级总结：运维流程已完成诊断步骤，但 Kubernetes 变更失败。失败原因：{reason}。建议先核对 RBAC、目标 workload 名称、namespace 白名单和 MCP 服务可达性。"
        if results:
            return "AI 降级总结：诊断步骤已执行，Kubernetes 变更返回成功。建议继续观察 Pod Ready、重启次数和事件是否恢复。"
        return "AI 降级总结：已完成诊断步骤，本次计划没有需要执行的 Kubernetes 变更。"

    try:
        def _call_llm() -> str:
            from agents.llm_client import get_llm
            llm = get_llm(temperature=0.05, max_tokens=900, profile_id=plan.get("model_profile_id") or None)
            prompt = (
                "你是企业级 AIOps 运维执行官。基于以下执行证据，用中文输出简洁结论：\n"
                "1. 判断故障是否已经定位；2. Kubernetes 变更是否成功；3. 下一步建议；"
                "4. 如果失败，明确最可能的失败原因。不要输出 JSON。\n\n"
                f"{json.dumps(_redact_sensitive(payload), ensure_ascii=False)[:9000]}"
            )
            result = llm.invoke(prompt)
            return getattr(result, "content", str(result))

        content = await asyncio.wait_for(asyncio.to_thread(_call_llm), timeout=18)
        return {
            "source": "llm",
            "content": content,
            "followup_plans": _derive_followup_plans(plan, content),
        }
    except Exception as e:
        content = _fallback()
        return {
            "source": "fallback",
            "content": content,
            "followup_plans": _derive_followup_plans(plan, content),
            "error": f"{type(e).__name__}: {e}",
        }


_CHANGE_FINGERPRINT_IGNORED_KEYS = {
    "reason", "summary", "description", "title", "risk", "auto_allowed", "rollback",
    "human_approved", "operator_confirmed", "requires_confirmation",
    "requires_high_risk_confirmation", "selection_source", "skill_supported", "skill_id",
    "approval_receipt", "approval_id", "approved_at", "approved_by", "operator_override_reason",
}


def _diagnostic_probe_fingerprint(step: dict) -> str:
    probe_id = str(step.get("id") or step.get("probe") or "").strip()
    if probe_id:
        return probe_id
    text = f"{step.get('title') or step.get('name') or ''} {step.get('description') or step.get('detail') or ''}".lower()
    mappings = (
        (("previous", "上一次", "退出日志"), "previous_logs"),
        (("log", "日志"), "current_logs"),
        (("event", "事件"), "events"),
        (("pvc", "pv", "存储", "挂载"), "storage_chain"),
        (("service", "endpoint", "网络", "dns"), "service_endpoints"),
        (("node", "节点", "调度"), "node_conditions"),
        (("workload", "deployment", "statefulset", "配置", "模板"), "workload_spec"),
    )
    return next((probe for terms, probe in mappings if any(term in text for term in terms)), "workload_spec")


def _canonical_change_value(value, key: str = ""):
    """保留真正影响执行结果的字段，避免模型改写说明文字后被当成新策略。"""
    if isinstance(value, dict):
        return {
            str(item_key): _canonical_change_value(item_value, str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
            if str(item_key) not in _CHANGE_FINGERPRINT_IGNORED_KEYS
        }
    if isinstance(value, list):
        return [_canonical_change_value(item, key) for item in value]
    if isinstance(value, str) and key in {"kubectl.kubernetes.io/restartedAt", "restartedAt", "timestamp"}:
        return "<timestamp>"
    return value


def _change_item_fingerprint(change: dict) -> str:
    canonical = _canonical_change_value(change if isinstance(change, dict) else {"type": str(change)})
    normalized = json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _change_fingerprint(plan: dict) -> str:
    changes = plan.get("changes") or []
    if changes:
        material = {
            "kind": "mutation",
            "changes": sorted(
                (_canonical_change_value(change) for change in changes),
                key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
            ),
        }
    else:
        # 诊断策略按真实探针语义去重；随机 id、标题和模型改写说明不能把
        # 同一轮 Events/Logs/PVC 采集伪装成新策略。
        probe_ids = sorted({
            _diagnostic_probe_fingerprint(step)
            for step in plan.get("steps", [])
            if isinstance(step, dict)
        })
        material = {
            "kind": "diagnostic",
            "target": plan.get("target"),
            "namespace": plan.get("namespace"),
            "probes": probe_ids,
            "manual_boundary": bool(plan.get("operator_steps")),
        }
    normalized = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _plan_action_types(plan: dict) -> set[str]:
    return {str(change.get("type") or "").strip() for change in plan.get("changes", []) if isinstance(change, dict) and change.get("type")}


def _history_action_types(history: list[dict]) -> set[str]:
    actions: set[str] = set()
    for item in history:
        result = item.get("result") if isinstance(item, dict) else {}
        for change in (result or {}).get("changes", []) or []:
            if isinstance(change, dict) and change.get("type"):
                actions.add(str(change["type"]))
        plan_actions = item.get("actions") if isinstance(item, dict) else None
        if isinstance(plan_actions, list):
            actions.update(str(action) for action in plan_actions if action)
    return actions


def _history_change_fingerprints(history: list[dict]) -> set[str]:
    fingerprints: set[str] = set()
    for item in history:
        if not isinstance(item, dict):
            continue
        fingerprints.update(str(value) for value in (item.get("change_fingerprints") or []) if value)
    return fingerprints


def _ops_attempt_summary(item: dict) -> dict:
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    errors: list[str] = []
    for change_result in result.get("results") or []:
        if not isinstance(change_result, dict) or change_result.get("status") not in {"failed", "blocked"}:
            continue
        raw = change_result.get("result") if isinstance(change_result.get("result"), dict) else {}
        value = str(raw.get("error") or change_result.get("message") or "").strip()
        if value and value not in errors:
            errors.append(_clip_text(_redact_text(value), 360))
    return {
        "attempt": int(item.get("attempt") or 0),
        "strategy": str(item.get("strategy") or "ops-plan")[:180],
        "fingerprint": str(item.get("fingerprint") or ""),
        "actions": [str(value) for value in (item.get("actions") or []) if value][:12],
        "change_fingerprints": [str(value) for value in (item.get("change_fingerprints") or []) if value][:12],
        "status": str(result.get("status") or "unknown"),
        "recovered": verification.get("recovered"),
        "outcome": _clip_text(
            str(verification.get("message") or result.get("message") or "本轮没有形成恢复证据。"),
            600,
        ),
        "errors": errors[:4],
    }


def _build_ops_continuation_context(
    job_id: str,
    plan: dict,
    result: dict,
    attempted: set[str],
    history: list[dict],
) -> dict:
    existing = plan.get("continuation_context") if isinstance(plan.get("continuation_context"), dict) else {}
    prior_attempts = [item for item in (plan.get("_prior_attempts") or existing.get("attempts") or []) if isinstance(item, dict)]
    prior_attempt_count = max(int(plan.get("_prior_attempt_count") or 0), int(existing.get("attempt_count") or 0), len(prior_attempts))
    prior_attempt_offset = max(0, prior_attempt_count - len(prior_attempts))
    summaries = [
        {
            "attempt": int(item.get("attempt") or prior_attempt_offset + index + 1),
            "strategy": str(item.get("strategy") or "ops-plan")[:180],
            "fingerprint": str(item.get("fingerprint") or ""),
            "actions": [str(value) for value in (item.get("actions") or []) if value][:12],
            "change_fingerprints": [str(value) for value in (item.get("change_fingerprints") or []) if value][:12],
            "status": str(item.get("status") or "unknown"),
            "recovered": item.get("recovered"),
            "outcome": _clip_text(str(item.get("outcome") or ""), 600),
            "errors": [_clip_text(_redact_text(str(value)), 360) for value in (item.get("errors") or []) if value][:4],
        }
        for index, item in enumerate(prior_attempts)
    ]
    current_summaries = [_ops_attempt_summary(item) for item in history if isinstance(item, dict)]
    for index, item in enumerate(current_summaries, start=1):
        item["attempt"] = prior_attempt_count + index
    summaries.extend(current_summaries)
    deduplicated: list[dict] = []
    seen: set[str] = set()
    for item in summaries:
        key = item.get("fingerprint") or json.dumps(
            {"strategy": item.get("strategy"), "actions": item.get("actions"), "outcome": item.get("outcome")},
            ensure_ascii=False,
            sort_keys=True,
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(item)
    change_fingerprints = {
        str(value) for value in (plan.get("_attempted_change_fingerprints") or existing.get("attempted_change_fingerprints") or []) if value
    }
    change_fingerprints.update(_history_change_fingerprints(history))
    actions = {
        str(value) for value in (plan.get("_attempted_actions") or existing.get("attempted_actions") or []) if value
    }
    actions.update(_history_action_types(history))
    last_attempt = deduplicated[-1] if deduplicated else {
        "strategy": plan.get("title") or plan.get("source") or "ops-plan",
        "status": result.get("status") or "unknown",
        "recovered": (result.get("verification") or {}).get("recovered"),
        "outcome": (result.get("verification") or {}).get("message") or result.get("message") or "本轮没有形成恢复证据。",
        "errors": [],
    }
    return {
        "version": 1,
        "lineage_id": str(plan.get("_lineage_id") or existing.get("lineage_id") or job_id),
        "parent_job_id": job_id,
        "attempt_count": prior_attempt_count + len(current_summaries),
        "attempted_strategy_fingerprints": sorted(
            set(attempted)
            | {str(value) for value in (plan.get("_attempted_strategy_fingerprints") or existing.get("attempted_strategy_fingerprints") or []) if value}
        ),
        "attempted_change_fingerprints": sorted(change_fingerprints),
        "attempted_actions": sorted(actions),
        "attempts": deduplicated[-12:],
        "last_failure": _redact_sensitive(last_attempt),
    }


def _attach_ops_continuation_context(
    job_id: str,
    plan: dict,
    result: dict,
    attempted: set[str],
    history: list[dict],
) -> dict:
    context = _build_ops_continuation_context(job_id, plan, result, attempted, history)
    result["continuation_context"] = context
    for candidate in result.get("alternative_plans") or []:
        if not isinstance(candidate, dict):
            continue
        candidate["continuation_context"] = copy.deepcopy(context)
        candidate["previous_attempt"] = copy.deepcopy(context.get("last_failure") or {})
    return result


def _apply_ops_continuation_context(plan: dict) -> dict:
    context = plan.get("continuation_context") if isinstance(plan.get("continuation_context"), dict) else {}
    if not context:
        return plan
    plan["_lineage_id"] = str(context.get("lineage_id") or plan.get("_lineage_id") or "")
    plan["_parent_job_id"] = str(context.get("parent_job_id") or "")
    plan["_prior_attempts"] = [item for item in (context.get("attempts") or []) if isinstance(item, dict)][-12:]
    plan["_prior_attempt_count"] = max(int(context.get("attempt_count") or 0), len(plan["_prior_attempts"]))
    plan["_attempted_strategy_fingerprints"] = sorted({
        str(value)
        for value in [*(plan.get("_attempted_strategy_fingerprints") or []), *(context.get("attempted_strategy_fingerprints") or [])]
        if value
    })
    plan["_attempted_change_fingerprints"] = sorted({
        str(value)
        for value in [*(plan.get("_attempted_change_fingerprints") or []), *(context.get("attempted_change_fingerprints") or [])]
        if value
    })
    plan["_attempted_actions"] = sorted({
        str(value)
        for value in [*(plan.get("_attempted_actions") or []), *(context.get("attempted_actions") or [])]
        if value
    })
    if isinstance(context.get("last_failure"), dict):
        plan["_last_failure"] = copy.deepcopy(context["last_failure"])
    return plan


def _operator_blocking_execution_failure(result: dict) -> bool:
    """只识别运维执行通道本身的阻断，不读取恢复验证中的业务错误文本。"""
    hard_terms = (
        "403", "forbidden", "unauthorized", "rbac", "serviceaccount cannot",
        "certificate verify failed", "ssl verify", "connection refused",
        "network is unreachable", "no route to host", "name or service not known",
        "executor not configured", "token is invalid",
    )
    for item in result.get("results") or []:
        if not isinstance(item, dict) or item.get("status") not in {"failed", "blocked"}:
            continue
        raw = item.get("result") if isinstance(item.get("result"), dict) else {}
        if item.get("permission_guidance") or raw.get("permission_guidance"):
            return True
        if raw.get("timeout"):
            return True
        error_text = json.dumps(
            _redact_sensitive({
                "error": raw.get("error"),
                "type": raw.get("type") or raw.get("exception_type"),
                "http_status": raw.get("http_status") or raw.get("status_code"),
            }),
            ensure_ascii=False,
            default=str,
        ).lower()
        if any(term in error_text for term in hard_terms):
            return True
    return False


def _operator_steps_from_execution_failure(result: dict) -> list[str]:
    steps: list[str] = []
    for item in result.get("results") or []:
        if not isinstance(item, dict):
            continue
        guidance = item.get("permission_guidance") or ((item.get("result") or {}).get("permission_guidance") if isinstance(item.get("result"), dict) else {}) or {}
        candidates = guidance.get("do_this") or ((item.get("result") or {}).get("operator_steps") if isinstance(item.get("result"), dict) else []) or []
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value and value not in steps:
                steps.append(value)
    return steps[:8]


def _plan_has_followup_work(plan: dict) -> bool:
    return bool(
        plan.get("changes")
        or plan.get("steps")
        or plan.get("operator_steps")
        or plan.get("summary")
        or plan.get("source")
        or plan.get("strategy_source")
    )


def _plan_can_continue_in_job(plan: dict, autonomous: bool) -> bool:
    if not _plan_has_followup_work(plan):
        return False
    changes = plan.get("changes") or []
    if not changes:
        return True
    if not autonomous:
        return True
    # 自动运维可以把高风险动作推进到“等待人工逐步确认”，但不会绕过确认直接提交。
    return _autonomous_plan_allowed(plan) or bool(
        plan.get("requires_confirmation")
        or plan.get("requires_high_risk_confirmation")
        or plan.get("stepwise_confirmation")
    )


def _select_next_ops_plan(alternatives: list[dict], attempted: set[str], autonomous: bool) -> dict | None:
    for candidate in alternatives:
        if not isinstance(candidate, dict):
            continue
        fingerprint = _change_fingerprint(candidate)
        if fingerprint in attempted:
            continue
        if _plan_can_continue_in_job(candidate, autonomous):
            return candidate
    return None


def _append_manual_exit_if_needed(plan: dict, result: dict, reason: str, attempted: set[str]) -> dict:
    if not isinstance(result, dict):
        result = {}
    if result.get("operator_steps"):
        return result
    alternatives = [item for item in (result.get("alternative_plans") or []) if isinstance(item, dict)]
    if any(item.get("operator_steps") for item in alternatives):
        return result
    verification = result.get("verification") or {}
    manual_plan = _manual_required_followup_plan(plan, verification, reason)
    manual_plan["previous_strategy"] = ", ".join(sorted(attempted)) or plan.get("title") or "unknown"
    result["alternative_plans"] = alternatives + [manual_plan]
    result["operator_steps"] = manual_plan.get("operator_steps") or _manual_required_steps(plan, verification, reason)
    result["blocked_reason"] = reason
    next_steps = _ops_terminal_next_steps(plan, verification, result["alternative_plans"], result["operator_steps"])
    result["next_steps"] = next_steps
    if isinstance(verification, dict):
        verification["next_steps"] = next_steps
        verification.setdefault("blocked_reason", reason)
    return result


async def _execute_ops_plan_once(
    plan: dict,
    cancel_event: asyncio.Event | None = None,
    progress=None,
    summarize: bool = True,
    change_approval=None,
) -> dict:
    async def emit(stage: str, message: str, **extra):
        if progress:
            await progress(stage, message, **extra)

    def heartbeat(stage: str, waiting_on: str):
        async def report(elapsed: float, remaining: float):
            await emit(
                stage,
                f"仍在执行：{waiting_on}（已等待 {int(elapsed)} 秒）",
                elapsed_seconds=round(elapsed, 1),
                remaining_seconds=round(remaining, 1),
                waiting_on=waiting_on,
                level="info",
            )
        return report

    if cancel_event and cancel_event.is_set():
        return {"status": "cancelled", "executed": False, "message": "任务已中断，未执行新的运维动作。"}
    release_gate = _ops_release_gate(plan)
    await emit("release_gate", "变更风险门禁已完成", release_gate=release_gate)
    if release_gate.get("allowed") is False:
        await emit(
            "release_blocked",
            release_gate.get("reason") or "SRE 变更门禁已阻断本次操作。",
            status="blocked",
            release_gate=release_gate,
            level="warning",
        )
        return {
            "status": "blocked",
            "executed": False,
            "release_gate": release_gate,
            "message": release_gate.get("reason") or "变更被错误预算门禁阻断。",
        }
    await emit("collecting_evidence", "采集 current/previous logs、Events、Workload、Service、存储与节点证据")
    evidence_timeout = max(10, int(os.getenv("OPS_EVIDENCE_TIMEOUT_SECONDS", "70")))
    try:
        plan["_runtime_evidence"] = await run_with_heartbeat(
            _collect_plan_deep_evidence(plan),
            stage="collecting_evidence",
            timeout_seconds=evidence_timeout,
            heartbeat_seconds=float(os.getenv("OPS_HEARTBEAT_SECONDS", "5")),
            cancel_event=cancel_event,
            on_heartbeat=heartbeat("collecting_evidence", "Rancher/Kubernetes/MCP 证据接口"),
        )
    except StageTimeoutError as exc:
        plan["_runtime_evidence"] = {
            "error": str(exc),
            "timeout": True,
            "operator_hint": "检查 Rancher API、MCP Server 网络和 RBAC；本轮会使用已有证据继续，不会永久卡住。",
        }
        await emit(
            "stage_timeout",
            f"证据采集超过 {evidence_timeout} 秒，已熔断慢调用并继续执行可用步骤。",
            timed_out_stage="collecting_evidence",
            timeout_seconds=evidence_timeout,
            level="warning",
        )
    except Exception as exc:
        plan["_runtime_evidence"] = {"error": f"{type(exc).__name__}: {_redact_text(str(exc))}"}
    deep_evidence = plan.get("_runtime_evidence") or {}
    evidence_summary = {
        "status": "warning" if deep_evidence.get("error") else "completed",
        "logs": len(deep_evidence.get("logs") or {}),
        "log_errors": sum(len(value or {}) for value in (deep_evidence.get("log_errors") or {}).values()),
        "events": len(deep_evidence.get("events") or []),
        "services": len(deep_evidence.get("services") or []),
        "storage": len(deep_evidence.get("storage") or []),
        "has_workload": bool(deep_evidence.get("workload")),
        "matching_pods": len(deep_evidence.get("matching_pods") or []),
        "selected_pod": deep_evidence.get("pod_name") or ((deep_evidence.get("pod") or {}).get("name")),
        "node": (deep_evidence.get("node") or {}).get("name"),
        "error": deep_evidence.get("error"),
    }
    await emit(
        "collecting_evidence_done",
        "证据采集完成，可进入逐步诊断。",
        evidence_summary=evidence_summary,
        level="warning" if deep_evidence.get("error") else "success",
    )
    if not deep_evidence.get("error"):
        changes_before_skill_route = len(plan.get("changes") or [])
        _attach_operator_skills_to_plan(
            plan,
            _skill_signal_payload(
                question=str(plan.get("summary") or plan.get("reason") or plan.get("title") or ""),
                alert={
                    "cluster": plan.get("cluster"),
                    "cluster_id": plan.get("cluster_id"),
                    "namespace": plan.get("namespace"),
                    "target": plan.get("target"),
                    "workload_type": _workload_identity_from_plan(plan)[1],
                    "workload_name": _workload_identity_from_plan(plan)[2],
                    "pod": _target_pod_from_plan(plan),
                },
                diagnosis={
                    "root_cause": plan.get("root_cause") or plan.get("reason") or plan.get("summary") or "",
                    "signals": deep_evidence.get("events") or [],
                    "previous_failure": plan.get("_last_failure") or {},
                },
                evidence=deep_evidence,
                plan=plan,
            ),
        )
        selected_skills = [
            {"id": item.get("id"), "name": item.get("name"), "confidence": item.get("confidence")}
            for item in (plan.get("operator_skills") or [])[:3]
            if isinstance(item, dict)
        ]
        await emit(
            "skill_routed",
            (
                "已按本轮实时证据重新匹配运维 Skill，并校验动作授权与恢复判据。"
                if selected_skills else
                "本轮实时证据没有匹配到可执行 Skill；写操作已阻断，继续进入证据重规划。"
            ),
            matched_skills=selected_skills,
            rejected_change_count=max(0, changes_before_skill_route - len(plan.get("changes") or [])),
            level="success" if selected_skills else "warning",
        )
    elif plan.get("changes"):
        # A stale Skill match or task-level confirmation cannot authorize a
        # mutation when fresh evidence collection failed. Preserve the proposal
        # for audit, convert this attempt to diagnosis, and replan safely.
        plan["_blocked_unverified_changes"] = copy.deepcopy(plan.get("changes") or [])
        plan["changes"] = []
        plan["decision"] = "fresh_evidence_required"
        plan["evidence_gap"] = (
            "实时 Kubernetes/Rancher 证据采集失败，无法重新校验 Skill 的 evidence_required；"
            "本轮不会提交任何变更。"
        )
        await emit(
            "skill_routed",
            "实时证据不可用，动态 Skill 执行门禁已阻断全部写操作；继续输出取证与恢复通道方案。",
            rejected_change_count=len(plan.get("_blocked_unverified_changes") or []),
            evidence_error=deep_evidence.get("error"),
            level="warning",
        )
    preflight_replans: list[dict] = []
    preflight_conflict = False
    if plan.get("changes") and not deep_evidence.get("error"):
        await emit("replanning", "用实时证据复核原方案，避免把症状当成根因")
        preflight_attempted = {str(change.get("type") or "") for change in plan.get("changes") or []}
        preflight_attempted.update(str(action) for action in (plan.get("_attempted_actions") or []) if action)
        preflight_replans = await _evidence_based_replan(
            plan,
            [],
            preflight_attempted,
            include_llm=False,
        )
        preflight_conflict = _preflight_evidence_conflict(plan)
        if preflight_conflict:
            meta = plan.get("_runtime_replan") or {}
            await emit(
                "strategy_switch",
                f"实时证据已否定原动作；根因转为 {meta.get('runbook_id') or '新的故障类别'}，原变更不会提交。",
                alternative_plan_count=len(preflight_replans),
                level="warning",
            )
    executed_steps = []
    steps = [step if isinstance(step, dict) else {"title": str(step)} for step in plan.get("steps", [])]
    for index, step in enumerate(steps, start=1):
        if cancel_event and cancel_event.is_set():
            return {
                "status": "cancelled",
                "executed": False,
                "steps": executed_steps,
                "release_gate": release_gate,
                "message": "任务已在诊断阶段中断，未继续提交变更。",
            }
        step_title = step.get("title") or step.get("name") or f"诊断步骤 {index}"
        await emit(
            "step_start",
            f"开始：{step_title}",
            step_index=index,
            steps_total=len(steps),
            step={"id": step.get("id"), "title": step_title, "description": step.get("description") or step.get("detail")},
        )
        step_timeout = max(5, int(os.getenv("OPS_STEP_TIMEOUT_SECONDS", "35")))
        try:
            step_result = await run_with_heartbeat(
                _collect_ops_step(step, plan),
                stage="step_waiting",
                timeout_seconds=step_timeout,
                heartbeat_seconds=float(os.getenv("OPS_HEARTBEAT_SECONDS", "5")),
                cancel_event=cancel_event,
                on_heartbeat=heartbeat("step_waiting", step_title),
            )
        except StageTimeoutError:
            step_result = {
                **step,
                "title": step_title,
                "status": "warning",
                "logs": [
                    f"[timeout] 诊断步骤超过 {step_timeout} 秒，已主动终止等待。",
                    "[next] 检查对应 Rancher/MCP/Kubernetes API 的网络、权限和响应时间。",
                ],
                "artifacts": {"timeout_seconds": step_timeout, "timed_out": True},
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            await emit(
                "stage_timeout",
                f"{step_title} 超过 {step_timeout} 秒，已跳过该慢探针，流程继续。",
                timed_out_stage="diagnostic_step",
                step_index=index,
                timeout_seconds=step_timeout,
                level="warning",
            )
        executed_steps.append(step_result)
        step_logs = step_result.get("logs") or []
        step_status = step_result.get("status") or "completed"
        await emit(
            "step_done",
            f"完成：{step_title}",
            step_index=index,
            steps_total=len(steps),
            step_status=step_status,
            step_result={
                "title": step_title,
                "status": step_status,
                "finished_at": step_result.get("finished_at"),
                "logs_tail": step_logs[-10:],
                "artifacts": sorted(list((step_result.get("artifacts") or {}).keys())),
            },
            level="warning" if step_status == "warning" else "success",
        )

    if preflight_conflict:
        meta = plan.get("_runtime_replan") or {}
        evidence_gap = str(meta.get("evidence_gap") or "实时证据与原方案冲突，需要核对新的最小变更。")
        has_candidate = bool(preflight_replans)
        verification = {
            "status": "diagnostic_completed",
            "recovered": None,
            "message": (
                "根因已重新定位，已生成新的受控修复方案；原变更已取消。"
                if has_candidate else
                "根因已重新定位，但缺少创建安全变更所需的批准参数；原变更已取消。"
            ),
            "proof": "实时 Events、PVC/PV 状态与原动作不匹配，系统未提交已失效的变更。",
            "blocked_reason": evidence_gap,
            "operator_steps": (
                ["核对下方新方案的目标、资源清单与回滚方式。", "高风险存储变更需要重新勾选确认后执行。"]
                if has_candidate else
                [
                    evidence_gap,
                    "由存储管理员提供批准的 StorageClass、NFS/CSI/LUN 模板；平台不会让 LLM 猜测生产存储路径。",
                    "在 ConfigMap k8s-agent-config 填写 AUTO_OPS_STATIC_PV_TEMPLATE_JSON，或填写 AUTO_OPS_STATIC_PV_NFS_SERVER 与 AUTO_OPS_STATIC_PV_NFS_BASE_PATH。",
                    "确认 k8s-agent-storage-provisioner 已绑定后重新运行本计划，系统将生成可确认的 create_pv/create_pvc 变更。",
                ]
            ),
        }
        next_steps = _ops_terminal_next_steps(plan, verification, preflight_replans, verification["operator_steps"])
        verification["next_steps"] = next_steps
        await emit(
            "verification_done",
            verification["message"],
            verification=verification,
            level="success" if has_candidate else "warning",
        )
        return {
            "status": "planned" if has_candidate else "diagnostic_completed",
            "executed": False,
            "steps": executed_steps,
            "changes": [],
            "results": [],
            "release_gate": release_gate,
            "verification": verification,
            "alternative_plans": preflight_replans,
            "blocked_reason": evidence_gap,
            "operator_steps": verification["operator_steps"],
            "next_steps": next_steps,
            "message": verification["message"],
        }

    results = []
    changes = [change if isinstance(change, dict) else {"type": str(change)} for change in plan.get("changes", [])]
    for index, change in enumerate(changes, start=1):
        if cancel_event and cancel_event.is_set():
            break
        change_target = f"{change.get('workload_type') or change.get('kind') or 'resource'}/{change.get('workload_name') or change.get('name') or plan.get('target') or '-'}"
        if change_approval is not None:
            approved = await change_approval(index, len(changes), change, change_target)
            if not approved:
                return {
                    "status": "cancelled",
                    "executed": bool(results),
                    "steps": executed_steps,
                    "results": results,
                    "release_gate": release_gate,
                    "message": f"第 {index} 项变更未获人工确认，后续动作已停止。",
                }
            change["human_approved"] = True
            change["operator_confirmed"] = True
        await emit(
            "change_start",
            f"提交变更：{change.get('type', 'change')} -> {change_target}",
            change_index=index,
            changes_total=len(changes),
            change={
                "type": change.get("type"),
                "target": change_target,
                "namespace": change.get("namespace") or plan.get("namespace"),
                "patch": change.get("patch"),
                "replicas": change.get("replicas"),
            },
        )
        change_timeout = max(10, int(os.getenv("OPS_CHANGE_TIMEOUT_SECONDS", "45")))
        try:
            change_result = await run_with_heartbeat(
                _execute_change(change, plan),
                stage="change_waiting",
                timeout_seconds=change_timeout,
                heartbeat_seconds=float(os.getenv("OPS_HEARTBEAT_SECONDS", "5")),
                cancel_event=cancel_event,
                on_heartbeat=heartbeat("change_waiting", f"受控变更执行器 {change_target}"),
            )
        except StageTimeoutError:
            change_result = {
                "change": _redact_sensitive(change),
                "status": "failed",
                "result": {
                    "error": f"Kubernetes 变更在 {change_timeout} 秒内没有返回，已停止后续自动动作。",
                    "timeout": True,
                    "operator_steps": ["读取目标 Workload 当前 generation/observedGeneration，确认 API 是否已经受理变更。"],
                },
            }
            await emit(
                "stage_timeout",
                f"{change_target} 的变更调用超时，状态不确定，已熔断后续动作。",
                timed_out_stage="kubernetes_change",
                change_index=index,
                timeout_seconds=change_timeout,
                level="error",
            )
        except Exception as exc:
            safe_error = f"{type(exc).__name__}: {_redact_text(str(exc))}"
            change_result = {
                "change": _redact_sensitive(change),
                "status": "failed",
                "result": {
                    "error": safe_error,
                    "exception_type": type(exc).__name__,
                    "operator_steps": [
                        "查看下方原始 API 回执，确认失败发生在 Rancher/Kubernetes/MCP/审计哪个环节。",
                        "如果是 403/Forbidden，先补 Rancher Token 或 ServiceAccount 的最小 RBAC 后重新执行同一计划。",
                        "如果是 409/Conflict，刷新目标对象 resourceVersion 后重新生成预演，避免覆盖新变更。",
                    ],
                },
            }
            await emit(
                "change_exception",
                f"{change_target} 的变更执行器异常退出，已停止把未知状态误报为成功。",
                change_index=index,
                changes_total=len(changes),
                error=safe_error,
                level="error",
            )
        results.append(change_result)
        change_status = change_result.get("status") or "completed"
        raw_result = change_result.get("result")
        await emit(
            "change_done",
            (
                f"变更返回：{change_target} -> {change_status}"
                if change_status not in {"failed", "blocked"} else
                f"变更未通过：{change_target} -> {change_status}"
            ),
            change_index=index,
            changes_total=len(changes),
            change_status=change_status,
            change_result={
                "status": change_status,
                "change": change_result.get("change") or _redact_sensitive(change),
                "error": (raw_result or {}).get("error") if isinstance(raw_result, dict) else None,
                "permission_guidance": change_result.get("permission_guidance") or (
                    (raw_result or {}).get("permission_guidance") if isinstance(raw_result, dict) else None
                ),
                "result_preview": _clip_text(json.dumps(_redact_sensitive(raw_result), ensure_ascii=False, default=str), 2200),
            },
            level="error" if change_status == "failed" else "warning" if change_status == "blocked" else "success",
        )

    if cancel_event and cancel_event.is_set():
        return {
            "status": "cancelled",
            "executed": bool(results),
            "steps": executed_steps,
            "results": results,
            "release_gate": release_gate,
            "message": "任务已中断；不会执行剩余变更。已提交动作的最终状态需要人工复核。",
        }
    failed = [r for r in results if r.get("status") in {"failed", "blocked"}]
    attempted_actions = {str(change.get("type") or "") for change in plan.get("changes", [])}
    attempted_actions.update(str(action) for action in (plan.get("_attempted_actions") or []) if action)
    evidence_replans: list[dict] = []
    if not plan.get("changes"):
        current_health = await _probe_plan_recovery(plan, [])
        if current_health.get("recovered") is True:
            current_health.update({
                "status": "completed",
                "message": "只读复核确认目标已经恢复，不再生成或提交新的变更。",
                "proof": current_health.get("proof") or "目标 Workload 的活动 Pod 均为 Ready，且未命中已知运行时异常。",
            })
            next_steps = _ops_terminal_next_steps(plan, current_health)
            current_health["next_steps"] = next_steps
            await emit(
                "verification_done",
                current_health["message"],
                verification=current_health,
                level="success",
            )
            return {
                "status": "completed",
                "executed": False,
                "steps": executed_steps,
                "changes": [],
                "results": [],
                "release_gate": release_gate,
                "verification": current_health,
                "alternative_plans": [],
                "operator_steps": [],
                "next_steps": next_steps,
                "message": current_health["message"],
            }
        evidence_replans = await _evidence_based_replan(plan, executed_steps, attempted_actions)
        evidence_gap = str(
            ((plan.get("planning") or {}).get("evidence_gap"))
            or (plan.get("_runtime_replan") or {}).get("evidence_gap")
            or plan.get("evidence_gap")
            or "现有日志、Events 与对象状态没有形成可验证的单一根因。"
        )
        verification = {
            "status": "diagnostic_completed",
            "recovered": None,
            "message": "深度诊断证据采集完成；已生成受控候选方案。" if evidence_replans else "深度诊断完成，但证据仍不足以支持安全变更。",
            "proof": "本轮只读，不宣称故障已经恢复。",
            "blocked_reason": evidence_gap,
            "operator_steps": [
                "查看每个诊断步骤中的日志、Events、存储链和 Workload 实际配置。",
                "补齐界面提示的存储后端、目标对象或 RBAC 权限后重新运行诊断。",
                "若候选策略已生成，在下方核对具体变更并由操作员确认执行。",
            ],
        }
        next_steps = _ops_terminal_next_steps(plan, verification, evidence_replans, verification["operator_steps"])
        verification["next_steps"] = next_steps
        await emit(
            "replanning",
            "LLM 与 EvidenceRunbookEngine 已基于真实证据重新规划",
            alternative_plan_count=len(evidence_replans),
            level="success" if evidence_replans else "warning",
        )
        await emit(
            "verification_done",
            verification["message"],
            verification=verification,
            level="warning" if not evidence_replans else "success",
        )
        await emit(
            "summarizing",
            "整理诊断证据、候选修复方案和下一步确认项",
            alternative_plan_count=len(evidence_replans),
        )
        ai_summary = await _llm_ops_summary(plan, executed_steps, []) if summarize else {
            "source": "deterministic", "content": verification["message"], "followup_plans": evidence_replans,
        }
        ai_summary["followup_plans"] = evidence_replans + (ai_summary.get("followup_plans") or [])
        return {
            "status": "planned" if evidence_replans else "diagnostic_completed",
            "executed": False,
            "steps": executed_steps,
            "changes": [],
            "results": [],
            "release_gate": release_gate,
            "verification": verification,
            "alternative_plans": evidence_replans,
            "ai_summary": ai_summary,
            "blocked_reason": evidence_gap,
            "operator_steps": verification["operator_steps"],
            "next_steps": next_steps,
            "message": verification["message"],
        }
    verify_grace = max(0, int(os.getenv("OPS_VERIFY_INITIAL_GRACE_SECONDS", "15")))
    await emit(
        "verifying",
        f"等待 Workload rollout；先给新 Pod {verify_grace} 秒重建/拉起窗口，再验证是否真正恢复"
        if verify_grace else
        "等待 Workload rollout 并验证 Pod 是否真正恢复",
        initial_grace_seconds=verify_grace,
    )
    verification = await _verify_plan_recovery(plan, results, cancel_event)
    await emit(
        "verification_done",
        verification.get("message") or verification.get("status") or "恢复验证完成",
        verification=verification,
        level="success" if verification.get("recovered") is True else "warning",
    )
    if verification.get("recovered") is not True:
        current_change_fingerprints = {
            _change_item_fingerprint(change)
            for change in (plan.get("changes") or [])
            if isinstance(change, dict)
        }
        prior_change_fingerprints = {
            str(value) for value in (plan.get("_attempted_change_fingerprints") or []) if value
        }
        plan["_attempted_change_fingerprints"] = sorted(prior_change_fingerprints | current_change_fingerprints)
        plan["_last_failure"] = {
            "attempted_changes": [_canonical_change_value(change) for change in (plan.get("changes") or []) if isinstance(change, dict)],
            "change_results": _redact_sensitive(results),
            "verification": _redact_sensitive(verification),
            "instruction": "上一轮未恢复，下一轮不得重复同一动作、目标和参数。",
        }
        failed_after_change = next(
            (
                item for item in (verification.get("terminal_unresolved") or verification.get("unresolved") or [])
                if item.get("name")
            ),
            None,
        )
        if failed_after_change and failed_after_change.get("name"):
            plan["pod_name"] = failed_after_change["name"]
        await emit(
            "replanning",
            "恢复验证未通过，重新采集失败后新 Pod 的 Logs、Events、Workload、PVC/PV 证据，再生成下一轮修复方案。",
            failed_pod=(failed_after_change or {}).get("name"),
            level="warning",
        )
        post_failure_timeout = max(10, int(os.getenv("OPS_POST_FAILURE_EVIDENCE_TIMEOUT_SECONDS", "45")))
        try:
            plan["_previous_runtime_evidence"] = plan.get("_runtime_evidence") or {}
            plan["_runtime_evidence"] = await run_with_heartbeat(
                _collect_plan_deep_evidence(plan),
                stage="post_failure_evidence",
                timeout_seconds=post_failure_timeout,
                heartbeat_seconds=float(os.getenv("OPS_HEARTBEAT_SECONDS", "5")),
                cancel_event=cancel_event,
                on_heartbeat=heartbeat("collecting_evidence", "失败后新 Pod 证据接口"),
            )
            await emit(
                "collecting_evidence_done",
                "失败后证据已更新，开始生成差异化下一轮方案。",
                evidence_summary={
                    "logs": len((plan.get("_runtime_evidence") or {}).get("logs") or {}),
                    "events": len((plan.get("_runtime_evidence") or {}).get("events") or []),
                    "storage": len((plan.get("_runtime_evidence") or {}).get("storage") or []),
                    "selected_pod": (plan.get("_runtime_evidence") or {}).get("pod_name") or plan.get("pod_name"),
                },
                level="success",
            )
        except StageTimeoutError:
            await emit(
                "stage_timeout",
                f"失败后证据采集超过 {post_failure_timeout} 秒，使用已有验证结果生成下一轮方案。",
                timed_out_stage="post_failure_evidence",
                timeout_seconds=post_failure_timeout,
                level="warning",
            )
        except Exception as exc:
            plan["_runtime_evidence"] = {
                **(plan.get("_runtime_evidence") or {}),
                "post_failure_error": f"{type(exc).__name__}: {_redact_text(str(exc))}",
            }
            await emit(
                "collecting_evidence_done",
                f"失败后证据采集异常，使用已有证据继续规划：{type(exc).__name__}",
                level="warning",
            )
        evidence_replans = await _evidence_based_replan(plan, executed_steps, attempted_actions)
    raw_alternatives = evidence_replans + _derive_alternative_plans(plan, verification, results)
    alternative_plans: list[dict] = []
    for candidate in raw_alternatives:
        if not isinstance(candidate, dict):
            continue
        candidate = _attach_operator_skills_to_plan(
            candidate,
            _skill_signal_payload(
                question=str(candidate.get("summary") or candidate.get("reason") or plan.get("summary") or ""),
                alert={
                    "cluster": plan.get("cluster"),
                    "cluster_id": plan.get("cluster_id"),
                    "namespace": candidate.get("namespace") or plan.get("namespace"),
                    "target": candidate.get("target") or plan.get("target"),
                },
                diagnosis={
                    "root_cause": candidate.get("summary") or plan.get("summary") or "",
                    "signals": (plan.get("_runtime_evidence") or {}).get("events") or [],
                    "previous_failure": plan.get("_last_failure") or {},
                },
                evidence=plan.get("_runtime_evidence") or plan.get("evidence") or {},
                plan=candidate,
            ),
        )
        candidate["stepwise_confirmation"] = bool(candidate.get("changes"))
        candidate["high_risk_confirmed"] = False
        candidate["operator_force_execute"] = False
        for change in candidate.get("changes") or []:
            if isinstance(change, dict):
                change.pop("human_approved", None)
                change.pop("operator_confirmed", None)
                change.pop("approval_receipt", None)
        alternative_plans.append(candidate)
    await emit("summarizing", "生成执行结论和下一步策略" if summarize else "使用确定性证据判断是否需要策略升级")
    ai_summary = await _llm_ops_summary(plan, executed_steps, results) if summarize else {
        "source": "deterministic",
        "content": "已完成本轮变更与恢复验证；自治流程将在最终状态统一生成 AI 总结。",
        "followup_plans": [],
    }
    if alternative_plans:
        existing = ai_summary.get("followup_plans") or []
        ai_summary["followup_plans"] = alternative_plans + existing
    # Kubernetes API 接受变更不等于故障恢复。只有明确 recovered=true 才
    # 能完成闭环；验证超时/不可读/目标未知都保持 unresolved 并继续给方案。
    unresolved = verification.get("recovered") is not True and not failed
    operator_steps: list[str] = []
    for alt_plan in alternative_plans:
        if alt_plan.get("source") != "storage_admin_required":
            continue
        for step in alt_plan.get("operator_steps") or []:
            text = str(step or "").strip()
            if text and text not in operator_steps:
                operator_steps.append(text)
    if (
        unresolved
        and _storage_permission_detected(plan, ai_summary.get("content") or "")
        and _storage_admin_boundary_detected(plan)
        and not operator_steps
    ):
        operator_steps = _storage_admin_steps(plan, "当前策略未恢复，需确认底层存储目录权限或命名空间安全策略。")
    if unresolved and not alternative_plans and not operator_steps:
        operator_steps = _manual_required_steps(plan, verification)
        alternative_plans = [_manual_required_followup_plan(plan, verification)]
        ai_summary["followup_plans"] = alternative_plans + (ai_summary.get("followup_plans") or [])
    next_steps = _ops_terminal_next_steps(plan, verification, alternative_plans, operator_steps, failed)
    verification["next_steps"] = next_steps
    payload = {
        "status": "failed" if failed else "unresolved" if unresolved else "completed",
        "executed": not failed,
        "steps": executed_steps,
        "changes": plan.get("changes", []),
        "results": results,
        "release_gate": release_gate,
        "verification": verification,
        "alternative_plans": alternative_plans,
        "ai_summary": ai_summary,
        "next_steps": next_steps,
        "message": (
            "部分 Kubernetes 变更失败，详情见 results。"
            if failed else
            "变更已执行，但尚未取得恢复证据，已切换替代策略。"
            if unresolved else
            "AI 运维流程执行完成，恢复验证明确通过。"
        ),
    }
    if operator_steps:
        payload["operator_steps"] = operator_steps[:8]
        payload["blocked_reason"] = "当前策略未取得恢复证据；系统已生成可执行的替代策略或管理员处理步骤。"
    try:
        candidate_skill = _capture_candidate_skill(plan, payload)
        if candidate_skill:
            payload["candidate_skill"] = candidate_skill
            payload["message"] += " 已生成待用户确认的候选 Skill。"
    except Exception as exc:
        payload["skill_learning_warning"] = f"{type(exc).__name__}: {_redact_text(str(exc))}"
    payload["effectiveness"] = record_remediation(plan, payload, model_id=plan.get("model_profile_id") or get_active_model_profile_id() or os.getenv("LLM_MODEL", "default"))
    return payload


async def execute_ops_plan(req: OpsExecuteRequest):
    plan = _enrich_plan_change_policies(copy.deepcopy(req.plan or {}))
    submitted_change_count = len(plan.get("changes") or [])
    plan = _attach_operator_skills_to_plan(
        plan,
        _skill_signal_payload(
            question=str(plan.get("summary") or plan.get("reason") or plan.get("title") or ""),
            alert={"cluster": plan.get("cluster"), "namespace": plan.get("namespace"), "target": plan.get("target")},
            diagnosis={"root_cause": plan.get("root_cause") or plan.get("reason") or plan.get("summary") or ""},
            evidence=plan.get("evidence") or {},
            plan=plan,
        ),
        preferred_skill_ids=[str(plan.get("selected_skill_id") or "")] if plan.get("selected_skill_id") else None,
    )
    if submitted_change_count and not plan.get("changes"):
        raise HTTPException(status_code=422, detail={
            "message": "变更没有通过动态 Skill 动作授权，不会执行。",
            "reason": plan.get("evidence_gap") or "matched Skill required",
        })
    if plan.get("changes") and not _env_bool("OPS_MUTATION_ENABLED", "false"):
        raise HTTPException(status_code=403, detail={
            "message": "服务端 OPS_MUTATION_ENABLED=false，当前只允许预演，不会提交集群变更。",
            "do_this": [
                "确认安全部门允许该环境开启 AI 运维变更。",
                "在 ConfigMap/Deployment 环境变量中设置 OPS_MUTATION_ENABLED=true。",
                "保留人工确认开关；高风险动作仍需要二次确认。",
            ],
        })
    if plan.get("changes"):
        return {
            "status": "pending_stepwise_approval",
            "executed": False,
            "message": "同步执行接口不再提交真实变更。请将该计划提交到 /api/ops/jobs，由统一状态机逐项展示并等待人工审批。",
            "plan": plan,
        }
    return await _execute_ops_plan_once(plan)


def _skill_signal_payload(*, question: str = "", alert: dict | None = None, diagnosis: dict | None = None, evidence: dict | None = None, plan: dict | None = None) -> dict:
    """把问题、诊断、证据和计划归一成 Skill 匹配输入。"""
    return {
        "question": question,
        "alert": alert or {},
        "diagnosis": diagnosis or {},
        "evidence": evidence or {},
        "plan": plan or {},
    }


def _public_skill_match(match: dict) -> dict:
    skill = match.get("skill") or {}
    return {
        "id": skill.get("id"),
        "name": skill.get("name"),
        "category": skill.get("category"),
        "summary": skill.get("summary"),
        "risk": skill.get("risk"),
        "confidence": match.get("confidence"),
        "score": match.get("score"),
        "why": match.get("why"),
        "allowed_actions": skill.get("allowed_actions") or [],
        "evidence_required": skill.get("evidence_required") or [],
        "success_criteria": skill.get("success_criteria") or [],
        "script_policy": skill.get("script_policy") or {"enabled": False},
        "version": skill.get("version"),
        "format": skill.get("format"),
        "portable": skill.get("portable", False),
        "execution_ready": skill.get("execution_ready", False),
        "execution_authorized": bool(match.get("_execution_authorized")),
        "evidence_collected": match.get("_evidence_collected") or [],
        "evidence_missing": match.get("_evidence_missing") or [],
    }


def _capture_candidate_skill(plan: dict, result: dict) -> dict | None:
    """Turn a verified recovery into a disabled candidate that an operator can publish."""
    verification = result.get("verification") or {}
    if verification.get("recovered") is not True or not plan.get("changes"):
        return None
    action_types = sorted({str(item.get("type") or "") for item in plan.get("changes") or [] if isinstance(item, dict) and item.get("type")})
    if not action_types:
        return None
    root_cause = str(plan.get("root_cause") or plan.get("reason") or ((plan.get("planning") or {}).get("root_cause") or "已验证故障恢复"))
    target = str(plan.get("target") or "Kubernetes resource")
    digest = hashlib.sha256(f"{root_cause}|{target}|{','.join(action_types)}".encode()).hexdigest()[:12]
    evidence = plan.get("_runtime_evidence") or plan.get("evidence") or {}
    evidence_text = " ".join([
        str(root_cause),
        str((evidence.get("logs") or {})),
        str((evidence.get("events") or [])),
    ]).lower()
    known_symptoms = [
        term for term in (
            "permission denied", "mkdir", "crashloopbackoff", "failedmount", "imagepullbackoff",
            "oomkilled", "failedscheduling", "read-only file system",
        ) if term in evidence_text
    ]
    diagnostics = [
        str(step.get("description") or step.get("title") or "").strip()
        for step in plan.get("steps") or [] if isinstance(step, dict) and str(step.get("description") or step.get("title") or "").strip()
    ]
    candidate = {
        "id": f"candidate-{digest}",
        "name": f"候选 Skill：{target} 已验证恢复",
        "category": "learned_recovery",
        "summary": root_cause[:800],
        "symptoms": known_symptoms or [root_cause[:160]],
        "applies_to": [str((plan.get("target") or "Kubernetes").split("/", 1)[0])],
        "evidence_required": list(plan.get("diagnostic_actions") or ["previous_logs", "events", "workload_spec"]),
        "diagnostic_steps": diagnostics[:10] or ["复核与本次恢复相同的实时证据、对象配置和最近变更。"],
        "allowed_actions": action_types,
        "success_criteria": list(plan.get("success_criteria") or ["pod_ready", "rollout_complete", "restart_count_stable", "events_no_new_backoff"]),
        "risk": "high" if any((ACTION_CATALOG.get(action) or {}).get("risk") == "high" for action in action_types) else "medium",
        "rollback": "；".join(dict.fromkeys(str(item.get("rollback") or "").strip() for item in plan.get("changes") or [] if isinstance(item, dict) and item.get("rollback")))[:1800],
        "owner": str(plan.get("_operator") or "local-operator"),
        "enabled": False,
        "lifecycle": "candidate",
        "execution_ready": True,
    }
    return OPS_SKILL_REGISTRY.upsert(candidate, actor=str(plan.get("_operator") or "learning-loop"))


def _enrich_plan_change_policies(plan: dict) -> dict:
    """用服务端动作目录补齐风险字段，避免前端漏掉高风险二次确认。"""
    if not isinstance(plan, dict):
        return plan
    enriched = []
    for raw in plan.get("changes") or []:
        change = dict(raw) if isinstance(raw, dict) else {"type": str(raw)}
        action = "patch_workload" if change.get("type") == "patch" else str(change.get("type") or "")
        policy = ACTION_CATALOG.get(action)
        if policy:
            change["type"] = action
            explicit_high = str(change.get("risk") or "").lower() == "high"
            change["risk"] = "high" if explicit_high else policy["risk"]
            change["auto_allowed"] = False if explicit_high else policy["auto_allowed"]
            change["rollback"] = change.get("rollback") or policy["rollback"]
            change["requires_high_risk_confirmation"] = change["risk"] == "high"
        enriched.append(change)
    plan["changes"] = enriched
    plan["requires_high_risk_confirmation"] = any(
        change.get("risk") == "high" or change.get("auto_allowed") is False
        for change in enriched
    )
    return plan


def _collected_skill_evidence(signal: dict) -> set[str]:
    """Map a fresh evidence bundle to the stable evidence ids used by Skills."""
    evidence = signal.get("evidence") if isinstance(signal, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    collected = {
        str(item).strip()
        for item in (evidence.get("collected_evidence") or evidence.get("evidence_collected") or [])
        if str(item).strip()
    }
    logs = evidence.get("logs")
    if isinstance(logs, dict) and logs:
        pod_status_by_name = {
            str(item.get("name") or ""): item
            for item in ((evidence.get("pod") or {}).get("containers") or [])
            if isinstance(item, dict)
        }
        if any(
            isinstance(content, dict) and "current" in content and not content.get("current_error")
            for content in logs.values()
        ):
            collected.add("current_logs")
        if any(
            isinstance(content, dict)
            and "previous" in content
            and not content.get("previous_error")
            and (
                bool(content.get("previous"))
                or int((pod_status_by_name.get(str(name)) or {}).get("restart_count") or 0) > 0
            )
            for name, content in logs.items()
        ):
            collected.add("previous_logs")
    if "events" in evidence and isinstance(evidence.get("events"), list):
        collected.add("events")
    pod = evidence.get("pod") if isinstance(evidence.get("pod"), dict) else {}
    if pod.get("containers"):
        collected.add("last_state")
    if pod and "security_context" in pod:
        collected.add("pod_security_context")
    workload = evidence.get("workload") if isinstance(evidence.get("workload"), dict) else {}
    if workload:
        collected.update({"workload_spec", "recent_changes"})
    storage = evidence.get("storage") if isinstance(evidence.get("storage"), list) else []
    if storage:
        collected.add("storage_chain")
        if any(isinstance(item, dict) and ("pvc_phase" in item or item.get("missing")) for item in storage):
            collected.add("pvc_binding")
        if any(isinstance(item, dict) and "storage_class" in item for item in storage):
            collected.add("storage_class")
    else:
        workload_volumes = (((workload.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or []
        if workload_volumes and not any(
            isinstance(volume, dict) and volume.get("persistentVolumeClaim")
            for volume in workload_volumes
        ):
            # emptyDir/configMap/secret/projected volumes have no PVC/PV chain;
            # reading the live template is the complete storage-chain evidence.
            collected.add("storage_chain")
    if (
        "services" in evidence
        and isinstance(evidence.get("services"), list)
        and not any(isinstance(item, dict) and item.get("error") for item in evidence.get("services") or [])
    ):
        collected.add("service_endpoints")
    if evidence.get("node") and not (evidence.get("node") or {}).get("error"):
        collected.update({"node_conditions", "node_capacity", "node_pressure", "node_labels", "system_pods"})
    diagnostics = evidence.get("diagnostics") if isinstance(evidence.get("diagnostics"), dict) else {}
    for key in diagnostics:
        if diagnostics.get(key) is not None:
            collected.add(str(key))
    return collected


def _attach_operator_skills_to_plan(
    plan: dict,
    signal: dict,
    *,
    top_k: int = 3,
    preferred_skill_ids: list[str] | None = None,
    routing: dict | None = None,
) -> dict:
    """把实时证据匹配到可执行 Skill，并让 Skill 约束实际变更。"""
    if not isinstance(plan, dict):
        return plan
    _enrich_plan_change_policies(plan)
    result = OPS_SKILL_REGISTRY.match(signal, top_k=top_k)
    matches = [item for item in result.get("matches") or [] if float(item.get("confidence") or 0) >= 0.28]
    preferred_skill_ids = [str(item) for item in preferred_skill_ids or [] if str(item)]
    if preferred_skill_ids:
        by_id = {str((item.get("skill") or {}).get("id")): item for item in matches}
        registry_skills = {str(item.get("id")): item for item in OPS_SKILL_REGISTRY.list().get("skills") or []}
        for index, skill_id in enumerate(preferred_skill_ids):
            if skill_id not in by_id and skill_id in registry_skills and registry_skills[skill_id].get("enabled", True):
                skill = registry_skills[skill_id]
                by_id[skill_id] = {
                    "skill": skill,
                    "score": max(0.3, 0.9 - index * 0.08),
                    "confidence": max(0.55, 0.92 - index * 0.08),
                    "why": "批量 Skill Router 根据异常证据与适用对象选择。",
                }
        matches = sorted(
            by_id.values(),
            key=lambda item: (
                preferred_skill_ids.index(str((item.get("skill") or {}).get("id")))
                if str((item.get("skill") or {}).get("id")) in preferred_skill_ids else len(preferred_skill_ids),
                -float(item.get("confidence") or 0),
            ),
        )[:top_k]
    if not matches:
        plan.setdefault("operator_skills", [])
        if plan.get("changes") and _env_bool("SKILL_EXECUTION_REQUIRED", "true"):
            rejected_actions = [str(change.get("type") or "") for change in plan.get("changes") or []]
            plan["changes"] = []
            plan["decision"] = "matched_skill_required"
            plan["evidence_gap"] = f"没有匹配到可执行 Skill，已阻断动作：{', '.join(rejected_actions)}。"
        return plan
    collected_evidence = _collected_skill_evidence(signal)
    for match in matches:
        skill = match.get("skill") or {}
        required = [str(item).strip() for item in (skill.get("evidence_required") or []) if str(item).strip()]
        missing = [item for item in required if item not in collected_evidence]
        match["_evidence_collected"] = [item for item in required if item in collected_evidence]
        match["_evidence_missing"] = missing
        match["_execution_authorized"] = bool(skill.get("execution_ready")) and not missing
    plan["operator_skills"] = [_public_skill_match(item) for item in matches]
    plan["skill_evidence"] = {
        "collected": sorted(collected_evidence),
        "missing_by_skill": {
            str((match.get("skill") or {}).get("id") or "unknown"): match.get("_evidence_missing") or []
            for match in matches
            if match.get("_evidence_missing")
        },
    }
    existing_step_ids = {str(step.get("id") or step.get("title")) for step in plan.get("steps") or [] if isinstance(step, dict)}
    skill_steps = [
        step for step in OPS_SKILL_REGISTRY.steps_from_matches(matches, limit=2)
        if str(step.get("id") or step.get("title")) not in existing_step_ids
    ]
    if skill_steps:
        plan["skill_suggested_steps"] = skill_steps[:6]
    criteria = list(plan.get("success_criteria") or [])
    for match in matches[:2]:
        for item in ((match.get("skill") or {}).get("success_criteria") or []):
            if item not in criteria:
                criteria.append(item)
    plan["success_criteria"] = criteria
    execution_matches = [match for match in matches if match.get("_execution_authorized")]
    actions = sorted({
        action
        for match in execution_matches
        for action in ((match.get("skill") or {}).get("allowed_actions") or [])
        if action in ACTION_CATALOG
    })
    if actions:
        plan["skill_allowed_actions"] = actions
        owners = [
            str((match.get("skill") or {}).get("id"))
            for match in execution_matches
        ]
        for change in plan.get("changes") or []:
            change["skill_supported"] = str(change.get("type") or "") in actions
            change["selection_source"] = "matched_skill" if change["skill_supported"] else "evidence_engine_fallback"
            if change["skill_supported"]:
                change["skill_id"] = next(
                    (
                        str((match.get("skill") or {}).get("id"))
                        for match in execution_matches
                        if str(change.get("type") or "") in ((match.get("skill") or {}).get("allowed_actions") or [])
                    ),
                    owners[0] if owners else "",
                )
        supported_changes = [change for change in plan.get("changes") or [] if change.get("skill_supported")]
        if supported_changes:
            plan["changes"] = supported_changes
            plan["change_source"] = "dynamic_skill"
            plan["selected_skill_id"] = supported_changes[0].get("skill_id")
            plan["decision"] = "ready_for_approval"
        elif plan.get("changes") and _env_bool("SKILL_EXECUTION_REQUIRED", "true"):
            rejected_actions = [str(change.get("type") or "") for change in plan.get("changes") or []]
            plan["changes"] = []
            plan["decision"] = "skill_action_mapping_required"
            plan["evidence_gap"] = f"匹配到 Skill，但它未授权当前动作：{', '.join(rejected_actions)}。请修订候选 Skill 后再审批。"
    elif plan.get("changes") and _env_bool("SKILL_EXECUTION_REQUIRED", "true"):
        rejected_actions = [str(change.get("type") or "") for change in plan.get("changes") or []]
        rejected_action_set = set(rejected_actions)
        relevant_matches = [
            match for match in matches
            if rejected_action_set & {
                str(item) for item in ((match.get("skill") or {}).get("allowed_actions") or [])
            }
        ] or matches
        missing = sorted({
            str(item)
            for match in relevant_matches
            for item in (match.get("_evidence_missing") or [])
        })
        plan["changes"] = []
        plan["decision"] = "skill_evidence_required" if missing else "execution_ready_skill_required"
        plan["evidence_gap"] = (
            f"匹配到 Skill，但执行前仍缺少证据：{', '.join(missing)}；已阻断动作：{', '.join(rejected_actions)}。"
            if missing else
            f"匹配到的 Skill 仅可用于诊断，尚未发布为 execution-ready；已阻断动作：{', '.join(rejected_actions)}。"
        )
    script_candidates = []
    approved_scripts = {item["id"]: item for item in approved_script_catalog() if item.get("enabled", True)}
    for match in matches[:2]:
        skill = match.get("skill") or {}
        policy = skill.get("script_policy") or {}
        script = approved_scripts.get(str(policy.get("script_id") or ""))
        if not policy.get("enabled") or not script:
            continue
        script_candidates.append({
            "skill_id": skill.get("id"),
            "skill_name": skill.get("name"),
            "script_id": script["id"],
            "script_name": script.get("name"),
            "description": script.get("description"),
            "risk": script.get("risk", "high"),
            "trigger_conditions": policy.get("trigger_conditions") or [],
            "trigger_description": policy.get("trigger_description"),
            "timeout_seconds": policy.get("timeout_seconds", 120),
            "status": "pending_evidence_and_confirmation",
            "requires_confirmation": True,
        })
    if script_candidates:
        plan["skill_script_candidates"] = script_candidates
    if routing:
        plan["skill_routing"] = _redact_sensitive(routing)
    plan["planning_engine"] = "DynamicSREPlanner/v4 + ExecutableSkillRouter/v3 (AgentSkillRouter/v2 compatible) + SkillMemory + ApprovalGate"
    plan["skill_match_policy"] = "AI 基于实时证据生成方案；动态匹配 Skill 校验证据、授权动作并定义恢复判据；任何变更仍需人工审批。"
    return plan


def _attach_operator_skills_to_chat(req: ChatRequest, data: dict) -> dict:
    """SRE 对话返回前注入匹配到的运维 Skill。"""
    if not isinstance(data, dict):
        return data
    raw = data.get("raw") or {}
    if raw.get("mode") == "general_chat":
        return data
    diagnosis = raw.get("diagnosis") or {}
    if not isinstance(diagnosis, dict):
        return data
    alert = raw.get("alert") or {
        "cluster": req.cluster,
        "cluster_id": req.cluster_id,
        "namespace": req.namespace,
        "workload_name": req.deployment,
        "workload_type": req.workload_type,
    }
    diagnosis_evidence = diagnosis.get("evidence") if isinstance(diagnosis.get("evidence"), dict) else {}
    k8s_context = raw.get("k8s_context") if isinstance(raw.get("k8s_context"), dict) else {}
    deep_diagnostics = k8s_context.get("diagnostics") if isinstance(k8s_context.get("diagnostics"), dict) else {}
    live_evidence = {
        **diagnosis_evidence,
        **k8s_context,
        "pod": k8s_context.get("pod") or diagnosis_evidence.get("pod") or {},
        "logs": k8s_context.get("logs") or diagnosis_evidence.get("logs") or {},
        "events": (
            (k8s_context.get("events") or {}).get("events")
            if isinstance(k8s_context.get("events"), dict) else k8s_context.get("events")
        ) or diagnosis_evidence.get("events") or [],
        "workload": k8s_context.get("workload") or diagnosis_evidence.get("workload") or {},
        "storage": deep_diagnostics.get("storage") or diagnosis_evidence.get("storage") or [],
        "services": deep_diagnostics.get("services") or diagnosis_evidence.get("services") or [],
        "node": deep_diagnostics.get("node") or diagnosis_evidence.get("node") or {},
        "log_errors": deep_diagnostics.get("log_errors") or diagnosis_evidence.get("log_errors") or {},
        "diagnostics": deep_diagnostics or diagnosis_evidence.get("diagnostics") or {},
    }
    signal = _skill_signal_payload(
        question=req.message,
        alert=alert,
        diagnosis=diagnosis,
        evidence=live_evidence,
        plan=diagnosis.get("remediation_plan") or {},
    )
    plan = diagnosis.get("remediation_plan")
    if isinstance(plan, dict):
        context_pod = live_evidence.get("pod") if isinstance(live_evidence.get("pod"), dict) else {}
        context_workload = context_pod.get("workload") if isinstance(context_pod.get("workload"), dict) else {}
        context_workload_name = str(context_pod.get("workload_name") or context_workload.get("name") or "")
        context_workload_type = str(context_pod.get("workload_kind") or context_workload.get("kind") or req.workload_type or "Deployment")
        if k8s_context.get("source") in {"rancher", "kubeconfig"}:
            if k8s_context.get("cluster"):
                plan["cluster"] = k8s_context["cluster"]
            if k8s_context.get("cluster_id"):
                plan["cluster_id"] = k8s_context["cluster_id"]
            plan["source"] = k8s_context["source"]
            if context_pod.get("namespace"):
                plan["namespace"] = context_pod["namespace"]
            if context_pod.get("name"):
                plan["pod_name"] = context_pod["name"]
            if context_workload_name:
                plan["target"] = f"{context_workload_type}/{context_workload_name}"
                for change in plan.get("changes") or []:
                    if isinstance(change, dict):
                        change["namespace"] = plan.get("namespace") or change.get("namespace")
                        if str(change.get("type") or "") in {
                            "restart", "patch_workload", "patch_workload_volume",
                            "patch_workload_runtime_security", "patch", "scale_out", "rollback_workload",
                        }:
                            change["workload_type"] = context_workload_type
                            change["workload_name"] = context_workload_name
        plan["evidence"] = {
            **(plan.get("evidence") or {}),
            **live_evidence,
        }
        diagnosis["remediation_plan"] = _attach_operator_skills_to_plan(plan, signal)
        diagnosis["operator_skills"] = diagnosis["remediation_plan"].get("operator_skills", [])
    return data


def _inspection_skill_signal(
    finding: dict,
    *,
    evidence_override: dict | None = None,
    plan_override: dict | None = None,
) -> dict:
    workload = finding.get("workload") or {}
    evidence = evidence_override if isinstance(evidence_override, dict) else finding.get("evidence") or {}
    return _skill_signal_payload(
        question=" ".join([
            str(finding.get("title") or ""),
            str(finding.get("summary") or ""),
            str(evidence.get("state_text") or ""),
        ]),
        alert={
            "category": finding.get("category"),
            "severity": finding.get("severity"),
            "cluster": finding.get("cluster"),
            "namespace": finding.get("namespace"),
            "workload_type": workload.get("kind"),
            "workload_name": workload.get("name") or finding.get("name"),
        },
        diagnosis={"root_cause": finding.get("summary"), "signals": evidence.get("events") or []},
        evidence=evidence,
        plan=plan_override if isinstance(plan_override, dict) else finding.get("ops_plan") or {},
    )


async def _route_inspection_findings_with_skills(payload: dict, model_profile_id: str = "") -> dict:
    """批量选择运维 Skill，并把 Skill 真正写入巡检计划而非只做展示标签。"""
    findings = [item for item in payload.get("findings") or [] if isinstance(item, dict)]
    skill_items = [item for item in OPS_SKILL_REGISTRY.list().get("skills") or [] if item.get("enabled", True)]
    if not findings or not skill_items:
        return payload

    deterministic: dict[str, list[str]] = {}
    for finding in findings:
        matches = OPS_SKILL_REGISTRY.match(_inspection_skill_signal(finding), top_k=3).get("matches") or []
        deterministic[str(finding.get("id"))] = [
            str((match.get("skill") or {}).get("id"))
            for match in matches
            if float(match.get("confidence") or 0) >= 0.28
        ]

    llm_routes: dict[str, dict] = {}
    router_error = ""
    if _env_bool("INSPECTION_SKILL_ROUTER_ENABLED", "true"):
        skill_catalog = [{
            "id": item.get("id"),
            "description": item.get("description") or item.get("summary"),
            "symptoms": item.get("symptoms") or [],
            "applies_to": item.get("applies_to") or [],
            "evidence_required": item.get("evidence_required") or [],
            "allowed_actions": item.get("allowed_actions") or [],
        } for item in skill_items[:40]]
        finding_catalog = [{
            "id": item.get("id"),
            "category": item.get("category"),
            "severity": item.get("severity"),
            "title": item.get("title"),
            "summary": item.get("summary"),
            "target": {
                "kind": (item.get("workload") or {}).get("kind"),
                "name": (item.get("workload") or {}).get("name") or item.get("name"),
                "namespace": item.get("namespace"),
            },
            "event_signals": [
                {"reason": event.get("reason"), "message": _clip_text(event.get("message"), 240)}
                for event in ((item.get("evidence") or {}).get("events") or [])[:5]
                if isinstance(event, dict)
            ],
            "deterministic_candidates": deterministic.get(str(item.get("id")), []),
        } for item in findings[:24]]

        router_trace = start_trace(
            "flawless.inspection.skill_router",
            trace_id=new_trace_id("skill-router"),
            user_id="inspection-engine",
            session_id=f"inspection-skills:{payload.get('timestamp') or datetime.now(timezone.utc).isoformat()}",
            input={"findings": finding_catalog, "skill_count": len(skill_catalog)},
            metadata={"model_profile_id": model_profile_id, "finding_count": len(finding_catalog)},
            tags=["flawless", "inspection", "skill-router"],
        )
        router_generation = start_generation(
            router_trace,
            "inspection_skill_routing",
            model=model_profile_id or os.getenv("LLM_MODEL", ""),
            input={"finding_count": len(finding_catalog), "skill_count": len(skill_catalog)},
            prompt_name="flawless.inspection.skill_router.v2",
        )

        def call_router() -> tuple[dict, dict]:
            from agents.llm_client import get_llm
            llm = get_llm(temperature=0.0, max_tokens=1400, profile_id=model_profile_id or None)
            prompt = (
                "你是企业 AIOps 的运维 Skill 路由器。根据真实异常摘要、对象类型和事件证据，"
                "为每个 finding 选择最多 3 个最相关的 Skill。只能选择目录中已有 id，不得生成动作、命令或新 Skill。"
                "优先选择能覆盖根因证据、对象类型和恢复判据的 Skill；证据不足时保留确定性候选并降低 confidence。"
                "只返回 JSON：{routes:[{finding_id,skill_ids,confidence,rationale}]}。\n"
                f"Skill目录={json.dumps(_redact_sensitive(skill_catalog), ensure_ascii=False)[:14000]}\n"
                f"巡检异常={json.dumps(_redact_sensitive(finding_catalog), ensure_ascii=False)[:18000]}"
            )
            response = llm.invoke(prompt)
            usage = ((getattr(response, "response_metadata", {}) or {}).get("token_usage") or {})
            return _extract_json_object(getattr(response, "content", str(response))), usage

        try:
            routed, router_usage = await asyncio.wait_for(
                asyncio.to_thread(call_router),
                timeout=float(os.getenv("INSPECTION_SKILL_ROUTER_TIMEOUT_SECONDS", "18")),
            )
            valid_ids = {str(item.get("id")) for item in skill_items}
            for route in routed.get("routes") or []:
                finding_id = str(route.get("finding_id") or "")
                selected = [str(item) for item in route.get("skill_ids") or [] if str(item) in valid_ids][:3]
                if finding_id and selected:
                    llm_routes[finding_id] = {
                        "skill_ids": selected,
                        "confidence": max(0.0, min(1.0, float(route.get("confidence") or 0.0))),
                        "rationale": _clip_text(route.get("rationale"), 500),
                    }
            end_observation(router_generation, output={"routes": list(llm_routes.values())}, usage=router_usage)
            score_observation(
                router_trace,
                name="inspection.skill_route_coverage",
                value=min(1.0, len(llm_routes) / max(1, len(finding_catalog))),
                comment="Share of inspected findings routed by the constrained LLM Skill router.",
            )
            update_trace(router_trace, output={"routed": len(llm_routes), "findings": len(finding_catalog)})
            flush_observability()
        except Exception as exc:
            router_error = f"{type(exc).__name__}: {_redact_text(str(exc))}"
            end_observation(router_generation, output={"fallback": "semantic_match"}, status_message=router_error, level="ERROR")
            update_trace(router_trace, output={"fallback": True, "error": router_error})
            flush_observability()

    routed_count = 0
    for finding in findings:
        finding_id = str(finding.get("id") or "")
        llm_route = llm_routes.get(finding_id)
        preferred = (llm_route or {}).get("skill_ids") or deterministic.get(finding_id, [])
        if not preferred:
            continue
        routing = {
            "engine": "AgentSkillRouter/v2",
            "source": "llm+semantic_match" if llm_route else "semantic_match_fallback",
            "selected_skill_ids": preferred,
            "confidence": (llm_route or {}).get("confidence"),
            "rationale": (llm_route or {}).get("rationale") or "根据症状、对象类型、证据字段和 Skill 描述匹配。",
            "router_error": router_error or None,
        }
        plan = finding.get("ops_plan") or _ops_plan_from_finding(finding)
        plan = _attach_operator_skills_to_plan(
            plan,
            _inspection_skill_signal(finding),
            top_k=3,
            preferred_skill_ids=preferred,
            routing=routing,
        )
        finding["ops_plan"] = plan
        finding["matched_skills"] = plan.get("operator_skills") or []
        finding["skill_routing"] = routing
        routed_count += 1

    payload.setdefault("summary", {})["skill_routed"] = routed_count
    payload["skill_router"] = {
        "engine": "AgentSkillRouter/v2",
        "llm_routed": len(llm_routes),
        "semantic_fallback": routed_count - len(llm_routes),
        "error": router_error or None,
    }
    return payload


async def list_ops_skills():
    return OPS_SKILL_REGISTRY.list()


async def import_ops_skill_package(request: Request, file: UploadFile = File(...)):
    """导入 Agent Skills 标准 ZIP；包内脚本不会自动获得执行信任。"""
    filename = Path(file.filename or "skill.zip").name
    content = await file.read(MAX_PACKAGE_BYTES + 1)
    if len(content) > MAX_PACKAGE_BYTES:
        raise HTTPException(status_code=413, detail=f"Skill 包不能超过 {MAX_PACKAGE_BYTES // 1024 // 1024} MiB")
    try:
        imported = OPS_SKILL_REGISTRY.import_packages(
            filename,
            content,
            actor=_request_actor(request),
            supported_actions=set(ACTION_CATALOG),
        )
    except AgentSkillPackageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for skill in imported:
        _audit_event(
            "aiops.skill.import",
            _request_actor(request),
            skill["id"],
            "accepted",
            version=skill.get("version"),
            execution_ready=skill.get("execution_ready"),
            unsupported_actions=skill.get("unsupported_actions") or [],
        )
    return {
        "status": "ok",
        "imported": imported,
        "message": f"已导入 {len(imported)} 个标准 Agent Skill；包内脚本默认不受信任。",
    }


async def export_ops_skill_package(skill_id: str):
    """导出可迁移到 Codex、Claude Code 等兼容智能体的标准 ZIP。"""
    try:
        filename, content = OPS_SKILL_REGISTRY.export_package(skill_id)
    except AgentSkillPackageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return StreamingResponse(
        iter([content]),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def upsert_ops_skill(req: OpsSkillDefinition, request: Request):
    allowed = set(ACTION_CATALOG.keys())
    unknown = sorted(set(req.allowed_actions or []) - allowed)
    if unknown:
        raise HTTPException(status_code=422, detail={
            "message": "Skill 的 allowed_actions 必须映射到平台受控动作目录，不能注入任意命令。",
            "unsupported_actions": unknown,
            "allowed_actions": sorted(allowed),
        })
    script_policy = req.script_policy
    if script_policy.enabled:
        approved_scripts = {item["id"]: item for item in approved_script_catalog() if item.get("enabled", True)}
        if script_policy.script_id not in approved_scripts:
            raise HTTPException(status_code=422, detail={
                "message": "脚本未进入企业批准目录，不能由 Skill 引用。",
                "script_id": script_policy.script_id,
                "approved_script_ids": sorted(approved_scripts),
                "configuration": "请在 ConfigMap 的 OPS_APPROVED_SCRIPTS_JSON 中登记脚本元数据，不要把脚本正文写入 Skill。",
            })
        allowed_triggers = {item["id"] for item in skill_option_catalog()["script_triggers"]}
        unsupported_triggers = sorted(set(script_policy.trigger_conditions) - allowed_triggers)
        if unsupported_triggers:
            raise HTTPException(status_code=422, detail={
                "message": "脚本触发条件必须从平台受控目录选择。",
                "unsupported_triggers": unsupported_triggers,
                "allowed_triggers": sorted(allowed_triggers),
            })
    try:
        skill = OPS_SKILL_REGISTRY.upsert(req.model_dump(), actor=_request_actor(request))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    _audit_event("aiops.skill.upsert", _request_actor(request), skill["id"], "accepted", category=skill.get("category"), risk=skill.get("risk"))
    return {"status": "ok", "skill": skill}


async def delete_ops_skill(skill_id: str, request: Request):
    result = OPS_SKILL_REGISTRY.delete(skill_id, actor=_request_actor(request))
    _audit_event("aiops.skill.delete", _request_actor(request), skill_id, result.get("status", "unknown"))
    return result


async def match_ops_skills(req: OpsSkillMatchRequest):
    return OPS_SKILL_REGISTRY.match({
        "question": req.question,
        "alert": req.alert,
        "evidence": req.evidence,
        "cluster": req.cluster,
        "namespace": req.namespace,
        "workload": req.workload,
    }, top_k=req.top_k)


INFRASTRUCTURE_ACTION_PREFIXES = ("db_", "vm_", "middleware_", "storage_", "cloud_", "infra_")


def _is_infrastructure_action(action: str) -> bool:
    return str(action or "").startswith(INFRASTRUCTURE_ACTION_PREFIXES)


def _infrastructure_target_bound(plan: dict) -> bool:
    if plan.get("resource_id") or plan.get("resource_type"):
        return True
    for change in plan.get("changes") or []:
        if isinstance(change, dict) and (change.get("resource_id") or change.get("resource_type")):
            return True
    target = str(plan.get("target") or "")
    return any(target.startswith(prefix) for prefix in ("database/", "virtual_machine/", "middleware/", "storage/", "cloud_service/"))


async def _execute_infrastructure_action(change: dict, plan: dict) -> dict:
    """把非 K8s 变更交给企业受控执行器，不接受 LLM 生成的 shell/SQL。"""
    webhook = os.getenv("INFRASTRUCTURE_ACTION_WEBHOOK_URL", "").strip()
    if not webhook:
        return {
            "error": "INFRASTRUCTURE_ACTION_WEBHOOK_URL 未配置，数据库/虚拟机变更不会假执行。",
            "do_this": [
                "准备一个企业受控执行器，例如堡垒机、DBA 平台、Ansible/AWX、SaltStack、云管平台或工单自动化服务。",
                "执行器只接受动作目录中的 action id、resource_id 和结构化参数，不接受任意 shell 或 SQL。",
                "在 k8s-agent-config 中配置 INFRASTRUCTURE_ACTION_WEBHOOK_URL，并让执行器返回 status、audit_id、evidence 和 rollback_hint。",
            ],
            "action": change.get("type"),
            "resource_id": change.get("resource_id"),
        }
    payload = {
        "action": change.get("type"),
        "resource": {
            "id": change.get("resource_id") or plan.get("resource_id"),
            "type": change.get("resource_type") or plan.get("resource_type"),
            "name": change.get("resource_name") or plan.get("resource_name"),
            "provider": change.get("provider") or plan.get("provider"),
        },
        "parameters": redact_infrastructure_sensitive(change.get("parameters") or change.get("patch") or {}),
        "reason": change.get("reason") or plan.get("summary"),
        "risk": change.get("risk"),
        "rollback": change.get("rollback"),
        "operator": plan.get("_operator") or "unknown",
        "plan_id": plan.get("id"),
        "evidence": redact_infrastructure_sensitive(plan.get("evidence") or {}),
        "confirmation": {
            "human_confirmed": bool(plan.get("high_risk_confirmed") or change.get("human_approved")),
            "stepwise_confirmation": bool(plan.get("stepwise_confirmation")),
        },
    }
    try:
        async with _client(int(os.getenv("INFRASTRUCTURE_ACTION_TIMEOUT_SECONDS", "45"))) as client:
            response = await client.post(webhook, json=payload, headers={**_internal_headers(), "Content-Type": "application/json"})
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return {
            "error": f"基础设施执行器调用失败：{type(exc).__name__}: {_redact_text(str(exc))}",
            "action": change.get("type"),
            "resource_id": change.get("resource_id"),
        }
    return {
        "status": data.get("status") or "submitted",
        "audit_id": data.get("audit_id") or data.get("ticket_id") or data.get("job_id"),
        "executor": data.get("executor") or "infrastructure-action-webhook",
        "message": data.get("message") or "基础设施动作已提交到受控执行器。",
        "evidence": _redact_sensitive(data.get("evidence") or {}),
        "rollback_hint": data.get("rollback_hint") or data.get("rollback"),
        "raw": _redact_sensitive(data),
    }


async def list_infrastructure_resources(resource_type: str = "all"):
    payload = infrastructure_providers_payload()
    if resource_type not in {"", "all", "*"}:
        payload["resources"] = [item for item in payload.get("resources") or [] if item.get("type") == resource_type]
    return payload


async def infrastructure_providers():
    return infrastructure_providers_payload()


async def _enrich_infrastructure_findings_with_llm(payload: dict, model_profile_id: str = "") -> dict:
    findings = [item for item in payload.get("findings") or [] if isinstance(item, dict)]
    if not findings or not _env_bool("INFRASTRUCTURE_LLM_PLANNER_ENABLED", "true"):
        return payload
    action_catalog = [
        {"id": key, "risk": value.get("risk"), "description": value.get("description")}
        for key, value in ACTION_CATALOG.items()
        if _is_infrastructure_action(key)
    ]
    prompt = (
        "你是顶尖全栈基础设施 SRE。根据数据库/虚拟机/中间件的真实证据，为每个 finding 生成更专业的诊断预演。"
        "只能选择 action_catalog 中已有动作；不得编造 shell、SQL、凭据、路径或执行成功结果。"
        "若证据不足，保留诊断步骤并说明还需要什么证据。"
        "只返回 JSON：{plans:[{finding_id,root_cause,operator_summary,steps:[{title,description}],changes:[{type,reason,parameters}],success_criteria:[...]}]}。\n"
        f"action_catalog={json.dumps(action_catalog, ensure_ascii=False)}\n"
        f"findings={json.dumps(_redact_sensitive(findings[:6]), ensure_ascii=False)[:14000]}"
    )
    try:
        from agents.llm_client import get_llm
        llm = get_llm(temperature=0.05, max_tokens=1800, profile_id=model_profile_id or None)
        response = await asyncio.to_thread(lambda: llm.invoke(prompt))
        parsed = _extract_json_object(getattr(response, "content", str(response)))
    except Exception as exc:
        payload["llm_planner_error"] = f"{type(exc).__name__}: {_redact_text(str(exc))}"
        return payload
    plans = {
        str(item.get("finding_id")): item
        for item in parsed.get("plans") or []
        if isinstance(item, dict) and item.get("finding_id")
    }
    for finding in findings:
        plan_update = plans.get(str(finding.get("id")))
        base_plan = finding.get("ops_plan") or {}
        if not plan_update or not isinstance(base_plan, dict):
            continue
        if plan_update.get("operator_summary"):
            base_plan["summary"] = str(plan_update["operator_summary"])[:1800]
        if plan_update.get("root_cause"):
            base_plan.setdefault("root_cause_hypotheses", [])
            base_plan["root_cause_hypotheses"] = [str(plan_update["root_cause"])[:1200]]
        steps = []
        for index, item in enumerate(plan_update.get("steps") or [], start=1):
            if isinstance(item, dict) and (item.get("title") or item.get("description")):
                steps.append({
                    "id": f"llm-infra-step-{index}",
                    "title": str(item.get("title") or f"AI 诊断步骤 {index}")[:120],
                    "description": str(item.get("description") or "")[:1200],
                    "status": "pending",
                    "source": "llm_infrastructure_planner",
                })
        if steps:
            base_plan["steps"] = steps + list(base_plan.get("steps") or [])
        changes = []
        for raw in plan_update.get("changes") or []:
            if not isinstance(raw, dict):
                continue
            action = str(raw.get("type") or "").strip()
            if action not in ACTION_CATALOG or not _is_infrastructure_action(action):
                continue
            changes.append({
                **(raw if isinstance(raw, dict) else {}),
                "type": action,
                "resource_id": finding.get("resource_id"),
                "resource_type": finding.get("resource_type"),
                "resource_name": ((finding.get("resource") or {}).get("name") or finding.get("resource_id")),
                "requires_external_executor": True,
                "reason": str(raw.get("reason") or plan_update.get("root_cause") or finding.get("summary") or "")[:1200],
            })
        if changes:
            base_plan["changes"] = changes[:2]
        if plan_update.get("success_criteria"):
            base_plan["success_criteria"] = [str(item) for item in plan_update.get("success_criteria") or [] if str(item)][:8]
        finding["ops_plan"] = _enrich_plan_change_policies(base_plan)
    payload["llm_planner"] = {"engine": "InfrastructureSREPlanner/v1", "plans": len(plans)}
    return payload


async def scan_infrastructure_resources(req: InfrastructureScanRequest):
    payload = await scan_infrastructure_provider_resources(req.resource_type, req.resource_id, include_probe=req.include_probe)
    payload = await _enrich_infrastructure_findings_with_llm(payload, req.model_profile_id)
    payload = await _route_inspection_findings_with_skills(payload, req.model_profile_id)
    payload["effectiveness"] = record_inspection(
        req.resource_type,
        req.resource_id or "all",
        payload,
        model_id=req.model_profile_id or get_active_model_profile_id() or os.getenv("LLM_MODEL", "default"),
    )
    payload["scope"] = {
        "resource_type": req.resource_type,
        "resource_id": req.resource_id,
        "production_mode": req.production_mode,
    }
    return payload


async def ops_capabilities():
    skills = OPS_SKILL_REGISTRY.list()
    return {
        "status": "ok",
        "planner": "EvidenceRunbookEngine/v1 + InfrastructureSREPlanner/v1 + constrained LLM replanner",
        "actions": action_catalog_payload(),
        "skill_options": skill_option_catalog(),
        "approved_scripts": approved_script_catalog(),
        "infrastructure": infrastructure_providers_payload().get("summary") or {},
        "operator_skills": {
            "total": (skills.get("summary") or {}).get("total", 0),
            "enabled": (skills.get("summary") or {}).get("enabled", 0),
            "custom": (skills.get("summary") or {}).get("custom", 0),
            "writable": skills.get("writable"),
        },
        "controls": {
            "arbitrary_shell": False,
            "human_confirmation": True,
            "high_risk_second_confirmation": True,
            "per_change_approval": True,
            "autonomous_max_risk": "medium",
            "namespace_allowlist": _csv_env("ALLOWED_NAMESPACES", "default"),
            "verification_timeout_seconds": int(os.getenv("OPS_VERIFY_TIMEOUT_SECONDS", "45")),
            "continuous_until_recovered": True,
            "strategy_summary_window": max(1, min(20, int(os.getenv("AUTO_OPS_MAX_ATTEMPTS", "3")))),
            "continuation_interval_seconds": max(5, min(900, int(os.getenv("OPS_CONTINUATION_INTERVAL_SECONDS", "30")))),
            "job_store": {
                "configured": OPS_JOB_STORE_PATH is not None,
                "path": str(OPS_JOB_STORE_PATH or ""),
                "error": OPS_JOB_STORE_ERROR or None,
                "restored_jobs": len(OPS_JOBS),
                "multi_replica_safe": False,
                "deployment_requirement": "API replicas must remain 1 until a distributed execution lease is configured.",
            },
        },
        "diagnostic_evidence": [
            "current/previous container logs", "Pod Events and last termination", "live workload template",
            "Service/Endpoint", "PVC/PV/StorageClass", "node conditions and capacity", "Rancher cluster context",
            "database connectivity/slow SQL/locks/replication/capacity/backup",
            "VM agent/system metrics/service status/disk usage/system logs/snapshot state",
        ],
    }


def _public_ops_job(job: dict) -> dict:
    return _redact_sensitive({k: v for k, v in job.items() if k != "plan"})


async def _append_ops_job_event(job_id: str, stage: str, message: str, **values):
    async with OPS_JOBS_LOCK:
        job = OPS_JOBS.get(job_id)
        if not job:
            return
        update_stage = values.pop("_update_stage", True)
        now = datetime.now(timezone.utc).isoformat()
        event = {
            "timestamp": now,
            "stage": stage,
            "message": message,
            "level": values.pop("level", "info"),
        }
        event.update(_redact_sensitive(values))
        events = job.setdefault("events", [])
        events.append(event)
        del events[:-240]
        job.update(values)
        if update_stage:
            job["stage"] = stage
            job["message"] = message
        job["updated_at"] = now
        _persist_ops_jobs()


async def _update_ops_job(job_id: str, **values):
    async with OPS_JOBS_LOCK:
        job = OPS_JOBS.get(job_id)
        if not job:
            return
        job.update(values)
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        _persist_ops_jobs()


def _ensure_effectiveness_record(plan: dict, result: dict) -> dict:
    if not isinstance(result, dict) or result.get("effectiveness"):
        return result
    try:
        result["effectiveness"] = record_remediation(
            plan,
            result,
            model_id=plan.get("model_profile_id") or get_active_model_profile_id() or os.getenv("LLM_MODEL", "default"),
        )
    except Exception as exc:
        result["effectiveness_error"] = f"{type(exc).__name__}: {_redact_text(str(exc))}"
    return result


def _continuous_diagnostic_plan(
    plan: dict,
    result: dict,
    *,
    cycle: int,
) -> dict:
    """Build the next read-only incident cycle without pretending the incident ended.

    A continuous diagnostic cycle is intentionally a new strategy identity: the
    evidence is time-dependent even when the probe catalog is unchanged.  It can
    discover a newly created Pod, a completed rollout, an administrator-side
    permission fix, or a different failure signature.  It never carries an old
    mutation approval into the next cycle.
    """
    namespace, workload_type, workload_name = _workload_identity_from_plan(plan)
    target = f"{workload_type}/{workload_name}" if workload_name else str(plan.get("target") or namespace)
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    continuation = result.get("continuation_context") if isinstance(result.get("continuation_context"), dict) else {}
    next_plan = copy.deepcopy(plan)
    next_plan.update({
        "id": f"continuous-diagnosis-{cycle}-{uuid.uuid4().hex[:8]}",
        "title": f"持续恢复复检：第 {cycle} 轮",
        "namespace": namespace,
        "target": target,
        "summary": str(
            verification.get("message")
            or result.get("message")
            or "目标尚未取得恢复证据，继续采集实时状态并生成下一修复方案。"
        ),
        "steps": [
            {"id": f"continuous-health-{cycle}", "title": "复核 Workload 与 Pod 健康", "description": "读取 rollout、Ready、重启次数和容器状态。", "status": "pending"},
            {"id": f"continuous-events-{cycle}", "title": "刷新失败证据", "description": "重新读取 current/previous logs、Events、存储、Service/Endpoint 与节点状态。", "status": "pending"},
            {"id": f"continuous-replan-{cycle}", "title": "重新匹配 Skill 并规划", "description": "根据新增证据生成不同的安全方案；有变更时重新等待人工逐项审批。", "status": "pending"},
        ],
        "changes": [],
        "source": "continuous_recovery_loop",
        "strategy_source": "continuous_recovery_loop",
        "requires_confirmation": False,
        "requires_high_risk_confirmation": False,
        "stepwise_confirmation": False,
        "high_risk_confirmed": False,
        "operator_force_execute": False,
        "operator_override_reason": "",
        "continuation_context": copy.deepcopy(continuation),
        "_continuous_cycle": cycle,
        "_last_failure": copy.deepcopy(continuation.get("last_failure") or plan.get("_last_failure") or {}),
        "verification_plan": _next_attempt_verification_plan(target),
    })
    next_plan.pop("pending_approval", None)
    next_plan.pop("approval_receipt", None)
    for change in next_plan.get("changes") or []:
        if isinstance(change, dict):
            change.pop("human_approved", None)
            change.pop("operator_confirmed", None)
            change.pop("approval_receipt", None)
    return _apply_ops_continuation_context(next_plan)


async def _wait_for_continuous_recheck(
    cancel_event: asyncio.Event,
    progress,
    *,
    cycle: int,
) -> bool:
    interval = max(5, min(900, int(os.getenv("OPS_CONTINUATION_INTERVAL_SECONDS", "30"))))
    await progress(
        "continuation_wait",
        f"故障尚未验证恢复；{interval} 秒后自动进入第 {cycle} 轮实时复检。",
        status="running",
        continuous_until_recovered=True,
        next_cycle=cycle,
        recheck_interval_seconds=interval,
        level="warning",
    )
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=interval)
        return False
    except asyncio.TimeoutError:
        return True


async def _run_ops_job(job_id: str, initial_plan: dict, autonomous: bool, cancel_event: asyncio.Event):
    current = _apply_ops_continuation_context(copy.deepcopy(initial_plan))
    attempted: set[str] = {
        str(value) for value in (current.get("_attempted_strategy_fingerprints") or []) if value
    }
    history: list[dict] = []
    attempt_offset = int(current.get("_prior_attempt_count") or len(current.get("_prior_attempts") or []))
    strategy_window = max(1, min(20, int(os.getenv("AUTO_OPS_MAX_ATTEMPTS", "3"))))

    async def progress(stage: str, message: str, **extra):
        await _append_ops_job_event(job_id, stage, message, **extra)

    async def await_change_approval(index: int, total: int, change: dict, change_target: str) -> bool:
        approval_event = asyncio.Event()
        OPS_JOB_STEP_APPROVAL_EVENTS[job_id] = approval_event
        approval_id = f"approve-{uuid.uuid4().hex}"
        change_fingerprint = _change_item_fingerprint(change)
        pending = {
            "approval_id": approval_id,
            "change_fingerprint": change_fingerprint,
            "strategy_fingerprint": _change_fingerprint(current),
            "lineage_attempt": int((OPS_JOBS.get(job_id) or {}).get("lineage_attempt") or 0),
            "change_index": index,
            "changes_total": total,
            "action": change.get("type") or "change",
            "target": change_target,
            "risk": change.get("risk") or "medium",
            "reason": change.get("reason") or "等待操作员核对本步骤。",
            "rollback": change.get("rollback") or "按变更前快照恢复。",
            "patch": change.get("patch"),
            "manifest": change.get("manifest"),
            "command": change.get("command"),
            "timeout_seconds": change.get("timeout_seconds"),
            "namespace": change.get("namespace") or current.get("namespace"),
            "pod_name": change.get("pod_name"),
            "container_name": change.get("container_name"),
            "node_name": change.get("node_name"),
            "api_version": change.get("api_version") or change.get("apiVersion"),
            "kind": change.get("kind"),
            "name": change.get("name"),
        }
        await _append_ops_job_event(
            job_id,
            "awaiting_change_approval",
            f"第 {index}/{total} 项变更已就绪，等待操作员逐步确认：{change.get('type')} -> {change_target}",
            status="awaiting_approval",
            pending_approval=pending,
            level="warning",
        )
        # 默认无限期等待人工批准。事故不会因为操作员暂时离开页面而被误写成
        # cancelled/unresolved；如需组织级超时，可显式设置正数秒数。
        timeout_seconds = max(0, int(os.getenv("OPS_STEP_APPROVAL_TIMEOUT_SECONDS", "0")))
        try:
            if timeout_seconds:
                await asyncio.wait_for(approval_event.wait(), timeout=timeout_seconds)
            else:
                await approval_event.wait()
        except asyncio.TimeoutError:
            OPS_JOB_STEP_APPROVAL_EVENTS.pop(job_id, None)
            await _append_ops_job_event(
                job_id,
                "approval_timeout",
                f"第 {index}/{total} 项变更等待人工确认超过 {timeout_seconds} 秒，已停止提交后续变更。",
                status="cancelled",
                pending_approval=None,
                approved_change_index=0,
                level="warning",
            )
            return False
        OPS_JOB_STEP_APPROVAL_EVENTS.pop(job_id, None)
        if cancel_event.is_set():
            return False
        job = OPS_JOBS.get(job_id) or {}
        consumed = job.get("last_consumed_approval") or {}
        approved = bool(
            consumed.get("confirmed") is True
            and str(consumed.get("approval_id") or "") == approval_id
            and str(consumed.get("change_fingerprint") or "") == change_fingerprint
            and int(consumed.get("change_index") or 0) == index
            and int(job.get("approved_change_index") or 0) == index
        )
        if approved:
            # 只有与本轮动作指纹精确绑定、一次性消费的步骤审批，才能授权
            # 高风险执行器。任务级确认和上一轮批准都不能泄漏到这里。
            change["human_approved"] = True
            change["operator_confirmed"] = True
            change["approval_receipt"] = {
                "approval_id": approval_id,
                "change_fingerprint": change_fingerprint,
                "approved_by": job.get("approval_actor") or "operator",
                "approved_at": datetime.now(timezone.utc).isoformat(),
            }
            await _append_ops_job_event(
                job_id,
                "change_approved",
                f"操作员已确认第 {index}/{total} 项变更，开始提交受控变更执行器。",
                status="running",
                pending_approval=None,
                approved_change_index=index,
                level="success",
            )
        return approved

    try:
        await progress("starting", "运维任务已进入受控执行队列", status="running")
        attempt = 0
        continuous_cycle = int(current.get("_continuous_cycle") or 0)
        while not cancel_event.is_set():
            attempt += 1
            if cancel_event.is_set():
                break
            previous_actions = _history_action_types(history)
            current["_attempted_actions"] = sorted(previous_actions | set(current.get("_attempted_actions") or []))
            current["_attempted_strategy_fingerprints"] = sorted(attempted)
            current["_attempted_change_fingerprints"] = sorted(
                _history_change_fingerprints(history)
                | {str(value) for value in (current.get("_attempted_change_fingerprints") or []) if value}
            )
            fingerprint = _change_fingerprint(current)
            if fingerprint in attempted:
                result = _append_manual_exit_if_needed(
                    current,
                    {"status": "unresolved", "executed": False, "history": history, "alternative_plans": []},
                    "系统检测到下一步与历史失败策略完全相同。不会重复提交该变更，将继续实时取证并等待新的差异化方案。",
                    attempted,
                )
                result = _attach_ops_continuation_context(job_id, current, result, attempted, history)
                await _update_ops_job(job_id, result=result, history=history)
                continuous_cycle += 1
                if not await _wait_for_continuous_recheck(cancel_event, progress, cycle=continuous_cycle):
                    break
                current = _continuous_diagnostic_plan(current, result, cycle=continuous_cycle)
                await _update_ops_job(job_id, plan=copy.deepcopy(current))
                continue
            attempted.add(fingerprint)
            await _append_ops_job_event(
                job_id,
                "attempt",
                f"正在执行故障链第 {attempt_offset + attempt} 轮差异化修复策略",
                status="running",
                attempt=attempt,
                lineage_attempt=attempt_offset + attempt,
                max_attempts=0,
                strategy_window=strategy_window,
                continuous_until_recovered=True,
                strategy=current.get("title") or current.get("source") or "ops-plan",
            )
            await _update_ops_job(job_id, plan=copy.deepcopy(current))
            result = await _execute_ops_plan_once(
                current,
                cancel_event,
                progress,
                summarize=False,
                change_approval=await_change_approval,
            )
            history.append({
                "attempt": attempt,
                "strategy": current.get("title") or current.get("source") or "ops-plan",
                "fingerprint": fingerprint,
                "actions": sorted(_plan_action_types(current)),
                "change_fingerprints": [
                    _change_item_fingerprint(change)
                    for change in (current.get("changes") or [])
                    if isinstance(change, dict)
                ],
                "result": result,
            })
            del history[:-60]
            result = _attach_ops_continuation_context(job_id, current, result, attempted, history)
            history[-1]["result"] = result
            await _update_ops_job(job_id, history=history, result=result)
            if result.get("status") == "cancelled" or cancel_event.is_set():
                result = _ensure_effectiveness_record(current, result)
                await _update_ops_job(job_id, result=result)
                break
            if result.get("status") == "failed":
                if _operator_blocking_execution_failure(result):
                    reason = "变更执行面被权限、证书或网络阻断；更换业务修复方案也无法绕过该边界。"
                    operator_steps = _operator_steps_from_execution_failure(result)
                    result = _append_manual_exit_if_needed(current, result, reason, attempted)
                    if operator_steps:
                        result["operator_steps"] = operator_steps
                        result["next_steps"] = _ops_terminal_next_steps(
                            current,
                            result.get("verification") or {},
                            result.get("alternative_plans") or [],
                            operator_steps,
                            result.get("results") or [],
                        )
                    await _append_ops_job_event(job_id, "summarizing", "生成执行面阻断原因和管理员处理步骤", status="running")
                    result["ai_summary"] = await _llm_ops_summary(current, result.get("steps") or [], result.get("results") or [])
                    result = _ensure_effectiveness_record(current, result)
                    # API/RBAC/证书通道失败意味着业务变更根本没有被接受，因此
                    # 允许在通道修复后用一个全新的审批回执重试相同必要动作。
                    attempted.discard(fingerprint)
                    history[-1]["change_fingerprints"] = []
                    blocked_change_fingerprints = {
                        _change_item_fingerprint(change)
                        for change in (current.get("changes") or [])
                        if isinstance(change, dict)
                    }
                    current["_attempted_change_fingerprints"] = [
                        value for value in (current.get("_attempted_change_fingerprints") or [])
                        if str(value) not in blocked_change_fingerprints
                    ]
                    retry_plan = copy.deepcopy(current)
                    retry_plan.update({
                        "title": f"执行通道恢复后重试：{current.get('title') or '受控变更'}",
                        "high_risk_confirmed": False,
                        "operator_force_execute": False,
                        "operator_override_reason": "",
                        "stepwise_confirmation": True,
                        "external_precondition": reason,
                    })
                    for change in retry_plan.get("changes") or []:
                        if isinstance(change, dict):
                            change.pop("human_approved", None)
                            change.pop("operator_confirmed", None)
                            change.pop("approval_receipt", None)
                    result.setdefault("alternative_plans", []).insert(0, retry_plan)
                    result = _attach_ops_continuation_context(job_id, current, result, attempted, history)
                    await _append_ops_job_event(
                        job_id,
                        "needs_operator",
                        "执行通道被阻断；任务保持运行。修复权限/网络/证书后，系统会重新取证并签发新的逐项审批。",
                        status="running",
                        result=result,
                        level="warning",
                    )
                    continuous_cycle += 1
                    if not await _wait_for_continuous_recheck(cancel_event, progress, cycle=continuous_cycle):
                        break
                    current = _continuous_diagnostic_plan(current, result, cycle=continuous_cycle)
                    await _update_ops_job(job_id, plan=copy.deepcopy(current))
                    continue
                await _append_ops_job_event(
                    job_id,
                    "replanning",
                    "本轮变更没有成功或目标仍未恢复；保留失败证据并选择不同的下一策略。",
                    status="running",
                    level="warning",
                )
            if result.get("status") == "blocked":
                # 门禁阻断发生在提交前，不应把动作记成“已经失败执行”；门禁
                # 条件变化后仍可用新的逐项审批回执重新评估同一必要动作。
                attempted.discard(fingerprint)
                history[-1]["change_fingerprints"] = []
                result = _ensure_effectiveness_record(current, result)
                await _append_ops_job_event(
                    job_id,
                    "release_blocked",
                    result.get("message") or "SRE 变更门禁已阻断本次操作。",
                    status="running",
                    result=result,
                    level="warning",
                )
                continuous_cycle += 1
                if not await _wait_for_continuous_recheck(cancel_event, progress, cycle=continuous_cycle):
                    break
                current = _continuous_diagnostic_plan(current, result, cycle=continuous_cycle)
                await _update_ops_job(job_id, plan=copy.deepcopy(current))
                continue
            if result.get("status") == "completed" and (result.get("verification") or {}).get("recovered") is True:
                await _append_ops_job_event(job_id, "summarizing", "生成恢复结论和验证证据", status="running")
                result["ai_summary"] = await _llm_ops_summary(current, result.get("steps") or [], result.get("results") or [])
                result = _ensure_effectiveness_record(current, result)
                await _append_ops_job_event(
                    job_id,
                    "recovered",
                    "恢复验证明确通过，运维闭环完成。",
                    status="completed",
                    result=result,
                    level="success",
                )
                return
            alternatives = [p for p in (result.get("alternative_plans") or []) if isinstance(p, dict)]
            next_plan = _select_next_ops_plan(alternatives, attempted, autonomous)
            if not next_plan:
                result = _append_manual_exit_if_needed(
                    current,
                    result,
                    (
                        f"已完成 {attempt} 轮策略仍未取得恢复证据。"
                        "平台不会重复无效变更，将持续复检并保留管理员处理步骤。"
                    ),
                    attempted,
                )
                await _append_ops_job_event(
                    job_id,
                    "summarizing",
                    "生成诊断结论和可确认的下一步策略",
                    status="running",
                    alternative_plan_count=len(alternatives),
                )
                if attempt == 1 or attempt % strategy_window == 0:
                    result["ai_summary"] = await _llm_ops_summary(current, result.get("steps") or [], result.get("results") or [])
                result = _ensure_effectiveness_record(current, result)
                result = _attach_ops_continuation_context(job_id, current, result, attempted, history)
                await _append_ops_job_event(
                    job_id,
                    "needs_operator",
                    (
                        "诊断已完成并生成候选修复方案，请在界面中确认后执行。"
                        if result.get("status") == "planned" and alternatives else
                        "当前策略未取得恢复证据；任务将持续复检并保留差异化下一步计划。"
                    ),
                    status="running",
                    result=result,
                    level="warning",
                )
                continuous_cycle += 1
                if not await _wait_for_continuous_recheck(cancel_event, progress, cycle=continuous_cycle):
                    break
                current = _continuous_diagnostic_plan(current, result, cycle=continuous_cycle)
                await _update_ops_job(job_id, plan=copy.deepcopy(current))
                continue
            previous_current = current
            current = {
                **copy.deepcopy(initial_plan),
                **copy.deepcopy(next_plan),
                "cluster": next_plan.get("cluster") or previous_current.get("cluster") or initial_plan.get("cluster"),
                "cluster_id": next_plan.get("cluster_id") or previous_current.get("cluster_id") or initial_plan.get("cluster_id"),
                "namespace": next_plan.get("namespace") or initial_plan.get("namespace"),
                "source": next_plan.get("source") or previous_current.get("source") or initial_plan.get("source"),
                "evidence": next_plan.get("evidence") or previous_current.get("evidence") or initial_plan.get("evidence") or {},
                "pod_name": next_plan.get("pod_name") or previous_current.get("pod_name") or initial_plan.get("pod_name") or "",
                "stepwise_confirmation": next_plan.get("stepwise_confirmation", initial_plan.get("stepwise_confirmation", True)),
                # 新策略是新审批对象。上一轮任务级/高风险确认绝不能泄漏到
                # 下一轮，即使目标和 change_index 相同也要重新展示并批准。
                "high_risk_confirmed": False,
                "operator_force_execute": False,
                "operator_override_reason": "",
                "_lineage_id": previous_current.get("_lineage_id") or initial_plan.get("_lineage_id") or job_id,
                "_parent_job_id": job_id,
                "_prior_attempts": [
                    item for item in ((result.get("continuation_context") or {}).get("attempts") or [])
                    if isinstance(item, dict)
                ][-12:],
                "_prior_attempt_count": int((result.get("continuation_context") or {}).get("attempt_count") or 0),
                "_attempted_actions": sorted(_history_action_types(history) | _plan_action_types(previous_current)),
                "_attempted_strategy_fingerprints": sorted(attempted),
                "_attempted_change_fingerprints": sorted(
                    _history_change_fingerprints(history)
                    | {
                        _change_item_fingerprint(change)
                        for change in (previous_current.get("changes") or [])
                        if isinstance(change, dict)
                    }
                ),
                "_last_failure": previous_current.get("_last_failure") or {
                    "verification": (result.get("verification") or {}),
                    "change_results": (result.get("results") or []),
                },
            }
            for change in current.get("changes") or []:
                if isinstance(change, dict):
                    change.pop("human_approved", None)
                    change.pop("operator_confirmed", None)
                    change.pop("approval_receipt", None)
            await progress("strategy_switch", f"恢复验证未通过，切换为：{current.get('title', '替代策略')}")

        await _append_ops_job_event(
            job_id,
            "cancelled",
            "持续运维已由操作员中断；未恢复故障不会被标记为成功。",
            status="cancelled",
            history=history,
            level="warning",
        )
    except asyncio.CancelledError:
        await _append_ops_job_event(
            job_id,
            "cancelled",
            "自动运维已中断。若取消发生在 API 提交阶段，请核对目标资源最终状态。",
            status="cancelled",
            history=history,
            level="warning",
        )
    except Exception as exc:
        await _append_ops_job_event(
            job_id,
            "failed",
            f"运维任务异常终止：{type(exc).__name__}: {_redact_text(str(exc))}",
            status="failed",
            history=history,
            level="error",
        )
    finally:
        # A background task must never disappear while its public status still
        # says running. This is the final state-machine safety net for every
        # early return, cancellation and unexpected exception path.
        job = OPS_JOBS.get(job_id)
        if job and job.get("status") in {"queued", "running", "awaiting_approval", "cancelling"}:
            await _append_ops_job_event(
                job_id,
                "failed",
                "运维任务已结束但没有生成有效终态；系统已停止等待，请查看最后一条事件后重新发起。",
                status="failed",
                history=history,
                result=job.get("result") or {
                    "status": "failed",
                    "executed": False,
                    "message": "任务状态机未能完成收尾，未继续提交新的 Kubernetes 变更。",
                },
                level="error",
            )
        OPS_JOB_TASKS.pop(job_id, None)
        OPS_JOB_CANCEL_EVENTS.pop(job_id, None)
        OPS_JOB_STEP_APPROVAL_EVENTS.pop(job_id, None)


@app.on_event("startup")
async def resume_persisted_ops_jobs():
    """Resume unfinished incident diagnosis after invalidating process-local approvals."""
    for job_id, job in list(OPS_JOBS.items()):
        if job.get("status") != "resume_pending":
            continue
        plan = copy.deepcopy(job.get("plan") or {})
        if not plan or not (plan.get("steps") or plan.get("changes")):
            await _append_ops_job_event(
                job_id,
                "failed",
                "无法恢复持续任务：持久化记录中缺少可重新取证的计划。",
                status="failed",
                level="error",
            )
            continue
        plan["high_risk_confirmed"] = False
        plan["operator_force_execute"] = False
        plan["operator_override_reason"] = ""
        plan["stepwise_confirmation"] = bool(plan.get("changes"))
        for change in plan.get("changes") or []:
            if isinstance(change, dict):
                change.pop("human_approved", None)
                change.pop("operator_confirmed", None)
                change.pop("approval_receipt", None)
        cancel_event = asyncio.Event()
        OPS_JOB_CANCEL_EVENTS[job_id] = cancel_event
        await _append_ops_job_event(
            job_id,
            "resume_pending",
            "正在恢复未闭环故障：先重新采集实时证据，所有旧审批均已作废。",
            status="queued",
            pending_approval=None,
            approved_change_index=0,
            level="warning",
        )
        OPS_JOB_TASKS[job_id] = asyncio.create_task(
            _run_ops_job(job_id, plan, bool(job.get("autonomous")), cancel_event)
        )


def _plan_execution_readiness(plan: dict) -> dict:
    """Reject plans that look impressive but cannot reach a concrete action API."""
    errors: list[str] = []
    normalized: list[dict] = []

    def has_placeholder(value) -> bool:
        if isinstance(value, dict):
            return any(has_placeholder(item) for item in value.values())
        if isinstance(value, list):
            return any(has_placeholder(item) for item in value)
        if isinstance(value, str):
            return bool(re.search(r"<[^>]+>|\bTODO\b|待填写|placeholder", value.replace("<now>", ""), re.I))
        return False

    for index, raw in enumerate(plan.get("changes") or [], start=1):
        if not isinstance(raw, dict):
            errors.append(f"第 {index} 项变更不是结构化对象")
            continue
        if has_placeholder(raw):
            errors.append(f"第 {index} 项 {raw.get('type') or 'change'} 仍包含占位符，不能执行")
            continue
        change, reason = _normalize_planner_change(raw, plan)
        if not change:
            errors.append(f"第 {index} 项 {raw.get('type') or 'change'} 不可达：{reason}")
            continue
        normalized.append(change)
    if len(normalized) != len(plan.get("changes") or []):
        ready = False
    else:
        ready = True
        plan["changes"] = normalized
        _enrich_plan_change_policies(plan)
    return {
        "ready": ready,
        "checked_changes": len(plan.get("changes") or []),
        "action_catalog_valid": not errors,
        "target_bound": bool(
            _infrastructure_target_bound(plan)
            or plan.get("target")
            or _workload_identity_from_plan(plan)[2]
            or _target_pod_from_plan(plan)
        ),
        "evidence_present": bool(plan.get("evidence") or plan.get("root_cause_hypotheses") or plan.get("reason") or plan.get("summary")),
        "errors": errors,
        "external_preconditions": [
            "Kubernetes/Rancher API 可达或基础设施执行器已配置",
            "目标资源仍存在",
            "执行身份具备最小 RBAC/DBA/主机运维权限",
        ],
    }


async def _enqueue_ops_job(plan_input: dict, actor: str, *, autonomous: bool, confirmed: bool) -> dict:
    plan = _apply_ops_continuation_context(
        _enrich_plan_change_policies(copy.deepcopy(plan_input or {}))
    )
    submitted_changes = copy.deepcopy(plan.get("changes") or [])
    submitted_change_count = len(submitted_changes)
    initially_high_risk = [
        str(change.get("type") or "change")
        for change in plan.get("changes") or []
        if change.get("risk") == "high" or change.get("auto_allowed") is False
    ]
    initially_confirmed = bool(plan.get("high_risk_confirmed") or plan.get("operator_force_execute"))
    if initially_high_risk and not initially_confirmed:
        raise HTTPException(status_code=409, detail={
            "message": "计划包含高风险操作，需要操作员在风险预览中二次确认后才能进入证据与 Skill 执行门禁。",
            "requires_high_risk_confirmation": True,
            "high_risk_actions": initially_high_risk,
            "do_this": "先核对目标、实时证据、完整差异和回滚方式，再进行高风险二次确认；真实提交时仍会逐项暂停审批。",
        })
    plan = _attach_operator_skills_to_plan(
        plan,
        _skill_signal_payload(
            question=str(plan.get("summary") or plan.get("reason") or plan.get("title") or ""),
            alert={"cluster": plan.get("cluster"), "namespace": plan.get("namespace"), "target": plan.get("target")},
            diagnosis={"root_cause": plan.get("root_cause") or plan.get("reason") or plan.get("summary") or ""},
            evidence=plan.get("evidence") or {},
            plan=plan,
        ),
        preferred_skill_ids=[str(plan.get("selected_skill_id") or "")] if plan.get("selected_skill_id") else None,
    )
    if submitted_change_count and not plan.get("changes"):
        # Creation time often has only shallow chat/inspection evidence. Keep
        # the exact proposal for runtime review, but do not authorize it here.
        # `_execute_ops_plan_once` recollects live evidence and reruns the Skill
        # gate before it can reach the per-change approval or executor.
        plan["skill_preflight"] = {
            "status": "pending_live_evidence",
            "reason": plan.get("evidence_gap") or "matched execution-ready Skill and required evidence pending",
            "submitted_change_count": submitted_change_count,
        }
        plan["changes"] = submitted_changes
        plan["decision"] = "awaiting_live_skill_gate"
        _enrich_plan_change_policies(plan)
    plan["_operator"] = actor
    operator_confirmed = bool(plan.get("high_risk_confirmed") or plan.get("operator_force_execute"))
    if operator_confirmed:
        plan["high_risk_confirmed"] = True
        plan["operator_force_execute"] = True
    if plan.get("changes") and not _env_bool("OPS_MUTATION_ENABLED", "false"):
        raise HTTPException(status_code=403, detail="服务端 OPS_MUTATION_ENABLED=false，禁止创建运维任务")
    if autonomous and not _env_bool("AUTONOMOUS_OPS_ENABLED", "false"):
        raise HTTPException(status_code=403, detail="服务端 AUTONOMOUS_OPS_ENABLED=false，禁止自治策略升级")
    if autonomous and plan.get("changes") and not operator_confirmed:
        raise HTTPException(status_code=409, detail={
            "message": "AI 巡检可以自动诊断和生成方案，但任何真实写操作都必须由操作员明确确认后才能进入执行流。",
            "requires_human_approval": True,
            "requires_high_risk_confirmation": bool(plan.get("requires_high_risk_confirmation")),
            "do_this": "核对目标、证据、配置差异和回滚方式后在待审批队列确认；每项真实写操作提交前仍会再次暂停。",
        })
    if plan.get("changes"):
        # 任务级确认只代表允许进入执行流，不能替代动作级审批。所有真实写操作
        # 都必须停在 awaiting_approval，逐项展示完整目标、差异、风险和回滚方式。
        plan["stepwise_confirmation"] = True
    if plan.get("changes") and not confirmed:
        raise HTTPException(status_code=409, detail="需要人工确认后才能创建运维任务")
    high_risk_actions = [
        str(change.get("type") or "change")
        for change in plan.get("changes") or []
        if change.get("risk") == "high" or change.get("auto_allowed") is False
    ]
    if high_risk_actions and not plan.get("high_risk_confirmed"):
        raise HTTPException(status_code=409, detail={
            "message": "计划包含高风险操作，需要操作员在风险预览中二次确认后才能执行。",
            "requires_high_risk_confirmation": True,
            "high_risk_actions": high_risk_actions,
            "do_this": "勾选“即使高风险也确认执行”，核对目标、变更差异和回滚方式后再次提交。",
        })
    readiness = _plan_execution_readiness(plan)
    plan["execution_readiness"] = readiness
    if not readiness["ready"]:
        raise HTTPException(status_code=422, detail={
            "message": "运维计划未通过执行就绪校验，不会进入假执行状态。",
            "errors": readiness["errors"],
            "do_this": "补齐具体目标、参数或批准模板后重新生成预演。",
        })
    if not plan.get("changes") and not plan.get("steps"):
        raise HTTPException(status_code=422, detail="计划中既没有诊断步骤，也没有可执行的 Kubernetes 变更")
    active = sum(1 for item in OPS_JOBS.values() if item.get("status") in {"queued", "running", "awaiting_approval", "cancelling"})
    if active >= int(os.getenv("OPS_MAX_CONCURRENT_JOBS", "4")):
        raise HTTPException(status_code=429, detail="自动运维并发已达到保护阈值")
    job_id = f"ops-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    job = {
        "id": job_id,
        "lineage_id": plan.get("_lineage_id") or job_id,
        "parent_job_id": plan.get("_parent_job_id") or "",
        "status": "queued",
        "stage": "queued",
        "message": "等待执行",
        "autonomous": bool(autonomous),
        "operator": actor,
        "target": plan.get("target"),
        "cluster": plan.get("cluster"),
        "cluster_id": plan.get("cluster_id"),
        "namespace": plan.get("namespace"),
        "source": plan.get("source"),
        "stepwise_confirmation": bool(plan.get("stepwise_confirmation")),
        "execution_readiness": readiness,
        "attempt": 0,
        "lineage_attempt": int(plan.get("_prior_attempt_count") or len(plan.get("_prior_attempts") or [])),
        "max_attempts": 0,
        "strategy_window": max(1, min(20, int(os.getenv("AUTO_OPS_MAX_ATTEMPTS", "3")))),
        "continuous_until_recovered": True,
        "history": [],
        "events": [{"timestamp": now, "stage": "queued", "message": "等待执行", "level": "info"}],
        "created_at": now,
        "updated_at": now,
        "plan": copy.deepcopy(plan),
    }
    cancel_event = asyncio.Event()
    async with OPS_JOBS_LOCK:
        bounded_append_order = list(OPS_JOBS.keys())
        while len(bounded_append_order) >= int(os.getenv("OPS_JOB_STORE_LIMIT", "200")):
            OPS_JOBS.pop(bounded_append_order.pop(0), None)
        OPS_JOBS[job_id] = job
        OPS_JOB_CANCEL_EVENTS[job_id] = cancel_event
        OPS_JOB_TASKS[job_id] = asyncio.create_task(_run_ops_job(job_id, plan, autonomous, cancel_event))
        _persist_ops_jobs()
    audit_warning = _safe_audit_event(
        "aiops.job.create",
        actor,
        f"{plan.get('cluster')}/{plan.get('namespace')}/{plan.get('target')}",
        "accepted",
        job_id=job_id,
        autonomous=bool(autonomous),
        change_fingerprint=_change_fingerprint(plan),
    )
    if audit_warning:
        await _append_ops_job_event(
            job_id,
            "audit_warning",
            "运维任务已创建，但审计日志写入失败；执行流程不会因此中断。",
            audit_warning=audit_warning,
            _update_stage=False,
            level="warning",
        )
    return _public_ops_job(job)


async def create_ops_job(req: OpsJobCreateRequest, request: Request):
    plan = copy.deepcopy(req.plan or {})
    shell_actions = {"run_shell", "exec_pod", "exec_node"}
    if any(str(change.get("type") or "") in shell_actions for change in plan.get("changes") or [] if isinstance(change, dict)):
        # Shell 必须在界面展示完整命令后逐项确认，不能只依赖任务级确认。
        plan["stepwise_confirmation"] = True
        plan["contains_shell"] = True
    # 审批字段同时作为请求级契约传递，避免计划在聊天、巡检或替代策略之间
    # 转换时丢失人工确认。服务端仍以显式 True 为准，不会自动放行。
    if req.high_risk_confirmed or req.operator_force_execute or req.allow_high_risk_after_confirmation:
        plan["high_risk_confirmed"] = True
        plan["operator_force_execute"] = True
    if req.operator_override_reason:
        plan["operator_override_reason"] = req.operator_override_reason
    if req.stepwise_confirmation:
        plan["stepwise_confirmation"] = True
    if plan.get("changes"):
        plan["stepwise_confirmation"] = True
    return await _enqueue_ops_job(
        plan,
        _request_actor(request),
        autonomous=req.autonomous,
        confirmed=req.confirm,
    )


async def get_ops_job(job_id: str):
    job = OPS_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="运维任务不存在或已过期")
    task = OPS_JOB_TASKS.get(job_id)
    if job.get("status") in {"queued", "running", "awaiting_approval", "cancelling"} and task is not None and task.done():
        error = ""
        if not task.cancelled():
            try:
                error = str(task.exception() or "")
            except Exception as exc:
                error = str(exc)
        await _append_ops_job_event(
            job_id,
            "failed",
            "执行协程已经停止，系统已关闭持续等待。" + (f" 原因：{_redact_text(error)}" if error else ""),
            status="failed",
            result=job.get("result") or {"status": "failed", "executed": False, "message": error or "执行协程提前结束"},
            level="error",
        )
    return _public_ops_job(job)


async def approve_ops_job_step(job_id: str, req: OpsStepApprovalRequest, request: Request):
    try:
        job = OPS_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="运维任务不存在或已过期")
        pending = job.get("pending_approval") or {}
        if job.get("status") != "awaiting_approval" or not pending:
            consumed = job.get("last_consumed_approval") or {}
            if (
                req.confirm
                and str(consumed.get("approval_id") or "") == req.approval_id
                and str(consumed.get("change_fingerprint") or "") == req.change_fingerprint
                and int(consumed.get("change_index") or 0) == int(req.change_index)
                and job.get("status") in {"running", "completed", "unresolved", "failed"}
            ):
                # 只对同一个一次性审批回执幂等；不能仅凭“第 1 步”复用到下一轮。
                return _public_ops_job(job)
            raise HTTPException(status_code=409, detail="当前任务没有等待确认的变更步骤")
        expected = int(pending.get("change_index") or 0)
        if req.change_index != expected:
            raise HTTPException(status_code=409, detail=f"当前等待确认的是第 {expected} 步，不能确认第 {req.change_index} 步")
        if req.approval_id != str(pending.get("approval_id") or ""):
            raise HTTPException(status_code=409, detail="审批凭据已过期或不属于当前变更，请刷新任务状态后重新核对")
        if req.change_fingerprint != str(pending.get("change_fingerprint") or ""):
            raise HTTPException(status_code=409, detail="审批对应的变更内容已经变化，旧确认不会被复用")
        if pending.get("consumed"):
            raise HTTPException(status_code=409, detail="该审批凭据已经消费，不能重复批准其他动作")
        event = OPS_JOB_STEP_APPROVAL_EVENTS.get(job_id)
        if not event:
            raise HTTPException(status_code=409, detail="逐步确认会话已经结束，请刷新任务状态")
        actor = _request_actor(request)
        consumed_receipt = {
            "approval_id": req.approval_id,
            "change_fingerprint": req.change_fingerprint,
            "change_index": expected,
            "lineage_attempt": pending.get("lineage_attempt"),
            "actor": actor,
            "confirmed": bool(req.confirm),
            "consumed_at": datetime.now(timezone.utc).isoformat(),
        }
        # 在第一次 await 之前原子标记消费，避免双击/重放并发批准同一步骤。
        pending["consumed"] = True
        pending["consumed_at"] = consumed_receipt["consumed_at"]
        job["last_consumed_approval"] = consumed_receipt
        if not req.confirm:
            cancel_event = OPS_JOB_CANCEL_EVENTS.get(job_id)
            if cancel_event:
                cancel_event.set()
            await _update_ops_job(
                job_id,
                approved_change_index=0,
                approval_comment=req.comment,
                approval_actor=actor,
                last_consumed_approval=consumed_receipt,
            )
            event.set()
            audit_warning = _safe_audit_event(
                "aiops.job.step.reject",
                actor,
                job_id,
                "accepted",
                change_index=expected,
                approval_id=req.approval_id,
                change_fingerprint=req.change_fingerprint,
                comment=req.comment,
            )
            if audit_warning:
                await _append_ops_job_event(
                    job_id,
                    "audit_warning",
                    "步骤拒绝已生效，但审计日志写入失败。",
                    audit_warning=audit_warning,
                    _update_stage=False,
                    level="warning",
                )
            current_job = OPS_JOBS.get(job_id)
            if not current_job:
                raise HTTPException(status_code=409, detail="任务状态已释放，请刷新页面")
            return _public_ops_job(current_job)
        await _update_ops_job(
            job_id,
            status="running",
            stage="change_approval_received",
            message=f"已收到第 {expected} 步人工确认，正在提交受控变更执行器。",
            approved_change_index=expected,
            approval_comment=req.comment,
            approval_actor=actor,
            last_consumed_approval=consumed_receipt,
        )
        event.set()
        audit_warning = _safe_audit_event(
            "aiops.job.step.approve",
            actor,
            job_id,
            "accepted",
            change_index=expected,
            approval_id=req.approval_id,
            change_fingerprint=req.change_fingerprint,
            change_action=pending.get("action"),
            target=pending.get("target"),
            comment=req.comment,
        )
        if audit_warning:
            await _append_ops_job_event(
                job_id,
                "audit_warning",
                "步骤确认已生效，但审计日志写入失败；系统继续执行受控变更。",
                audit_warning=audit_warning,
                _update_stage=False,
                level="warning",
            )
        current_job = OPS_JOBS.get(job_id)
        if not current_job:
            raise HTTPException(status_code=409, detail="任务状态已释放，请刷新页面")
        return _public_ops_job(current_job)
    except HTTPException:
        raise
    except Exception as exc:
        await _append_ops_job_event(
            job_id,
            "approval_failed",
            f"确认步骤处理失败：{type(exc).__name__}: {_redact_text(str(exc))}",
            status="failed",
            result={"status": "failed", "executed": False, "message": _redact_text(str(exc))},
            level="error",
        )
        raise HTTPException(status_code=409, detail=f"确认步骤处理失败：{type(exc).__name__}: {_redact_text(str(exc))}")


async def cancel_ops_job(job_id: str, request: Request):
    job = OPS_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="运维任务不存在或已过期")
    if job.get("status") in {"completed", "failed", "cancelled", "unresolved", "blocked"}:
        return _public_ops_job(job)
    previous_status = job.get("status")
    event = OPS_JOB_CANCEL_EVENTS.get(job_id)
    if event:
        event.set()
    await _update_ops_job(
        job_id,
        status="cancelling",
        stage="cancelling",
        message="已收到中断请求；当前原子 API 调用结束后不再执行后续动作。",
    )
    task = OPS_JOB_TASKS.get(job_id)
    if task and not task.done():
        task.cancel()
    audit_warning = _safe_audit_event(
        "aiops.job.cancel",
        _request_actor(request),
        job_id,
        "accepted",
        previous_status=previous_status,
    )
    if audit_warning:
        await _append_ops_job_event(
            job_id,
            "audit_warning",
            "中断请求已生效，但审计日志写入失败。",
            audit_warning=audit_warning,
            _update_stage=False,
            level="warning",
        )
    return _public_ops_job(OPS_JOBS[job_id])


def _attach_inspection_algorithms(payload: dict) -> dict:
    findings = payload.get("findings") or []
    ranked = prioritize_inspection_findings(findings)
    payload["findings"] = ranked["findings"]
    payload["inspection_algorithm"] = ranked["algorithm"]
    payload.setdefault("summary", {})["priority_top"] = ranked["top_risks"]
    _record_algorithm_decision(
        "InspectionEvidencePriority",
        "AI 巡检 / /api/inspection/run",
        {
            "total_findings": len(ranked["findings"]),
            "top_risks": ranked["top_risks"],
            "algorithm": ranked["algorithm"],
        },
        {"source": payload.get("source"), "clusters": (payload.get("summary") or {}).get("clusters")},
        "用于决定巡检结果排序、自动运维优先队列和告警去重。",
    )
    return payload


def _attach_model_profile_to_payload(payload: dict, model_profile_id: str = "") -> dict:
    profile_id = model_profile_id or get_active_model_profile_id() or os.getenv("LLM_MODEL", "default")
    payload["model_profile_id"] = profile_id
    for finding in payload.get("findings") or []:
        if isinstance(finding, dict):
            finding["model_profile_id"] = profile_id
            plan = finding.get("ops_plan")
            if isinstance(plan, dict):
                plan["model_profile_id"] = profile_id
    return payload


async def preview_ai_inspection_finding(req: InspectionPreviewRequest):
    """Generate an on-demand preview from live evidence instead of a UI template."""
    finding = next(
        (
            item for item in (LAST_INSPECTION_PAYLOAD.get("findings") or [])
            if isinstance(item, dict) and str(item.get("id") or "") == req.finding_id
        ),
        None,
    )
    if not finding:
        raise HTTPException(status_code=404, detail="巡检结果已过期，请先重新执行巡检")

    finding = copy.deepcopy(finding)
    base_plan = _ops_plan_from_finding(finding)
    base_plan["model_profile_id"] = req.model_profile_id or finding.get("model_profile_id") or ""
    evidence_timeout = max(10, int(os.getenv("OPS_EVIDENCE_TIMEOUT_SECONDS", "70")))
    try:
        deep = await run_with_heartbeat(
            _collect_plan_deep_evidence(base_plan),
            stage="inspection_preview_evidence",
            timeout_seconds=evidence_timeout,
            heartbeat_seconds=float(os.getenv("OPS_HEARTBEAT_SECONDS", "5")),
        )
    except Exception as exc:
        deep = {"error": f"{type(exc).__name__}: {_redact_text(str(exc))}"}
    base_plan["_runtime_evidence"] = deep

    workload = finding.get("workload") or {}
    pod = deep.get("pod") or ((finding.get("evidence") or {}).get("pod") or {})
    engine_plan = build_remediation_plan(
        {
            "alert_name": finding.get("category") or "inspection_finding",
            "summary": finding.get("summary") or finding.get("title") or "",
            "namespace": base_plan.get("namespace"),
            "workload_type": workload.get("kind") or _workload_identity_from_plan(base_plan)[1],
            "workload_name": workload.get("name") or _workload_identity_from_plan(base_plan)[2],
            "pod": pod.get("name") or base_plan.get("pod_name") or "",
        },
        {
            "root_cause": finding.get("summary") or "",
            "signals": deep.get("events") or [],
        },
        {
            **deep,
            "pod": pod,
            "pods": [pod] if pod else [],
            "events": {"events": deep.get("events") or []},
        },
    )
    replans = await _evidence_based_replan(base_plan, [], set(), include_llm=True)
    if replans:
        plan = replans[0]
        plan.update({
            "id": f"preview-{req.finding_id}",
            "title": f"实时 AI 预演：{finding.get('title') or base_plan.get('target')}",
            "cluster": base_plan.get("cluster"),
            "cluster_id": base_plan.get("cluster_id"),
            "source": base_plan.get("source"),
            "namespace": base_plan.get("namespace"),
            "target": base_plan.get("target"),
            "pod_name": base_plan.get("pod_name") or pod.get("name") or "",
        })
    else:
        plan = {
            **base_plan,
            "id": f"preview-{req.finding_id}",
            "title": f"实时 AI 预演：{finding.get('title') or base_plan.get('target')}",
            "steps": engine_plan.get("steps") or base_plan.get("steps") or [],
            "changes": engine_plan.get("changes") or [],
            "summary": engine_plan.get("reason") or finding.get("summary") or "",
            "reason": engine_plan.get("reason") or "",
            "evidence_gap": engine_plan.get("evidence_gap") or deep.get("error") or "",
            "root_cause_hypotheses": engine_plan.get("hypotheses") or [],
            "success_criteria": engine_plan.get("success_criteria") or [],
            "planning": base_plan.get("_runtime_replan") or {},
        }

    namespace, workload_type, workload_name = _workload_identity_from_plan(base_plan)
    pod_name = base_plan.get("pod_name") or pod.get("name") or ""
    rejected = []
    bound_changes = []
    for raw_change in plan.get("changes") or []:
        change = dict(raw_change)
        action = str(change.get("type") or "")
        if action in {"restart", "patch_workload", "patch_workload_volume", "scale_out", "rollback_workload"}:
            if change.get("workload_name") and change.get("workload_name") != workload_name:
                rejected.append({"action": action, "target": change.get("workload_name")})
                continue
            change.update({"namespace": namespace, "workload_type": workload_type, "workload_name": workload_name})
        if action in {"recreate_pod", "evict_pod"}:
            if change.get("pod_name") and change.get("pod_name") != pod_name:
                rejected.append({"action": action, "target": change.get("pod_name")})
                continue
            change.update({"namespace": namespace, "pod_name": pod_name})
        bound_changes.append(change)
    plan["changes"] = bound_changes
    plan["preview_mode"] = "live_evidence_ai"
    plan["generated_at"] = datetime.now(timezone.utc).isoformat()
    plan["target_binding"] = "inspection_finding_id"
    plan["rejected_cross_target_actions"] = rejected
    plan["evidence_summary"] = {
        "events": len(deep.get("events") or []),
        "log_streams": len(deep.get("logs") or {}),
        "storage_objects": len(deep.get("storage") or []),
        "services": len(deep.get("services") or []),
        "has_workload": bool(deep.get("workload")),
        "error": deep.get("error"),
    }
    preferred = ((finding.get("skill_routing") or {}).get("selected_skill_ids") or [])
    plan = _attach_operator_skills_to_plan(
        plan,
        _inspection_skill_signal(
            finding,
            evidence_override={
                **(finding.get("evidence") or {}),
                **deep,
            },
            plan_override=plan,
        ),
        top_k=3,
        preferred_skill_ids=preferred,
        routing=finding.get("skill_routing") or None,
    )
    _enrich_plan_change_policies(plan)
    plan.pop("_runtime_evidence", None)
    return {
        "finding_id": req.finding_id,
        "status": "ready" if plan.get("changes") else "evidence_only",
        "plan": _redact_sensitive(plan),
        "evidence_summary": plan.get("evidence_summary"),
        "message": (
            "已基于实时证据、运维 Skill 和受约束 LLM 生成可确认预演。"
            if plan.get("changes") else
            "实时取证已完成，但当前证据或批准参数不足以生成安全变更。"
        ),
    }


async def run_ai_inspection(req: InspectionRequest):
    global LAST_INSPECTION_PAYLOAD
    if _rancher_enabled() or _managed_cluster_metadata():
        if _rancher_enabled() and (_managed_cluster_metadata() and (req.cluster or "all") in {"", "all", "*", "所有"}):
            rancher_payload, managed_payload = await asyncio.gather(_rancher_inspection(req), _managed_inspection(req))
            payload = {**rancher_payload, "source": "rancher+kubeconfig", "clusters": [*rancher_payload.get("clusters", []), *managed_payload.get("clusters", [])], "findings": [*rancher_payload.get("findings", []), *managed_payload.get("findings", [])], "namespaces_by_cluster": {**rancher_payload.get("namespaces_by_cluster", {}), **managed_payload.get("namespaces_by_cluster", {})}}
            payload["summary"] = {**rancher_payload.get("summary", {}), "total": len(payload["findings"]), "critical": sum(1 for finding in payload["findings"] if finding.get("severity") in {"P0", "P1"}), "clusters": len(payload["clusters"])}
        elif any((req.cluster or "all") in {cluster["id"], cluster["name"]} for cluster in _managed_cluster_metadata()) or (not _rancher_enabled()):
            payload = await _managed_inspection(req)
        else:
            payload = await _rancher_inspection(req)
        payload = await _route_inspection_findings_with_skills(payload, req.model_profile_id)
        payload = _attach_inspection_algorithms(payload)
        payload = _attach_model_profile_to_payload(payload, req.model_profile_id)
        LAST_INSPECTION_PAYLOAD = payload
        payload["effectiveness"] = record_inspection(req.cluster, req.namespace, payload, model_id=req.model_profile_id or get_active_model_profile_id() or os.getenv("LLM_MODEL", "default"))
        return payload

    if (req.cluster or "all") not in {"", "all", "*", "local", "local-cluster", "所有"}:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "ok",
            "source": "mcp-local",
            "clusters": [{"id": "local", "name": "local-cluster"}],
            "namespaces_by_cluster": {"local-cluster": []},
            "findings": [],
            "summary": {"total": 0, "critical": 0, "auto_ops": req.auto_ops, "production_mode": req.production_mode, "clusters": 1},
            "node_condition_standard": "当前未配置 Rancher，只能巡检本集群。",
        }
        payload = await _route_inspection_findings_with_skills(payload, req.model_profile_id)
        payload = _attach_inspection_algorithms(payload)
        payload = _attach_model_profile_to_payload(payload, req.model_profile_id)
        LAST_INSPECTION_PAYLOAD = payload
        payload["effectiveness"] = record_inspection(req.cluster, req.namespace, payload, model_id=req.model_profile_id or get_active_model_profile_id() or os.getenv("LLM_MODEL", "default"))
        return payload

    findings: list[dict] = []
    nodes_data = await _call_mcp_tool("list_nodes", {})
    for node in nodes_data.get("nodes", []) if isinstance(nodes_data, dict) else []:
        if node.get("health") != "healthy":
            reason = ",".join(node.get("problems") or ["NotReady"])
            finding = {
                "id": _finding_id("node", "", node.get("name", ""), reason),
                "category": "node",
                "severity": "P1" if node.get("health") == "not_ready" else "P2",
                "title": f"Node {node.get('name')} 状态异常",
                "summary": f"Node condition: {reason}. 判定标准：{node.get('condition_standard')}",
                "source": "mcp",
                "cluster": "local-cluster",
                "cluster_id": "local",
                "namespace": "",
                "name": node.get("name"),
                "evidence": node,
            }
            finding["ops_plan"] = _ops_plan_from_finding(finding)
            findings.append(finding)

    namespace_arg = req.namespace if (req.namespace or "all") not in {"", "*", "所有"} else "all"
    topology = await _call_mcp_tool("get_resilience_topology", {"namespace": namespace_arg})
    namespace_list: set[str] = set()
    for section in topology.get("topology", []) if isinstance(topology, dict) else []:
        ns = section.get("namespace", "default")
        namespace_list.add(ns)
        for workload in section.get("workloads", []):
            impact = workload.get("impact", {})
            pods = workload.get("pods", [])
            workload_findings = []
            for pod in pods:
                events = []
                category, severity, reason = _classify_pod_issue(pod, events)
                if not category:
                    continue
                # Fetch events only for suspicious pods, then reclassify with richer evidence.
                event_data = await _call_mcp_tool("get_pod_events", {"namespace": ns, "pod_name": pod.get("name", "")})
                events = event_data.get("events", []) if isinstance(event_data, dict) else []
                category, severity, reason = _classify_pod_issue(pod, events)
                workload_findings.append({
                    "pod": pod,
                    "events": events[:8],
                    "category": category,
                    "severity": severity,
                    "reason": reason,
                })

            for item in workload_findings:
                pod = item["pod"]
                finding = {
                    "id": _finding_id("pod", ns, pod.get("name", ""), item["category"]),
                    "category": item["category"],
                    "severity": item["severity"],
                    "title": f"Pod {pod.get('name')} {item['reason']}",
                    "summary": f"{item['reason']}。所属 {workload.get('kind')}/{workload.get('name')}，重启 {pod.get('restart_count', 0)} 次，phase={pod.get('phase')}",
                    "source": "mcp",
                    "cluster": "local-cluster",
                    "cluster_id": "local",
                    "namespace": ns,
                    "name": pod.get("name"),
                    "workload": workload,
                    "evidence": {
                        "pod": pod,
                        "events": item["events"],
                        "state_text": _pod_state_text(pod),
                    },
                }
                finding["ops_plan"] = _ops_plan_from_finding(finding)
                findings.append(finding)

            has_primary_runtime_issue = any(f["category"] in {"crashloop", "image_pull", "network", "storage_config", "scheduling"} for f in workload_findings)
            bad_pods = [p for p in pods if not p.get("ready") or p.get("restart_count", 0) > 5]
            if not has_primary_runtime_issue and (impact.get("level") in {"critical", "high"} or bad_pods):
                category = "capacity"
                finding = {
                    "id": _finding_id("workload", ns, workload.get("name", ""), impact.get("summary", category)),
                    "category": category,
                    "severity": "P1" if impact.get("level") == "critical" else "P2",
                    "title": f"{workload.get('kind')}/{workload.get('name')} 存在可靠性风险",
                    "summary": impact.get("summary") or "Pod readiness/restart signals indicate risk.",
                    "source": "mcp",
                    "cluster": "local-cluster",
                    "cluster_id": "local",
                    "namespace": ns,
                    "name": workload.get("name"),
                    "workload": workload,
                    "evidence": {"impact": impact, "bad_pods": bad_pods[:5]},
                }
                finding["ops_plan"] = _ops_plan_from_finding(finding)
                findings.append(finding)

            if req.production_mode:
                first_pod = pods[0] if pods else {}
                production_workload = {
                    **workload,
                    "cluster": "local-cluster",
                    "cluster_id": "local",
                    "namespace": ns,
                    "containers": first_pod.get("containers") or [],
                    "pod_spec": {
                        "securityContext": first_pod.get("security_context") or {},
                        "hostNetwork": first_pod.get("hostNetwork", False),
                        "hostPID": first_pod.get("hostPID", False),
                        "hostIPC": first_pod.get("hostIPC", False),
                    },
                    "labels": first_pod.get("labels") or {},
                }
                findings.extend(_workload_production_risk_findings(
                    production_workload,
                    cluster={"id": "local", "name": "local-cluster"},
                    source="mcp",
                ))

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "source": "mcp-local",
        "clusters": [{"id": "local", "name": "local-cluster"}],
        "namespaces_by_cluster": {"local-cluster": sorted(namespace_list)},
        "findings": findings,
        "summary": {
            "total": len(findings),
            "critical": sum(1 for f in findings if f.get("severity") in {"P0", "P1"}),
            "auto_ops": req.auto_ops,
            "production_mode": req.production_mode,
            "clusters": 1,
        },
        "node_condition_standard": nodes_data.get("condition_standard") if isinstance(nodes_data, dict) else "",
    }
    payload = await _route_inspection_findings_with_skills(payload, req.model_profile_id)
    payload = _attach_inspection_algorithms(payload)
    payload = _attach_model_profile_to_payload(payload, req.model_profile_id)
    LAST_INSPECTION_PAYLOAD = payload
    payload["effectiveness"] = record_inspection(req.cluster, req.namespace, payload, model_id=req.model_profile_id or get_active_model_profile_id() or os.getenv("LLM_MODEL", "default"))
    return payload


async def mcp_call(req: MCPToolRequest):
    """直接调用 MCP K8s 工具（list_pods, get_pod_events, get_pod_logs, list_nodes 等）"""
    try:
        async with OUTBOUND_BULKHEAD.slot():
            async with _client(30) as c:
                resp = await c.post(
                    _mcp_tools_url(),
                    json={"tool": req.tool, "arguments": req.arguments},
                    headers=_internal_headers(),
                )
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}
            if resp.status_code >= 400:
                return {
                    "error": "MCP tool call failed.",
                    "tool": req.tool,
                    "http_status": resp.status_code,
                    "detail": data,
                }
            return data
    except httpx.ConnectError as remote_error:
        if os.getenv("ENABLE_LOCAL_MCP_FALLBACK", "false").lower() != "true":
            return {
                "error": "MCP service unavailable.",
                "tool": req.tool,
                "mcp_url": _mcp_tools_url(),
                "remote_error": str(remote_error),
            }
        # 仅本地开发显式开启时才回退到同进程调用；集群内前端容器不应冒用自己的 ServiceAccount 访问 K8S。
        import sys
        sys.path.insert(0, str(ROOT_DIR))
        try:
            from mcp_servers.k8s_mcp_server import (
                list_pods,
                get_pod_events,
                get_pod_logs,
                get_pod_diagnostics,
                list_nodes,
                list_pod_metrics,
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
                get_cluster_summary,
                list_all_pods,
                list_all_deployments,
                list_namespaces,
                get_resilience_topology,
                get_external_traffic_candidates,
                check_access,
            )
        except Exception as fallback_import_error:
            return {
                "error": "MCP service unavailable and local fallback failed.",
                "tool": req.tool,
                "remote_error": str(remote_error),
                "fallback_error": str(fallback_import_error),
            }
        tool_map = {
            "list_pods": list_pods,
            "get_pod_events": get_pod_events,
            "get_pod_logs": get_pod_logs,
            "get_pod_diagnostics": get_pod_diagnostics,
            "list_nodes": list_nodes,
            "list_pod_metrics": list_pod_metrics,
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
            "get_cluster_summary": get_cluster_summary,
            "list_all_pods": list_all_pods,
            "list_all_deployments": list_all_deployments,
            "list_namespaces": list_namespaces,
            "get_resilience_topology": get_resilience_topology,
            "get_external_traffic_candidates": get_external_traffic_candidates,
            "check_access": check_access,
        }
        fn = tool_map.get(req.tool)
        if fn is None:
            raise HTTPException(400, f"Unknown tool: {req.tool}")
        try:
            return fn(**req.arguments)
        except TypeError:
            try:
                return fn()
            except Exception as local_error:
                return {
                    "error": str(local_error),
                    "tool": req.tool,
                    "remote_error": str(remote_error),
                "fallback_error": traceback.format_exc(limit=4),
            }
        except Exception as local_error:
            return {
                "error": str(local_error),
                "tool": req.tool,
                "remote_error": str(remote_error),
                "fallback_error": traceback.format_exc(limit=4),
            }
    except Exception as remote_error:
        return {
            "error": "MCP proxy failed.",
            "tool": req.tool,
            "mcp_url": _mcp_tools_url(),
            "remote_error": str(remote_error),
        }


async def scan_and_trigger_alert(req: AlertScanRequest):
    """Scan real namespace state before creating an alert investigation."""
    started_at = datetime.now(timezone.utc)
    scan_source = "mcp-local"
    scan_clusters: list[dict] = [{"id": "local", "name": "local-cluster"}]
    scan_errors: dict[str, str] = {}
    if _rancher_enabled():
        pods, scan_clusters, scan_errors = await _rancher_pods_for_alert_scan(req.cluster, req.namespace)
        scan_source = "rancher"
    else:
        pods_data = await _call_mcp_tool("list_pods", {"namespace": req.namespace})
        pods = pods_data.get("pods", []) if isinstance(pods_data, dict) else []
    findings: list[dict] = []
    alert_name = "NamespaceHealthCheck"
    summary = ""
    description = ""
    priority = "high" if req.severity in ("P0", "P1", "P2") else "medium"

    if req.intent == "crashloop":
        findings = [p for p in pods if _is_crash_pod(p)]
        alert_name = "KubePodCrashLooping"
        summary = f"{len(findings)} pod(s) show crash/restart signals in namespace {req.namespace}"
        description = "Pods with CrashLoopBackOff/ImagePullBackOff/OOMKilled/Error state or restart_count > 5."

    elif req.intent == "pending":
        findings = [p for p in pods if p.get("phase") == "Pending"]
        alert_name = "KubePodPending"
        summary = f"{len(findings)} pod(s) are Pending in namespace {req.namespace}"
        description = "Pods remain Pending and may require scheduling/resource investigation."

    elif req.intent == "highcpu":
        threshold_m = float(os.getenv("HIGH_CPU_MILLICORES_THRESHOLD", "800"))
        if _rancher_enabled():
            pod_by_key = {
                (p.get("cluster_id"), p.get("namespace"), p.get("name")): p
                for p in pods
            }
            for cluster in scan_clusters:
                cid = cluster.get("id") or cluster.get("name")
                if not cid:
                    continue
                try:
                    ns_path = "" if req.namespace in {"", "all", "*", "所有"} else f"/namespaces/{quote(req.namespace, safe='')}"
                    metrics = await _rancher_k8s_get(cid, f"/apis/metrics.k8s.io/v1beta1{ns_path}/pods", timeout=18)
                    for raw in metrics.get("items", []) if isinstance(metrics, dict) else []:
                        meta = raw.get("metadata") or {}
                        ns = meta.get("namespace", req.namespace)
                        name = meta.get("name", "")
                        total_m = sum(_cpu_to_millicores((c.get("usage") or {}).get("cpu")) for c in raw.get("containers", []))
                        if total_m >= threshold_m:
                            item = dict(pod_by_key.get((cid, ns, name), {}))
                            item.update({
                                "name": item.get("name") or name,
                                "namespace": item.get("namespace") or ns,
                                "cluster": cluster.get("name") or cid,
                                "cluster_id": cid,
                                "cpu_millicores": total_m,
                                "metrics_source": "rancher-metrics-api",
                            })
                            findings.append(item)
                except Exception as exc:
                    scan_errors[cluster.get("name", cid)] = f"metrics: {type(exc).__name__}: {exc}"
        else:
            metrics = await _call_mcp_tool("list_pod_metrics", {"namespace": req.namespace})
            if metrics.get("error"):
                return {
                    "status": "no_signal",
                    "reason": "缺少 metrics-server 或 metrics.k8s.io 权限，无法证明高 CPU。",
                    "intent": req.intent,
                    "cluster": req.cluster,
                    "namespace": req.namespace,
                    "evidence": {"source": scan_source, "clusters": scan_clusters, "errors": scan_errors, "pods": pods[:10], "metrics_error": metrics.get("error")},
                    "incidents": INCIDENTS_STORE[-50:],
                    "postmortems": POSTMORTEMS_STORE[-50:],
                }
            metric_pods = metrics.get("pods", [])
            for item in metric_pods:
                total_m = sum(_cpu_to_millicores(c.get("cpu")) for c in item.get("containers", []))
                if total_m >= threshold_m:
                    item["cpu_millicores"] = total_m
                    findings.append(item)
        alert_name = "HighCPUUsage"
        summary = f"{len(findings)} pod(s) exceed CPU evidence threshold in namespace {req.namespace}"
        description = f"Pod metrics show CPU usage >= {int(threshold_m)}m."

    else:
        return {"status": "failed", "error": f"Unknown alert scan intent: {req.intent}"}

    if not findings:
        return {
            "status": "no_signal",
            "reason": "当前 namespace 未发现该类异常证据，未触发 LLM 故障诊断。",
            "intent": req.intent,
            "cluster": req.cluster,
            "namespace": req.namespace,
            "evidence": {"source": scan_source, "clusters": scan_clusters, "errors": scan_errors, "pods_checked": len(pods), "sample": pods[:10]},
            "incidents": INCIDENTS_STORE[-50:],
            "postmortems": POSTMORTEMS_STORE[-50:],
        }

    first = findings[0]
    detected_severity = _auto_severity(req.intent, findings) if str(req.severity).lower() in {"", "auto", "自动", "自动识别"} else req.severity
    priority = "critical" if detected_severity in ("P0", "P1") else "high" if detected_severity == "P2" else "medium"
    body = {
        # 告警扫描只负责发现、诊断和产出计划。即使调用方请求自动修复，
        # 也不能绕过 OpsJob 的逐步人工审批。
        "auto_healing_enabled": False,
        "alerts": [{
            "labels": {
                "alertname": alert_name,
                "cluster": first.get("cluster") or req.cluster,
                "cluster_id": first.get("cluster_id") or req.cluster,
                "namespace": first.get("namespace") or req.namespace,
                "pod": first.get("name"),
                "workload_name": _pod_owner_name(first) if req.intent != "highcpu" else "",
                "workload_type": first.get("workload_kind") or (first.get("workload") or {}).get("kind") or "Deployment",
                "severity": detected_severity,
                "priority": priority,
                "auto_healing_enabled": "false",
            },
            "annotations": {
                "summary": summary,
                "description": description,
                "evidence": str(findings[:5]),
            },
        }],
    }

    incoming = body
    normalized, meta = _normalize_alertmanager_body(incoming)
    try:
        async with _client(120) as c:
            resp = await c.post(f"{SERVICES['observability']}/alertmanager/webhook", json=normalized)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        data = {"status": "fallback", "error": str(e)}

    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        _record_llm_observation("alert_scan", req.model_dump(), data if isinstance(data, dict) else {}, started_at, error=(data or {}).get("error", "") if isinstance(data, dict) else "")
    for item in results:
        if not isinstance(item, dict):
            item = {"result": item}
        _remember_graph_result(item.get("raw"))
        _record_llm_observation("alert_scan", req.model_dump(), item if isinstance(item, dict) else {}, started_at)

    bounded_append(ALERT_HISTORY, {
        "id": str(uuid.uuid4())[:8],
        "type": "scan",
        "alert_name": meta["alert_name"],
        "namespace": meta["namespace"],
        "deployment": meta["deployment"],
        "severity": meta["severity"],
        "message": meta["message"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "result": data,
    }, STORE_LIMIT)

    return {
        **data,
        "scan": {
            "intent": req.intent,
            "namespace": req.namespace,
            "findings": findings[:10],
            "severity": detected_severity,
            "severity_source": "auto" if str(req.severity).lower() in {"", "auto", "自动", "自动识别"} else "manual",
        },
        "incidents": INCIDENTS_STORE[-50:],
        "postmortems": POSTMORTEMS_STORE[-50:],
    }


# ============================================================
# Incidents API
# ============================================================
async def list_incidents():
    return {"incidents": INCIDENTS_STORE[-50:], "total": len(INCIDENTS_STORE)}


async def create_incident(request: Request):
    body = await request.json()
    inc_id = f"INC-{uuid.uuid4().hex[:8]}"
    incident = {
        "incident_id": inc_id,
        "title": body.get("title", "Manual Incident"),
        "severity": body.get("severity", "P2"),
        "namespace": body.get("namespace", "default"),
        "service": body.get("service", ""),
        "summary": body.get("summary", ""),
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    bounded_append(INCIDENTS_STORE, incident, STORE_LIMIT)

    # 同时通知 incident-agent
    try:
        async with _client(10) as c:
            await c.post(SERVICES["incident"], json={
                "id": str(uuid.uuid4()),
                "source_agent": "frontend",
                "target_agent": "incident-agent",
                "task_type": "incident.create",
                "payload": incident,
            })
    except Exception:
        pass

    return incident


# ============================================================
# Post-Mortems API
# ============================================================
async def list_postmortems():
    return {"postmortems": POSTMORTEMS_STORE[-50:], "total": len(POSTMORTEMS_STORE)}


# ============================================================
# Alert History API
# ============================================================
async def list_alerts(limit: int = 50):
    return {"alerts": ALERT_HISTORY[-limit:], "total": len(ALERT_HISTORY)}


# ============================================================
# A2A Traces API
# ============================================================
async def list_traces(limit: int = 20):
    return {"traces": A2A_TRACES[-limit:], "total": len(A2A_TRACES)}


async def llm_observability(limit: int = 50):
    items = LLM_OBSERVABILITY_STORE[-limit:]
    total_tokens = 0
    input_tokens = 0
    output_tokens = 0
    by_day: dict[str, dict] = {}
    by_hour: dict[str, dict] = {}
    by_source: dict[str, int] = {}
    by_model: dict[str, int] = {}
    data_flows: dict[str, int] = {}
    total_cost_usd = 0.0
    langfuse_trace_count = 0
    quality_totals: dict[str, float] = {}
    quality_count = 0
    for item in items:
        usage = ((item.get("llm") or {}).get("token_usage") or {})
        in_tok = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        total_tok = int(usage.get("total_tokens") or (in_tok + out_tok) or 0)
        input_tokens += in_tok
        output_tokens += out_tok
        total_tokens += total_tok
        source = item.get("source") or "unknown"
        llm_block = item.get("llm") or {}
        total_cost_usd += float(llm_block.get("estimated_cost_usd") or 0)
        if item.get("trace_id") or llm_block.get("langfuse_trace_id"):
            langfuse_trace_count += 1
        quality = llm_block.get("quality_scores") or (item.get("output") or {}).get("quality_scores") or {}
        if isinstance(quality, dict) and quality:
            quality_count += 1
            for key, value in quality.items():
                try:
                    quality_totals[key] = quality_totals.get(key, 0.0) + float(value)
                except Exception:
                    pass
        model = llm_block.get("model_profile_id") or llm_block.get("model") or "unknown"
        by_source[source] = by_source.get(source, 0) + 1
        by_model[model] = by_model.get(model, 0) + 1
        ts = str(item.get("timestamp") or "")
        day = ts[:10] or "unknown"
        hour = ts[:13].replace("T", " ") or "unknown"
        by_day.setdefault(day, {"date": day, "calls": 0, "tokens": 0, "failures": 0})
        by_day[day]["calls"] += 1
        by_day[day]["tokens"] += total_tok
        by_day[day]["failures"] += 1 if item.get("status") != "ok" else 0
        by_hour.setdefault(hour, {"hour": hour, "calls": 0, "tokens": 0, "avg_latency_ms": 0, "_latency_total": 0})
        by_hour[hour]["calls"] += 1
        by_hour[hour]["tokens"] += total_tok
        by_hour[hour]["_latency_total"] += int(item.get("latency_ms") or 0)
        for flow in item.get("data_flow") or []:
            key = f"{flow.get('stage', '-')}: {flow.get('name', '-')}"
            data_flows[key] = data_flows.get(key, 0) + 1
    for row in by_hour.values():
        row["avg_latency_ms"] = int(row["_latency_total"] / row["calls"]) if row["calls"] else 0
        row.pop("_latency_total", None)
    failures = [x for x in items if x.get("status") != "ok" or (x.get("output") or {}).get("llm_error")]
    latency_values = [int(x.get("latency_ms") or 0) for x in items]
    latency_values.sort()
    p95 = latency_values[int(len(latency_values) * 0.95) - 1] if latency_values else 0
    observed_days = max(1, len(by_day))
    avg_daily_tokens = int(total_tokens / observed_days) if total_tokens else int(os.getenv("LLM_ESTIMATED_DAILY_BASE_TOKENS", "12000"))
    avg_call_tokens = int(total_tokens / len(items)) if items and total_tokens else int(os.getenv("LLM_ESTIMATED_TOKENS_PER_CALL", "1800"))
    inspection_interval = int(os.getenv("AI_INSPECTION_INTERVAL_MINUTES", "30"))
    weekly_inspection_runs = int(7 * 24 * 60 / max(1, inspection_interval))
    estimated_tokens_per_inspection = int(os.getenv("LLM_ESTIMATED_TOKENS_PER_INSPECTION", str(max(avg_call_tokens * 2, 3000))))
    weekly_without_auto = avg_daily_tokens * 7
    weekly_auto_extra = weekly_inspection_runs * estimated_tokens_per_inspection
    quality_scores = [
        {"name": key, "avg": round(value / max(1, quality_count), 3)}
        for key, value in sorted(quality_totals.items())
    ]
    lf_status = langfuse_status()
    return {
        "status": "ok",
        "enabled": True,
        "langfuse": {
            **lf_status,
            "trace_hierarchy": trace_hierarchy_schema(),
            "score_dimensions": [
                {"id": "root_cause_quality", "name": "根因质量", "meaning": "根因置信度与证据一致性"},
                {"id": "evidence_completeness", "name": "证据完整度", "meaning": "是否采集到日志、事件、状态和上下文"},
                {"id": "actionability", "name": "可执行性", "meaning": "是否给出可验证、可审批、可执行步骤"},
                {"id": "safety_gate", "name": "安全门禁", "meaning": "高风险变更是否进入人工审批和白名单"},
                {"id": "overall", "name": "综合得分", "meaning": "用于模型横向评测的质量基线"},
            ],
        },
        "summary": {
            "total": len(LLM_OBSERVABILITY_STORE),
            "shown": len(items),
            "failures": len(failures),
            "langfuse_traces": langfuse_trace_count,
            "total_tokens": total_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": round(total_cost_usd, 6),
            "avg_latency_ms": int(sum(int(x.get("latency_ms") or 0) for x in items) / len(items)) if items else 0,
            "p95_latency_ms": p95,
            "throughput_per_min": round(len(items) / max(1, ((datetime.now(timezone.utc) - datetime.fromisoformat(items[0]["timestamp"])).total_seconds() / 60)) if items else 0, 2),
        },
        "analytics": {
            "daily_usage": sorted(by_day.values(), key=lambda x: x["date"]),
            "hourly_throughput": sorted(by_hour.values(), key=lambda x: x["hour"])[-24:],
            "by_source": [{"name": k, "calls": v} for k, v in sorted(by_source.items(), key=lambda x: -x[1])],
            "by_model": [{"name": k, "calls": v} for k, v in sorted(by_model.items(), key=lambda x: -x[1])],
            "data_flows": [{"name": k, "count": v} for k, v in sorted(data_flows.items(), key=lambda x: -x[1])[:12]],
            "quality_scores": quality_scores,
            "weekly_analysis": {
                "observed_days": observed_days,
                "avg_daily_tokens": avg_daily_tokens,
                "avg_call_tokens": avg_call_tokens,
                "inspection_interval_minutes": inspection_interval,
                "weekly_inspection_runs": weekly_inspection_runs,
                "estimated_tokens_per_inspection": estimated_tokens_per_inspection,
                "weekly_tokens_without_auto_inspection": weekly_without_auto,
                "weekly_tokens_with_auto_inspection": weekly_without_auto + weekly_auto_extra,
                "auto_inspection_extra_tokens": weekly_auto_extra,
                "difference_ratio": round((weekly_auto_extra / max(1, weekly_without_auto)), 2),
            },
        },
        "items": list(reversed(items)),
    }


# ============================================================
# Unified integrations and observability
# ============================================================
async def _probe_observability_source(base: str, paths: list[str]) -> str:
    if not base:
        return "not_configured"
    async with _client(3) as client:
        for path in paths:
            try:
                response = await client.get(f"{base.rstrip('/')}{path}")
                if response.status_code < 500:
                    return "connected"
            except Exception:
                continue
    return "unreachable"


def _collaboration_configured(channel: str) -> bool:
    checks = {
        "slack": bool(os.getenv("SLACK_WEBHOOK_URL", "").strip()) or bool(os.getenv("SLACK_BOT_TOKEN", "").strip() and os.getenv("SLACK_CHANNEL", "").strip()),
        "telegram": bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHAT_ID", "").strip()),
        "lark": bool(os.getenv("LARK_WEBHOOK_URL", "").strip() or os.getenv("FEISHU_WEBHOOK_URL", "").strip()),
        "dingtalk": bool(os.getenv("DINGTALK_WEBHOOK_URL", "").strip()),
        "wecom": bool(os.getenv("WECOM_WEBHOOK_URL", "").strip()),
        "webhook": bool(os.getenv("OUTBOUND_WEBHOOK_URL", "").strip()),
    }
    return checks.get(channel, False)


async def _send_collaboration_notification(channel: str, message: str) -> None:
    message = message.strip()[:800]
    if not _collaboration_configured(channel):
        raise HTTPException(status_code=409, detail=f"{channel} 通道尚未完整配置")

    url = ""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    payload: dict = {}
    if channel == "slack":
        url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
        if url:
            payload = {"text": message}
        else:
            url = "https://slack.com/api/chat.postMessage"
            headers["Authorization"] = f"Bearer {os.getenv('SLACK_BOT_TOKEN', '').strip()}"
            payload = {"channel": os.getenv("SLACK_CHANNEL", "").strip(), "text": message}
    elif channel == "telegram":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip(), "text": message}
    elif channel == "lark":
        url = os.getenv("LARK_WEBHOOK_URL", "").strip() or os.getenv("FEISHU_WEBHOOK_URL", "").strip()
        payload = {"msg_type": "text", "content": {"text": message}}
    elif channel == "dingtalk":
        url = os.getenv("DINGTALK_WEBHOOK_URL", "").strip()
        payload = {"msgtype": "text", "text": {"content": message}}
    elif channel == "wecom":
        url = os.getenv("WECOM_WEBHOOK_URL", "").strip()
        payload = {"msgtype": "text", "text": {"content": message}}
    elif channel == "webhook":
        url = os.getenv("OUTBOUND_WEBHOOK_URL", "").strip()
        payload = {
            "event": "flawless.integration.test",
            "message": message,
            "source": "flawless-console",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    else:
        raise HTTPException(status_code=400, detail="不支持的协作通道")

    try:
        async with OUTBOUND_BULKHEAD.slot():
            async with _client(10) as client:
                response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        if channel == "slack" and not os.getenv("SLACK_WEBHOOK_URL", "").strip():
            body = response.json()
            if not body.get("ok"):
                raise RuntimeError(f"Slack API 拒绝请求：{str(body.get('error') or 'unknown')[:80]}")
        if channel == "telegram":
            body = response.json()
            if not body.get("ok"):
                raise RuntimeError("Telegram API 拒绝请求")
    except HTTPException:
        raise
    except BulkheadRejected as exc:
        raise HTTPException(status_code=503, detail="通知通道繁忙，请稍后重试") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"通知服务返回 HTTP {exc.response.status_code}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"通知发送失败：{str(exc)[:160]}") from exc


async def integrations_status():
    probe_specs = {
        "prometheus": (SERVICES.get("prometheus", ""), ["/-/ready", "/api/v1/status/buildinfo"]),
        "cmdb": (SERVICES.get("cmdb", ""), ["/ready", "/health"]),
        "loki": (SERVICES.get("loki", ""), ["/ready"]),
        "tempo": (SERVICES.get("tempo", ""), ["/ready", "/status"]),
        "grafana": (SERVICES.get("grafana", ""), ["/api/health"]),
    }
    names = list(probe_specs)
    probed = await asyncio.gather(
        *[_probe_observability_source(*probe_specs[name]) for name in names],
        return_exceptions=True,
    )
    source_status = {
        name: ("unreachable" if isinstance(result, Exception) else result)
        for name, result in zip(names, probed)
    }

    def configured(*names_: str) -> bool:
        return any(bool(os.getenv(name, "").strip()) for name in names_)

    items = [
        {"id": "kubernetes", "name": "Kubernetes MCP", "category": "infrastructure", "status": "connected" if SERVICES.get("mcp") else "not_configured", "capability": "Pod、Workload、Node、网络和存储的只读诊断与受控变更", "configuration_hint": "MCP_SERVER_URL"},
        {"id": "rancher", "name": "Rancher 多集群", "category": "infrastructure", "status": "configured" if _rancher_enabled() else "not_configured", "capability": "自动发现并统一纳管 Rancher 中的全部集群", "configuration_hint": "RANCHER_URL + RANCHER_TOKEN"},
        {"id": "cmdb", "name": "CMDB", "category": "infrastructure", "status": source_status["cmdb"], "capability": "有向资源依赖、业务数据流与爆炸半径上下文", "configuration_hint": "CMDB_URL"},
        {"id": "cloud", "name": "云适配器", "category": "infrastructure", "status": "configured" if configured("CLOUD_ADAPTERS_JSON") and os.getenv("CLOUD_ADAPTERS_JSON", "[]") != "[]" else "not_configured", "capability": "Rancher、Generic CSI Storage、Virtualization Platform、阿里云、AWS 与私有云资源清单", "configuration_hint": "CLOUD_ADAPTERS_JSON"},
        {"id": "prometheus", "name": "Prometheus", "category": "observability", "status": source_status["prometheus"], "capability": "多集群 CPU、内存、重启和可用性指标", "configuration_hint": "PROMETHEUS_URL"},
        {"id": "loki", "name": "Loki", "category": "observability", "status": source_status["loki"], "capability": "LogQL 日志检索与 RCA 证据关联", "configuration_hint": "LOKI_URL"},
        {"id": "tempo", "name": "Tempo", "category": "observability", "status": source_status["tempo"], "capability": "TraceQL 链路检索与跨服务因果证据", "configuration_hint": "TEMPO_URL"},
        {"id": "grafana", "name": "Grafana", "category": "observability", "status": source_status["grafana"], "capability": "指标、日志与链路的深度探索面板", "configuration_hint": "GRAFANA_URL"},
        {"id": "langfuse", "name": "Langfuse", "category": "observability", "status": "configured" if _env_bool("LANGFUSE_ENABLED", "false") and configured("LANGFUSE_HOST") else "not_configured", "capability": "模型调用、Token、延迟、失败与提示词链路", "configuration_hint": "LANGFUSE_ENABLED + LANGFUSE_HOST"},
        {"id": "slack", "name": "Slack", "category": "collaboration", "status": "configured" if _collaboration_configured("slack") else "not_configured", "capability": "告警通知、诊断回填与审批入口", "configuration_hint": "SLACK_WEBHOOK_URL 或 SLACK_BOT_TOKEN + SLACK_CHANNEL"},
        {"id": "telegram", "name": "Telegram", "category": "collaboration", "status": "configured" if _collaboration_configured("telegram") else "not_configured", "capability": "告警通知与运维会话回填", "configuration_hint": "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID"},
        {"id": "lark", "name": "飞书 / Lark", "category": "collaboration", "status": "configured" if _collaboration_configured("lark") else "not_configured", "capability": "事件卡片、诊断回填和人工审批", "configuration_hint": "LARK_WEBHOOK_URL"},
        {"id": "dingtalk", "name": "钉钉", "category": "collaboration", "status": "configured" if _collaboration_configured("dingtalk") else "not_configured", "capability": "国内企业告警与审批通道", "configuration_hint": "DINGTALK_WEBHOOK_URL"},
        {"id": "wecom", "name": "企业微信", "category": "collaboration", "status": "configured" if _collaboration_configured("wecom") else "not_configured", "capability": "企业微信通知与事件协作", "configuration_hint": "WECOM_WEBHOOK_URL"},
        {"id": "webhook", "name": "Webhook", "category": "collaboration", "status": "configured" if _collaboration_configured("webhook") else "not_configured", "capability": "对接组织内部流程、工单和消息总线", "configuration_hint": "OUTBOUND_WEBHOOK_URL"},
        {"id": "llm", "name": "可插拔模型", "category": "ai", "status": "configured" if configured("LLM_API_BASE", "LLM_GATEWAY_BASE", "MODEL_PROFILES_JSON") else "not_configured", "capability": "OAuth 动态 Token 或 API Key；支持热切换和影子测评", "configuration_hint": "MODEL_PROFILES_JSON"},
        {"id": "rag", "name": "运维 RAG", "category": "ai", "status": "connected", "capability": "产品知识、Runbook、历史事件和模型上下文检索", "configuration_hint": "内置知识源，可扩展向量库"},
    ]
    coverage = [
        {"capability": "Coordinator + 专家 Agent", "status": "ready", "detail": "SRE Graph 按告警类型编排观测、事件、修复和复盘 Agent"},
        {"capability": "告警自动调查与 RCA", "status": "ready", "detail": "告警触发证据采集、根因判断、事件和复盘闭环"},
        {"capability": "指标 / 日志 / 链路关联", "status": "ready" if source_status["loki"] == "connected" and source_status["tempo"] == "connected" else "partial", "detail": "Prometheus 已内置；Loki 与 Tempo 按环境启用"},
        {"capability": "多集群 Kubernetes", "status": "ready", "detail": "Rancher 全集群资源、巡检、指标、拓扑和修复范围"},
        {"capability": "受控远程执行", "status": "ready", "detail": "拒绝任意 Shell；使用动作目录、dry-run、风险门禁、审批和恢复验证"},
        {"capability": "可插拔模型与热路由", "status": "ready", "detail": "企业 OAuth 网关、OpenAI-compatible API Key、影子评测"},
        {"capability": "运维知识库与代码检索", "status": "ready", "detail": "应用手册、Runbook 和故障知识统一 RAG"},
        {"capability": "IM 协作通道", "status": "configured" if any(item["category"] == "collaboration" and item["status"] == "configured" for item in items) else "optional", "detail": "Slack、Telegram、飞书、钉钉、企业微信和 Webhook"},
        {"capability": "算法化爆炸半径与变更门禁", "status": "ready", "detail": "有向拓扑传播、Amp 放大系数、错误预算安全包络"},
        {"capability": "模型运维效果量化", "status": "ready", "detail": "按模型记录诊断、变更成功率、恢复 Pod 和风险降低率"},
    ]
    return {"status": "ok", "items": items, "coverage": coverage}


async def test_collaboration_notification(req: CollaborationNotificationRequest):
    channel = req.channel.strip().lower()
    if channel not in {"slack", "telegram", "lark", "dingtalk", "wecom", "webhook"}:
        raise HTTPException(status_code=400, detail="不支持的协作通道")
    await _send_collaboration_notification(channel, req.message)
    return {"status": "ok", "channel": channel, "delivery_id": f"notify-{uuid.uuid4().hex[:12]}"}


async def query_loki_logs(request: Request):
    base = SERVICES.get("loki", "").rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="LOKI_URL 未配置")
    body = await request.json()
    query = str(body.get("query") or "").strip()
    if not query or len(query) > 500 or "\n" in query or "\r" in query:
        raise HTTPException(status_code=400, detail="LogQL 查询为空或超出限制")
    limit = max(1, min(200, int(body.get("limit") or 80)))
    end_ns = int(time.time() * 1_000_000_000)
    start_ns = end_ns - max(60, min(86400, int(body.get("range_seconds") or 3600))) * 1_000_000_000
    async with _client(12) as client:
        response = await client.get(
            f"{base}/loki/api/v1/query_range",
            params={"query": query, "start": start_ns, "end": end_ns, "limit": limit, "direction": "backward"},
        )
        response.raise_for_status()
        payload = response.json()
    streams = ((payload.get("data") or {}).get("result") or []) if isinstance(payload, dict) else []
    return {"status": "ok", "source": "loki", "query": query, "streams": _redact_sensitive(streams)}


async def query_tempo_traces(service: str = "", limit: int = 20):
    base = SERVICES.get("tempo", "").rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="TEMPO_URL 未配置")
    safe_service = re.sub(r"[^A-Za-z0-9_.:/-]", "", service)[:120]
    params = {"limit": max(1, min(100, limit))}
    if safe_service:
        params["tags"] = f"service.name={safe_service}"
    async with _client(12) as client:
        response = await client.get(f"{base}/api/search", params=params)
        response.raise_for_status()
        payload = response.json()
    return {"status": "ok", "source": "tempo", "traces": _redact_sensitive(payload.get("traces") or [])}


# ============================================================
# Dashboard Stats
# ============================================================
async def dashboard():
    """聚合仪表盘数据"""
    # 尝试获取真实 K8s 数据
    pods_data = None
    nodes_data = None
    try:
        async with _client(10) as c:
            pods_resp = await c.post(_mcp_tools_url(), json={"tool": "list_pods", "arguments": {"namespace": "default"}}, headers=_internal_headers())
            pods_data = pods_resp.json()
    except Exception:
        pass

    try:
        async with _client(10) as c:
            nodes_resp = await c.post(_mcp_tools_url(), json={"tool": "list_nodes", "arguments": {}}, headers=_internal_headers())
            nodes_data = nodes_resp.json()
    except Exception:
        pass

    # 统计
    pod_stats = {"total": 0, "running": 0, "pending": 0, "failed": 0, "unknown": 0}
    if pods_data:
        for p in pods_data.get("pods", []):
            pod_stats["total"] += 1
            phase = p.get("phase", "Unknown")
            if phase == "Running":
                pod_stats["running"] += 1
            elif phase == "Pending":
                pod_stats["pending"] += 1
            elif phase in ("Failed", "Error", "CrashLoopBackOff"):
                pod_stats["failed"] += 1
            else:
                pod_stats["unknown"] += 1

    node_stats = {"total": 0, "ready": 0, "not_ready": 0}
    if nodes_data:
        for n in nodes_data.get("nodes", []):
            node_stats["total"] += 1
            conditions = n.get("conditions", [])
            ready = any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)
            if ready:
                node_stats["ready"] += 1
            else:
                node_stats["not_ready"] += 1

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pods": pod_stats,
        "nodes": node_stats,
        "incidents_open": sum(1 for i in INCIDENTS_STORE if i.get("status") == "open"),
        "incidents_total": len(INCIDENTS_STORE),
        "alerts_total": len(ALERT_HISTORY),
        "postmortems_total": len(POSTMORTEMS_STORE),
    }


# ============================================================
# Fallback
# ============================================================
def _fallback_diagnosis_response(req: ChatRequest) -> dict:
    """Return an evidence-only degraded plan when the SRE dependency is unavailable."""
    target = f"{req.workload_type}/{req.deployment}" if req.deployment else "待绑定 Workload"
    remediation_plan = {
        "id": f"degraded-evidence-{uuid.uuid4().hex[:8]}",
        "title": "SRE 依赖异常：先恢复证据链并重新诊断",
        "namespace": req.namespace,
        "target": target,
        "summary": "当前 SRE Adapter/Agent/LLM 调用失败，尚无足够证据确定根因；本计划仅取证，不执行任何变更。",
        "steps": [
            {"id": "current_logs", "title": "读取 current logs", "description": "确认当前容器是否产生了可用日志，并保留读取失败原因。", "status": "pending"},
            {"id": "previous_logs", "title": "读取 previous logs", "description": "若容器发生重启，读取上一次退出前日志；没有日志时明确记录证据缺口。", "status": "pending"},
            {"id": "events", "title": "读取 Pod Events", "description": "区分镜像、调度、挂载、配置引用、探针和节点侧失败。", "status": "pending"},
            {"id": "workload_spec", "title": "读取 Workload 实际模板", "description": "核对镜像、探针、资源、securityContext、卷、ServiceAccount 和最近 generation。", "status": "pending"},
            {"id": "service_endpoints", "title": "读取服务与依赖证据", "description": "核对 Service/Endpoint，并在适用时检查 PVC/PV、节点和外部依赖。", "status": "pending"},
        ],
        "changes": [],
        "decision": "evidence_required",
        "success_criteria": ["pod_ready", "rollout_complete", "restart_count_stable", "events_no_new_backoff"],
        "requires_confirmation": False,
        "source": "dependency_failure_evidence_plan",
    }
    return {
        "answer": (
            f"SRE 诊断依赖当前不可用，系统没有获得足以判断 {target} 根因的实时证据，"
            "因此不会给出猜测性修复或声称已经恢复。已生成只读取证计划；恢复 Adapter/Agent/LLM 连通性后，"
            "请运行该计划并基于新日志、Events、Workload、存储与依赖证据重新匹配运维 Skill。"
        ),
        "raw": {
            "diagnosis": {
                "root_cause": "诊断依赖不可用；业务根因尚未确定",
                "impact": "证据不足，影响范围待实时对象状态确认",
                "confidence": 0,
                "suggested_action": "investigate",
                "immediate_actions": [
                    "读取当前与上一次容器日志",
                    "检查 Pod Events 与 Workload 模板",
                    "检查 Service/Endpoint、PVC/PV 与节点状态",
                ],
                "proposed_changes": [],
                "need_human_approval": True,
                "remediation_plan": remediation_plan,
                "diagnosis_metadata": {"source": "dependency_failure", "evidence_complete": False},
            },
            "decision": {
                "action": "investigate",
                "require_human_approval": True,
                "dry_run": True,
                "diagnostic_actions": ["current_logs", "previous_logs", "events", "workload_spec", "service_endpoints", "storage_chain"],
                "target": {
                    "namespace": req.namespace,
                    "workload_type": req.workload_type,
                    "workload_name": req.deployment or "unknown",
                },
            },
            "alert": {
                "cluster": req.cluster,
                "cluster_id": req.cluster_id,
                "namespace": req.namespace,
                "workload_type": req.workload_type,
                "workload_name": req.deployment,
            },
        },
    }


async def _submit_release_job(release: dict, actor: str) -> dict:
    """兼容旧调用点，实际转换逻辑位于独立发布执行服务。"""
    return await submit_release_job(release, actor, _enqueue_ops_job)


# ============================================================
# 功能路由装配
# ============================================================
# application.py 只在这里统一装配功能路由。每个 URL 的归属位于
# backend/app/api/features，新增接口时不得再直接使用 @app 装饰器。
from backend.app.api.features.algorithms import build_router as build_algorithms_router
from backend.app.api.features.chat import build_router as build_chat_router
from backend.app.api.features.inventory import build_router as build_inventory_router
from backend.app.api.features.knowledge import build_router as build_knowledge_router
from backend.app.api.features.models import build_router as build_models_router
from backend.app.api.features.observability import build_router as build_observability_router
from backend.app.api.features.operations import build_router as build_operations_router
from backend.app.api.features.records import build_router as build_records_router
from backend.app.api.features.system import build_router as build_system_router
from backend.app.api.features.topology import build_router as build_topology_router


_RUNTIME_HANDLERS = globals()
for _feature_router in (
    build_system_router(_RUNTIME_HANDLERS),
    build_chat_router(_RUNTIME_HANDLERS),
    build_knowledge_router(_RUNTIME_HANDLERS),
    build_models_router(_RUNTIME_HANDLERS),
    build_algorithms_router(_RUNTIME_HANDLERS),
    build_topology_router(_RUNTIME_HANDLERS),
    build_inventory_router(_RUNTIME_HANDLERS),
    build_operations_router(_RUNTIME_HANDLERS),
    build_records_router(_RUNTIME_HANDLERS),
    build_observability_router(_RUNTIME_HANDLERS),
):
    app.include_router(_feature_router)


app.include_router(build_reliability_router(ReliabilityDependencies(
    store=RELIABILITY_STORE,
    gate_evaluator=evaluate_release_gate,
    submit_release=_submit_release_job,
)))


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("FRONTEND_PORT", "8080"))
    print(f"""
╔══════════════════════════════════════════════════╗
║     flawless Control Plane API                ║
║     访问: http://localhost:{port}                    ║
╚══════════════════════════════════════════════════╝
""")
    uvicorn.run(app, host="0.0.0.0", port=port)

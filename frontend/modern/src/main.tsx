import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  BookOpen,
  Bot,
  Boxes,
  BrainCircuit,
  Beaker,
  ChevronRight,
  CheckCircle2,
  CircleDot,
  Cable,
  Database,
  Eye,
  FileUp,
  Gauge,
  GitBranch,
  KeyRound,
  LineChart,
  LayoutDashboard,
  Loader2,
  MessageSquareText,
  Moon,
  Network,
  PackageSearch,
  Play,
  RefreshCcw,
  Copy,
  Save,
  Search,
  Send,
  Square,
  Settings2,
  ShieldCheck,
  Sparkles,
  Sun,
  TerminalSquare,
  Wrench,
  Workflow,
  ZoomIn,
  ZoomOut
} from "lucide-react";
import { Topology2D } from "./Topology2D";
import { OpsJobProgress, OpsPlanPanel } from "./components/OpsPlanPanel";
import { useAsync } from "./hooks/useAsync";
import {
  ApiState,
  adminAuthHeaders,
  apiGet,
  apiPost,
  asList,
  clearAdminCredentials,
  makeId,
  markdownToHtml,
  preloadApplicationResources,
  setAdminCredentials,
} from "./lib/api";
import {
  AlgorithmsPage,
  AssistantDock,
  DashboardPage,
  InfrastructurePage,
  IntegrationsPage,
  OpsSkillsPage,
  OperationsPage,
  ResourcesPage,
  SignalsPage,
} from "./UnifiedPages";
import "./styles.css";

type PageKey = "chat" | "inspection" | "topology" | "dashboard" | "opsHub" | "skills" | "reliability" | "effectiveness" | "platform";
type Theme = "light" | "dark";

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  status?: "streaming" | "done" | "stopped" | "error";
  activity?: Array<string | { stage?: string; message: string }>;
  data?: any;
  target?: ChatTarget;
};

type ChatTarget = {
  cluster: string;
  cluster_id: string;
  namespace: string;
  workload_type: string;
  workload_name: string;
  pod_name?: string;
  target_id: string;
};

type ModelProfile = {
  id: string;
  provider: string;
  model: string;
  base_url: string;
  auth_type: string;
  role?: string;
  enabled?: boolean;
  description?: string;
};

const navItems = [
  { key: "chat", label: "SRE 对话", group: "核心", icon: MessageSquareText },
  { key: "inspection", label: "AI 巡检", group: "核心", icon: Search },
  { key: "topology", label: "拓扑影响", group: "核心", icon: Network },
  { key: "dashboard", label: "运行总览", group: "运维闭环", icon: LayoutDashboard },
  { key: "opsHub", label: "资源事件", group: "运维闭环", icon: PackageSearch },
  { key: "skills", label: "Skill 库", group: "运维闭环", icon: BrainCircuit },
  { key: "reliability", label: "发布治理", group: "运维", icon: ShieldCheck },
  { key: "effectiveness", label: "运维成效", group: "运维", icon: LineChart },
  { key: "platform", label: "平台能力", group: "平台", icon: Settings2 }
] as const;

const quickPrompts = [
  "值班巡检：先看所有 P0/P1 异常",
  "定位 CrashLoop 根因并生成可执行修复",
  "检查最近发布后的异常影响面"
];

function cx(...items: Array<string | false | undefined>) {
  return items.filter(Boolean).join(" ");
}

function canCreateWebGLContext() {
  try {
    if (new URLSearchParams(window.location.search).get("webgl") === "off") return false;
    const probe = document.createElement("canvas");
    const context = probe.getContext("webgl2") || probe.getContext("webgl");
    if (!context) return false;
    context.getExtension("WEBGL_lose_context")?.loseContext();
    return true;
  } catch {
    return false;
  }
}

function webGLFailureDetail(error: unknown) {
  const detail = error instanceof Error ? error.message : String(error);
  return /webgl|context/i.test(detail)
    ? "当前浏览器无法创建 WebGL 上下文，已自动使用 2D 兼容视图。请检查浏览器硬件加速、GPU 策略或远程桌面图形能力。"
    : `3D 渲染初始化失败，已自动使用 2D 兼容视图：${detail}`;
}

function prettyNumber(value: unknown) {
  const n = Number(value || 0);
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(Math.round(n * 100) / 100);
}

function markdownish(text: string) {
  return markdownToHtml(text);
}

function App() {
  const [page, setPage] = useState<PageKey>("chat");
  const [visited, setVisited] = useState<Set<PageKey>>(() => new Set<PageKey>(["chat"]));
  const [theme, setTheme] = useState<Theme>(() => (localStorage.getItem("flawless-theme") as Theme) || "light");
  const [health, refreshHealth] = useAsync<any>(() => apiGet("/api/health"), []);
  const [build] = useAsync<any>(() => apiGet("/api/build"), []);
  const [session, refreshSession] = useAsync<any>(() => apiGet(`/api/session?ts=${Date.now()}`), []);
  const [registry, refreshRegistry] = useAsync<any>(() => apiGet("/api/model-registry"), []);
  const [activeModelId, setActiveModelId] = useState(() => localStorage.getItem("flawless-active-model") || "");
  const [adminDialog, setAdminDialog] = useState(false);
  const [adminUser, setAdminUser] = useState("admin");
  const [adminPassword, setAdminPassword] = useState("");
  const [adminError, setAdminError] = useState("");
  const [adminBusy, setAdminBusy] = useState(false);

  useEffect(() => {
    document.body.dataset.theme = theme;
    localStorage.setItem("flawless-theme", theme);
  }, [theme]);

  useEffect(() => {
    document.body.dataset.admin = session.data?.role === "admin" ? "true" : "false";
  }, [session.data?.role]);

  useEffect(() => {
    preloadApplicationResources();
  }, []);

  useEffect(() => {
    setVisited((current) => current.has(page) ? current : new Set([...current, page]));
  }, [page]);

  useEffect(() => {
    const next = registry.data?.active_profile_id || registry.data?.profiles?.[0]?.id || "";
    if (next && !activeModelId) setActiveModelId(next);
  }, [registry.data, activeModelId]);

  async function activateModel(profileId: string) {
    setActiveModelId(profileId);
    localStorage.setItem("flawless-active-model", profileId);
    try {
      await apiPost("/api/model-registry/active", { profile_id: profileId });
      refreshRegistry();
    } catch {
      // Keep the UI usable for read-only deployments; the request body still carries the selected profile id.
    }
  }

  async function enterAdminMode() {
    setAdminBusy(true);
    setAdminError("");
    setAdminCredentials(adminUser.trim(), adminPassword);
    try {
      const verified = await apiGet<any>(`/api/session?verify=${Date.now()}`);
      if (verified.role !== "admin") throw new Error(verified.admin_mode ? "用户名或密码不正确" : "服务端 CONSOLE_ADMIN_MODE 尚未开启");
      setAdminPassword("");
      setAdminDialog(false);
      refreshSession();
      refreshRegistry();
    } catch (error: any) {
      clearAdminCredentials();
      setAdminError(error.message || "管理员身份校验失败");
    } finally {
      setAdminBusy(false);
    }
  }

  function leaveAdminMode() {
    clearAdminCredentials();
    refreshSession();
    refreshRegistry();
  }

  const serviceCount = useMemo(() => {
    const services = health.data?.services || {};
    const total = Object.keys(services).length;
    const up = Object.values(services).filter((item: any) => item?.status === "up").length;
    return { up, total };
  }, [health.data]);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">Flawless</div>
          <div>
            <strong>SRE Console</strong>
            <span>{build.data?.version || "production control plane"}</span>
          </div>
        </div>
        <nav className="nav">
          {navItems.map((item, index) => {
            const Icon = item.icon;
            return (
              <React.Fragment key={item.key}>
                {(index === 0 || navItems[index - 1].group !== item.group) && <span className="nav-section-label">{item.group}</span>}
                <button
                  className={cx("nav-button", page === item.key && "active")}
                  onClick={() => React.startTransition(() => setPage(item.key as PageKey))}
                  aria-label={item.label}
                  title={item.label}
                >
                  <Icon size={17} />
                  <strong>{item.label}</strong>
                </button>
              </React.Fragment>
            );
          })}
        </nav>
        <div className="side-card">
          <div className="side-row">
            <span>Agents</span>
            <strong>{health.loading ? "--" : `${serviceCount.up}/${serviceCount.total}`}</strong>
          </div>
          <div className="health-dots">
            {Object.entries(health.data?.services || {}).slice(0, 8).map(([name, value]: [string, any]) => (
              <span key={name} className={cx("health-dot", value?.status === "up" ? "ok" : "warn")} title={name} />
            ))}
          </div>
          <button className="ghost tiny" onClick={refreshHealth}><RefreshCcw size={13} />刷新状态</button>
        </div>
        <div className="author-watermark" title="Created by the maintainer">
          <span>Created by</span>
          <strong>the maintainer</strong>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <h1>{navItems.find((x) => x.key === page)?.label}</h1>
          <div className="top-actions">
            <label className="top-model">
              <span>当前模型</span>
              <select value={activeModelId} onChange={(e) => activateModel(e.target.value)}>
                {asList(registry.data?.profiles).filter((p: ModelProfile) => p.enabled !== false).map((profile: ModelProfile) => (
                  <option key={profile.id} value={profile.id}>{profile.id}</option>
                ))}
              </select>
            </label>
            <button className="ghost" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}> 
              {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
              {theme === "dark" ? "日间" : "夜间"}
            </button>
            <button
              className={cx("ghost", session.data?.role === "admin" && "admin-active")}
              onClick={() => session.data?.role === "admin" ? leaveAdminMode() : setAdminDialog(true)}
              title={session.data?.role === "admin" ? "退出管理员配置模式" : "进入管理员配置模式"}
            >
              <KeyRound size={16} />
              {session.data?.role === "admin" ? "Admin 已启用" : "管理员"}
            </button>
          </div>
        </header>
        <div className="page-stack">
          {visited.has("chat") && <div className={cx("page-layer", page === "chat" && "active")}><ChatPage activeModelId={activeModelId} /></div>}
          {visited.has("dashboard") && <div className={cx("page-layer", page === "dashboard" && "active")}><DashboardPage /></div>}
          {visited.has("inspection") && <div className={cx("page-layer", page === "inspection" && "active")}><InspectionPage activeModelId={activeModelId} /></div>}
          {visited.has("topology") && <div className={cx("page-layer", page === "topology" && "active")}><TopologyPage /></div>}
          {visited.has("opsHub") && <div className={cx("page-layer", page === "opsHub" && "active")}><OpsHubPage /></div>}
          {visited.has("skills") && <div className={cx("page-layer", page === "skills" && "active")}><OpsSkillsPage /></div>}
          {visited.has("reliability") && <div className={cx("page-layer", page === "reliability" && "active")}><ReliabilityPage /></div>}
          {visited.has("effectiveness") && <div className={cx("page-layer", page === "effectiveness" && "active")}><EffectivenessPage /></div>}
          {visited.has("platform") && <div className={cx("page-layer", page === "platform" && "active")}><PlatformPage activeModelId={activeModelId} onActivate={activateModel} refreshRegistry={refreshRegistry} registry={registry} /></div>}
        </div>
      </main>
      <AssistantDock page={navItems.find((item) => item.key === page)?.label || page} />
      {adminDialog && <div className="admin-dialog-backdrop" role="presentation" onMouseDown={() => setAdminDialog(false)}>
        <section className="admin-dialog" role="dialog" aria-modal="true" aria-label="管理员配置模式" onMouseDown={(event) => event.stopPropagation()}>
          <header><KeyRound size={18} /><div><strong>管理员配置模式</strong><span>凭据只保存在当前浏览器会话，不写入前端配置或仓库。</span></div></header>
          <label>用户名<input value={adminUser} onChange={(event) => setAdminUser(event.target.value)} autoComplete="username" /></label>
          <label>密码<input type="password" value={adminPassword} onChange={(event) => setAdminPassword(event.target.value)} autoComplete="current-password" onKeyDown={(event) => { if (event.key === "Enter") void enterAdminMode(); }} /></label>
          {adminError && <div className="inline-error">{adminError}</div>}
          <footer><button className="ghost" onClick={() => setAdminDialog(false)}>取消</button><button className="primary" disabled={adminBusy || !adminUser || !adminPassword} onClick={enterAdminMode}>{adminBusy ? <Loader2 className="spin" size={15} /> : <ShieldCheck size={15} />}验证并进入</button></footer>
        </section>
      </div>}
    </div>
  );
}

function OpsHubPage() {
  const [tab, setTab] = useState<"resources" | "operations">("resources");
  return (
    <section className="hub-page">
      <div className="hub-tabs">
        <button className={tab === "resources" ? "active" : ""} onClick={() => setTab("resources")}><PackageSearch size={15} />资源浏览</button>
        <button className={tab === "operations" ? "active" : ""} onClick={() => setTab("operations")}><Wrench size={15} />事件与工具</button>
      </div>
      {tab === "resources" ? <ResourcesPage /> : <OperationsPage />}
    </section>
  );
}

function PlatformPage({
  activeModelId,
  onActivate,
  refreshRegistry,
  registry,
}: {
  activeModelId: string;
  onActivate: (profileId: string) => Promise<void>;
  refreshRegistry: () => void;
  registry: ApiState<any>;
}) {
  const [tab, setTab] = useState<"models" | "knowledge" | "observability" | "algorithms" | "infrastructure" | "integrations">("models");
  return (
    <section className="hub-page">
      <div className="hub-tabs platform-tabs">
        <button className={tab === "models" ? "active" : ""} onClick={() => setTab("models")}><Beaker size={15} />模型实验室</button>
        <button className={tab === "knowledge" ? "active" : ""} onClick={() => setTab("knowledge")}><BookOpen size={15} />知识库</button>
        <button className={tab === "observability" ? "active" : ""} onClick={() => setTab("observability")}><Activity size={15} />可观测</button>
        <button className={tab === "algorithms" ? "active" : ""} onClick={() => setTab("algorithms")}><Workflow size={15} />算法决策</button>
        <button className={tab === "infrastructure" ? "active" : ""} onClick={() => setTab("infrastructure")}><Database size={15} />全栈资源</button>
        <button className={tab === "integrations" ? "active" : ""} onClick={() => setTab("integrations")}><Cable size={15} />集成</button>
      </div>
      {tab === "models" && <ModelLabPage activeModelId={activeModelId} onActivate={onActivate} refreshRegistry={refreshRegistry} registry={registry} />}
      {tab === "knowledge" && <KnowledgePage activeModelId={activeModelId} />}
      {tab === "observability" && <SignalsPage />}
      {tab === "algorithms" && <AlgorithmsPage />}
      {tab === "infrastructure" && <InfrastructurePage activeModelId={activeModelId} />}
      {tab === "integrations" && <IntegrationsPage />}
    </section>
  );
}

function ChatPage({ activeModelId }: { activeModelId: string }) {
  const [input, setInput] = useState("");
  const [cluster, setCluster] = useState("all");
  const [namespace, setNamespace] = useState("all");
  const [workload, setWorkload] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [riskRankLoading, setRiskRankLoading] = useState(false);
  const [riskRankOrder, setRiskRankOrder] = useState<string[]>([]);
  const [riskRankRationales, setRiskRankRationales] = useState<Record<string, string>>({});
  const [riskRankSource, setRiskRankSource] = useState("");
  const conversationRef = useRef<HTMLDivElement | null>(null);
  const streamControllerRef = useRef<AbortController | null>(null);
  const [inventory, refreshInventory] = useAsync<any>(() => apiGet("/api/rancher/inventory").catch(() => ({ clusters: [], inventory: [] })), []);
  const clusters = asList(inventory.data?.clusters);
  const selectedCluster = clusters.find((item: any) => cluster === item.id || cluster === item.name);
  const selectedClusterLabel = cluster === "all" ? "所有集群" : selectedCluster?.name || selectedCluster?.id || cluster;
  const scopedInventory = useMemo(() => cluster === "all" ? asList(inventory.data?.inventory) : asList(inventory.data?.inventory).filter((item: any) => cluster === item.cluster?.id || cluster === item.cluster?.name), [cluster, inventory.data]);
  const namespaces = useMemo(() => Array.from(new Set(scopedInventory.flatMap((item: any) => asList(item.namespaces).map((entry: any) => String(entry.name))))).sort(), [scopedInventory]);
  const workloads = useMemo(() => scopedInventory.flatMap((scope: any) => asList(scope.workloads).map((item: any) => ({
    ...item,
    cluster: item.cluster || scope.cluster?.name || scope.cluster?.id || "local-cluster",
    cluster_id: item.cluster_id || scope.cluster?.id || item.cluster || "local",
  }))).filter((item: any) => namespace === "all" || item.namespace === namespace), [namespace, scopedInventory]);
  const workloadIdentity = (item: any) => `${item.cluster_id || item.cluster}|${item.namespace || "default"}|${item.kind || "Workload"}|${item.name}`;
  const selectedWorkload = useMemo(() => workloads.find((item: any) => workloadIdentity(item) === workload), [workload, workloads]);
  const selectedWorkloadName = selectedWorkload?.name || "";
  const workloadInScope = (pod: any) => !selectedWorkloadName
    || pod.workload_name === selectedWorkloadName
    || pod.workload?.name === selectedWorkloadName
    || pod.owner_name === selectedWorkloadName
    || String(pod.name || "").startsWith(`${selectedWorkloadName}-`);
  const problemPods = useMemo(() => scopedInventory
    .flatMap((item: any) => asList(item.pods).map((pod: any) => ({ ...pod, cluster: pod.cluster || item.cluster?.name || item.cluster?.id })))
    .filter((pod: any) => {
      const phase = String(pod.phase || "");
      const completed = phase === "Succeeded" || phase === "Completed";
      return (namespace === "all" || pod.namespace === namespace)
        && workloadInScope(pod)
        && !completed
        && (pod.issue || !pod.ready || Number(pod.restart_count || 0) > 5);
    }), [namespace, scopedInventory, selectedWorkloadName]);
  const riskyWorkloads = useMemo(() => workloads
    .filter((item: any) => !selectedWorkloadName || item.name === selectedWorkloadName)
    .filter((item: any) => {
      const replicas = Number(item.replicas || 0);
      return replicas > 0 && Number(item.ready_replicas || 0) < replicas;
    }), [selectedWorkloadName, workloads]);
  const topRisks = useMemo(() => {
    const rank: Record<string, number> = { P0: 500, critical: 480, P1: 400, high: 380, P2: 300, medium: 280, P3: 200, low: 100 };
    const grouped = new Map<string, any>();
    const severityOf = (value: any, fallback = "P2") => {
      const text = String(value || fallback);
      return /^p[0-3]$/i.test(text) ? text.toUpperCase() : text.toLowerCase();
    };
    const upsert = (item: any) => {
      const existing = grouped.get(item.key);
      if (!existing) { grouped.set(item.key, item); return; }
      existing.score = Math.max(existing.score, item.score);
      existing.restart_count = Math.max(existing.restart_count || 0, item.restart_count || 0);
      existing.reasons = Array.from(new Set([...asList(existing.reasons), ...asList(item.reasons)])).slice(0, 3);
      existing.pods = Array.from(new Set([...asList(existing.pods), ...asList(item.pods)])).slice(0, 8);
      if ((rank[item.severity] || 0) > (rank[existing.severity] || 0)) existing.severity = item.severity;
    };

    riskyWorkloads.forEach((item: any) => {
      const replicas = Number(item.replicas || 0);
      const ready = Number(item.ready_replicas || 0);
      const severity = severityOf(item.issue?.severity, ready === 0 && replicas > 0 ? "P1" : "P2");
      upsert({
        key: `workload:${item.cluster}:${item.namespace}:${item.kind}:${item.name}`,
        type: "workload",
        cluster: item.cluster,
        cluster_id: item.cluster_id,
        namespace: item.namespace || "default",
        kind: item.kind || "Workload",
        name: item.name,
        severity,
        ready_replicas: ready,
        replicas,
        restart_count: 0,
        reasons: [`${ready}/${replicas} Ready`],
        pods: [],
        score: (rank[severity] || 0) + Math.max(0, replicas - ready) * 18,
      });
    });

    problemPods.forEach((pod: any) => {
      const kind = String(pod.workload_kind || pod.workload?.kind || "Workload");
      const owner = String(pod.workload_name || pod.workload?.name || pod.owner_name || "");
      const hasWorkload = Boolean(owner) && kind.toLowerCase() !== "pod";
      const severity = severityOf(pod.issue?.severity, /crash|oom|imagepull/i.test(String(pod.issue?.reason || pod.phase || "")) ? "P1" : "P2");
      const reason = String(pod.issue?.reason || pod.phase || "NotReady");
      const restartCount = Number(pod.restart_count || 0);
      upsert({
        key: hasWorkload ? `workload:${pod.cluster}:${pod.namespace}:${kind}:${owner}` : `pod:${pod.cluster}:${pod.namespace}:${pod.name}`,
        type: hasWorkload ? "workload" : "pod",
        cluster: pod.cluster,
        cluster_id: pod.cluster_id || pod.cluster,
        namespace: pod.namespace || "default",
        kind: hasWorkload ? kind : "Pod",
        name: hasWorkload ? owner : pod.name,
        severity,
        ready_replicas: hasWorkload ? 0 : undefined,
        replicas: hasWorkload ? 1 : undefined,
        restart_count: restartCount,
        reasons: [reason],
        pods: [pod.name],
        score: (rank[severity] || 0) + Math.min(80, restartCount * 4) + (!pod.ready ? 20 : 0),
      });
    });
    return Array.from(grouped.values()).sort((a, b) => b.score - a.score || a.name.localeCompare(b.name)).slice(0, 6);
  }, [problemPods, riskyWorkloads]);
  const displayedRisks = useMemo(() => {
    if (!riskRankOrder.length) return topRisks;
    const byKey = new Map(topRisks.map((item: any) => [String(item.key), item]));
    const ordered = riskRankOrder.map((key) => byKey.get(key)).filter(Boolean);
    const orderedKeys = new Set(riskRankOrder);
    return [...ordered, ...topRisks.filter((item: any) => !orderedKeys.has(String(item.key)))];
  }, [riskRankOrder, topRisks]);

  useEffect(() => {
    setRiskRankOrder([]);
    setRiskRankRationales({});
    setRiskRankSource("");
  }, [cluster, namespace, selectedWorkloadName]);

  async function rerankRisks() {
    if (!topRisks.length || riskRankLoading) return;
    setRiskRankLoading(true);
    try {
      const response: any = await apiPost("/api/chat/risk-rank", {
        cluster,
        namespace,
        model_profile_id: activeModelId,
        risks: topRisks.map((item: any) => ({
          key: item.key,
          type: item.type,
          cluster: item.cluster,
          namespace: item.namespace,
          kind: item.kind,
          name: item.name,
          severity: item.severity,
          score: item.score,
          replicas: item.replicas,
          ready_replicas: item.ready_replicas,
          restart_count: item.restart_count,
          reasons: item.reasons,
          pods: item.pods,
        })),
      });
      setRiskRankOrder(asList(response.ordered_keys).map(String));
      setRiskRankRationales(response.rationales || {});
      setRiskRankSource(response.source || "evidence_ranker");
    } catch (error: any) {
      setRiskRankSource(`排序失败：${error.message}`);
    } finally {
      setRiskRankLoading(false);
    }
  }

  function diagnoseRisk(item: any) {
    const target: ChatTarget = {
      cluster: item.cluster || selectedClusterLabel,
      cluster_id: item.cluster_id || item.cluster || cluster,
      namespace: item.namespace || "default",
      workload_type: item.type === "workload" ? item.kind : "Pod",
      workload_name: item.type === "workload" ? item.name : "",
      pod_name: item.type === "pod" ? item.name : asList(item.pods)[0] || "",
      target_id: item.key,
    };
    if (item.type === "workload") {
      const podEvidence = asList(item.pods).length ? `，关联异常 Pod：${asList(item.pods).join("、")}` : "";
      send(`请诊断 ${item.cluster || selectedClusterLabel} 集群 ${item.namespace} namespace 下 ${item.kind}/${item.name} 的高风险问题${podEvidence}。先追溯所有关联 Pod、日志、Events、rollout、配置、存储和依赖，再匹配最适合的运维 Skill，给出可确认执行的修复方案。`, target);
      return;
    }
    send(`请诊断 ${item.cluster || cluster} 集群 ${item.namespace} namespace 下没有上游 Workload 的 Pod ${item.name}。先读取日志、Events、Pod 配置和节点状态，再匹配最适合的运维 Skill，给出可确认执行的修复方案。`, target);
  }

  useEffect(() => {
    requestAnimationFrame(() => {
      if (conversationRef.current) conversationRef.current.scrollTop = conversationRef.current.scrollHeight;
    });
  }, [messages]);

  async function send(text = input, explicitTarget?: ChatTarget) {
    const prompt = text.trim();
    if (!prompt || streaming) return;
    const selectedTarget: ChatTarget | undefined = explicitTarget || (selectedWorkload ? {
      cluster: selectedWorkload.cluster || selectedClusterLabel,
      cluster_id: selectedWorkload.cluster_id || cluster,
      namespace: selectedWorkload.namespace || namespace,
      workload_type: selectedWorkload.kind || "Workload",
      workload_name: selectedWorkload.name,
      pod_name: "",
      target_id: workloadIdentity(selectedWorkload),
    } : undefined);
    const requestCluster = selectedTarget?.cluster_id || cluster;
    const requestNamespace = selectedTarget?.namespace || namespace;
    setInput("");
    const user: ChatMessage = { id: makeId(), role: "user", text: prompt, target: selectedTarget };
    const assistant: ChatMessage = { id: makeId(), role: "assistant", text: "", status: "streaming", activity: [], target: selectedTarget };
    setMessages((old) => [...old, user, assistant]);
    setStreaming(true);
    const controller = new AbortController();
    streamControllerRef.current = controller;
    try {
      const res = await fetch("/api/chat/stream", {
        method: "POST",
        headers: adminAuthHeaders({ "Content-Type": "application/json" }),
        signal: controller.signal,
        body: JSON.stringify({
          message: prompt,
          original_message: prompt,
          model_profile_id: activeModelId,
          cluster: selectedTarget?.cluster || requestCluster,
          cluster_id: requestCluster,
          namespace: requestNamespace,
          deployment: selectedTarget?.workload_name || "",
          workload_type: selectedTarget?.workload_type || "Workload",
          pod: selectedTarget?.pod_name || "",
          target_id: selectedTarget?.target_id || "",
          severity: "P2",
          auto_healing_enabled: false
        })
      });
      if (!res.ok || !res.body) throw new Error(`${res.status} ${res.statusText}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.trim()) continue;
          const event = JSON.parse(line);
          if (event.type === "delta") {
            setMessages((old) => old.map((m) => (m.id === assistant.id ? { ...m, text: m.text + event.text } : m)));
          }
          if (event.type === "status") {
            setMessages((old) => old.map((m) => m.id === assistant.id ? { ...m, activity: [...(m.activity || []), { stage: event.stage, message: event.message }].slice(-8) } : m));
          }
          if (event.type === "final") {
            setMessages((old) => old.map((m) => m.id === assistant.id ? { ...m, data: event.data } : m));
          }
          if (event.type === "error") throw new Error(event.message);
        }
      }
      setMessages((old) => old.map((m) => (m.id === assistant.id ? { ...m, status: "done" } : m)));
    } catch (error: any) {
      if (controller.signal.aborted || error?.name === "AbortError") {
        setMessages((old) => old.map((m) => (m.id === assistant.id ? { ...m, status: "stopped", text: m.text || "已由你中断本次回答。", activity: [] } : m)));
      } else {
        setMessages((old) => old.map((m) => (m.id === assistant.id ? { ...m, status: "error", text: m.text || `请求失败：${error.message}` } : m)));
      }
    } finally {
      if (streamControllerRef.current === controller) streamControllerRef.current = null;
      setStreaming(false);
    }
  }

  function stopStreaming() {
    streamControllerRef.current?.abort();
  }

  return (
    <section className={cx("chat-layout", messages.length === 0 ? "chat-empty-state" : "chat-active-state") }>
      <div className={cx("chat-content-grid", messages.length > 0 && "with-risk-rail")}>
      <div className="conversation" ref={conversationRef}>
        {messages.length === 0 ? <div className="welcome-panel">
          <div className="welcome-orbit"><Bot size={24} /></div>
          <h2>今天要处理什么？</h2>
          <p>描述现象或目标。我会先读取真实集群证据，再给出可预演、可审批、可验证的处置计划。</p>
          <div className="ops-scope-bar">
            <div className="ops-scope-selects">
              <label><span>集群</span><select value={cluster} onChange={(e) => { setCluster(e.target.value); setNamespace("all"); setWorkload(""); }}>
                <option value="all">所有集群</option>
                {clusters.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}
              </select></label>
              <label><span>Namespace</span><select value={namespace} onChange={(e) => { setNamespace(e.target.value); setWorkload(""); }} disabled={inventory.loading}>
                <option value="all">所有 Namespace</option>
                {namespaces.map((item) => <option key={item} value={item}>{item}</option>)}
              </select></label>
              <label><span>Workload</span><select value={workload} onChange={(e) => setWorkload(e.target.value)} disabled={inventory.loading}>
                <option value="">所有 Workload</option>
                {workloads.map((item: any) => <option key={workloadIdentity(item)} value={workloadIdentity(item)}>{item.kind}/{item.name}</option>)}
              </select></label>
            </div>
            <button className="ghost tiny" onClick={refreshInventory} disabled={inventory.loading}>
              {inventory.loading ? <Loader2 className="spin" size={13} /> : <RefreshCcw size={13} />}
              刷新异常数
            </button>
          </div>
          <div className={cx("ops-brief", inventory.loading && "loading")}>
            <div><span>当前范围</span><strong>{inventory.loading ? "读取中" : selectedClusterLabel}</strong><small>{namespace === "all" ? "所有 Namespace" : namespace}{selectedWorkload ? ` / ${selectedWorkload.kind}/${selectedWorkload.name}` : ""}</small></div>
            <div><span>异常 Pod</span><strong>{inventory.loading ? <Loader2 className="spin" size={17} /> : problemPods.length}</strong><small>未就绪 / 重启 / Events</small></div>
            <div><span>异常 Workload</span><strong>{inventory.loading ? <Loader2 className="spin" size={17} /> : riskyWorkloads.length}</strong><small>Ready 副本不足</small></div>
          </div>
          {inventory.error && <div className="error-box">集群资源读取失败：{inventory.error}</div>}
          {!inventory.loading && displayedRisks.length === 0 && <div className="notice-box">当前范围暂未发现异常 Pod 或 Ready 副本不足的 Workload。</div>}
          {displayedRisks.length > 0 && <div className="ops-hotlist">
            {displayedRisks.map((item: any) => <button key={item.key} onClick={() => diagnoseRisk(item)} disabled={streaming}>
              <span>{item.severity} · {asList(item.reasons)[0] || "高风险"}</span>
              <strong>{item.type === "workload" ? `${item.kind}/${item.name}` : `Pod/${item.name}`}</strong>
              <small>{item.cluster || cluster} / {item.namespace}{asList(item.pods).length ? ` · ${asList(item.pods).length} 个异常 Pod` : ""}</small>
            </button>)}
          </div>}
          <div className="prompt-grid">
            {quickPrompts.map((prompt) => (
              <button key={prompt} onClick={() => send(prompt)}>{prompt}<ChevronRight size={15} /></button>
            ))}
          </div>
        </div> : messages.map((message) => <MessageBubble key={message.id} message={message} />)}
      </div>
      {messages.length > 0 && <aside className="chat-risk-rail">
        <div className="risk-rail-head"><div><span>当前范围高风险</span><strong>{selectedClusterLabel}</strong><small>{namespace === "all" ? "所有 Namespace" : namespace} · Top {displayedRisks.length}/6</small></div><button className="row-icon-button" onClick={refreshInventory} disabled={inventory.loading} title="刷新风险">{inventory.loading ? <Loader2 className="spin" size={14} /> : <RefreshCcw size={14} />}</button></div>
        <div className="risk-rail-stats"><span><b>{problemPods.length}</b>异常 Pod</span><span><b>{riskyWorkloads.length}</b>异常 Workload</span></div>
        <div className="risk-rail-list">
          {displayedRisks.length ? displayedRisks.map((item: any, index: number) => <button key={item.key} onClick={() => diagnoseRisk(item)} disabled={streaming}>
            <i>{index + 1}</i><div><span>{item.severity} · {item.type === "workload" ? "Workload" : "独立 Pod"}</span><strong>{item.type === "workload" ? `${item.kind}/${item.name}` : item.name}</strong><small>{riskRankRationales[item.key] || asList(item.reasons).join(" · ")}{item.restart_count ? ` · restart ${item.restart_count}` : ""}</small></div><ChevronRight size={14} />
          </button>) : <div className="radar-empty">{inventory.loading ? "正在读取风险..." : "当前范围没有高风险资源"}</div>}
        </div>
        {riskRankSource && <small className="risk-rank-source">{riskRankSource === "llm_constrained_ranking" ? "已按 AI 业务影响模型重新排序" : riskRankSource === "deterministic_fallback" ? "已按证据评分重新排序" : riskRankSource}</small>}
        <button className="ghost risk-ai-rank" onClick={rerankRisks} disabled={riskRankLoading || !topRisks.length}>{riskRankLoading ? <Loader2 className="spin" size={14} /> : <Sparkles size={14} />}{riskRankLoading ? "正在重排..." : "AI 重新排序"}</button>
      </aside>}
      </div>
      <div className="composer-wrap">
        <div className="composer">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); } }}
          placeholder="描述集群现象、业务影响、Pod 名称或你想完成的操作..."
        />
        <div className="composer-footer">
          <div className="scope">
            <select value={cluster} onChange={(e) => { setCluster(e.target.value); setNamespace("all"); setWorkload(""); }}>
              <option value="all">所有集群</option>
              {clusters.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}
            </select>
            <select value={namespace} onChange={(e) => { setNamespace(e.target.value); setWorkload(""); }}><option value="all">所有 Namespace</option>{namespaces.map((item) => <option key={item} value={item}>{item}</option>)}</select>
            <select value={workload} onChange={(e) => setWorkload(e.target.value)}><option value="">所有 Workload</option>{workloads.map((item: any) => <option key={workloadIdentity(item)} value={workloadIdentity(item)}>{item.kind}/{item.name}</option>)}</select>
          </div>
          <button className={cx("chat-send", streaming && "stop")} onClick={streaming ? stopStreaming : () => send()} disabled={!streaming && !input.trim()} title={streaming ? "中断回答" : "发送"}>
            {streaming ? <Square size={14} fill="currentColor" /> : <Send size={17} />}
          </button>
        </div>
        </div>
        <small className="composer-note">AI 可能出错；所有集群变更都会经过风险门禁和人工确认。</small>
      </div>
    </section>
  );
}

function MessageBubble({ message }: { message: ChatMessage }) {
  const plan = useMemo(() => chatPlanFromResponse(message.data), [message.data]);
  const [copied, setCopied] = useState(false);
  const activities = asList(message.activity).map((item: any) => typeof item === "string" ? { message: item } : item);
  const latestActivity = activities.at(-1)?.message;
  async function copyAnswer() {
    try {
      await navigator.clipboard.writeText(message.text || "");
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch { setCopied(false); }
  }
  return (
    <article className={cx("message", message.role, message.status === "streaming" && "streaming")}>
      <div className="avatar">{message.role === "assistant" ? <Bot size={16} /> : "我"}</div>
      <div className="bubble">
        <div className="message-meta">{message.role === "assistant" ? "Flawless SRE" : "你"} {message.status === "streaming" && <span>正在生成</span>}{message.status === "stopped" && <span>已中断</span>}</div>
        {message.target && <div className="message-target"><CircleDot size={11} /><span>{message.target.cluster} / {message.target.namespace}</span><b>{message.target.workload_name ? `${message.target.workload_type}/${message.target.workload_name}` : `Pod/${message.target.pod_name}`}</b></div>}
        {message.role === "assistant" && activities.length > 0 && (
          <details className="agent-activity" open={message.status === "streaming"}>
            <summary>{message.status === "streaming" ? <Loader2 className="spin" size={13} /> : <CheckCircle2 size={13} />}<span>{message.status === "streaming" ? latestActivity : `已完成 ${activities.length} 个分析阶段`}</span></summary>
            <div>{activities.map((item: any, index: number) => <span key={`${item.stage || "stage"}-${index}`}><i>{index + 1}</i>{item.message}</span>)}</div>
          </details>
        )}
        {message.text ? <div className="markdown" dangerouslySetInnerHTML={{ __html: markdownish(message.text) }} /> : message.status === "streaming" ? <div className="response-waiting"><i /><i /><i /></div> : null}
        {message.role === "assistant" && message.text && message.status !== "streaming" && <div className="message-actions"><button className="row-icon-button" onClick={copyAnswer} title="复制回答">{copied ? <CheckCircle2 size={13} /> : <Copy size={13} />}</button></div>}
        {plan && message.status !== "streaming" && <OpsPlanPanel plan={plan} />}
      </div>
    </article>
  );
}

function chatPlanFromResponse(data: any) {
  const raw = data?.raw || {};
  if (raw.mode === "general_chat") return null;
  const diagnosis = raw.diagnosis || {};
  const decision = raw.decision || {};
  const remediation = diagnosis.remediation_plan || {};
  const hasCanonicalRemediation = Boolean(diagnosis.remediation_plan && typeof diagnosis.remediation_plan === "object");
  const alert = raw.alert || {};
  // remediation_plan 是服务端经过目标绑定、实时证据和 Skill 门禁后的唯一计划。
  // 即使它的 changes 被安全门禁清空，也不能从 decision.proposed_changes
  // 复活未经授权的原始 LLM 动作。
  const changes = hasCanonicalRemediation ? asList(remediation.changes) : asList(decision.proposed_changes).length ? decision.proposed_changes : diagnosis.proposed_changes;
  const steps = hasCanonicalRemediation ? asList(remediation.steps) : asList(diagnosis.immediate_actions).map((item: any, index: number) => ({ id: `diagnostic-${index}`, title: typeof item === "string" ? item : item.title || item.action || `诊断步骤 ${index + 1}`, description: typeof item === "string" ? item : item.description || "" }));
  if (!steps.length && !asList(changes).length) return null;
  return {
    id: `chat-${makeId()}`,
    title: "SRE 对话处置计划",
    cluster: remediation.cluster || raw.k8s_context?.cluster || alert.cluster || "all",
    cluster_id: remediation.cluster_id || raw.k8s_context?.cluster_id || alert.cluster_id || alert.cluster || "all",
    namespace: remediation.namespace || raw.k8s_context?.pod?.namespace || alert.namespace || "default",
    target: remediation.target || `${raw.k8s_context?.pod?.workload_kind || alert.workload_type || "Workload"}/${raw.k8s_context?.pod?.workload_name || alert.workload_name || alert.deployment || "selected-target"}`,
    pod_name: remediation.pod_name || raw.k8s_context?.pod?.name || alert.pod || "",
    summary: diagnosis.root_cause || diagnosis.summary || "基于 SRE 对话证据生成的处置计划。",
    reason: remediation.reason || decision.reason || diagnosis.root_cause || "",
    evidence_gap: remediation.evidence_gap || diagnosis.evidence_gap || "",
    root_cause_hypotheses: remediation.hypotheses || diagnosis.root_cause_hypotheses || [],
    success_criteria: remediation.success_criteria || decision.success_criteria || [],
    evidence: remediation.evidence || raw.k8s_context?.diagnostics || {},
    steps,
    changes: asList(changes),
    operator_skills: remediation.operator_skills || diagnosis.operator_skills || [],
    skill_allowed_actions: remediation.skill_allowed_actions || [],
    planning_engine: remediation.planning_engine || remediation.engine || "",
    step_source: remediation.step_source || "",
    requires_high_risk_confirmation: Boolean(remediation.requires_high_risk_confirmation),
    requires_confirmation: asList(changes).length > 0,
    model_profile_id: diagnosis.diagnosis_metadata?.model_profile_id || "",
    source: remediation.source || raw.k8s_context?.source || (alert.cluster_id && !["all", "local", "local-cluster"].includes(alert.cluster_id) ? "rancher" : "sre_chat"),
  };
}

function InspectionPage({ activeModelId }: { activeModelId: string }) {
  const [cluster, setCluster] = useState("all");
  const [namespace, setNamespace] = useState("all");
  const [autoOps, setAutoOps] = useState(false);
  const [productionMode, setProductionMode] = useState(true);
  const [scheduled, setScheduled] = useState(false);
  const [schedulePreset, setSchedulePreset] = useState("120");
  const [customMinutes, setCustomMinutes] = useState("120");
  const [lastScheduledAt, setLastScheduledAt] = useState("");
  const [result, setResult] = useState<ApiState<any>>({ loading: false });
  const [selectedPlan, setSelectedPlan] = useState<any>(null);
  const [previewState, setPreviewState] = useState<ApiState<any>>({ loading: false });
  const [previewingId, setPreviewingId] = useState("");
  const [autoJobs, setAutoJobs] = useState<any[]>([]);
  const [pendingApprovalPlans, setPendingApprovalPlans] = useState<any[]>([]);
  const runLock = useRef(false);
  const [inventory] = useAsync<any>(() => apiGet("/api/rancher/inventory").catch(() => ({ clusters: [], inventory: [] })), []);
  const inspectionClusters = asList(inventory.data?.clusters);
  const inspectionNamespaces = useMemo(() => {
    const scoped = cluster === "all" ? asList(inventory.data?.inventory) : asList(inventory.data?.inventory).filter((item: any) => cluster === item.cluster?.id || cluster === item.cluster?.name);
    return Array.from(new Set(scoped.flatMap((item: any) => asList(item.namespaces).map((entry: any) => String(entry.name))))).sort();
  }, [cluster, inventory.data]);

  const scheduleMinutes = Math.max(30, Number(schedulePreset === "custom" ? customMinutes : schedulePreset) || 120);

  async function run(trigger: "manual" | "scheduled" = "manual") {
    if (runLock.current) return;
    runLock.current = true;
    setResult({ loading: true });
    setSelectedPlan(null);
    setPreviewState({ loading: false });
    setPreviewingId("");
    setAutoJobs([]);
    setPendingApprovalPlans([]);
    try {
      const data = await apiPost<any>("/api/inspection/run", { cluster, namespace, auto_ops: autoOps, production_mode: productionMode, model_profile_id: activeModelId });
      setResult({ loading: false, data });
      if (autoOps) {
        // 浅层巡检 finding 只用于发现异常，不能直接成为执行计划。先逐项调用
        // 实时深取证/LLM/Skill 预演，再把所有计划留给人工审批；这里绝不自动写集群。
        const candidates = asList(data?.findings).slice(0, 5);
        const previews = await Promise.all(candidates.map(async (finding: any) => {
          try { return await apiPost<any>("/api/inspection/preview", { finding_id: finding.id, model_profile_id: activeModelId }); }
          catch (error: any) { return { error: error.message, finding_id: finding.id }; }
        }));
        const plans = previews.map((item: any) => item?.plan).filter((plan: any) => plan && (asList(plan.steps).length || asList(plan.changes).length));
        setPendingApprovalPlans(plans);
        if (plans[0]) setSelectedPlan(plans[0]);
        const failedPreviews = previews.filter((item: any) => item?.error);
        if (failedPreviews.length) setPreviewState({ loading: false, error: `${failedPreviews.length} 个异常项实时预演失败：${failedPreviews.map((item: any) => item.error).join("；")}` });
      }
      if (trigger === "scheduled") setLastScheduledAt(new Date().toLocaleString("zh-CN", { hour12: false }));
    } catch (error: any) {
      setResult({ loading: false, error: error.message });
    } finally {
      runLock.current = false;
    }
  }

  useEffect(() => {
    if (!scheduled) return;
    const timer = window.setInterval(() => {
      if (!runLock.current) void run("scheduled");
    }, scheduleMinutes * 60 * 1000);
    return () => window.clearInterval(timer);
  }, [scheduled, scheduleMinutes, cluster, namespace, autoOps, productionMode, activeModelId]);

  async function previewFinding(finding: any) {
    const findingId = String(finding?.id || "");
    if (!findingId) return;
    setPreviewingId(findingId);
    setSelectedPlan(null);
    setPreviewState({ loading: true });
    try {
      const data = await apiPost<any>("/api/inspection/preview", { finding_id: findingId, model_profile_id: activeModelId });
      setSelectedPlan(data.plan || null);
      setPreviewState({ loading: false, data });
    } catch (error: any) {
      setPreviewState({ loading: false, error: error.message });
    } finally {
      setPreviewingId("");
    }
  }

  useEffect(() => {
    const activeJobs = autoJobs.filter((job) => job.id && !String(job.id).startsWith("rejected-") && ["queued", "running", "awaiting_approval", "resume_pending", "cancelling"].includes(job.status));
    if (!activeJobs.length) return;
    const timer = window.setTimeout(async () => {
      const next = await Promise.all(autoJobs.map(async (job) => {
        if (!activeJobs.some((activeJob) => activeJob.id === job.id)) return job;
        try { return await apiGet(`/api/ops/jobs/${encodeURIComponent(job.id)}`); }
        catch (error: any) {
          const failures = Number(job.poll_failures || 0) + 1;
          if (failures < 3) return { ...job, poll_failures: failures };
          return {
            ...job,
            status: "failed",
            stage: "failed",
            poll_failures: failures,
            message: `任务状态连续读取失败，已停止等待：${error.message}`,
            updated_at: new Date().toISOString(),
            events: [...asList(job.events), { timestamp: new Date().toISOString(), stage: "failed", level: "error", message: `任务状态接口不可达：${error.message}` }],
          };
        }
      }));
      setAutoJobs(next);
    }, 1400);
    return () => window.clearTimeout(timer);
  }, [autoJobs]);

  const findings = asList(result.data?.findings);
  const summary = result.data?.summary || {};
  return (
    <section className="workspace-grid inspection-workspace">
      <Panel className="control-panel">
        <PanelTitle icon={Search} title="巡检范围" action={<button className="primary" onClick={() => run("manual")} disabled={result.loading}>{result.loading ? <Loader2 className="spin" size={16} /> : <Play size={16} />}立即巡检</button>} />
        <div className="form-grid">
          <label>集群<select value={cluster} onChange={(e) => { setCluster(e.target.value); setNamespace("all"); }}><option value="all">所有集群</option>{inspectionClusters.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></label>
          <label>Namespace<select value={namespace} onChange={(e) => setNamespace(e.target.value)}><option value="all">所有 Namespace</option>{inspectionNamespaces.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
          <label>定时巡检<select value={schedulePreset} onChange={(e) => setSchedulePreset(e.target.value)}><option value="30">每 30 分钟</option><option value="60">每 1 小时</option><option value="120">每 2 小时</option><option value="360">每 6 小时</option><option value="custom">自定义分钟</option></select></label>
          {schedulePreset === "custom" && <label>自定义分钟<input type="number" min={30} step={10} value={customMinutes} onChange={(e) => setCustomMinutes(e.target.value)} /></label>}
          <label className="toggle"><input type="checkbox" checked={autoOps} onChange={(e) => setAutoOps(e.target.checked)} />自动深取证并生成方案（所有变更仍需人工审批）</label>
          <label className="toggle"><input type="checkbox" checked={productionMode} onChange={(e) => setProductionMode(e.target.checked)} />生产模式</label>
          <label className="toggle"><input type="checkbox" checked={scheduled} onChange={(e) => setScheduled(e.target.checked)} />开启定时巡检（最小 30 分钟）</label>
        </div>
        <div className="ops-event-chips">
          <span><RefreshCcw size={12} /> 周期 {scheduleMinutes} 分钟</span>
          <span>{scheduled ? "定时已开启" : "定时未开启"}</span>
          {lastScheduledAt && <span>上次自动巡检 {lastScheduledAt}</span>}
        </div>
      </Panel>
      <div className="metric-strip">
        <Metric title="发现问题" value={summary.total ?? findings.length} />
        <Metric title="P0/P1" value={(findings.filter((f: any) => ["P0", "P1"].includes(f.severity)).length)} tone="danger" />
        <Metric title="可执行计划" value={findings.filter((f: any) => f.ops_plan).length} />
        <Metric title="Skill 路由" value={summary.skill_routed || 0} />
      </div>
      <Panel className="span-all">
        <PanelTitle icon={ShieldCheck} title="异常队列" subtitle="每一项都可查看根因证据、预演步骤、变更差异和恢复判据" />
        {result.error && <div className="error-box">{result.error}</div>}
        {!findings.length && !result.loading ? <EmptyState text="尚未执行巡检，或当前范围没有新问题。" /> : (
          <div className="finding-list">
            {findings.map((f: any) => (
              <div className="finding" key={f.id || f.title}>
                <div>
                  <strong>{f.title || f.summary || "异常项"}</strong>
                  <p>{f.summary || f.reason || "等待 AI 补充诊断。"}</p>
                  <div className="chips"><span>{f.cluster || cluster}</span><span>{f.namespace || namespace}</span><span>{f.category || "runtime"}</span>{asList(f.matched_skills).slice(0, 2).map((skill: any) => <span key={skill.id}>Skill · {skill.name}</span>)}</div>
                </div>
                <div className="finding-actions"><span className={cx("severity", f.severity === "P0" || f.severity === "P1" ? "hot" : "")}>{f.severity || "P2"}</span><button className="ghost tiny" onClick={() => previewFinding(f)} disabled={previewState.loading}>{previewingId === String(f.id) ? <Loader2 className="spin" size={14} /> : <Eye size={14} />}{previewingId === String(f.id) ? "实时取证" : "AI 预演"}</button></div>
              </div>
            ))}
          </div>
        )}
      </Panel>
      {previewState.error && <Panel className="span-all inspection-plan-panel"><div className="error-box">AI 预演失败：{previewState.error}</div></Panel>}
      {selectedPlan && <Panel className="span-all inspection-plan-panel"><PanelTitle icon={TerminalSquare} title="实时 AI 运维预演" subtitle="已重新读取实时证据并完成 Skill、根因、动作与恢复判据校验；确认前不会修改集群" /><OpsPlanPanel plan={selectedPlan} /></Panel>}
      {pendingApprovalPlans.length > 0 && <Panel className="span-all"><PanelTitle icon={ShieldCheck} title="实时处置计划待确认" subtitle="所有计划均来自深取证 + 动态 Skill 路由；任何真实写操作都不会自动执行，并会在每一步再次暂停确认" /><div className="inspection-approval-queue">{pendingApprovalPlans.map((plan: any) => <OpsPlanPanel key={plan.id || plan.target} plan={plan} autonomous={false} />)}</div></Panel>}
      {autoJobs.length > 0 && <Panel className="span-all"><PanelTitle icon={Activity} title="自动运维执行流" subtitle="任务在切换页面后仍会继续运行，可随时中断" /><div className="auto-job-grid detailed">{autoJobs.map((job) => <OpsJobProgress key={job.id} job={job} compact onCancel={["queued", "running", "awaiting_approval", "cancelling"].includes(job.status) ? async () => { const next = await apiPost(`/api/ops/jobs/${encodeURIComponent(job.id)}/cancel`, {}); setAutoJobs((items) => items.map((item) => item.id === job.id ? next : item)); } : undefined} />)}</div></Panel>}
    </section>
  );
}

function flowDirectionLabel(direction: string) {
  if (direction === "ingress") return "入站";
  if (direction === "egress") return "出站";
  if (direction === "cross_cluster") return "跨集群";
  return direction || "未知";
}

function flowEndpointLabel(endpoint: any) {
  if (!endpoint) return "-";
  const name = endpoint.name || endpoint.address || "-";
  const prefix = endpoint.kind ? `${endpoint.kind}/` : "";
  const port = endpoint.port ? `:${endpoint.port}` : "";
  return `${prefix}${name}${port}`;
}

function ExternalTrafficPage() {
  const [cluster, setCluster] = useState("all");
  const [namespace, setNamespace] = useState("all");
  const [workload, setWorkload] = useState("");
  const [windowSize, setWindowSize] = useState("30m");
  const [source, setSource] = useState("auto");
  const [direction, setDirection] = useState("all");
  const [traffic, setTraffic] = useState<ApiState<any>>({ loading: false });
  const [inventory, refreshInventory] = useAsync<any>(() => apiGet("/api/rancher/inventory").catch(() => ({ clusters: [], inventory: [] })), []);
  const clusters = asList(inventory.data?.clusters);
  const scopedInventory = useMemo(() => cluster === "all" ? asList(inventory.data?.inventory) : asList(inventory.data?.inventory).filter((item: any) => cluster === item.cluster?.id || cluster === item.cluster?.name), [cluster, inventory.data]);
  const namespaces = useMemo(() => Array.from(new Set(scopedInventory.flatMap((item: any) => asList(item.namespaces).map((entry: any) => String(entry.name))))).sort(), [scopedInventory]);
  const workloads = useMemo(() => scopedInventory.flatMap((scope: any) => asList(scope.workloads).map((item: any) => ({
    id: `${item.cluster_id || scope.cluster?.id || scope.cluster?.name}:${item.namespace}:${item.kind}/${item.name}`,
    label: `${item.kind}/${item.name}`,
    name: item.name,
    namespace: item.namespace,
    cluster: item.cluster_id || scope.cluster?.id || scope.cluster?.name,
  }))).filter((item: any) => (namespace === "all" || item.namespace === namespace)), [scopedInventory, namespace]);

  const flows = useMemo(() => {
    const rows = asList(traffic.data?.flows);
    return direction === "all" ? rows : rows.filter((flow: any) => flow.direction === direction);
  }, [traffic.data, direction]);

  useEffect(() => {
    runTraffic();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (namespace !== "all" && !namespaces.includes(namespace)) setNamespace("all");
  }, [namespace, namespaces]);

  async function runTraffic() {
    setTraffic({ loading: true });
    try {
      const selectedCluster = clusters.find((item: any) => cluster === item.id || cluster === item.name);
      const data = await apiPost("/api/network/external-flows", {
        cluster: cluster === "all" ? "all" : (selectedCluster?.name || cluster),
        cluster_id: cluster === "all" ? "" : (selectedCluster?.id || cluster),
        namespace,
        workload,
        window: windowSize,
        source,
        include_static_inference: true,
        include_cmdb: true,
      });
      setTraffic({ loading: false, data });
    } catch (error: any) {
      setTraffic({ loading: false, error: error.message });
    }
  }

  const summary = traffic.data?.summary || {};
  const graph = traffic.data?.graph || {};
  const sourceStatus = asList(traffic.data?.data_source_status);
  return (
    <section className="traffic-page">
      <Panel className="traffic-command">
        <PanelTitle icon={Cable} title="外部数据流" subtitle="只展示集群边界外或跨集群的数据流，方向、目的地和证据分开呈现。" action={<button className="ghost" onClick={() => { refreshInventory(); runTraffic(); }}><RefreshCcw size={15} />刷新</button>} />
        <div className="traffic-filters">
          <label>集群<select value={cluster} onChange={(e) => { setCluster(e.target.value); setNamespace("all"); setWorkload(""); }}>
            <option value="all">所有集群</option>
            {clusters.map((item: any) => <option value={item.id || item.name} key={item.id || item.name}>{item.name || item.id}</option>)}
          </select></label>
          <label>Namespace<select value={namespace} onChange={(e) => { setNamespace(e.target.value); setWorkload(""); }}>
            <option value="all">所有 Namespace</option>
            {namespaces.map((item) => <option value={item} key={item}>{item}</option>)}
          </select></label>
          <label>Workload<select value={workload} onChange={(e) => setWorkload(e.target.value)}>
            <option value="">所有 Workload</option>
            {workloads.map((item: any) => <option value={item.name} key={item.id}>{item.label}</option>)}
          </select></label>
          <label>窗口<select value={windowSize} onChange={(e) => setWindowSize(e.target.value)}>
            <option value="15m">15 分钟</option>
            <option value="30m">30 分钟</option>
            <option value="1h">1 小时</option>
            <option value="6h">6 小时</option>
          </select></label>
          <label>来源<select value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="auto">自动</option>
            <option value="observed">真实观测优先</option>
            <option value="static">只看配置推断</option>
          </select></label>
          <button className="primary" onClick={runTraffic} disabled={traffic.loading}>{traffic.loading ? <Loader2 className="spin" size={15} /> : <Play size={15} />}分析数据流</button>
        </div>
      </Panel>

      <div className="traffic-kpis">
        <Metric title="边界流" value={traffic.loading ? "..." : summary.total || 0} />
        <Metric title="入站" value={summary.ingress || 0} />
        <Metric title="出站" value={summary.egress || 0} />
        <Metric title="跨集群" value={summary.cross_cluster || 0} />
        <Metric title="真实观测" value={summary.observed || 0} tone={summary.observed ? "good" : undefined} />
        <Metric title="推断流" value={summary.inferred || 0} />
      </div>

      {traffic.error && <div className="error-box">{traffic.error}</div>}

      <div className="traffic-layout">
        <Panel className="traffic-map-panel">
          <PanelTitle icon={Network} title="方向雷达" subtitle={traffic.data?.message || "点击分析后展示外部入站、出站与跨集群流向。"} />
          <div className="traffic-radar">
            <div className="traffic-radar-center">
              <Network size={22} />
              <strong>Cluster Boundary</strong>
              <span>{cluster === "all" ? "all clusters" : cluster}</span>
            </div>
            <div className="traffic-lane ingress">
              <b>外部进入</b>
              {flows.filter((f: any) => f.direction === "ingress").slice(0, 6).map((flow: any) => <span key={flow.id}>{flowEndpointLabel(flow.source)} → {flowEndpointLabel(flow.destination)}</span>)}
            </div>
            <div className="traffic-lane egress">
              <b>向外输出</b>
              {flows.filter((f: any) => f.direction === "egress").slice(0, 6).map((flow: any) => <span key={flow.id}>{flowEndpointLabel(flow.source)} → {flowEndpointLabel(flow.destination)}</span>)}
            </div>
            <div className="traffic-lane cross">
              <b>跨集群</b>
              {flows.filter((f: any) => f.direction === "cross_cluster").slice(0, 6).map((flow: any) => <span key={flow.id}>{flowEndpointLabel(flow.source)} → {flowEndpointLabel(flow.destination)}</span>)}
            </div>
          </div>
          <div className="traffic-source-strip">
            {sourceStatus.map((item: any) => <span key={item.id} className={cx("status-pill", item.status === "connected" || item.status === "ok" ? "ok" : item.status === "failed" ? "warn" : "muted")}><i />{item.id}: {item.status}</span>)}
          </div>
        </Panel>

        <Panel className="traffic-table-panel">
          <PanelTitle icon={Activity} title="边界流列表" subtitle={`${flows.length} 条，点击方向筛选可快速定位。`} action={<div className="segmented"><button className={direction === "all" ? "active" : ""} onClick={() => setDirection("all")}>全部</button><button className={direction === "ingress" ? "active" : ""} onClick={() => setDirection("ingress")}>入站</button><button className={direction === "egress" ? "active" : ""} onClick={() => setDirection("egress")}>出站</button><button className={direction === "cross_cluster" ? "active" : ""} onClick={() => setDirection("cross_cluster")}>跨集群</button></div>} />
          {traffic.loading ? <EmptyState text="正在梳理 Pod、Service、Ingress、Endpoint、CMDB 与可选流量观测数据..." /> : flows.length ? (
            <div className="traffic-flow-list">
              {flows.map((flow: any) => <article className={cx("traffic-flow-card", flow.direction)} key={flow.id}>
                <header>
                  <span>{flowDirectionLabel(flow.direction)}</span>
                  <strong>{flowEndpointLabel(flow.source)} <ChevronRight size={13} /> {flowEndpointLabel(flow.destination)}</strong>
                  <em>{Math.round(Number(flow.confidence || 0) * 100)}%</em>
                </header>
                <div className="traffic-flow-meta">
                  <span>{flow.source?.cluster || "-"} / {flow.source?.namespace || "-"}</span>
                  <span>{flow.protocol || "unknown"} {flow.port ? `:${flow.port}` : ""}</span>
                  <span>{flow.observed ? "真实观测" : "配置推断"} · {flow.source_system}</span>
                  {(flow.bytes || flow.rps) && <span>{flow.bytes ? `${prettyNumber(flow.bytes)} bytes` : ""} {flow.rps ? `${prettyNumber(flow.rps)} rps` : ""}</span>}
                </div>
                <details>
                  <summary>查看证据</summary>
                  <div>{asList(flow.evidence).map((item: string, index: number) => <p key={`${flow.id}-${index}`}>{item}</p>)}</div>
                </details>
              </article>)}
            </div>
          ) : <EmptyState text="当前范围没有发现集群外/跨集群数据流；如果要看真实流量，请接入 Hubble、Kiali 或自研 Flow Observation。 " />}
        </Panel>
      </div>

      <Panel className="traffic-graph-facts">
        <PanelTitle icon={GitBranch} title="图谱事实" subtitle="供拓扑影响、爆炸半径和后续 AI SRE 根因分析复用。" />
        <div className="traffic-fact-grid">
          <Metric title="图节点" value={asList(graph.nodes).length} />
          <Metric title="图关系" value={asList(graph.edges).length} />
          <Metric title="外部端点" value={summary.external_endpoints || 0} />
          <Metric title="数据源" value={asList(traffic.data?.data_sources).join(" / ") || "-"} />
        </div>
      </Panel>
    </section>
  );
}

function ExternalTrafficEmbedded({ cluster, namespace, workload = "" }: { cluster: string; namespace: string; workload?: string }) {
  const [windowSize, setWindowSize] = useState("30m");
  const [source, setSource] = useState("auto");
  const [direction, setDirection] = useState("all");
  const [traffic, setTraffic] = useState<ApiState<any>>({ loading: false });

  const flows = useMemo(() => {
    const rows = asList(traffic.data?.flows);
    return direction === "all" ? rows : rows.filter((flow: any) => flow.direction === direction);
  }, [traffic.data, direction]);

  async function runTraffic() {
    setTraffic({ loading: true });
    try {
      const data = await apiPost("/api/network/external-flows", {
        cluster: cluster === "all" ? "all" : cluster,
        cluster_id: cluster === "all" ? "" : cluster,
        namespace,
        workload,
        window: windowSize,
        source,
        include_static_inference: true,
        include_cmdb: true,
      });
      setTraffic({ loading: false, data });
    } catch (error: any) {
      setTraffic({ loading: false, error: error.message });
    }
  }

  const summary = traffic.data?.summary || {};
  return (
    <div className="topology-traffic-embed">
      <div className="topology-traffic-head">
        <div>
          <span><Cable size={13} />外部数据流</span>
          <strong>集群边界与跨集群数据流</strong>
          <p>只聚合流入、流出和跨集群关系，供影响面分析判断“故障会不会传出去”。</p>
        </div>
        <button className="ghost tiny" onClick={runTraffic} disabled={traffic.loading}>
          {traffic.loading ? <Loader2 className="spin" size={13} /> : <Play size={13} />}
          分析边界流
        </button>
      </div>
      <div className="topology-traffic-controls">
        <select value={windowSize} onChange={(e) => setWindowSize(e.target.value)}>
          <option value="15m">15 分钟</option>
          <option value="30m">30 分钟</option>
          <option value="1h">1 小时</option>
          <option value="6h">6 小时</option>
        </select>
        <select value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="auto">自动证据</option>
          <option value="observed">观测优先</option>
          <option value="static">配置推断</option>
        </select>
        <div className="segmented compact">
          <button className={direction === "all" ? "active" : ""} onClick={() => setDirection("all")}>全部</button>
          <button className={direction === "ingress" ? "active" : ""} onClick={() => setDirection("ingress")}>入站</button>
          <button className={direction === "egress" ? "active" : ""} onClick={() => setDirection("egress")}>出站</button>
          <button className={direction === "cross_cluster" ? "active" : ""} onClick={() => setDirection("cross_cluster")}>跨集群</button>
        </div>
      </div>
      <div className="topology-traffic-kpis">
        <Metric title="边界流" value={traffic.loading ? "..." : summary.total || 0} />
        <Metric title="入站" value={summary.ingress || 0} />
        <Metric title="出站" value={summary.egress || 0} />
        <Metric title="跨集群" value={summary.cross_cluster || 0} />
        <Metric title="真实观测" value={summary.observed || 0} tone={summary.observed ? "good" : undefined} />
      </div>
      {traffic.error && <div className="error-box">{traffic.error}</div>}
      {traffic.loading ? (
        <div className="topology-flow-empty">正在汇总 Service、Ingress、Endpoint、CMDB 与可观测流量...</div>
      ) : flows.length ? (
        <div className="topology-flow-strip">
          {flows.slice(0, 6).map((flow: any) => (
            <article className={cx("topology-flow-card", flow.direction)} key={flow.id}>
              <span>{flowDirectionLabel(flow.direction)}</span>
              <strong>{flowEndpointLabel(flow.source)} <ChevronRight size={12} /> {flowEndpointLabel(flow.destination)}</strong>
              <small>{flow.observed ? "真实观测" : "配置推断"} · {Math.round(Number(flow.confidence || 0) * 100)}%</small>
            </article>
          ))}
        </div>
      ) : (
        <div className="topology-flow-empty">点击“分析边界流”后展示该拓扑范围内的集群外/跨集群数据流。</div>
      )}
    </div>
  );
}

function TrafficFlow3DPanel({ cluster, namespace, workload }: { cluster: string; namespace: string; workload: string }) {
  const [windowSize, setWindowSize] = useState("30m");
  const [source, setSource] = useState("auto");
  const [direction, setDirection] = useState("all");
  const [traffic, setTraffic] = useState<ApiState<any>>({ loading: false });

  const flows = useMemo(() => {
    const rows = asList(traffic.data?.flows);
    return direction === "all" ? rows : rows.filter((flow: any) => flow.direction === direction);
  }, [traffic.data, direction]);

  async function runTraffic() {
    setTraffic({ loading: true });
    try {
      const data = await apiPost("/api/network/external-flows", {
        cluster: cluster === "all" ? "all" : cluster,
        cluster_id: cluster === "all" ? "" : cluster,
        namespace,
        workload,
        window: windowSize,
        source,
        include_static_inference: true,
        include_cmdb: true,
      });
      setTraffic({ loading: false, data });
    } catch (error: any) {
      setTraffic({ loading: false, error: error.message });
    }
  }

  useEffect(() => {
    runTraffic();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cluster, namespace, workload]);

  const summary = traffic.data?.summary || {};
  const sourceStatus = asList(traffic.data?.data_source_status);
  return (
    <div className="flow-module">
      <div className="flow-module-head">
        <div>
          <span><Cable size={14} />eBPF 数据流</span>
          <strong>{workload ? `只看 ${workload} 的边界流` : "集群边界入站/出站/跨集群流"}</strong>
          <p>优先使用 eBPF / Hubble / Calico Flow / Beyla 真实观测；缺失时用 CMDB 和 K8s 配置推断兜底。</p>
        </div>
        <div className="flow-controls">
          <select value={windowSize} onChange={(event) => setWindowSize(event.target.value)}>
            <option value="15m">15 分钟</option>
            <option value="30m">30 分钟</option>
            <option value="1h">1 小时</option>
            <option value="6h">6 小时</option>
          </select>
          <select value={source} onChange={(event) => setSource(event.target.value)}>
            <option value="auto">自动证据</option>
            <option value="observed">观测优先</option>
            <option value="static">配置推断</option>
          </select>
          <button className="primary tiny" onClick={runTraffic} disabled={traffic.loading}>
            {traffic.loading ? <Loader2 className="spin" size={13} /> : <Play size={13} />}
            重新分析
          </button>
        </div>
      </div>
      <div className="flow-source-strip">
        {sourceStatus.map((item: any) => (
          <span key={item.id} className={cx("status-pill", item.status === "connected" || item.status === "ok" ? "ok" : item.status === "failed" ? "warn" : "muted")}>
            <i />{item.id}: {item.status}{typeof item.flows === "number" ? ` · ${item.flows} 条` : ""}
          </span>
        ))}
      </div>
      <div className="flow-kpi-row">
        <Metric title="边界流" value={traffic.loading ? "..." : summary.total || 0} />
        <Metric title="eBPF 真实观测" value={summary.ebpf_observed || 0} tone={summary.ebpf_observed ? "good" : undefined} />
        <Metric title="入站" value={summary.ingress || 0} />
        <Metric title="出站" value={summary.egress || 0} />
        <Metric title="跨集群" value={summary.cross_cluster || 0} />
        <Metric title="外部端点" value={summary.external_endpoints || 0} />
      </div>
      {traffic.error && <div className="error-box">{traffic.error}</div>}
      <div className="flow-module-grid">
        <div className="flow-3d-shell">
          {traffic.loading ? (
            <EmptyState text="正在读取 eBPF、CMDB、Service、Ingress 和 Endpoint 证据..." />
          ) : flows.length ? (
            <TrafficFlowCanvas graph={traffic.data?.graph || { nodes: [], edges: [] }} flows={flows} />
          ) : (
            <EmptyState text="当前范围未发现边界数据流。若希望看到真实字节级流量，请确认 eBPF Collector 已写入 Loki/Flow API。" />
          )}
        </div>
        <div className="flow-trace-panel">
          <div className="segmented compact">
            <button className={direction === "all" ? "active" : ""} onClick={() => setDirection("all")}>全部</button>
            <button className={direction === "ingress" ? "active" : ""} onClick={() => setDirection("ingress")}>入站</button>
            <button className={direction === "egress" ? "active" : ""} onClick={() => setDirection("egress")}>出站</button>
            <button className={direction === "cross_cluster" ? "active" : ""} onClick={() => setDirection("cross_cluster")}>跨集群</button>
          </div>
          <div className="flow-trace-list">
            {flows.slice(0, 18).map((flow: any) => (
              <article className={cx("flow-trace-card", flow.direction)} key={flow.id}>
                <header>
                  <span>{flowDirectionLabel(flow.direction)}</span>
                  <b>{flow.observed ? "真实观测" : "配置推断"}</b>
                </header>
                <strong>{flowEndpointLabel(flow.source)} <ChevronRight size={13} /> {flowEndpointLabel(flow.destination)}</strong>
                <p>{flow.source?.cluster || "-"} / {flow.source?.namespace || "-"} · {flow.protocol || "unknown"} {flow.port ? `:${flow.port}` : ""} · {flow.source_system}</p>
                <details>
                  <summary>溯源证据</summary>
                  {asList(flow.evidence).map((item: string, index: number) => <small key={`${flow.id}-${index}`}>{item}</small>)}
                </details>
              </article>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function TrafficFlow2DFallback({ graph, flows, message }: { graph: any; flows: any[]; message: string }) {
  const nodes = asList(graph?.nodes).slice(0, 28);
  const nodeIds = new Set(nodes.map((node: any) => String(node.id)));
  const edges = asList(graph?.edges).filter((edge: any) => nodeIds.has(String(edge.source)) && nodeIds.has(String(edge.target)));
  const width = 820;
  const height = 450;
  const centerX = width / 2;
  const centerY = height / 2 + 8;
  const radiusX = Math.min(310, 100 + nodes.length * 9);
  const radiusY = Math.min(170, 65 + nodes.length * 5);
  const positions = new Map<string, { x: number; y: number }>();
  nodes.forEach((node: any, index: number) => {
    const angle = nodes.length > 1 ? (index / nodes.length) * Math.PI * 2 - Math.PI / 2 : 0;
    positions.set(String(node.id), {
      x: centerX + Math.cos(angle) * radiusX,
      y: centerY + Math.sin(angle) * radiusY,
    });
  });
  const flowById = new Map(flows.map((flow: any) => [String(flow.id), flow]));

  return <div className="flow-2d-fallback">
    <svg className="flow-2d-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="2D 边界数据流兼容视图">
      <defs>
        <marker id="flow-2d-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke" />
        </marker>
      </defs>
      {edges.map((edge: any, index: number) => {
        const source = positions.get(String(edge.source));
        const target = positions.get(String(edge.target));
        if (!source || !target) return null;
        const flow: any = flowById.get(String(edge.id));
        const observed = Boolean(flow?.observed);
        const direction = String(edge.direction || flow?.direction || "");
        const stroke = observed ? "#31ddea" : direction === "ingress" ? "#65d694" : direction === "cross_cluster" ? "#e8b94d" : "#6c9ee8";
        return <line
          key={`${edge.id || index}-${edge.source}-${edge.target}`}
          x1={source.x} y1={source.y} x2={target.x} y2={target.y}
          stroke={stroke} strokeWidth={observed ? 2.6 : 1.7}
          strokeDasharray={observed ? undefined : "7 6"}
          markerEnd="url(#flow-2d-arrow)"
        />;
      })}
      {nodes.map((node: any) => {
        const position = positions.get(String(node.id))!;
        const external = Boolean(node.external) || /external|domain|ip/.test(String(node.type || "").toLowerCase());
        const label = String(node.title || node.name || node.id);
        return <g className="flow-2d-node" key={String(node.id)} transform={`translate(${position.x} ${position.y})`}>
          <circle r={external ? 10 : 8} fill={external ? "#dcae48" : "#3fb98a"} stroke={external ? "#ffe09b" : "#9cf0ca"} strokeWidth="2" />
          <text y="-15" textAnchor="middle">{label.length > 24 ? `${label.slice(0, 22)}…` : label}</text>
        </g>;
      })}
    </svg>
    <div className="flow-2d-notice"><strong>已切换到 2D 兼容模式</strong><span>{message}</span></div>
  </div>;
}

function TrafficFlowCanvas({ graph, flows }: { graph: any; flows: any[] }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [canvasError, setCanvasError] = useState("");

  useEffect(() => {
    let disposed = false;
    let frame = 0;
    let cleanup = () => {};
    const canvas = canvasRef.current;
    const host = canvas?.parentElement;
    if (!canvas || !host) return cleanup;
    setCanvasError("");

    (async () => {
      if (!canCreateWebGLContext()) throw new Error("WebGL context unavailable");
      const THREE = await import("three");
      const { OrbitControls } = await import("three/examples/jsm/controls/OrbitControls.js");
      if (disposed) return;

      const nodes = asList(graph?.nodes);
      const edges = asList(graph?.edges);
      const scene = new THREE.Scene();
      scene.background = new THREE.Color(0x03101b);
      scene.fog = new THREE.FogExp2(0x03101b, 0.0011);
      const camera = new THREE.PerspectiveCamera(48, 1, 0.1, 2200);
      const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      const controls = new OrbitControls(camera, renderer.domElement);
      controls.enableDamping = true;
      controls.dampingFactor = 0.08;
      controls.minDistance = 80;
      controls.maxDistance = 680;
      camera.position.set(0, 105, 320);
      controls.target.set(0, 0, 0);

      scene.add(new THREE.AmbientLight(0xc9e7ff, 0.7));
      const key = new THREE.DirectionalLight(0x8ed8ff, 1.15);
      key.position.set(120, 140, 160);
      scene.add(key);
      scene.add(new THREE.PointLight(0x31e6ff, 1.1, 360));

      const nodeMeshes = new Map<string, any>();
      const nodeById = new Map(nodes.map((node: any) => [String(node.id), node]));
      const total = Math.max(nodes.length, 1);
      const ringRadius = Math.min(170, Math.max(92, 36 + Math.sqrt(total) * 28));
      nodes.forEach((node: any, index: number) => {
        const external = Boolean(node.external) || /external|domain|ip/.test(String(node.type || "").toLowerCase());
        const angle = (index / total) * Math.PI * 2;
        const layer = external ? 1 : 0;
        const radius = ringRadius * (external ? 1.12 : 0.58);
        const y = external ? 18 * Math.sin(angle * 1.7) : -22 + 28 * Math.sin(angle * 1.3);
        const position = new THREE.Vector3(Math.cos(angle) * radius, y + layer * 12, Math.sin(angle) * radius);
        const material = new THREE.MeshStandardMaterial({
          color: external ? 0xffd36a : /service/.test(String(node.type || "")) ? 0x55d7ff : 0x75e6a8,
          emissive: external ? 0x6b3a00 : 0x06321f,
          emissiveIntensity: 0.45,
          metalness: 0.24,
          roughness: 0.36,
        });
        const geometry = external ? new THREE.OctahedronGeometry(6.8, 0) : new THREE.SphereGeometry(5.8, 22, 14);
        const mesh = new THREE.Mesh(geometry, material);
        mesh.position.copy(position);
        scene.add(mesh);
        nodeMeshes.set(String(node.id), mesh);
        const label = makeLabelSprite(THREE, String(node.title || node.name || node.id).slice(0, 36), external ? "#ffe5a8" : "#d8f7ff");
        label.position.copy(position.clone().add(new THREE.Vector3(0, 12, 0)));
        label.scale.set(external ? 36 : 30, external ? 10 : 8.4, 1);
        scene.add(label);
      });

      const flowGeometry = new THREE.SphereGeometry(1.2, 12, 10);
      const edgeObjects = edges.map((edge: any, index: number) => {
        const observedFlow = flows.find((flow: any) => flow.id === edge.id);
        const observed = Boolean(observedFlow?.observed);
        const direction = String(edge.direction || observedFlow?.direction || "");
        const line = new THREE.Line(
          new THREE.BufferGeometry(),
          new THREE.LineBasicMaterial({
            color: observed ? 0x31f3ff : direction === "ingress" ? 0x8bffb3 : direction === "cross_cluster" ? 0xffd86a : 0x66a3ff,
            transparent: true,
            opacity: observed ? 0.86 : 0.46,
          })
        );
        scene.add(line);
        const particle = new THREE.Mesh(
          flowGeometry,
          new THREE.MeshBasicMaterial({ color: observed ? 0xffffff : 0x9fd8ff, transparent: true, opacity: 0.94 })
        );
        scene.add(particle);
        return { edge, line, particle, phase: (index * 0.19) % 1, observed };
      });

      const curveFor = (source: any, target: any) => {
        const midpoint = source.position.clone().add(target.position).multiplyScalar(0.5);
        midpoint.y += 35 + source.position.distanceTo(target.position) * 0.08;
        return new THREE.QuadraticBezierCurve3(source.position.clone(), midpoint, target.position.clone());
      };

      const resize = () => {
        const rect = host.getBoundingClientRect();
        renderer.setSize(Math.max(1, rect.width), Math.max(1, rect.height), false);
        camera.aspect = Math.max(1, rect.width) / Math.max(1, rect.height);
        camera.updateProjectionMatrix();
      };
      const ro = new ResizeObserver(resize);
      ro.observe(host);
      resize();

      const animate = () => {
        controls.update();
        const elapsed = performance.now() * 0.00018;
        edgeObjects.forEach(({ edge, line, particle, phase, observed }) => {
          const source = nodeMeshes.get(String(edge.source));
          const target = nodeMeshes.get(String(edge.target));
          if (!source || !target) return;
          const curve = curveFor(source, target);
          line.geometry.setFromPoints(curve.getPoints(24));
          line.geometry.attributes.position.needsUpdate = true;
          particle.position.copy(curve.getPoint((elapsed * (observed ? 2.05 : 1.2) + phase) % 1));
        });
        nodeMeshes.forEach((mesh, id) => {
          const node = nodeById.get(id);
          const pulse = /external/.test(String(node?.type || "")) ? 0.04 : 0.025;
          const scale = 1 + Math.sin(performance.now() * 0.002 + id.length) * pulse;
          mesh.scale.setScalar(scale);
        });
        renderer.render(scene, camera);
        frame = requestAnimationFrame(animate);
      };
      animate();

      cleanup = () => {
        ro.disconnect();
        controls.dispose();
        renderer.dispose();
        scene.traverse((obj: any) => {
          obj.geometry?.dispose?.();
          if (Array.isArray(obj.material)) obj.material.forEach((m: any) => m.dispose?.());
          else obj.material?.dispose?.();
        });
      };
    })().catch((error) => {
      if (!disposed) setCanvasError(webGLFailureDetail(error));
    });

    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      cleanup();
    };
  }, [graph, flows]);

  if (canvasError) return <TrafficFlow2DFallback graph={graph} flows={flows} message={canvasError} />;

  return <div className="flow-3d-wrap">
    <canvas ref={canvasRef} className="flow-3d-canvas" />
  </div>;
}

type TopologyNode = {
  id: string;
  name: string;
  title: string;
  type: string;
  kind: string;
  cluster: string;
  namespace: string;
  risk?: string;
  raw: any;
};

type TopologyEdge = {
  source: string;
  target: string;
  type: string;
  traffic?: string;
  raw: any;
};

function normalizeTopologyGraph(data: any): { nodes: TopologyNode[]; edges: TopologyEdge[] } {
  const rawNodes = asList(data?.nodes);
  const nodes = rawNodes.map((n: any, index: number) => {
    const id = String(n.id || n.name || n.title || `node-${index}`);
    const name = String(n.name || n.title || id);
    const phase = String(n.phase || n.status?.phase || n.status || "").toLowerCase();
    const completed = phase === "succeeded" || phase === "completed";
    return {
      id,
      name,
      title: String(n.title || name),
      type: String(n.type || n.category || n.kind || "node").toLowerCase(),
      kind: String(n.kind || n.type || ""),
      cluster: String(n.cluster || n.cluster_name || n.cluster_id || n.meta?.cluster || n.raw?.cluster || "local-cluster"),
      namespace: String(n.namespace || n.ns || n.meta?.namespace || n.raw?.namespace || ""),
      risk: completed ? "normal" : (n.risk || (typeof n.status === "string" ? n.status : n.health) || "normal"),
      raw: n,
    };
  });
  const known = new Set(nodes.map((n) => n.id));
  const edges = asList(data?.edges).map((e: any) => ({
    source: String(e.source || e.from || e.src || e.caller || ""),
    target: String(e.target || e.to || e.dst || e.callee || ""),
    type: String(e.type || e.protocol || "dependency"),
    traffic: e.traffic || e.qps || e.weight || "",
    raw: e,
  })).filter((e) => known.has(e.source) && known.has(e.target));
  return { nodes, edges };
}

function TopologyPage() {
  const [topology, refreshTopology] = useAsync<any>(() => apiGet("/api/cmdb/topology"), []);
  const graph = useMemo(() => normalizeTopologyGraph(topology.data), [topology.data]);
  const clusters = useMemo(() => ["all", ...Array.from(new Set(graph.nodes.map((n) => n.cluster))).filter(Boolean)], [graph.nodes]);
  const [cluster, setCluster] = useState("all");
  const [namespace, setNamespace] = useState("all");
  const [module, setModule] = useState<"relation" | "flow">("relation");
  const [workloadFilter, setWorkloadFilter] = useState("");
  const [view, setView] = useState<"2d" | "3d">(() => (localStorage.getItem("flawless-topology-view") as "2d" | "3d") || "2d");
  const [viewNotice, setViewNotice] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [analysis, setAnalysis] = useState<ApiState<any>>({ loading: false });
  const canvasApiRef = useRef<{ reset: () => void; zoom: (factor: number) => void } | null>(null);
  const handleTopologyUnavailable = useCallback((message: string) => {
    setViewNotice(message);
    setView("2d");
  }, []);
  const namespaces = useMemo(() => {
    const scoped = cluster === "all" ? graph.nodes : graph.nodes.filter((n) => n.cluster === cluster);
    return ["all", ...Array.from(new Set(scoped.map((n) => n.namespace).filter(Boolean))).sort()];
  }, [cluster, graph.nodes]);
  const workloadOptions = useMemo(() => {
    const scoped = graph.nodes.filter((node) => {
      const clusterOk = cluster === "all" || node.cluster === cluster;
      const namespaceOk = namespace === "all" || node.namespace === namespace || !node.namespace;
      const text = `${node.type} ${node.kind} ${node.name} ${node.title}`.toLowerCase();
      const useful = /deployment|statefulset|daemonset|workload|pod|service|kafka|redis|mysql|elastic|topic|queue/.test(text);
      return clusterOk && namespaceOk && useful;
    });
    const seen = new Set<string>();
    return scoped.map((node) => ({
      id: node.id,
      value: node.name,
      label: `${node.kind || node.type || "Node"}/${node.name}`,
    })).filter((item) => {
      const key = item.value.toLowerCase();
      if (!item.value || seen.has(key)) return false;
      seen.add(key);
      return true;
    }).sort((a, b) => a.label.localeCompare(b.label));
  }, [cluster, namespace, graph.nodes]);

  const visibleGraph = useMemo(() => {
    const clusterNodes = cluster === "all" ? graph.nodes : graph.nodes.filter((n) => n.cluster === cluster);
    const namespaceOwned = new Set(clusterNodes.filter((n) => namespace === "all" || n.namespace === namespace).map((n) => n.id));
    const directlyConnected = new Set<string>();
    graph.edges.forEach((edge) => {
      if (namespaceOwned.has(edge.source)) directlyConnected.add(edge.target);
      if (namespaceOwned.has(edge.target)) directlyConnected.add(edge.source);
    });
    const nodes = namespace === "all"
      ? clusterNodes
      : clusterNodes.filter((n) => namespaceOwned.has(n.id) || (!n.namespace && directlyConnected.has(n.id)));
    const ids = new Set(nodes.map((n) => n.id));
    let scopedEdges = graph.edges.filter((e) => ids.has(e.source) && ids.has(e.target));
    if (workloadFilter) {
      const wanted = workloadFilter.toLowerCase();
      const seedIds = new Set(nodes.filter((node) => {
        const text = `${node.name} ${node.title} ${node.kind}/${node.name}`.toLowerCase();
        return text.includes(wanted);
      }).map((node) => node.id));
      const related = new Set(seedIds);
      let changed = true;
      while (changed) {
        changed = false;
        scopedEdges.forEach((edge) => {
          if (related.has(edge.source) && !related.has(edge.target)) {
            related.add(edge.target);
            changed = true;
          }
          if (related.has(edge.target) && !related.has(edge.source)) {
            related.add(edge.source);
            changed = true;
          }
        });
      }
      const filteredNodes = nodes.filter((node) => related.has(node.id));
      const filteredIds = new Set(filteredNodes.map((node) => node.id));
      return { nodes: filteredNodes, edges: scopedEdges.filter((edge) => filteredIds.has(edge.source) && filteredIds.has(edge.target)) };
    }
    return { nodes, edges: scopedEdges };
  }, [cluster, namespace, workloadFilter, graph]);
  const selected = visibleGraph.nodes.find((n) => n.id === selectedId) || visibleGraph.nodes[0];

  useEffect(() => {
    localStorage.setItem("flawless-topology-view", view);
    requestAnimationFrame(() => canvasApiRef.current?.reset());
  }, [view]);

  useEffect(() => {
    if (selected && selected.id !== selectedId) setSelectedId(selected.id);
    if (!visibleGraph.nodes.length) setSelectedId("");
  }, [selected?.id, selectedId, visibleGraph.nodes.length]);

  useEffect(() => {
    if (!namespaces.includes(namespace)) setNamespace("all");
  }, [namespace, namespaces]);

  async function analyzeSelected() {
    if (!selected) return;
    setAnalysis({ loading: true });
    try {
      const data = await apiPost("/api/topology/impact", {
        selected: selected.raw || selected,
        graph: { nodes: visibleGraph.nodes.map((n) => n.raw || n), edges: visibleGraph.edges.map((e) => e.raw || e) },
        scenario: selected.type === "pod" ? "single_pod_change" : "topology_change",
      });
      setAnalysis({ loading: false, data });
    } catch (error: any) {
      setAnalysis({ loading: false, error: error.message });
    }
  }

  const policy = analysis.data?.policy || {};
  const blast = policy.blast_radius || {};
  return (
    <section className="topology-modern">
      <Panel className="topology-map">
        <PanelTitle
          icon={GitBranch}
          title={view === "2d" ? "有向依赖拓扑" : "3D 集群世界"}
          subtitle="节点与边严格来自同一份 CMDB 数据"
          action={<button className="ghost" onClick={refreshTopology}><RefreshCcw size={15} />刷新</button>}
        />
        <div className="topology-tools">
          <div className="segmented topology-view-switch">
            <button className={view === "2d" ? "active" : ""} onClick={() => { setViewNotice(""); setView("2d"); }}><Workflow size={14} />依赖图</button>
            <button className={view === "3d" ? "active" : ""} onClick={() => { setViewNotice(""); setView("3d"); }}><Network size={14} />3D 世界</button>
          </div>
          <div className="segmented topology-module-switch">
            <button className={module === "relation" ? "active" : ""} onClick={() => setModule("relation")}><GitBranch size={14} />关系模块</button>
            <button className={module === "flow" ? "active" : ""} onClick={() => setModule("flow")}><Cable size={14} />数据流模块</button>
          </div>
          <select value={cluster} onChange={(e) => { setCluster(e.target.value); setNamespace("all"); setWorkloadFilter(""); }}>
            {clusters.map((item) => <option value={item} key={item}>{item === "all" ? "所有集群" : item}</option>)}
          </select>
          <select value={namespace} onChange={(e) => { setNamespace(e.target.value); setWorkloadFilter(""); }}>
            {namespaces.map((item) => <option value={item} key={item}>{item === "all" ? "所有 Namespace" : item}</option>)}
          </select>
          <select value={workloadFilter} onChange={(e) => setWorkloadFilter(e.target.value)}>
            <option value="">所有 Workload / 端点</option>
            {workloadOptions.map((item) => <option value={item.value} key={item.id}>{item.label}</option>)}
          </select>
          {module === "relation" && <>
            <button className="ghost tiny" onClick={() => canvasApiRef.current?.zoom(0.82)}><ZoomIn size={14} />放大</button>
            <button className="ghost tiny" onClick={() => canvasApiRef.current?.zoom(1.18)}><ZoomOut size={14} />缩小</button>
            <button className="ghost tiny" onClick={() => canvasApiRef.current?.reset()}><RefreshCcw size={14} />复位</button>
          </>}
          <div className="topology-legend-inline"><span className="workload">Workload</span><span className="pod">Pod</span><span className="service">Service</span><span className="data">Data</span><span className="risk">Risk</span></div>
        </div>
        {module === "relation" ? (
          <>
            {viewNotice && <div className="topology-fallback-notice">{viewNotice}</div>}
            <div className={cx("topology-stage", view === "2d" && "mode-2d")}>
              {visibleGraph.nodes.length ? (
                view === "2d" ? <Topology2D nodes={visibleGraph.nodes} edges={visibleGraph.edges} selectedId={selected?.id || ""} onSelect={setSelectedId} apiRef={canvasApiRef} /> :
                  <TopologyCanvas nodes={visibleGraph.nodes} edges={visibleGraph.edges} selectedId={selected?.id || ""} onSelect={setSelectedId} apiRef={canvasApiRef} onUnavailable={handleTopologyUnavailable} />
              ) : (
                <EmptyState text={topology.error || topology.data?.message || "CMDB 当前没有返回拓扑节点；请确认 CMDB 已接入 Rancher/Service/Kafka/ELK 数据。"} />
              )}
            </div>
            <ExternalTrafficEmbedded cluster={cluster} namespace={namespace} workload={workloadFilter} />
          </>
        ) : (
          <TrafficFlow3DPanel cluster={cluster} namespace={namespace} workload={workloadFilter} />
        )}
      </Panel>
      <Panel className="topology-insight">
        <PanelTitle icon={BrainCircuit} title="AI 影响分析" action={<button className="primary" onClick={analyzeSelected} disabled={!selected || analysis.loading}>{analysis.loading ? <Loader2 className="spin" size={15} /> : <Play size={15} />}分析</button>} />
        <div className="insight-stack">
          <Metric title="拓扑节点" value={visibleGraph.nodes.length} />
          <Metric title="关系边" value={visibleGraph.edges.length} />
          <Metric title="CMDB 状态" value={topology.data?.status || "unknown"} />
        </div>
        {selected && (
          <div className="analysis-card selected-node">
            <span>{selected.type || "node"} · {selected.cluster}</span>
            <strong>{selected.title}</strong>
            <p>{selected.namespace || "global"} · 风险状态 {selected.risk || "normal"}</p>
          </div>
        )}
        {analysis.error && <div className="error-box">{analysis.error}</div>}
        {analysis.data ? (
          <div className="analysis-card">
            <div className="score-grid">
              <span>等级 {policy.impact_level || "-"}</span>
              <span>score {policy.impact_score ?? "-"}</span>
              <span>Amp {policy.amplification_factor ?? "-"}</span>
              <span>路径 {asList(blast.critical_paths).length}</span>
            </div>
            <div className="markdown" dangerouslySetInnerHTML={{ __html: markdownish(analysis.data.analysis || "暂无分析结果。") }} />
          </div>
        ) : (
          <div className="analysis-card">
            <strong>选择节点开始分析</strong>
            <p>系统会计算上游、下游、关键路径与变更风险。</p>
          </div>
        )}
      </Panel>
    </section>
  );
}

function TopologyCanvas({
  nodes,
  edges,
  selectedId,
  onSelect,
  apiRef,
  onUnavailable,
}: {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
  selectedId: string;
  onSelect: (id: string) => void;
  apiRef: React.MutableRefObject<{ reset: () => void; zoom: (factor: number) => void } | null>;
  onUnavailable: (message: string) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const selectedIdRef = useRef(selectedId);
  const [canvasError, setCanvasError] = useState("");

  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  useEffect(() => {
    let disposed = false;
    let frame = 0;
    let cleanup = () => {};
    const canvas = canvasRef.current;
    const host = canvas?.parentElement;
    if (!canvas || !host) return cleanup;
    setCanvasError("");

    (async () => {
      if (!canCreateWebGLContext()) throw new Error("WebGL context unavailable");
      const THREE = await import("three");
      const { OrbitControls } = await import("three/examples/jsm/controls/OrbitControls.js");
      if (disposed) return;

      const scene = new THREE.Scene();
      scene.background = new THREE.Color(0x020611);
      scene.fog = new THREE.FogExp2(0x020611, 0.00036);
      const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 7200);
      const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      const controls = new OrbitControls(camera, renderer.domElement);
      controls.enableDamping = true;
      controls.dampingFactor = 0.08;
      controls.minDistance = 42;
      controls.maxDistance = 1800;
      controls.target.set(0, 0, 0);

      scene.add(new THREE.AmbientLight(0xb9d7ff, 0.64));
      const key = new THREE.DirectionalLight(0x9ecbff, 1.28);
      key.position.set(60, 80, 90);
      scene.add(key);
      const fill = new THREE.PointLight(0x4de1ff, 1.22, 210);
      fill.position.set(-40, -20, 50);
      scene.add(fill);

      // A deterministic star field gives depth without introducing a decorative foreground object.
      const starPositions: number[] = [];
      let starSeed = 94731;
      const seeded = () => {
        starSeed = (starSeed * 16807) % 2147483647;
        return (starSeed - 1) / 2147483646;
      };
      for (let index = 0; index < 1800; index += 1) {
        starPositions.push((seeded() - 0.5) * 1800, (seeded() - 0.5) * 1100, (seeded() - 0.5) * 1200);
      }
      const starGeometry = new THREE.BufferGeometry();
      starGeometry.setAttribute("position", new THREE.Float32BufferAttribute(starPositions, 3));
      const starField = new THREE.Points(starGeometry, new THREE.PointsMaterial({ color: 0xb8d8ff, size: 0.72, transparent: true, opacity: 0.76, sizeAttenuation: true }));
      scene.add(starField);

      const clusters = Array.from(new Set(nodes.map((n) => n.cluster || "local-cluster")));
      const clusterNodes = new Map(clusters.map((name) => [name, nodes.filter((node) => node.cluster === name)]));
      const clusterCenters = new Map<string, any>();
      const hostRect = host.getBoundingClientRect();
      const hostWidth = Math.max(360, hostRect.width || 960);
      const hostHeight = Math.max(420, hostRect.height || 720);
      camera.aspect = hostWidth / hostHeight;
      const compactness = Math.max(0.76, Math.min(1.08, Math.min(hostWidth / 1120, hostHeight / 760)));
      const columns = Math.max(1, Math.ceil(Math.sqrt(clusters.length)));
      const rows = Math.max(1, Math.ceil(clusters.length / columns));
      const largestCluster = Math.max(...clusters.map((name) => clusterNodes.get(name)?.length || 1));
      const densityRadius = (58 + Math.sqrt(largestCluster) * 12) * compactness;
      const fitRadius = Math.min(hostWidth / (columns * 2.38), hostHeight / (rows * 2.28));
      const galaxyRadius = Math.max(62, Math.min(168, densityRadius, fitRadius));
      const clusterSpan = galaxyRadius * 2;
      const worldSpacingX = galaxyRadius * 2.38;
      const worldSpacingY = galaxyRadius * 2.28;
      const sceneWidth = Math.max(clusterSpan, (columns - 1) * worldSpacingX + clusterSpan);
      const sceneHeight = Math.max(clusterSpan, (rows - 1) * worldSpacingY + clusterSpan);
      const fitFov = THREE.MathUtils.degToRad(camera.fov);
      const cameraDistance = Math.max(280, Math.max(
        sceneHeight / (2 * Math.tan(fitFov / 2)),
        sceneWidth / (2 * Math.tan(fitFov / 2) * Math.max(0.45, camera.aspect)),
      ) * 1.18);
      controls.minDistance = Math.max(22, galaxyRadius * 0.36);
      controls.maxDistance = Math.max(1300, cameraDistance * 2.7);
      const initialCamera = new THREE.Vector3(sceneWidth * 0.08, sceneHeight * 0.18, cameraDistance);
      camera.position.copy(initialCamera);
      camera.far = Math.max(7200, cameraDistance * 4);
      camera.updateProjectionMatrix();

      const layerOf = (node: TopologyNode) => {
        const text = `${node.type} ${node.kind} ${node.name}`.toLowerCase();
        if (/cluster|namespace/.test(text)) return 0;
        if (/deployment|statefulset|daemonset|workload|replicaset|job|cronjob/.test(text)) return 1;
        if (/service|ingress|gateway|endpoint/.test(text)) return 2;
        if (/pod|container/.test(text)) return 3;
        if (/kafka|redis|mysql|elastic|postgres|data|storage|pvc|pv|topic|queue|middleware/.test(text)) return 4;
        if (/node|infra|host/.test(text)) return 5;
        return 2;
      };
      const shellRatio = (layer: number) => {
        const table: Record<number, number> = { 0: 0.08, 1: 0.34, 2: 0.50, 3: 0.69, 4: 0.84, 5: 0.98 };
        return table[layer] ?? 0.58;
      };
      const spherePoint = (index: number, count: number, radius: number, twist: number) => {
        if (count <= 1) return { x: 0, y: radius, z: 0 };
        const goldenAngle = Math.PI * (3 - Math.sqrt(5));
        const y = 1 - (index / (count - 1)) * 2;
        const radial = Math.sqrt(Math.max(0, 1 - y * y));
        const theta = index * goldenAngle + twist;
        return {
          x: Math.cos(theta) * radial * radius,
          y: y * radius,
          z: Math.sin(theta) * radial * radius,
        };
      };
      const placement = new Map<string, { x: number; y: number; z: number }>();
      clusters.forEach((cluster) => {
        const members = clusterNodes.get(cluster) || [];
        const byLayer = new Map<number, TopologyNode[]>();
        members.forEach((node) => {
          const layer = layerOf(node);
          const bucket = byLayer.get(layer) || [];
          bucket.push(node);
          byLayer.set(layer, bucket);
        });
        Array.from(byLayer.entries()).forEach(([layer, layerNodes]) => {
          const baseRadius = Math.max(8, galaxyRadius * shellRatio(layer));
          const shellCapacity = Math.max(12, Math.floor((4 * Math.PI * baseRadius * baseRadius) / 900));
          layerNodes.forEach((node, index) => {
            if (layer === 0 && layerNodes.length <= 2) {
              placement.set(node.id, { x: 0, y: index * 12, z: 0 });
              return;
            }
            const shell = Math.floor(index / shellCapacity);
            const shellIndex = index % shellCapacity;
            const shellSize = Math.min(shellCapacity, layerNodes.length - shell * shellCapacity);
            placement.set(node.id, spherePoint(
              shellIndex,
              shellSize,
              Math.min(galaxyRadius * 0.98, baseRadius + shell * 18),
              layer * 0.71 + shell * 0.33,
            ));
          });
        });
      });

      const worlds: any[] = [];
      clusters.forEach((cluster, index) => {
        const column = index % columns;
        const row = Math.floor(index / columns);
        const center = new THREE.Vector3((column - (columns - 1) / 2) * worldSpacingX, ((rows - 1) / 2 - row) * worldSpacingY, 0);
        clusterCenters.set(cluster, center);
        const galaxy = new THREE.Group();
        galaxy.position.copy(center);
        const boundary = new THREE.Mesh(
          new THREE.SphereGeometry(galaxyRadius * 1.04, 34, 24),
          new THREE.MeshBasicMaterial({ color: 0x3d8fbd, wireframe: true, transparent: true, opacity: 0.15 })
        );
        galaxy.add(boundary);
        [0.36, 0.52, 0.70, 0.86, 1.0].forEach((ratio, orbitIndex) => {
          const orbit = new THREE.Mesh(
            new THREE.TorusGeometry(galaxyRadius * ratio, 0.24, 6, 120),
            new THREE.MeshBasicMaterial({ color: orbitIndex === 2 ? 0x4d9acb : 0x285071, transparent: true, opacity: orbitIndex === 2 ? 0.26 : 0.14 })
          );
          orbit.rotation.set(orbitIndex * 0.47, orbitIndex * 0.63, orbitIndex * 0.29);
          galaxy.add(orbit);
        });
        scene.add(galaxy);
        const core = new THREE.Mesh(
          new THREE.SphereGeometry(Math.max(5.2, galaxyRadius * 0.055), 32, 22),
          new THREE.MeshStandardMaterial({ color: 0x7bc8ff, emissive: 0x168bff, emissiveIntensity: 1.4, roughness: 0.24, metalness: 0.1 })
        );
        core.position.copy(center);
        scene.add(core);
        const coreLight = new THREE.PointLight(index % 2 ? 0x59e1ff : 0x74a7ff, 1.6, galaxyRadius * 1.5);
        coreLight.position.copy(center);
        scene.add(coreLight);
        worlds.push({ boundary: galaxy, core, speed: 0.00012 + (index % 3) * 0.00004 });
        const label = makeLabelSprite(THREE, cluster, "#9bd7ff");
        label.position.copy(center.clone().add(new THREE.Vector3(0, galaxyRadius * 1.18, 0)));
        label.scale.set(40, 9, 1);
        scene.add(label);
      });

      const nodeMeshes = new Map<string, any>();
      const labelSprites = new Map<string, any>();
      const originalPositions = new Map<string, any>();
      const nodeClusterById = new Map(nodes.map((node) => [node.id, node.cluster]));
      const nodeById = new Map(nodes.map((node) => [node.id, node]));
      nodes.forEach((node) => {
        const center = clusterCenters.get(node.cluster) || new THREE.Vector3();
        const type = `${node.type} ${node.kind}`.toLowerCase();
        const placed = placement.get(node.id) || { x: 0, y: clusterSpan * 0.26, z: 0 };
        const position = center.clone().add(new THREE.Vector3(
          placed.x,
          placed.y,
          placed.z
        ));
        const size = node.type.includes("data") || node.name.toLowerCase().includes("kafka") ? 6.1 : node.type.includes("service") ? 4.9 : node.type.includes("pod") ? 3.65 : 4.75;
        const geometry = type.includes("pod")
          ? new THREE.SphereGeometry(size, 22, 14)
          : /deployment|statefulset|daemonset|workload/.test(type)
            ? new THREE.BoxGeometry(size * 2, size * 1.35, size * 1.7)
            : type.includes("service") || type.includes("ingress")
              ? new THREE.OctahedronGeometry(size, 0)
              : type.includes("data") || type.includes("kafka")
                ? new THREE.CylinderGeometry(size, size, size * 1.7, 18)
                : new THREE.IcosahedronGeometry(size, 0);
        const mesh = new THREE.Mesh(
          geometry,
          new THREE.MeshStandardMaterial({
            color: colorForNode(THREE, node),
            emissive: selectedId === node.id ? 0x33ddff : 0x071b33,
            emissiveIntensity: selectedId === node.id ? 0.78 : 0.28,
            metalness: 0.28,
            roughness: 0.38,
          })
        );
        mesh.position.copy(position);
        mesh.userData = { id: node.id };
        scene.add(mesh);
        nodeMeshes.set(node.id, mesh);
        originalPositions.set(node.id, position.clone());
        const label = makeLabelSprite(THREE, `${node.kind || node.type} · ${node.name}`, selectedId === node.id ? "#ffffff" : "#d7e7f8");
        label.position.copy(position.clone().add(new THREE.Vector3(0, size + 2.2, 0)));
        const labelScale = type.includes("pod") ? 18 : type.includes("service") ? 28 : 32;
        const showAllLabels = nodes.length <= 18;
        label.scale.set(labelScale, labelScale * 0.28, 1);
        label.visible = showAllLabels || !type.includes("pod") || selectedId === node.id || String(node.risk || "").toLowerCase().includes("high");
        scene.add(label);
        labelSprites.set(node.id, label);
      });

      const flowGeometry = new THREE.SphereGeometry(0.62, 12, 9);
      const edgeLines = edges.map((edge, index) => {
        const isDataFlow = /kafka|data|stream|log|elk/i.test(edge.type);
        const line = new THREE.Line(
          new THREE.BufferGeometry(),
          new THREE.LineBasicMaterial({
            color: isDataFlow ? 0xffcc66 : 0x49c6ff,
            transparent: true,
            opacity: isDataFlow ? 0.88 : 0.52,
          })
        );
        scene.add(line);
        const particle = new THREE.Mesh(
          flowGeometry,
          new THREE.MeshBasicMaterial({ color: isDataFlow ? 0xffd477 : 0x8ae8ff, transparent: true, opacity: 0.92 })
        );
        scene.add(particle);
        return { edge, line, particle, phase: (index * 0.173) % 1, isDataFlow };
      });

      const curveFor = (source: any, target: any) => {
        const midpoint = source.position.clone().add(target.position).multiplyScalar(0.5);
        const sourceCenter = clusterCenters.get(nodeClusterById.get(source.userData.id) || "") || new THREE.Vector3();
        const targetCenter = clusterCenters.get(nodeClusterById.get(target.userData.id) || "") || new THREE.Vector3();
        const sameWorld = sourceCenter.distanceTo(targetCenter) < 0.01;
        if (sameWorld) {
          const outward = midpoint.clone().sub(sourceCenter).normalize().multiplyScalar(13);
          midpoint.add(outward);
        } else {
          midpoint.y += 28 + source.position.distanceTo(target.position) * 0.07;
        }
        return new THREE.QuadraticBezierCurve3(source.position.clone(), midpoint, target.position.clone());
      };

      const raycaster = new THREE.Raycaster();
      const pointer = new THREE.Vector2();
      const dragPlane = new THREE.Plane();
      const intersection = new THREE.Vector3();
      let dragged: any = null;

      const resize = () => {
        const rect = host.getBoundingClientRect();
        renderer.setSize(Math.max(1, rect.width), Math.max(1, rect.height), false);
        camera.aspect = Math.max(1, rect.width) / Math.max(1, rect.height);
        const responsiveDistance = cameraDistance * Math.max(1, 0.9 / camera.aspect);
        camera.position.z = responsiveDistance;
        camera.updateProjectionMatrix();
      };
      const ro = new ResizeObserver(resize);
      ro.observe(host);
      resize();

      const setPointer = (event: PointerEvent) => {
        const rect = canvas.getBoundingClientRect();
        pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
        pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      };
      const onPointerDown = (event: PointerEvent) => {
        setPointer(event);
        raycaster.setFromCamera(pointer, camera);
        const hits = raycaster.intersectObjects(Array.from(nodeMeshes.values()), false);
        if (!hits.length) return;
        dragged = hits[0].object;
        onSelect(String(dragged.userData.id));
        controls.enabled = false;
        dragPlane.setFromNormalAndCoplanarPoint(camera.getWorldDirection(new THREE.Vector3()).normalize(), dragged.position);
        canvas.setPointerCapture(event.pointerId);
      };
      const onPointerMove = (event: PointerEvent) => {
        if (!dragged) return;
        setPointer(event);
        raycaster.setFromCamera(pointer, camera);
        if (raycaster.ray.intersectPlane(dragPlane, intersection)) {
          dragged.position.copy(intersection);
          const label = labelSprites.get(String(dragged.userData.id));
          if (label) label.position.copy(intersection.clone().add(new THREE.Vector3(0, 4, 0)));
        }
      };
      const onPointerUp = (event: PointerEvent) => {
        dragged = null;
        controls.enabled = true;
        try { canvas.releasePointerCapture(event.pointerId); } catch {}
      };
      canvas.addEventListener("pointerdown", onPointerDown);
      canvas.addEventListener("pointermove", onPointerMove);
      canvas.addEventListener("pointerup", onPointerUp);
      canvas.addEventListener("pointerleave", onPointerUp);

      apiRef.current = {
        reset: () => {
          nodeMeshes.forEach((mesh, id) => {
            const pos = originalPositions.get(id);
            if (pos) mesh.position.copy(pos);
            const label = labelSprites.get(id);
            if (label && pos) label.position.copy(pos.clone().add(new THREE.Vector3(0, 4, 0)));
          });
          camera.position.set(initialCamera.x, initialCamera.y, cameraDistance * Math.max(1, 0.9 / camera.aspect));
          controls.target.set(0, 0, 0);
          controls.update();
        },
        zoom: (factor: number) => {
          camera.position.multiplyScalar(factor);
          camera.updateProjectionMatrix();
        },
      };

      const animate = () => {
        controls.update();
        starField.rotation.y += 0.000025;
        worlds.forEach(({ boundary, speed }) => {
          boundary.rotation.z += speed;
        });
        nodeMeshes.forEach((mesh, id) => {
          const active = selectedIdRef.current === id;
          mesh.material.emissive.setHex(active ? 0x33ddff : 0x071b33);
          mesh.material.emissiveIntensity = active ? 0.86 : 0.18;
          mesh.scale.lerp(new THREE.Vector3(active ? 1.28 : 1, active ? 1.28 : 1, active ? 1.28 : 1), 0.12);
          const label = labelSprites.get(id);
          const node = nodeById.get(id);
          const nodeType = `${node?.type || ""} ${node?.kind || ""}`.toLowerCase();
          if (label && nodeType.includes("pod")) {
            label.visible = nodes.length <= 18 || active || String(node?.risk || "").toLowerCase().includes("high");
          }
        });
        const elapsed = performance.now() * 0.00016;
        edgeLines.forEach(({ edge, line, particle, phase, isDataFlow }) => {
          const source = nodeMeshes.get(edge.source);
          const target = nodeMeshes.get(edge.target);
          if (source && target) {
            const curve = curveFor(source, target);
            line.geometry.setFromPoints(curve.getPoints(18));
            line.geometry.attributes.position.needsUpdate = true;
            particle.position.copy(curve.getPoint((elapsed * (isDataFlow ? 1.55 : 1) + phase) % 1));
          }
        });
        renderer.render(scene, camera);
        frame = requestAnimationFrame(animate);
      };
      animate();

      cleanup = () => {
        ro.disconnect();
        canvas.removeEventListener("pointerdown", onPointerDown);
        canvas.removeEventListener("pointermove", onPointerMove);
        canvas.removeEventListener("pointerup", onPointerUp);
        canvas.removeEventListener("pointerleave", onPointerUp);
        controls.dispose();
        renderer.dispose();
        scene.traverse((obj: any) => {
          obj.geometry?.dispose?.();
          if (Array.isArray(obj.material)) obj.material.forEach((m: any) => m.dispose?.());
          else obj.material?.dispose?.();
        });
      };
    })().catch((error) => {
      if (!disposed) {
        const message = webGLFailureDetail(error);
        setCanvasError(message);
        onUnavailable(message);
      }
    });

    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      cleanup();
      apiRef.current = null;
    };
  }, [apiRef, edges, nodes, onSelect, onUnavailable]);

  return <div className="topology-canvas-wrap">
    <canvas className="topology-canvas" ref={canvasRef} />
    {canvasError && <div className="topology-canvas-error">{canvasError}</div>}
  </div>;
}

function colorForNode(THREE: any, node: TopologyNode) {
  const text = `${node.type} ${node.name} ${node.kind}`.toLowerCase();
  if (String(node.risk || "").toLowerCase().includes("high")) return new THREE.Color(0xff5d73);
  if (text.includes("kafka") || text.includes("data") || text.includes("redis") || text.includes("mysql") || text.includes("elastic")) return new THREE.Color(0xffcc66);
  if (text.includes("service") || text.includes("svc")) return new THREE.Color(0x55d7ff);
  if (text.includes("pod")) return new THREE.Color(0x69e6a3);
  if (text.includes("infra") || text.includes("node")) return new THREE.Color(0xb59cff);
  return new THREE.Color(0x77a7ff);
}

function makeLabelSprite(THREE: any, label: string, color: string) {
  const canvas = document.createElement("canvas");
  canvas.width = 768;
  canvas.height = 176;
  const ctx = canvas.getContext("2d")!;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.font = "700 54px Inter, Segoe UI, Microsoft YaHei, Arial";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "rgba(3, 12, 28, 0.92)";
  roundRect(ctx, 18, 38, 732, 100, 22);
  ctx.fill();
  ctx.strokeStyle = "rgba(116, 199, 255, 0.55)";
  ctx.lineWidth = 3;
  ctx.stroke();
  ctx.fillStyle = color;
  const text = label.length > 22 ? `${label.slice(0, 20)}...` : label;
  ctx.fillText(text, 384, 88);
  const texture = new THREE.CanvasTexture(canvas);
  texture.needsUpdate = true;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true }));
  sprite.scale.set(24, 5.5, 1);
  return sprite;
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function ReleaseJobTracker({ initial }: { initial: any }) {
  const [job, setJob] = useState(initial);
  const active = ["queued", "running", "awaiting_approval", "resume_pending", "cancelling"].includes(String(job?.status || ""));
  useEffect(() => {
    if (!active || !job?.id) return;
    const timer = window.setTimeout(async () => {
      try { setJob(await apiGet(`/api/ops/jobs/${encodeURIComponent(job.id)}`)); } catch { /* next refresh can retry */ }
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [active, job?.id, job?.updated_at, job?.events?.length]);
  return <OpsJobProgress job={job} compact onCancel={active ? async () => setJob(await apiPost(`/api/ops/jobs/${encodeURIComponent(job.id)}/cancel`, {})) : undefined} />;
}

function ReleaseReportPanel({ report }: { report: any }) {
  if (!report) return null;
  const shortRisks = asList(report.short_term_risks);
  const longRisks = asList(report.long_term_risks);
  const recommendations = asList(report.recommendations);
  const evidence = asList(report.evidence);
  return <div className="release-report">
    <div className="release-report-head">
      <ShieldCheck size={15} />
      <div><strong>{report.headline || "发布风险摘要"}</strong><span>{report.risk_decision || "需要人工复核后执行。"}</span></div>
    </div>
    <div className="release-report-grid">
      <section><b>灰度允许范围</b><p>{report.allowed_scope || "未生成灰度建议。"}</p></section>
      <section><b>爆炸半径</b><p>{report.blast_radius || "拓扑证据不足，建议先补充 CMDB/调用链数据。"}</p></section>
      <section><b>镜像检查</b><p>{report.image_check || "未发现镜像变更。"}</p></section>
    </div>
    <div className="release-report-columns">
      <section><b>短期风险</b>{shortRisks.slice(0, 3).map((item: any) => <p key={item}>{item}</p>)}</section>
      <section><b>长期风险</b>{longRisks.slice(0, 3).map((item: any) => <p key={item}>{item}</p>)}</section>
      <section><b>变更建议</b>{recommendations.slice(0, 4).map((item: any) => <p key={item}>{item}</p>)}</section>
      <section><b>判定依据</b>{evidence.slice(0, 5).map((item: any) => <p key={item}>{item}</p>)}</section>
    </div>
  </div>;
}

function ReliabilityPage() {
  const [summary, refreshSummary] = useAsync<any>(() => apiGet("/api/reliability/summary"), []);
  const [releaseState, refreshReleases] = useAsync<any>(() => apiGet("/api/releases"), []);
  const [inventory] = useAsync<any>(() => apiGet("/api/rancher/inventory").catch(() => ({ clusters: [], inventory: [] })), []);
  const [objective, setObjective] = useState({ service: "", target_percent: "99.9", window_days: "30", observed_availability_percent: "100", observed_minutes: "43200", downtime_minutes: "0" });
  const [release, setRelease] = useState({ release_mode: "existing", change_channel: "standard", emergency_action: "rollback", emergency_reason: "", service: "", cluster: "local", namespace: "default", workload_kind: "Deployment", workload_name: "", container_name: "app", image: "", change_summary: "", manifest_yaml: "" });
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [jobs, setJobs] = useState<Record<string, any>>({});
  const objectives = asList(summary.data?.objectives);
  const auditStorage = summary.data?.audit_storage || {};
  const releases = asList(releaseState.data?.releases);
  const clusters = asList(inventory.data?.clusters);
  const scopedInventory = asList(inventory.data?.inventory).filter((item: any) => !release.cluster || release.cluster === item.cluster?.id || release.cluster === item.cluster?.name);
  const namespaces = Array.from(new Set(scopedInventory.flatMap((item: any) => asList(item.namespaces).map((entry: any) => String(entry.name))))).sort();
  const workloads = scopedInventory.flatMap((item: any) => asList(item.workloads)).filter((item: any) => !release.namespace || item.namespace === release.namespace);

  function generateManifest() {
    const name = release.workload_name || "new-application";
    const namespace = release.namespace || "default";
    const image = release.image || "registry.example.com/application:v1.0.0";
    const kind = release.workload_kind || "Deployment";
    setRelease((current) => ({ ...current, manifest_yaml: `apiVersion: apps/v1\nkind: ${kind}\nmetadata:\n  name: ${name}\n  namespace: ${namespace}\n  labels:\n    app.kubernetes.io/name: ${name}\nspec:\n  replicas: 2\n  selector:\n    matchLabels:\n      app.kubernetes.io/name: ${name}\n  template:\n    metadata:\n      labels:\n        app.kubernetes.io/name: ${name}\n    spec:\n      automountServiceAccountToken: false\n      securityContext:\n        runAsNonRoot: true\n      containers:\n        - name: ${release.container_name || "app"}\n          image: ${image}\n          securityContext:\n            allowPrivilegeEscalation: false\n          resources:\n            requests:\n              cpu: 100m\n              memory: 128Mi\n            limits:\n              cpu: 1\n              memory: 512Mi\n          readinessProbe:\n            httpGet:\n              path: /health\n              port: 8080\n            periodSeconds: 10\n` }));
  }

  async function saveObjective(event: React.FormEvent) {
    event.preventDefault(); setBusy("objective"); setError(""); setNotice("");
    try {
      await apiPost("/api/reliability/objectives", {
        ...objective,
        target_percent: Number(objective.target_percent),
        window_days: Number(objective.window_days),
        observed_availability_percent: Number(objective.observed_availability_percent),
        observed_minutes: Number(objective.observed_minutes),
        downtime_minutes: Number(objective.downtime_minutes),
      });
      refreshSummary();
    } catch (requestError: any) { setError(requestError.message); }
    finally { setBusy(""); }
  }

  async function submitRelease(event: React.FormEvent) {
    event.preventDefault(); setBusy("release"); setError(""); setNotice("");
    const payload = {
      ...release,
      cluster: release.cluster || "local",
      namespace: release.namespace || "default",
      service: (release.service || release.workload_name || "application-release").trim(),
      workload_name: release.workload_name.trim(),
      container_name: release.container_name.trim(),
      image: release.image.trim(),
      manifest_yaml: release.manifest_yaml.trim(),
    };
    if (payload.release_mode === "existing" && !payload.workload_name) {
      setBusy("");
      setError("请选择要变更的现有 Workload；如果是新应用，请切换到“发布新应用”并提交完整 YAML。");
      return;
    }
    const emergency = payload.change_channel === "emergency_recovery";
    if (emergency && payload.emergency_reason.trim().length < 8) {
      setBusy("");
      setError("紧急修复必须填写故障现象、业务影响或恢复理由，至少 8 个字符。");
      return;
    }
    if (emergency && payload.emergency_action === "rollback" && !payload.image) {
      setBusy("");
      setError("回滚到稳定版本必须填写上一稳定镜像版本或 digest。");
      return;
    }
    if (emergency && payload.emergency_action === "restore_config" && !payload.manifest_yaml) {
      setBusy("");
      setError("恢复误删配置必须提交完整的期望状态 YAML。");
      return;
    }
    if (payload.release_mode === "existing" && !payload.image && !payload.manifest_yaml && !(emergency && payload.emergency_action === "restart_component")) {
      setBusy("");
      setError("现有 Workload 发布至少要填写不可变镜像，或提交完整期望状态 YAML；否则平台无法判断你要变更什么。");
      return;
    }
    if (payload.image && !payload.container_name) {
      setBusy("");
      setError("镜像发布必须指定 Container 名称，避免改错同 Pod 内其他容器。");
      return;
    }
    try {
      const response = await apiPost<any>("/api/releases", payload);
      refreshReleases();
      setNotice(`风险判定已生成：${response.release?.status || "awaiting_approval"}。通过后会在下方出现“批准”，批准后出现“执行发布”。`);
    } catch (requestError: any) { setError(requestError.message); }
    finally { setBusy(""); }
  }

  async function approve(item: any) {
    const id = item.id;
    setBusy(id); setError(""); setNotice("");
    const comment = item.change_channel === "emergency_recovery"
      ? `已复核紧急修复动作 ${item.emergency_action}、影响范围和回退条件`
      : "已核对 SLO、影响面和发布目标";
    try { await apiPost(`/api/releases/${encodeURIComponent(id)}/approve`, { confirm: true, comment }); refreshReleases(); setNotice("已批准该发布申请，现在可以执行发布。"); }
    catch (requestError: any) { setError(requestError.message); }
    finally { setBusy(""); }
  }

  async function execute(id: string) {
    setBusy(id); setError(""); setNotice("");
    try {
      const response = await apiPost<any>(`/api/releases/${encodeURIComponent(id)}/execute`, {});
      setJobs((current) => ({ ...current, [id]: response.job }));
      refreshReleases();
      setNotice("发布任务已提交到受控运维执行流，下方会持续展示步骤、回执和恢复验证。");
    } catch (requestError: any) { setError(requestError.message); }
    finally { setBusy(""); }
  }

  return <section className="workspace-grid reliability-workspace">
    <div className="reliability-hero span-all">
      <div><span><ShieldCheck size={16} />SRE 发布控制面</span><h2>稳定性决定发布速度</h2><p>默认 99.9% SLO 对应月度约 43.2 分钟错误预算。预算用完后冻结常规发布，只放行故障恢复和回滚。</p></div>
      <div className={cx("budget-state", Number(summary.data?.summary?.exhausted || 0) > 0 && "frozen")}><strong>{summary.data?.summary?.changes_frozen || 0}</strong><span>冻结中的服务</span></div>
    </div>
    <div className="metric-strip span-all">
      <Metric title="SLO 服务" value={summary.data?.summary?.total || 0} />
      <Metric title="预算健康" value={summary.data?.summary?.healthy || 0} tone="good" />
      <Metric title="预算告急" value={summary.data?.summary?.at_risk || 0} />
      <Metric title="预算耗尽" value={summary.data?.summary?.exhausted || 0} tone={summary.data?.summary?.exhausted ? "danger" : undefined} />
    </div>
    {auditStorage.active_path && !auditStorage.durable && <div className="audit-storage-warning span-all"><ShieldCheck size={14} /><span>发布审计当前使用应急存储 <code>{auditStorage.active_path}</code>。提交仍可用，但 Pod 重建前应恢复 PVC 写入能力，确保审计长期保存。</span></div>}
    <Panel className="span-all">
      <PanelTitle icon={Gauge} title="错误预算" subtitle="同一份预算同时约束 SRE 自动变更与应用发布" action={<button className="ghost tiny" onClick={refreshSummary}><RefreshCcw size={13} />刷新</button>} />
      <div className="budget-grid">
        {objectives.map((item: any) => {
          const budget = item.budget || {};
          const used = Math.min(100, Number(budget.consumed_ratio || 0) * 100);
          return <article className={cx("budget-card", budget.state)} key={item.id}>
            <header><div><strong>{item.service}</strong><span>{item.cluster || "all"} / {item.namespace || "all"}</span></div><b>{budget.state === "exhausted" ? "变更冻结" : budget.state === "at_risk" ? "预算告急" : "健康"}</b></header>
            <div className="budget-value"><strong>{budget.target_percent}%</strong><span>SLO · {budget.error_budget_percent}% 错误预算</span></div>
            <div className="budget-bar"><i style={{ width: `${used}%` }} /></div>
            <footer><span>已消耗 {used.toFixed(1)}%</span><span>剩余 {budget.remaining_downtime_minutes ?? 0} 分钟</span><span>燃烧率 {budget.burn_rate ?? 0}x</span></footer>
          </article>;
        })}
      </div>
    </Panel>
    <Panel>
      <PanelTitle icon={Settings2} title="定义 SLO" subtitle="可由监控同步作业调用同一 API 持续更新可用率证据" />
      <form className="governance-form" onSubmit={saveObjective}>
        <label>应用<input value={objective.service} onChange={(e) => setObjective({ ...objective, service: e.target.value })} /></label>
        <div className="form-pair"><label>SLO 目标 %<input type="number" step="0.01" min="50" max="99.999" value={objective.target_percent} onChange={(e) => setObjective({ ...objective, target_percent: e.target.value })} /></label><label>窗口 天<input type="number" min="1" value={objective.window_days} onChange={(e) => setObjective({ ...objective, window_days: e.target.value })} /></label></div>
        <div className="form-pair"><label>实际可用率 %<input type="number" step="0.001" min="0" max="100" value={objective.observed_availability_percent} onChange={(e) => setObjective({ ...objective, observed_availability_percent: e.target.value })} /></label><label>故障分钟<input type="number" min="0" value={objective.downtime_minutes} onChange={(e) => setObjective({ ...objective, downtime_minutes: e.target.value })} /></label></div>
        <button className="primary" disabled={busy === "objective"}><Save size={15} />保存并重算预算</button>
      </form>
    </Panel>
    <Panel>
      <PanelTitle icon={GitBranch} title="提交应用变更" subtitle="选择真实目标或新建 Workload，提交期望 YAML 后执行安全校验、风险门禁与人工审批" />
      <form className="governance-form" onSubmit={submitRelease}>
        <div className="release-channel-switch"><button type="button" className={release.change_channel === "standard" ? "active" : ""} onClick={() => setRelease({ ...release, change_channel: "standard", emergency_reason: "" })}><GitBranch size={13} />常规发布</button><button type="button" className={release.change_channel === "emergency_recovery" ? "active emergency" : ""} onClick={() => setRelease({ ...release, change_channel: "emergency_recovery", release_mode: "existing", emergency_action: "rollback", workload_name: "", image: "", manifest_yaml: "" })}><Activity size={13} />紧急修复通道</button></div>
        {release.change_channel === "standard" ? <div className="release-mode-switch"><button type="button" className={release.release_mode === "existing" ? "active" : ""} onClick={() => setRelease({ ...release, release_mode: "existing", workload_name: "", manifest_yaml: "" })}>变更现有应用</button><button type="button" className={release.release_mode === "new" ? "active" : ""} onClick={() => setRelease({ ...release, release_mode: "new", workload_name: "", manifest_yaml: "" })}>发布新应用</button></div> : <div className="emergency-policy-note"><ShieldCheck size={14} /><span>只允许恢复稳定性的限定动作。错误预算耗尽时可进入审批，但不会跳过安全校验、审计、人工确认和恢复验证。</span></div>}
        {release.change_channel === "emergency_recovery" && <label>紧急动作<select value={release.emergency_action} onChange={(e) => setRelease({ ...release, emergency_action: e.target.value, image: "", manifest_yaml: "" })}><option value="rollback">回滚到上一稳定版本</option><option value="restore_config">恢复被误删的配置</option><option value="restart_component">重启故障组件</option></select></label>}
        <label>应用标识<input value={release.service} onChange={(e) => setRelease({ ...release, service: e.target.value })} placeholder="用于匹配 SLO，输入业务应用名" /></label>
        <div className="form-pair">
          <label>集群<select value={release.cluster} onChange={(e) => setRelease({ ...release, cluster: e.target.value || "local", namespace: e.target.value === "local" ? "default" : "", workload_name: "" })}><option value="local">本地 Agent 集群</option>{clusters.filter((item: any) => item.id !== "local").map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></label>
          <label>Namespace<select value={release.namespace} onChange={(e) => setRelease({ ...release, namespace: e.target.value || "default", workload_name: "" })}><option value="">选择 Namespace</option>{namespaces.map((item) => <option key={item} value={item}>{item}</option>)}{release.cluster === "local" && !namespaces.includes("default") && <option value="default">default</option>}</select></label>
        </div>
        {release.release_mode === "existing" ? <label>现有 Workload<select value={release.workload_name ? `${release.workload_kind}|${release.workload_name}` : ""} onChange={(e) => { const [kind, name] = e.target.value.split("|"); setRelease({ ...release, workload_kind: kind || "Deployment", workload_name: name || "", service: release.service || name || "" }); }}><option value="">选择 Workload</option>{workloads.map((item: any) => <option key={`${item.namespace}-${item.kind}-${item.name}`} value={`${item.kind}|${item.name}`}>{item.kind}/{item.name}</option>)}</select></label> : <div className="form-pair"><label>资源类型<select value={release.workload_kind} onChange={(e) => setRelease({ ...release, workload_kind: e.target.value })}><option>Deployment</option><option>StatefulSet</option><option>DaemonSet</option></select></label><label>新 Workload 名称<input value={release.workload_name} onChange={(e) => setRelease({ ...release, workload_name: e.target.value, service: release.service || e.target.value })} placeholder="new-application" /></label></div>}
        {(release.change_channel === "standard" || release.emergency_action === "rollback") && <div className="form-pair"><label>Container<input value={release.container_name} onChange={(e) => setRelease({ ...release, container_name: e.target.value })} /></label><label>{release.change_channel === "emergency_recovery" ? "上一稳定镜像" : "不可变镜像"}<input placeholder="registry/app:v1.2.3" value={release.image} onChange={(e) => setRelease({ ...release, image: e.target.value })} /></label></div>}
        {(release.change_channel === "standard" || release.emergency_action === "restore_config") && <label className="manifest-editor-label"><span>{release.emergency_action === "restore_config" ? "恢复后的期望状态 YAML" : "期望状态 YAML"} <button type="button" className="ghost tiny" onClick={generateManifest}><FileUp size={13} />生成生产模板</button></span><textarea className="manifest-editor" value={release.manifest_yaml} onChange={(e) => setRelease({ ...release, manifest_yaml: e.target.value })} placeholder={release.release_mode === "new" ? "新应用必须提交完整 apps/v1 Workload YAML" : "提交完整期望 YAML；平台会校验并生成可审计差异"} /></label>}
        <div className="manifest-policy"><ShieldCheck size={14} /><span>提交前检查：目标一致性、不可变镜像、非特权运行、ServiceAccount Token、hostPath、Linux capabilities。Secret 不允许进入该入口。</span></div>
        {release.change_channel === "emergency_recovery" && <label>紧急修复理由<textarea value={release.emergency_reason} onChange={(e) => setRelease({ ...release, emergency_reason: e.target.value })} placeholder="说明当前故障、业务影响、为什么必须立即恢复，以及失败时如何停止或回退" /></label>}
        <label>变更说明<textarea value={release.change_summary} onChange={(e) => setRelease({ ...release, change_summary: e.target.value })} placeholder="说明业务目标、风险和回滚条件" /></label>
        <button className={release.change_channel === "emergency_recovery" ? "primary emergency-submit" : "primary"} disabled={busy === "release"}><ShieldCheck size={15} />{release.change_channel === "emergency_recovery" ? "提交紧急修复判定" : "提交风险判定"}</button>
        <div className="form-hint">流程：风险判定通过后进入“发布审计链”显示批准按钮；人工批准后显示执行发布按钮，并进入可中断的运维执行流。</div>
      </form>
    </Panel>
    <Panel className="span-all">
      <PanelTitle icon={Workflow} title="发布审计链" subtitle="申请、预算快照、算法判定、审批和执行任务可追溯" action={<button className="ghost tiny" onClick={refreshReleases}><RefreshCcw size={13} />刷新</button>} />
      <div className="release-list">
        {releases.map((item: any) => <article className="release-row" key={item.id}>
          <div className="release-main"><div className="release-badges"><span className={cx("release-status", item.status)}>{item.status}</span>{item.change_channel === "emergency_recovery" && <span className="release-status emergency">紧急修复 · {item.emergency_action}</span>}</div><strong>{item.service} · {item.workload_kind}/{item.workload_name}</strong><small>{item.cluster}/{item.namespace} · {item.release_mode === "new" ? "新建" : "变更"} · {item.manifest_validation ? `YAML 已校验 ${item.manifest_validation.digest}` : item.image || (item.emergency_action === "restart_component" ? "受控重启" : "配置变更")}</small></div>
          <div className="release-decision"><b>{item.gate?.verdict || "-"}</b><span>风险 {item.gate?.risk?.diff_risk ?? item.gate?.risk?.amplification_factor ?? "-"} · 预算剩余 {Math.round(Number(item.error_budget?.remaining_ratio || 0) * 100)}%</span><small>{item.gate?.reason}</small></div>
          <div className="release-actions">{item.status === "awaiting_approval" && <button className="ghost" disabled={busy === item.id} onClick={() => approve(item)}><CheckCircle2 size={14} />批准</button>}{item.status === "approved" && <button className="primary" disabled={busy === item.id} onClick={() => execute(item.id)}><Play size={14} />{item.change_channel === "emergency_recovery" ? "执行修复" : "执行发布"}</button>}{item.status === "blocked" && <span className="release-block-note">门禁阻断</span>}</div>
          <ReleaseReportPanel report={item.report} />
          {jobs[item.id] && <div className="release-job"><ReleaseJobTracker initial={jobs[item.id]} /></div>}
        </article>)}
        {!releases.length && <EmptyState text="还没有发布申请。提交第一条变更后，门禁证据会显示在这里。" />}
      </div>
    </Panel>
    {notice && <div className="notice-box span-all">{notice}</div>}
    {error && <div className="error-box span-all">{error}</div>}
  </section>;
}

function EffectivenessPage() {
  const [state, refresh] = useAsync<any>(() => apiGet("/api/effectiveness"), []);
  const [selectedRecord, setSelectedRecord] = useState<any>(null);
  const summary = state.data?.summary || {};
  const storage = state.data?.storage || {};
  const models = asList(state.data?.by_model);
  const records = asList(state.data?.recent_remediations);
  return (
    <section className="workspace-grid">
      <div className="metric-strip effectiveness-metrics span-all">
        <Metric title="巡检次数" value={summary.inspection_runs || 0} />
        <Metric title="变更成功率" value={`${Math.round((summary.change_success_rate || 0) * 100)}%`} />
        <Metric title="恢复 Pod" value={summary.pods_recovered || 0} tone="good" />
        <Metric title="风险降低率" value={`${Math.round((summary.risk_reduction_rate || 0) * 100)}%`} />
        <Metric title="记录存储" value={storage.durable ? "PVC" : storage.path ? "临时" : "加载中"} tone={storage.durable ? "good" : undefined} />
      </div>
      <Panel>
        <PanelTitle icon={Gauge} title="模型效果对比" action={<button className="ghost" onClick={refresh}><RefreshCcw size={15} />刷新</button>} />
        {state.loading && <div className="quiet-empty">正在读取模型效果和运维审计...</div>}
        {state.error && <div className="error-box">{state.error}</div>}
        <div className="model-list">
          {models.length ? models.map((m: any) => <div className="model-row" key={m.model_id}><strong>{m.model_id}</strong><span>变更 {m.successful_changes || 0}/{m.changes_total || 0}</span><span>恢复 {m.pods_recovered || 0} Pod</span><button className="ghost tiny" onClick={() => setSelectedRecord((m.records || [])[0] || m)}>查看记录</button></div>) : <EmptyState text="暂无模型效果记录；执行一次 AI 巡检或 SRE 运维后会自动生成。" />}
        </div>
      </Panel>
      <Panel>
        <PanelTitle icon={TerminalSquare} title="最近 AI 改动记录" />
        <div className="record-list">
          {records.length ? records.slice().reverse().map((r: any) => <div className="record" key={r.id}><strong>{r.target || "诊断任务"}</strong><p>{r.cluster}/{r.namespace} · {r.status} · 故障链第 {r.lineage_attempt || 1} 轮 · 变更 {r.changes_succeeded}/{r.changes_total} · 恢复 {r.pods_recovered || 0} Pod</p><button className="ghost tiny" onClick={() => setSelectedRecord(r)}>查看详情</button></div>) : <EmptyState text="暂无变更审计记录；只读诊断、门禁阻断和执行失败也会从现在开始进入这里。" />}
        </div>
      </Panel>
      {selectedRecord && <Panel className="span-all">
        <PanelTitle icon={FileUp} title="运维记录详情" subtitle={selectedRecord.id || selectedRecord.model_id || "record"} action={<button className="ghost tiny" onClick={() => setSelectedRecord(null)}>关闭</button>} />
        <div className="effectiveness-detail">
          <div><span>目标</span><strong>{selectedRecord.target || selectedRecord.model_id || "-"}</strong></div>
          <div><span>范围</span><strong>{selectedRecord.cluster || "-"} / {selectedRecord.namespace || "-"}</strong></div>
          <div><span>状态</span><strong>{selectedRecord.status || "recorded"}</strong></div>
          <div><span>变更</span><strong>{selectedRecord.changes_succeeded ?? selectedRecord.successful_changes ?? 0}/{selectedRecord.changes_total ?? 0}</strong></div>
          <div><span>故障链</span><strong>{selectedRecord.lineage_id || "单轮任务"}</strong></div>
          <div><span>策略轮次</span><strong>第 {selectedRecord.lineage_attempt || 1} 轮</strong></div>
        </div>
        {asList(selectedRecord.attempted_strategies).length > 0 && <details className="ops-lineage-history" open>
          <summary>差异化策略历史</summary>
          <div>{asList(selectedRecord.attempted_strategies).map((attempt: any, index: number) => <span key={`${attempt.fingerprint || attempt.strategy}-${index}`}><i>{attempt.attempt || index + 1}</i><b>{attempt.strategy || `策略 ${index + 1}`}</b><em>{attempt.recovered === true ? "已恢复" : attempt.status || "未恢复"}</em><small>{attempt.outcome || "本轮未取得恢复证据。"}</small>{asList(attempt.actions).length > 0 && <code>{asList(attempt.actions).join(" · ")}</code>}</span>)}</div>
        </details>}
        {asList(selectedRecord.changes).length > 0 && <div className="record-evidence-list">{asList(selectedRecord.changes).map((change: any, index: number) => <article key={`${change.type}-${index}`}><b>{change.type} · {change.target}</b><span>{change.status} · {change.risk}</span><pre>{JSON.stringify(change.patch || change.payload || {}, null, 2)}</pre></article>)}</div>}
        {selectedRecord.verification && <div className="analysis-card"><strong>恢复验证</strong><p>{selectedRecord.verification.message || selectedRecord.verification.proof || JSON.stringify(selectedRecord.verification)}</p></div>}
      </Panel>}
    </section>
  );
}

function ObservabilityPage() {
  const [state, refresh] = useAsync<any>(() => apiGet("/api/llm-observability?limit=80"), []);
  const summary = state.data?.summary || {};
  const analytics = state.data?.analytics || {};
  return (
    <section className="workspace-grid">
      <div className="metric-strip span-all">
        <Metric title="调用次数" value={summary.total || 0} />
        <Metric title="Token" value={prettyNumber(summary.total_tokens)} />
        <Metric title="平均延迟" value={`${summary.avg_latency_ms || 0}ms`} />
        <Metric title="P95" value={`${summary.p95_latency_ms || 0}ms`} />
      </div>
      <Panel>
        <PanelTitle icon={Activity} title="数据来源" action={<button className="ghost" onClick={refresh}><RefreshCcw size={15} />刷新</button>} />
        <Bars rows={asList(analytics.by_source)} />
      </Panel>
      <Panel>
        <PanelTitle icon={Boxes} title="模型调用" />
        <Bars rows={asList(analytics.by_model)} />
      </Panel>
    </section>
  );
}

function ModelLabPage({
  activeModelId,
  onActivate,
  refreshRegistry,
  registry
}: {
  activeModelId: string;
  onActivate: (profileId: string) => void;
  refreshRegistry: () => void;
  registry: ApiState<any>;
}) {
  const profiles = asList(registry.data?.profiles) as ModelProfile[];
  const [benchmark, refreshBenchmark] = useAsync<any>(() => apiGet("/api/model-benchmark"), []);
  const [form, setForm] = useState({
    id: "",
    provider: "oauth-gateway",
    model: "",
    base_url: "",
    auth_type: "oauth_client_credentials",
    token_url: "",
    client_id: "",
    client_secret: "",
    api_key: "",
    max_tokens: 4096,
    description: "",
    verify_ssl: false,
    set_active: true
  });
  const [selected, setSelected] = useState<string[]>([]);
  const [testState, setTestState] = useState<ApiState<any>>({ loading: false });
  const [runState, setRunState] = useState<ApiState<any>>({ loading: false });
  const latestRun = runState.data?.run || benchmark.data?.runs?.[0];
  const latestResults = asList(latestRun?.results);
  const rubric = latestRun?.rubric || benchmark.data?.rubric || {};

  useEffect(() => {
    if (!selected.length && profiles.length) setSelected(profiles.slice(0, 3).map((p) => p.id));
  }, [profiles, selected.length]);

  function updateForm(key: string, value: any) {
    setForm((old) => ({ ...old, [key]: value }));
  }

  async function saveProfile() {
    setTestState({ loading: true });
    try {
      await apiPost("/api/model-registry", form);
      setTestState({ loading: false, data: { status: "ok", answer: "模型配置已保存，secret 不会在前端回显。" } });
      if (form.set_active) onActivate(form.id || `${form.provider}:${form.model}`);
      refreshRegistry();
    } catch (error: any) {
      setTestState({ loading: false, error: error.message });
    }
  }

  async function testProfile(profileId = activeModelId) {
    if (!profileId) return;
    setTestState({ loading: true });
    try {
      const data = await apiPost(`/api/model-registry/${encodeURIComponent(profileId)}/test`, {});
      setTestState({ loading: false, data });
    } catch (error: any) {
      setTestState({ loading: false, error: error.message });
    }
  }

  async function runBenchmark() {
    setRunState({ loading: true });
    try {
      const data = await apiPost("/api/model-benchmark/run", {
        model_profile_ids: selected.length ? selected : [activeModelId],
        cluster: "all",
        namespace: "all",
        include_latest_findings: true
      });
      setRunState({ loading: false, data });
      refreshBenchmark();
    } catch (error: any) {
      setRunState({ loading: false, error: error.message });
    }
  }

  return (
    <section className="model-lab">
      <Panel className="span-all">
        <PanelTitle
          icon={Settings2}
          title="可插拔 AI 模型"
          subtitle="支持 Token URL + Base URL 的企业网关，也支持 Base URL + API Key 的 OpenAI-compatible 接入"
          action={<button className="ghost" onClick={refreshRegistry}><RefreshCcw size={15} />刷新</button>}
        />
        <div className="profile-grid">
          {profiles.map((profile) => (
            <div className={cx("profile-card", activeModelId === profile.id && "active")} key={profile.id}>
              <div className="profile-head">
                <div>
                  <strong>{profile.id}</strong>
                  <span>{profile.provider} · {profile.model}</span>
                </div>
                {activeModelId === profile.id ? <CheckCircle2 size={18} /> : <button className="ghost tiny" onClick={() => onActivate(profile.id)}>启用</button>}
              </div>
              <p>{profile.description || profile.base_url}</p>
              <div className="chips"><span>{profile.auth_type}</span><span>{profile.role || "candidate"}</span></div>
            </div>
          ))}
          {!profiles.length && <EmptyState text="还没有模型配置。可以从右侧新增一个企业网关或 API Key 模型。" />}
        </div>
      </Panel>

      <Panel>
        <PanelTitle icon={KeyRound} title="新增 / 更新模型" />
        <div className="form-stack">
          <label>Profile ID<input value={form.id} onChange={(e) => updateForm("id", e.target.value)} placeholder="deepseek-prod" /></label>
          <div className="form-grid two">
            <label>Provider<input value={form.provider} onChange={(e) => updateForm("provider", e.target.value)} /></label>
            <label>Model<input value={form.model} onChange={(e) => updateForm("model", e.target.value)} placeholder="your-model" /></label>
          </div>
          <label>Base URL<input value={form.base_url} onChange={(e) => updateForm("base_url", e.target.value)} placeholder="https://gateway/engines/xxx 或 .../chat/completions" /></label>
          <label>鉴权方式<select value={form.auth_type} onChange={(e) => updateForm("auth_type", e.target.value)}>
            <option value="oauth_client_credentials">Token URL + Client Credentials</option>
            <option value="api_key">Base URL + API Key</option>
            <option value="none">无鉴权 / 本地模型</option>
          </select></label>
          {form.auth_type === "oauth_client_credentials" ? (
            <>
              <label>Token URL<input value={form.token_url} onChange={(e) => updateForm("token_url", e.target.value)} /></label>
              <div className="form-grid two">
                <label>Client ID<input value={form.client_id} onChange={(e) => updateForm("client_id", e.target.value)} /></label>
                <label>Client Secret<input type="password" value={form.client_secret} onChange={(e) => updateForm("client_secret", e.target.value)} placeholder="保存后不会回显" /></label>
              </div>
            </>
          ) : form.auth_type === "api_key" ? (
            <label>API Key<input type="password" value={form.api_key} onChange={(e) => updateForm("api_key", e.target.value)} placeholder="保存后不会回显" /></label>
          ) : null}
          <div className="form-grid two">
            <label>Max tokens<input type="number" value={form.max_tokens} onChange={(e) => updateForm("max_tokens", Number(e.target.value))} /></label>
            <label className="toggle"><input type="checkbox" checked={form.verify_ssl} onChange={(e) => updateForm("verify_ssl", e.target.checked)} />校验证书</label>
          </div>
          <label>说明<input value={form.description} onChange={(e) => updateForm("description", e.target.value)} placeholder="例如：生产诊断主模型 / 候选测评模型" /></label>
          <label className="toggle"><input type="checkbox" checked={form.set_active} onChange={(e) => updateForm("set_active", e.target.checked)} />保存后设为当前模型</label>
          <button className="primary" onClick={saveProfile}><Save size={16} />保存模型</button>
          {testState.error && <div className="error-box">{testState.error}</div>}
          {testState.data && <div className="success-box">{testState.data.answer || testState.data.status}{testState.data.latency_ms ? ` · ${testState.data.latency_ms}ms` : ""}</div>}
        </div>
      </Panel>

      <Panel>
        <PanelTitle
          icon={Beaker}
          title="运维能力测评"
          subtitle="同一批巡检证据给多个模型，比较根因覆盖、动作可执行性、安全门禁、延迟和 token 成本"
          action={<button className="primary" onClick={runBenchmark} disabled={runState.loading}>{runState.loading ? <Loader2 className="spin" size={16} /> : <Play size={16} />}开始测评</button>}
        />
        <div className="benchmark-standard">
          <header><ShieldCheck size={16} /><div><strong>{rubric.name || "FrontierSRE-Production-Rubric"}</strong><span>公开、可审计的生产 SRE 代理标准，不冒充任何组织未公开的内部标准</span></div></header>
          <div className="benchmark-weight-grid">
            {Object.entries(rubric.weights || { "证据落地": 22, "根因推理": 20, "修复深度": 22, "安全变更": 16, "恢复验证": 12, "响应效率": 8 }).map(([name, weight]) => <div key={name}><b>{Number(weight)}%</b><span>{name}</span></div>)}
          </div>
          <details><summary>评分如何计算</summary><p>{rubric.formula || "总分 = Σ(维度得分 × 维度权重)。"} 离线测评检查回答是否引用真实证据、是否给出可执行且可回滚的动作、是否具备安全门禁和恢复验证；线上“运维成效”另行记录真实变更成功率、恢复 Pod 与风险降低率，避免把会说误当成会修。</p><p>依据：{asList(rubric.basis).join("；") || "SLO/错误预算、Kubernetes 安全变更、DORA 成效与 OpenTelemetry 证据链。"}</p></details>
        </div>
        <div className="check-list">
          {profiles.map((profile) => (
            <label className="check-row" key={profile.id}>
              <input
                type="checkbox"
                checked={selected.includes(profile.id)}
                onChange={(e) => setSelected((old) => e.target.checked ? [...old, profile.id] : old.filter((x) => x !== profile.id))}
              />
              <span><strong>{profile.id}</strong><small>{profile.model}</small></span>
              <button type="button" className="ghost tiny" onClick={() => testProfile(profile.id)}>测试</button>
            </label>
          ))}
        </div>
        {runState.error && <div className="error-box">{runState.error}</div>}
        {latestRun?.rubric && <div className="benchmark-method"><ShieldCheck size={15} /><div><strong>{latestRun.rubric.name} · {latestRun.rubric.version}</strong><span>{asList(latestRun.rubric.dimensions).join(" / ")}</span></div></div>}
        <div className="leaderboard">
          {asList(benchmark.data?.leaderboard).map((row: any, index: number) => (
            <div className="rank-row" key={row.profile_id}>
              <strong>#{index + 1} {row.profile_id}</strong>
              <span>平均分 {row.avg_score}</span>
              <span>{row.avg_latency_ms}ms</span>
            </div>
          ))}
          {!asList(benchmark.data?.leaderboard).length && <EmptyState text="暂无测评记录。先执行一次测评，就能看到模型排行榜。" />}
        </div>
      </Panel>

      <Panel className="span-all">
        <PanelTitle icon={TerminalSquare} title="最近测评报告" subtitle="每一分都有维度权重、命中证据、缺失项和改进建议" />
        <div className="benchmark-runs">
          {latestResults.map((result: any) => (
            <div className="benchmark-card" key={result.profile_id}>
              <div className="profile-head">
                <div><strong>{result.profile_id}</strong><span>{result.model || result.status}</span></div>
                <div className="benchmark-grade"><b>{result.score?.grade || "-"}</b><strong>{result.score?.total || 0}</strong><small>/ 100</small></div>
              </div>
              {result.error ? <p className="danger-text">{result.error}</p> : <div className="rubric-list">{asList(result.score?.criteria).map((criterion: any) => <div key={criterion.id}><header><strong>{criterion.label}</strong><span>{criterion.score} × {criterion.weight}% = {criterion.weighted_score}</span></header><i><b style={{ width: `${Math.min(100, Number(criterion.score || 0))}%` }} /></i><small>{asList(criterion.evidence).slice(0, 3).join(" · ") || "未命中有效证据"}</small></div>)}</div>}
              {!result.error && <div className="benchmark-basis"><div><b>优势</b><span>{asList(result.score?.strengths).join("、") || "尚未形成明显优势"}</span></div><div><b>主要缺口</b><span>{asList(result.score?.gaps).join("；") || "未发现关键缺口"}</span></div><div><b>提升建议</b><span>{asList(result.score?.recommendations).join("；") || "保持当前策略"}</span></div></div>}
              {!result.error && <details className="benchmark-answer"><summary>查看模型原始回答</summary><p>{result.answer_preview}</p></details>}
            </div>
          ))}
          {!latestResults.length && <EmptyState text="运行一次测评后，这里会生成完整的六维能力报告。" />}
        </div>
      </Panel>
    </section>
  );
}

function KnowledgePage({ activeModelId }: { activeModelId: string }) {
  const [domain, setDomain] = useState("app");
  const [question, setQuestion] = useState("AI 巡检发现生产风险后我应该怎么执行修复？");
  const [includePrinciple, setIncludePrinciple] = useState(false);
  const [state, setState] = useState<ApiState<any>>({ loading: false });
  const [ingestState, setIngestState] = useState<ApiState<any>>({ loading: false });
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [ingestForm, setIngestForm] = useState({
    domain: "auto",
    title: "",
    tags: "",
    content: "",
    embed: true,
  });
  const [sources, refreshSources] = useAsync<any>(() => apiGet("/api/knowledge/sources"), []);
  const [documents, refreshDocuments] = useAsync<any>(() => apiGet(`/api/knowledge/documents?domain=${encodeURIComponent(domain)}`), [domain]);
  const embedding = sources.data?.embedding || {};

  function updateIngest(key: string, value: any) {
    setIngestForm((old) => ({ ...old, [key]: value }));
  }

  async function ask() {
    const text = question.trim();
    if (!text) return;
    setState({ loading: true });
    try {
      const data = await apiPost("/api/knowledge/ask", {
        question: text,
        domain,
        include_principle: includePrinciple,
        model_profile_id: activeModelId,
      });
      setState({ loading: false, data });
    } catch (error: any) {
      setState({ loading: false, error: error.message });
    }
  }

  async function addKnowledge() {
    if (!ingestForm.content.trim()) {
      setIngestState({ loading: false, error: "请先填写知识内容" });
      return;
    }
    setIngestState({ loading: true });
    try {
      const data = await apiPost<any>("/api/knowledge/documents", {
        ...ingestForm,
        source: "frontend",
        document_type: "text",
      });
      setIngestState({ loading: false, data });
      setIngestForm((old) => ({ ...old, title: "", tags: "", content: "" }));
      refreshSources();
      refreshDocuments();
    } catch (error: any) {
      setIngestState({ loading: false, error: error.message });
    }
  }

  async function uploadKnowledge() {
    if (!uploadFile) {
      setIngestState({ loading: false, error: "请先选择要导入的文档" });
      return;
    }
    setIngestState({ loading: true });
    try {
      const form = new FormData();
      form.set("file", uploadFile);
      form.set("domain", ingestForm.domain);
      form.set("title", ingestForm.title);
      form.set("tags", ingestForm.tags);
      form.set("embed", String(ingestForm.embed));
      const response = await fetch("/api/knowledge/upload", { method: "POST", body: form, headers: adminAuthHeaders({ Accept: "application/json" }) });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || data.error || `${response.status} ${response.statusText}`);
      setIngestState({ loading: false, data });
      setUploadFile(null);
      setIngestForm((old) => ({ ...old, title: "", tags: "", content: "" }));
      refreshSources();
      refreshDocuments();
    } catch (error: any) {
      setIngestState({ loading: false, error: error.message });
    }
  }

  async function reindexKnowledge() {
    setIngestState({ loading: true });
    try {
      const data = await apiPost<any>("/api/knowledge/reindex", { domain, force: true });
      setIngestState({ loading: false, data: { status: data.status, message: `重建索引完成：${data.reindexed || 0} 个文档，失败 ${data.failed || 0} 个` } });
      refreshSources();
      refreshDocuments();
    } catch (error: any) {
      setIngestState({ loading: false, error: error.message });
    }
  }

  async function deleteKnowledge(documentId: string) {
    setIngestState({ loading: true });
    try {
      const res = await fetch(`/api/knowledge/documents/${encodeURIComponent(documentId)}`, { method: "DELETE", headers: adminAuthHeaders({ Accept: "application/json" }) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || `${res.status} ${res.statusText}`);
      setIngestState({ loading: false, data: { status: "ok", message: "知识条目已删除" } });
      refreshSources();
      refreshDocuments();
    } catch (error: any) {
      setIngestState({ loading: false, error: error.message });
    }
  }

  return (
    <section className="knowledge-layout">
      <Panel>
        <PanelTitle icon={BookOpen} title="运维 RAG 知识库" subtitle="给小助手和运维人员使用：问系统怎么用，也问复杂故障怎么排。" />
        <div className="form-stack">
          <label>知识域<select value={domain} onChange={(e) => setDomain(e.target.value)}>
            <option value="app">应用使用知识库</option>
            <option value="ops">运维 Runbook 知识库</option>
          </select></label>
          <textarea className="knowledge-input" value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="问我这个系统怎么用，或者问某类 K8s 问题怎么处理..." />
          <label className="toggle"><input type="checkbox" checked={includePrinciple} onChange={(e) => setIncludePrinciple(e.target.checked)} />同时解释原理</label>
          <button className="primary" onClick={ask} disabled={state.loading}>{state.loading ? <Loader2 className="spin" size={16} /> : <Send size={16} />}提问</button>
          {state.error && <div className="error-box">{state.error}</div>}
        </div>
      </Panel>
      <Panel>
        <PanelTitle icon={BrainCircuit} title="助手回答" />
        {state.data ? (
          <div className="knowledge-answer">
            <div className="chips"><span>{state.data.source}</span><span>{state.data.status}</span></div>
            <div className="markdown" dangerouslySetInnerHTML={{ __html: markdownish(state.data.answer || "") }} />
            <div className="source-list">
              {asList(state.data.citations).map((doc: any) => (
                <div className="source-card" key={doc.id}>
                  <strong>{doc.title}</strong>
                  <div className="source-meta"><span>{doc.retrieval || doc.source || "source"}</span>{doc.score !== undefined && <span>score {doc.score}</span>}</div>
                  <p>{doc.content}</p>
                </div>
              ))}
            </div>
          </div>
        ) : <EmptyState text="可以把它当成会说话的产品手册和运维手册。LLM 不可用时也会给出检索兜底答案。" />}
      </Panel>
      <Panel>
        <PanelTitle icon={Database} title="新增知识" subtitle="上传文件或粘贴正文，系统会抽取内容、自动切片并调用 embedding 入库。" />
        <div className="form-stack">
          <div className="form-grid two">
            <label>知识域<select value={ingestForm.domain} onChange={(e) => updateIngest("domain", e.target.value)}>
              <option value="auto">自动识别</option>
              <option value="app">应用使用知识</option>
              <option value="ops">运维 Runbook</option>
            </select></label>
            <label className="toggle"><input type="checkbox" checked={ingestForm.embed} onChange={(e) => updateIngest("embed", e.target.checked)} />保存后向量化</label>
          </div>
          <label>标题<input value={ingestForm.title} onChange={(e) => updateIngest("title", e.target.value)} placeholder="例如：NFS PVC 权限问题处理 Runbook" /></label>
          <label>标签<input value={ingestForm.tags} onChange={(e) => updateIngest("tags", e.target.value)} placeholder="crashloop, pvc, rancher；留空会自动识别" /></label>
          <label className="knowledge-dropzone">
            <FileUp size={21} />
            <span><strong>{uploadFile ? uploadFile.name : "选择知识库文件"}</strong><small>支持 PDF、Word、PowerPoint、Excel、CSV、Markdown、HTML、JSON、YAML、日志和纯文本</small></span>
            <input type="file" accept=".pdf,.docx,.pptx,.xlsx,.odt,.csv,.md,.markdown,.txt,.log,.json,.yaml,.yml,.html,.htm,.xml,.rtf" onChange={(event) => setUploadFile(event.target.files?.[0] || null)} />
          </label>
          <textarea className="knowledge-input knowledge-ingest-input" value={ingestForm.content} onChange={(e) => updateIngest("content", e.target.value)} placeholder="粘贴知识正文。系统会自动识别应用/运维知识域，按段落切片，并调用 embedding 模型生成向量。" />
          <div className="knowledge-actions">
            <button className="primary" onClick={uploadKnowledge} disabled={ingestState.loading || !uploadFile}><FileUp size={16} />导入文件</button>
            <button className="primary" onClick={addKnowledge} disabled={ingestState.loading}>{ingestState.loading ? <Loader2 className="spin" size={16} /> : <Save size={16} />}保存知识</button>
            <button className="ghost" onClick={reindexKnowledge} disabled={ingestState.loading}><RefreshCcw size={15} />重建索引</button>
          </div>
          {ingestState.error && <div className="error-box">{ingestState.error}</div>}
          {ingestState.data && <div className="success-box">{ingestState.data.message || ingestState.data.status}</div>}
        </div>
      </Panel>
      <Panel>
        <PanelTitle icon={Boxes} title="已添加知识" subtitle="这些知识会被小助手、知识库问答和后续运维 RAG 复用。" action={<button className="ghost" onClick={refreshDocuments}><RefreshCcw size={15} />刷新</button>} />
        <div className="knowledge-doc-list">
          {asList(documents.data?.documents).map((doc: any) => (
            <div className="knowledge-doc-card" key={doc.id}>
              <div>
                <strong>{doc.title}</strong>
                <small>{doc.domain} · {doc.chunk_count || asList(doc.chunks).length} chunks · {doc.embedding_status}</small>
              </div>
              <div className="chips">{asList(doc.tags).slice(0, 5).map((tag: string) => <span key={tag}>{tag}</span>)}</div>
              {doc.embedding_error && <p>{doc.embedding_error}</p>}
              <button className="ghost tiny" onClick={() => deleteKnowledge(doc.id)}>删除</button>
            </div>
          ))}
          {!asList(documents.data?.documents).length && <EmptyState text="还没有前端添加的知识。保存一条 Runbook 后，这里会显示切片和 embedding 状态。" />}
        </div>
      </Panel>
      <Panel className="span-all">
        <PanelTitle icon={Boxes} title="知识源状态" />
        <div className="knowledge-meta">
          <span>Embedding：{embedding.enabled ? "已启用" : "未启用"}</span>
          <span>{embedding.model || "未配置模型"}</span>
          <span>{embedding.runtime_documents || 0} 文档 / {embedding.runtime_chunks || 0} 向量片段</span>
          <span>{embedding.store_path || "runtime store"}</span>
        </div>
        <div className="profile-grid">
          {asList(sources.data?.domains).map((item: any) => (
            <div className="profile-card" key={item.id}><strong>{item.name}</strong><p>{item.documents} 个知识片段，可被小助手、SRE 对话和运维流程复用。</p></div>
          ))}
        </div>
      </Panel>
    </section>
  );
}

function Panel({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <section className={cx("panel", className)}>{children}</section>;
}

function PanelTitle({ icon: Icon, title, subtitle, action }: { icon: any; title: string; subtitle?: string; action?: React.ReactNode }) {
  return <div className="panel-title"><div><span><Icon size={16} />{title}</span>{subtitle && <p>{subtitle}</p>}</div>{action}</div>;
}

function Metric({ title, value, tone }: { title: string; value: React.ReactNode; tone?: "good" | "danger" }) {
  return <div className={cx("metric", tone)}><span>{title}</span><strong>{value}</strong></div>;
}

function EmptyState({ text }: { text: string }) {
  return <div className="empty-state"><CircleDot size={18} />{text}</div>;
}

function Bars({ rows }: { rows: any[] }) {
  const max = Math.max(1, ...rows.map((r) => Number(r.calls || r.count || 0)));
  return <div className="bars">{rows.length ? rows.slice(0, 8).map((row) => <div className="bar" key={row.name}><span>{row.name}</span><i><b style={{ width: `${(Number(row.calls || row.count || 0) / max) * 100}%` }} /></i><strong>{row.calls || row.count || 0}</strong></div>) : <EmptyState text="暂无观测数据。" />}</div>;
}

createRoot(document.getElementById("root")!).render(
  <App />
);

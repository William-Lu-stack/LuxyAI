import { useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BellRing,
  Bot,
  Boxes,
  BrainCircuit,
  CheckCircle2,
  ChevronRight,
  CircleDot,
  CloudCog,
  Database,
  Download,
  FileClock,
  Eye,
  Gauge,
  GitBranch,
  HardDrive,
  Layers3,
  LineChart,
  Loader2,
  MessageSquareText,
  Network,
  RefreshCcw,
  Search,
  Send,
  ServerCog,
  ShieldCheck,
  Sparkles,
  Square,
  TerminalSquare,
  Trash2,
  Upload,
  Workflow,
  X,
} from "lucide-react";
import { useAsync } from "./hooks/useAsync";
import { ApiState, adminAuthHeaders, apiDelete, apiGet, apiPost, asList as list, compactNumber, invalidateApiCache } from "./lib/api";
import { OpsPlanPanel } from "./components/OpsPlanPanel";

function timeText(value: string | undefined) {
  if (!value) return "-";
  try { return new Date(value).toLocaleString("zh-CN", { hour12: false }); } catch { return value; }
}

function SectionHead({ icon: Icon, title, meta, action }: { icon: any; title: string; meta?: string; action?: React.ReactNode }) {
  return <div className="section-head"><div><span><Icon size={16} />{title}</span>{meta && <small>{meta}</small>}</div>{action}</div>;
}

function StatusPill({ status, text }: { status: string; text?: string }) {
  const tone = /ok|up|connected|ready|enabled|healthy/i.test(status) ? "ok" : /disabled|not_configured|unknown/i.test(status) ? "muted" : "warn";
  return <span className={`status-pill ${tone}`}><i />{text || status}</span>;
}

function Empty({ text }: { text: string }) {
  return <div className="unified-empty"><CircleDot size={18} /><span>{text}</span></div>;
}

function Kpi({ label, value, detail, tone = "" }: { label: string; value: React.ReactNode; detail?: string; tone?: string }) {
  return <div className={`unified-kpi ${tone}`}><span>{label}</span><strong>{value}</strong>{detail && <small>{detail}</small>}</div>;
}

export function DashboardPage() {
  const [cluster, setCluster] = useState("all");
  const [selectedProblem, setSelectedProblem] = useState<any>(null);
  const [advice, setAdvice] = useState<ApiState<any>>({ loading: false });
  const [rancher, refreshRancher] = useAsync<any>(() => apiGet("/api/rancher/status"), []);
  const [inventory, refreshInventory] = useAsync<any>(() => apiGet("/api/rancher/inventory").catch(() => apiGet("/api/dashboard")), []);
  const [metrics, refreshMetrics] = useAsync<any>(() => apiGet(`/api/prometheus/summary?cluster=${encodeURIComponent(cluster)}`), [cluster]);
  const [health, refreshHealth] = useAsync<any>(() => apiGet("/api/health"), []);

  const clusters = list(rancher.data?.clusters);
  const selectedInventory = useMemo(() => {
    const items = list(inventory.data?.inventory);
    if (cluster === "all") return items;
    return items.filter((item: any) => cluster === item.cluster?.id || cluster === item.cluster?.name);
  }, [cluster, inventory.data]);
  const pods = selectedInventory.flatMap((item: any) => list(item.pods));
  const workloads = selectedInventory.flatMap((item: any) => list(item.workloads));
  const nodes = selectedInventory.flatMap((item: any) => list(item.nodes));
  const values = metrics.data?.values || {};
  const fallback = inventory.data?.pods && !inventory.data?.inventory;
  const summary = fallback ? inventory.data : {
    pods: { total: pods.length, running: pods.filter((pod: any) => pod.phase === "Running").length, failed: pods.filter((pod: any) => pod.issue || !pod.ready).length },
    nodes: { total: nodes.length, ready: nodes.filter((node: any) => node.ready).length },
  };
  const problems = pods.filter((pod: any) => pod.issue || (!pod.ready && pod.phase !== "Succeeded"));

  async function askForAdvice(pod: any) {
    setSelectedProblem(pod);
    setAdvice({ loading: true });
    try {
      const data = await apiPost<any>("/api/chat", {
        message: `请基于真实集群证据分析 Pod ${pod.name} 的异常原因并给出简洁处置建议。当前信号：${pod.issue?.reason || pod.phase || "NotReady"}`,
        cluster: pod.cluster || cluster,
        cluster_id: pod.cluster_id || pod.cluster || cluster,
        namespace: pod.namespace || "default",
        deployment: pod.workload_name || pod.workload?.name || "",
        workload_type: pod.workload_kind || pod.workload?.kind || "Workload",
        severity: pod.issue?.severity || "P2",
        auto_healing_enabled: false,
      });
      setAdvice({ loading: false, data });
    } catch (error: any) { setAdvice({ loading: false, error: error.message }); }
  }

  const refresh = () => { refreshRancher(); refreshInventory(); refreshMetrics(); refreshHealth(); };
  return (
    <div className="unified-page">
      <div className="page-commandbar">
        <div className="scope-control"><span>监控范围</span><select value={cluster} onChange={(event) => setCluster(event.target.value)}><option value="all">所有集群</option>{clusters.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></div>
        <button className="ghost" onClick={refresh}><RefreshCcw size={15} />刷新</button>
      </div>
      <section className="kpi-grid six">
        <Kpi label="集群" value={cluster === "all" ? (rancher.data?.cluster_count || selectedInventory.length || 1) : 1} detail={rancher.data?.status || "local"} />
        <Kpi label="Pods" value={summary.pods?.total || 0} detail={`${summary.pods?.running || 0} Running`} />
        <Kpi label="异常" value={summary.pods?.failed || problems.length || 0} detail="未就绪或运行异常" tone={(summary.pods?.failed || problems.length) ? "danger" : "good"} />
        <Kpi label="CPU" value={`${Number(values.cpu_cores || 0).toFixed(2)} C`} detail={metrics.data?.source || "metrics"} />
        <Kpi label="内存" value={`${(Number(values.memory_bytes || 0) / 1024 / 1024 / 1024).toFixed(2)} GiB`} detail="Working set" />
        <Kpi label="节点" value={summary.nodes?.total || nodes.length || 0} detail={`${summary.nodes?.ready || nodes.filter((node: any) => node.ready).length || 0} Ready`} />
      </section>
      <section className="unified-grid dashboard-grid">
        <div className="surface span-two">
          <SectionHead icon={AlertTriangle} title="需要关注" meta={`${problems.length} 项实时异常`} />
          {problems.length ? <div className="compact-list attention-scroll">{problems.map((pod: any) => <div className="compact-row attention-row" key={`${pod.cluster}-${pod.namespace}-${pod.name}`}><span className="resource-icon risk"><Boxes size={15} /></span><div><strong>{pod.name}</strong><small>{pod.cluster} / {pod.namespace} · {pod.issue?.reason || pod.phase || "NotReady"}</small></div><div className="attention-actions"><StatusPill status={pod.issue?.severity || "warning"} /><button className="row-icon-button" onClick={() => { setSelectedProblem(pod); setAdvice({ loading: false }); }} title="查看异常详情"><Eye size={14} /></button></div></div>)}</div> : <Empty text="当前范围没有发现异常 Pod" />}
        </div>
        <div className="surface">
          <SectionHead icon={Activity} title="平台服务" />
          <div className="service-matrix">{Object.entries(health.data?.services || {}).map(([name, value]: [string, any]) => <div key={name}><span>{name}</span><StatusPill status={value?.status || "unknown"} /></div>)}</div>
          {health.error && <div className="inline-error">{health.error}</div>}
        </div>
        {selectedProblem && <div className="surface span-three attention-detail">
          <SectionHead icon={Eye} title={selectedProblem.name} meta={`${selectedProblem.cluster || cluster} / ${selectedProblem.namespace || "default"}`} action={<button className="primary" onClick={() => askForAdvice(selectedProblem)} disabled={advice.loading}>{advice.loading ? <Loader2 className="spin" size={15} /> : <Sparkles size={15} />}AI 建议</button>} />
          <div className="attention-detail-grid"><div><span>异常原因</span><strong>{selectedProblem.issue?.reason || selectedProblem.phase || "NotReady"}</strong></div><div><span>容器状态</span><strong>{selectedProblem.ready ? "Ready" : "NotReady"} · restart {selectedProblem.restart_count || 0}</strong></div><div><span>上游工作负载</span><strong>{selectedProblem.workload_kind || selectedProblem.workload?.kind || "-"}/{selectedProblem.workload_name || selectedProblem.workload?.name || "-"}</strong></div></div>
          {advice.error && <div className="inline-error">{advice.error}</div>}
          {advice.data?.answer && <div className="attention-advice"><BrainCircuit size={17} /><p>{advice.data.answer}</p></div>}
        </div>}
        <div className="surface span-three">
          <SectionHead icon={Layers3} title="工作负载健康" meta={`${workloads.length} workloads`} />
          {workloads.length ? <div className="workload-strip">{workloads.slice(0, 12).map((item: any) => {
            const healthy = Number(item.ready_replicas || 0) >= Number(item.replicas || 0);
            return <div key={`${item.cluster}-${item.namespace}-${item.kind}-${item.name}`}><span>{item.kind}</span><strong>{item.name}</strong><small>{item.cluster}/{item.namespace}</small><StatusPill status={healthy ? "healthy" : "degraded"} text={`${item.ready_replicas || 0}/${item.replicas || 0}`} /></div>;
          })}</div> : <Empty text="Rancher 尚未返回工作负载清单" />}
        </div>
      </section>
    </div>
  );
}

export function ResourcesPage() {
  const [state, refresh] = useAsync<any>(() => apiGet("/api/rancher/inventory"), []);
  const [cluster, setCluster] = useState("all");
  const [namespace, setNamespace] = useState("all");
  const [kind, setKind] = useState<"pods" | "workloads" | "nodes">("pods");
  const [query, setQuery] = useState("");
  const inventory = list(state.data?.inventory);
  const clusters = list(state.data?.clusters);
  const scoped = cluster === "all" ? inventory : inventory.filter((item: any) => cluster === item.cluster?.id || cluster === item.cluster?.name);
  const namespaces = Array.from(new Set(scoped.flatMap((item: any) => list(item.namespaces).map((item: any) => item.name)))).sort();
  const rows = scoped.flatMap((item: any) => list(item[kind])).filter((item: any) => namespace === "all" || item.namespace === namespace).filter((item: any) => !query || JSON.stringify(item).toLowerCase().includes(query.toLowerCase()));

  return <div className="unified-page">
    <div className="page-commandbar resource-toolbar">
      <div className="segmented"><button className={kind === "pods" ? "active" : ""} onClick={() => setKind("pods")}>Pods</button><button className={kind === "workloads" ? "active" : ""} onClick={() => setKind("workloads")}>Workloads</button><button className={kind === "nodes" ? "active" : ""} onClick={() => setKind("nodes")}>Nodes</button></div>
      <select value={cluster} onChange={(event) => { setCluster(event.target.value); setNamespace("all"); }}><option value="all">所有集群</option>{clusters.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select>
      {kind !== "nodes" && <select value={namespace} onChange={(event) => setNamespace(event.target.value)}><option value="all">所有 Namespace</option>{namespaces.map((item) => <option key={item} value={item}>{item}</option>)}</select>}
      <label className="search-field"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选资源" /></label>
      <button className="ghost" onClick={refresh}><RefreshCcw size={15} /></button>
    </div>
    <div className="surface resource-surface">
      <SectionHead icon={kind === "nodes" ? ServerCog : Boxes} title={kind === "pods" ? "Pod 清单" : kind === "workloads" ? "Workload 清单" : "Node 清单"} meta={`${rows.length} resources`} />
      {state.error && <div className="inline-error">{state.error}</div>}
      {rows.length ? <div className="resource-table"><div className="resource-table-head"><span>名称</span><span>位置</span><span>类型 / 状态</span><span>健康</span></div>{rows.slice(0, 200).map((item: any, index: number) => {
        const healthy = kind === "nodes" ? item.ready : kind === "workloads" ? Number(item.ready_replicas || 0) >= Number(item.replicas || 0) : item.ready || item.phase === "Succeeded";
        return <div className="resource-table-row" key={`${item.cluster}-${item.namespace}-${item.name}-${index}`}><div><strong>{item.name}</strong><small>{item.workload_kind && item.workload_name ? `${item.workload_kind}/${item.workload_name}` : item.kind || ""}</small></div><span>{item.cluster || "-"}<small>{item.namespace || "global"}</small></span><span>{item.kind || item.phase || "Node"}<small>{kind === "workloads" ? `${item.ready_replicas || 0}/${item.replicas || 0} ready` : item.issue?.reason || ""}</small></span><StatusPill status={healthy ? "healthy" : "degraded"} /></div>;
      })}</div> : !state.loading && <Empty text="当前筛选范围没有资源" />}
    </div>
  </div>;
}

export function InfrastructurePage({ activeModelId = "" }: { activeModelId?: string }) {
  const [resourceType, setResourceType] = useState("all");
  const [resourceId, setResourceId] = useState("");
  const [selectedFindingId, setSelectedFindingId] = useState("");
  const [providers, refreshProviders] = useAsync<any>(() => apiGet("/api/infrastructure/providers").catch(() => ({ catalog: [], resources: [], summary: {} })), []);
  const [resources, refreshResources] = useAsync<any>(() => apiGet(`/api/infrastructure/resources?resource_type=${encodeURIComponent(resourceType)}`).catch(() => ({ resources: [] })), [resourceType]);
  const [scan, setScan] = useState<ApiState<any>>({ loading: false });
  const catalog = list(providers.data?.catalog);
  const resourceRows = list(resources.data?.resources || providers.data?.resources);
  const scopedResources = resourceType === "all" ? resourceRows : resourceRows.filter((item: any) => item.type === resourceType);
  const selectedResource = resourceRows.find((item: any) => item.id === resourceId);
  const findings = list(scan.data?.findings);
  const selectedFinding = findings.find((item: any) => item.id === selectedFindingId) || findings[0];
  const selectedPlan = selectedFinding?.ops_plan;
  const summary = scan.data?.summary || {};

  async function runScan() {
    setScan({ loading: true });
    setSelectedFindingId("");
    try {
      const data = await apiPost<any>("/api/infrastructure/scan", {
        resource_type: resourceType,
        resource_id: resourceId,
        model_profile_id: activeModelId,
        production_mode: true,
        include_probe: true,
      });
      setScan({ loading: false, data });
      const first = list(data.findings)[0];
      if (first?.id) setSelectedFindingId(first.id);
    } catch (error: any) {
      setScan({ loading: false, error: error.message });
    }
  }

  function refreshAll() {
    refreshProviders();
    refreshResources();
  }

  return <div className="unified-page infrastructure-page">
    <div className="page-commandbar infrastructure-toolbar">
      <div className="scope-control"><span>资源类型</span><select value={resourceType} onChange={(event) => { setResourceType(event.target.value); setResourceId(""); }}><option value="all">全部基础设施</option>{catalog.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></div>
      <div className="scope-control"><span>目标资源</span><select value={resourceId} onChange={(event) => setResourceId(event.target.value)}><option value="">当前类型全部资源</option>{scopedResources.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></div>
      <button className="primary" onClick={runScan} disabled={scan.loading}>{scan.loading ? <Loader2 className="spin" size={15} /> : <Search size={15} />}AI SRE 巡检</button>
      <button className="ghost" onClick={refreshAll}><RefreshCcw size={15} />刷新</button>
    </div>
    <section className="kpi-grid six">
      <Kpi label="纳管资源" value={providers.data?.summary?.total || resourceRows.length || 0} detail={providers.data?.summary?.configured ? "已配置 Provider" : "等待接入"} />
      <Kpi label="数据库" value={providers.data?.summary?.by_type?.database || 0} detail="MySQL / Oracle / Redis" />
      <Kpi label="虚拟机" value={providers.data?.summary?.by_type?.virtual_machine || 0} detail="VMware / ECS / Linux" />
      <Kpi label="中间件" value={providers.data?.summary?.by_type?.middleware || 0} detail="Kafka / MQ / ELK" />
      <Kpi label="本次异常" value={summary.total || 0} detail={`${summary.p1 || 0} P1 · ${summary.p2 || 0} P2`} tone={summary.total ? "danger" : "good"} />
      <Kpi label="受控执行器" value={providers.data?.summary?.action_webhook_configured ? "Ready" : "待配置"} detail="外部变更 Webhook" tone={providers.data?.summary?.action_webhook_configured ? "good" : ""} />
    </section>
    <section className="unified-grid infrastructure-grid">
      <div className="surface">
        <SectionHead icon={CloudCog} title="Provider 目录" meta="K8s 之外的全栈资源入口" />
        <div className="infra-provider-list">
          {catalog.map((item: any) => <article key={item.id}>
            <span className="resource-icon">{item.id === "database" ? <Database size={15} /> : item.id === "virtual_machine" ? <ServerCog size={15} /> : item.id === "storage" ? <HardDrive size={15} /> : <CloudCog size={15} />}</span>
            <div><strong>{item.name}</strong><p>{item.description}</p><small>{list(item.typical_actions).slice(0, 4).join(" · ")}</small></div>
          </article>)}
        </div>
      </div>
      <div className="surface span-two">
        <SectionHead icon={Layers3} title="资源清单" meta={`${scopedResources.length} resources`} />
        {resources.error && <div className="inline-error">{resources.error}</div>}
        {scopedResources.length ? <div className="infra-resource-grid">{scopedResources.slice(0, 60).map((item: any) => <button className={resourceId === item.id ? "selected" : ""} key={item.id} onClick={() => setResourceId(item.id)}>
          <span className="resource-icon">{item.type === "database" ? <Database size={15} /> : item.type === "virtual_machine" ? <ServerCog size={15} /> : item.type === "storage" ? <HardDrive size={15} /> : <CloudCog size={15} />}</span>
          <div><strong>{item.name || item.id}</strong><small>{item.type} · {item.provider || item.subtype} · {item.cluster || "external"}</small><small>{item.business_service || item.owner || item.endpoint || item.host || "未绑定业务服务"}</small></div>
          <StatusPill status={item.actions_enabled ? "actions" : "read-only"} text={item.actions_enabled ? "可申请变更" : "只读诊断"} />
        </button>)}</div> : <div className="infra-config-guide">
          <Database size={22} />
          <div><strong>还没有接入 K8s 之外的资源</strong><p>在 ConfigMap 中配置 <code>INFRASTRUCTURE_RESOURCES_JSON</code>、<code>DATABASE_TARGETS_JSON</code>、<code>VM_TARGETS_JSON</code> 后，这里会自动出现数据库、虚拟机、中间件和存储资源。</p></div>
        </div>}
      </div>
      <div className="surface">
        <SectionHead icon={AlertTriangle} title="异常队列" meta={`${findings.length} findings`} />
        {scan.error && <div className="inline-error">{scan.error}</div>}
        {scan.loading && <Empty text="正在探测资源、读取指标并让 AI SRE 生成预演" />}
        {!scan.loading && findings.length ? <div className="infra-finding-list">{findings.map((item: any) => <button className={selectedFinding?.id === item.id ? "selected" : ""} key={item.id} onClick={() => setSelectedFindingId(item.id)}>
          <StatusPill status={item.severity || "P2"} />
          <strong>{item.title}</strong>
          <small>{item.resource_type}/{item.resource_id}</small>
          <p>{item.summary}</p>
        </button>)}</div> : !scan.loading && <Empty text={providers.data?.summary?.configured ? "点击 AI SRE 巡检后展示异常和可执行预演" : "先配置资源 Provider，再进行全栈巡检"} />}
      </div>
      <div className="surface span-two infra-plan-shell">
        <SectionHead icon={BrainCircuit} title="AI SRE 运维预演" meta={selectedResource ? `${selectedResource.type}/${selectedResource.name}` : selectedPlan?.target || "waiting"} />
        {selectedPlan ? <OpsPlanPanel plan={selectedPlan} /> : <div className="infra-config-guide">
          <ShieldCheck size={22} />
          <div><strong>执行边界已经预留</strong><p>数据库、虚拟机、存储和云资源的真实变更统一提交到 <code>INFRASTRUCTURE_ACTION_WEBHOOK_URL</code>。执行器负责对接 DBA、虚拟化平台、ITSM 或企业脚本平台，页面保留审批、审计和恢复验证。</p></div>
        </div>}
      </div>
    </section>
  </div>;
}

export function OperationsPage() {
  const [tab, setTab] = useState<"scan" | "incidents" | "alerts" | "postmortems" | "capabilities" | "skills">("scan");
  const [cluster, setCluster] = useState("all");
  const [namespace, setNamespace] = useState("all");
  const [intent, setIntent] = useState("crashloop");
  const [severity, setSeverity] = useState("auto");
  const [scan, setScan] = useState<ApiState<any>>({ loading: false });
  const [inventory] = useAsync<any>(() => apiGet("/api/rancher/inventory").catch(() => ({ clusters: [], inventory: [] })), []);
  const [incidents, refreshIncidents] = useAsync<any>(() => apiGet("/api/incidents"), []);
  const [alerts, refreshAlerts] = useAsync<any>(() => apiGet("/api/alerts"), []);
  const [postmortems, refreshPostmortems] = useAsync<any>(() => apiGet("/api/postmortems"), []);
  const [capabilities, refreshCapabilities] = useAsync<any>(() => apiGet("/api/ops/capabilities"), []);
  const sources = { incidents: list(incidents.data?.incidents), alerts: list(alerts.data?.alerts), postmortems: list(postmortems.data?.postmortems) };
  const rows = tab === "scan" || tab === "capabilities" || tab === "skills" ? [] : sources[tab];
  const clusters = list(inventory.data?.clusters);
  const scoped = cluster === "all" ? list(inventory.data?.inventory) : list(inventory.data?.inventory).filter((item: any) => cluster === item.cluster?.id || cluster === item.cluster?.name);
  const namespaces = Array.from(new Set(scoped.flatMap((item: any) => list(item.namespaces).map((entry: any) => String(entry.name))))).sort();
  const refresh = () => { refreshIncidents(); refreshAlerts(); refreshPostmortems(); refreshCapabilities(); };
  async function runScan() {
    setScan({ loading: true });
    try { setScan({ loading: false, data: await apiPost("/api/alert/scan", { cluster, namespace, intent, severity, auto_healing_enabled: false }) }); }
    catch (error: any) { setScan({ loading: false, error: error.message }); }
  }
  return <div className="unified-page">
    <div className="page-commandbar"><div className="segmented"><button className={tab === "scan" ? "active" : ""} onClick={() => setTab("scan")}>扫描诊断</button><button className={tab === "incidents" ? "active" : ""} onClick={() => setTab("incidents")}>事件</button><button className={tab === "alerts" ? "active" : ""} onClick={() => setTab("alerts")}>告警</button><button className={tab === "postmortems" ? "active" : ""} onClick={() => setTab("postmortems")}>复盘</button><button className={tab === "capabilities" ? "active" : ""} onClick={() => setTab("capabilities")}>运维工具</button><button className={tab === "skills" ? "active" : ""} onClick={() => setTab("skills")}>Skill 库</button></div><button className="ghost" onClick={refresh}><RefreshCcw size={15} />刷新</button></div>
    {tab === "scan" ? <div className="operations-scan-grid"><div className="surface"><SectionHead icon={Search} title="证据扫描" meta="仅在发现真实信号后触发 AI 诊断" /><div className="ops-scan-form"><label>集群<select value={cluster} onChange={(event) => { setCluster(event.target.value); setNamespace("all"); }}><option value="all">所有集群</option>{clusters.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></label><label>Namespace<select value={namespace} onChange={(event) => setNamespace(event.target.value)}><option value="all">所有 Namespace</option>{namespaces.map((item) => <option key={item} value={item}>{item}</option>)}</select></label><label>异常类型<select value={intent} onChange={(event) => setIntent(event.target.value)}><option value="crashloop">CrashLoop / 镜像 / OOM</option><option value="pending">Pending / 调度</option><option value="highcpu">高 CPU</option></select></label><label>严重级别<select value={severity} onChange={(event) => setSeverity(event.target.value)}><option value="auto">自动识别</option><option value="P1">P1</option><option value="P2">P2</option><option value="P3">P3</option></select></label><button className="primary" onClick={runScan} disabled={scan.loading}>{scan.loading ? <Loader2 className="spin" size={15} /> : <Search size={15} />}扫描并诊断</button></div></div><div className="surface"><SectionHead icon={BrainCircuit} title="诊断结果" meta={scan.data?.status || "waiting"} />{scan.error && <div className="inline-error">{scan.error}</div>}{scan.data ? <div className="scan-result"><StatusPill status={scan.data.status || "ok"} /><h3>{scan.data.reason || scan.data.scan?.findings?.[0]?.issue?.reason || "扫描完成"}</h3><p>{scan.data.results?.[0]?.answer || scan.data.answer || `检查 ${scan.data.evidence?.pods_checked ?? list(scan.data.scan?.findings).length} 个 Pod，发现 ${list(scan.data.scan?.findings).length} 条匹配信号。`}</p><div className="compact-list">{list(scan.data.scan?.findings).map((item: any) => <div className="compact-row" key={`${item.cluster}-${item.namespace}-${item.name}`}><span className="resource-icon risk"><Boxes size={14} /></span><div><strong>{item.name}</strong><small>{item.cluster}/{item.namespace} · {item.issue?.reason || item.phase}</small></div><StatusPill status={scan.data.scan?.severity || "warning"} /></div>)}</div></div> : <Empty text="选择范围和异常类型后开始扫描" />}</div></div> : tab === "capabilities" ? <div className="surface"><SectionHead icon={TerminalSquare} title="受控运维能力" meta={capabilities.data?.planner} /><div className="capability-grid">{list(capabilities.data?.actions).map((item: any) => <div className="capability-card" key={item.action || item.id}><span>{item.risk || "controlled"}</span><strong>{item.action || item.id || item.name}</strong><p>{item.description || item.summary || "通过证据、预演、审批和恢复验证执行"}</p></div>)}</div></div> : tab === "skills" ? <OpsSkillsPage /> : <div className="surface"><SectionHead icon={tab === "incidents" ? BellRing : tab === "alerts" ? AlertTriangle : FileClock} title={tab === "incidents" ? "事件时间线" : tab === "alerts" ? "告警记录" : "复盘报告"} meta={`${rows.length} records`} />{rows.length ? <div className="timeline-list">{rows.slice().reverse().map((item: any, index: number) => <div className="timeline-item" key={item.incident_id || item.id || index}><i /><div><div><strong>{item.title || item.alert_name || item.name || "记录"}</strong><StatusPill status={item.status || item.severity || "recorded"} /></div><p>{item.summary || item.description || item.root_cause || item.report || "已进入审计时间线"}</p><small>{item.cluster || ""} {item.namespace || ""} · {timeText(item.created_at || item.timestamp)}</small></div></div>)}</div> : <Empty text="暂无记录；新的告警与处置会自动进入这里" />}</div>}
  </div>;
}

type SkillChoice = {
  id: string;
  label?: string;
  name?: string;
  description?: string;
  when_to_use?: string;
  operator_note?: string;
  rollback?: string;
  risk?: string;
  auto_allowed?: boolean;
  allowed_targets?: string[];
  required_evidence?: string[];
};

const fallbackSkillOptions = {
  applies_to: [
    { id: "Pod", label: "Pod", description: "单个运行实例，适合日志、重启、挂载、探针和调度问题。" },
    { id: "Deployment", label: "Deployment", description: "无状态工作负载，适合模板、镜像、副本和发布问题。" },
    { id: "StatefulSet", label: "StatefulSet", description: "有状态工作负载，需关注稳定身份和持久卷。" },
    { id: "Service", label: "Service", description: "服务发现和流量入口，适合 selector、端口和 Endpoint 问题。" },
    { id: "Node", label: "Node", description: "集群节点，适合压力、NotReady、隔离和恢复调度问题。" },
    { id: "PVC", label: "PVC", description: "存储声明，适合 Pending、扩容和绑定问题。" },
    { id: "Database", label: "Database", description: "数据库实例或集群，适合连接、慢 SQL、锁、复制、容量和备份问题。" },
    { id: "MySQL", label: "MySQL", description: "MySQL / MariaDB 实例，关注连接池、主从复制、慢查询和 InnoDB 锁。" },
    { id: "Oracle", label: "Oracle", description: "Oracle 数据库实例，关注表空间、会话、归档、锁等待和 Data Guard。" },
    { id: "Redis", label: "Redis", description: "缓存与内存型数据库，关注内存、主从、慢命令、过期策略和连接数。" },
    { id: "VirtualMachine", label: "VirtualMachine", description: "虚拟机、云主机或物理主机，适合系统服务、磁盘、网络和 Agent 问题。" },
    { id: "LinuxHost", label: "LinuxHost", description: "Linux 主机，适合 systemd、文件系统、内核、进程和网络排障。" },
    { id: "StorageArray", label: "StorageArray", description: "企业存储或 NAS/SAN 后端，适合容量、路径、ACL 和快照问题。" },
  ],
  evidence_required: [
    { id: "previous_logs", label: "上一次容器日志", description: "CrashLoop 场景优先读取，定位上次退出前的错误。" },
    { id: "events", label: "Kubernetes Events", description: "确认调度、挂载、镜像、探针和准入失败。" },
    { id: "workload_spec", label: "Workload 配置", description: "读取镜像、探针、资源、卷和安全上下文。" },
    { id: "dependency_topology", label: "依赖拓扑", description: "读取 CMDB、调用链和跨集群中间件数据流。" },
    { id: "db_connectivity", label: "数据库连通性", description: "确认实例、监听端口、账号权限和网络路径。" },
    { id: "db_slow_queries", label: "慢 SQL 证据", description: "读取慢 SQL、执行计划和热点表信息。" },
    { id: "db_locks", label: "锁等待 / 长事务", description: "确认阻塞会话、锁等待、长事务和影响范围。" },
    { id: "db_replication", label: "复制 / HA 状态", description: "确认主从、延迟、只读、故障转移和同步状态。" },
    { id: "db_capacity", label: "数据库容量", description: "检查表空间、磁盘、连接数、内存和日志空间。" },
    { id: "vm_agent_status", label: "主机 Agent 状态", description: "确认监控、云助手、虚拟化 Agent 或安全 Agent 是否在线。" },
    { id: "vm_system_metrics", label: "主机系统指标", description: "读取 CPU、内存、磁盘、IO、网络和文件句柄。" },
    { id: "vm_service_status", label: "系统服务状态", description: "读取 systemd / Windows Service 状态和最近错误。" },
    { id: "vm_disk_usage", label: "主机磁盘使用", description: "确认文件系统、inode、挂载点、增长目录和扩容能力。" },
  ],
  success_criteria: [
    { id: "pod_ready", label: "Pod Ready", description: "目标 Pod 连续通过 readiness 并保持稳定。" },
    { id: "rollout_complete", label: "发布完成", description: "期望副本全部可用，generation 已收敛。" },
    { id: "restart_count_stable", label: "重启数稳定", description: "观察窗口内重启数不再增长。" },
    { id: "error_rate_recovered", label: "错误率恢复", description: "错误率回到 SLO 或变更前基线。" },
    { id: "db_connection_recovered", label: "数据库连接恢复", description: "业务连接成功率和实例连接数恢复到安全区间。" },
    { id: "db_replication_caught_up", label: "复制追平", description: "复制延迟回到阈值内，HA 状态正常。" },
    { id: "db_slow_query_reduced", label: "慢 SQL 降低", description: "慢查询和锁等待回落，核心 SQL 不再阻塞业务。" },
    { id: "vm_agent_online", label: "主机 Agent 在线", description: "监控、虚拟化或云助手 Agent 恢复在线。" },
    { id: "vm_service_active", label: "服务运行正常", description: "关键服务 active/running，业务探针恢复。" },
    { id: "vm_disk_pressure_relieved", label: "磁盘压力解除", description: "磁盘、inode 或挂载点容量回到安全阈值。" },
  ],
  script_triggers: [
    { id: "symptom_matched", label: "症状精确命中", description: "日志、事件或告警命中 Skill 症状关键词。" },
    { id: "required_evidence_collected", label: "必要证据已齐", description: "本 Skill 选择的必要证据全部采集完成。" },
    { id: "root_cause_confirmed", label: "根因已确认", description: "证据评分达到确认阈值，不凭猜测执行。" },
    { id: "manual_confirmation", label: "必须人工确认", description: "运维人员查看影响和参数后点击确认。" },
  ],
};

const fallbackActionOptions: SkillChoice[] = [
  { id: "apply_manifest", label: "应用 Kubernetes YAML", description: "创建或更新任意 Kubernetes API 资源。", risk: "high", when_to_use: "AI 已基于真实对象生成完整 YAML。", operator_note: "展示完整 YAML 与差异后逐步确认。" },
  { id: "patch_resource", label: "修改 Kubernetes 资源", description: "Patch Workload、ConfigMap、Secret、PV/PVC 等资源。", risk: "high", when_to_use: "已锁定 apiVersion、kind、namespace、name 和最小 Patch。", operator_note: "必须保存原对象快照。" },
  { id: "delete_resource", label: "删除 Kubernetes 资源", description: "删除一个明确目标资源。", risk: "high", when_to_use: "证据证明删除必要且影响可控。", operator_note: "必须显示恢复快照并逐步确认。" },
  { id: "run_shell", label: "执行平台 Shell", description: "在 Flawless 运行环境执行完整命令。", risk: "high", when_to_use: "Skill 明确要求平台侧命令。", operator_note: "逐步确认、超时、输出审计。" },
  { id: "exec_pod", label: "执行 Pod 命令", description: "进入指定 Pod/container 执行命令。", risk: "high", when_to_use: "必须在容器内取证或处置。", operator_note: "显示完整命令与目标后逐步确认。" },
  { id: "exec_node", label: "执行节点命令", description: "通过受控节点执行器运行命令。", risk: "high", when_to_use: "必须在指定 Kubernetes 节点处置。", operator_note: "特权高风险，逐步确认并审计。" },
  { id: "patch_workload", label: "修改 Workload 配置", description: "修正镜像、探针、资源、副本、环境变量或安全上下文。", risk: "medium", when_to_use: "证据确认 Deployment、StatefulSet 或 DaemonSet 模板配置有误。", operator_note: "执行前展示差异，可恢复原模板回滚。" },
  { id: "restart", label: "滚动重启组件", description: "触发受控滚动重启，不修改 Workload 配置。", risk: "medium", when_to_use: "配置正确但进程卡死、连接未刷新或需要重新拉起 Pod。", operator_note: "不会修复错误配置，需确认副本和 PDB 安全。" },
  { id: "scale_out", label: "增加副本", description: "在平台上限内增加 Workload 副本。", risk: "medium", when_to_use: "CPU、流量或并发证据证明容量不足。", operator_note: "观察资源配额和下游依赖承载能力。" },
  { id: "recreate_pod", label: "重建异常 Pod", description: "删除单个异常 Pod，由控制器按原模板重建。", risk: "medium", when_to_use: "只有单个 Pod 状态异常，模板和其他副本正常。", operator_note: "不适合模板级或存储级故障。" },
  { id: "rollback_workload", label: "回滚 Workload", description: "回滚到真实观测过的稳定镜像或模板 revision。", risk: "high", when_to_use: "故障与最近发布高度相关，并存在稳定回滚点。", operator_note: "高风险，必须人工确认。" },
  { id: "create_pvc", label: "创建缺失 PVC", description: "按批准存储策略创建 Workload 缺失的 PVC。", risk: "high", when_to_use: "Workload 明确引用不存在的 PVC，容量和访问模式已确认。", operator_note: "不能由 LLM 编造 StorageClass 和容量策略。" },
  { id: "create_pv", label: "创建静态 PV", description: "按存储管理员批准模板创建静态 PV。", risk: "high", when_to_use: "动态供卷不可用且后端路径、回收策略已批准。", operator_note: "严禁编造 NFS、LUN 或目录路径。" },
  { id: "patch_workload_volume", label: "修正卷引用", description: "修正 Workload 的 PVC、volume 或 mount 引用。", risk: "high", when_to_use: "完整存储链证据证明原卷引用错误。", operator_note: "需要保存原配置回滚点。" },
  { id: "patch_service", label: "修正 Service", description: "修正 selector、port 或 targetPort 不匹配。", risk: "high", when_to_use: "Service 没有 Endpoint，且证据证明配置不匹配。", operator_note: "错误修改会造成流量黑洞。" },
  { id: "patch_service_account", label: "修正 ServiceAccount", description: "绑定企业批准的 imagePullSecret。", risk: "medium", when_to_use: "镜像拉取失败且缺少批准的凭据引用。", operator_note: "不读取或修改 Secret 明文。" },
  { id: "create_configmap", label: "恢复 ConfigMap", description: "从运维人员批准模板恢复缺失 ConfigMap。", risk: "high", when_to_use: "Workload 引用的配置缺失且存在批准模板。", operator_note: "不能让 LLM 自行生成生产配置值。" },
  { id: "patch_hpa", label: "调整 HPA 范围", description: "调整 HPA 最小和最大副本。", risk: "medium", when_to_use: "HPA 上下限阻止合理扩缩容，指标语义正常。", operator_note: "不修改 HPA 指标算法。" },
  { id: "expand_pvc", label: "扩容 PVC", description: "扩展支持在线扩容的已绑定 PVC。", risk: "high", when_to_use: "卷容量逼近上限且 StorageClass 支持扩容。", operator_note: "通常不可逆，需核对备份和文件系统。" },
  { id: "cordon_node", label: "隔离节点", description: "停止在问题节点上调度新 Pod。", risk: "high", when_to_use: "节点明确存在压力、NotReady 或硬件故障。", operator_note: "不会自动迁移已有 Pod。" },
  { id: "evict_pod", label: "受控驱逐 Pod", description: "通过 Eviction API 迁移 Pod，并遵守 PDB。", risk: "high", when_to_use: "节点维护或隔离后需要迁移工作负载。", operator_note: "高风险且必须人工确认。" },
  { id: "uncordon_node", label: "恢复节点调度", description: "将已恢复节点重新加入调度。", risk: "high", when_to_use: "节点 Ready、压力和系统组件均已恢复。", operator_note: "恢复前必须完成健康验证。" },
  { id: "patch_pdb", label: "修正 PDB", description: "修正导致发布或驱逐死锁的中断预算。", risk: "high", when_to_use: "PDB 与副本数形成死锁且业务可用性证据充分。", operator_note: "持续观察可用副本和 SLO。" },
  { id: "db_expand_storage", label: "扩容数据库存储", description: "通过 DBA/存储受控执行器扩容数据库表空间或磁盘。", risk: "high", when_to_use: "数据库容量证据达到阈值，备份和扩容策略已确认。", operator_note: "通常不可逆，必须保留变更单和容量审批。" },
  { id: "db_kill_session", label: "终止阻塞会话", description: "终止确认阻塞业务的数据库会话。", risk: "high", when_to_use: "锁等待、长事务和会话来源证据完整。", operator_note: "必须展示会话、SQL、业务影响和回滚说明。" },
  { id: "db_failover", label: "数据库主备切换", description: "按 HA 预案触发数据库故障转移。", risk: "high", when_to_use: "主库故障或复制链路异常且备用节点健康。", operator_note: "必须二次确认 RPO/RTO、只读状态和回切方案。" },
  { id: "db_apply_parameter", label: "调整数据库参数", description: "按批准模板调整数据库运行参数。", risk: "high", when_to_use: "证据证明参数导致连接、锁或性能故障。", operator_note: "不能由 LLM 编造生产参数值。" },
  { id: "db_restart_instance", label: "重启数据库实例", description: "通过受控执行器重启数据库实例。", risk: "high", when_to_use: "只在 HA、窗口、备份和影响范围均确认后使用。", operator_note: "高风险，通常作为最后手段。" },
  { id: "vm_restart_service", label: "重启主机服务", description: "重启虚拟机或主机上的指定系统服务。", risk: "medium", when_to_use: "服务进程异常且配置、依赖、磁盘和权限已确认。", operator_note: "必须指定服务名和恢复探针。" },
  { id: "vm_expand_disk", label: "扩容主机磁盘", description: "扩展虚拟磁盘并执行文件系统扩容。", risk: "high", when_to_use: "磁盘或 inode 压力达到阈值，快照和挂载点已确认。", operator_note: "需要外部虚拟化/云平台执行器。" },
  { id: "vm_reboot", label: "重启虚拟机", description: "对故障主机执行受控重启。", risk: "high", when_to_use: "内核、Agent、系统服务无法恢复，且业务冗余已确认。", operator_note: "必须作为高风险动作二次确认。" },
  { id: "middleware_rebalance", label: "中间件再均衡", description: "对 Kafka/MQ 等中间件执行分区或实例再均衡。", risk: "high", when_to_use: "消费者滞后、Broker 压力或分区分布异常证据充分。", operator_note: "需要限速、窗口和回滚策略。" },
  { id: "storage_expand_volume", label: "扩容存储卷", description: "通过存储受控执行器扩展企业存储卷。", risk: "high", when_to_use: "存储池、卷、映射和业务挂载关系确认无误。", operator_note: "必须由存储团队批准容量策略。" },
  { id: "infra_run_approved_action", label: "执行批准基础设施动作", description: "调用外部执行器中已经登记的企业标准动作。", risk: "high", when_to_use: "非 K8s 对象需要平台外动作，且动作已在企业执行器登记。", operator_note: "平台只传递结构化计划，不执行任意命令。" },
];

function createEmptySkillForm() {
  return {
    id: "",
    name: "",
    category: "runtime",
    summary: "",
    symptoms: "",
    applies_to: ["Pod", "Deployment", "StatefulSet", "Service"],
    evidence_required: ["previous_logs", "events", "workload_spec"],
    diagnostic_steps: "",
    allowed_actions: ["patch_workload", "recreate_pod"],
    success_criteria: ["pod_ready", "restart_count_stable"],
    risk: "medium",
    owner: "",
    script_enabled: false,
    script_id: "",
    script_trigger_conditions: ["required_evidence_collected", "root_cause_confirmed", "manual_confirmation"],
    script_trigger_description: "",
    script_timeout_seconds: 120,
  };
}

type SkillForm = ReturnType<typeof createEmptySkillForm>;

function splitSkillList(value: string) {
  return value.split(/[\n,，]+/).map((item) => item.trim()).filter(Boolean);
}

function SkillMultiSelect({
  title,
  selected,
  options,
  onChange,
  onInspect,
  hint,
}: {
  title: string;
  selected: string[];
  options: SkillChoice[];
  onChange: (value: string[]) => void;
  onInspect: (option: SkillChoice, title: string) => void;
  hint: string;
}) {
  function toggle(id: string) {
    onChange(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]);
  }
  const selectedOptions = selected.map((id) => options.find((item) => item.id === id) || { id, label: id });
  return <div className="skill-multiselect">
    <div className="skill-field-title"><span>{title}</span><small>可多选 · {selected.length} 项</small></div>
    <details>
      <summary>{selected.length ? selectedOptions.slice(0, 3).map((item) => item.label || item.name || item.id).join("、") + (selected.length > 3 ? ` 等 ${selected.length} 项` : "") : hint}<ChevronRight size={14} /></summary>
      <div className="skill-option-menu">
        {options.map((option) => <div className={selected.includes(option.id) ? "selected" : ""} key={option.id}>
          <label><input type="checkbox" checked={selected.includes(option.id)} onChange={() => toggle(option.id)} /><span><b>{option.label || option.name || option.id}</b><small>{option.description || option.when_to_use || option.id}</small></span></label>
          <button type="button" onClick={() => onInspect(option, title)} title={`查看${option.label || option.id}说明`}><Eye size={14} /></button>
        </div>)}
      </div>
    </details>
    <div className="skill-selected-chips">
      {selectedOptions.map((option) => <button type="button" key={option.id} onClick={() => toggle(option.id)} title="点击移除">{option.label || option.name || option.id}<X size={11} /></button>)}
    </div>
  </div>;
}

export function OpsSkillsPage() {
  const [skills, refreshSkills] = useAsync<any>(() => apiGet("/api/ops/skills"), []);
  const [capabilities] = useAsync<any>(() => apiGet("/api/ops/capabilities"), []);
  const [form, setForm] = useState<SkillForm>(() => createEmptySkillForm());
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [matchQuestion, setMatchQuestion] = useState("Pod CrashLoopBackOff，previous log 提示 permission denied，挂载 PVC 后启动失败");
  const [match, setMatch] = useState<ApiState<any>>({ loading: false });
  const [inspected, setInspected] = useState<{ title: string; option: SkillChoice } | null>(null);
  const [importing, setImporting] = useState(false);
  const importInput = useRef<HTMLInputElement>(null);
  const actions = (list(capabilities.data?.actions).length ? list(capabilities.data?.actions) : fallbackActionOptions) as SkillChoice[];
  const optionCatalog = capabilities.data?.skill_options || fallbackSkillOptions;
  const appliesToOptions = (list(optionCatalog.applies_to).length ? list(optionCatalog.applies_to) : fallbackSkillOptions.applies_to) as SkillChoice[];
  const evidenceOptions = (list(optionCatalog.evidence_required).length ? list(optionCatalog.evidence_required) : fallbackSkillOptions.evidence_required) as SkillChoice[];
  const successOptions = (list(optionCatalog.success_criteria).length ? list(optionCatalog.success_criteria) : fallbackSkillOptions.success_criteria) as SkillChoice[];
  const scriptTriggerOptions = (list(optionCatalog.script_triggers).length ? list(optionCatalog.script_triggers) : fallbackSkillOptions.script_triggers) as SkillChoice[];
  const approvedScripts = list(capabilities.data?.approved_scripts) as SkillChoice[];
  const selectedScript = approvedScripts.find((item) => item.id === form.script_id);

  function update<K extends keyof SkillForm>(key: K, value: SkillForm[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function inspectOption(option: SkillChoice, title: string) {
    setInspected({ title, option });
  }

  function editSkill(skill: any) {
    const scriptPolicy = skill.script_policy || {};
    setForm({
      id: skill.id || "",
      name: skill.name || "",
      category: skill.category || "runtime",
      summary: skill.summary || "",
      symptoms: list(skill.symptoms).join("\n"),
      applies_to: list(skill.applies_to),
      evidence_required: list(skill.evidence_required),
      diagnostic_steps: list(skill.diagnostic_steps).join("\n"),
      allowed_actions: list(skill.allowed_actions),
      success_criteria: list(skill.success_criteria),
      risk: skill.risk || "medium",
      owner: skill.owner || "",
      script_enabled: Boolean(scriptPolicy.enabled),
      script_id: scriptPolicy.script_id || "",
      script_trigger_conditions: list(scriptPolicy.trigger_conditions),
      script_trigger_description: scriptPolicy.trigger_description || "",
      script_timeout_seconds: Number(scriptPolicy.timeout_seconds || 120),
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function saveSkill() {
    setSaving(true);
    setMessage("");
    try {
      await apiPost("/api/ops/skills", {
        id: form.id,
        name: form.name,
        category: form.category,
        summary: form.summary,
        symptoms: splitSkillList(form.symptoms),
        applies_to: form.applies_to,
        evidence_required: form.evidence_required,
        diagnostic_steps: splitSkillList(form.diagnostic_steps),
        allowed_actions: form.allowed_actions,
        success_criteria: form.success_criteria,
        risk: form.risk,
        owner: form.owner,
        script_policy: {
          enabled: form.script_enabled,
          script_id: form.script_enabled ? form.script_id : "",
          trigger_conditions: form.script_enabled ? form.script_trigger_conditions : [],
          trigger_description: form.script_enabled ? form.script_trigger_description : "",
          timeout_seconds: form.script_timeout_seconds,
          require_confirmation: true,
        },
      });
      setForm(createEmptySkillForm());
      setMessage("Skill 已保存，会参与 SRE 对话和 AI 巡检的自动匹配。");
      refreshSkills();
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setSaving(false);
    }
  }

  async function disableSkill(skill: any) {
    setMessage("");
    try {
      await apiPost(`/api/ops/skills/${encodeURIComponent(skill.id)}/delete`, {});
      setMessage(skill.builtin ? "内置 Skill 已禁用。" : "自定义 Skill 已删除。");
      refreshSkills();
    } catch (error: any) {
      setMessage(error.message);
    }
  }

  async function testMatch() {
    setMatch({ loading: true });
    try {
      setMatch({ loading: false, data: await apiPost("/api/ops/skills/match", { question: matchQuestion, top_k: 5 }) });
    } catch (error: any) {
      setMatch({ loading: false, error: error.message });
    }
  }

  async function importSkill(file?: File) {
    if (!file) return;
    setImporting(true);
    setMessage("");
    try {
      const body = new FormData();
      body.append("file", file);
      const response = await fetch("/api/ops/skills/import", { method: "POST", body, headers: adminAuthHeaders({ Accept: "application/json" }) });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `${response.status} ${response.statusText}`);
      invalidateApiCache("/api/ops/skills");
      invalidateApiCache("/api/ops/capabilities");
      setMessage(data.message || `已导入 ${list(data.imported).length} 个标准 Agent Skill。`);
      refreshSkills();
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setImporting(false);
      if (importInput.current) importInput.current.value = "";
    }
  }

  function exportSkill(skill: any) {
    window.location.assign(`/api/ops/skills/${encodeURIComponent(skill.id)}/export`);
  }

  return <div className="skill-workbench">
    <div className="surface">
      <SectionHead icon={BrainCircuit} title="运维 Skill 注入" meta="保存即生成标准 SKILL.md，可跨智能体复用" action={<div className="skill-head-actions"><input ref={importInput} type="file" accept=".zip,application/zip" hidden onChange={(event) => importSkill(event.target.files?.[0])} /><button className="ghost" onClick={() => importInput.current?.click()} disabled={importing}>{importing ? <Loader2 className="spin" size={15} /> : <Upload size={15} />}导入 Skill</button><button className="ghost" onClick={refreshSkills}><RefreshCcw size={15} />刷新</button></div>} />
      <div className="skill-form">
        <label>Skill 名称<input value={form.name} onChange={(event) => update("name", event.target.value)} placeholder="例如：PVC Pending 静态 PV 恢复" /></label>
        <label>类别<select value={form.category} onChange={(event) => update("category", event.target.value)}><option value="runtime">运行时</option><option value="database">数据库</option><option value="virtual_machine">虚拟机 / 主机</option><option value="middleware">中间件</option><option value="storage">存储</option><option value="network">网络</option><option value="release">发布</option><option value="security">安全</option><option value="cloud">云资源</option><option value="custom">自定义</option></select></label>
        <label>风险<select value={form.risk} onChange={(event) => update("risk", event.target.value)}><option value="low">low</option><option value="medium">medium</option><option value="high">high</option></select></label>
        <label>负责人<input value={form.owner} onChange={(event) => update("owner", event.target.value)} placeholder="团队或姓名" /></label>
        <label className="span-two">一句话说明<textarea value={form.summary} onChange={(event) => update("summary", event.target.value)} placeholder="这条经验解决什么场景，AI 什么时候应该考虑它；可以是 K8s、数据库、虚拟机、存储或中间件。" /></label>
        <label>症状关键词<textarea value={form.symptoms} onChange={(event) => update("symptoms", event.target.value)} placeholder="一行一个，例如 FailedMount、permission denied、ImagePullBackOff、表空间不足、锁等待、主机磁盘满" /></label>
        <label>诊断步骤<textarea value={form.diagnostic_steps} onChange={(event) => update("diagnostic_steps", event.target.value)} placeholder="按真实运维流程写，一行一步。" /></label>
        <SkillMultiSelect title="适用对象" selected={form.applies_to} options={appliesToOptions} onChange={(value) => update("applies_to", value)} onInspect={inspectOption} hint="选择 K8s、数据库、虚拟机、中间件、存储或云资源对象" />
        <SkillMultiSelect title="需要证据" selected={form.evidence_required} options={evidenceOptions} onChange={(value) => update("evidence_required", value)} onInspect={inspectOption} hint="选择执行前必须读取的真实证据" />
        <SkillMultiSelect title="允许动作" selected={form.allowed_actions} options={actions} onChange={(value) => update("allowed_actions", value)} onInspect={inspectOption} hint="选择经过平台门禁的受控动作" />
        <SkillMultiSelect title="恢复判据" selected={form.success_criteria} options={successOptions} onChange={(value) => update("success_criteria", value)} onInspect={inspectOption} hint="选择如何客观判断问题已恢复" />
        <div className="skill-script-policy span-two">
          <div className="skill-script-header">
            <div><ShieldCheck size={16} /><span><strong>企业批准脚本</strong><small>可选能力，脚本正文不进入 Skill</small></span></div>
            <label className="skill-toggle"><input type="checkbox" checked={form.script_enabled} onChange={(event) => update("script_enabled", event.target.checked)} /><i /><span>{form.script_enabled ? "允许作为候选" : "不使用脚本"}</span></label>
          </div>
          {form.script_enabled && <div className="skill-script-body">
            <label>批准脚本<select value={form.script_id} onChange={(event) => update("script_id", event.target.value)}>
              <option value="">选择 ConfigMap 中登记的脚本</option>
              {approvedScripts.filter((item: any) => item.enabled !== false).map((item) => <option key={item.id} value={item.id}>{item.name || item.id} · {item.risk || "high"}</option>)}
            </select></label>
            <div className="script-inspect">
              <button type="button" className="ghost tiny" disabled={!selectedScript} onClick={() => selectedScript && inspectOption(selectedScript, "企业批准脚本")}><Eye size={13} />查看脚本说明</button>
              {!approvedScripts.length && <small>尚未配置 OPS_APPROVED_SCRIPTS_JSON，脚本模式不能保存。</small>}
            </div>
            <SkillMultiSelect title="脚本触发条件" selected={form.script_trigger_conditions} options={scriptTriggerOptions} onChange={(value) => update("script_trigger_conditions", value)} onInspect={inspectOption} hint="选择必须同时满足的触发门槛" />
            <label>最长执行时间<select value={form.script_timeout_seconds} onChange={(event) => update("script_timeout_seconds", Number(event.target.value))}><option value={30}>30 秒</option><option value={60}>60 秒</option><option value={120}>120 秒</option><option value={300}>300 秒</option><option value={600}>600 秒</option></select></label>
            <label className="span-two">具体触发场景<textarea value={form.script_trigger_description} onChange={(event) => update("script_trigger_description", event.target.value)} placeholder="例如：Pod 连续 3 次 CrashLoop，previous log 明确出现 permission denied，PVC 已 Bound，且 securityContext 与存储目录权限不一致时，允许调用该脚本；仅凭用户描述不得触发。" /></label>
            <div className="skill-script-guard span-two"><ShieldCheck size={15} /><span>脚本必须先在 ConfigMap 批准目录登记。命中 Skill 后只会成为候选，仍需证据齐全、影响范围检查、人工确认、超时控制和执行审计。</span></div>
          </div>}
        </div>
        {inspected && <div className="skill-info-panel span-two">
          <header><div><Eye size={15} /><span><small>{inspected.title}</small><strong>{inspected.option.label || inspected.option.name || inspected.option.id}</strong></span></div><button type="button" onClick={() => setInspected(null)} title="关闭说明"><X size={16} /></button></header>
          <p>{inspected.option.description || inspected.option.when_to_use || "暂无详细说明。"}</p>
          <div>
            {inspected.option.when_to_use && <span><b>何时使用</b>{inspected.option.when_to_use}</span>}
            {inspected.option.operator_note && <span><b>操作注意</b>{inspected.option.operator_note}</span>}
            {inspected.option.risk && <span><b>风险等级</b>{inspected.option.risk}</span>}
            {typeof inspected.option.auto_allowed === "boolean" && <span><b>自动执行</b>{inspected.option.auto_allowed ? "满足门禁时允许" : "必须人工确认"}</span>}
            {inspected.option.rollback && <span><b>回退方式</b>{inspected.option.rollback}</span>}
            {list(inspected.option.allowed_targets).length > 0 && <span><b>允许对象</b>{list(inspected.option.allowed_targets).join("、")}</span>}
            {list(inspected.option.required_evidence).length > 0 && <span><b>脚本前置证据</b>{list(inspected.option.required_evidence).join("、")}</span>}
          </div>
        </div>}
        <div className="skill-portability-note span-two"><Workflow size={15} /><span><strong>兼容 Agent Skills 开放规范</strong><small>平台会生成 SKILL.md、agents/openai.yaml 与 references/ops-policy.yaml；原有证据门禁和执行审批保持不变。</small></span></div>
        <button className="primary span-two" onClick={saveSkill} disabled={saving || !form.name.trim()}>{saving ? <Loader2 className="spin" size={15} /> : <CheckCircle2 size={15} />}保存并生成 Skill 包</button>
        {message && <div className={message.includes("已") ? "success-box span-two" : "inline-error span-two"}>{message}</div>}
      </div>
    </div>
    <div className="surface">
      <SectionHead icon={Search} title="匹配测试" meta="模拟 AI 如何选择专家经验" />
      <div className="skill-match-box">
        <textarea value={matchQuestion} onChange={(event) => setMatchQuestion(event.target.value)} />
        <button className="primary" onClick={testMatch} disabled={match.loading}>{match.loading ? <Loader2 className="spin" size={15} /> : <Sparkles size={15} />}测试匹配</button>
      </div>
      {match.error && <div className="inline-error">{match.error}</div>}
      <div className="skill-match-list">
        {list(match.data?.matches).map((item: any) => <div className="skill-card matched" key={item.skill?.id}><span>{Math.round(Number(item.confidence || 0) * 100)}%</span><strong>{item.skill?.name}</strong><p>{item.why}</p><small>{list(item.matched_terms).slice(0, 8).join(" / ")}</small></div>)}
      </div>
    </div>
    <div className="surface span-two">
      <SectionHead icon={TerminalSquare} title="Skill 库" meta={`${skills.data?.summary?.enabled || 0}/${skills.data?.summary?.total || 0} enabled · ${skills.data?.summary?.candidates || 0} 待审核`} />
      {skills.error && <div className="inline-error">{skills.error}</div>}
      <div className="skill-grid">
        {list(skills.data?.skills).map((skill: any) => <article className={`skill-card ${skill.enabled ? "" : "disabled"}`} key={skill.id}>
          <div><span>{skill.lifecycle === "candidate" ? "候选待审核" : skill.category} · {skill.risk} · v{skill.version || "1.0.0"}</span><strong>{skill.name}</strong></div>
          <p>{skill.summary}</p>
          <div className="chips">{list(skill.allowed_actions).slice(0, 4).map((item: any) => <span key={item}>{item}</span>)}</div>
          {skill.script_policy?.enabled && <div className="skill-script-badge"><TerminalSquare size={13} /><span>批准脚本：{skill.script_policy.script_id}</span></div>}
          <footer><small>{skill.builtin ? "内置" : skill.lifecycle === "candidate" ? "AI 从成功恢复中沉淀" : "自定义"} · {skill.owner || "operator"} · {skill.execution_ready ? "可执行映射" : "指令型"}</small><div><button className="ghost tiny" onClick={() => exportSkill(skill)} title="导出标准 Agent Skill ZIP"><Download size={13} />导出</button><button className="ghost tiny" onClick={() => editSkill(skill)}>{skill.lifecycle === "candidate" ? "审核并发布" : "编辑"}</button><button className="ghost tiny" onClick={() => disableSkill(skill)}>{skill.builtin ? "禁用" : "删除"}</button></div></footer>
        </article>)}
      </div>
    </div>
  </div>;
}

function flatten(value: any, prefix = "", rows: Array<{ label: string; value: string }> = []) {
  if (rows.length >= 9) return rows;
  if (Array.isArray(value)) { rows.push({ label: prefix || "items", value: value.length ? value.slice(0, 3).map((item) => Array.isArray(item) ? item.join(" → ") : typeof item === "object" ? Object.values(item).join(" · ") : String(item)).join("；") : "0 项" }); return rows; }
  if (value && typeof value === "object") { Object.entries(value).forEach(([key, item]) => flatten(item, prefix ? `${prefix}.${key}` : key, rows)); return rows; }
  if (prefix) rows.push({ label: prefix.split(".").pop()!.replaceAll("_", " "), value: value === undefined || value === null || value === "" ? "-" : String(value) });
  return rows;
}

export function AlgorithmsPage() {
  const [state, refresh] = useAsync<any>(() => apiGet("/api/algorithms/workbench"), []);
  const cases = list(state.data?.cases);
  const decisions = list(state.data?.recent_decisions);
  return <div className="unified-page"><div className="page-commandbar"><div className="quiet-note"><BrainCircuit size={15} />算法只在实际决策链路中展示，不做静态概念陈列</div><button className="ghost" onClick={refresh}><RefreshCcw size={15} />刷新</button></div>
    <div className="algorithm-overview">{list(state.data?.module_map).map((item: any, index: number) => <div className="algorithm-stage" key={item.algorithm}><span>0{index + 1}</span><div><strong>{item.module}</strong><small>{item.algorithm}</small></div><ChevronRight size={16} /><p>{item.effect}</p></div>)}</div>
    {cases.length ? <div className="algorithm-case-grid">{cases.map((item: any) => <div className="surface algorithm-case" key={item.id}><SectionHead icon={Workflow} title={item.title} meta={item.where_used} /><div className="decision-flow"><div><span>输入证据</span>{flatten(item.input).map((row) => <b key={row.label}>{row.label}<small>{row.value}</small></b>)}</div><i>→</i><div className="algorithm-core"><BrainCircuit size={22} /><strong>{item.algorithm}</strong></div><i>→</i><div><span>决策输出</span>{flatten(item.output).map((row) => <b key={row.label}>{row.label}<small>{row.value}</small></b>)}</div></div><p className="algorithm-effect">{item.action_effect}</p></div>)}</div> : <div className="surface"><Empty text="运行一次巡检、拓扑分析或变更门禁后，这里会出现真实算法样本" /></div>}
    <div className="surface"><SectionHead icon={FileClock} title="决策审计" meta={`${decisions.length} decisions`} />{decisions.length ? <div className="audit-grid">{decisions.slice(0, 12).map((item: any, index: number) => <div key={`${item.timestamp}-${index}`}><StatusPill status="recorded" text={item.algorithm} /><strong>{item.used_by}</strong><p>{item.action_effect}</p><small>{timeText(item.timestamp)}</small></div>)}</div> : <Empty text="暂无算法审计记录" />}</div>
  </div>;
}

export function SignalsPage() {
  const [cluster, setCluster] = useState("all");
  const [inventory] = useAsync<any>(() => apiGet("/api/rancher/inventory").catch(() => ({ clusters: [] })), []);
  const [metrics, refreshMetrics] = useAsync<any>(() => apiGet(`/api/prometheus/summary?cluster=${encodeURIComponent(cluster)}`), [cluster]);
  const [llm, refreshLlm] = useAsync<any>(() => apiGet("/api/llm-observability?limit=200"), []);
  const [integrations, refreshIntegrations] = useAsync<any>(() => apiGet("/api/integrations"), []);
  const [logs, setLogs] = useState<ApiState<any>>({ loading: false });
  const [traces, setTraces] = useState<ApiState<any>>({ loading: false });
  const [logQuery, setLogQuery] = useState('{namespace=~".+"}');
  const [traceService, setTraceService] = useState("");
  const [selectedCall, setSelectedCall] = useState<any>(null);
  const values = metrics.data?.values || {};
  const summary = llm.data?.summary || {};
  const analytics = llm.data?.analytics || {};
  const weekly = analytics.weekly_analysis || {};
  const langfuse = llm.data?.langfuse || {};
  const clusters = list(inventory.data?.clusters);
  const sources = list(integrations.data?.items).filter((item: any) => item.category === "observability");
  async function queryLogs() {
    setLogs({ loading: true });
    try { setLogs({ loading: false, data: await apiPost("/api/observability/logs", { query: logQuery, limit: 80 }) }); } catch (error: any) { setLogs({ loading: false, error: error.message }); }
  }
  async function queryTraces() {
    setTraces({ loading: true });
    try { setTraces({ loading: false, data: await apiGet(`/api/observability/traces?service=${encodeURIComponent(traceService)}&limit=30`) }); } catch (error: any) { setTraces({ loading: false, error: error.message }); }
  }
  const refresh = () => { refreshMetrics(); refreshLlm(); refreshIntegrations(); };
  const maxDailyTokens = Math.max(1, ...list(analytics.daily_usage).map((item: any) => Number(item.tokens || 0)));
  return <div className="unified-page"><div className="page-commandbar"><div className="scope-control"><span>Metrics scope</span><select value={cluster} onChange={(event) => setCluster(event.target.value)}><option value="all">所有集群</option>{clusters.map((item: any) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></div><button className="ghost" onClick={refresh}><RefreshCcw size={15} />刷新</button></div>
    <section className="kpi-grid six"><Kpi label="CPU" value={`${Number(values.cpu_cores || 0).toFixed(2)} C`} detail={metrics.data?.source || "Prometheus"} /><Kpi label="内存" value={`${(Number(values.memory_bytes || 0) / 1024 / 1024 / 1024).toFixed(2)} GiB`} detail="working set" /><Kpi label="重启 / 1h" value={values.pod_restarts_1h || 0} /><Kpi label="LLM 调用" value={summary.total || 0} detail={`${summary.failures || 0} failed`} /><Kpi label="Token" value={compactNumber(summary.total_tokens)} detail={`$${Number(summary.estimated_cost_usd || 0).toFixed(4)} · ${compactNumber(summary.input_tokens)} in`} /><Kpi label="P95" value={`${summary.p95_latency_ms || 0} ms`} detail={`${summary.throughput_per_min || 0} req/min`} /></section>
    <section className="unified-grid signals-grid">
      <div className="surface span-two"><SectionHead icon={Activity} title="信号源" meta="Metrics · Logs · Traces · LLM" /><div className="integration-strip">{sources.map((item: any) => <div key={item.id}><span className="resource-icon"><Database size={15} /></span><div><strong>{item.name}</strong><small>{item.capability}</small></div><StatusPill status={item.status} /></div>)}</div></div>
      <div className="surface"><SectionHead icon={Gauge} title="模型调用分布" /><div className="mini-bars">{list(analytics.by_model).slice(0, 7).map((item: any) => <div key={item.name}><span>{item.name}</span><i><b style={{ width: `${Math.min(100, Number(item.calls || 0) * 10)}%` }} /></i><strong>{item.calls || 0}</strong></div>)}</div></div>
      <div className="surface span-three langfuse-lens"><SectionHead icon={GitBranch} title="Langfuse 黑盒拆解" meta={`${summary.langfuse_traces || 0} traces · ${langfuse.active ? "active" : langfuse.configured ? "configured" : "not configured"}`} /><div className="langfuse-chain">{["User", "Session", "Trace", "Generation", "Tool Call", "Score"].map((item) => <div key={item}><span>{item}</span><small>{item === "User" ? "Operator / Alert" : item === "Session" ? "Incident / Inspection" : item === "Trace" ? "SRE Workflow" : item === "Generation" ? "LLM Tokens" : item === "Tool Call" ? "MCP / Healing" : "Quality Eval"}</small></div>)}</div><div className="quality-strip">{list(analytics.quality_scores).length ? list(analytics.quality_scores).map((item: any) => <div key={item.name}><span>{item.name}</span><i><b style={{ width: `${Math.round(Number(item.avg || 0) * 100)}%` }} /></i><strong>{Math.round(Number(item.avg || 0) * 100)}</strong></div>) : <Empty text="运行 SRE 对话或巡检后展示 Langfuse 质量评分" />}</div></div>
      <div className="surface span-two"><SectionHead icon={LineChart} title="每日 Token 用量" meta={`${weekly.observed_days || 0} observed days`} /><div className="usage-chart">{list(analytics.daily_usage).length ? list(analytics.daily_usage).map((item: any) => <div key={item.date}><div><i style={{ height: `${Math.max(4, Number(item.tokens || 0) / maxDailyTokens * 100)}%` }} /></div><strong>{compactNumber(item.tokens)}</strong><span>{item.date?.slice(5)}</span></div>) : <Empty text="产生 LLM 调用后展示每日 Token 曲线" />}</div></div>
      <div className="surface"><SectionHead icon={BrainCircuit} title="一周用量预测" /><div className="weekly-forecast"><div><span>不开自动巡检</span><strong>{compactNumber(weekly.weekly_tokens_without_auto_inspection)}</strong></div><div><span>开启自动巡检</span><strong>{compactNumber(weekly.weekly_tokens_with_auto_inspection)}</strong></div><p>每 {weekly.inspection_interval_minutes || 30} 分钟巡检，预计增加 {compactNumber(weekly.auto_inspection_extra_tokens)} Token / 周</p></div></div>
      <div className="surface"><SectionHead icon={Workflow} title="LLM 数据流" /><div className="flow-list">{list(analytics.data_flows).map((item: any, index: number) => <div key={item.name}><span>{String(index + 1).padStart(2, "0")}</span><strong>{item.name}</strong><small>{item.count} calls</small></div>)}</div></div>
      <div className="surface span-two"><SectionHead icon={FileClock} title="调用审计" meta={`${summary.shown || 0} shown`} /><div className="call-table"><div><span>时间</span><span>来源 / 模型</span><span>延迟</span><span>状态</span><span /></div>{list(llm.data?.items).slice(0, 80).map((item: any) => <button key={item.id} onClick={() => setSelectedCall(item)}><span>{timeText(item.timestamp)}</span><span>{item.source}<small>{item.llm?.model_profile_id || item.llm?.model}{item.trace_id ? ` · ${String(item.trace_id).slice(0, 10)}` : ""}</small></span><span>{item.latency_ms || 0} ms</span><StatusPill status={item.status || "unknown"} /><Eye size={14} /></button>)}</div></div>
      {selectedCall && <div className="surface span-three"><SectionHead icon={Eye} title="调用详情" meta={selectedCall.id} action={<button className="ghost tiny" onClick={() => setSelectedCall(null)}>关闭</button>} /><div className="call-detail-grid"><div><span>输入范围</span><pre>{JSON.stringify(selectedCall.metadata || selectedCall.input, null, 2)}</pre></div><div><span>Agent 链</span><pre>{JSON.stringify(selectedCall.chain || [], null, 2)}</pre></div><div><span>输出摘要</span><pre>{JSON.stringify(selectedCall.output || {}, null, 2)}</pre></div></div></div>}
      <div className="surface span-three"><SectionHead icon={HardDrive} title="日志查询" meta="受限 LogQL，只读访问 Loki" /><div className="querybar"><input value={logQuery} onChange={(event) => setLogQuery(event.target.value)} /><button className="primary" onClick={queryLogs} disabled={logs.loading}>{logs.loading ? <Loader2 className="spin" size={15} /> : <Search size={15} />}查询</button></div>{logs.error && <div className="inline-error">{logs.error}</div>}{list(logs.data?.streams).length ? <div className="log-view">{list(logs.data.streams).flatMap((stream: any) => list(stream.values).map((value: any[], index: number) => <div key={`${value[0]}-${index}`}><span>{value[0]}</span><code>{value[1]}</code></div>)).slice(0, 120)}</div> : <Empty text="配置 Loki 后可在这里关联检索日志；未连接时不会伪造数据" />}</div>
      <div className="surface span-three"><SectionHead icon={GitBranch} title="链路查询" meta="Tempo / TraceQL backend" /><div className="querybar"><input value={traceService} onChange={(event) => setTraceService(event.target.value)} placeholder="service.name，可留空查看最近链路" /><button className="primary" onClick={queryTraces} disabled={traces.loading}>{traces.loading ? <Loader2 className="spin" size={15} /> : <Search size={15} />}查询</button></div>{traces.error && <div className="inline-error">{traces.error}</div>}{list(traces.data?.traces).length ? <div className="trace-list">{list(traces.data.traces).map((trace: any, index: number) => <div key={trace.traceID || index}><strong>{trace.rootServiceName || trace.serviceName || "trace"}</strong><code>{trace.traceID}</code><span>{trace.durationMs || trace.duration || "-"} ms</span></div>)}</div> : <Empty text="配置 Tempo 并上报 OTLP Trace 后可关联检索调用链" />}</div>
    </section>
  </div>;
}

export function IntegrationsPage() {
  const [state, refresh] = useAsync<any>(() => apiGet("/api/integrations"), []);
  const [cloud] = useAsync<any>(() => apiGet("/api/cloud/adapters"), []);
  const [testing, setTesting] = useState("");
  const [feedback, setFeedback] = useState<{ tone: "ok" | "warn"; text: string } | null>(null);
  const [clusters, refreshClusters] = useAsync<any>(() => apiGet("/api/clusters"), []);
  const [kubeconfig, setKubeconfig] = useState("");
  const [dynamicToken, setDynamicToken] = useState("");
  const [clusterName, setClusterName] = useState("");
  const [contexts, setContexts] = useState<any[]>([]);
  const [context, setContext] = useState("");
  const [editingClusterId, setEditingClusterId] = useState("");
  const [savingCluster, setSavingCluster] = useState(false);
  const groups = [
    ["infrastructure", "基础设施", Network],
    ["observability", "可观测", Activity],
    ["collaboration", "协作通道", MessageSquareText],
    ["ai", "AI 与知识", BrainCircuit],
  ] as const;
  async function testChannel(channel: string) {
    setTesting(channel); setFeedback(null);
    try {
      await apiPost("/api/integrations/notify/test", { channel });
      setFeedback({ tone: "ok", text: `${channel} 测试通知已送达` });
    } catch (error: any) {
      setFeedback({ tone: "warn", text: error.message });
    } finally { setTesting(""); }
  }
  async function inspectKubeconfig(content = kubeconfig) {
    if (!content.trim()) return;
    try {
      const result = await apiPost<any>("/api/clusters/contexts", { kubeconfig: content });
      const values = list(result.contexts);
      setContexts(values);
      setContext(values.find((item: any) => item.current)?.name || values[0]?.name || "");
      setFeedback({ tone: "ok", text: `识别到 ${values.length} 个 context` });
    } catch (error: any) { setFeedback({ tone: "warn", text: error.message }); }
  }
  async function importCluster() {
    setSavingCluster(true); setFeedback(null);
    try {
      const result = editingClusterId && !kubeconfig.trim()
        ? await apiPost<any>(`/api/clusters/${encodeURIComponent(editingClusterId)}/token`, { bearer_token: dynamicToken })
        : await apiPost<any>("/api/clusters", { kubeconfig, name: clusterName, context, cluster_id: editingClusterId, bearer_token: dynamicToken || undefined });
      setKubeconfig(""); setDynamicToken(""); setClusterName(""); setContexts([]); setContext(""); setEditingClusterId("");
      refreshClusters();
      setFeedback({ tone: "ok", text: `${result.name} 已连接并纳管` });
    } catch (error: any) { setFeedback({ tone: "warn", text: error.message }); }
    finally { setSavingCluster(false); }
  }
  async function removeCluster(id: string, name: string) {
    if (!window.confirm(`确定停止纳管集群“${name}”？本地加密凭据与缓存将一并删除。`)) return;
    try { await apiDelete(`/api/clusters/${encodeURIComponent(id)}`); refreshClusters(); setFeedback({ tone: "ok", text: `${name} 已删除` }); }
    catch (error: any) { setFeedback({ tone: "warn", text: error.message }); }
  }
  const cloudAdapters = list(cloud.data?.available || cloud.data?.adapters);
  return <div className="unified-page"><div className="page-commandbar"><div className="quiet-note"><ShieldCheck size={15} />凭据在服务端加密存储，前端不回传敏感内容</div>{feedback && <span className={`channel-feedback ${feedback.tone}`}>{feedback.text}</span>}<button className="ghost" onClick={refresh}><RefreshCcw size={15} />检测</button></div>
    <section className="surface"><SectionHead icon={ServerCog} title="Kubernetes 集群纳管" meta="Rancher 或加密 kubeconfig" />
      <div className="cluster-connect-grid"><div className="cluster-connect-form">{editingClusterId && <div className="quiet-note"><RefreshCcw size={14} />可只填写新 Token 进行原子刷新，或上传完整 kubeconfig；验证成功后才替换原密文。<button className="ghost tiny" onClick={() => { setEditingClusterId(""); setClusterName(""); setDynamicToken(""); }}>取消</button></div>}<label>集群名称<input value={clusterName} onChange={(event) => setClusterName(event.target.value)} placeholder="例如：生产华东集群" /></label><label>上传 kubeconfig<input type="file" accept=".yaml,.yml,.conf,.config" onChange={async (event) => { const file = event.target.files?.[0]; if (!file) return; const text = await file.text(); setKubeconfig(text); await inspectKubeconfig(text); }} /></label><label>或粘贴 kubeconfig<textarea value={kubeconfig} onChange={(event) => setKubeconfig(event.target.value)} rows={8} placeholder="apiVersion: v1…" /></label><label>动态 Bearer Token（可选）<input type="password" autoComplete="off" value={dynamicToken} onChange={(event) => setDynamicToken(event.target.value)} placeholder="含 exec/auth-provider 的 kubeconfig 必须单独填写当前 Token" /><small>Token 仅在服务端加密保存；平台不会执行 kubeconfig 中的外部认证命令。</small></label><div className="querybar"><button className="ghost" onClick={() => inspectKubeconfig()} disabled={!kubeconfig.trim()}>读取 Context</button>{contexts.length > 0 && <select value={context} onChange={(event) => setContext(event.target.value)}>{contexts.map((item: any) => <option key={item.name} value={item.name}>{item.name}{item.current ? "（当前）" : ""}</option>)}</select>}<button className="primary" onClick={importCluster} disabled={savingCluster || (editingClusterId ? (!dynamicToken.trim() && (!kubeconfig.trim() || !context)) : (!kubeconfig.trim() || !context))}>{savingCluster ? <Loader2 className="spin" size={15} /> : <Upload size={15} />}{editingClusterId && !kubeconfig.trim() ? "验证并刷新 Token" : editingClusterId ? "验证并更新" : "验证并纳管"}</button></div></div>
      <div className="managed-cluster-list">{list(clusters.data?.items).length ? list(clusters.data.items).map((item: any) => <div key={item.id}><span className="resource-icon"><ServerCog size={16} /></span><div><strong>{item.name}</strong><p>{item.context_name} · Kubernetes {item.version || "待验证"}</p><small>{item.node_count || 0} nodes · 最近验证 {timeText(item.last_checked_at)}</small>{item.last_error && <small className="inline-error">{item.last_error}</small>}</div><StatusPill status={item.status} /><button className="channel-test" onClick={() => { setEditingClusterId(item.id); setClusterName(item.name); setKubeconfig(""); setDynamicToken(""); setContexts([]); setContext(""); }} title="更新 kubeconfig 或动态 Token"><Upload size={13} /></button><button className="channel-test" onClick={async () => { await apiPost(`/api/clusters/${encodeURIComponent(item.id)}/verify`, {}); refreshClusters(); }} title="重新验证"><RefreshCcw size={13} /></button><button className="channel-test danger" onClick={() => removeCluster(item.id, item.name)} title="删除集群"><Trash2 size={13} /></button></div>) : <Empty text="尚未通过 kubeconfig 纳管集群；Rancher 接入仍可并行使用" />}</div></div>
    </section>
    <div className="integration-groups">{groups.map(([id, title, Icon]) => <section className="surface" key={id}><SectionHead icon={Icon} title={title} /><div className="integration-cards">{list(state.data?.items).filter((item: any) => item.category === id).map((item: any) => <div key={item.id}><span className="resource-icon"><CloudCog size={16} /></span><div><strong>{item.name}</strong><p>{item.capability}</p><small>{item.configuration_hint}</small></div><div className="integration-actions"><StatusPill status={item.status} />{id === "collaboration" && item.status === "configured" && <button className="channel-test" onClick={() => testChannel(item.id)} disabled={testing === item.id} title={`发送 ${item.name} 测试通知`}>{testing === item.id ? <Loader2 className="spin" size={13} /> : <Send size={13} />}</button>}</div></div>)}</div></section>)}</div>
    <section className="surface"><SectionHead icon={GitBranch} title="云资源适配器" meta="Rancher · Generic CSI Storage · Virtualization Platform · Public Cloud" /><div className="capability-grid">{cloudAdapters.length ? cloudAdapters.map((item: any) => <div className="capability-card" key={item.id || item.provider}><span>{item.enabled ? "enabled" : "available"}</span><strong>{item.display_name || item.name || item.provider}</strong><p>{list(item.capabilities).join(" · ") || item.description}</p><small>{item.auth_mode} · {item.inventory_scope}</small></div>) : <Empty text="通过 CLOUD_ADAPTERS_JSON 接入阿里云、通用 CSI 存储、虚拟化平台或其他云适配器" />}</div></section>
    <section className="surface"><SectionHead icon={CheckCircle2} title="能力覆盖" meta="对标 OnGrid 已发布能力，同时保留 Flawless 的差异化能力" /><div className="coverage-table"><div><strong>能力</strong><strong>本系统</strong><strong>说明</strong></div>{list(state.data?.coverage).map((item: any) => <div key={item.capability}><span>{item.capability}</span><StatusPill status={item.status} /><small>{item.detail}</small></div>)}</div></section>
  </div>;
}

type AssistantMessage = {
  role: "user" | "assistant";
  text: string;
  at: number;
  page?: string;
  domain?: "app" | "ops";
  sourceCount?: number;
};

const ASSISTANT_OPS_PATTERN = /pod|k8s|kubernetes|故障|排障|集群|网络|存储|告警|修复|巡检|拓扑|prometheus|cmdb|rancher|namespace|workload|deployment|statefulset/i;

function assistantSuggestions(page: string) {
  if (page.includes("SRE")) return ["这次诊断如何安全执行？", "帮我把回答整理成操作步骤", "如果修复失败下一步做什么？"];
  if (page.includes("巡检")) return ["如何只看新增风险？", "生产模式会检查哪些隐患？", "怎样开启人工确认修复？"];
  if (page.includes("拓扑")) return ["怎么读懂影响范围？", "解释关键路径和放大系数", "Kafka/ELK 数据流在哪里看？"];
  if (page.includes("模型")) return ["怎么接入 OAuth 模型？", "怎么比较模型运维能力？", "如何做影子测评？"];
  if (page.includes("知识")) return ["Runbook 应该怎么沉淀？", "产品使用知识和运维知识区别？", "如何让助手使用这些知识？"];
  return ["当前页面怎么用？", "给我推荐下一步", "遇到异常先看哪里？"];
}

function assistantDomain(question: string, page: string): "app" | "ops" {
  return ASSISTANT_OPS_PATTERN.test(`${page}\n${question}`) ? "ops" : "app";
}

export function AssistantDock({ page }: { page: string }) {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [messages, setMessages] = useState<AssistantMessage[]>(() => {
    try { return JSON.parse(localStorage.getItem("flawless-unified-assistant") || "[]"); } catch { return []; }
  });
  const suggestions = useMemo(() => assistantSuggestions(page), [page]);
  const scroller = useRef<HTMLDivElement | null>(null);
  useEffect(() => { localStorage.setItem("flawless-unified-assistant", JSON.stringify(messages.slice(-40))); requestAnimationFrame(() => { if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight; }); }, [messages]);
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); setOpen(true); }
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, []);
  async function ask(override?: string) {
    const question = (override || input).trim();
    if (!question || loading) return;
    setInput(""); setOpen(true); setLoading(true);
    const domain = assistantDomain(question, page);
    setMessages((current) => [...current, { role: "user", text: question, at: Date.now(), page, domain }]);
    try {
      const data = await apiPost<any>("/api/knowledge/ask", {
        question: `当前页面：${page}\n用户问题：${question}`,
        domain,
        include_principle: /原理|机制|为什么|principle/i.test(question),
      });
      setMessages((current) => [...current, { role: "assistant", text: data.answer || "没有检索到答案。", at: Date.now(), page, domain, sourceCount: list(data.sources).length }]);
    } catch (error: any) {
      setMessages((current) => [...current, { role: "assistant", text: `助手暂时不可用：${error.message}`, at: Date.now(), page, domain }]);
    } finally { setLoading(false); }
  }
  return <>
    <button className="assistant-launcher" onClick={() => setOpen(true)} title="打开 Flawless 助手"><Bot size={19} /><span>助手</span><kbd>⌘K</kbd></button>
    <aside className={`assistant-drawer ${open ? "open" : ""}`} aria-hidden={!open}>
      <header>
        <div><span className="assistant-mark"><Bot size={18} /></span><div><strong>Flawless 助手</strong><small>当前页面：{page}</small></div></div>
        <div className="assistant-header-actions">
          <button onClick={() => setMessages([])} title="清空对话"><RefreshCcw size={15} /></button>
          <button onClick={() => setOpen(false)} title="关闭"><X size={18} /></button>
        </div>
      </header>
      <div className="assistant-context">
        <span><BrainCircuit size={14} />知识库路由</span>
        <strong>{ASSISTANT_OPS_PATTERN.test(page) ? "运维 Runbook" : "产品使用 + 运维 RAG"}</strong>
      </div>
      <div className="assistant-suggestions">
        {suggestions.map((item) => <button key={item} onClick={() => ask(item)} disabled={loading}>{item}</button>)}
      </div>
      <div className="assistant-messages" ref={scroller}>
        {messages.length ? messages.map((item, index) => <div className={`assistant-message ${item.role}`} key={`${item.at}-${index}`}>
          <span>{item.role === "assistant" ? "Flawless" : "你"}{item.page ? ` · ${item.page}` : ""}{item.domain ? ` · ${item.domain === "ops" ? "运维知识" : "产品知识"}` : ""}</span>
          <p>{item.text}</p>
          {item.role === "assistant" && typeof item.sourceCount === "number" && <small>{item.sourceCount} 个知识片段参与回答</small>}
        </div>) : <div className="assistant-welcome"><BrainCircuit size={24} /><strong>需要我帮你怎么用这套系统？</strong><p>我会结合当前页面、产品知识库和运维 Runbook 给出下一步。</p></div>}
        {loading && <div className="assistant-thinking"><i /><i /><i />正在检索知识库</div>}
      </div>
      <footer>
        <textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); ask(); } }} placeholder="问产品用法、运维 Runbook 或当前页面下一步" />
        <button onClick={() => ask()} disabled={loading || !input.trim()}><Send size={16} /></button>
      </footer>
    </aside>
    {open && <button className="assistant-backdrop" onClick={() => setOpen(false)} aria-label="关闭助手遮罩" />}
  </>;
}

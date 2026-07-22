import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Clock3, FileText, Loader2, ShieldCheck, Square, TerminalSquare } from "lucide-react";

import { apiGet, apiPost, asList } from "../lib/api";


export type OpsPlan = {
  id?: string;
  title?: string;
  target?: string;
  summary?: string;
  steps?: any[];
  changes?: any[];
  success_criteria?: any[];
  [key: string]: any;
};


function classNames(...items: Array<string | false | undefined>) {
  return items.filter(Boolean).join(" ");
}

const ACTIVE_STATUSES = new Set(["queued", "running", "awaiting_approval", "resume_pending", "cancelling"]);
const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled", "unresolved", "blocked"]);
const ACTIVE_EVENT_STAGES = new Set([
  "queued", "starting", "attempt", "collecting_evidence", "step_start", "step_waiting",
  "skill_routed", "change_start", "change_waiting", "change_approval_received", "verifying", "replanning", "summarizing", "strategy_switch",
  "continuation_wait", "resume_pending",
]);
const EXECUTION_PHASES = ["采集证据", "根因诊断", "提交变更", "恢复验证"];
const HIGH_RISK_ACTIONS = new Set([
  "apply_manifest", "patch_resource", "delete_resource", "run_shell", "exec_pod", "exec_node",
  "create_workload", "expand_pvc", "create_pvc", "create_pv", "patch_workload_volume",
  "patch_workload_runtime_security",
  "cordon_node", "evict_pod", "uncordon_node", "rollback_workload", "patch_service",
  "create_configmap", "patch_pdb",
]);

function formatTime(value: unknown) {
  if (!value) return "-";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function stageLabel(stage: unknown) {
  const key = String(stage || "");
  const labels: Record<string, string> = {
    apply_manifest: "应用 Kubernetes YAML", patch_resource: "修改 Kubernetes 资源", delete_resource: "删除 Kubernetes 资源",
    run_shell: "执行平台 Shell", exec_pod: "执行 Pod 命令", exec_node: "执行节点命令",
    queued: "排队",
    starting: "启动",
    attempt: "策略尝试",
    release_gate: "风险门禁",
    release_blocked: "门禁阻断",
    collecting_evidence: "采集证据",
    collecting_evidence_done: "证据完成",
    skill_routed: "Skill 动态路由",
    step_waiting: "诊断进行中",
    step_start: "诊断开始",
    step_done: "诊断完成",
    change_waiting: "等待变更回执",
    change_start: "提交变更",
    change_done: "变更回执",
    awaiting_change_approval: "等待逐步确认",
    change_approval_received: "确认已收到",
    change_approved: "步骤已批准",
    stage_timeout: "阶段超时",
    verifying: "恢复验证",
    verification_done: "验证完成",
    replanning: "策略重规划",
    summarizing: "生成结论",
    strategy_switch: "切换策略",
    continuation_wait: "等待持续复检",
    resume_pending: "恢复故障会话",
    recovered: "恢复完成",
    needs_operator: "等待人工",
    execution_failed: "执行失败",
    failed: "异常终止",
    cancelled: "已中断",
    exhausted: "尝试耗尽",
    deduplicated: "重复策略",
  };
  return labels[key] || key || "运行中";
}

function statusTone(status: unknown) {
  const value = String(status || "").toLowerCase();
  if (["completed", "success", "recovered"].includes(value)) return "success";
  if (["failed", "error"].includes(value)) return "danger";
  if (["blocked", "unresolved", "warning", "cancelled", "awaiting_approval"].includes(value)) return "warning";
  if (["running", "queued", "resume_pending", "cancelling"].includes(value)) return "running";
  return "";
}

function compactJson(value: unknown, limit = 1600) {
  if (value === undefined || value === null || value === "") return "";
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return text.length > limit ? `${text.slice(0, limit)}\n...` : text;
}

function eventIcon(event: any, active: boolean) {
  const tone = event?.level || statusTone(event?.change_status || event?.step_status || event?.stage);
  if (active && ACTIVE_EVENT_STAGES.has(String(event?.stage))) return <Loader2 className="spin" size={12} />;
  if (tone === "error" || tone === "danger") return <AlertTriangle size={12} />;
  if (tone === "warning") return <AlertTriangle size={12} />;
  if (tone === "success") return <CheckCircle2 size={12} />;
  return <Clock3 size={12} />;
}

function phaseIndex(stage: unknown) {
  const value = String(stage || "");
  if (["queued", "starting", "attempt", "release_gate", "collecting_evidence", "collecting_evidence_done"].includes(value)) return 0;
  if (["skill_routed", "step_start", "step_waiting", "step_done", "replanning", "strategy_switch", "continuation_wait", "resume_pending"].includes(value)) return 1;
  if (["awaiting_change_approval", "change_approval_received", "change_approved", "change_start", "change_waiting", "change_done"].includes(value)) return 2;
  return 3;
}

function permissionGuidance(value: any) {
  if (value?.permission_guidance) return value.permission_guidance;
  if (value?.result?.permission_guidance) return value.result.permission_guidance;
  if (value?.operator_steps?.summary) return value.operator_steps;
  return null;
}

function changeLabel(change: any) {
  const labels: Record<string, string> = {
    create_pvc: "创建缺失 PVC", create_pv: "创建静态 PV", expand_pvc: "扩容 PVC",
    patch_workload_volume: "修正存储卷引用", patch_workload: "修改 Workload 配置",
    recreate_pod: "重建异常 Pod", restart: "滚动重启", cordon_node: "隔离节点",
    patch_workload_runtime_security: "修复运行时权限",
    patch_service_account: "修正 ServiceAccount", create_configmap: "恢复 ConfigMap",
    db_restart_instance: "重启数据库实例", db_kill_session: "终止数据库会话",
    db_expand_storage: "扩容数据库存储", db_failover: "数据库主备切换",
    db_apply_parameter: "调整数据库参数", vm_restart_service: "重启主机服务",
    vm_reboot: "重启虚拟机", vm_expand_disk: "扩容虚拟机磁盘",
    vm_run_approved_script: "执行批准主机脚本", vm_snapshot: "创建虚拟机快照",
    middleware_rebalance: "中间件重平衡", storage_expand_volume: "扩容企业存储卷",
    infra_run_approved_action: "基础设施批准动作",
  };
  return labels[String(change?.type || "")] || change?.type || "基础设施变更";
}

function PermissionCard({ guidance }: { guidance: any }) {
  if (!guidance) return null;
  return <div className="ops-permission-card">
    <b>权限阻断，不是诊断卡死</b>
    <p>{guidance.summary || "当前身份缺少提交该 Kubernetes 变更所需的权限。"}</p>
    {asList(guidance.do_this).map((item: any, index: number) => <span key={`${item}-${index}`}><i>{index + 1}</i>{String(item)}</span>)}
    {asList(guidance.minimal_resources).length > 0 && <small>最小资源范围：{asList(guidance.minimal_resources).join("、")}</small>}
  </div>;
}

function progressPercent(job: any) {
  const status = String(job?.status || "");
  if (["completed"].includes(status)) return 100;
  if (["failed", "cancelled", "unresolved", "blocked"].includes(status)) return 100;
  const events = asList(job?.events);
  const weighted = events.reduce((total, event) => {
    const stage = String(event?.stage || "");
    if (stage.endsWith("_done") || stage === "recovered") return total + 14;
    if (stage === "change_start" || stage === "step_start" || stage === "verifying") return total + 8;
    return total + 4;
  }, 4);
  return Math.max(8, Math.min(96, weighted));
}

function renderEventDetails(event: any) {
  const evidence = event?.evidence_summary;
  const stepResult = event?.step_result;
  const change = event?.change;
  const changeResult = event?.change_result;
  const verification = event?.verification;
  const releaseGate = event?.release_gate;
  const guidance = permissionGuidance(changeResult);
  const logs = asList(stepResult?.logs_tail);
  return (
    <>
      {evidence && (
        <div className="ops-event-chips">
          <span>logs {evidence.logs ?? 0}</span><span>events {evidence.events ?? 0}</span><span>svc {evidence.services ?? 0}</span><span>storage {evidence.storage ?? 0}</span>{evidence.node && <span>node {evidence.node}</span>}
          {evidence.error && <span className="hot">证据异常</span>}
        </div>
      )}
      {releaseGate && (
        <div className="ops-event-chips">
          <span>风险 {releaseGate.risk_score ?? releaseGate.risk_level ?? "-"}</span>
          <span>{releaseGate.allowed === false ? "门禁拦截" : "门禁通过"}</span>
        </div>
      )}
      {stepResult && (
        <div className="ops-event-details">
          <span>{stepResult.status || "completed"} · {stepResult.finished_at ? formatTime(stepResult.finished_at) : "刚刚"}</span>
          {asList(stepResult.artifacts).length > 0 && <small>证据：{asList(stepResult.artifacts).join(" / ")}</small>}
          {logs.length > 0 && <pre>{logs.join("\n")}</pre>}
        </div>
      )}
      {change && (
        <div className="ops-event-details">
          <span>{change.type || "change"} · {change.target || change.namespace || "-"}</span>
          {change.patch && <pre>{compactJson(change.patch, 900)}</pre>}
          {change.manifest && <pre>{compactJson(change.manifest, 1400)}</pre>}
          {change.command && <pre>{compactJson(change.command, 1400)}</pre>}
        </div>
      )}
      {changeResult && (
        <div className="ops-event-details">
          <span>{changeResult.status || "completed"}</span>
          {changeResult.error && <small className="danger-text">{changeResult.error}</small>}
          <PermissionCard guidance={guidance} />
          {changeResult.result_preview && <details><summary>查看原始 API 回执</summary><pre>{changeResult.result_preview}</pre></details>}
        </div>
      )}
      {verification && (
        <div className="ops-event-details">
          <span>{verification.status || "verified"} · {verification.recovered === true ? "已恢复" : verification.recovered === false ? "未恢复" : "尚未证明恢复"}</span>
          <small>{verification.message || verification.proof || "验证完成"}</small>
        </div>
      )}
      {event?.alternative_plan_count !== undefined && <div className="ops-event-chips"><span>候选策略 {event.alternative_plan_count}</span></div>}
      {event?.waiting_on && (
        <div className="ops-event-chips">
          <span>等待：{event.waiting_on}</span>
          <span>已用 {Math.round(Number(event.elapsed_seconds || 0))} 秒</span>
          <span>剩余 {Math.max(0, Math.round(Number(event.remaining_seconds || 0)))} 秒后熔断</span>
        </div>
      )}
      {event?.timed_out_stage && <div className="ops-event-chips"><span className="danger-text">已终止慢调用：{event.timed_out_stage}</span></div>}
    </>
  );
}

export function OpsJobProgress({
  job,
  onCancel,
  compact = false,
}: {
  job: any;
  onCancel?: () => void | Promise<void>;
  compact?: boolean;
}) {
  const [liveJob, setLiveJob] = useState(job);
  const [pollFailures, setPollFailures] = useState(0);
  const [followupJob, setFollowupJob] = useState<any>(null);
  const [followupError, setFollowupError] = useState("");
  const [followupApprovals, setFollowupApprovals] = useState<Record<string, boolean>>({});
  const [stepApprovalChecked, setStepApprovalChecked] = useState(false);
  const [stepApprovalBusy, setStepApprovalBusy] = useState(false);
  const [stepApprovalError, setStepApprovalError] = useState("");
  const eventListRef = useRef<HTMLDivElement | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const currentJob = liveJob || job;
  const active = ACTIVE_STATUSES.has(String(currentJob?.status || ""));
  const events = asList(currentJob?.events);
  const result = currentJob?.result || {};
  const continuation = result?.continuation_context || {};
  const lineageAttempts = asList(continuation?.attempts);
  const steps = asList(result.steps);
  const changes = asList(result.results);
  const nextSteps = useMemo(
    () => asList(result?.next_steps || result?.verification?.next_steps || result?.ai_summary?.next_steps)
      .map((item: any) => String(item || "").trim())
      .filter(Boolean)
      .slice(0, 6),
    [result?.next_steps, result?.verification?.next_steps, result?.ai_summary?.next_steps],
  );
	  const followups = useMemo(() => {
	    const seen = new Set<string>();
	    return [...asList(result?.alternative_plans), ...asList(result?.ai_summary?.followup_plans)].filter((plan: any) => {
	      const fingerprint = JSON.stringify({ target: plan?.target, changes: plan?.changes, title: plan?.title });
	      const hasNextStep = asList(plan?.changes).length || asList(plan?.steps).length || asList(plan?.operator_steps).length;
	      if (!hasNextStep || seen.has(fingerprint)) return false;
	      seen.add(fingerprint);
	      return true;
	    }).slice(0, 3);
  }, [result?.alternative_plans, result?.ai_summary?.followup_plans]);
  const latestEvents = events.slice(compact ? -10 : -24);
  const percent = progressPercent(currentJob);
  const statusClass = statusTone(currentJob?.status);
  const currentPhase = phaseIndex(currentJob?.stage || latestEvents.at(-1)?.stage);
  const pendingApproval = currentJob?.pending_approval;

  useEffect(() => {
    setLiveJob(job);
    setPollFailures(0);
  }, [job?.id]);

  useEffect(() => {
    setStepApprovalChecked(false);
    setStepApprovalError("");
  }, [pendingApproval?.approval_id, pendingApproval?.change_fingerprint]);

  useEffect(() => {
    const list = eventListRef.current;
    if (list) list.scrollTo({ top: list.scrollHeight, behavior: active ? "smooth" : "auto" });
    bottomRef.current?.scrollIntoView({ block: "nearest", behavior: active ? "smooth" : "auto" });
  }, [latestEvents.length, currentJob?.stage, currentJob?.status, pendingApproval?.change_index, active]);

  useEffect(() => {
    if (!active || !currentJob?.id) return;
    const delay = Math.min(15000, 1200 * (2 ** Math.min(pollFailures, 3)));
    const timer = window.setTimeout(async () => {
      try {
        setLiveJob(await apiGet(`/api/ops/jobs/${encodeURIComponent(currentJob.id)}`));
        setPollFailures(0);
      } catch (error: any) {
        setPollFailures((count) => {
          const next = count + 1;
          if (next === 3) {
            setLiveJob((current: any) => ({
              ...current,
              message: `状态接口暂时不可达，仍会继续重试：${error.message}`,
              updated_at: new Date().toISOString(),
              events: [...asList(current?.events), {
                timestamp: new Date().toISOString(),
                stage: "poll_retry",
                level: "warning",
                message: `任务状态接口连续读取失败，前端保持故障会话并退避重试：${error.message}`,
              }],
            }));
          }
          return next;
        });
      }
    }, delay);
    return () => window.clearTimeout(timer);
  }, [active, currentJob?.id, currentJob?.updated_at, currentJob?.events?.length, pollFailures]);

  async function runFollowup(plan: any, highRisk: boolean, approvalKey: string) {
    setFollowupError("");
    const followupChanges = asList(plan.changes);
    const enrichedPlan = {
      ...plan,
      cluster: plan.cluster || currentJob?.cluster,
      cluster_id: plan.cluster_id || currentJob?.cluster_id,
      namespace: plan.namespace || currentJob?.namespace,
      target: plan.target || currentJob?.target,
      source: plan.source || currentJob?.source,
      high_risk_confirmed: highRisk ? Boolean(followupApprovals[approvalKey]) : Boolean(plan.high_risk_confirmed),
      operator_force_execute: true,
      stepwise_confirmation: followupChanges.length > 0,
      operator_override_reason: highRisk ? "操作员已核对替代策略，并明确确认即使高风险也执行" : "操作员在执行结果中确认替代策略",
      continuation_context: plan.continuation_context || result.continuation_context || {},
    };
    try {
      setFollowupJob(await apiPost("/api/ops/jobs", {
        plan: enrichedPlan,
        confirm: true,
        autonomous: false,
        high_risk_confirmed: Boolean(enrichedPlan.high_risk_confirmed || highRisk),
        operator_force_execute: true,
        allow_high_risk_after_confirmation: true,
        stepwise_confirmation: followupChanges.length > 0,
        operator_override_reason: enrichedPlan.operator_override_reason || "操作员确认执行替代策略",
      }));
    } catch (error: any) {
      setFollowupError(error.message);
    }
  }

  async function approveStep() {
    if (!currentJob?.id || !pendingApproval?.change_index || !stepApprovalChecked) return;
    setStepApprovalBusy(true);
    setStepApprovalError("");
    try {
      setLiveJob(await apiPost(`/api/ops/jobs/${encodeURIComponent(currentJob.id)}/approve-step`, {
        change_index: pendingApproval.change_index,
        approval_id: pendingApproval.approval_id,
        change_fingerprint: pendingApproval.change_fingerprint,
        confirm: true,
        comment: "操作员已核对本步骤目标、差异、风险与回滚方式",
      }));
    } catch (error: any) {
      setStepApprovalError(error.message);
    } finally {
      setStepApprovalBusy(false);
    }
  }

  return (
    <div className={classNames("ops-job", compact && "compact")}>
      <div className="ops-job-status">
        <span className={classNames("job-dot", active && "active", statusClass)} />
        <div>
          <strong>{currentJob?.message || stageLabel(currentJob?.stage)}</strong>
          <small>{currentJob?.status || "running"} · {stageLabel(currentJob?.stage)} · 故障链第 {currentJob?.lineage_attempt || currentJob?.attempt || 0} 轮 · {currentJob?.continuous_until_recovered ? "持续到恢复验证成功或人工中断" : `本任务上限 ${currentJob?.max_attempts || 1} 轮`}</small>
        </div>
        {active && onCancel && <button className="ghost tiny" onClick={onCancel}><Square size={13} />中断</button>}
      </div>
      <div className="ops-live-progress" aria-label={`执行进度 ${percent}%`}>
        <i style={{ width: `${percent}%` }} />
      </div>
      <div className="ops-phase-track" aria-label="运维执行阶段">
        {EXECUTION_PHASES.map((label, index) => {
          const finished = index < currentPhase || (TERMINAL_STATUSES.has(String(currentJob?.status)) && index <= currentPhase);
          return <div className={classNames(finished && "done", index === currentPhase && active && "active")} key={label}>
            <i>{finished ? <CheckCircle2 size={11} /> : index + 1}</i><span>{label}</span>
          </div>;
        })}
      </div>
      {pendingApproval && currentJob?.status === "awaiting_approval" && <div className="ops-step-approval-card">
        <header><span>故障链第 {pendingApproval.lineage_attempt || currentJob?.lineage_attempt || 1} 轮 · 第 {pendingApproval.change_index}/{pendingApproval.changes_total} 步</span><strong>{changeLabel({ type: pendingApproval.action })}</strong><em>{pendingApproval.risk || "medium"}</em></header>
        <div className="ops-step-approval-target"><span>将要修改</span><b>{pendingApproval.target}</b></div>
        <p>{pendingApproval.reason || "等待操作员确认本步骤。"}</p>
        <div className="ops-step-rollback"><span>回滚方式</span><b>{pendingApproval.rollback || "恢复变更前配置"}</b></div>
        {(pendingApproval.patch || pendingApproval.manifest) && <details open><summary>查看本步骤配置差异</summary><pre>{compactJson(pendingApproval.patch || pendingApproval.manifest, 5000)}</pre></details>}
        {pendingApproval.command && <details open><summary>待执行完整命令</summary><pre>{compactJson(pendingApproval.command, 20000)}</pre><small>目标：{pendingApproval.node_name ? `Node/${pendingApproval.node_name}` : pendingApproval.pod_name ? `Pod/${pendingApproval.pod_name}${pendingApproval.container_name ? `/${pendingApproval.container_name}` : ""}` : "Flawless runtime"} · 超时 {pendingApproval.timeout_seconds || 120}s</small></details>}
        <small>审批凭据 {String(pendingApproval.approval_id || "").slice(-12)} · 变更指纹 {pendingApproval.change_fingerprint}</small>
        <label><input type="checkbox" checked={stepApprovalChecked} onChange={(event) => setStepApprovalChecked(event.target.checked)} />我已核对本步骤的目标、差异、风险和回滚方式</label>
        <button className="primary" onClick={approveStep} disabled={!stepApprovalChecked || stepApprovalBusy}><ShieldCheck size={14} />{stepApprovalBusy ? "正在提交确认..." : `确认执行第 ${pendingApproval.change_index} 步`}</button>
        {stepApprovalError && <div className="error-box">{stepApprovalError}</div>}
      </div>}
      <div className="ops-live-grid">
        <div><span>目标</span><strong>{currentJob?.target || currentJob?.id || "-"}</strong></div>
        <div><span>集群/命名空间</span><strong>{currentJob?.cluster || "-"} / {currentJob?.namespace || "-"}</strong></div>
        <div><span>操作员</span><strong>{currentJob?.operator || "system"}</strong></div>
        <div><span>更新时间</span><strong>{formatTime(currentJob?.updated_at)}</strong></div>
      </div>
      {!active && result?.verification && result.verification.recovered !== true && <div className="ops-blocked-guidance">
        <b>故障闭环仍然打开</b>
        <p>当前没有取得明确恢复证据。系统不会把未知状态当成功；请继续执行下方差异化方案或外部处理步骤，直到恢复验证明确通过。</p>
      </div>}
      {lineageAttempts.length > 0 && <details className="ops-lineage-history">
        <summary>同一故障链已执行 {lineageAttempts.length} 轮，查看失败方案与换路依据</summary>
        <div>{lineageAttempts.map((attempt: any, index: number) => <span key={`${attempt.fingerprint || attempt.strategy}-${index}`}>
          <i>{attempt.attempt || index + 1}</i>
          <b>{attempt.strategy || `策略 ${index + 1}`}</b>
          <em className={statusTone(attempt.recovered === true ? "completed" : attempt.status)}>{attempt.recovered === true ? "已恢复" : attempt.status || "未恢复"}</em>
          <small>{attempt.outcome || "本轮没有取得恢复证据。"}</small>
          {asList(attempt.actions).length > 0 && <code>{asList(attempt.actions).join(" · ")}</code>}
        </span>)}</div>
      </details>}
      <div className="ops-event-list" ref={eventListRef}>
        {latestEvents.map((event: any, index: number) => {
          const isCurrent = active && index === latestEvents.length - 1;
          return (
            <div className={classNames("ops-event", event.level, isCurrent && "current")} key={`${event.timestamp}-${event.stage}-${index}`}>
              <span className="ops-event-dot">{eventIcon(event, isCurrent)}</span>
              <div>
                <header><strong>{stageLabel(event.stage)}</strong><small>{formatTime(event.timestamp)}</small></header>
                <p>{event.message || "-"}</p>
                {renderEventDetails(event)}
              </div>
            </div>
          );
        })}
        <div ref={bottomRef} />
      </div>
      {(steps.length > 0 || changes.length > 0 || result.verification) && (
        <div className="ops-result-grid">
          {steps.length > 0 && (
            <section className="ops-result-section">
              <h4><TerminalSquare size={13} />诊断证据</h4>
              {steps.map((step: any, index: number) => (
                <article key={`${step.id || step.title}-${index}`}>
                  <b>{step.title || step.name || `步骤 ${index + 1}`}</b><span className={statusTone(step.status)}>{step.status || "completed"}</span>
                  {asList(step.logs).length > 0 && <pre>{asList(step.logs).slice(-8).join("\n")}</pre>}
                </article>
              ))}
            </section>
          )}
          {changes.length > 0 && (
            <section className="ops-result-section">
              <h4><FileText size={13} />变更回执</h4>
              {changes.map((item: any, index: number) => (
                <article key={`${item.status || "change"}-${index}`}>
                  <b>{changeLabel(item.change)} · {item.change?.workload_type || item.change?.kind || "resource"}/{item.change?.workload_name || item.change?.name || item.change?.pvc_name || "-"}</b>
                  <span className={statusTone(item.status)}>{item.status || "completed"}</span>
                  <p>{item.change?.reason || (item.status === "failed" ? "Kubernetes API 未接受该变更。" : "Kubernetes API 已返回变更回执。")}</p>
                  <PermissionCard guidance={permissionGuidance(item)} />
                  {item.result && <details><summary>查看原始 API 回执</summary><pre>{compactJson(item.result, 1100)}</pre></details>}
                </article>
              ))}
            </section>
          )}
          {result.verification && (
            <section className="ops-result-section ops-verification">
              <h4><CheckCircle2 size={13} />恢复验证</h4>
              <article>
                <b>{result.verification.status || "verified"}</b>
                <span className={result.verification.recovered === true ? "success" : "warning"}>{result.verification.recovered === true ? "已恢复" : result.verification.recovered === false ? "未恢复" : "尚未证明"}</span>
                <p>{result.verification.message || result.verification.proof || "验证完成"}</p>
              </article>
            </section>
          )}
        </div>
      )}
      {result?.ai_summary?.content && <div className="ops-conclusion"><b>AI 执行结论</b><p>{result.ai_summary.content}</p></div>}
      {nextSteps.length > 0 && (
        <div className="ops-next-steps">
          <b>下一步应该怎么做</b>
          {nextSteps.map((item, index) => <span key={`${item}-${index}`}><i>{index + 1}</i>{item}</span>)}
        </div>
      )}
      {(result?.blocked_reason || asList(result?.operator_steps).length > 0) && (
        <div className="ops-blocked-guidance">
          <b>{result.executed ? "为什么本轮还没恢复" : "为什么本轮没有执行变更"}</b>
          <p>{result.blocked_reason || result?.verification?.blocked_reason || "没有形成满足安全门禁的变更证据。"}</p>
          {asList(result.operator_steps || result?.verification?.operator_steps).map((item: any, index: number) => <span key={`${item}-${index}`}><i>{index + 1}</i>{String(item)}</span>)}
        </div>
      )}
      {followups.length > 0 && !followupJob && (
        <div className="ops-followups">
          <b>可确认的下一步策略</b>
	          {followups.map((plan: any, index: number) => {
	            const planChanges = asList(plan.changes);
	            const highRisk = planChanges.some((change: any) => change.risk === "high" || change.auto_allowed === false || change.requires_high_risk_confirmation);
	            const risk = planChanges.length ? highRisk ? "高风险" : "受控" : "只读诊断";
	            const approvalKey = String(plan.id || plan.title || index);
	            const previousAttempt = plan.previous_attempt || plan.continuation_context?.last_failure || continuation?.last_failure;
	            return (
	              <article key={`${plan.title || "followup"}-${index}`}>
	                <div><strong>{plan.title || `替代策略 ${index + 1}`}</strong><span>{risk} · {planChanges.length} 项变更</span></div>
	                <p>{plan.summary || plan.reason || "基于上一轮证据生成的差异化修复策略。"}</p>
	                {previousAttempt && <div className="ops-followup-difference"><b>为什么换方案</b><span>上一轮：{previousAttempt.strategy || "上一策略"}</span><small>{previousAttempt.outcome || "恢复验证未通过；本轮必须更换动作、目标参数或根因假设。"}</small></div>}
	                {planChanges.length > 0 ? <div className="ops-followup-change-list">{planChanges.map((change: any, changeIndex: number) => <span key={`${change.type}-${changeIndex}`}><i>{changeIndex + 1}</i><b>{changeLabel(change)}</b><em>{change.workload_type || change.kind || "resource"}/{change.workload_name || change.name || change.pvc_name || plan.target || "-"}</em><small>{change.reason || "执行后按恢复判据验证。"}</small></span>)}</div> : <div className="ops-followup-verification"><b>下一步诊断</b>{asList(plan.steps || plan.operator_steps).slice(0, 4).map((step: any, stepIndex: number) => <span key={`${step?.title || step}-${stepIndex}`}><i>{stepIndex + 1}</i>{String(step?.description || step?.title || step)}</span>)}</div>}
	                {asList(plan.verification_plan).length > 0 && <div className="ops-followup-verification"><b>执行后验证</b>{asList(plan.verification_plan).slice(0, 4).map((item: any, verifyIndex: number) => <span key={`${item}-${verifyIndex}`}><i>{verifyIndex + 1}</i>{String(item)}</span>)}</div>}
	                {highRisk && <label className="toggle danger-toggle"><input type="checkbox" checked={Boolean(followupApprovals[approvalKey])} onChange={(event) => setFollowupApprovals((current) => ({ ...current, [approvalKey]: event.target.checked }))} />即使高风险也确认执行</label>}
	                <button className="primary" onClick={() => runFollowup(plan, highRisk, approvalKey)} disabled={highRisk && !followupApprovals[approvalKey]}><ShieldCheck size={13} />{planChanges.length ? "确认并执行" : "开始下一轮诊断"}</button>
	              </article>
	            );
	          })}
        </div>
      )}
      {followupJob && <OpsJobProgress job={followupJob} compact={compact} />}
      {followupError && <div className="error-box">{followupError}</div>}
    </div>
  );
}


/**
 * Shared human-in-the-loop operation surface used by chat and inspection.
 * It only submits the normalized OpsPlan contract and never constructs a
 * Kubernetes command in the browser.
 */
export function OpsPlanPanel({ plan, autonomous = false }: { plan: OpsPlan; autonomous?: boolean }) {
  const [reviewed, setReviewed] = useState(false);
  const [forceApproved, setForceApproved] = useState(false);
  const [job, setJob] = useState<any>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [serverRequiresHighRisk, setServerRequiresHighRisk] = useState(false);
  const active = job && ACTIVE_STATUSES.has(job.status);
  const changes = useMemo(() => asList(plan?.changes), [plan]);
  const hypotheses = useMemo(() => asList(plan?.root_cause_hypotheses).slice(0, 3), [plan]);
  const operatorSkills = useMemo(() => asList(plan?.operator_skills), [plan]);
  const requiresHighRisk = useMemo(
    () => serverRequiresHighRisk || Boolean(plan?.requires_high_risk_confirmation) || changes.some((change: any) => change.risk === "high" || change.auto_allowed === false || change.requires_high_risk_confirmation || HIGH_RISK_ACTIONS.has(String(change.type || change.action || ""))),
    [changes, plan?.requires_high_risk_confirmation, serverRequiresHighRisk],
  );
  const evidenceReason = plan?.evidence_gap || plan?.reason || plan?.message || "当前缺少能证明某个 Kubernetes 变更一定优于其他方案的直接证据。";
  const riskPreview = changes.length
    ? `${changes.length} 项变更；最高风险 ${requiresHighRisk ? "high，需要人工二次确认" : "medium/low，可受控执行"}`
    : "本轮不直接修改集群，会先采集日志、Events、Workload、Service、存储和节点证据，再重新规划。";

  async function execute() {
    setError("");
    if (!reviewed) {
      setError("请先完成第 1 步：核对目标、诊断步骤和拟变更内容。");
      return;
    }
    if (requiresHighRisk && !forceApproved) {
      setError("该计划包含高风险动作。请完成第 2 步，勾选红色的“即使高风险也确认执行”。");
      return;
    }
    setSubmitting(true);
    const executionPlan = {
      ...plan,
      stepwise_confirmation: changes.length > 0,
      high_risk_confirmed: requiresHighRisk ? forceApproved : Boolean(plan?.high_risk_confirmed),
      operator_force_execute: true,
      operator_override_reason: requiresHighRisk && forceApproved ? "操作员已阅读风险预览并确认执行" : plan?.operator_override_reason,
    };
    try {
      setJob(await apiPost("/api/ops/jobs", {
        plan: executionPlan,
        confirm: true,
        autonomous,
        high_risk_confirmed: executionPlan.high_risk_confirmed,
        operator_force_execute: executionPlan.operator_force_execute,
        allow_high_risk_after_confirmation: Boolean(requiresHighRisk && forceApproved),
        operator_override_reason: executionPlan.operator_override_reason || "",
        stepwise_confirmation: executionPlan.stepwise_confirmation,
      }));
    }
    catch (executeError: any) {
      const message = executeError.message || "提交运维任务失败";
      if (/高风险|二次确认|high.risk/i.test(message)) {
        setServerRequiresHighRisk(true);
        setForceApproved(false);
        setError("后端风险门禁已识别出高风险动作。请在下方完成红色的第 2 步确认，再点击“二次确认并执行变更”。");
      } else setError(message);
    } finally { setSubmitting(false); }
  }

  async function cancel() {
    if (!job?.id) return;
    try { setJob(await apiPost(`/api/ops/jobs/${encodeURIComponent(job.id)}/cancel`, {})); }
    catch (cancelError: any) { setError(cancelError.message); }
  }

  return <div className="ops-plan-card">
    <div className="ops-plan-heading"><div><span>受控运维计划</span><strong>{plan.title || plan.target}</strong>{plan.planning_engine && <small>{plan.step_source === "llm_evidence_expert" ? "AI 专家动态路径" : "确定性 Runbook 兜底"} · {plan.planning_engine}</small>}</div><span className={classNames("severity", changes.length ? "hot" : "")}>{changes.length ? `${changes.length} 项变更` : "只读诊断"}</span></div>
    <p className="ops-plan-summary">{plan.summary}</p>
    {plan.preview_mode === "live_evidence_ai" && <div className="ops-live-preview-proof">
      <div><span>预演来源</span><strong>实时证据 + AI 动态规划 + Skill 记忆</strong></div>
      <div><span>证据覆盖</span><strong>{Number(plan.evidence_summary?.events || 0)} Events · {Number(plan.evidence_summary?.log_streams || 0)} 日志流 · {Number(plan.evidence_summary?.storage_objects || 0)} 存储对象</strong></div>
      <div><span>首要根因</span><strong>{asList(plan.root_cause_hypotheses)[0]?.title || plan.planning?.selected_runbook || "仍在收敛证据"}</strong></div>
      <div><span>目标锁</span><strong>{plan.target} · 不允许跨对象变更</strong></div>
    </div>}
    <div className={classNames("ops-risk-note", requiresHighRisk && "hot")}>
      <b>{changes.length ? "算法变更预览" : "为什么暂不直接变更"}</b>
      <span>{changes.length ? `将修改 ${changes.map((change: any) => `${change.workload_type || change.kind || "resource"}/${change.workload_name || change.name || plan.target}`).join("、")}。${riskPreview}。执行后会继续验证 Ready、重启次数、Events 和错误率。` : `${evidenceReason} 原理：SRE 门禁要求“根因证据 -> 最小变更 -> 可回滚 -> 可验证”闭环；证据不足时直接改配置会扩大故障半径，所以先执行深度诊断并让系统重规划。`}</span>
    </div>
    {operatorSkills.length > 0 && <div className="ops-skill-strip">
      <b>匹配到的运维 Skill</b>
      {operatorSkills.map((skill: any) => <span key={skill.id}><strong>{skill.name}</strong><small>{Math.round(Number(skill.confidence || 0) * 100)}% · {skill.category} · {skill.risk} · {skill.execution_authorized ? "所需证据已齐，可进入审批" : asList(skill.evidence_missing).length ? `仍缺 ${asList(skill.evidence_missing).join("、")}` : "仅诊断，未发布执行权限"}</small></span>)}
    </div>}
    {hypotheses.length > 0 && <div className="ops-decision-basis">
      <b>可审计决策依据</b>
      <small>展示证据、置信度和动作依据，不展示或伪造模型内部思维链。</small>
      {hypotheses.map((hypothesis: any, index: number) => <div key={`${hypothesis.id || hypothesis.title || "hypothesis"}-${index}`}>
        <i>{index + 1}</i><span><strong>{hypothesis.title || hypothesis.root_cause || hypothesis.name || "候选根因"}</strong><small>{hypothesis.reason || asList(hypothesis.matched_evidence).join(" · ") || "由当前日志、Events 和资源状态共同支持。"}</small></span><em>{Math.round(Number(hypothesis.confidence || 0) * 100)}%</em>
      </div>)}
    </div>}
    <div className="ops-plan-columns">
      <div><b>执行流程</b>{asList(plan.steps).map((step: any, index: number) => <div className="ops-plan-step" key={`${step.id || "step"}-${index}`}><i>{index + 1}</i><span><strong>{step.title || step.name || step}</strong><small>{step.description || step.detail || "收集证据并记录结果"}</small>{step.probe && <em>证据探针 · {step.probe}</em>}{step.decision_rule && <small className="ops-step-decision">判断：{step.decision_rule}</small>}</span></div>)}</div>
      <div><b>拟变更内容</b>{changes.length ? changes.map((change: any, index: number) => <div className="change-preview" key={`${change.type}-${index}`}><strong>{changeLabel(change)}</strong><span>{change.node_name ? `Node/${change.node_name}` : change.pod_name ? `Pod/${change.pod_name}` : `${change.workload_type || change.kind || "resource"}/${change.workload_name || change.name || plan.target}`}</span><code>{typeof change.command === "string" ? change.command : JSON.stringify(change.patch || change.manifest || change.value || change.storage || change.replicas || {}, null, 2)}</code>{change.skill_id && <small>动态 Skill：{change.skill_id}</small>}</div>) : <div className="quiet-empty">本轮先采集日志、事件和配置证据，不修改集群。</div>}</div>
    </div>
    {asList(plan.success_criteria).length > 0 && <div className="success-criteria"><b>恢复判据</b>{asList(plan.success_criteria).map((item: any) => <span key={String(item)}><CheckCircle2 size={13} />{typeof item === "string" ? item : item.description || item.name}</span>)}</div>}
    {!job && <div className="ops-approval">
      <div className="ops-approval-steps">
        <label className={classNames("ops-approval-step", reviewed && "checked")}><i>1</i><input type="checkbox" checked={reviewed} onChange={(event) => { setReviewed(event.target.checked); setError(""); }} /><span><b>确认目标与变更</b><small>我已核对 {plan.target || "目标对象"}、执行步骤、配置差异和恢复判据。</small></span></label>
        {requiresHighRisk && <label className={classNames("ops-approval-step high-risk", forceApproved && "checked")}><i>2</i><input type="checkbox" checked={forceApproved} onChange={(event) => { setForceApproved(event.target.checked); setError(""); }} /><span><b>高风险二次确认</b><small>我理解该动作可能触发滚动、存储或流量影响，即使高风险也确认执行。</small></span></label>}
      </div>
      {changes.length > 0 && <label className="ops-stepwise-option"><input type="checkbox" checked readOnly /><span><b>逐步确认模式（强制）</b><small>只读诊断自动完成；每项真实 Kubernetes 写操作提交前都会暂停，逐项展示目标、差异、风险、指纹和回滚方式，由我确认后继续。</small></span></label>}
      {error && <div className="ops-submit-error"><AlertTriangle size={14} /><span>{error}</span></div>}
      <button className="primary ops-execute-button" onClick={execute} disabled={submitting}><ShieldCheck size={15} />{submitting ? "正在提交运维任务..." : changes.length ? requiresHighRisk ? "二次确认并执行变更" : "确认执行变更" : "开始深度诊断并重规划"}</button>
    </div>}
    {job && <OpsJobProgress job={job} onCancel={active ? cancel : undefined} />}
    {job && error && <div className="error-box">{error}</div>}
  </div>;
}

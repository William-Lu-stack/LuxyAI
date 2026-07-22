export type ApiState<T> = {
  data?: T;
  loading: boolean;
  error?: string;
};

type CacheEntry = {
  expiresAt: number;
  data: unknown;
};

const getCache = new Map<string, CacheEntry>();
const pendingGets = new Map<string, Promise<unknown>>();
const ADMIN_AUTH_KEY = "flawless-admin-basic";

export function adminAuthHeaders(extra: Record<string, string> = {}) {
  const token = window.sessionStorage.getItem(ADMIN_AUTH_KEY) || "";
  return token ? { ...extra, Authorization: `Basic ${token}` } : extra;
}

export function setAdminCredentials(username: string, password: string) {
  window.sessionStorage.setItem(ADMIN_AUTH_KEY, window.btoa(`${username}:${password}`));
  invalidateApiCache();
}

export function clearAdminCredentials() {
  window.sessionStorage.removeItem(ADMIN_AUTH_KEY);
  invalidateApiCache();
}

const GET_CACHE_RULES: Array<[RegExp, number]> = [
  [/^\/api\/build$/, 120_000],
  [/^\/api\/health$/, 5_000],
  [/^\/api\/model-registry$/, 20_000],
  [/^\/api\/rancher\/status$/, 15_000],
  [/^\/api\/rancher\/inventory$/, 15_000],
  [/^\/api\/prometheus\/summary(?:\?|$)/, 8_000],
  [/^\/api\/cmdb\/topology$/, 30_000],
  [/^\/api\/effectiveness$/, 10_000],
  [/^\/api\/llm-observability(?:\?|$)/, 8_000],
  [/^\/api\/integrations$/, 30_000],
  [/^\/api\/cloud\/adapters$/, 60_000],
  [/^\/api\/dashboard$/, 8_000],
  [/^\/api\/(?:alerts|incidents|postmortems)(?:\?|$)/, 8_000],
  [/^\/api\/ops\/capabilities$/, 60_000],
  [/^\/api\/ops\/skills$/, 30_000],
  [/^\/api\/infrastructure\/providers$/, 60_000],
  [/^\/api\/infrastructure\/resources(?:\?|$)/, 30_000],
  [/^\/api\/model-benchmark(?:\?|$)/, 15_000],
  [/^\/api\/effectiveness(?:\?|$)/, 10_000],
  [/^\/api\/knowledge\/sources$/, 30_000],
  [/^\/api\/reliability\/summary$/, 10_000],
  [/^\/api\/releases(?:\?|$)/, 8_000],
  [/^\/api\/algorithms\/workbench$/, 30_000],
];

const PRELOAD_URLS = [
  "/api/rancher/inventory",
  "/api/dashboard",
  "/api/cmdb/topology",
  "/api/effectiveness",
  "/api/llm-observability?limit=200",
  "/api/model-benchmark",
  "/api/knowledge/sources",
  "/api/reliability/summary",
  "/api/releases",
  "/api/alerts",
  "/api/incidents",
  "/api/postmortems",
  "/api/ops/capabilities",
  "/api/ops/skills",
  "/api/infrastructure/providers",
  "/api/infrastructure/resources",
  "/api/algorithms/workbench",
  "/api/integrations",
  "/api/cloud/adapters",
];

function cacheTtl(url: string) {
  return GET_CACHE_RULES.find(([pattern]) => pattern.test(url))?.[1] || 0;
}

export async function apiGet<T>(url: string): Promise<T> {
  const ttl = cacheTtl(url);
  const now = Date.now();
  const cached = ttl ? getCache.get(url) : undefined;
  if (cached && cached.expiresAt > now) return cached.data as T;

  const inFlight = ttl ? pendingGets.get(url) : undefined;
  if (inFlight) return inFlight as Promise<T>;

  const request = (async () => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), url.includes("/api/ops/jobs/") ? 12_000 : 30_000);
    let response: Response;
    try {
      response = await fetch(url, { headers: adminAuthHeaders({ Accept: "application/json" }), signal: controller.signal });
    } catch (error: any) {
      if (error?.name === "AbortError") throw new Error("请求超时，服务没有在限定时间内返回");
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = data as { detail?: unknown; error?: string };
      const detail = formatApiErrorDetail(error.detail);
      throw new Error(detail || error.error || `${response.status} ${response.statusText}`);
    }
    if (ttl) getCache.set(url, { expiresAt: Date.now() + ttl, data });
    return data as T;
  })();
  if (ttl) {
    pendingGets.set(url, request);
    request.then(
      () => pendingGets.delete(url),
      () => pendingGets.delete(url),
    );
  }
  return request;
}

export function preloadApplicationResources() {
  const run = async () => {
    for (let index = 0; index < PRELOAD_URLS.length; index += 4) {
      await Promise.allSettled(PRELOAD_URLS.slice(index, index + 4).map((url) => apiGet(url)));
    }
    // Load the sizeable WebGL dependency only after API requests are underway.
    await import("three").catch(() => undefined);
  };
  const browser = window as Window & { requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number };
  if (browser.requestIdleCallback) browser.requestIdleCallback(() => void run(), { timeout: 1600 });
  else window.setTimeout(() => void run(), 500);
}

export function invalidateApiCache(prefix = "") {
  if (!prefix) {
    getCache.clear();
    pendingGets.clear();
    return;
  }
  for (const key of Array.from(getCache.keys())) {
    if (key.startsWith(prefix)) getCache.delete(key);
  }
  for (const key of Array.from(pendingGets.keys())) {
    if (key.startsWith(prefix)) pendingGets.delete(key);
  }
}

export async function apiPost<T>(url: string, body: unknown): Promise<T> {
  const controller = new AbortController();
  const timeoutMs = url.startsWith("/api/ops/jobs") ? 30_000 : 120_000;
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: adminAuthHeaders({ "Content-Type": "application/json", Accept: "application/json" }),
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (error: any) {
    if (error?.name === "AbortError") throw new Error(`请求超过 ${timeoutMs / 1000} 秒未返回，已停止等待；后台任务可在执行记录中继续查询`);
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = data as { detail?: unknown; error?: string };
    const detail = formatApiErrorDetail(error.detail);
    throw new Error(detail || error.error || `${response.status} ${response.statusText}`);
  }
  if (url.startsWith("/api/releases") || url.startsWith("/api/reliability/")) {
    invalidateApiCache("/api/releases");
    invalidateApiCache("/api/reliability/");
  }
  if (url.startsWith("/api/ops/skills")) {
    invalidateApiCache("/api/ops/skills");
    invalidateApiCache("/api/ops/capabilities");
  }
  return data as T;
}

export async function apiDelete<T>(url: string): Promise<T> {
  const response = await fetch(url, { method: "DELETE", headers: adminAuthHeaders({ Accept: "application/json" }) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(formatApiErrorDetail((data as any).detail) || (data as any).error || `${response.status} ${response.statusText}`);
  invalidateApiCache("/api/clusters");
  invalidateApiCache("/api/rancher/inventory");
  return data as T;
}

function formatApiErrorDetail(detail: unknown): string {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item: any) => {
      const path = Array.isArray(item?.loc) ? item.loc.filter((part: string) => part !== "body").join(".") : "";
      const msg = item?.msg || item?.message || String(item);
      return path ? `${path}: ${msg}` : msg;
    }).join("；");
  }
  if (typeof detail === "object") {
    const value = detail as { message?: string; msg?: string; errors?: unknown; do_this?: unknown };
    const lines = [value.message || value.msg || ""];
    if (Array.isArray(value.errors)) lines.push(...value.errors.map(String));
    else if (value.errors) lines.push(String(value.errors));
    if (Array.isArray(value.do_this)) lines.push(...value.do_this.map(String));
    else if (value.do_this) lines.push(String(value.do_this));
    return lines.filter(Boolean).join("；") || JSON.stringify(value);
  }
  return String(detail);
}

export function asList(value: unknown): any[] {
  return Array.isArray(value) ? value : [];
}

export function compactNumber(value: unknown) {
  const number = Number(value || 0);
  if (number >= 1_000_000_000) return `${(number / 1_000_000_000).toFixed(1)}G`;
  if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(1)}M`;
  if (number >= 1_000) return `${(number / 1_000).toFixed(1)}K`;
  return String(Math.round(number * 100) / 100);
}

export function makeId() {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi && typeof cryptoApi.randomUUID === "function") return cryptoApi.randomUUID();
  if (cryptoApi && typeof cryptoApi.getRandomValues === "function") {
    const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
    return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
  }
  return `local-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

export function markdownToHtml(text: string) {
  const escape = (value: string) => value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (value: string) => escape(value)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
  const output: string[] = [];
  let list: "ul" | "ol" | "" = "";
  let inCode = false;
  let code: string[] = [];
  const closeList = () => {
    if (!list) return;
    output.push(`</${list}>`);
    list = "";
  };
  for (const line of String(text || "").replace(/\r\n/g, "\n").split("\n")) {
    if (line.trim().startsWith("```")) {
      closeList();
      if (inCode) {
        output.push(`<pre><code>${escape(code.join("\n"))}</code></pre>`);
        code = [];
      }
      inCode = !inCode;
      continue;
    }
    if (inCode) { code.push(line); continue; }
    const heading = line.match(/^(#{2,3})\s+(.+)$/);
    if (heading) {
      closeList();
      const level = heading[1].length;
      output.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    const unordered = line.match(/^\s*[-*]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      const next = unordered ? "ul" : "ol";
      if (list !== next) { closeList(); list = next; output.push(`<${list}>`); }
      output.push(`<li>${inline((unordered || ordered)![1])}</li>`);
      continue;
    }
    const quote = line.match(/^>\s?(.*)$/);
    if (quote) { closeList(); output.push(`<blockquote>${inline(quote[1])}</blockquote>`); continue; }
    if (!line.trim()) { closeList(); continue; }
    closeList();
    output.push(`<p>${inline(line)}</p>`);
  }
  closeList();
  if (inCode && code.length) output.push(`<pre><code>${escape(code.join("\n"))}</code></pre>`);
  return output.join("");
}

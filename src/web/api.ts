export class HttpError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
export class StaleSessionError extends Error {}
export type User = { id: string; display_name: string; roles: string[] };
export type Profile = { user: User; capabilities: string[] };
export type ApiReply<T> = { data: T; etag: string | null };
export type PendingCommand = { fingerprint: string; key: string; match?: string; path: string; method: string; replayBody?: string; unknownOutcome?: boolean };
export const etag = (row: { revision: number; etag?: string }) => row.etag || `"${row.revision}"`;
export const id = encodeURIComponent;
export const errorMessage = (error: unknown) => error instanceof TypeError ? "网络连接异常，请刷新原记录或以相同输入重试。" : error instanceof Error ? error.message : "操作未完成";

export type DocumentMetadata = { title: string; category?: string; model_scope: string[]; revision_label?: string; source_metadata: { channel: string; source_description: string; received_at?: string; source_party?: string; traceable_ref?: string } };
export const stableJSON = (value: unknown): string => JSON.stringify(value, (_key, item) => item && typeof item === "object" && !Array.isArray(item) ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b))) : item);

export class ApiClient {
  csrf: string | null = null;
  owner: string | null = null;
  private epoch = 0;
  onAccessLost: (message: string) => void = () => {};
  setSession(owner: string | null, csrf: string | null) { this.epoch++; this.owner = owner; this.csrf = csrf; }
  async request<T>(path: string, options: RequestInit = {}): Promise<ApiReply<T>> {
    const epoch = this.epoch;
    const headers = new Headers(options.headers);
    if (options.method && options.method !== "GET" && this.csrf) headers.set("X-CSRF-Token", this.csrf);
    const response = await fetch(path, { ...options, headers, cache: "no-store", credentials: "same-origin" });
    const body = response.status === 204 ? null : await response.json().catch(() => null);
    if (epoch !== this.epoch) throw new StaleSessionError("登录状态已改变，已忽略旧请求。");
    if (!response.ok) {
      const detail = body?.detail;
      const message = typeof detail === "string" ? detail : detail?.message || body?.message || "操作未完成，请检查输入或刷新原记录。";
      if ([401, 403, 404].includes(response.status) && this.owner) this.onAccessLost("当前登录或资料访问条件已变化，已清除页面内容。请重新登录或联系管理员。");
      throw new HttpError(message, response.status);
    }
    return { data: body as T, etag: response.headers.get("ETag") };
  }
  async get<T>(path: string): Promise<T> { return (await this.request<T>(path)).data; }
  async mutate<T>(path: string, payload: unknown, match?: string, method = "POST", retainBody = true): Promise<T> {
    return this.command<T>(path, JSON.stringify(payload), JSON.stringify(payload), match, method, retainBody);
  }
  async upload<T>(path: string, file: File, metadata: object): Promise<T> {
    const epoch = this.epoch;
    const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    const hash = Array.from(new Uint8Array(digest), x => x.toString(16).padStart(2, "0")).join("");
    if (epoch !== this.epoch) throw new StaleSessionError("登录状态已改变，已停止旧上传。");
    const form = new FormData(); form.set("file", file, file.name); form.set("metadata", JSON.stringify(metadata));
    return this.command<T>(path, form, JSON.stringify({ metadata, hash, name: file.name }), undefined, "POST");
  }
  async uploadDocument<T>(knowledgeBaseId: string, file: File, metadata: DocumentMetadata): Promise<T> {
    if (!file.size || !file.name.toLowerCase().endsWith(".pdf") || (file.type && file.type !== "application/pdf")) throw new Error("请选择非空 PDF 文件。");
    // A stable metadata order makes an explicit retry the same command after a reload.
    return this.upload<T>(`/knowledge-bases/${id(knowledgeBaseId)}/documents`, file, JSON.parse(stableJSON(metadata)));
  }
  async uploadVersion<T>(documentId: string, file: File, metadata: { revision_label: string | null; model_scope: string | null }): Promise<T> {
    const epoch = this.epoch;
    const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    if (epoch !== this.epoch) throw new StaleSessionError("登录状态已改变，已停止旧上传。");
    const hash = Array.from(new Uint8Array(digest), x => x.toString(16).padStart(2, "0")).join("");
    const query = new URLSearchParams();
    if (metadata.revision_label !== null) query.set("revision_label", metadata.revision_label);
    if (metadata.model_scope !== null) query.set("model_scope", metadata.model_scope);
    const form = new FormData(); form.set("file", file, file.name);
    return this.command<T>(`/documents/${id(documentId)}/versions${query.size ? `?${query}` : ""}`, form, JSON.stringify({ metadata, hash, name: file.name }), undefined, "POST");
  }
  private async command<T>(path: string, body: string | FormData, fingerprint: string, match: string | undefined, method: string, retainBody = true): Promise<T> {
    const epoch = this.epoch;
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(fingerprint));
    const fingerprintHash = Array.from(new Uint8Array(digest), x => x.toString(16).padStart(2, "0")).join("");
    if (epoch !== this.epoch || !this.owner) throw new StaleSessionError("登录状态已改变，已停止旧操作。");
    const slot = `dc-workspace-command:${this.owner}:${method}:${path.split("?")[0]}`;
    let pending: PendingCommand | null = null;
    try { pending = JSON.parse(sessionStorage.getItem(slot) || "null"); } catch { sessionStorage.removeItem(slot); }
    if (pending && pending.fingerprint !== fingerprintHash) throw new Error("此操作上次结果未知；请恢复相同输入读取原回执，再执行其他变更。");
    pending ||= { key: crypto.randomUUID(), fingerprint: fingerprintHash, match, path, method, ...(retainBody && typeof body === "string" ? { replayBody: body } : {}) };
    sessionStorage.setItem(slot, JSON.stringify(pending));
    try {
      const result = await this.request<T>(path, { method, headers: { "Idempotency-Key": pending.key, ...(typeof body === "string" ? { "Content-Type": "application/json" } : {}), ...(pending.match ? { "If-Match": pending.match } : {}) }, body });
      sessionStorage.removeItem(slot); return result.data;
    } catch (error) {
      if (error instanceof HttpError && error.status < 500 && !pending.unknownOutcome) sessionStorage.removeItem(slot);
      else {
        // A later denial cannot prove that an earlier request was not committed.
        // Keep its original key until an actual operation receipt is observed.
        pending.unknownOutcome = true; sessionStorage.setItem(slot, JSON.stringify(pending));
        if (!(error instanceof StaleSessionError) && (!(error instanceof HttpError) || error.status >= 500)) throw new Error("操作结果未知；请保持相同输入重试以读取同一操作回执。" + errorMessage(error));
      }
      throw error;
    }
  }
  pendingCommands(): PendingCommand[] {
    if (!this.owner) return [];
    const prefix = `dc-workspace-command:${this.owner}:`, commands: PendingCommand[] = [];
    for (let index = 0; index < sessionStorage.length; index++) {
      const key = sessionStorage.key(index); if (!key?.startsWith(prefix)) continue;
      try { const entry = JSON.parse(sessionStorage.getItem(key) || "null"); if (entry?.replayBody && entry.path && entry.method) commands.push(entry); } catch { /* Invalid tab metadata is not executable. */ }
    }
    return commands;
  }
  async recoverCommand(command: PendingCommand) { return this.command(command.path, command.replayBody!, command.replayBody!, command.match, command.method); }
}
export const api = new ApiClient();

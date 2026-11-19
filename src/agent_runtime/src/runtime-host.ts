/** Private worker-owned JSON-lines subset. No CLI, extensions, skills or MCP. */
import { createInterface } from "node:readline";
import { mkdir, readFile, realpath } from "node:fs/promises";
import { resolve, join, sep } from "node:path";
import { Type } from "typebox";
import { PI_AI_VERSION } from "@earendil-works/pi-ai";
import { SessionManager, VERSION, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { createRuntimeSession, SESSION_RESOURCES, SESSION_SETTINGS } from "./pi00-session.js";
import { createControlledProvider, sha } from "./controlled-provider.js";

const BRIDGE = {
  protocol: "data-center-json-lines/1",
  worker_methods: ["session.open", "run.prompt", "run.abort"],
  host_methods: ["tool.invoke", "run.event"],
  operations: ["answer"],
  max_message_bytes: 1048576,
  max_pending_messages: 16,
};
const allowed = new Set(["dc_context_read", "dc_sources_search", "dc_evidence_search", "dc_sources_read", "dc_artifacts_validate", "dc_artifacts_deliver", "dc_tasks_request_input", "dc_tasks_control"]);
const terminalToolStates = new Set(["accepted", "waiting_input", "reconciliation_required", "succeeded", "blocked", "failed", "cancelled"]);
const input = createInterface({ input: process.stdin });
const pending = new Map<string, { resolve: (value: any) => void; reject: (error: Error) => void }>();
const stop = new AbortController();
type Session = Awaited<ReturnType<typeof createRuntimeSession>>["session"];
let session: Session | undefined;
let manager: SessionManager;
let state: "initial" | "opening" | "open" | "running" | "finished" = "initial";
let waiting = false, calls = 0, sequence = 0;

function send(message: unknown) {
  const line = JSON.stringify(message) + "\n";
  if (Buffer.byteLength(line) > BRIDGE.max_message_bytes || process.stdout.writableLength > BRIDGE.max_message_bytes) {
    throw new Error("Worker channel overloaded");
  }
  process.stdout.write(line);
}

function canonical(value: any): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value !== null && typeof value === "object") {
    return "{" + Object.keys(value).sort().map((key) => JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}";
  }
  return JSON.stringify(value);
}

function event(status: "opened" | "running" | "stopped" | "settled") {
  send({ method: "run.event", params: { kind: "run_status", sequence: ++sequence, status } });
}

function rejectPending() {
  for (const waiter of pending.values()) waiter.reject(new Error("Worker channel stopped"));
  pending.clear();
}

function replyError(id: string, code: string) {
  send({ id, error: { code } });
}

input.on("line", (line) => {
  try {
    if (Buffer.byteLength(line) + 1 > BRIDGE.max_message_bytes) throw new Error("Worker message exceeds limit");
    const message = JSON.parse(line);
    if (!message || typeof message.id !== "string" || message.id.length === 0 || message.id.length > 128) {
      throw new Error("Invalid worker message");
    }
    const keys = Object.keys(message);
    if (Object.hasOwn(message, "method")) {
      if (typeof message.method !== "string" || keys.some((key) => !["id", "method", "params"].includes(key))) {
        throw new Error("Invalid worker request");
      }
      if (message.method === "session.open" && state === "initial") {
        state = "opening";
        void open(message.id, message.params);
      } else if (message.method === "run.prompt" && state === "open" && keys.length === 2) {
        state = "running";
        void prompt(message.id);
      } else if (message.method === "run.abort" && keys.length === 2) {
        void abort(message.id);
      } else {
        replyError(message.id, BRIDGE.worker_methods.includes(message.method) ? "BRIDGE_STATE_INVALID" : "BRIDGE_METHOD_UNSUPPORTED");
      }
      return;
    }
    const waiter = pending.get(message.id);
    const isError = Object.hasOwn(message, "error");
    if (keys.length !== 2 || isError === Object.hasOwn(message, "result")) throw new Error("Invalid tool reply");
    if (!waiter && stop.signal.aborted && /^tool-\d+$/.test(message.id)) return;
    if (!waiter) throw new Error("Unexpected tool reply");
    pending.delete(message.id);
    if (isError) waiter.reject(new Error("Controlled tool rejected"));
    else waiter.resolve(message.result);
  } catch {
    process.exitCode = 2;
    input.close();
  }
});
input.on("close", () => {
  stop.abort();
  rejectPending();
  void session?.abort().catch(() => {});
});

async function open(id: string, config: any) {
  try {
    const manifest = config.artifact_manifest;
    if (canonical(config.bridge) !== canonical(BRIDGE)
        || canonical(config.settings) !== canonical(SESSION_SETTINGS)
        || canonical(config.resources) !== canonical(SESSION_RESOURCES)
        || manifest.node_version !== process.version || VERSION !== "1.5.2" || PI_AI_VERSION !== "1.5.2"
        || manifest.platform !== process.platform || manifest.arch !== process.arch
        || !/^[0-9a-f]{64}$/.test(config.configuration_hash)) {
      throw new Error("Runtime profile is incompatible");
    }
    for (const name of ["runtime-host.js", "pi00-session.js", "controlled-provider.js"]) {
      const actual = sha(await readFile(new URL(name, import.meta.url)));
      if (manifest.built_files["dist/" + name] !== actual) throw new Error("Running bridge file differs from profile");
    }
    if (!Array.isArray(config.tools) || config.tools.length !== allowed.size
        || new Set(config.tools.map((tool: any) => tool.name)).size !== allowed.size
        || config.tools.some((tool: any) => !allowed.has(tool.name))
        || sha(canonical(config.tools)) !== config.tool_set_hash) throw new Error("Tool schema differs from profile");

    const root = await realpath(resolve(config.session_root));
    const cwd = join(root, "workspace"), agentDir = join(root, "agent"), journal = join(root, "journal");
    for (const path of [cwd, agentDir, journal]) {
      await mkdir(path, { recursive: true });
      if (await realpath(path) !== path) throw new Error("Working directory outside binding scope");
    }
    const wire = createControlledProvider({ gateUrl: config.gate_url, runId: config.run_id, token: config.token,
      configurationHash: config.configuration_hash, generation: config.generation, fixedGeneration: config.fixed_generation,
      stopSignal: stop.signal });
    const tools: ToolDefinition[] = config.tools.map((tool: any) => ({
      name: tool.name, label: tool.name, description: tool.description, parameters: Type.Unsafe(tool.parameters),
      async execute(piCallId: string, args: Record<string, unknown>) {
        if (waiting || stop.signal.aborted || pending.size >= BRIDGE.max_pending_messages) {
          throw new Error("Business execution cannot accept another tool");
        }
        const callId = `tool-${++calls}`;
        const result = await new Promise<any>((resolve, reject) => {
          pending.set(callId, { resolve, reject });
          send({ id: callId, method: "tool.invoke", params: { pi_tool_call_id: piCallId, name: tool.name, args } });
        });
        if (terminalToolStates.has(result.state)) waiting = true;
        return { content: [{ type: "text", text: JSON.stringify(result) }], details: { state: result.state, call_id: result.call_id } };
      },
    }));
    if (config.session_file) {
      const existing = await realpath(resolve(config.session_file));
      if (!existing.startsWith(journal + sep) || !existing.endsWith(".jsonl")) throw new Error("Journal outside binding scope");
      manager = SessionManager.open(existing, journal, cwd);
    } else manager = SessionManager.create(cwd, journal);
    const created = await createRuntimeSession({ cwd, agentDir, ...wire, tools, sessionManager: manager,
      waiting: () => waiting || stop.signal.aborted });
    session = created.session;
    stop.signal.throwIfAborted();
    state = "open";
    send({ id, result: { protocol: BRIDGE.protocol, node_version: process.version, sdk_version: VERSION,
      configuration_hash: config.configuration_hash, tool_set_hash: config.tool_set_hash } });
    event("opened");
  } catch {
    state = "finished";
    session?.dispose();
    replyError(id, "RUNTIME_PROFILE_INCOMPATIBLE");
  }
}

async function prompt(id: string) {
  try {
    if (!session) throw new Error("Session not open");
    event("running");
    await session.prompt("Run the accepted device query using only the registered controlled tools and current backend projection.");
    const last = session.agent.state.messages.at(-1);
    const result = stop.signal.aborted ? "aborted"
      : last?.role === "assistant" && (last.stopReason === "error" || last.stopReason === "aborted") ? "model_error" : "settled";
    if (result !== "aborted") event("settled");
    send({ id, result: { state: result, waiting,
      pi_session_id: manager.getSessionId(), session_file: manager.getSessionFile() } });
  } catch {
    if (stop.signal.aborted) send({ id, result: { state: "aborted" } });
    else replyError(id, "RUNTIME_HOST_FAILED");
  } finally {
    state = "finished";
    if (!stop.signal.aborted) session?.dispose();
  }
}

async function abort(id: string) {
  if (stop.signal.aborted) {
    replyError(id, "BRIDGE_STATE_INVALID");
    return;
  }
  stop.abort();
  rejectPending();
  try {
    if (state === "running") await session?.abort();
    state = "finished";
    session?.dispose();
    event("stopped");
    send({ id, result: { state: "aborted" } });
  } catch {
    replyError(id, "RUNTIME_ABORT_FAILED");
  }
}

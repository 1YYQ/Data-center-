import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { once } from "node:events";
import { createHash, randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { Type } from "typebox";
import { fauxProvider, fauxAssistantMessage, fauxToolCall } from "@earendil-works/pi-ai";
import { ModelRuntime, SessionManager } from "@earendil-works/pi-coding-agent";
import { createPI00Session, SESSION_RESOURCES, SESSION_SETTINGS } from "../dist/pi00-session.js";
import { createPI00WireProvider } from "../dist/pi00-wire.js";

async function fixture(t, extra = {}) {
  const cwd = await mkdtemp(join(tmpdir(), "data-center-pi00-"));
  const agentDir = join(cwd, "controlled-agent");
  await mkdir(agentDir);
  const faux = fauxProvider({ provider: "data-center-pi00", models: [{ id: "synthetic", contextWindow: 8192, maxTokens: 512 }], tokensPerSecond: 0 });
  const active = [];
  t.after(async () => {
    for (const probe of active) probe.session.dispose();
    assert.equal(dirname(cwd), resolve(tmpdir()));
    assert.ok(basename(cwd).startsWith("data-center-pi00-"));
    await rm(cwd, { recursive: true, force: true });
  });
  async function create(options = {}) {
    const probe = await createPI00Session({ cwd, agentDir, provider: faux.provider, model: faux.getModel(), ...extra, ...options });
    active.push(probe);
    return probe;
  }
  return { cwd, agentDir, faux, create };
}

test("real SDK ignores ambient context/settings and enables no coding tools", async (t) => {
  const f = await fixture(t);
  await writeFile(join(f.cwd, "AGENTS.md"), "AMBIENT_CONTEXT_MUST_NOT_ENTER");
  await mkdir(join(f.cwd, ".pi"));
  await writeFile(join(f.cwd, ".pi", "settings.json"), JSON.stringify({ retry: { enabled: true }, defaultTools: ["bash"] }));
  const p = await f.create();
  assert.ok(p.runtime instanceof ModelRuntime);
  assert.deepEqual(p.resources.getAgentsFiles().agentsFiles, []);
  assert.deepEqual(p.session.agent.state.tools, []);
  f.faux.setResponses([fauxAssistantMessage("synthetic reply")]);
  await p.session.prompt("SYNTHETIC_ONLY");
  assert.equal(f.faux.state.callCount, 1);
  assert.equal(JSON.stringify(p.session.agent.state.messages).includes("AMBIENT_CONTEXT_MUST_NOT_ENTER"), false);
});

test("accepted tool receipt stops after settled tool batch, without another model request", async (t) => {
  let waiting = false;
  const tool = { name: "controlled_probe", label: "Synthetic probe", description: "Synthetic accepted receipt", parameters: Type.Object({}),
    async execute() {
      waiting = true;
      return { content: [{ type: "text", text: "waiting_input; business task is incomplete" }], details: { state: "waiting_input" } };
    } };
  const f = await fixture(t, { tools: [tool], waiting: () => waiting });
  const p = await f.create();
  assert.deepEqual(p.session.agent.state.tools.map((x) => x.name), ["controlled_probe"]);
  f.faux.setResponses([fauxAssistantMessage(fauxToolCall("controlled_probe", {}), { stopReason: "toolUse" }), fauxAssistantMessage("MUST_NOT_RUN")]);
  await p.session.prompt("Run the synthetic controlled tool once.");
  assert.equal(waiting, true);
  assert.equal(f.faux.state.callCount, 1);
  assert.equal(f.faux.getPendingResponseCount(), 1);
});

test("SDK session can be rebuilt from its own persisted synthetic journal", async (t) => {
  const f = await fixture(t);
  const manager = SessionManager.create(f.cwd, join(f.cwd, "sessions"));
  const p = await f.create({ sessionManager: manager });
  f.faux.setResponses([fauxAssistantMessage("synthetic first reply")]);
  await p.session.prompt("SYNTHETIC_HISTORY_MARKER");
  const file = manager.getSessionFile();
  assert.ok(file);
  p.session.dispose();
  const restored = SessionManager.open(file, join(f.cwd, "sessions"), f.cwd);
  const second = await f.create({ sessionManager: restored });
  assert.ok(JSON.stringify(second.session.agent.state.messages).includes("SYNTHETIC_HISTORY_MARKER"));
  assert.equal(f.faux.state.callCount, 1, "rebuilding must not itself call the model");
});

test("explicit compaction uses the registered synthetic provider", async (t) => {
  const f = await fixture(t);
  const manager = SessionManager.inMemory(f.cwd);
  for (let n = 0; n < 12; n++) {
    manager.appendMessage({ role: "user", content: [{ type: "text", text: `synthetic-${n} ` + "context ".repeat(200) }], timestamp: Date.now() });
    manager.appendMessage(fauxAssistantMessage(`synthetic reply ${n}`));
  }
  const p = await f.create({ sessionManager: manager });
  f.faux.setResponses([fauxAssistantMessage("Synthetic compacted context; no business evidence.")]);
  const before = f.faux.state.callCount;
  await p.session.compact();
  assert.equal(f.faux.state.callCount, before + 1);
});

test("PI00 entry rejects other providers", async (t) => {
  const f = await fixture(t);
  await assert.rejects(f.create({ provider: { ...f.faux.provider, id: "not-approved" } }), /explicit synthetic provider/);
});

test("private host rejects unknown methods and incompatible tool schema, then aborts an active SDK prompt", { timeout: 20000 }, async (t) => {
  const f = await fixture(t);
  let started, observed = new Promise((resolve) => { started = resolve; });
  const gateway = createServer((request) => { started(request.url); });
  await new Promise((ready) => gateway.listen(0, "127.0.0.1", ready));
  t.after(() => { gateway.closeAllConnections(); gateway.close(); });
  const token = randomUUID();
  const hash = (value) => createHash("sha256").update(value).digest("hex");
  const host = new URL("../dist/runtime-host.js", import.meta.url);
  const built = Object.fromEntries(await Promise.all(["runtime-host.js", "pi00-session.js", "controlled-provider.js"]
    .map(async (name) => ["dist/" + name, hash(await readFile(new URL(name, host)))])));
  const tools = ["dc_context_read", "dc_sources_search", "dc_evidence_search", "dc_sources_read", "dc_artifacts_validate",
    "dc_artifacts_deliver", "dc_tasks_request_input", "dc_tasks_control"].map((name) => ({ description: name, name, parameters: {} }));
  const base = {
    artifact_manifest: { node_version: process.version, platform: process.platform, arch: process.arch, built_files: built },
    bridge: { protocol: "data-center-json-lines/1", worker_methods: ["session.open", "run.prompt", "run.abort"],
      host_methods: ["tool.invoke", "run.event"], operations: ["answer"], max_message_bytes: 1048576, max_pending_messages: 16 },
    settings: SESSION_SETTINGS,
    resources: SESSION_RESOURCES,
    generation: { model: "pi-local-synthetic", adapter: "pi-ai-1.5.2/openai-completions", service_adapter: "openai-chat-v1",
      api_family: "openai-chat-completions-sse-v1", tokenizer: "utf8-byte-ceiling-v1", context_window: 8192,
      max_output_tokens: 128, capabilities: ["text", "function_tools", "sse", "stream_usage"] },
    fixed_generation: true,
    session_root: f.cwd,
    session_file: null,
    run_id: randomUUID(),
    gate_url: `http://127.0.0.1:${gateway.address().port}/`,
    token, tools, tool_set_hash: hash(JSON.stringify(tools)), configuration_hash: "1".repeat(64),
  };
  function bridge() {
    const child = spawn(process.execPath, [fileURLToPath(host)], { windowsHide: true, stdio: ["pipe", "pipe", "ignore"] });
    const lines = createInterface({ input: child.stdout });
    const messages = lines[Symbol.asyncIterator]();
    t.after(() => { lines.close(); child.stdin.end(); if (child.exitCode === null) child.kill(); });
    return {
      send(message) { child.stdin.write(JSON.stringify(message) + "\n"); },
      async next() {
        const { value, done } = await messages.next();
        assert.equal(done, false);
        assert.equal(value.includes(token), false);
        return JSON.parse(value);
      },
    };
  }
  const incompatible = bridge();
  incompatible.send({ id: "open", method: "session.open", params: { ...base, tool_set_hash: "0".repeat(64) } });
  assert.deepEqual(await incompatible.next(), { id: "open", error: { code: "RUNTIME_PROFILE_INCOMPATIBLE" } });
  const runtime = bridge();
  runtime.send({ id: "unknown", method: "session.resume" });
  assert.deepEqual(await runtime.next(), { id: "unknown", error: { code: "BRIDGE_METHOD_UNSUPPORTED" } });
  runtime.send({ id: "too-early", method: "run.prompt" });
  assert.deepEqual(await runtime.next(), { id: "too-early", error: { code: "BRIDGE_STATE_INVALID" } });
  runtime.send({ id: "open", method: "session.open", params: base });
  assert.equal((await runtime.next()).result.protocol, base.bridge.protocol);
  assert.deepEqual((await runtime.next()).params, { kind: "run_status", sequence: 1, status: "opened" });
  runtime.send({ id: "run", method: "run.prompt" });
  assert.equal((await runtime.next()).params.status, "running");
  assert.match(await observed, /\/context-project$/);
  runtime.send({ id: "abort", method: "run.abort" });
  const replies = [await runtime.next(), await runtime.next(), await runtime.next()];
  assert.deepEqual(replies.find((reply) => reply.id === "abort"), { id: "abort", result: { state: "aborted" } });
  assert.equal(replies.find((reply) => reply.id === "run").result.state, "aborted");
  assert.deepEqual(replies.find((reply) => reply.method === "run.event").params, { kind: "run_status", sequence: 3, status: "stopped" });
});

async function wireFixture(t) {
  const f = await fixture(t);
  const bodies = [];
  let status = 200;
  const upstream = createServer(async (request, response) => {
    let body = "";
    for await (const part of request) body += part;
    bodies.push(body);
    if (status !== 200) { response.writeHead(status); response.end('{"error":{"message":"synthetic unavailable"}}'); return; }
    const event = { id: "pi00", object: "chat.completion.chunk", created: 1, model: "pi00-synthetic",
      choices: [{ index: 0, delta: { role: "assistant", content: "Synthetic HTTP reply." }, finish_reason: null }] };
    const end = { ...event, choices: [{ index: 0, delta: {}, finish_reason: "stop" }] };
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    response.end(`data: ${JSON.stringify(event)}\n\ndata: ${JSON.stringify(end)}\n\ndata: [DONE]\n\n`);
  });
  await new Promise((accept) => upstream.listen(0, "127.0.0.1", accept));
  const key = randomUUID();
  const child = spawn(process.env.PI00_PYTHON ?? "python", ["-X", "utf8", fileURLToPath(new URL("./support/pi00_gateway.py", import.meta.url)), `http://127.0.0.1:${upstream.address().port}`], {
    windowsHide: true, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, PI00_SYNTHETIC_KEY: key },
  });
  const lines = createInterface({ input: child.stdout });
  const errors = [];
  child.stderr.on("data", (chunk) => errors.push(chunk.toString().slice(0, 2000)));
  t.after(async () => {
    lines.close();
    if (child.exitCode === null) { child.kill(); await once(child, "exit"); }
    upstream.closeAllConnections();
    await new Promise((done) => upstream.close(done));
  });
  const ready = await once(lines, "line", { signal: AbortSignal.timeout(15000) }).catch((error) => { throw new Error(`PI00 fixture startup failed: ${errors.join("")}`, { cause: error }); });
  const gateUrl = `http://127.0.0.1:${JSON.parse(ready[0]).port}/`;
  async function post(path, body, extra = {}) {
    return fetch(new URL(path, gateUrl), { method: "POST", headers: { "Content-Type": "application/json", "X-PI00-Key": key, ...extra }, body: JSON.stringify(body), redirect: "error" });
  }
  let purpose = "answer";
  const wire = createPI00WireProvider({ gateUrl, key, purpose: () => purpose });
  return { ...f, wire, bodies, post, setPurpose(value) { purpose = value; }, failUpstream() { status = 503; } };
}

test("native SDK answer, compaction and auxiliary summary cross the same real local FastAPI gate", async (t) => {
  const f = await wireFixture(t);
  const manager = SessionManager.inMemory(f.cwd);
  for (let n = 0; n < 12; n++) {
    manager.appendMessage({ role: "user", content: [{ type: "text", text: "UNAPPROVED_OLD_CONTEXT ".repeat(200) }], timestamp: Date.now() });
    manager.appendMessage(fauxAssistantMessage("Old synthetic reply."));
  }
  const p = await f.create({ ...f.wire, sessionManager: manager });
  await p.session.prompt("SDK_CONTEXT_MUST_NOT_ENTER_HTTP");
  assert.equal(p.session.agent.state.messages.at(-1).stopReason, "stop");
  f.setPurpose("compaction");
  await p.session.compact();
  f.setPurpose("summary");
  const result = await p.runtime.streamSimple(f.wire.model, { messages: [{ role: "user", content: "UNAPPROVED_OLD_CONTEXT", timestamp: 1 }] }, {
    onPayload: () => { throw new Error("untrusted callback must not run"); }, maxRetries: 8,
    fetch: () => { throw new Error("untrusted transport must not run"); },
  }).result();
  assert.equal(result.stopReason, "stop");
  assert.equal(f.bodies.length, 3);
  for (const body of f.bodies) {
    assert.ok(body.includes("PI00 AUTHORIZED SYNTHETIC SOURCE"));
    assert.ok(!body.includes("UNAPPROVED_OLD_CONTEXT") && !body.includes("SDK_CONTEXT_MUST_NOT_ENTER_HTTP"));
  }
  const state = await (await f.post("/fixture/control", {})).json();
  assert.deepEqual(state.calls.map((call) => [call.purpose, call.state]), [["answer", "completed"], ["compaction", "completed"], ["summary", "completed"]]);
});

test("gate refuses missing or altered permits, replay and revocation between authorization and dispatch", async (t) => {
  const f = await wireFixture(t);
  assert.throws(() => createPI00WireProvider({ gateUrl: "https://example.invalid/", key: "synthetic", purpose: () => "answer" }), /loopback gate/);
  const path = "/v1/chat/completions";
  assert.equal((await f.post(path, {})).status, 409);
  const projection = await (await f.post("/context/project", { purpose: "answer" })).json();
  const body = { model: "pi00-synthetic", messages: [{ role: "user", content: "SYNTHETIC_DIRECT_GATE_PROBE" }] };
  const payloadDigest = createHash("sha256").update(JSON.stringify(body)).digest("hex");
  const request = { purpose: "answer", projection_id: projection.id, context_digest: projection.context_digest, adapter: "pi-ai-1.5.2/openai-completions", payload_digest: payloadDigest };
  assert.equal((await f.post("/model/authorize", { ...request, context_digest: "invalid" })).status, 403);
  const permit = await (await f.post("/model/authorize", request)).json();
  const header = { "X-Model-Call": permit.id };
  assert.equal((await f.post(path, { ...body, extra: "tamper" }, header)).status, 409);
  assert.equal((await f.post(path, body, header)).status, 200);
  assert.equal((await f.post(path, body, header)).status, 409);
  assert.equal(f.bodies.length, 1);
  await f.post("/fixture/control", { revoke_on_authorize: true });
  const p = await f.create(f.wire);
  await p.session.prompt("SYNTHETIC_REVOKED");
  assert.equal(p.session.agent.state.messages.at(-1).stopReason, "error");
  assert.equal(f.bodies.length, 1, "revoked admission cannot reach upstream");
});

test("a real HTTP upstream failure is not retried by the SDK adapter", async (t) => {
  const f = await wireFixture(t);
  f.failUpstream();
  const p = await f.create(f.wire);
  await p.session.prompt("SYNTHETIC_FAILURE");
  assert.equal(p.session.agent.state.messages.at(-1).stopReason, "error");
  assert.equal(f.bodies.length, 1);
  const state = await (await f.post("/fixture/control", {})).json();
  assert.equal(state.calls.length, 1);
  assert.equal(state.calls[0].state, "failed");
});

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

// Only HTTP and tab storage boundaries are controlled. No product database,
// credentials, provider, or browser session is used by these client checks.
const source = await readFile(new URL("../api.ts", import.meta.url), "utf8");
function compileModule(source) {
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText;
  return `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`;
}
const apiModule = compileModule(source);
const { api, ApiClient, HttpError, StaleSessionError } = await import(apiModule);
class TabStorage {
  data = new Map();
  getItem(key) { return this.data.get(key) ?? null; }
  setItem(key, value) { this.data.set(key, String(value)); }
  removeItem(key) { this.data.delete(key); }
  get length() { return this.data.size; }
  key(index) { return [...this.data.keys()][index] ?? null; }
}
const response = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
function setup() { globalThis.sessionStorage = new TabStorage(); const client = new ApiClient(); client.setSession("employee-1", "csrf-1"); return client; }

test("writes use same-origin cookies and CSRF; reads bypass browser cache", async () => {
  const client = setup(), calls = [];
  globalThis.fetch = async (path, options) => { calls.push({ path, options }); return response({ ok: true }); };
  await client.mutate("/documents/one/review", { decision: "approved" }, '"4"');
  await client.get("/tasks/one");
  assert.equal(calls[0].options.headers.get("X-CSRF-Token"), "csrf-1");
  assert.equal(calls[0].options.headers.get("If-Match"), '"4"');
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.equal(calls[1].options.cache, "no-store");
  assert.equal(calls[1].options.headers.get("X-CSRF-Token"), null);
});
test("unknown writes retain original key and If-Match across restore; changed payload is blocked", async () => {
  const client = setup(), calls = [];
  globalThis.fetch = async (_path, options) => { calls.push(options); if (calls.length === 1) throw new TypeError("response lost"); return response({ receipt: "original" }); };
  await assert.rejects(client.mutate("/documents/one/publish", { preview_id: "frozen-1" }, '"2"'), /操作结果未知/);
  const restored = new ApiClient(); restored.setSession("employee-1", "csrf-new");
  await assert.rejects(restored.mutate("/documents/one/publish", { preview_id: "different" }, '"3"'), /相同输入/);
  assert.equal(calls.length, 1);
  assert.deepEqual(await restored.mutate("/documents/one/publish", { preview_id: "frozen-1" }, '"3"'), { receipt: "original" });
  assert.equal(calls[0].headers.get("Idempotency-Key"), calls[1].headers.get("Idempotency-Key"));
  assert.equal(calls[1].headers.get("If-Match"), '"2"');
  assert.equal(calls[1].headers.get("X-CSRF-Token"), "csrf-new");
  assert.equal(sessionStorage.data.size, 0);
});
test("definitive validation failures release input; server errors preserve it", async () => {
  const client = setup(); let status = 422;
  globalThis.fetch = async () => response({ detail: { message: "input rejected" } }, status);
  await assert.rejects(client.mutate("/knowledge/queries", { query: "one" }), error => error instanceof HttpError && error.status === 422);
  assert.equal(sessionStorage.data.size, 0);
  status = 500;
  await assert.rejects(client.mutate("/knowledge/queries", { query: "two" }), /操作结果未知/);
  assert.equal(sessionStorage.data.size, 1);
});
test("loss of access clears session and fences earlier successful in-flight responses", async () => {
  const client = setup(); let finish;
  client.onAccessLost = () => client.setSession(null, null);
  globalThis.fetch = path => path === "/slow" ? new Promise(resolve => { finish = resolve; }) : Promise.resolve(response({ message: "access revoked" }, 403));
  const previous = client.get("/slow");
  await assert.rejects(client.get("/protected"), error => error instanceof HttpError && error.status === 403);
  finish(response({ quote: "must not be rendered" }));
  await assert.rejects(previous, StaleSessionError);
});
test("session changes while a command fingerprint is computed never dispatch as the next user", async () => {
  const client = setup(); let calls = 0;
  globalThis.fetch = async () => { calls++; return response({}); };
  const previous = client.mutate("/documents/one/publish", { preview_id: "one" });
  client.setSession("employee-2", "csrf-2");
  await assert.rejects(previous, StaleSessionError);
  assert.equal(calls, 0);
});
test("management commands can recover the exact persisted JSON after page refresh", async () => {
  const client = setup(); let calls = 0;
  globalThis.fetch = async () => { if (++calls === 1) throw new TypeError("lost response"); return response({ receipt: "same" }); };
  await assert.rejects(client.mutate("/documents/one/publish", { preview_id: "frozen" }, '"2"'));
  const restored = new ApiClient(); restored.setSession("employee-1", "csrf-new");
  const commands = restored.pendingCommands(); assert.equal(commands.length, 1);
  assert.deepEqual(await restored.recoverCommand(commands[0]), { receipt: "same" });
  assert.equal(restored.pendingCommands().length, 0);
});
test("a denial after an unknown outcome retains the original key across re-authentication", async () => {
  const client = setup(), calls = [];
  client.onAccessLost = () => client.setSession(null, null);
  globalThis.fetch = async (_path, options) => {
    calls.push(options);
    if (calls.length === 1) throw new TypeError("response lost after commit");
    if (calls.length === 2) return response({ message: "permission revoked" }, 403);
    return response({ receipt: "original" });
  };
  const payload = { preview_id: "frozen" };
  await assert.rejects(client.mutate("/documents/one/publish", payload, '"2"'));
  await assert.rejects(client.mutate("/documents/one/publish", payload, '"2"'), error => error instanceof HttpError && error.status === 403);
  client.setSession("employee-1", "renewed-csrf");
  assert.equal(client.pendingCommands().length, 1);
  await client.mutate("/documents/one/publish", payload, '"5"');
  assert.equal(calls[0].headers.get("Idempotency-Key"), calls[1].headers.get("Idempotency-Key"));
  assert.equal(calls[0].headers.get("Idempotency-Key"), calls[2].headers.get("Idempotency-Key"));
  assert.equal(calls[2].headers.get("If-Match"), '"2"');
});
test("version upload places metadata in query, file in form, and replays the same file key", async () => {
  const client = setup(), calls = [], file = new File(["synthetic PDF bytes"], "revision.pdf", { type: "application/pdf" });
  globalThis.fetch = async (path, options) => { calls.push({ path, options }); if (calls.length === 1) throw new TypeError("upload response lost"); return response({ version: { id: "new-draft" } }); };
  const metadata = { revision_label: "R2", model_scope: "MODEL-A,MODEL-B" };
  await assert.rejects(client.uploadVersion("doc/one", file, metadata), /操作结果未知/);
  await assert.rejects(client.uploadVersion("doc/one", file, { ...metadata, revision_label: "R3" }), /相同输入/);
  assert.equal(calls.length, 1);
  const restored = new ApiClient(); restored.setSession("employee-1", "csrf-2");
  await restored.uploadVersion("doc/one", file, metadata);
  const url = new URL(calls[1].path, "https://local.invalid");
  assert.equal(url.pathname, "/documents/doc%2Fone/versions");
  assert.equal(url.searchParams.get("revision_label"), "R2");
  assert.equal(url.searchParams.get("model_scope"), "MODEL-A,MODEL-B");
  assert.equal(calls[1].options.body.get("metadata"), null);
  assert.equal(calls[1].options.body.get("file").name, "revision.pdf");
  assert.equal(calls[0].options.headers.get("Idempotency-Key"), calls[1].options.headers.get("Idempotency-Key"));
  assert.equal(calls[1].options.headers.get("Content-Type"), null);
});

test("new document uses encoded KB path and multipart provenance metadata", async () => {
  const client = setup(); let call;
  globalThis.fetch = async (path, options) => { call = { path, options }; return response({ document: { id: "doc" }, version: { id: "version" } }); };
  const file = new File(["PDF"], "manual.pdf", { type: "application/pdf" });
  const metadata = { title: "manual", model_scope: ["M1"], source_metadata: { channel: "local", source_description: "approved sample" } };
  await client.uploadDocument("kb/one", file, metadata);
  assert.equal(call.path, "/knowledge-bases/kb%2Fone/documents");
  assert.deepEqual(JSON.parse(call.options.body.get("metadata")), metadata);
  assert.equal(call.options.headers.get("Content-Type"), null);
  assert.equal(call.options.headers.get("X-CSRF-Token"), "csrf-1");
});
test("new document unknown response replays same file and reordered metadata with original key", async () => {
  const client = setup(), calls = [], file = new File(["PDF"], "manual.pdf", { type: "application/pdf" });
  const metadata = { title: "manual", model_scope: ["M1"], source_metadata: { channel: "local", source_description: "sample" } };
  globalThis.fetch = async (_path, options) => { calls.push(options); if (calls.length === 1) throw new TypeError("lost"); return response({ receipt: "same" }); };
  await assert.rejects(client.uploadDocument("kb", file, metadata), /操作结果未知/);
  const restored = new ApiClient(); restored.setSession("employee-1", "new-csrf");
  await restored.uploadDocument("kb", file, { source_metadata: { source_description: "sample", channel: "local" }, model_scope: ["M1"], title: "manual" });
  assert.equal(calls[0].headers.get("Idempotency-Key"), calls[1].headers.get("Idempotency-Key"));
  assert.equal(calls[1].headers.get("X-CSRF-Token"), "new-csrf");
});
test("new document changed bytes cannot replace an unknown file operation", async () => {
  const client = setup(); let calls = 0;
  globalThis.fetch = async () => { calls++; throw new TypeError("lost"); };
  const metadata = { title: "manual", model_scope: [], source_metadata: { channel: "local", source_description: "sample" } };
  await assert.rejects(client.uploadDocument("kb", new File(["PDF1"], "manual.pdf"), metadata));
  await assert.rejects(client.uploadDocument("kb", new File(["PDF2"], "manual.pdf"), metadata), /相同输入/);
  assert.equal(calls, 1);
});
test("new document changed source cannot replace an unknown upload", async () => {
  const client = setup(); let calls = 0;
  globalThis.fetch = async () => { calls++; throw new TypeError("lost"); };
  const file = new File(["PDF"], "manual.pdf"), metadata = { title: "manual", model_scope: [], source_metadata: { channel: "local", source_description: "sample" } };
  await assert.rejects(client.uploadDocument("kb", file, metadata));
  await assert.rejects(client.uploadDocument("kb", file, { ...metadata, source_metadata: { ...metadata.source_metadata, source_description: "another" } }), /相同输入/);
  assert.equal(calls, 1);
});
test("new document rejects empty and non-PDF selections before dispatch", async () => {
  const client = setup(); let calls = 0;
  globalThis.fetch = async () => { calls++; return response({}); };
  const metadata = { title: "manual", model_scope: [], source_metadata: { channel: "local", source_description: "sample" } };
  for (const file of [new File([], "empty.pdf"), new File(["text"], "manual.txt"), new File(["text"], "manual.pdf", { type: "text/plain" })]) await assert.rejects(client.uploadDocument("kb", file, metadata), /PDF/);
  assert.equal(calls, 0);
});
test("new document session change during file hashing fences upload", async () => {
  const client = setup(); let calls = 0;
  globalThis.fetch = async () => { calls++; return response({}); };
  const previous = client.uploadDocument("kb", new File(["PDF"], "manual.pdf"), { title: "manual", model_scope: [], source_metadata: { channel: "local", source_description: "sample" } });
  client.setSession("employee-2", "csrf-2");
  await assert.rejects(previous, StaleSessionError); assert.equal(calls, 0);
});
test("logical delete uses exact reason and ETag, accepts 204 receipt", async () => {
  const client = setup(); let options;
  globalThis.fetch = async (_path, value) => { options = value; return new Response(null, { status: 204 }); };
  assert.equal(await client.mutate("/documents/one", { reason: "duplicate approved sample" }, '"8"', "DELETE"), null);
  assert.equal(options.method, "DELETE"); assert.equal(options.headers.get("If-Match"), '"8"');
  assert.deepEqual(JSON.parse(options.body), { reason: "duplicate approved sample" });
  assert.equal(client.pendingCommands().length, 0);
});
test("unknown logical delete survives later denial and recovers original ETag/key", async () => {
  const client = setup(), calls = [];
  globalThis.fetch = async (_path, options) => { calls.push(options); if (calls.length === 1) throw new TypeError("lost"); if (calls.length === 2) return response({ detail: "denied" }, 403); return new Response(null, { status: 204 }); };
  await assert.rejects(client.mutate("/documents/one", { reason: "sample lifecycle" }, '"8"', "DELETE"));
  await assert.rejects(client.mutate("/documents/one", { reason: "sample lifecycle" }, '"9"', "DELETE"));
  assert.equal(client.pendingCommands().length, 1);
  await client.recoverCommand(client.pendingCommands()[0]);
  assert.equal(calls[2].headers.get("If-Match"), '"8"');
  assert.equal(calls[0].headers.get("Idempotency-Key"), calls[2].headers.get("Idempotency-Key"));
});

test("index switch sends the selected action and exact previous pointer and recovers with that same fence", async () => {
  setup();
  api.setSession("manager-1", "csrf-index");
  const helperSource = await readFile(new URL("../features/knowledge/indexApi.ts", import.meta.url), "utf8");
  const { switchIndexGeneration } = await import(compileModule(helperSource.replace('"../../api"', JSON.stringify(apiModule))));
  const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    if (calls.length === 1) throw new TypeError("switch reply lost");
    return response({ active_generation_id: "prior", etag: '"updated-pointer-tag"' });
  };
  const base = "/knowledge-bases/kb-1";
  const before = { active_generation_id: "current", content_revision: 17, etag: '"current-pointer-tag"' };
  await assert.rejects(switchIndexGeneration(base, before, "prior", "rollback"), /操作结果未知/);
  const [command] = api.pendingCommands();
  assert.equal(command.path, base + "/active-index");
  assert.deepEqual(JSON.parse(command.replayBody), {
    generation_id: "prior", action: "rollback", expected_active_id: "current", expected_source_revision: 17,
  });
  assert.deepEqual(await api.recoverCommand(command), { active_generation_id: "prior", etag: '"updated-pointer-tag"' });
  for (const { path, options } of calls) {
    assert.equal(path, base + "/active-index");
    assert.equal(options.method, "PUT");
    assert.equal(options.headers.get("If-Match"), before.etag);
    assert.equal(options.headers.get("X-CSRF-Token"), "csrf-index");
  }
  assert.equal(calls[0].options.headers.get("Idempotency-Key"), calls[1].options.headers.get("Idempotency-Key"));
  assert.deepEqual(api.pendingCommands(), []);
});

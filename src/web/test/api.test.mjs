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
test("unknown writes keep identity and reject both repeated and changed writes", async () => {
  const client = setup(), calls = [];
  globalThis.fetch = async (path, options) => { calls.push({path,options}); if (options.method) throw new TypeError("response lost"); return response({state:"completed",method:"POST",path:"/documents/one/publish",resource_refs:{}}); };
  await assert.rejects(client.mutate("/documents/one/publish", {preview_id:"frozen-1"}, '"2"'), /操作结果未知/);
  const restored = new ApiClient(); restored.setSession("employee-1","csrf-new");
  await assert.rejects(restored.mutate("/documents/one/publish",{preview_id:"different"},'"3"'),/原回执/);
  await assert.rejects(restored.mutate("/documents/one/publish",{preview_id:"frozen-1"},'"3"'),/不能重复提交/);
  assert.equal(calls.length,1);
  const command=restored.pendingCommands()[0]; assert.equal(command.match,'"2"');
  await restored.recoverCommand(command); assert.equal(calls[1].options.method,undefined);
  assert.equal(restored.pendingCommands().length,0);
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
test("unknown commands recover via read-only receipt without storing business text", async () => {
  const client = setup(), calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    if (calls.length === 1) throw new TypeError("lost response");
    return response({ state: "completed", method: "POST", path: "/knowledge/queries", resource_refs: { task_id: "same-task" } });
  };
  await assert.rejects(client.mutate("/knowledge/queries", { query: "private business text" }));
  assert.ok(![...sessionStorage.data.values()].join().includes("private business text"));
  const restored = new ApiClient(); restored.setSession("employee-1", "csrf-new");
  const commands = restored.pendingCommands(); assert.equal(commands.length, 1);
  const receipt = await restored.recoverCommand(commands[0]);
  assert.equal(receipt.resource_refs.task_id, "same-task");
  assert.match(calls[1].path, /^\/operation-receipts\?key=/);
  assert.equal(calls[1].options.method, undefined);
  assert.equal(restored.pendingCommands().length, 0);
});
test("absent receipt keeps original identity and never sends another write", async () => {
  const client = setup(); let posts = 0;
  globalThis.fetch = async (_path, options) => {
    if (options.method === "POST") { posts++; throw new TypeError("unknown"); }
    return response({ state: "unresolved", retry_allowed: false, resource_refs: {} });
  };
  await assert.rejects(client.mutate("/knowledge/queries", { query: "one" }));
  const pending = client.pendingCommands()[0];
  await assert.rejects(client.recoverCommand(pending), /不能重复提交/);
  assert.equal(client.pendingCommands()[0].key, pending.key);
  assert.equal(posts, 1);
});
test("denied receipt after unknown keeps the original key across reauthentication", async () => {
  const client=setup(),calls=[]; client.onAccessLost=()=>client.setSession(null,null);
  globalThis.fetch=async(path,options)=>{calls.push({path,options});if(calls.length===1)throw new TypeError("lost");if(calls.length===2)return response({message:"denied"},403);return response({state:"completed",method:"POST",path:"/documents/one/publish",resource_refs:{}});};
  await assert.rejects(client.mutate("/documents/one/publish",{preview_id:"frozen"},'"2"'));
  const original=client.pendingCommands()[0];
  await assert.rejects(client.recoverCommand(original),error=>error instanceof HttpError&&error.status===403);
  client.setSession("employee-1","renewed-csrf");assert.equal(client.pendingCommands()[0].key,original.key);
  await client.recoverCommand(original);
  assert.equal(calls.filter(x=>x.options.method==="POST").length,1);
  assert.equal(calls[1].path,calls[2].path);
});

test("version upload freezes file and metadata; unknown upload is recovered by GET only", async () => {
  const client=setup(),calls=[],file=new File(["synthetic PDF bytes"],"revision.pdf",{type:"application/pdf"});
  globalThis.fetch=async(path,options)=>{calls.push({path,options});if(options.method)throw new TypeError("lost");return response({state:"completed",method:"POST",path:"/documents/doc%2Fone/versions",resource_refs:{}});};
  const metadata={revision_label:"R2",model_scope:"MODEL-A,MODEL-B"};
  await assert.rejects(client.uploadVersion("doc/one",file,metadata),/操作结果未知/);
  await assert.rejects(client.uploadVersion("doc/one",file,{...metadata,revision_label:"R3"}),/原回执/);
  await assert.rejects(client.uploadVersion("doc/one",file,metadata),/不能重复提交/);
  const url=new URL(calls[0].path,"https://local.invalid");assert.equal(url.pathname,"/documents/doc%2Fone/versions");assert.equal(url.searchParams.get("revision_label"),"R2");assert.equal(url.searchParams.get("model_scope"),"MODEL-A,MODEL-B");
  assert.equal(calls[0].options.body.get("metadata"),null);assert.equal(calls[0].options.body.get("file").name,"revision.pdf");assert.equal(calls[0].options.headers.get("Content-Type"),null);
  await client.recoverCommand(client.pendingCommands()[0]);assert.equal(calls.length,2);assert.equal(calls[1].options.method,undefined);
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
test("new document reordered metadata preserves fingerprint but cannot resend unknown upload", async () => {
  const client=setup(),calls=[],file=new File(["PDF"],"manual.pdf",{type:"application/pdf"});
  const metadata={title:"manual",model_scope:["M1"],source_metadata:{channel:"local",source_description:"sample"}};
  globalThis.fetch=async(path,options)=>{calls.push({path,options});if(options.method)throw new TypeError("lost");return response({state:"completed",method:"POST",path:"/knowledge-bases/kb/documents",resource_refs:{}});};
  await assert.rejects(client.uploadDocument("kb",file,metadata),/操作结果未知/);
  const restored=new ApiClient();restored.setSession("employee-1","new-csrf");
  await assert.rejects(restored.uploadDocument("kb",file,{source_metadata:{source_description:"sample",channel:"local"},model_scope:["M1"],title:"manual"}),/不能重复提交/);
  assert.equal(calls.length,1);await restored.recoverCommand(restored.pendingCommands()[0]);assert.equal(calls[1].options.method,undefined);
});

test("new document changed bytes cannot replace an unknown file operation", async () => {
  const client = setup(); let calls = 0;
  globalThis.fetch = async () => { calls++; throw new TypeError("lost"); };
  const metadata = { title: "manual", model_scope: [], source_metadata: { channel: "local", source_description: "sample" } };
  await assert.rejects(client.uploadDocument("kb", new File(["PDF1"], "manual.pdf"), metadata));
  await assert.rejects(client.uploadDocument("kb", new File(["PDF2"], "manual.pdf"), metadata), /原回执/);
  assert.equal(calls, 1);
});
test("new document changed source cannot replace an unknown upload", async () => {
  const client = setup(); let calls = 0;
  globalThis.fetch = async () => { calls++; throw new TypeError("lost"); };
  const file = new File(["PDF"], "manual.pdf"), metadata = { title: "manual", model_scope: [], source_metadata: { channel: "local", source_description: "sample" } };
  await assert.rejects(client.uploadDocument("kb", file, metadata));
  await assert.rejects(client.uploadDocument("kb", file, { ...metadata, source_metadata: { ...metadata.source_metadata, source_description: "another" } }), /原回执/);
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
test("unknown logical delete cannot resend and retains its original ETag until receipt", async () => {
 const client=setup(),calls=[];
 globalThis.fetch=async(path,options)=>{calls.push({path,options});if(options.method)throw new TypeError("lost");return response({state:"completed",method:"DELETE",path:"/documents/one",resource_refs:{}});};
 await assert.rejects(client.mutate("/documents/one",{reason:"sample lifecycle"},'"8"',"DELETE"));
 await assert.rejects(client.mutate("/documents/one",{reason:"sample lifecycle"},'"9"',"DELETE"),/不能重复提交/);
 const command=client.pendingCommands()[0];assert.equal(command.match,'"8"');await client.recoverCommand(command);
 assert.equal(calls.length,2);assert.equal(calls[1].options.method,undefined);assert.equal(client.pendingCommands().length,0);
});

test("index switch freezes the original pointer and resolves its receipt without repeating PUT", async () => {
  setup(); api.setSession("manager-1", "csrf-index");
  const helperSource = await readFile(new URL("../features/knowledge/indexApi.ts", import.meta.url), "utf8");
  const { switchIndexGeneration } = await import(compileModule(helperSource.replace('"../../api"', JSON.stringify(apiModule))));
  const calls = [], base = "/knowledge-bases/kb-1";
  globalThis.fetch = async (path, options) => { calls.push({ path, options }); if (calls.length === 1) throw new TypeError("switch reply lost"); return response({ state: "completed", method: "PUT", path: base + "/active-index", resource_refs: {} }); };
  const before = { active_generation_id: "current", content_revision: 17, etag: '\"current-pointer-tag\"' };
  await assert.rejects(switchIndexGeneration(base, before, "prior", "rollback"), /操作结果未知/);
  const [command] = api.pendingCommands();
  assert.equal(command.replayBody, undefined);
  assert.deepEqual(JSON.parse(calls[0].options.body), { generation_id: "prior", action: "rollback", expected_active_id: "current", expected_source_revision: 17 });
  assert.equal(calls[0].options.headers.get("If-Match"), before.etag);
  assert.equal(calls[0].options.headers.get("X-CSRF-Token"), "csrf-index");
  await api.recoverCommand(command);
  assert.equal(calls[1].options.method, undefined);
  assert.match(calls[1].path, /^\/operation-receipts/);
  assert.deepEqual(api.pendingCommands(), []);
});

test("late binary denial from an old session cannot clear the new session",async()=>{
 const client=setup();let resolve;let cleared=0;client.onAccessLost=()=>cleared++;
 globalThis.fetch=()=>new Promise(r=>resolve=r);
 const previous=client.blob("/original");client.setSession("employee-2","csrf-2");resolve(response({message:"denied"},404));
 await assert.rejects(previous,StaleSessionError);assert.equal(cleared,0);
});
test("receipt identity mismatch keeps pending identity and never permits another mutation",async()=>{
 const client=setup();let writes=0;
 globalThis.fetch=async(_path,options)=>{if(options.method){writes++;throw new TypeError("lost");}return response({state:"completed",method:"POST",path:"/wrong",resource_refs:{}});};
 await assert.rejects(client.mutate("/knowledge/queries",{query:"one"}));const original=client.pendingCommands()[0];
 await assert.rejects(client.recoverCommand(original),/身份不匹配/);
 assert.equal(client.pendingCommands()[0].key,original.key);assert.equal(writes,1);
});

test("success headers with truncated JSON keep unknown identity and cannot create another task",async()=>{
 const client=setup();let writes=0;
 globalThis.fetch=async(_path,options)=>{
   if(options.method){writes++;return {ok:true,status:202,headers:new Headers(),json:async()=>{throw new TypeError("body truncated");}};}
   return response({state:"completed",method:"POST",path:"/knowledge/queries",resource_refs:{task_id:"original"}});
 };
 await assert.rejects(client.mutate("/knowledge/queries",{query:"one"}),/回执不完整/);
 const original=client.pendingCommands()[0];assert.ok(original.unknownOutcome);
 await assert.rejects(client.mutate("/knowledge/queries",{query:"one"}),/不能重复提交/);
 assert.equal(writes,1);await client.recoverCommand(original);assert.equal(client.pendingCommands().length,0);
});
test("successful query JSON without task identity remains unknown",async()=>{
 for(const data of [null,{}]){
  const client=setup();globalThis.fetch=async()=>response(data,202);
  await assert.rejects(client.mutate("/knowledge/queries",{query:"one"}),/缺少任务身份/);
  assert.equal(client.pendingCommands().length,1);
 }
});
test("JSON and binary reads bind to the originating page session",async()=>{
 const client=setup(),calls=[];
 globalThis.fetch=async(_path,options)=>{calls.push(options);return response({ok:true});};
 await client.get("/me");await client.blob("/original");
 for(const options of calls)assert.equal(new Headers(options.headers).get("X-Workspace-CSRF"),"csrf-1");
});

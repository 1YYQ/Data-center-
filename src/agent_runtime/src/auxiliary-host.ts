/** Single-use worker-owned SDK host. It never opens the answer journal. */
import { readFile } from "node:fs/promises";
import { PI_AI_VERSION, type AssistantMessage } from "@earendil-works/pi-ai";
import { SessionManager, VERSION } from "@earendil-works/pi-coding-agent";
import { createRuntimeSession } from "./pi00-session.js";
import { createControlledProvider, sha } from "./controlled-provider.js";

async function main() {
  const input: Buffer[] = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 32_768) throw new Error("Invalid auxiliary host input");
    input.push(chunk);
  }
  const config = JSON.parse(Buffer.concat(input).toString("utf8"));
  const operation = config.operation;
  const manifest = config.manifest;
  if ((operation !== "summary" && operation !== "compaction") || manifest.node_version !== process.version
      || manifest.platform !== process.platform || manifest.arch !== process.arch
      || PI_AI_VERSION !== "1.5.2" || VERSION !== "1.5.2"
      || !/^[a-f0-9]{64}$/.test(config.configuration_hash)) {
    throw new Error("Incompatible auxiliary host");
  }
  for (const name of ["auxiliary-host.js", "pi00-session.js", "controlled-provider.js"]) {
    if (manifest.built_files["dist/" + name] !== sha(await readFile(new URL(name, import.meta.url)))) {
      throw new Error("Auxiliary host differs from the locked artifacts");
    }
  }
  const cwd = process.cwd();
  const stop = new AbortController();
  const wire = createControlledProvider({ gateUrl: config.gate_url, runId: config.run_id, token: config.token,
    configurationHash: config.configuration_hash, generation: config.generation,
    fixedGeneration: true, stopSignal: stop.signal, operation });
  const manager = SessionManager.inMemory(cwd);
  if (operation === "compaction") {
    // Manual compaction needs enough in-memory turns to establish a cut. These
    // placeholders carry no answer history and the provider projects neither.
    const placeholder = "A turn is represented by the checked projection. ".repeat(80);
    for (let index = 0; index < 4; index++) {
      manager.appendMessage({ role: "user", content: [{ type: "text", text: placeholder }], timestamp: index * 2 });
      // The short reply leaves the retained-token cut at a user-turn boundary.
      manager.appendMessage({ role: "assistant", content: [{ type: "text", text: "Turn acknowledged." }],
        api: wire.model.api, provider: wire.model.provider, model: wire.model.id, stopReason: "stop",
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } }, timestamp: index * 2 + 1 });
    }
  }
  const { session } = await createRuntimeSession({ cwd, agentDir: cwd, ...wire, sessionManager: manager });
  try {
    if (operation === "summary") {
      await session.prompt("Organize the current verified support according to the auxiliary summary contract.");
      const reply = session.agent.state.messages.at(-1) as AssistantMessage | undefined;
      if (reply?.role !== "assistant" || reply.stopReason !== "stop") throw new Error("Auxiliary summary did not settle");
    } else {
      await session.compact("Retain the exact checked work items and continuity references in the auxiliary contract.");
    }
    process.stdout.write(JSON.stringify({ state: "settled", operation }) + "\n");
  } finally {
    stop.abort();
    session.dispose();
  }
}

void main().catch(() => { process.exitCode = 2; });

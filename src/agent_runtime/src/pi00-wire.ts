/** Loopback-only SDK adapter probe. Not a production model-egress service. */
import { createHash } from "node:crypto";
import {
  createAssistantMessageEventStream, createProvider, type Model, type ProviderStreams,
  type TranscriptContext,
} from "@earendil-works/pi-ai";
import { streamSimple as nativeStream } from "@earendil-works/pi-ai/api/openai-completions";

export type ProbePurpose = "answer" | "compaction" | "summary";
const digest = (body: string) => createHash("sha256").update(body).digest("hex");

export function createPI00WireProvider(input: { gateUrl: string; key: string; purpose: () => ProbePurpose }) {
  const gate = new URL(input.gateUrl);
  if (gate.protocol !== "http:" || gate.hostname !== "127.0.0.1" || !gate.port || gate.pathname !== "/" || gate.search || gate.hash || gate.username || gate.password) {
    throw new Error("PI00 wire probe requires an explicit loopback gate");
  }
  const base = gate.origin;
  const nativeModel = Object.freeze<Model<"openai-completions">>({
    id: "pi00-synthetic", name: "PI00 synthetic HTTP fixture", provider: "data-center-pi00",
    api: "openai-completions", baseUrl: base + "/v1", reasoning: false,
    input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 8192, maxTokens: 512,
  });
  async function post(path: string, body: unknown, signal?: AbortSignal) {
    const response = await fetch(base + path, {
      method: "POST", headers: { "Content-Type": "application/json", "X-PI00-Key": input.key },
      body: JSON.stringify(body), redirect: "error", signal,
    });
    if (!response.ok) throw new Error(`PI00 gate rejected ${path}: ${response.status}`);
    return response.json();
  }
  // SDK contexts and arbitrary callbacks/options are deliberately not trusted.
  // Only the host purpose and abort signal enter this synthetic probe path.
  const stream: ProviderStreams["streamSimple"] = (_model, _context, options) => {
    const output = createAssistantMessageEventStream();
    void (async () => {
      try {
        const purpose = input.purpose();
        const projection = await post("/context/project", { purpose }, options?.signal);
        if (projection.purpose !== purpose || digest(projection.context_json) !== projection.context_digest) throw new Error("PI00 projection mismatch");
        const context: TranscriptContext = JSON.parse(projection.context_json);
        const transport: typeof fetch = async (url, init) => {
          if (String(url) !== base + "/v1/chat/completions" || typeof init?.body !== "string") throw new Error("PI00 transport mismatch");
          const admission = await post("/model/authorize", {
            purpose, projection_id: projection.id, context_digest: projection.context_digest,
            adapter: "pi-ai-1.5.2/openai-completions", payload_digest: digest(init.body),
          }, options?.signal);
          return fetch(base + "/v1/chat/completions", {
            method: "POST", body: init.body, redirect: "error", signal: options?.signal,
            headers: { "Content-Type": "application/json", "X-PI00-Key": input.key, "X-Model-Call": admission.id },
          });
        };
        const response = nativeStream(nativeModel, context, {
          apiKey: "synthetic-adapter-key", fetch: transport, signal: options?.signal,
          maxRetries: 0, maxRetryDelayMs: 0, timeoutMs: 5000, cacheRetention: "none", maxTokens: 128,
        });
        for await (const event of response) output.push(event);
      } catch {
        // No raw context, key or remote error body is emitted in a safe error.
        output.push({ type: "error", reason: "error", error: {
          role: "assistant", content: [], api: nativeModel.api, provider: nativeModel.provider, model: nativeModel.id,
          usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
          stopReason: "error", errorMessage: "PI00 controlled model call failed", timestamp: Date.now(),
        } });
      }
    })();
    return output;
  };
  const api: ProviderStreams = { stream, streamSimple: stream };
  const provider = createProvider({ id: "data-center-pi00", models: [nativeModel],
    auth: { apiKey: { name: "PI00 synthetic", resolve: async () => ({ auth: {} }) } }, api });
  return { provider, model: nativeModel };
}

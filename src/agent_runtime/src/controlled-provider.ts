/** Fixed native adapter -> application-owned projection/admission/forward gate. */
import { createHash, randomUUID } from "node:crypto";
import { createAssistantMessageEventStream, createProvider, normalizeContext, type Context, type Model, type ProviderStreams } from "@earendil-works/pi-ai";
import { streamSimple as nativeStream } from "@earendil-works/pi-ai/api/openai-completions";

export const sha = (body: string | Buffer) => createHash("sha256").update(body).digest("hex");

const ADAPTER = "pi-ai-1.5.2/openai-completions";
const CAPABILITIES = ["text", "function_tools", "sse", "stream_usage"];
const BAILIAN_WIRE = "bailian-beijing-qwen38-flash-v1";
// The timeout includes private-gateway authorization and source replays.
export const GATEWAY_REQUEST_TIMEOUT_MS = 20000;
const BAILIAN_GATEWAY_TIMEOUT_MS = 25000;

export interface GenerationProfile {
  model: string;
  adapter: string;
  service_adapter: string;
  api_family: string;
  tokenizer: string;
  context_window: number;
  max_output_tokens: number;
  capabilities: string[];
  wire_protocol?: typeof BAILIAN_WIRE;
}

export function checkGeneration(profile: GenerationProfile) {
  const bailian = profile?.wire_protocol === BAILIAN_WIRE;
  if (!profile || typeof profile.model !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$/.test(profile.model)
      || profile.adapter !== ADAPTER || profile.service_adapter !== "openai-chat-v1"
      || profile.api_family !== "openai-chat-completions-sse-v1" || profile.tokenizer !== "utf8-byte-ceiling-v1"
      || !Number.isInteger(profile.context_window) || profile.context_window < 4096 || profile.context_window > 65536
      || !Number.isInteger(profile.max_output_tokens) || profile.max_output_tokens < 1
      || profile.max_output_tokens > Math.min(4096, Math.floor(profile.context_window / 4))
      || JSON.stringify(profile.capabilities) !== JSON.stringify(CAPABILITIES)
      || Object.keys(profile).length !== (bailian ? 9 : 8)
      || (bailian ? profile.model !== "qwen3.8-flash" || profile.context_window !== 32768 || profile.max_output_tokens !== 2048
        : profile.wire_protocol !== undefined)) {
    throw new Error("Incompatible controlled generation profile");
  }
  return profile;
}

export function createControlledProvider(input: {
  gateUrl: string; runId: string; token: string; configurationHash: string; generation: GenerationProfile;
  fixedGeneration: boolean; stopSignal: AbortSignal; operation?: "answer" | "summary" | "compaction";
}) {
  const base = new URL(input.gateUrl);
  if (base.protocol !== "http:" || base.hostname !== "127.0.0.1" || !base.port || base.pathname !== "/" || base.search || base.hash || base.username || base.password || !/^[0-9a-f-]{36}$/.test(input.runId)) {
    throw new Error("Controlled provider requires the exact private loopback gate");
  }
  const prefix = `${base.origin}/private/runtime/runs/${input.runId}`;
  function nativeModel(profile: GenerationProfile): Model<"openai-completions"> {
    const bailian = profile.wire_protocol === BAILIAN_WIRE;
    return Object.freeze<Model<"openai-completions">>({
      id: profile.model, name: profile.model, provider: "data-center-local",
      api: "openai-completions", baseUrl: prefix + "/models", reasoning: false, input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: profile.context_window, maxTokens: profile.max_output_tokens,
      compat: { supportsStore: !bailian, supportsUsageInStreaming: true, supportsStrictMode: false,
        supportsOpenAIGrammarTools: false, maxTokensField: bailian ? "max_tokens" : "max_completion_tokens" },
    });
  }
  const model = nativeModel(checkGeneration(input.generation));
  let auxiliaryStarted = false;
  async function post(path: string, data: unknown, signal?: AbortSignal) {
    const response = await fetch(prefix + path, { method: "POST", headers: { "Content-Type": "application/json", "X-Runtime-Token": input.token },
      body: JSON.stringify(data), redirect: "error", signal });
    if (!response.ok) throw new Error(`Controlled model gate refused request (${response.status})`);
    return response.json();
  }
  const stream: ProviderStreams["streamSimple"] = (requestedModel, _sdkContext, options) => {
    const output = createAssistantMessageEventStream();
    void (async () => {
      try {
        const operation = input.operation ?? "answer";
        if (operation !== "answer") {
          if (auxiliaryStarted) throw new Error("Auxiliary execution permits one model call");
          auxiliaryStarted = true;
        }
        const timeout = input.generation.wire_protocol === BAILIAN_WIRE ? BAILIAN_GATEWAY_TIMEOUT_MS : GATEWAY_REQUEST_TIMEOUT_MS;
        const signals = [input.stopSignal, AbortSignal.timeout(timeout)];
        if (options?.signal) signals.push(options.signal);
        const signal = AbortSignal.any(signals);
        signal.throwIfAborted();
        if (requestedModel.id !== model.id || requestedModel.provider !== model.provider) throw new Error("Model mismatch");
        const projection = await post("/context-project", { operation, business_purpose: "answer" }, signal);
        if (projection.operation !== operation || projection.business_purpose !== "answer"
            || projection.profile_configuration_hash !== input.configurationHash
            || sha(projection.context_json) !== projection.context_digest) throw new Error("Projection mismatch");
        const chosen = checkGeneration(projection.generation);
        if (input.fixedGeneration && Object.entries(input.generation).some(([key, value]) =>
          JSON.stringify(chosen[key as keyof GenerationProfile]) !== JSON.stringify(value))) throw new Error("Generation profile mismatch");
        const projected: Context = JSON.parse(projection.context_json);
        const context = normalizeContext(projected);
        const bailian = chosen.wire_protocol === BAILIAN_WIRE;
        if (bailian && (projected.tools?.length ?? 0) > 1) throw new Error("Unexpected Bailian tool scope");
        const tool = projected.tools?.[0];
        const serverOwnedArguments = bailian && (tool?.name === "dc_evidence_search" || tool?.name === "dc_artifacts_validate");
        let dispatched = false;
        const transport: typeof fetch = async (url, init) => {
          const transportSignal = init?.signal ? AbortSignal.any([signal, init.signal]) : signal;
          transportSignal.throwIfAborted();
          if (dispatched || String(url) !== prefix + "/models/chat/completions" || typeof init?.body !== "string") throw new Error("Unexpected adapter transport");
          if (Buffer.byteLength(init.body) + chosen.max_output_tokens > chosen.context_window) throw new Error("Generation context budget exceeded");
          const admission = await post("/model-authorizations", { projection_id: projection.id, context_digest: projection.context_digest,
            adapter: ADAPTER, payload_digest: sha(init.body), request_key: randomUUID() }, transportSignal);
          if (admission.state !== "reserved") throw new Error("Model call already has an outcome; reconcile first");
          dispatched = true;
          return fetch(prefix + "/models/chat/completions", { method: "POST", headers: { "Content-Type": "application/json", "X-Runtime-Token": input.token, "X-Model-Call": admission.id },
            body: init.body, redirect: "error", signal: transportSignal });
        };
        // Provider options are added before the wire-byte hash is authorized.
        // Caller options and SDK history never reach the adapter.
        for await (const event of nativeStream(nativeModel(chosen), context, { apiKey: "private-gate", fetch: transport, signal,
          maxRetries: 0, maxRetryDelayMs: 0, timeoutMs: timeout,
          cacheRetention: "none", maxTokens: chosen.max_output_tokens,
          onPayload: bailian ? (payload) => ({ ...payload as object, enable_thinking: false, preserve_thinking: false,
            ...(tool && { tool_choice: { type: "function", function: { name: tool.name } }, parallel_tool_calls: false }) }) : undefined })) {
          if (serverOwnedArguments && event.type === "done") {
            const calls = event.message.content.filter(block => block.type === "toolCall");
            if (event.reason !== "toolUse" || calls.length !== 1 || calls[0].name !== tool?.name || calls[0].argumentsError) {
              throw new Error("Unexpected Bailian tool for the projected operation");
            }
            // The gateway has attested the raw tool call. Search and validation
            // take their inputs exclusively from the frozen server projection.
            const content = event.message.content.map(block => block === calls[0] ? { ...calls[0], arguments: {} } : block);
            output.push({ ...event, message: { ...event.message, content } });
          } else {
            output.push(event);
          }
        }
      } catch {
        output.push({ type: "error", reason: "error", error: { role: "assistant", content: [], api: model.api, provider: model.provider, model: model.id,
          usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
          stopReason: "error", errorMessage: "Controlled model call failed; inspect the durable receipt", timestamp: Date.now() } });
      }
    })();
    return output;
  };
  return { model, provider: createProvider({ id: model.provider, models: [model], auth: { apiKey: { name: "Private runtime gate", resolve: async () => ({ auth: {} }) } },
    api: { stream, streamSimple: stream } }) };
}

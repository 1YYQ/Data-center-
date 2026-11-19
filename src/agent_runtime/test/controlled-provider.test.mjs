import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { validateToolArguments } from "@earendil-works/pi-ai";
import { createControlledProvider, sha } from "../dist/controlled-provider.js";

test("Bailian replay keeps server-owned tool inputs and preserves the model's delivery arguments", async () => {
  const generation = {
    model: "qwen3.8-flash", adapter: "pi-ai-1.5.2/openai-completions", service_adapter: "openai-chat-v1",
    api_family: "openai-chat-completions-sse-v1", tokenizer: "utf8-byte-ceiling-v1", context_window: 32768,
    max_output_tokens: 2048, capabilities: ["text", "function_tools", "sse", "stream_usage"],
    wire_protocol: "bailian-beijing-qwen38-flash-v1",
  };
  const runId = "a".repeat(36);
  const configurationHash = "b".repeat(64);
  const noArguments = { type: "object", properties: {}, additionalProperties: false };
  const plan = { schema_version: "answer-plan/1", sections: [{ heading_code: "operation", items: [] }] };
  const observedSearchArguments = { question: "SYNTHETIC-1 startup and pressure", top_k: "10" };
  const cases = [
    ["dc_evidence_search", noArguments, observedSearchArguments, {}],
    ["dc_artifacts_validate", noArguments, { evidence_ids: ["invented-ref"] }, {}],
    ["dc_artifacts_deliver", { type: "object", properties: { answer_plan: { type: "object" } }, required: ["answer_plan"] },
      { answer_plan: plan }, { answer_plan: plan }],
  ];
  let replay;
  const server = createServer(async (request, response) => {
    for await (const _ of request) { /* Drain the real SDK request body. */ }
    const [name, parameters, modelArgs] = replay;
    const route = request.url.split("/").at(-1);
    if (route === "context-project") {
      const context = JSON.stringify({ systemPrompt: "Synthetic SDK replay", tools: [{ name, parameters, description: name }],
        messages: [{ role: "user", content: "Synthetic only", timestamp: 0 }] });
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({ id: "synthetic-projection", context_json: context, context_digest: sha(context),
        operation: "answer", business_purpose: "answer", profile_configuration_hash: configurationHash, generation }));
    } else if (route === "model-authorizations") {
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({ id: "synthetic-call", state: "reserved" }));
    } else {
      const base = { id: "chatcmpl-replay", object: "chat.completion.chunk", created: 1, model: generation.model };
      const args = JSON.stringify(modelArgs);
      const frames = [
        { ...base, choices: [{ index: 0, finish_reason: null, delta: { role: "assistant", tool_calls: null } }] },
        { ...base, choices: [{ index: 0, finish_reason: null, delta: { tool_calls: [{ index: 0, id: "call_t", type: "function",
          function: { name, arguments: args.slice(0, 7) } }] } }] },
        { ...base, choices: [{ index: 0, finish_reason: null, delta: { tool_calls: [{ index: 0, id: null,
          function: { name: null, arguments: args.slice(7) } }] } }] },
        { ...base, choices: [{ index: 0, finish_reason: "tool_calls", delta: { tool_calls: null } }] },
        { ...base, choices: [], usage: { prompt_tokens: 572, completion_tokens: 28, total_tokens: 600 } },
      ];
      response.setHeader("content-type", "text/event-stream");
      response.end(frames.map(frame => "data: " + JSON.stringify(frame) + "\n\n").join("") + "data: [DONE]\n\n");
    }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    const wire = createControlledProvider({ gateUrl: `http://127.0.0.1:${server.address().port}`, runId,
      token: "synthetic-private-token", configurationHash, generation, fixedGeneration: true,
      stopSignal: new AbortController().signal });
    for (replay of cases) {
      const [name, parameters, _, expected] = replay;
      const stream = wire.provider.streamSimple(wire.model, { messages: [] });
      const message = await stream.result();
      assert.equal(message.stopReason, "toolUse");
      const toolCall = message.content.find(block => block.type === "toolCall");
      assert.equal(toolCall.name, name);
      assert.deepEqual(validateToolArguments({ name, parameters }, toolCall), expected);
    }
  } finally {
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  }
});

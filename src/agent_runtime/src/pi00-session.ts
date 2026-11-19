/** Fixed SDK construction for the isolated probe and private runtime. */
import { join } from "node:path";
import {
  createAgentSession, DefaultResourceLoader, ModelRuntime,
  SessionManager, SettingsManager, type ToolDefinition,
} from "@earendil-works/pi-coding-agent";
import { InMemoryCredentialStore, type Api, type Model, type Provider } from "@earendil-works/pi-ai";

export const SESSION_SETTINGS = {
  compaction: { enabled: false, reserveTokens: 128, keepRecentTokens: 128 },
  retry: { enabled: false, maxRetries: 0, provider: { maxRetries: 0 } },
  cacheWarming: "off" as const,
};
export const SESSION_RESOURCES = {
  extensions: [], skills: [], prompts: [], themes: [], context_files: [], builtin_tools: [],
  disabled_builtin_extensions: ["mcp", "codemode"] as const,
  system_prompt: "Controlled local execution. Business context comes only from the private authorized projection.",
};

export interface ProbeOptions {
  cwd: string;
  agentDir: string;
  provider: Provider;
  model: Model<Api>;
  tools?: ToolDefinition[];
  sessionManager?: SessionManager;
  waiting?: () => boolean;
}

export async function createPI00Session(options: ProbeOptions) {
  // This entry point deliberately cannot be used with a real provider.
  if (options.provider.id !== "data-center-pi00" || options.model.provider !== options.provider.id) {
    throw new Error("PI00 requires the explicit synthetic provider");
  }
  return createLockedSession(options);
}

/** Local business runner entry; its provider can reach only the private gate. */
export async function createRuntimeSession(options: ProbeOptions) {
  const address = new URL(options.model.baseUrl);
  if (options.provider.id !== "data-center-local" || options.model.provider !== options.provider.id || address.protocol !== "http:" || address.hostname !== "127.0.0.1") {
    throw new Error("Runtime requires the controlled local provider");
  }
  return createLockedSession(options);
}

async function createLockedSession(options: ProbeOptions) {
  const settings = SettingsManager.inMemory(structuredClone(SESSION_SETTINGS));
  const resources = new DefaultResourceLoader({
    cwd: options.cwd,
    agentDir: options.agentDir,
    settingsManager: settings,
    noExtensions: true,
    disabledBuiltinExtensions: [...SESSION_RESOURCES.disabled_builtin_extensions],
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    systemPrompt: SESSION_RESOURCES.system_prompt,
    agentsFilesOverride: () => ({ agentsFiles: [] }),
    appendSystemPromptOverride: () => [],
  });
  await resources.reload();
  if (resources.getExtensions().extensions.length || resources.getExtensions().errors.length
      || resources.getSkills().skills.length || resources.getPrompts().prompts.length
      || resources.getThemes().themes.length || resources.getAgentsFiles().agentsFiles.length
      || resources.getAppendSystemPrompt().length || resources.getSystemPrompt() !== SESSION_RESOURCES.system_prompt
      || settings.getCompactionEnabled() || settings.getRetryEnabled() || settings.getRetrySettings().maxRetries !== 0
      || settings.getProviderRetrySettings().maxRetries !== 0 || settings.getCacheWarmingMode() !== "off"
      || settings.getCompactionReserveTokens(options.model) !== SESSION_SETTINGS.compaction.reserveTokens
      || settings.getCompactionKeepRecentTokens(options.model) !== SESSION_SETTINGS.compaction.keepRecentTokens) {
    throw new Error("Installed SDK did not enforce the locked settings and resources");
  }
  const runtime = await ModelRuntime.create({
    credentials: new InMemoryCredentialStore(),
    modelsPath: null,
    modelsStorePath: join(options.agentDir, "models-cache.json"),
    allowModelNetwork: false,
    refreshOnCreate: false,
  });
  runtime.registerNativeProvider(options.provider);
  const tools = options.tools ?? [];
  const { session } = await createAgentSession({
    cwd: options.cwd,
    agentDir: options.agentDir,
    modelRuntime: runtime,
    model: options.model,
    scopedModels: [{ model: options.model, thinkingLevel: "off" }],
    thinkingLevel: "off",
    noTools: "builtin",
    tools: tools.map((tool) => tool.name),
    customTools: tools,
    settingsManager: settings,
    resourceLoader: resources,
    sessionManager: options.sessionManager ?? SessionManager.inMemory(options.cwd),
  });
  if (JSON.stringify(session.agent.state.tools.map((tool) => tool.name)) !== JSON.stringify(tools.map((tool) => tool.name))) {
    session.dispose();
    throw new Error("Installed SDK did not enforce the locked tool set");
  }
  // Keep the SDK's installed boundary hook; end only after tool results settle.
  const sdkFinish = session.agent.finishTurn;
  session.agent.finishTurn = async (turn, signal) => {
    const decision = await sdkFinish?.call(session.agent, turn, signal);
    if (options.waiting?.()) return { action: "end" };
    if (decision) return decision;
    return undefined;
  };
  return { session, settings, resources, runtime };
}

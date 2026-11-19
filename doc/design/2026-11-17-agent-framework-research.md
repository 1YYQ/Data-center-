# Agent 开源框架调研：Pi、DeepSeek Harness、Grok Build

日期：2026-11-17。初稿状态：研究建议，未做本项目集成实测。同日后续用户已确定Pi基底；现行决定及具体设计见 [Pi接入方案](../architecture/Pi-Agent接入与改造方案.md)。本文比较保留为当时的选型依据，不再表示框架方向待定。

范围：核对三个候选的官方文档与公开源代码说明，判断可复用边界；没有安装、运行框架，没有向外部服务发送企业资料，没有修改架构、任务状态或业务代码。下列链接指向检索时的主分支，并非锁定的生产版本。

## 判断

建议优先复用开源 Agent 运行时，通过 SDK、工具接口和扩展机制接入现有业务；有明确扩展缺口时才考虑修改上游源码。当前阶段不值得为通用模型循环、流式事件、会话与工具调度重新建设完整框架。

项目价值仍由自己的业务服务保证：文档发布与适用版本、当前用户权限、证据定位、警告完整性、确定性计算和成果核验。运行时可以帮助执行这些能力，但不能凭其开源、会话持久化或工具审批功能推导业务正确性。项目现状是已有 20 项工具设计、尚未统一注册和验收，见 [STATUS](../planning/STATUS.md)。

在本次三者中，Pi 适合优先验证轻量嵌入；DeepSeek Harness 适合验证更完整的插件与执行能力是否能减少实际集成工作；Grok Build 更适合作为编码工具和工程设计参考，暂不作为企业文档办公运行时首选。这是基于公开接口与项目约束的推断，不是性能、工期或稳定性测试结果。

## 官方事实与适配判断

| 候选 | 已核对的事实 | 对本项目的含义 |
| --- | --- | --- |
| Pi | 原 `badlogic/pi-mono` 已重定向 `earendil-works/pi`，MIT；当前包名为 `@earendil-works/pi-*`。`pi-ai` 提供多供应商模型接口，`pi-agent-core` 提供工具执行、状态及事件；CLI 与可嵌入 TypeScript SDK 分开存在。[仓库](https://github.com/earendil-works/pi)、[agent-core](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)、[SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md) | 可以复用库而不继承整个终端产品。Python 后端需评估 Node 子进程/RPC 或额外运行时的部署与隔离成本，不能只看 Agent 循环代码量。 |
| DeepSeek Harness | MIT，Cordis 插件架构；模型适配、工具、会话日志及 Agent 循环可替换，提供 `web/headless/sdk/sdk-minimal/acp` 配置。Python SDK 通过 stdio JSON-RPC 驱动配套 `dsh` 子进程；多模型适配支持兼容网关、自托管服务。[README](https://github.com/deepseek-ai/deepseek-harness)、[架构](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)、[Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/README.md)、[模型适配](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-pi-ai/README.md) | 更完整的现成执行能力值得比较，不能因为现有后端是 Python 就排除；同时引入插件、配置及子进程生命周期等维护责任。 |
| Grok Build | Apache-2.0，Rust 编码 Agent/CLI/TUI；支持 headless、ACP 及自定义应用接入。可配置其他供应商或本地模型，不能描述为只能调用 Grok。仓库定期从内部 monorepo 同步，当前不接受外部贡献。[README](https://github.com/xai-org/grok-build)、[ACP](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/15-agent-mode.md)、[自定义模型](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/11-custom-models.md) | 可集成不等于最适合本项目。其文件、终端及代码工作区导向与当前业务有距离；直接 fork 会增加维护一套外部同步 Rust 产品的成本。 |

## 决定能否采用的约束

**成熟度不能一概而论。** Pi 已提供 `pi-durable`，具备持久化任务与恢复能力，不能沿用“只有极简循环”的旧印象；但该包明确标注 Experimental，API 可无通知变更。DeepSeek Harness README 明示 developer preview 与兼容破坏，安全说明明确尚未安全审计、不能视为生产就绪或唯一隔离控制。[Pi durable](https://github.com/earendil-works/pi/blob/main/packages/durable/README.md)、[DSH 安全说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/SAFETY.md)

**工具许可不等于资料权限。** Pi 根 README 明确没有内置文件系统、进程、网络和凭据访问限制，默认继承启动进程权限；需要宿主沙箱。DSH 有受保护的工具执行管线，Grok 有审批规则和可选操作系统沙箱，但这些功能不能证明本项目的文档、版本、缓存和分享撤权已被正确执行。[Pi 权限说明](https://github.com/earendil-works/pi#permissions--containerization)、[DSH 工具管线](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/tools.md)、[Grok 权限](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/22-permissions-and-safety.md)

**默认数据流需要单独核对。** DSH 官方 `session-log-deepseek` 文档写明：标准配置挂载该插件，`enabled` 默认 `true`，使用官方模型 API 时增量发送规范化会话日志；关闭后重新开启会继续发送未被确认接收的日志，包括关闭期间记录的事件。base 文档另述 OTel 默认 `FEEDBACK_ONLY`，反馈、评分及其修改/撤回可释放至该事件的完整上下文前缀。这些是不同通道，不能仅关闭一个开关或改模型地址就声称禁止了企业资料外发。[会话日志上传](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-log-deepseek/README.md)、[base 默认行为](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/base/README.md)

**恢复语义必须与业务副作用分开。** Pi durable 文档明确，具有外部副作用的 replay-safe 工具仍需自身实现幂等。DSH 会话恢复保留完整日志，为未完成工具补记中断结果并闭合中断 turn；这不等于自动完成原业务任务。Grok 自动保存会话并支持恢复，也不能由此推出写数据库、修改文件或发布分享恰好执行一次。[Pi 工具恢复](https://github.com/earendil-works/pi/blob/main/packages/durable/README.md)、[DSH 持久化](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/persistence.md)、[Grok 会话](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/17-sessions.md)

## 后续验证建议（未执行、未据此启动开发）

以相同获准非敏感样本、模型和业务工具比较候选：带版本和页序号的资料查询；缺项等待补充；受控计算/表格成果核验；进程中断后恢复且不重复写入；权限撤回及缓存隔离。同时观察真实网络目标、会话与日志内容、工具调用轨迹、耗时/用量和接入维护工作。

采用的判据是：通过 SDK/插件实现完整用户任务且保留强制业务边界，所节省的通用开发工作大于适配、部署、上游升级和数据治理成本。当前没有可支持“节省某个百分比工期”“某框架在本项目最快”或“已可上线”的运行证据。

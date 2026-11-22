# 首期运行边界与外部服务登记

## 本地运行

2026-11-19最新用户调整：暂停本机新增备份及恢复演练，采用Git回溯代码/文档。此前试点每日任务已实际安装并触发一次，现按用户要求停用（Disabled），不再产生新副本；先前每日/7份/RPO/RTO参数作为被暂停政策保留，不自动恢复。Git不保存运行数据库、企业原件、向量、日志、模型权重或凭据，因此不提供这些数据的恢复能力。当前实际证据和Git提交位置见STATUS/tasks。

- `DATACENTER_DATABASE_URL`：本地运行须显式指定隔离的 SQLite 文件或专用回环 PostgreSQL 库。配置中保留的默认 `sqlite:///./outputs/data-center.db` 及其路径和硬链接别名已隔离，连接和迁移入口均拒绝该目标。
- `DATACENTER_STORAGE_DIR`：原件和派生产物目录；默认 `storage/`，必须和数据库备份一起管理。
- `DATACENTER_BOOTSTRAP_TOKEN`：一次性初始化凭据，只从部署环境注入；为空时禁用 `/auth/bootstrap`。
- `DATACENTER_MAX_UPLOAD_BYTES`：上传大小上限，默认 50 MiB。
- `DATACENTER_SESSION_IDLE_SECONDS` / `DATACENTER_SESSION_ABSOLUTE_SECONDS`：会话空闲和绝对 TTL。
- `DATACENTER_SECURE_COOKIE`：HTTPS 部署必须开启；本地 HTTP 验证默认关闭。

当前API服务只负责受理与只读状态/结果读取，查询由显式Worker执行。Pi路径需要先构建固定Node宿主、建立与制品/工具集绑定的不可变本地运行配置；单独启动`python run.py`不代表已具备模型配置、可信审批或可用Worker。实际Worker命令、隔离合成环境及可复现检查见[本地查询执行与验证](../../doc/development/本地查询执行与验证.md)。

本地运行配置支持固定回环合成上游，以及当前获准的百炼北京qwen3.8-flash精确合成试点；不能仅修改URL接入任意provider。持久试点入口和副作用见[本地执行说明](../../doc/development/本地查询执行与验证.md#持久隔离试点入口)。SDK/工具/adapter及服务绑定变化须建立新不可变配置并核验，不覆盖旧配置；旧日志、调用预算和未知结果不得因升级清零或自动重发。真实库升级、企业资料外发和生产切换仍需各自明确边界，测试夹具不能作为真实信任根初始化。

## 本地容量边界（2026-11-19）

持久模拟试点使用唯一`pilot serve`进程和其中一个查询消费者：同时执行1项，pending/running合计最多4项，即最多1项执行＋3项等待；第5项得到明确429。等待不启动SDK、不预占模型调用预算，执行时仍重验当前权限、来源、服务准入与原预算。不要另启CLI/Celery消费者或第二个serve：受理限额不是跨进程worker槽位。现有任务失败/待输入/待核对不占pending/running名额，但恢复时须重新通过受理限额及原围栏。

通用非pilot入口的`DATACENTER_QUERY_MAX_INFLIGHT`默认0（保持原来的不设受理上限），允许整数0–64；`pilot.load_settings`显式固定4，不由环境扩大。修改受理数不等于经过容量验证。源码/制品变动须重绑运行配置，准入自检到期安全阻断，不自动付费续测。

适用环境为本机i5-12450H、约15.75 GiB RAM、BGE-M3 CPU/2线程、PostgreSQL/PGvector、已预热模型、每个浏览器1秒安全状态轮询，查询记录折叠时不加载，展开每次5项。实际矩阵、资料规模、等待时间、失败及证据唯一见[指标记录](artifacts/metrics/status.md#本地并发与容量2026-11-19)。这是小型合成试点边界，不能外推89份手册、持续多用户吞吐、供应商账户QPS或业务SLA。建议保留至少1 GiB空闲内存，查询时不同时运行MinerU、重建大索引或第二份BGE模型；这是保守运行建议，不是应用已实现内存配额/自动限流。正式扩大范围前按实际资料、用户及服务配额重新测量。

本地备份仍Disabled，不创建容量快照或恢复副本；测试只新建并清理UUID隔离库。Git仅回溯代码和文档，不保存运行数据。

### 持续运行与规模控制

BGE模型预载与编码共用进程内唯一推理线程，调用方取得模型锁后才提交，不形成无界推理队列；数据库Session、人员身份和业务权限不传入模型线程。`pilot serve`在监听和有限查询租约前完成预载。适配器源码变化会改变服务绑定，须重新自检、逐用途选用及构建对应代际，不能直接改旧pin或复用旧自检来消除stale。

模型启动前为现存BGE权重与至少512MiB运行余量留足内存，本机建议先确保至少3GiB空闲；预热后建议仍保留至少1GiB。验证夹具在空闲不足512MiB时停止新阶段。上述为运行建议与测试保护，不是产品已实现主机内存配额。其他应用、MCP工具宿主和Docker服务的占用也影响余量；不得为了通过测试擅停无关服务。

资料上限须同时记录文档数、页数、叶块/索引成员、文本字节和查询依赖来源数。89份页数匹配的稀疏合成资料不代表真实版式、文本密度或文件大小；单PDF600页预检保持，超限文件须另行处理，不能悄悄提高门槛。现有规模反例与最终持续范围以[本轮指标](artifacts/metrics/status.md#持续运行与资料规模2026-11-21进行中)为准；完整来源证明超过原轮次时限时，不以放宽超时或跨请求缓存权限/签认换取通过。

已测小域指知识库的完整资料范围。活动代际仍复核整库当前性；在大库中按型号只检索一份资料，仍承担该库的完整证明成本，不能套用小库的容量结论。

未完成代际不得激活；旧代际过期时普通查询须可见阻断，不回退到过期资料或输出无依据正式答案。已知服务失败只能在明确重试后接续原任务和剩余预算；响应不完整导致unknown时保持调用围栏，不能换幂等键重发。消费者重启仍须核对当前授权、完整来源、准入和原任务账本。进程恢复验证不等于数据库/原件丢失恢复，后者仍依用户要求暂停。

## 外部服务登记字段

每个 provider、账号和资料范围单独登记以下字段，未登记时保持禁用：

| 字段 | 说明 |
| --- | --- |
| provider / operation | `mineru.parse`、`embedding.encode`、`generation.answer` 等具体调用 |
| approved_by / approved_at | 企业业务或数据负责人批准依据 |
| data_scope | 可发送的知识库、文档版本和脱敏范围 |
| credential_ref | 密钥管理系统引用，不写入仓库或日志 |
| retention_policy | provider 留存、删除和备份规则 |
| budget / quota | 费用归属、配额、超限行为 |
| tls_evidence | 证书/网络验证记录和失效日期 |
| status | `pending`、`approved`、`blocked`、`revoked` |

`/workspace/services`执行固定适配器配置、自检、预检及启停；另行逐用途选用且私有精确合成输入证明有效时，selected_service解析、嵌入作业和Pi可用固定修订执行。持久试点使用百炼主回答及本地真实BGE向量检索；通用部署未设置`DATACENTER_QUERY_SERVICE_MODE`时仍为local，持久试点显式默认selected_service，失败不回退。企业资料准入仍未配置，未知不重发、停用后旧pin不复活。[服务适配与本地准入](../../doc/development/服务适配与本地准入.md)

## 本轮待落实的逐服务登记

首份手册试点准备按实际采用的服务逐用途登记。获准后先核对原件字节数、页数、归档大小和协议限额，再查页序、警告/脚注、异常回执和原任务恢复。当前本机 selected_service 的 64 KiB 原件上限仅覆盖合成适配，范围清单的 89 份文件均超过该值。生成主回答、摘要及压缩仅在对应流程实际启用且分别具备服务与资料准入时进入真实验证。

2026-11-19按当前仓库补齐登记结构：本地Standard环境已有实际合成推理证据；真实服务/资料批准仍未登记。拟定企业数据/费用责任和系统管理员分工见[首期开发基线](../../doc/planning/开工前待确定项.md#7-拟定岗位责任与待签认材料)，需用户补充的最小项及受影响任务只在其“真实验收最小确认清单”维护。

| operation | 已定方向 | 本轮状态及待填真实值 |
| --- | --- | --- |
| mineru.parse／本地Standard | 固定4.2.3 Standard优先，已有现存模型包与隔离home实际推理/回放 | 技术证据已具备；G1实际本地用途/职责、原页人工核验和真实试点留存仍pending；无需重复提供模型下载/账号，不自动外发 |
| mineru.parse／云VLM | 独立备选复核 | 未准入、未启用，当前无需补齐；启用前另登记账号/凭据引用、资料范围、留存/费用/配额、TLS与旧任务核对 |
| embedding.encode | 第一版BGE-M3/1024维，现存本地离线权重 | 已实现真实权重、持久child批次/账本、完整PGvector代际及实际向量检索；实际缓存路径见下节。三句编码仅为历史探针，当前完整运行证据由STATUS/tasks维护 |
| generation.answer | 阿里云百炼；第一版qwen3.8-flash，其他三项显式候选 | 已实现固定北京网关、真实Pi SDK/回执、阶段工具及并发未决围栏，受精确合成allowlist限制；完整查询运行证据由STATUS/tasks维护，不升级企业资料外发/真实验收 |
| generation.summary | 独立用途、默认关闭 | 未准入、未启用；不阻塞主回答/本地解析验证，不从answer许可继承 |
| generation.compaction | 独立用途、SDK自动压缩关闭 | 未准入、未启用；不阻塞主回答/本地解析验证，不从answer/summary许可继承 |

未批准字段保持null/pending，不以零预算、假批准人或默认允许补齐。岗位实际任命、真实资料准入、保存/删除政策及RPO/RTO/恢复窗口是对应真实运行的条件；不要求在独立本地文档准备前先采购部署硬件或取得全部生产参数。候选原件仍不进入Git；源码/设计审阅快照不携带企业原件、解析正文、参考答案或凭据。

### 真实验收的最小登记表

每个实际使用的用途分别填一份，可共同引用同一份有效批准、供应商政策或账户记录，但须明确该记录覆盖哪些用途。没有实际批准依据时保持pending；政策/职责材料与凭据放受控位置，开发文档只留引用。下表是字段模板；当前已确认值与技术证据以随后“本轮批注已确认的试点参数”为准，其余未知字段继续留空。试点政策批准、运行配置生效和实际达标分别判断。

| 信息组 | 最小实际字段 | 当前可预填／实际缺项 |
| --- | --- | --- |
| 服务身份 | operation、provider、model/修订、API地址/协议、账户引用、credential_ref、本地/外部部署范围 | 主回答百炼/qwen3.8-flash、env:DASHSCOPE_API_KEY；嵌入本地BAAI/bge-m3/1024维，详见本轮试点参数；不在聊天提供密钥 |
| 数据和批准 | 精确文档/版本或获准范围、可发送内容类别（含片段/向量/日志）、用途、批准人/依据/日期/有效期 | 全部真实许可待补；G1已选定与正式89份清单不等于外发许可。仅本地用途批准也不授权外部发送 |
| 留存与费用 | 供应商留存/训练使用/删除及备份规则的有效依据、费用归属、单次/日或试点总预算、限额与超限处置 | 均待补；未知用量保持unknown，不记零；默认不对未知调用自动重试 |
| 工程取证 | 工具协议/终态、输入及响应限额、实际TLS/证书/代理与允许出口、固定修订/最小试调用/停用记录 | Agent在对应调用获准后核实，不要求用户重做技术测试；TLS不通过则修证书/可信链，不关闭校验 |
| 试点数据政策 | 原件、attempt包、IR、Chunk/索引、历史回答、JSONL/快照/响应/摘要的保留及回退窗口；法律保留、存储路径/配额、数据责任人、到期处置 | 30天封闭试点政策已获用户接受；无自动purge，范围与到期处置见下节，未宣称产品已强制执行 |
| 备份与恢复 | 一致DB＋文件备份范围、故障隔离位置/访问控制、频率/份数、RPO/RTO、停写/恢复窗口、恢复责任/批准、撤权/删除/停用追平点 | 每日/最近7份、RPO≤24h/RTO≤4h试点目标已接受，完整应用备份/隔离恢复及Windows调度CLI已实现，实际安装/触发与同库恢复见STATUS/tasks；独立故障域仍待落实，事故默认SQLite排除 |


### 本轮批注已确认的试点参数

依据：2026-11-19当前用户六项批注。批准范围为第一版三角色模拟试点及所接受的技术/留存恢复建议，不构成虚构企业人员身份、专业签认或无限数据外发/费用承诺。参数快照为`outputs/annotated-pilot/2026-11-19/pilot-policy.json`，运行证据按下述范围判断。

| 类别 | 已确认值 | 实际生效／验证边界 |
| --- | --- | --- |
| 主回答 | 百炼`qwen3.8-flash`；其余三项为显式候选，禁自动fallback；凭据`env:DASHSCOPE_API_KEY` | 固定`https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions`、公共IPv4 pin及CA/SNI/TLS校验。128 token探针为历史证据；当前应用单次最多2048输出token，每任务最多8次调用、262144 token预留，保守上下文预算32768。只是程序上界，实际收费/账户总费用仍未知 |
| 协议选项 | `enable_thinking=false`、`preserve_thinking=false` | 应用已固定兼容路径、模型、选项和密钥引用，绑定endpoint/adapter/profile及实际回执；只接受当前阶段的唯一授权工具及合法结束状态，正式候选独立核对交付阶段。并发授权/派发共用任务围栏，未知保持预留、不自动重发 |
| 向量 | `BAAI/bge-m3`、`bge-m3-local-v1`，1024维；CPU本地离线，无下载/外发 | 完整权重在`ai_models/modelscope_cache/models/models/BAAI--bge-m3/snapshots/master`，`ai_models/bge-m3`为空。固定权重/tokenizer及运行依赖哈希；实际持久批次写入float32 PG向量，完整代际核验后显式激活。查询用同一向量空间实际cosine运算；历史三句探针不替当前链路证据 |
| 留存 | 每批获准试点自受理起30天；覆盖原PDF、attempt原包、两层IR、Chunk/代际、历史回答及私有JSONL/快照/响应/摘要；保留回退依据 | 到期停止该批新增并人工处置，不自动purge。未知法律保留不填“无”，有hold则禁止删除。具体存储配额仍按真实样本测量，政策接受不等于程序强制留存已实现 |
| 备份 | 每日03:00一致备份完整应用数据库＋私有引用文件；至少最近7份，目标RPO≤24h/RTO≤4h | Windows当前用户Interactive/Limited、StartWhenAvailable、IgnoreNew、执行上限1小时；备份/回执在明确试点根内。安装/触发实际状态由STATUS/tasks维护；不自动删除，不能为凑7份复制同一备份。机器/用户/Docker离线不保证RPO，同盘副本不具独立故障域 |
| 恢复门禁 | 隔离新库/存储、固定manifest SHA、核对撤权/删除/停用epoch/unknown预算；事故默认SQLite排除 | 目前依赖同一在线权威源，身份与全部表逻辑状态必须和cutoff完全一致，任何后续变化要求新备份。恢复副本封存，普通服务拒绝启动；不是独立治理重放或生产灾备切换，不执行历史付费调用。配置/DPAPI凭据/密钥/CA/权重/运行环境另行恢复 |

本轮仅允许固定合成文本；未向百炼发送G1或企业原文。主回答准入不覆盖云MinerU/summary/compaction。当前有限试点已绑定逐任务预算和精确输入证明；账户免费额度/实际收费/日总预算未验证，不将单次程序上界解释为持续企业业务费用批准。

官方依据（2026-11-19核实）：[模型目录](https://www.alibabacloud.com/help/en/model-studio/models)、[Chat兼容接口与地域地址](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)、[思考/历史参数](https://help.aliyun.com/en/model-studio/qwen-api-via-openai-chat-completions)、[API Key配置](https://help.aliyun.com/zh/model-studio/get-api-key)、[供应商留存政策](https://www.alibabacloud.com/help/zh/model-studio/data-retention-policy)。服务级无保留与Agent/有状态功能的条件分别判断，不把“不用于训练”解释为所有链路均不存储；官方政策不替企业相应资料许可。

## 本地显式初始化与正式网页

应用启动只校验当前 schema，不执行 DDL 或 seed。先按安全迁移指南对隔离库显式执行 `plan` / `apply`。本地迁移可初始化专用回环 PostgreSQL 新库，既有非空旧 schema 升级需要另行停写、备份和迁移评审。裸 Alembic 不接受未校验目标。[安全迁移](../../doc/development/安全迁移与事故核查.md)

React/Vite 工作区包含 `/workspace/query`、`/workspace/manage`、`/workspace/access-review` 和仅用于本地合成服务配置的 `/workspace/services`。页面与构建说明见 [Web 源码入口](../../src/web/README.md)，实际浏览器和恢复证据见 [项目状态](../../doc/planning/STATUS.md)。

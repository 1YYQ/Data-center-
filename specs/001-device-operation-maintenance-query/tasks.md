# Tasks: 设备操作维护资料查询

**Input**: `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`  
**Progress**: 项目汇总见 [STATUS](../../doc/planning/STATUS.md)。本文件是首期任务状态、依赖、修复周期和续跑检查点的维护入口。  
**Checkbox rule**: `[X]` 表示该任务自身范围的交付物及适用验收已核实；`[ ]` 可能是未实现、部分实现或已有代码待验收，不能据此认定代码不存在。T001–T003 只验证入口与格式文件，不代表业务资料、外发资格或运行条件已获确认。

<a id="当前调度与本次审查preflight-001"></a>

## 当前调度与本地实施（2026-11-19）

当前新增授权（并发与容量）：DONE，VERIFIED_LOCAL_SYNTHETIC。诊断并修复前轮20s/25s/Host90s并发超时，按本机实测明确可承受的活跃查询数、并行查阅/排队与资料规模边界；权限、版本、签认、完整源证明、原件字节、未知与预算围栏不降低。先做真实生产六成员证明/SQL调用树测量，再测有界实际SDK并发及本地真实BGE，有限真实供应商复验；大负载只用合成回环以免付费压力与企业内容外发。证据根`outputs/pilot-capacity/2026-11-19/`，Git基准`a686df0`、沿已批准分支。备份保持Disabled，保护默认SQLite，不恢复/复制持久库；使用新UUID临时库并清理。最终数据/修复与容量口径写既有metrics/status与本节；真实签认不阻断本地工程验证。

本轮已核实：同次证据日志批量复核、来源确认单行集合指纹、历史按需分页及有界受理已实现；权限/日志/来源变异定向回归、PG最后槽争抢与原幂等重放、实际Chrome排队/分页/429均通过。真实本地BGE＋SDK1/2/4活跃任务有界波次全部成功，但低内存与宿主余量限制日常固定单消费者、合计4项受理；数值/适用边界只在[metrics](artifacts/metrics/status.md#本地并发与容量2026-11-19)与[operations](operations.md#本地容量边界2026-11-19)维护。10页两文档整库、单消费者4项实际排队/原件读取已通过，临时PG已清理；已退出测试权重进程、明确复验并重绑Pi rev5，恢复8879单消费者，完成一次真实百炼网页查询与只读重开、历史完整读取。旧任务/预算/快照保全。6个UUID临时PG生命周期已逐项核对created/dropped且实际均不存在；保护默认SQLite哈希不变、备份任务Disabled。最终源码/构建、报告及Git结果在证据根candidate-final.json绑定；代码/文档沿已批准分支保存，不提交运行数据。本授权内验证与修复已完成；后续仅真实签认/企业资料/生产规模与服务许可消费各自外部依赖，恢复从STATUS→本节，不要求用户重复继续。未进行企业资料/签认、备份或生产验收。

### 前轮核心A–F（已完成，保留范围）

当前新增授权：按已确认模拟基线完成三角色A–F正式网页验收，已完成，VERIFIED_LOCAL_SYNTHETIC。A/B/C由主代理复用持久链/实际SDK和隔离PG核对；D独立分支验证分享与资料撤权；E用既存真实Standard包及必要少量合成图文/扫描样本核对解析和KM修正；F验证员工错误、SA修复及原任务恢复/未知围栏。只修对应缺口，不改真实业务签认、不把模拟结论记作组织真实验收。Git基准`2bdd761`、分支`codex/pilot-query-20261119`，证据`outputs/pilot-af/2026-11-19/`。本地备份继续Disabled，不创建备份/恢复副本；旧分段结果和被暂停工作保留历史资格。

本轮最终交接：核心A–F模拟验收已完成，各场景原始报告、驱动边界及未覆盖项唯一见[本轮验收记录](artifacts/acceptance/status.md#本轮模拟三角色af2026-11-19)，不把合成签认或真实供应商协议回执升级为组织环境验收。正式React已补单回答分享/逐人撤回、原任务重试、KM文字/警告校正。修复跨快照隐式迁移、旧幂等回执重绑、分享时区/快钟误撤、无DR写入、PageLabels误称印刷号、旧租约收尾覆盖重试、索引错误提示、过期链接重交及取消后未知/限额围栏。来源证明在同一只读边界去重，保留晚时点专业核验、完整指纹及跨边界重验。

持久闭环最终：原task `8a7c370c-aa0c-4179-bc74-c06cd7619d1e`、Pi profile rev4及既有完整BGE代际。首轮冷装载90.73秒受阻，先装载再监听后网页恢复原task，第2执行修订48.96秒成功；累计5/8次真实百炼received、163840/262144预留tokens，前两份和原预算完整保留，正式快照`9901f41a-fe69-468d-b619-863c972ebb9a`含6条当前成员、完整警告及原PDF哈希。网页202后取证脚本误读响应字段，已凭状态/账本确认后只读重开，无第二次重试或新query；失败原样保留。真实CPU向量检索和自检账本见`outputs/pilot-af/2026-11-19/live-query-ledger.json`，冷启动日志在`persistent/`；配置/服务自检因必要源码变更重新绑定，旧回执不重发、无自动付费续测。

最终Web为`index-CJFS-fko.js`，Node构建只纠正native aborted分类；原生10项、授权/发布规则24项、最终读取/分享/服务路由7项通过。E实际三类解析与扫描正负例、F故障/恢复/取消围栏、D正式分享撤权和C版本检查按各报告分段绑定，不累计早期失败为通过。并行验收负载下回环SDK20秒仍有受控超时，保留报告，不修改模型20s/百炼25s/Host90s或宣称容量达标；C/D的权限与版本正例分离为生产child工具＋真实回环回执，实际SDK＋真实供应商由持久链另证。完整候选、构建/证据适用及Git分支提交以`outputs/pilot-af/2026-11-19/candidate-final.json`绑定。

当前没有本授权内未处理的就绪工程项；真实人员/企业参考与签认、生产部署/容量和连续对话等扩展继续消费各自依赖，不由本轮核心A–F关闭。Git回溯沿用户已批准的`codex/pilot-query-20261119`，不提交运行库/PDF/模型/日志/凭据；本地备份持续Disabled且不新建恢复副本。真实验收恢复入口仍为STATUS→本节及其任务依赖，不承诺进程退出后自动继续。

最新用户范围调整：因本机存储考虑，暂停新增本地备份及完整恢复演练，转为Git代码/文档回溯。自有每日任务`DataCenter-Pilot-Backup-9b8f1c1c0bcb`已确认Disabled/Enabled=false，`schedule-user-disabled.json`为证；已生成首份完整75表/12文件备份约465063字节，保留原状，不再创建新快照，不未经授权删除。首份实际触发退出0为已发生事实，完整同持久试点恢复尚未执行且按最新要求DEFERRED，不伪称通过。查询闭环和代码/文档版本保存继续执行；下面初始范围中备份/恢复后续动作由本条取代。

### 前轮持久试点记录（历史，保留当时范围）

本轮百炼qwen3.8-flash受控网关＋真实SDK＋本地BGE-M3/PGvector＋持久三角色试点查询为VERIFIED_LOCAL_SYNTHETIC，已在实际Chrome完成一次正式答案及原PDF回查。每日任务安装/触发本身已验证，后依用户最新要求停用，后续备份/恢复DEFERRED。Git代码/文档回溯已上传指定新分支并核对远端；不将真实供应商调用升级为真实企业G1/人员签认。复用源QC/完整代际/TaskEvidence/AnswerPlan/双账本和安全迁移，不降门禁。

本轮中间检查点：BGE完整实际PDF/HTTP/CPU批次账本/PGvector语义查询与原PDF回查已通过（`outputs/annotated-pilot/2026-11-19/embedding/app-integration/bge-closed-loop.xml`），既有适配/服务回归113项通过；百炼受控网关真实SDK回环正负2项通过，外网完整混合查询由唯一持久试点串联；完整应用SQLite/PG备份恢复与迁移17项通过，独立审查三组P2已修复，`outputs/pilot-runtime/2026-11-19/path-guards-final.xml`验证路径保护2项通过，`routing.xml`验证网页默认选用可信服务无静默回退。中间BGE测试夹具浮点序列化/缺import/缺真实原件导致的失败及pytest缺临时父目录setup错误均保留，不放宽读权限，不累加失败候选数量冒充最终验收。

持久串联结果：`configure-sdk-final.json`绑定Pi rev3及原完整BGE代际。真实Chrome任务`a3399359-108c-446d-abdd-6ae965a31b1c`一次POST成功，3份百炼HTTP200/received/用量回执对应search→validate→deliver；真实CPU BGE/PGvector审计为`query.vector_retrieved`、1024维、2候选/6证据，正式快照保留两步、警告及5Pa，原PDF SHA一致。实际截图已查看，0页面异常，见`outputs/pilot-runtime/2026-11-19/browser/result.json`、`final-query.png`及`live-query-ledger.json`。本机服务`http://127.0.0.1:8879/workspace/query`，凭据只用本地`python -m src.server.pilot credentials --show`查看。准入自检有效期1小时，过期安全阻断，不安排自动付费续测。

修复与失败保留：首次Chrome任务`475109df-65dc-4455-968e-f2ccdf2c2f07`的8次HTTP200回执和已用预算不变，未伪造正式快照；根因为模型给无参工具附加字段导致SDK拒绝。仅在严格授权search/validate阶段采用服务端冻结参数，delivery不变，原始SSE不改；同执行重复投影防止无进展继续付费，未知跨整个task围栏，已知显式重试可在新执行修订使用剩余额度。独立复核发现的跨修订误挡已修复，真实SDK离线回放及PG最终节点3通过/1显式live跳过（`frozen-tool-replay-final.xml`），该跳过由本轮实际持久Chrome补充真实调用证据。`browser-first-failed/`、中间stale绑定失败及配置修订/阶段恢复日志保留，不能累计为通过。

最终交付与续跑：当前Node原生套件10通过/0失败/0跳过，见`node-final-unrestricted.log`；默认沙箱子进程/回环夹具超时的`node-final.log`单独保留，不改超时或跳测试。文档检查1174链接/71节点通过、Memory结构及Python编译通过。首次将已有未提交项目基线纳入Git时，raw diff-check报告既有Markdown换行、冻结v1 SQL空白和旧EOF空行，保留兼容fixture字节，不冒充零格式告警。暂存扫描329文件/4606438字节，无实际API Key/DB密码/私钥/运行数据。已获用户对具体远端和分支明确批准，提交`e0d80b84ddabb3497f076115dedd88fac5f2ad54`推送到`https://github.com/1YYQ/Data-center-`的`codex/pilot-query-20261119`且ls-remote一致；不强推/改main。该提交仅含代码、配置示例、文档和非敏感样例，`.agents/`及既有本地生成证据保留工作区。最后状态记录与候选/远端回执在`outputs/pilot-runtime/2026-11-19/`，Git不是运行数据备份。

本轮调整后范围已交付，无需再说“继续”。下游真实G1/真实业务A–F仅在其资料与人员证据就绪后开展；本轮未发企业内容、未伪造真实签认、不自动续付费自检，未继续用户暂停的备份/恢复。

允许现存本地模型推理、有限真实百炼合成内容调用、必要代码/配置/隔离库及已批准每日备份任务；禁止企业原文外发、事故默认SQLite及其别名、真实人员/业务签认伪造、自动purge或未授权部署。使用独立持久试点库/存储，真实API Key只从环境读取；数据库密码按现有本机容器环境内存引用。源库与私有文件备份、恢复验证和调度都必须绑定同一明确试点配置。证据目录`outputs/pilot-runtime/2026-11-19/`；`before.json`记录基准/边界，工作区原有混合变更保留。

验收边界：网页/HTTP受理→实际Pi SDK→百炼真实回执→本地真实向量检索→严格有源答案快照→原PDF/精确证据读取与撤权；同一完整应用DB/文件备份→新隔离目标恢复→已存答案/引用可读→过期/撤权/停用/unknown预算不可复活；每日任务实际存在、显式触发并产出有效备份。RPO/RTO区分实测/目标，缺独立故障域不冒充灾备。进行中验证失败持续修复，不在探针/局部测试结束时交付。

本轮用户批注落实：已接受三角色与第一版模拟参考、阿里云百炼候选及本地`DASHSCOPE_API_KEY`、BGE-M3最小离线验证、30天留存与每日/7份/RPO24h/RTO4h目标。[确认清单](../../doc/planning/开工前待确定项.md#真实验收最小确认清单)不再要求重复提供已定决定；[operations](operations.md#本轮批注已确认的试点参数)拥有实际参数。真实岗位/签认留给真实验收，不能继续全局阻断第一版隔离模拟。

| 本轮分段结果 | 验证与准确边界 |
| --- | --- |
| 资料与模拟参考 | G1原件身份再次匹配；5条schema合格参考。独立子代理实际查看两页，确认Setup/表1事实、保留s冲突；补逐事实/原文顺序映射、2项明确mock资格的答案renderer正例与缺成员拒绝。全Run真实发布资格未改变 |
| 三角色 | 独立新SQLite中采用生产权限函数及明确模拟审批/完整授权元组，14项允许/拒绝、未授予文档、撤权及过期通过；不是完整HTTP/Chrome或真实人员授权验收 |
| 百炼 | 四个候选在实际账户模型列表均可见，首选qwen3.8-flash；一次最多128输出token的合成SSE工具调用HTTP200，参数/终态正确、无重试，密钥未落盘。T024-PI-03取得有限真实协议证据，不代表SDK/业务网关已接线或企业资料许可 |
| BGE-M3 | 使用既存完整本地缓存、CPU及现有依赖，真实编码3句合成文本，shape=[3,1024]、有限非零；无安装/下载/外发。没有把tokenizer/固定向量称作编码，但应用provider/ledger/索引仍待接线 |
| 留存与恢复 | 试点参数已获用户接受，不再标成未获决定；小型自定义合成SQLite/文件静态快照恢复保全原件引用、撤销状态、unknown与预算。并非完整应用恢复或备份后治理重放；每日调度、独立故障域备份和到期执行尚未安装 |

本轮证据根为`outputs/annotated-pilot/2026-11-19/`：`pilot-policy.json`、`bailian/result.json`、`embedding/result.json`、`g1/independent-review.json`、`g1/role-matrix-result.json`、`g1/positive-component-result.json`、`recovery/receipt-final.json`。应用源码候选沿用上轮，不把新的文档快照冒充旧候选全部字节未变。

上表为本轮批注初次落实时的历史分段证据；当前百炼/BGE接线、实际查询和停用调度以上方结果为准。完整G1结构/质量/全Run查询及设备真实A–F仍消费自身真实资料、人工参考和签认，不为了模拟正例绕开门禁。后续新增本地备份/完整恢复须等待用户重新授权，不继续原计划。

用户已明确批准按调整后的开工基线开始本地实施：先完成T019-PDF-01/02的产物归档和持久解析，再持续推进依赖就绪的首期查询能力；同范围验证失败自主修复，不逐项等待“继续”。允许业务代码、前端、必要增量迁移及显式隔离合成数据库/存储/进程验证；不修改事故默认SQLite或其他真实库，不改变真实授权，不外发企业资料、不创建未准入真实服务配置、不部署。真实签认、权重/实际profile就绪、服务准入、保留/恢复参数只限制消费它们的真实活动。

Git基准为`0e2d4611dffebd9b24f933a5cf3e8c602bc9f6dd`＋现有混合工作树；与PREFLIGHT候选核对无变化。实施前态、授权/治理及后续候选/报告存于`outputs/pdf-implementation/2026-11-19/`，只在本节记录能力/证据/下一动作，不另起任务体系。

| 能力分段 | 当前状态与依赖 | 当前交接 |
| --- | --- | --- |
| T019-PDF-01/02：归档与持久链路 | VERIFIED_LOCAL_SYNTHETIC；完整原包、两阶段落盘/绑定、取消/未知围栏、PG独占及阶段恢复；新增有界扫描入口 | 真实离线Standard已在隔离home跑通；G1业务/复杂版式/正式容量另验 |
| P2/P3：来源、结构和源QC | VERIFIED_LOCAL_SYNTHETIC，另有实际引擎原包回放；两层冻结IR、全页/资产/关系、源QC与人工签认分开，派生Run可继承多代已证来源 | 实际4.2.3省略空白页默认blocks已兼容，非空缺键/null仍拒；真实关系语义与专业核验仍EXTERNAL_PENDING |
| P4：离线切分及来源启用 | VERIFIED_LOCAL_SYNTHETIC；实际固定tokenizer/真实成员/独立集合QC、候选不选用、显式draft选择及refresh/quality_repair围栏 | 管理浏览器已观察；真实签认缺项只阻止正式消费 |
| P5：完整child索引/切换/检索 | VERIFIED_LOCAL_SYNTHETIC；逐用途批次账本、实际PG向量及完整成员、单独构建和CAS启用/回滚；从未ready_partial回退 | PG实际向量运算、HTTP代际和显式切换有证据；真实嵌入服务质量/准入另待 |
| PDF→Pi及组织答案 | VERIFIED_LOCAL_SYNTHETIC；精确TaskEvidence/模型投影/严格AnswerPlan与候选私存，实际SDK完整正负节点和Chrome正式回答/原页读取通过 | 两项原始请求、两组/六成员；漏警告、无真实回执、自由文字拒绝；代际切换不换历史引文，撤权阻断读取/交付 |
| Pi摘要/压缩与私有日志 | 工程候选已接；实际SDK独立prompt/compact、逐用途pin/回执/内部产物、源manifest与兼容恢复。默认不自动启用 | 独立回环技术验证通过；实际用途/留存/恢复及语义验收只限制相应活动 |
| G1/真实岗位/人工参考/实际provider及业务A–F | EXTERNAL_PENDING | 不签造确认，不外发真实企业资料，不部署或触碰事故库 |

最终源文件/构建产物及分段证据绑定见`outputs/pdf-implementation/2026-11-19/candidate-final.json`；恢复指针为同目录`implementation-checkpoint.json`，实施前态和早期候选保留。报告各自覆盖当时执行路径，最终只新增负向来源围栏和版本提示时做对应定向检查，不宣称重跑过所有历史节点；红报告与环境限制保留，不累加旧通过数量代表最终候选。

本轮真实离线Standard：使用已存在、与固定缓存revision/LFS SHA一致的两GGUF与ONNX，在全新隔离home经SDK自身manifest/ModelPath校验后，对两页合成PDF推理61.8秒，闭合ZIP原件CRC及字节回查通过。实际输出暴露客观空白页省略默认blocks的契约缺口，已最小修复；同一原包零重推理回放Normalized/Structured/SourceQC通过，未升级为企业G1或复杂版式质量验收。证据在`standard-offline/`。

浏览器`browser/pdf-workspace-observed.xml`新PDF/切分/员工精确引文/系统权限4项通过；`browser/legacy-workspace-fixed.xml`更新后的三角色3项通过，pageerror均0、UUID PG库清理、事故默认SQLite哈希未变。原旧同步/自动启用断言失败保留；改为真实202→后台推进→独立核验→候选显式选用→BGE批次与启用，没有恢复Basic/自动发布。

本轮修复周期：`final-core.xml`267节点为263通过、3失败、1未选PG运行条件的skip；三失败分别为旧同步解析断言、无结果空字符串及JSON返回期间session撤销漏复验。定向`core-repairs.xml`16项全部通过，跳过的非空库迁移另由`migration-0017/sqlite.xml`和`postgres.xml`各1项实际验证。独立复查修正取消回执正文、分享替换CAS/失权新增、字段借数值/复合标题假覆盖及实际计划缺项投影；`answer-coverage-and-cancel.xml`24项、`share-fences.xml`真实PG并发/失权1项通过。

实际SDK时延故障已从ASGI观测区分：旧路径对每证据重复全来源核验，6成员上下文/授权各11秒、转发往返22秒；现每次边界批量共享核验、唯一原件实字节校验、perVersion发布指纹仅算一次，跨边界仍重验。原生7秒超时未覆盖控制面与5秒供应商时限，已改成绑定RuntimeProfile/adapter的20秒总请求时限，并传递原生abort信号；上游5秒、双租约/配额/投影到期及取消不放宽。`child-answer-pipeline-local.xml`最终1项通过（0失败/跳过）：实际SDK1.5.2、回环SSE/调用账本、真实PG向量和已安装Chrome，16384字节profile的三次请求为4386/9363/9798字节，8192字节profile在再次外发前明确拒绝。三次调用均有真实received回执，正式快照仅含实际声明证据；分享接收者仅DR＋task.read即可读同一回答/原PDF，无新查询POST、无页面错误。此为合成上游/签认，不代表实际模型质量或企业准入。

收尾验证：`final-runtime.xml`103项运行链路通过；最后不完整child身份拒绝降级的围栏及既有正向路径在`runtime-contract-final.xml`42项通过。Node真实SDK检查`node-sdk-final.log`9项通过；React类型/构建及客户端17项通过，`browser-final-copy.xml`1项实际Chrome验证最终bundle的未知版本提示、精确引文、原图及撤权。构建仅有既有AntD指令/包体积提示，未更换全局依赖。候选未提交、未部署，保留用户既有混合工作树；事故默认SQLite字节与本轮前态一致。

下一入场按依赖分别处理：①T028-PDF-01取得G1本地用途与真实人工参考/核验职责后，复用本轮Standard环境执行T024-PDF-01及T029-PDF-06，不重建已完成代码；②T024-PI-03按主回答/嵌入/辅助各自取得provider、数据、账号、费用、TLS/出口与留存准入后，只做相应用途真实验证；③T010-PDF-01/T010-PI-01取得保留/恢复窗口与责任后，补真实生命周期和恢复演练，不先清理原证据。无一分支阻塞其他已具备条件的活动。当前这些必要输入均未在仓库落实，未安装常驻自动执行器，不承诺进程结束后自行恢复。

本次获准的本地查询闭环已交付；纯工程子项按自身代码及技术证据勾选，混合真实验收的验证/移交项继续保留未关闭并消费本节已通过片段。摘要/压缩有真实SDK独立回环技术证据，但完整真实多来源语义、逐用途准入及保留恢复未验收，默认不启用；私有副本仅完成可见性隔离、来源/兼容恢复与清理前盘点，不宣称物理清理或完整备份恢复已实现。与本轮无关的实验、完整20工具、额外格式和生产部署不纳入本次闭环，真实断言不因合成技术通过关闭。

2026-11-19真实入场准备补充：已在[最小确认清单](../../doc/planning/开工前待确定项.md#真实验收最小确认清单)集中列出六项用户缺项/建议/直接受阻任务，并按operations拆分五用途准入及留存恢复字段。只读重核G1原件身份通过，五项原页核验草稿位于`outputs/real-acceptance-preparation/2026-11-19/g1-review-draft.json`；人工签认和真实批准均未补造。应用源码/运行配置不变，原最终候选保留为上次交付快照；本次文档修改不冒充旧候选哈希仍完全一致。收到一项有效记录后只解除对应分支，不要求其他用途同步准入。 本次检查：`outputs/real-acceptance-preparation/2026-11-19/docs-check.json`为PASS（1166链接、71节点、零错误），58个本地证据目标另核实存在；原页草稿保持UNREVIEWED/真实批准字段全空，250份应用/测试及构建源快照和事故SQLite与上轮候选一致，差异检查通过。不重跑未变更的业务测试。

## 历史实现线索（2026-11-16，非当前状态）

以下表格保留当时观察及失败边界；不作为2026-11-18实现清单。当前逐任务能力与剩余验收见[Dependencies](#dependencies)，最新执行候选见[检查点](#执行检查点)及[STATUS](../../doc/planning/STATUS.md)。

| 任务 | 当前候选与证据边界 |
| --- | --- |
| T001–T003 | feature 入口、89 份清单/参考答案格式及运行登记字段存在；静态交付物核对，不是业务验收 |
| T004–T010 | 本地模型、迁移与安全/存储/任务代码存在；完整账号、会话、授权和异步恢复门槛尚无对应全面验证 |
| T011–T014 | 本地查询/证据/分享代码存在；2026-11-16三项失败保留。后续CACHE-PERM-002记录夹具缺少必填授权维度；该线索已静态核对，尚待修正夹具后的定向复验，不据此宣称产品链路通过 |
| T015–T016 | 静态查询页及 React 组件边界存在；正式 React/Vite 工程及真实浏览器任务未验收 |
| T017–T018、T021 | 本地上传、状态与发布代码存在；完整上传/并发/发布和实际资料验收未完成 |
| T019–T020 | 本地解析和内容块代码存在；MinerU 仍为阻塞适配器，复杂 PDF、远端恢复及完整核验链路未完成 |
| T022、T027 | 仅有管理组件边界，完整管理操作界面未完成 |
| T023–T026 | 本地管理端点、失效和运行记录代码存在；完整授权矩阵、逐服务准入/费用、恢复及删除演练未验收 |
| T028 | 已有 89 份草稿范围清单与格式；缺少业务确认、黄金样本、人工答案和负面案例证据 |
| T029–T030 | A–F 场景与业务指标仍未验证 |
| T031 | 入口收敛部分已有记录；2026-11-17 同步普通使用者讨论，首期整体验收前仍未完成 |
| T032–T033 | 2026-11-17 用户确认持续对话/条件记忆及资料查阅；当前仅有需求和候选契约，未实现/验收 |
| T034 | 员工从对话/手册反馈、知识管理员后续处理已确认；仅需求/候选对接，未实现；反馈不改变业务确认/内容核验/发布 |

历史迁移：旧版本曾以“本地已有实现”勾选 T001–T018、T021、T023。2026-11-16 保留该历史，将缺少任务适用验收或已出现失败的 T004–T018、T021、T023 恢复为未验收；这不是删除实现或认定所有功能失败。没有用任务勾选数量计算完成率。

## Phase 1: Setup

- [X] T001 Record the native feature directory and source-of-truth links in `.specify/feature.json` and `specs/001-device-operation-maintenance-query/README.md` (FR-001, FR-014)
- [X] T002 [P] Create the non-sensitive 89-file inventory import format and reference-answer schema in `specs/001-device-operation-maintenance-query/artifacts/` (FR-001, FR-014, SC-001, SC-006)
- [X] T003 [P] Define local environment boundaries, secret names, external-service approval records and cost/quota fields in `specs/001-device-operation-maintenance-query/operations.md` (FR-012, SC-007)

### 实施前资料与验收基线

- [ ] T028 Assemble the 89-file formal-scope inventory, representative golden sample, human-reviewed reference answers and negative cases in `specs/001-device-operation-maintenance-query/artifacts/` before implementation that consumes formal business material and before real quality acceptance (FR-001, FR-014, SC-001, SC-002). 实际原件/候选答案/业务依据保存在被Git忽略的受控位置；人工复核不由Agent草稿替代。

2026-11-17编排修正：将原T028从最终验收阶段前移，保留ID、范围和未完成状态；不要求89份全部提前发布，但黄金样本必须有真实身份/适用版本/访问依据及人工参考。正式查询只使用满足门槛的子集，其余继续待确认。最终T029/T030使用并补充该基线，不反向阻塞样本准备。

## Phase 2: Foundational Persistence and Security

- [ ] T004 Create Alembic migrations for users, identities, sessions, roles, permissions, approvals, authorization grants, audit events and revisions in `src/server/migrations/` (FR-002, FR-003, FR-010, FR-011)
- [ ] T005 Implement platform-account bootstrap, credential activation/reset and one-time administrator initialization in `src/server/app.py`, `src/server/security.py` and `src/server/services.py` (FR-002, SC-005)
- [ ] T006 Implement session cookies, session rotation, idle/absolute expiry, CSRF checks and revocation in `src/server/app.py`, `src/server/security.py` and `src/server/services.py` (FR-002, SC-003, SC-007)
- [ ] T007 Implement complete authorization-tuple evaluation and safe 401/403/404 responses in `src/server/app.py` and `src/server/services.py` (FR-003, FR-009, SC-002, SC-004)
- [ ] T008 Implement idempotency records, ETag/If-Match handling and task/outbox/attempt persistence in `src/server/models.py`, `src/server/app.py` and `src/server/services.py` (FR-005, FR-008, SC-003)
- [ ] T009 [P] Implement sanitized audit events and provider-call accounting without business正文, credentials or signed URLs in `src/server/services.py` and `src/server/operations.py` (FR-010, FR-012, SC-007)
- [ ] T010 [P] Define file-object storage, temporary cleanup, retention states, cache invalidation and deletion hooks in `src/server/services.py` and `src/server/operations.py` (FR-005, FR-011, SC-004, SC-007)

## Phase 3: User Story 1 — Ordinary employee query (P1)

**Independent test criteria**: A permitted user can continue a conversation with confirmed conditions, start/recover a separate conversation, search default or narrowed authorized scope, find/open manuals directly, verify citations, and share one answer with conditions/version/source. Missing evidence, model mismatch, version conflict, permission loss and service failure remain explicit; whole-conversation sharing is excluded.

- [ ] T011 [US1] Implement query task creation linked to the relevant conversation turn, frozen condition/scope snapshots, persisted state transitions, polling and refresh recovery in `src/server/app.py` and `src/server/services.py` (FR-008, SC-003)
- [ ] T012 [US1] Implement current-version, applicability, review-status, model and authorization filtering in `src/server/app.py` and `src/server/services.py` (FR-007, SC-001, SC-002)
- [ ] T013 [US1] Implement evidence snapshots, locator rendering, warning codes and insufficient-evidence results in `src/server/app.py` and `src/server/services.py` (FR-007, FR-008, SC-001, SC-002)
- [ ] T014 [US1] Implement single-answer sharing with device conditions/version/source, expiry, revoke and dependency re-check; do not share the conversation in `src/server/app.py` and `src/server/services.py` (FR-009, SC-004)
- [ ] T015 [P] [US1] Build the proposed desktop conversation/history, condition/scope controls, manual lookup and result/evidence views after layout/visual confirmation in `src/web/features/query/` (FR-013, SC-005)
- [ ] T016 [P] [US1] Add user-facing states for network failure, access change, missing evidence, pending review and version conflict in `src/web/features/query/` (FR-013, SC-002, SC-005)

## Phase 4: User Story 2 — Knowledge administrator lifecycle (P1)

**Independent test criteria**: A knowledge administrator can register a source file, preserve its hash and metadata, inspect parsed blocks, record separate business/content statuses, publish an effective version and see audit history; duplicate or unreviewed material cannot become formal evidence.

- [ ] T017 [US2] Implement file type/size/hash validation, idempotent upload and interrupted-upload reconciliation in `src/server/app.py` and `src/server/services.py` (FR-005, SC-003)
- [ ] T018 [US2] Implement document/version state transitions and optimistic concurrency checks in `src/server/app.py` and `src/server/services.py` (FR-004, FR-005)
- [ ] T019 [US2] Implement MinerU adapter boundary, remote-task recovery and normalized parse-run persistence in `src/server/parsing/` (FR-006, FR-012)
- [ ] T020 [US2] Implement content blocks, locators, review states, correction batches and untrusted-document-content handling in `src/server/parsing/` (FR-006, SC-001, SC-002)
- [ ] T021 [US2] Implement business approval records, registration records, publication/withdrawal and active-index gates in `src/server/app.py` and `src/server/services.py` (FR-004, FR-007)
- [ ] T022 [P] [US2] Build knowledge-admin source list, upload, review, correction and publication views in `src/web/features/knowledge/` (FR-013, SC-005)

## Phase 5: User Story 3 — System administrator access and operations (P1)

**Independent test criteria**: A system administrator can manage accounts, roles, approved authorization tuples, service configuration and sanitized operational status without implicit access to business正文; revocation, expiry, audit and recovery behavior remain enforceable.

- [ ] T023 [US3] Implement account, identity, role-assignment and authorization-grant management with approval linkage in `src/server/app.py` (FR-002, FR-003, FR-010)
- [ ] T024 [US3] Implement service configuration, external-data approval, quota/cost accounting and provider health state in `src/server/app.py` and `src/server/operations.py` (FR-012, SC-006)
- [ ] T025 [US3] Implement cache/index invalidation after revoke, withdrawal, deletion and final cleanup in `src/server/operations.py` and `src/server/services.py` (FR-011, SC-004)
- [ ] T026 [US3] Implement sanitized operations views, failure classification, backup/restore metadata and deletion-drill records in `src/server/app.py` and `src/server/operations.py` (FR-010, FR-011, SC-007)
- [ ] T027 [P] [US3] Build administrator account, authorization, service-status and audit views in `src/web/features/admin/` (FR-013, SC-005)

## Phase 6: Cross-cutting acceptance and polish

- [ ] T029 Run the `quickstart.md` scenarios for normal query, missing evidence, version conflict, permission/share revocation, parse ambiguity and service failure; record results in `specs/001-device-operation-maintenance-query/artifacts/acceptance/` (SC-001–SC-005)
- [ ] T030 Record retrieval quality, citation accuracy, task latency, context use, provider cost, backup/restore and deletion-drill baselines in `specs/001-device-operation-maintenance-query/artifacts/metrics/` (FR-014, SC-006, SC-007)
- [ ] T031 Reconcile source-of-truth links, implementation mappings and current evidence in README, project specifications/plans, API/database designs and this feature before declaring first-release readiness (FR-001–FR-014, SC-001–SC-007)

## 2026-11-17 Confirmed Ordinary-User Additions

Appended IDs preserve existing task references. Their implementation precedes the dependent integrated acceptance, regardless of their numeric position.

- [ ] T032 [US1] Implement conversation/message/confirmed-context revision persistence, owner/turn/task links, default or manual retrieval scope and history/model-context permission re-checks in `src/server/models.py`, `src/server/schemas.py`, `src/server/app.py`, `src/server/services.py` and `src/server/migrations/` according to API/database section 13 after candidate review (FR-007, FR-008, FR-009, FR-015, SC-001–SC-005)
- [ ] T033 [US1] Implement authorized cross-library manual lookup by equipment model or filename and version-specific original/citation opening in `src/server/app.py`, `src/server/schemas.py`, `src/server/services.py` and `src/web/features/query/`; retain draft/version-state access rules (FR-013, FR-016, SC-001, SC-002, SC-004, SC-005)

- [ ] T034 [US1/US2] Implement employee feedback submission and in-origin progress/reply retrieval from the current conversation/manual page, permission-checked update notices and requery creating a new answer while retaining the old one, validated source/dependency links, scoped knowledge-manager handling and audit in the existing server/web modules after reviewing API 13.4/database 13.3; keep feedback separate from approval, content review, publication and conversation sharing (FR-017, SC-002, SC-004, SC-005)

## Dependencies

2026-11-18编排收敛：本节是直接依赖和当前任务能力分段的唯一维护位置；[plan](plan.md)与[准备基线](baseline.md)只引用批次。[FR/SC追踪](traceability.md)按本次证据快照关联断言，不代替任务勾选。

### 依赖的解释

- 下表“准备输入”表示该任务领取某能力分段时所需的契约/已验证能力，不要求前置任务整项勾选。涉及身份、授权、持久化的实际执行仍须具备对应安全能力；未满足时先修该分段，不能跳过门禁。
- T004–010/T023中的真实任命、永久清理或恢复等未具备项，只限制消费它们的活动，不把T028或全部管理任务的整项完成作为所有合成准备的前提。
- T028格式/候选整理仅依赖T002，不等待用户故事；其真实事实仍是正式资料与真实验收的前置。
- T029场景脚本、T030测量方法、T031文档收敛可独立准备。T030的指标采集消费场景运行；T031的最终关闭消费实际验收。不得用“T029等T030最终记录、T030又等T029”形成环。
- T015/T016/T022的准备可基于已审API/数据契约与对应能力并行；正式首期验收必须集成T032/T033/T034。FR-017不因无运行证据后置到首期之外。
- 所有EV号只关联指定候选的证据/缺口。局部验证不等于整项完成；原34条任务描述、勾选与业务验收标准保留。

### 直接依赖与当前能力范围

直接依赖持续有效；下表EV引用与已观察范围是2026-11-18编排时的限定证据，不是最新实施清单。2026-11-19新增PDF/Pi能力、最终候选与剩余范围统一见本文件顶部当前调度，勿从历史EV覆盖不足推断代码仍未实现。

| 任务 | 准备输入（直接前置） | 当前证据 | 已观察范围 | 剩余适用验收 |
| --- | --- | --- | --- | --- |
| T001 | 无 | EV-01 | 入口/文档静态交付已核实 | 不产生业务或外发批准 |
| T002 | T001 | EV-01 | 清单/参考答案格式已核实 | 真实样本与人工答案仍由T028落实 |
| T003 | T001 | EV-01 | 环境/服务登记字段已核实 | 真实环境值、账号/外发/费用未落实 |
| T004 | T001、T003 | EV-03、EV-12、EV-13、EV-14 | 隔离SQLite/PG迁移和持久表有局部运行证据 | 旧PG受控升级、历史迁移与全约束组合 |
| T005 | T004 | EV-03、EV-04 | 合成初始化、账号/凭据与管理局部验证 | 实际平台任命及完整初始化/重置矩阵 |
| T006 | T004、T005 | EV-03、EV-09 | 合成会话/CSRF/撤销及当前身份局部验证 | 完整TTL/轮转/跨会话/真实环境矩阵 |
| T007 | T004、T006 | EV-03、EV-07、EV-10 | 完整元组、读取/维护/交付围栏局部验证 | 真实职责/批准范围和完整授权组合 |
| T008 | T004、T006 | EV-06、EV-08、EV-12、EV-15 | 回执/CAS/outbox/任务与一次副作用局部验证 | 未覆盖并发/未知提交窗口与实际环境恢复 |
| T009 | T004、T006 | EV-04、EV-06、EV-07、EV-14 | 关键提交/拒绝审计及服务账本安全投影局部验证 | 全面审计白名单、读写分离、真实费用/主体归属 |
| T010 | T004、T007、T008、T009 | EV-06、EV-07、EV-13 | 文件稳定安装、逻辑保留与失效局部验证 | 保留期/临时清理/永久清理及一致备份恢复 |
| T011 | T007、T008、T009、T032 | EV-08、EV-09、EV-12 | 持久查询/缺项响应/Worker与刷新恢复局部验证 | 完整持续对话/步骤状态/真实工作场景 |
| T012 | T007、T018、T020、T021 | EV-05、EV-08、EV-11 | 当前版本/授权/型号/索引筛选局部验证 | 真实语义检索质量与所有范围/版本组合 |
| T013 | T008、T011、T012 | EV-08、EV-09、EV-10 | 证据/警告/定位与最终回答快照局部验证 | 人工逐事实核对、复杂PDF警告完整性 |
| T014 | T006、T007、T008、T013 | EV-03、EV-10 | 单条快照分享/当前授权/撤销局部验证 | 真实接收者、期限与权限组合及历史引用 |
| T015 | T011、T013、T014、T033 | EV-09、EV-16 | 当前桌面查询/原文/恢复子流程浏览器验证 | T032/T033完整交互与T034员工反馈集成、真实可用性 |
| T016 | T011、T013、T033 | EV-08、EV-09、EV-16 | 网络/缺项/拒绝/未知回执局部可见状态验证 | 完整异常/反馈恢复及真实用户可理解性 |
| T017 | T007、T008、T010、T018 | EV-06、EV-07 | 合成新文档/版本上传、中断与同键核对验证 | 真实类型/限额/规模及完整临时清理策略 |
| T018 | T004、T007、T008、T009 | EV-05、EV-06、EV-07 | 版本/来源/CAS/草稿隔离局部验证 | 真实适用/专业依据和全状态转换矩阵 |
| T019 | T003、T008、T010、T018、T024 | EV-14、EV-15、EV-17 | ParseRun选用固定MinerU修订、原batch恢复/ZIP隔离及合成并发 | 真实准入/格式兼容、复杂原页人工核对及留存清理 |
| T020 | T007、T018、T019 | EV-05、EV-14 | 定位/逐块核验/精确选用及安全规范化局部验证 | 获准复杂PDF逐原页/表格/图注/警告人工核对 |
| T021 | T007、T008、T009、T018、T020 | EV-05、EV-07、EV-11 | 确认登记/核验集/预检发布/撤回及索引局部验证 | 真实确认/审批/冲突裁决与业务选用全链 |
| T022 | T017、T020、T021 | EV-05、EV-07、EV-18 | 知识管理创建/维护/发布子流程Chrome验证 | T034受理/回访及剩余首期管理职责、真实验收 |
| T023 | T005、T006、T007、T008、T009 | EV-03、EV-04 | 审批登记/核查/撤销与账号停用局部验证 | 真实任命/完整角色依据与授权管理矩阵 |
| T024 | T003、T007、T008、T009 | EV-14、EV-15、EV-16、EV-17 | 固定合成及业务用途选用/精确许可/预算、HTTP/TLS/未知账本验证 | 企业secret/真实逐用途准入、结算/健康及许可续期 |
| T025 | T007、T008、T009、T010、T018 | EV-03、EV-07、EV-10、EV-11 | 撤权/撤回/删除后读用与交付竞争局部验证 | 最终清理、恢复后失效与完整多资源组合 |
| T026 | T003、T007、T008、T009、T010、T025 | EV-07、EV-12、EV-13 | 运维/生命周期元数据及本地恢复护栏局部验证 | 真实一致备份/恢复/删除演练、RPO/RTO与读写分离 |
| T027 | T023、T024、T026 | EV-04、EV-16 | 审批与服务配置桌面子流程验证 | 完整首期账号/授权/运维审计职责及真实角色验收 |
| T028 | T002 | EV-01、EV-02 | 格式/候选身份准备；真实部分TODO | 真实来源/访问依据、适用/专业签认、人工答案和负例 |
| T029 | T002、T003 | EV-02 | A–F及扩展场景准备；真实执行NOT_RUN | 执行需T028及相关用户故事/管理能力；通过还需T030口径及真实对照 |
| T030 | T002、T003 | EV-01、EV-02 | 测量字段/方法准备；真实指标TODO | 采集需T028/场景运行，阈值经业务确认；不以合成耗时代替基线 |
| T031 | T001 | EV-01、EV-02 | 本轮文档追踪/依赖/基线分段核对 | 整项关闭需T029/T030及适用FR/SC最终一致，不由本轮代替 |
| T032 | T004、T006、T007、T008、T009 | EV-08、EV-09、EV-10 | 对话条件/任务关联/补型号与上下文局部验证 | 完整消息/历史/新会话、条件修订与当前权限矩阵 |
| T033 | T007、T013、T018、T021 | EV-05、EV-07、EV-09 | 授权原文/定位/精确参考开放及查阅局部验证 | 完整跨库查找/型号/文件名/历史及真实手册矩阵 |
| T034 | T007、T008、T009、T013、T018、T032、T033 | EV-18 | 合成HTTP/Chrome验证两端回访、显式范围领取/答复、授权失效、幂等和主动重查；PG验证并发领取 | 完整分派/移交及真实处理人、授权和资料验收 |

T029/T030/T031的正式完成条件如下，和上表准备依赖分开：

| 对象 | 正式执行/关闭的必要输入 |
| --- | --- |
| T029真实A–F及扩展 | T028真实资料/答案/访问依据；T004–027中对应安全、生命周期、服务与三角色能力；T032–034；所用服务实际准入；T030已确认的测量/判定口径 |
| T030实际指标 | T028人工样本；相应场景真实运行及资源/费用记录；真实业务基线和已确认阈值。尚无阈值时不宣称达标 |
| T031整项关闭 | T029/T030及全部适用首期需求/契约/证据一致；源码与配置版本可复核；已知问题、待办和发布门槛明确 |

真实任命、签认、人工答案、服务准入和运行参数先列[真实待办](baseline.md#5-真实待办及解除条件)，缺哪个只限制相应真实活动。2026-11-18编排任务只获文档授权；2026-11-19用户已明确批准本地实施，范围与交付以本文件顶部为准，不推导真实授权变更、外发或部署许可。

## Parallel Opportunities

- T002/T003可在T001后并行。T009/T010的契约与夹具准备可先行；其实际持久化/授权/副作用能力必须消费上方直接依赖，不是目录存在即能执行。
- T015/T016的交互稿和客户端契约准备可与服务器工作并行；实际端点集成及验收消费对应查询/证据/查阅能力，FR-017仍纳入首期最终验收。
- T022可在内容块/发布/反馈契约固定后并行准备页面，实际管理动作按其直接依赖和当前权限执行。
- T027可在授权/审计/服务契约固定后并行准备，实际管理集成仍需T023/T024/T026对应能力分段；不从并行准备推导真实授权或运行准入。

## MVP Scope

MVP is US1 plus the minimum foundational security, persistence, document lifecycle and evidence work required to make the employee query complete and safe: T004–T021, T028–T030, T032–T034. US2 and US3 administrative surfaces are required for a sustained pilot, while calculation, report, broad-format and SSO work remains out of scope.


## 执行检查点

### PREFLIGHT-001：已完成开工前审查（历史范围）

2026-11-18启动、2026-11-19交付。本次任务是“开工前全方位审查并调整”。授权写入开发文档、文档检查/生成工具及本地审阅产物；不启动业务功能开发、依赖/权重安装、DDL、真实配置/授权变更、企业资料外发或部署。Git前态为`0e2d4611dffebd9b24f933a5cf3e8c602bc9f6dd`加已有未提交工作树；完整前态见`outputs/preflight-review/2026-11-18/before.json`。

本次完成条件：入口/文档所有权明确；首期组织答案范围贯通FR/方案/任务/验收；工程与真实前置不互相误阻塞；维护和连续推进规则可执行；静态检查、生成一致性与针对性负例通过，应用代码及原有任务勾选不变。审查记录、验证结果和精确候选在本节及STATUS收口，不新建平行任务表。

**任务状态：DONE；交付层级：VERIFIED（文档与检查工具）**。19份既有开发文档原位调整，新增`src/tools/project_docs.py`及4项定向负例检查。FR/SC编号、34父任务与37子任务身份/勾选保留；既有产品代码、依赖/迁移及正式资料清单字节未变，没有读取企业原件或执行真实配置/外发/DDL。审查不表示首期产品或所有外部条件已就绪。

实际验证：离线文档检查PASS（1160个本地链接、71任务节点，17FR/7SC追踪，0结构错误）；4项检查器测试通过；不含outputs的隔离源码/文档副本PASS；只在该副本注入依赖环及生成文件偏移时，两项均被拒绝。Memory结构、受影响差异检查通过。独立只读builder复查确认组织答案判据、依赖分段及历史/当前入口3项重要问题已解决，无剩余重要规划问题；这是静态复查。`pytest`启动有现有Requests依赖组合警告，未导致用例失败，当前环境记录与Python锁定待办保留。

精确前态、候选、命令/报告、隔离副本与保护检查统一存于`outputs/preflight-review/2026-11-18/`（18日开工，19日完成）；目录被Git忽略，换环境须重新取得。检查器只检查结构和明确映射，不证明语义、真实批准、供应商行为或历史运行证据仍适用。业务代码/浏览器/Standard/模型及真实A–F本轮均Not run，不复跑未受影响的历史产品测试。

| 后续工作包（沿既有任务） | 可开始条件与出口 | 当前调度结论 |
| --- | --- | --- |
| 归档与持久解析：T019-PDF-01/02 → T029-PDF-01技术片段 | 本地实施请求明确；固定保存包schema、合成输入、隔离目标和既有安全能力。先产物持久/恢复，再IR | 工程依赖已澄清；本轮不执行产品代码。真实G1/Standard断言另候P0真实条件 |
| Pi制品与出口：T024-PI-01/02 → T029-PI-01 | 同一范围实施授权、SDK/profile与回环合成环境；可与归档独立推进 | 不等待PDF全部完成；不选真实provider或启用未许可出口 |
| IR→审核/切分→完整索引：对应PDF直接依赖图 | 消费上游已验证技术片段；真实源资格另判断 | 不重做已有上传/权限/Worker；不接纳ready_partial为正式代际 |
| PDF→Pi组织答案：T012-PI-01、T013-PI-01、T025-PI-01 | 消费child/支持组与完整代际接口；API18.5的answer-plan/1固定门禁 | 自由改写不自动正式发布；本批无新增答案人工签批流程 |
| 真实资料/服务与最终验收：P0、T024-PI-03、T029/T030 | 实际资料用途/职责/人工参考、Standard就绪、所用服务及保留恢复边界分别落实 | EXTERNAL_PENDING仅作用于对应真实活动；G1不替代设备三角色A–F |

**连续推进**：将一个工程项标IMPLEMENTED后，自动选择同授权范围内的验证项；失败回原工程项修复，成功后推进已就绪下游或独立分支。不能因子项“停止/交付”而要求用户重复发送继续；当前请求只覆盖本次审查，不能借历史开发批准跨入新的业务实施。没有就绪授权工作时，记录准确的下一包、缺项和解除条件。

**历史阅读边界**：以下历史实现表和“执行检查点”中的“当前/本轮/继续/交付后停止”只描述该条形成时的任务，不是当前调度指令或新增权限。当前执行以本节、STATUS和用户最新范围为准。



### DESIGN-PDF-001：PDF 文档处理架构专项设计

- 任务/授权：2026-11-18用户要求工程调查→现状审计→官方选型→架构/IR/切分/QC/最小评测SPEC→一致性检查，仅文档。**本任务覆盖范围到SPEC交付为止；审核前不得按旧续推授权自动转入PDF实现、依赖安装、数据库修改或真实调用。**原34任务/勾选和真实A–F验收状态不变。
- 基准：HEAD `0e2d4611dffebd9b24f933a5cf3e8c602bc9f6dd`＋既有未提交工作区，AGENTS SHA-256 `ff577a0013825deb998dfb08f65cf043cb853a5fa53a30c0e586c5606b0fa194`；范围为既有8份文档，231份源码/配置/治理保护对象和文档前态见本地忽略的`outputs/pdf-processing-spec/2026-11-18/baseline.json`。默认SQLite只用只读immutable查看schema，不查业务行、不迁移。
- 已调查：实际本地同步PyMuPDF/Basic与云受控异步链、临时Middle清理、云归一化结构/资源损失、Block级关键词检索与向量存储、租约/未知任务恢复、旧knowledge_base独立演示链。默认库及ORM缺Page/实际Chunk/Asset映射，现有数据库第6节只是部分目标设计；当前已有图形/矢量清点，旧“只有正文块”结果限定为修前复现。
- 已编写：主体[SPEC10](../../doc/requirements/SPEC.md#10-pdf-文档处理架构-spec2026-11-18待审核)、唯一字段契约[数据库22](../../doc/database/数据库设计.md#22-document-ir-数据契约候选01)、[固定MinerU审计](../../doc/architecture/模型与解析方案.md#11-mineru-版本能力与输出契约审计)、[API24](../../doc/api/接口设计.md#24-pdf-处理与-ir-衔接候选)；PLAN与索引只导航，不复制完整字段或任务状态。官方Latest及本地安装为4.2.3，不将云API v4与自托管/DocVortex协议混用。
- 方案状态：首轮及四约束修订的历史结论保留；用户本轮明确以已审核SPEC制定Plan/Tasks。当前仅规划授权，完整实现仍未批准；以下子任务为待执行范围，不是本轮可执行命令。云许可不作为本地链路前置，实际人工参考和Standard就绪限制正式解析实施及质量验收。

实施顺序由本轮[PDF Plan](plan.md#pdf-processing-实施-plan2026-11-18待计划审核)和下方[PDF实施任务](#pdf-processing-实施任务p0p6)接管；旧七包表被细化，不再维护第二份依赖表。原34项任务、勾选与历史证据保留。

- 首轮检查（历史）：新增本地链接/锚点、原34任务身份/勾选、231份源码/依赖/治理保护对象及Git HEAD核对通过；当时候选/结果见`outputs/pdf-processing-spec/2026-11-18/checks.json`、`candidate.json`。限定builder复核指出并闭合五类实施问题及同IR重切分分支，返回nothing important left。该结论仅覆盖首轮文档，不自动覆盖本次修订或实现。
- 首轮未运行（历史）：新的MinerU/视觉/Embedding或外部服务调用、产品/浏览器测试、DDL/迁移、A/B/C基准及真实A–F。当时没有新已批准决策，Memory未改；本次复审新决策及其Memory更新见下方，不混用两次检查证据。
- 首轮停止（历史）：SPEC交付后等待审核。本次用户已初审认可方向并确认部分决策，不再重问这些问题；更新后的实施表和下方复审检查点取代原“S1–S3/P4先行”的顺序，开发与真实验收仍须另外批准。

#### 复审修订：四项工程约束（2026-11-18）

- 授权仅补文档/一致性审查，完整实现未批准。原有原件/批次/切分及恢复设计已存在，不复制新文档；本次补归档清理屏障与独立产物、六里程碑/审核失效判据、五类关系逐项验收、完整代际独立构建/启用/原子切换/回滚。权威位置为SPEC10.4.1/10.6–10.8、数据库7.2/22.2/22.4/22.7及API7/24。
- 本轮代码静态核对仍见本地TemporaryDirectory清原Middle、ready ChunkSet仅标记、critical集合未完整识别参数/单位/条件/步骤、create_index自动激活ready_partial。设计修订不代表这些缺口已修复；源码与首轮231保护对象一致，G1文件身份匹配既有正式清单，未运行MinerU/模型、迁移或产品测试。
- 复审DONE/VERIFIED_DOCS：8份既有文档的18个新增本地链接/锚点、233个保护对象（含源码/依赖/治理、G1 PDF和默认SQLite）、原34任务身份/勾选及Git diff检查通过；Memory结构校验通过。有界builder指出并闭合Run级Parsed恢复聚合、冻结关系与独立审核证明、旧技术资格失效后的显式quality_repair接管三处冲突，定向复核返回nothing important left。只证明文档可规划，不代表解析、业务或生产验收通过。
- 复审证据/精确候选位于`outputs/pdf-processing-spec/2026-11-18/review-01/checks.json`与`candidate.json`，保留独立baseline及首轮证据。本轮未运行产品/模型/数据库迁移/A–F或A/B/C，不升级原T任务；新关系审核scope、质量修复模式和完整索引门禁仍待实现。
- 交付后停止等待审核，完整实现仍未批准。下一动作是审阅新增契约及具体实施授权；G1原件已具备，人工参考/实际核验职责与Standard模型包就绪仍待落实，不能先标Verified或声称首个闭环完成。


#### PDF Processing 实施任务（P0–P6）

本清单唯一维护PDF子任务、直接依赖、验收和停止条件；它细化既有父T任务，**不新增顶层任务体系、不重做已实现能力、不改变原34项勾选**。规范引用简写：S10.x=[项目SPEC10](../../doc/requirements/SPEC.md#10-pdf-文档处理架构-spec2026-11-18待审核)对应节，DBx=[数据库](../../doc/database/数据库设计.md)对应节，API24=[PDF接口契约](../../doc/api/接口设计.md#24-pdf-处理与-ir-衔接候选)。FR/SC继承父任务，非新增需求；阶段与技术决策在[Plan](plan.md#pdf-processing-实施-plan2026-11-18待计划审核)。

**统一执行契约**：以下23项待执行，当前状态从本文件顶部调度入口读取。P0为准备/真实验证；其余“工程”任务只交付业务代码/迁移或界面，不混入文档收敛和测试运行。“验证”任务只补必要检查/运行/记录，不修业务代码；失败返回原工程任务，修复后只重验受影响断言。工程交付先标IMPLEMENTED，对应验证通过后再标VERIFIED；checkbox需本任务验收证据齐备才勾选。每项均先核对当前候选/治理修订和允许副作用，不从旧授权、父任务未勾选或本计划推断新权限。

每项`直接依赖`是可检查的工程能力图，边指向所消费的已验证片段，不表示整个父任务必须关闭；`运行边界`另限定真实数据、环境、许可和质量断言。合成与真实结果分别记录。验证任务的技术片段通过后，下游本地工程可继续；仍欠真实断言的该验证整项保持未勾选。

基础依赖以T004/T007/T008/T009/T010/T018的**已验证能力片段**（隔离迁移、DM授权、幂等/ETag/lease、审计、私有存储、版本身份）为准，不要求这些整项先关闭；若片段失效仅阻断使用它的子任务。P0真实条件未齐时可在同一有效本地实施授权内做隔离工程及合成技术验证，不能据此开启真实处理、正式发布或质量验收。测试与真实业务证据分开保存；真实企业内容/标注/原包不进Git。

##### P0：实施准备与前置条件

- [ ] **T028-PDF-01｜G1人工黄金参考及实际职责**（准备；EXTERNAL_PENDING，父T028）
  - 目标/依据：冻结Leybold P5.1.2.3两页参考，落实业务人员关键核验、KM登记/发布、平台技术QC的职责；S10.6–10.8、FR-001/014、SC-001/002/006。
  - 直接依赖：T002。
  - 运行边界：T002已有格式；本地G1原件身份已核对；实际有权业务核验人及本地处理/访问用途依据，不能由Agent扮演。
  - 模块/交付：复用`artifacts/`参考格式及`outputs/first-release-baseline/`候选登记；实际参考存受控忽略目录。交付每条关键事实、实际存在五类关系的端点/part/作用域、原页区域、完整支持组、问题及拒答答案、真实审核依据/日期。没有表格/警告时记N/A，不伪造。
  - 验收/验证：逐项人工打开两页原件核对；原文版本/适用范围未知明确保留。参考用于结构验收的用途与正式查询发布的业务资格分别登记；拟定岗位/Agent草稿/模拟答案不算黄金就绪。
  - 停止：有真实签认及用途边界后交付P1；无人核验或无法确认的事实保持EXTERNAL_PENDING，停止正式质量判定，不猜值补签。

- [ ] **T024-PDF-01｜Standard固定环境与真实输出契约验证**（运行验证；EXTERNAL_PENDING，父T024/T019）
  - 目标/依据：在获准隔离环境验证4.2.3 Standard能实际处理G1；S10.3/10.7/10.8、模型方案11、FR-006/012。
  - 直接依赖：T003。
  - 运行边界：T003环境边界；G1获准本地解析用途（T028-PDF-01可先交付此用途片段）；安装/下载/推理另获明确授权，目标机/模型目录可用。实际黄金质量判断仍等完整T028-PDF-01。
  - 模块/交付：核对`ai_models/mineru-venv`、现有`local_mineru.py`调用契约及`config.py`配置，不改业务源码。保存包/模型/后端修订、权重完整性、tier/parse_mode/producer、命令与输出manifest、冷/热耗时和峰值资源，完整ZIP/资源落受控忽略目录。
  - 验收/验证：真实Standard推理而非仅包检查；Middle schema=docvortex.middle/2.0、两页原页映射及全部资源可读取，实际profile符合固定请求；关键差异逐项送人工参考，不以模型自评通过。
  - 停止：输出契约/profile和容量可用于P1且记录限制后退出；权重缺失/OOM/协议不符则保留诊断，停止正式接入，调整须有据；不降Basic、自动上云或声称黄金验收通过。

- [ ] **T010-PDF-01｜留存、历史与回退运行边界**（准备文档/实际责任确认；EXTERNAL_PENDING，父T010/T026）
  - 目标/依据：确定PDF、Run/attempt原包、两层IR、Chunk/审核和代际的留存/备份/清理职责；S10.1/10.8、DB11/22.2、7.2、FR-011。
  - 直接依赖：T010、T026。
  - 运行边界：既有T010私有文件/T026恢复基础；实际数据/运维责任人和试点存储、隔离库边界。
  - 模块/交付：只在现有`operations.md`及所引用的受控政策登记中补实际留存窗口、法律保留、磁盘/归档配额、回退窗口、RPO/RTO、审批/恢复人；复用安全迁移指南，不另建政策文件。
  - 验收/验证：人工确认归档后清理屏障、引用保护和默认不自动清旧证据；试点可使用责任人明确批准的临时保留窗口，不能编造生产期限。检查DB＋文件一致备份及非空库升级边界。
  - 停止：试点所需窗口/容量/责任落实后交付；政策缺失不启自动清理/真实升级，磁盘不足拒新处理且保留原证据。

##### P1：解析接入与原始数据持久化

- [X] **T019-PDF-01｜产物存储与归档屏障**（工程，父T019/T010，使用T004迁移机制）
  - 目标/依据：保全每Run/attempt成功、失败和部分原输出；S10.1/10.4、DB22.2/22.7。
  - 直接依赖：T002、T003、T004、T007、T008、T009、T010、T018。
  - 运行边界：本地工程消费固定4.2.3 Middle/保存包schema、允许profile和安全存储契约，以明确标识的合成包在隔离库实现，不等待G1签认、权重下载或真实留存参数。真实Standard/G1处理与正式质量判定仍须T028-PDF-01的相应用途、T024-PDF-01及T010-PDF-01实际窗口具备；未知时不调用、不自动清理。
  - 模块/交付：扩`models.py`、`migrations/versions/`、`parse_jobs._attach_archive`；在`parsing/artifacts.py`新增清单/归档服务。实现parse_artifacts、Run/profile/parent/阶段元数据、持久attempt工作区、FileObject引用/大小哈希/完整资源检查及孤立对象对账；原云ZIP逻辑复用。
  - 验收/验证：原件/原包不覆盖；字节持久且可回查后才释放工作区；DB失败/取消/无输出/畸形包均有准确处置，失败证据不会被清理。静态契约审查，运行由T029-PDF-01取证。
  - 停止：代码交付到IMPLEMENTED，验证通过才VERIFIED；归档身份或一致性不成立则不Parsed、不向下游提交，不扩大清理权限。

- [X] **T019-PDF-02｜本地Standard持久Worker与逐页预检恢复**（工程，父T019/T008）
  - 目标/依据：把HTTP内本地解析改为持久受理，预检全部原页并保留可恢复阶段；S10.4/10.4.1、API24、DB22.7。
  - 直接依赖：T019-PDF-01。
  - 运行边界：T019-PDF-01；已验证任务/attempt/lease、授权与版本围栏。
  - 模块/交付：修改`app.parse_version`、`services.make_parse_run`、`parse_jobs.py`、`parse_worker.py`、`parsing/providers.py/local_pdf.py/local_mineru.py`；移除唯一原包的TemporaryDirectory路径。按固定Standard输出完整保存包，预检页几何/分类/图形清单先存preflight artifact；P2再投影Page表。原同profile完整页可恢复采用，profile变更新Run；不另建调度账本。
  - 验收/验证：202只在持久受理后返回，GET不执行解析；网络/推理不占长事务；失败attempt仍失败，有效页清单可跨attempt聚合Parsed；取消/失权后晚结果不提交当前集，无隐式降档/云回退。T029-PDF-01运行验证。
  - 停止：仅交付异步/恢复代码，待下项验证；页缺失或profile不符保留partial/blocked，不能按“已有文本”跳过混合页图形或自动建ready切分。

- [ ] **T029-PDF-01｜P1归档与恢复定向验证**（验证，父T029）
  - 目标/依据：验证本地长请求、临时清理和提交窗口风险；S10.4/10.7、SC-003/007。
  - 直接依赖：T019-PDF-01、T019-PDF-02。
  - 运行边界：对应代码已交付，隔离DB/私有存储和合成契约包就绪即可验证归档/恢复技术片段；真实Standard/G1附加断言另消费P0真实用途/环境/保留窗口，未具备时单列EXTERNAL_PENDING，不将本项合成通过称真实解析通过。
  - 模块/交付：复用`test_local_pdf.py`、`test_parse_service_use.py`、`test_migration_safety.py`相应节点，必要时仅新增原包/阶段中断断言；保存候选/输入/检查结果，不改业务代码。
  - 验收/验证：隔离新/旧schema迁移；G1真实原包留存；在归档前后、DB引用提交前后中断可恢复；失败partial和畸形包留存；租约过期/取消晚到结果拒绝、原PDF不变。不是每个函数各写Mock。
  - 停止：全部目标断言通过交P2；失败回T019相应子项，存储或真实用途不可用仅标相关验证受阻，不重新运行无关全量。

##### P2：Document IR归一化及来源映射

- [X] **T020-PDF-01｜Normalized IR与受检数据库投影**（工程，父T020）
  - 目标/依据：固定原包无损适配到应用IR，保留原始节点/资源/几何；S10.4、DB22.2–22.5、API24。
  - 直接依赖：T029-PDF-01。
  - 运行边界：T029-PDF-01的归档/恢复技术片段通过，固定保存包/预检schema及适配器修订；本地可用显式合成包，真实Standard映射结论仍待实际原包验证。
  - 模块/交付：新增`parsing/ir.py`，扩`models.py`/迁移、`schemas.py`及`app.py`页/块/受控资源投影；Page表、Block来源/父字段、Asset视图复用parse_artifacts。独立保存`ir/normalized.json`，以manifest导入DB；规范renderer/text_map、raw_ref、0基原页转1基、原bbox/旋转CropBox/裁区变换、未知类型与缺资源issue。
  - 验收/验证：两页及空白/失败/未请求均可对账；表格行列0基与旧BlockContent1基显式转换；外链不抓取，HTML不执行；文件和DB不双写漂移。运行由T029-PDF-02取证。
  - 停止：Normalized候选完整交付；未知协议隔离、page_only明确降级，不造坐标或丢字段使schema通过；未通过映射不得进入Structured。

- [ ] **T029-PDF-02｜IR来源、资源与派生复用验证**（验证，父T029）
  - 目标/依据：验证坐标变换、资源丢失、冻结/复用和旧引用风险；S10.6/10.7、DB22.7、SC-001/003。
  - 直接依赖：T020-PDF-01。
  - 运行边界：固定schema的合成原包/原页及隔离库可验证来源/资源技术片段；真实G1映射与内容质量另消费实际原包、用途及人工参考，不拿合成坐标代替原页核验。
  - 模块/交付：在现有本地PDF验证基础上新增必要IR契约/迁移节点；记录Normalized artifact、DB manifest及原页目视对照；企业内容不固化到测试Git夹具。
  - 验收/验证：G1全部原节点/资源可追溯；旋转/裁区/抽页/表区至少按已见风险使用最小定向样本查变换；原节点/资源断链拒绝；同profile派生Run创建本Run artifact关联并链接真实父来源，冻结后晚到结果不改IR；旧page-only不被伪造升级。
  - 停止：映射/回查通过交P3；坏来源回对应适配/存储任务，测试样本通过只证明所覆盖契约，不称完整真实版式验收。

##### P3：结构恢复、关键关系及源级质量审核

- [X] **T020-PDF-02｜Structured IR与关键语义关系恢复**（工程，父T020）
  - 目标/依据：恢复章节/阅读顺序/步骤/续接，并保留有源关键关系；S10.4/10.6、DB22.4。
  - 直接依赖：T029-PDF-02。
  - 运行边界：T029-PDF-02来源/资源技术片段通过；Normalized IR与规则修订固定，实际内容/关系正确性仍需真实参考。
  - 模块/交付：新增`parsing/structure.py`及block_relations迁移/投影；独立冻结`ir/structured.json`；复用现有本地表格/图形线索，但不以启发式结果直接确认。关系端点/part/作用域/basis、容器与leaf/observation分开保存，审核state为独立证明投影。
  - 验收/验证：树/continues无环、阅读顺序及关系同Run；公式—变量/条件、参数—单位、表—表头、步骤—前置/警告、图—图注/动作均有明确规则和候选未决状态；无证据不按邻近误连。T029-PDF-03验证。
  - 停止：Structured只表示结构产物就绪；原文不改、关系不自批，未决关键关系继续阻断正式使用；内容/端点改变建派生Run。

- [X] **T020-PDF-03｜源级QC、业务核验与有效性重算**（工程，父T020/T021）
  - 目标/依据：实现源QC及Parsed/Normalized/Structured/QC/源Verified的无环判据；S10.4.1/10.6、DB22.4/22.7。
  - 直接依赖：T020-PDF-02。
  - 运行边界：T020-PDF-02；现有BusinessConfirmation/VerificationEvent/hold及职责授权能力。
  - 模块/交付：新增`parsing/quality.py`；扩`governance.py`、`schemas.py`及现有确认scope/指纹，qc_report沿parse_artifacts。关键集合识别覆盖七类及paragraph内关键内容；leaf核验、container覆盖和observation处置分开；按原件/内容/坐标/关系/规则/人员依据判断有效性。
  - 验收/验证：源技术QC不依赖ChunkSet/ReviewSet；源Run.succeeded不被新集合失败回退；新Run不复制approved；仅新增/撤销证明不改冻结IR；真实源缺陷触发hold和修订失效，模型/OCR高置信不能过业务门禁。T029-PDF-03验证。
  - 停止：技术门禁代码就绪；业务待确认必须pending/stale，不能为进入P4伪造Verified；后续集合级判据由T020-PDF-04实现。

- [X] **T022-PDF-01｜现有管理页的原页关系核验与状态投影**（工程，父T022）
  - 目标/依据：让真实业务人员/KM在现有入口完成逐项核对，平台呈现技术门禁；S10.4.1/10.6、API24、FR-013。
  - 直接依赖：T020-PDF-02、T020-PDF-03。
  - 运行边界：T020-PDF-02/03接口交付；当前DM/业务核验职责及受控原件/区域读取。
  - 模块/交付：扩`src/web/features/knowledge/KnowledgeAdmin.tsx`、`confirmations.tsx/types.ts`、`src/server/governance_api.py/app.py/schemas.py`；显示全部页处置、关键事实/关系端点、原页/原区、实际确认依据及失效原因。受控asset读取继承当前版本权限，不暴露原包路径或执行HTML。
  - 验收/验证：Structured/QC Passed与业务Verified分开；业务核验不能被技术按钮替代；有效版可查看未选候选，候选未通过不能误显示已发布/可检索；失权后原图安全拒绝。T029-PDF-03用浏览器验证。
  - 停止：仅扩已有管理流程且可交验；无真实签认保留待核验，不替业务人员确认，不另建审核系统。

- [ ] **T029-PDF-03｜源QC、关系与审核失效验证**（验证，父T029）
  - 目标/依据：以原页逐项验证五类实际关系及技术/业务边界；S10.6/10.7、SC-001/002/004。
  - 直接依赖：T020-PDF-02、T020-PDF-03、T022-PDF-01。
  - 运行边界：合成职责/关系可验证状态及拒绝机制，必须标识合成；G1逐关系正确性、业务核验有效性另待人工参考和实际核验人，不阻塞前者且不升级后者。
  - 模块/交付：复用`test_governance.py`门禁节点，补最少关系/QC断言；浏览器验证管理页，保存真实关系匹配/未覆盖类型及人工核验结论。
  - 验收/验证：按端点＋part＋类型＋作用域逐条对照，分别报错连/漏连；错误单位/缺定义/错表头/条件丢失不能被文字准确率掩盖；依赖改变旧证明失效，单纯新增证明不改IR，源QC与ReviewSet无循环。G1未包含类型使用后续真实样本，合成负例仅作技术证据。
  - 停止：受影响技术断言与G1实际源核验通过后交P4发布分支；真实人员/参考缺失则人工分支EXTERNAL_PENDING，允许已获准的独立切分工程，不发布。

##### P4：类型感知语义切分与Parent-Child Chunk

- [X] **T020-PDF-04｜独立切分、真实成员映射与集合QC**（工程，父T020）
  - 目标/依据：将已恢复IR转为可独立重切分的证据单元；S10.5、DB22.5/22.7。
  - 直接依赖：T020-PDF-02、T020-PDF-03、T029-PDF-02。
  - 运行边界：T020-PDF-02/03及T029-PDF-02通过；源QC技术验证片段有效。生成候选不等待业务源核验或ReviewSet，正式使用仍必须等待。
  - 模块/交付：新增`parsing/chunking.py`，扩`models.py`/迁移/`schemas.py`/`app.py`；真实chunks/chunk_blocks/一层parent-child、允许splitter profile、Unicode半开区间/region映射、固定tokenizer及字节检查；集合QC报告绑定源QC。重切分POST只接受登记profile，不调MinerU。
  - 验收/验证：正文/标题/步骤/公式定义/表头行组/图注/跨页/附录按合法边界；关键依赖闭合，标题表头是有源context；超长原子项保留oversized不截断；新集合失败不改源IR/旧集合。T029-PDF-04验证。
  - 停止：真实成员和集合QC代码交付；ready仅构建完成，未满足源核验不得Verified/正式Embedding；不以改splitter_version假装已重切分。

- [X] **T021-PDF-01｜候选ReviewSet、发布及同版source_rebind**（工程，父T021/T020）
  - 目标/依据：在新集合上重新判断审核，并安全启用同一业务Version的新来源；S10.4.1、DB22.7、API24。
  - 直接依赖：T020-PDF-03、T020-PDF-04。
  - 运行边界：T020-PDF-03/04；源核验/集合QC接口可用。正式执行另需T029-PDF-03有效源核验和适用/分类依据。
  - 模块/交付：扩`governance.create_review_set/review_set_is_current/publication_is_current`、PublicationPreview/Event、`governance_api.py`、`app.py/schemas.py`及现有`publication.tsx`。候选精确绑定(Run,ChunkSet)但不写selected；显式publish/rebind，refresh与quality_repair分别预检。
  - 验收/验证：源QC＋人工源核验＋集合QC汇合新ReviewSet；未变源依据须证明等价才能引用，不继承失效approved。最终事务按既有锁序/ETag/幂等重查，原子处理有据质量hold/指针/资格事件/KB修订/outbox；不复活withdrawn/conflicted，不改历史引文。T029-PDF-04验证。
  - 停止：代码候选交验；关键项未确认/未知hold拒绝，失败保持旧指针和原阻断；源变更后整库index_stale，不宣称已可正式检索。

- [ ] **T029-PDF-04｜切分语义、重处理与来源切换验证**（验证，父T029）
  - 目标/依据：验证关键证据组、审核有效性与同版修复；S10.5–10.7、SC-001–004。
  - 直接依赖：T020-PDF-04、T021-PDF-01、T029-PDF-03。
  - 运行边界：T029-PDF-03技术片段有效；合成确认/QC用于验证切分、候选和切换机制。真实G1语义与正式source_rebind仍须实际源核验、业务适用/分类依据和授权。
  - 模块/交付：定向切分映射检查、`test_governance.py`相关并发/回执节点及管理页候选/发布操作证据；验证代码与真实参考分别存储。
  - 验收/验证：每个非分隔字符和region可追溯；必需组/长度/超限状态正确；重切分零MinerU调用；同Run新集合失败不影响旧合格集合；重新解析/改关系/改单位使旧依据失效；候选不选用、refresh/quality_repair成功及拒绝分支、未知提交原回执对账。
  - 停止：上述断言通过交P5；源业务资格不成立不发布，不用模拟签认充真实结果；失败回切分/审核工程子项。

##### P5：索引独立构建、完整性检查、原子切换与回滚

- [X] **T024-PDF-02｜child Embedding与完整代际构建**（工程，父T024/T012）
  - 目标/依据：从合格来源独立构建可验证的新代际，替换Block唯一向量限制；S10.4/10.5、DB7.2/22.5/22.7。
  - 直接依赖：T029-PDF-04。
  - 运行边界：T029-PDF-04切分/切换技术片段通过；固定BGE-M3 tokenizer/1024维profile及输入限额契约。真实向量化还需该用途服务/数据许可；许可未齐只准隔离技术验证，不运行企业嵌入。
  - 模块/交付：扩`embedding_jobs.py`、`embedding_worker.py`、`vector_store.py`、`models.py`/迁移、`services.create_index`；固定embedding_profiles、全库expected_sources/child manifest、generation隔离chunk_embeddings；原账本/lease/预算继续复用，进度可恢复。
  - 验收/验证：building不写active；全部当前已发布source都进expected，不合格不能省略；parent/asset_only等非向量项仍须证明关键覆盖；双向差集/重复/内容/上下文/profile/有限非零向量检查通过才ready。T029-PDF-05验证。
  - 停止：交付构建代码；ready_partial/缺成员/关键不可检索覆盖拒ready，旧索引不受构建失败影响；不原位重绑旧block_id或混模型空间。

- [X] **T021-PDF-02｜显式启用、原子切换与受控回滚**（工程，父T021/T025）
  - 目标/依据：禁止不完整代际进入正式检索，恢复动作也遵守当前资格；S10.1/10.7、DB7.2、API7。
  - 直接依赖：T024-PDF-02。
  - 运行边界：T024-PDF-02；现有KB锁序/ETag/幂等/失效outbox及当前完整DM范围。
  - 模块/交付：扩`services.py/operations.py`、`app.py/schemas.py`索引动作与`publication.tsx`既有入口；同一短事务CAS切active_generation_id、profile/manifest审计回执和outbox，保留旧代际；回滚复用相同门禁。
  - 验收/验证：启用前再查revision、成员报告、selected/QC/ReviewSet/发布/hold/profile和全库权限；未知提交先查原回执；旧代际来源失效/修订不兼容/retired不可回滚，不改旧revision；不与source_rebind拼成伪原子事务。T029-PDF-05验证。
  - 停止：启用代码交验；任一门禁失败不切指针，源已失效则安全拒查，不为可用性恢复不合格旧来源；不自动清理旧代际。

- [X] **T012-PDF-01｜child检索、关系补全与精确Evidence**（工程，父T012/T013/T025）
  - 目标/依据：命中小child后返回完整有源语境，替换全批warning追加；S10.5/10.6、DB22.5、API24。
  - 直接依赖：T024-PDF-02、T021-PDF-02。
  - 运行边界：T024-PDF-02、T021-PDF-02接口交付；Chunk/关系/资源及当前资格检查可用。
  - 模块/交付：改`services.search_blocks`及当前资格检查、`execution.py`查询工具、TaskEvidence/迁移/`schemas.py`、`src/web/features/query/App.tsx`引文呈现；提供代际内child关键词和精确向量查询路径、固定策略选择；一层父及必要关系按源范围去重装配，region证据不伪造quote。
  - 验收/验证：整代选择legacy/new路径，不混成员/分数；父/图/警告/定义逐项重鉴权，必要条件超预算减少候选或拒答；输出含所有实际Block/区间，旧历史快照仍原ID；新源/审核/ACL变化使读取、外发、交付立即失效。T029-PDF-05验证。
  - 停止：交付检索代码候选；不完整/过期活动代际返回明确不可用，不用模型补知识；无图能力时显示原图或证据不足，不从纯图猜动作。

- [ ] **T029-PDF-05｜代际完整性、并发切换与检索技术验证**（验证，父T029）
  - 目标/依据：验证不完整启用、来源竞争、回滚复活和关系漏带的已见风险；S10.7、DB7.2、SC-001–004。
  - 直接依赖：T024-PDF-02、T021-PDF-02、T012-PDF-01。
  - 运行边界：T024-PDF-02、T021-PDF-02、T012-PDF-01；隔离PG/pgvector、存储及测试范围许可。
  - 模块/交付：复用`test_embedding_service_use.py/test_governance.py`及现有PG竞争设施，补精确成员/切换断言；受控真实嵌入可用时记录实际向量，否则只标技术验证，不关闭真实检索验收。
  - 验收/验证：缺/多/重复成员、向量异常、profile/revision/审核变化拒ready/activate；building/ready_partial零正式访问；构建与启用提交窗口中断/两KM竞争一次生效，旧指针不被失败改写；合格回滚成功/失效回滚拒绝；child/关系/region及历史引文当前鉴权正确。
  - 停止：明确技术证据及真实服务缺口交P6；失败回对应工程子项，不跑无关计算/Pi全套测试，不把合成向量质量当真实召回率。

##### P6：真实端到端验收及RAG检索验证

- [ ] **T029-PDF-06｜G1真实闭环与同原包A/B/C**（真实验收，父T029）
  - 目标/依据：证明首个真实文档从接入到授权检索/原页复核可执行；S10.7、SC-001–004/006。
  - 直接依赖：T029-PDF-01、T029-PDF-02、T029-PDF-03、T029-PDF-04、T029-PDF-05、T028-PDF-01、T024-PDF-01、T010-PDF-01。
  - 运行边界：T029-PDF-01–05通过；T028-PDF-01真实黄金参考及业务资格；实际角色/权限、Standard、所用embedding与可选生成服务准入；T030已有测量字段与人工判定口径，不等待最终指标任务以免循环。
  - 模块/交付：按quickstart的G1步骤执行现有API/Worker/三角色网页相关流程；原包、IR、Chunk、审核、发布、代际与查询逐个标识。固定一份Standard原包派生A原块基线、B只加结构、C切分/QC；相同问题/预算/profile/k=8，按事实/区域评分。
  - 验收/验证：关键事实/实际关系逐项原页核对，技术四项约束与必要拒答状态通过；零静默关键丢失/无依据放行/错版型号越权；被隔离内容仍在总分母；C须同时报告可答覆盖及复核成本，不以拒答制造高准确率。
  - 停止：G1限定范围有真实证据才关闭；无真实签认/服务则EXTERNAL_PENDING，不替换为模拟通过；G1通过不称设备操作维护首期整体完成。

- [ ] **T030-PDF-01｜实测指标、成本与业务验收阈值**（测量/签认，父T030）
  - 目标/依据：给出可复核的质量/检索/人工成本结论；S10.7、FR-014、SC-006/007。
  - 直接依赖：T029-PDF-06。
  - 运行边界：T029-PDF-06实际运行记录；测量方法已按现有T030准备，实际阈值签认人可用。
  - 模块/交付：更新现有`artifacts/metrics/status.md`非敏感状态、实际明细保存受控忽略目录。记录字符/数字单位、逐关键项/五类关系、证据组、Recall/Precision@8/完整支持召回、错出处/拒答/可答覆盖、耗时资源tokens/费用已知值、人工分钟数及A/B/C差异。
  - 验收/验证：人工参考确定分母；普通文字误差、检索/关系阈值、吞吐/人工预算由业务与运维依据实测确认；未知费用/指标不填0，小样本不报虚构p95或生产结论；不另跑全链路凑指标。
  - 停止：已测量并记录签认/未决限制后交扩样；没有阈值只能报告测量，正式“达标”与T031最终关闭继续待定，不降低安全断言。

- [ ] **T029-PDF-07｜按实际缺口扩展及设备三角色验收**（真实验收，父T029/T028）
  - 目标/依据：补足G1不覆盖的扫描、双栏、复杂/跨页表格与设备警告/操作；S10.7、原quickstart A–F、SC-001–005。
  - 直接依赖：T029-PDF-06、T030-PDF-01、T029-PI-02、T024-PI-03。
  - 运行边界：T029-PDF-06、T030-PDF-01测量结果；逐份扩展样本的原件身份/授权/人工参考与实际岗位就绪；设备answer场景还须Pi组织答案技术门禁及实际生成用途准入，不能用retrieve/摘录代替首期组织答案。不要求全部89份先确认发布。
  - 模块/交付：优先S1–S3真实手册按缺口取页/章节，P4公开PDF先冻结实际字节；真实三角色网页A–F及原页检查记录更新既有验收入口。G1缺少的类型列coverage缺口，X1仅定向回归。
  - 验收/验证：实际文字/扫描/图文设备资料及完整页清单；关键警告、表头单位/关系、来源、权限/撤权、版本冲突和故障恢复满足原验收；按未见文档/章节留出，不能用同页裁图当盲测或把合成岗位签认为真实。
  - 停止：被声明支持的类型/设备场景全部有相应真实证据才关闭；缺类型/职责保持未验证或收窄支持声明并提交审核，不自动扩引擎/模型/格式。

- [ ] **T031-PDF-01｜PDF实施证据与文档收敛移交**（文档，父T031）
  - 目标/依据：将已实现/已验证/剩余限制准确映射到原文档和任务；S10.8、FR-014、SC-001–007。
  - 直接依赖：T029-PDF-06、T029-PDF-07、T030-PDF-01。
  - 运行边界：T029-PDF-06/07、T030-PDF-01的实际结果；所有所声明能力/阈值具备有效证据。可先独立更新已有结果，不因缺真实证据伪关闭。
  - 模块/交付：只更新现有SPEC/模型方案/数据库/API的实现映射、plan/tasks/STATUS及既有验收/指标入口；链接精确源码/候选/输入/环境/结论，不增会话报告体系。
  - 验收/验证：对照当前候选静态审查、任务依赖及本地链接检查；父T任务未覆盖部分仍未关闭；正式发布/真实升级须有单独授权，历史/未决/回退限制明确；本项不改代码、不重跑业务验收。
  - 停止：适用PDF交付与证据一致后移交审核；未过条件保留未验证/EXTERNAL_PENDING，不宣称整项目DONE、不自动进入计算或报告开发。

#### PLAN-PDF-001：本轮规划检查点（2026-11-18）

- 授权：基于已审核PDF SPEC制定Plan和可执行Tasks，仅文档；用户没有批准业务实现、依赖安装、DDL、P0真实运行或生产切换。
- 交付：现有plan补P0–P6/真实差距/迁移回退/阶段门禁；本清单23项父T子任务，逐项有依据/前置/模块/结果/验收/验证/停止；research/data-model/contracts/quickstart就地补对应规划，不复制IR字段或另建任务台账。
- 当前依赖：P0人工参考/实际职责、Standard模型与目标机就绪、留存回退边界均EXTERNAL_PENDING；G1原件身份已有，云准入不阻断本地准备。后续可执行动作是计划审核及P0相关范围授权/真实责任落实，不能从本次文档完成自行开工。
- 验证：本轮仅静态源码/契约核对、文档依赖/引用/原34任务与保护对象检查；结果及精确候选在`outputs/pdf-processing-plan/2026-11-18/`，不是产品测试或真实A/B/C通过证据。
- 状态：DONE / VERIFIED_DOCS（仅本轮Plan/Tasks交付）。23项子任务字段齐备、直接依赖无环、178条本地文档链接/锚点有效；原34项身份/勾选及174个源码/依赖/治理/原件/默认SQLite保护对象未变。独立约束复核指出的验证指南自前置已修正。未运行产品/模型/数据库迁移或业务测试；原34任务及23项子任务仍未升级完成。本轮交付后停止等待审核。

### 首份手册隔离演练（T028/T029/T030/T034，2026-11-18）

- 用户要求以真实手册、模拟岗位与授权执行本轮。上传既有真实原件，核对文件哈希和编号，代理按三页原文编制参考答案，两份实际原件未经改写。演练业务签认和授权仅在专用本地实验库成立。
- 完成 A/B/D/F 与 FR-017 原入口反馈、回复、重查和历史快照复核。C 验证待确认草稿隔离、参考开放关闭和冲突保护暂停，员工端仍待区分冲突原因。E 拥有真实 PDF 解析、扫描待核验、人工修正与拒绝发布的局部证据，自动 OCR、印刷页码、表格及警告结构仍待验证。
- A 的三个中文问法最初未命中。移除中文双字词的 80 字限制并通过定向回归。实测目录与异常/复测证据见[隔离演练报告](../../outputs/real-manual-pilot/2026-11-18/README.md)。T028/T029/T030/T034 整项范围沿用原清单，局部结果不替代未覆盖的条件。
- C/E 补测提供 T012/T020/T029/T030 的隔离证据。C 使用同一真实原件和模拟冲突事件核对员工可见结果与授权。E 整本扫描从 63 页修正到 72 页识别，抽查印刷页码、表格和警告，复测待核验及发布预检。组织环境核验与扫描内容选用仍按原条件进行。判定和剩余风险见[场景验收记录](artifacts/acceptance/status.md#真实原件与模拟角色演练2026-11-18)。
- E 扫描表格漏识的本地修复与隔离复测已完成，覆盖同一真实原件的关机警示、× 键、授权裁图和留在待核验状态的出口。T020/T029/T030 的组织验收及逐原页专业核验范围仍按原条件。具体判定见[扫描表格补测](../../outputs/real-manual-ce/2026-11-18/README.md#扫描表格警示与按键补测)。
- 已逐项核对 29 处键位与印刷操作说明，保留不可靠图形的原表和待核验状态，验证直接标记核验通过时的拦截及原图授权访问。T020/T029/T030 的本轮判定见[复杂图形键逐项核验](../../outputs/real-manual-ce/2026-11-18/README.md#复杂图形键逐项核验)。
- E 追加按原页校正 20 个待核验单元格及 26 处图形。通过现有接口创建人工批次，在 Chrome 分别重新核验两张表，并验证原件、父批次、权限和整批发布门禁。T020/T029/T030 局部证据见[校正与重新核验](../../outputs/real-manual-ce/2026-11-18/README.md#图形键按原页校正与重新核验)。

### T034 与首份真实手册试点准备（2026-11-18）

- 本地完成范围、候选、Chrome／PostgreSQL证据见[验证包](../../outputs/readiness-followon/README.md)。反馈通过已审批的显式受理范围进入管理端。没有可确认关联资料的反馈保留待受理。已就绪的新版本通过当前索引和权限派生重查入口，另建任务且保留原回答。局部验证保留T034整项验收状态。
- T028复用三份候选、十项答案草稿和四项扩展交互候选。T030先采用已建的测量口径。首份真实试点消费有效岗位/访问依据、手册业务和专业签认、人工答案、适用服务准入，以及持久数据保护前提。取得后先跑一份获准手册再执行真实A–F，当前无真实运行记录，T029/T030/T031以真实结果关闭。

### BASELINE-FIRST-001：2026-11-18本地服务用途衔接

- 授权/范围：消费已审文档基线的T024/T019及Pi服务修订、逐用途许可、业务出口选用；仅本地合成样本，默认SQLite隔离，无真实授权/企业外发/部署。前态290源文件、34原任务及旧291证据身份见outputs/service-use/2026-11-18/baseline.json；HEAD仍0e2d4611dffebd9b24f933a5cf3e8c602bc9f6dd，保留既有未提交工作区。
- IMPLEMENTED：0009逐用途预检/选用/停用、不可变任务pin/精确合成输入证明/预算与调用账本；业务ParseRun原batch恢复及原ZIP保留；已发布精确块BGE作业/pgvector原子写入；Pi固定修订与独立用途证明、发送前及返回门禁。默认local/关键词保持，显式selected失败不回退。
- VERIFIED_LOCAL_SYNTHETIC：443项合并检查通过（0失败/错误/跳过）；前端16客户端/类型/构建通过。定向实际Chrome/PG、Pi1.5.2、独立解析/嵌入子进程和HTTP/TLS均使用合成资料；实际节点/源码/异常及修复见[本轮证据](../../outputs/service-use/2026-11-18/README.md)。解析进程实测包括退出及受控时钟推进，不声称真实等待121秒或覆盖所有崩溃窗口。
- 修复依据：未知解析曾可用新HTTP key创建新pin，已按version/file/hash未决逻辑动作复用/拒绝并验证并发；迁移schema指纹已补服务/作业定义；嵌入首轮撤权测试等待到达超时，保留失败与后续实测，不降低原子性/权限断言。
- 保留：原34项整任务勾选不变，真实A–F NOT_RUN；签认/适用确认/人工答案/真实服务准入TODO。SDK自动摘要/压缩全流程、真实语义质量/费用健康、许可续期、原ZIP孤儿清理/留存仍未验收；既有构建链high及历史截图缺口保留。
- 下一可执行依赖：完善独立本地管理生命周期及其剩余浏览器/异常恢复覆盖，T034反馈与运维职责/对账/保留删除按既有T任务推进；真实材料具备后才消费T028→T029/T030真实A–F→T031最终一致性关闭。不得因等待真实事实停止可独立工程，亦不得从合成测试推导真实准入。

### BASELINE-FIRST-001：2026-11-18文档编排收敛

- 授权：统一文档冲突/状态，补FR—任务—验收—证据及依赖，真实签认/人工答案/服务准入先TODO；仅文档与本地审阅产物，无代码/真实授权/外发/部署变更。
- 前态：HEAD沿用0e2d4611dffebd9b24f933a5cf3e8c602bc9f6dd；上一219源文件/199封存artifact核对无漂移，173保护对象及34条任务原文/勾选由outputs/development-baseline/2026-11-18/baseline.json记录。保留原未提交工作区。
- 文档交付：原生baseline.md定义分阶段入场/责任与真实待办；traceability.json/traceability.md覆盖17FR/7SC和34任务，静态/合成/无运行证据分开；本节维护直接依赖和能力分段，明确准备与最终验收不同。
- 验证边界：仅运行文档/关系/来源/哈希保全检查，不重跑404产品检查、不修改或勾选原任务；原运行证据按代码身份及实际范围复用，真实A–F/指标仍未验收。文档完成结论及审阅包见[本轮证据](../../outputs/development-baseline/2026-11-18/README.md)。
- VERIFIED_DOCS：17FR/7SC/34任务、18类证据/108条准备依赖及原任务显式标签均已核对，173个保护对象与旧199封存artifact保持。首个720链接/84-artifact检查快照保留；最终状态同步后复核的文档身份和审阅包见[final候选](../../outputs/development-baseline/2026-11-18/final/README.md)，不把文档交付升级为T031整项完成。
- 续跑：本轮停止边界为可复核文档基线交付。后续如进入代码阶段，先消费T024/T019及Pi的服务修订/逐用途许可/业务选用衔接包，保持独立合成验证；真实许可不由本基线生成。首期整体仍IN_PROGRESS。


### BASELINE-FIRST-001：首期开发基线及查询闭环推进

- 目标：遵循用户2026-11-17要求，形成规则一致、证据齐备、依赖可执行可验收的首期基线，继而交付可复核查询闭环；不将本轮文档/候选准备冒充最终完成。
- 准备阶段授权（历史）：先落实可逆准备，遵守此前“不要先修改代码”。用户明确选择DM显式开放精确待确认草稿原件仅供参考，正式问答仍待确认/核验/发布/索引就绪；实际业务/专业核验人员要求先自拟岗位。准备阶段仅写相关文档、.gitignore和本地候选/验证产物。后续本地代码批准见下方检查点，不改变正式89份清单、原件、真实账号/授权或部署。
- 基准：HEAD `0e2d4611dffebd9b24f933a5cf3e8c602bc9f6dd`加既有未提交工作区；治理、23份文档/配置前态和63份受保护代码/实验/配置及正式清单身份见`outputs/first-release-baseline/2026-11-17/baseline.json`。不覆盖历史输出或清零既有修复周期。
- 本轮实际推进：统一SPEC/API17.6/数据库17.6与角色/查询契约/参考开放验收；T028前移但编号/勾选不变，T029–T031使用其样本；将原三项未知原因与后续夹具维度线索关联；.gitignore保护本地样本/模型/旧解析；开发基线集中拟定职责、门槛和已知实现差距。三份原件匹配既有身份，重新渲染13个完整页，生成3份候选登记、10项参考/负面答案草稿及4项参考开放交互；没有实际业务签认或产品运行。
- 当前状态：文档收敛与候选准备已核对，可供用户评审；整体目标IN_PROGRESS。T028、真实开发入场、PI批次和A–F保持未完成/未验证；岗位草案不等于真实任命，Agent参考答案不等于人工黄金样本。
- 验证要求：现有34个任务/17项FR身份和勾选保留，T028依赖无回环；参考开放与当前权限/正式资格边界一致；修改范围本地链接/锚点、Memory、Diff通过；候选JSON结构与来源/原页可回溯，实际源文件与63份保护对象不变；隔离和审阅包不带企业正文/原件/凭据。文档/结构通过只覆盖相应范围，不执行产品或外部测试。
- 证据位置：本轮`before`、`pages`、`sample-register.candidate.json`、`reference-answer-candidates.json`、`sample-preparation-evidence.json`、`checks.json`及`candidate.json`；SOURCE/PDF资料与候选答案仅本地企业范围，源码/设计包单独脱敏排除。
- 本轮核对：既有34项任务/17项FR及原勾选保留，T028准备无反向依赖；本地链接/章节、参考候选格式、来源/页图、未晋级业务签认、Memory与Diff检查通过，原件/63份保护对象保持。一次有界独立builder check返回nothing important left，仅覆盖基线/参考开放/依赖，不是全量架构或安全运行证明。最终文件绑定、精确计数和源码/设计包校验以checks/candidate为准；此为文档准备周期，未执行产品修复/测试，不重置既有周期。
- 后续依赖：本地代码与PI-00进度以下方批准检查点为准，再按PI-01–03与T029/T030推进真实闭环。真实职责/样本签认、逐服务许可和相应生产参数逐项取得，不能用未回复、时间或本轮自拟代替。

#### 代码阶段批准与执行检查点（2026-11-17）

- 用户已明确选择“批准按该基线进入本地代码修正与PI-00验证；使用合成或明确获准样本”。此前代码禁写边界在本阶段解除；真实企业资料外发、真实授权变更、部署及业务签认未因此获准。
- 本阶段先修严格授权夹具、首次幂等回执、版本/审批已知差距，再执行固定Pi候选的最小安装/接入/边界验证；保持既定SDK/目标栈路线。准备阶段最终候选、审阅包、失败证据和原件保留；当前候选不得借用准备阶段“代码未变”证据。
- 代码阶段前态、授权与41份文件身份已保存在`outputs/first-release-baseline/2026-11-17/code-phase/baseline.json`。首轮原服务回归复现4通过/3失败，报告`before-services.xml`；不存在将旧失败静默改写为已通过。
- 实际修正：共享合成夹具补齐严格授权维度；幂等回执与业务同事务提交，重放沿原If-Match识别，待核验解析不伪报成功；通用PATCH不能写确认/核验或型号范围。发布/索引/查询绑定明确的selected_chunk_set_id，不采用最新解析；缺型号或未核验块不发布/不整批入索引，未知生效日保持空，撤回不改业务适用性。完整确认/核验集/预检和选用接口仍待T018/T020/T021。
- 审批修正及额外已复现机制：登记人和实际批准人分离，默认pending/unverified，旧approve返回409；读取和新授权统一核对核实状态、受益人、单一完整范围及期限；旧approved记录迁移后保持原值但unverified，不产生有效权限。移除未被HTTP使用的create_default_kb隐式自批/自授予。真实外部审批登记/核查流程仍待T023/API19，不能通过此修正登记真实批准。
- 迁移：0002保存明确切分绑定；0003增加登记人/核查状态并允许实际批准人未知。新建及冻结最小v1升级检查通过，保留原件引用/审批历史，不推断旧绑定或核实状态。尚未验证完整历史生产库、PostgreSQL或并发升级；未对真实库执行迁移。
- 验证：原7项夹具修正后全通过；新增HTTP修复前7项、审批身份/范围前5项、状态/警告/型号前3项分别复现失败，原报告保留。最终43项服务/HTTP/权限/迁移合并回归通过，见server-final.xml。受限环境TestClient事件循环socketpair阻塞已定位，使用获准环境运行同一合成用例；不将环境中止算产品通过。SDK初次类型构建和本地网关就绪竞态已修正，最终TypeScript构建及8项SDK/本地真实HTTP检查通过，见pi00-candidate.xml；没有浏览器或业务验收。
- PI-00范围：固定SDK1.5.2、源码b4afdddf44e059c67ccf5097e5317d913521b470、Node22.20.0及npm锁文件，禁安装脚本；资源/设置隔离、受控工具/finishTurn、会话重建、显式压缩已观察。主回答/压缩/辅助摘要经真实原生adapter及本地FastAPI合成网关，篡改/重放/授权后撤销被拒，上游失败不重试；官方faux只用于前5项响应控制。未接真实业务权限/MemorySelection/租约/预算/持久账本，不证明生产PI-AC02或PI-00完整准入通过。
- 证据：当前代码、SDK/锁文件/许可清单、修复前后JUnit、增量patch、审阅快照及候选哈希统一见`outputs/first-release-baseline/2026-11-17/code-phase/README.md`；准备快照和历史CACHE-PERM报告保留，不复用其“代码未变”结论。真实样本/正式清单、既有实验与治理保持。
- 周期/状态：本轮本地修正与PI-00合成可行性证据已交付，完成1个整体修复周期（多次定向复现/修正/合并核验不重置历史周期）；首期目标仍IN_PROGRESS，34项任务勾选不升级，PI-00完整门禁未通过。下一执行点先补PI-00真实投影/许可/持久回执及隔离证明（PI-01业务接入前置），并补T018/T020/T021/T023领域流程，再按PI-01接入Worker宿主最小桥接；取得真实职责/样本签认及对应服务准入后才执行PI-03、T029/T030 A–F。

#### 后续授权与本地治理/PI-01–03检查点（2026-11-17，当前）

- 授权：用户明确要求按依赖补权限投影、许可、持久账本和审批/核验/发布，再推进PI-01–03；仅本地及合成/明确获准样本，保留证据，真实验收前不宣称闭环。上述首轮43项/8项及旧“下一执行点”为历史，由本节承接；不要求用户再次发送继续。
- 基准与写入范围：HEAD仍为`0e2d4611dffebd9b24f933a5cf3e8c602bc9f6dd`，保留既有dirty/untracked。`outputs/local-foundations/2026-11-17/cycle-1/baseline.json`绑定治理、121份前态及上一候选无漂移检查；本轮只改相关源码/测试、迁移和实施映射/状态/命令文档及本地忽略证据，不改真实手册、89份正式清单、账号/授权、外部服务、部署或真实库。
- 治理基础：0004及领域/API加入真实审批/确认登记、附件、受控证据核查/撤销、精确ReviewSet/选用、依赖冻结发布/撤回预检、hold及参考开放；仅附件/状态/管理员点击不能核实。合成根由隔离测试建立，无公共自任命/attest入口；真实业务/专业/授权根和签认仍缺。当前批准只覆盖user受益人，role受益人不假实现。
- 模型基础：0005及私有模型API加入可信任务受理范围、当前权限/来源/索引投影、封闭adapter/body hash许可、持久预算/调用意图/回执、步骤+会话双租约/epoch和一次派发；未知用量不计零、未知调用跨run保留预留并fence。投影正文仅临时内存，持久为主键/hash依赖，避免复制正文/凭据；SDK私有日志仍按敏感派生数据管理。
- PI-01：显式本地outbox Worker、真实Node/Pi SDK1.5.2子进程及stdio工具桥，绑定制品/配置、心跳租约和不可变私有日志快照；GET状态/结果不执行任务。失权/旧来源日志隔离重建，已提交的T16不被后续SDK日志错误伪改为未交付。未接真实Celery/RabbitMQ或证明全部杀进程窗口。
- PI-02：0006持久真实消息/条件与CAS/tombstone、冻结原始输入/有效输入/执行修订、真实工具逻辑动作与Pi调用映射；只向SDK启用T01/T03/T04/T05/T14/T16/T17/T18查询子集，其余12项不启用。完整四层和20工具AC仍未通过。
- PI-03：当前权限与已发布选用证据→完整警告/出处→T14校验→T16回答快照，模型自由文字不直接交付。缺型号由真实员工响应恢复原任务，条件头变化不追改旧任务；分享绑定确切快照，接收者看不到原输入/其他会话。真实SDK/native adapter/回环HTTP与有效合成PDF通过本地路径检查；上游503不自动重发，显式恢复保留同任务预算和私有日志。
- 实际缺陷及修正：autoflush=False导致待写发布状态被旧行覆盖，改为资格快照前flush；ACL访问模式不再冻结为业务资格、当前ACL仍实时核查；已选用批次不可原位核验、未选用新run不破坏旧选用。待确认草稿上传/参考开关/核验不再误增formal corpus修订；首次未知分类只从可信精确事实补None，完整管理tuple后验失败在savepoint回滚，既有限制/历史不被新草稿降低。T17/条件头并发改为SQL CAS，最终工具写入绑定双租约和当前任务修订，旧执行证据不混入新回答。均为合成复现/判别，不推断真实事故原因。
- 独立复核修正：126项聚合后，跨模块只读复核发现取消与旧执行写入缺少共同任务fence。修前真实HTTP判别3/3失败：T04复活为running并写证据/回执，T17复活为waiting_input并写补充请求/回执，Worker取消后仍成功准入run。统一task_write_guard按owner/status/revision/execution事务CAS，于准入/首个工具写/缺型号请求/Worker状态及取消入口执行；T16原双租约/最终CAS保留。修后4/4通过，取消保留且无上述新副作用，加入accepted检查后取消的准入判别。证据runtime/cancel-fence-before.xml、cancel-fence-after.xml和report.json；PostgreSQL锁行为仍是实现推断，不作运行通过。
- 验证：中间`aggregate.xml`115项通过；收尾发现草稿/首次分类缺口后新增11项治理判别，24项治理定向通过。126项修前聚合保留在before-cancellation-fence/；取消修正后重新冻结56份代码/配置执行最终fresh聚合，取消修正后130项通过；收尾契约核对发现T14/T16临时别名与原Pi固定映射不同，已统一为dc_artifacts_validate/dc_artifacts_deliver，新增Python启用集/Node白名单/20项目录映射检查（修前1失败、修后1通过）。130项候选和审阅包归档before-tool-alias-alignment，最新候选重新构建/冻结后131项聚合及8项SDK检查全部通过，0失败/错误/跳过，56份代码/测试/配置无漂移。结果以`final-test-start.json`、`final-test-command.json`、`final-aggregate.xml`与候选为准，不能用中间报告替代最终候选。SDK8项、TypeScript构建、Memory、文档/保全检查分别保存，不把编译或结构校验升级为业务验收。
- 浏览器观察：真实本机Chrome、1366×900、隔离账号上下文/仅本地路由；缺项等待→真实补充→刷新恢复→完整正文/警告→确切PDF#page=1，原PDF文本/页图核对；离线清理/恢复、DR撤权清理及系统管理员无隐式正文、知识管理员有权读原文。截图/report/PDF见本轮browser目录；知识/系统完整管理UI和真实三角色A–F未验收。
- 周期与状态：本次本地基础与查询子集已实施并通过相应合并检查，形成1个整体实施/修复周期，内部定向修复和中间报告不清零/另计历史周期；整体继续IN_PROGRESS。原34项T任务勾选、17项FR、真实A–F及正式样本pending状态均保持。PI-01–03为本地查询子集证据，不标完整批次DONE。
- 下一可执行点：复用本地协议/账本，按PI-01–03/T019–T027补目标数据库/队列、正式前端与管理/生命周期及必要中断证据；实际业务/专业岗位签认、人工参考答案/样本适用和真实审批依据落实后，再在逐服务准入范围接真实解析/检索/生成，执行T029/T030 A–F。Agent可整理材料，不能代替实际批准或验收。精确源码/文档/报告/审阅包绑定见本轮README/candidate。

#### 本地目标栈与管理流程（2026-11-17，本轮）

- 授权：按依赖先PG/pgvector、Celery/RabbitMQ本地接入，再完善审批/核验/发布；仍合成/获准范围，无真实授权/部署批准，不宣称闭环。
- 起点：HEAD未变，保留既有dirty/untracked；上一144份候选无漂移，baseline见outputs/local-stack/2026-11-17。沿用34项任务/17项FR及历史失败，未勾选真实A–F。
- 基础设施：用户打开Docker后，独立回环PG18.7/pgvector0.8.7迁移head0007与9项向量门禁通过；RabbitMQ4/Celery5.3.4 solo真实消费及PG/Node联合11项通过，重复dispatch无新增snapshot/run/call/budget。队列仅ID，publisher SQL CAS/confirm，未知确认同dispatch复核；不自动重试新模型调用。实际source绑定见postgres/pgvector-evidence.json与queue/report.json。
- 管理端：本地页面/精确批次投影、pending审批及确认材料关联已实际Chrome走通核查/撤销、完整核验/预检/发布/索引/撤回及失权清屏；未核实材料仍拒绝，专业确认/角色依据受可信证据门禁，无公开attest/自任命。对象级DM、无职责替换审批拒绝、确认关联不自证3项与原治理24项定向通过。页面异步重入导致页签永久disabled已确定性复现，统一操作锁修复后真实Chrome两项通过；冻结后157项合并检查全部通过，0失败/错误/跳过、源码无漂移。另在两个新建并迁移0007的PostgreSQL库复用两套真实Chrome管理流程，2项通过，源码无漂移且库已清理；report.json保留逐库回执。首轮154通过/2失败的聚合、原缓存夹具与UI锁问题修复证据分别保留，不改原34项任务或A–F状态。
- 执行事件：一次误将Alembic指向默认SQLite，当前head0006且所查业务表0行，事前状态未知，影响未判定；已停止对其操作，不自动回滚，incident.json保留。其余迁移/测试显式隔离目标；不从事后空表断言从未有影响。
- 下一步：沿已验证本地依赖补正式前端/完整管理与并发、全中断及恢复验证；仍需实际岗位/手册签认与人工答案、逐服务准入、正式前端/全生命周期及真实三角色A–F，首期整体IN_PROGRESS。

#### 默认SQLite影响、防误迁移与正式前端/恢复（2026-11-17起，跨日至18日）

- 授权/依赖：用户要求先事故核查及保护，再正式前端与管理/并发恢复；仅本地、合成/获准样本，不更改真实授权或部署。起点160份候选无漂移，baseline在outputs/local-hardening/2026-11-17。
- 核查：只读默认主文件、内存检查，无sidecar/可写连接；原incident hash未变，0006、51张业务表空、完整性/FK通过；事前身份/备份未找到，历史影响UNKNOWN，不回滚/删除。
- 保护：移除ini隐式URL，env只准受控连接；DBAPI前隔离默认库/别名；启动不DDL/seed；显式计划/目标/前态/来源校验、锁后复核、SQLite备份及行/trigger保全、非空旧PG升级拒绝。提交前落COMMITTING，提交后证据失败/未知不误报REFUSED。修前/修后与独立复核证据保留。
- 最终验证：186项完整合并通过，0失败/错误/跳过；98份受测源码/配置无漂移。另8项客户端检查、TypeScript、Vite构建及npm审计0漏洞通过；15保护/7恢复/3管理并发/3正式浏览器包含在186内，不重复计数。独立PG旧schema拒绝及3项修正复核保留；旧30项与所有失败/中间报告不覆盖最终候选。
- 实施/真实观察：React三角色在同一候选的独立PG上完成丢响应原任务恢复/补型号/真实Node/完整警告/PDF/撤权；已有文档新版本上传/来源/本地解析/逐块与专业核验/核验集/预检发布/真实索引状态/撤回；审批登记/材料关联/核查/撤销/角色拒绝。pageerrors=[]，库清理齐备、默认hash未变。补来源GET和审批来源投影；所有未知/确认门禁保持。旧静态回归及失败/通过证据保留，原34T/17FR/89份真实样本/A–F不升级。
- 并发修复：同If-Match不同key丢写已在真实PG确定性复现；统一mutation改事务内revision-row CAS持锁，3项新回归一胜一412/同key一次操作。提交后证据故障误报REFUSED已修，COMMIT_UNKNOWN不盲目重跑。中间测试工具multipart中继/locator/撤权注入时序及实际投影缺陷分别记录。
- 终态与恢复：本轮已授权本地子流程IMPLEMENTED/VERIFIED，首期整体IN_PROGRESS。源码/网页构建/最终命令/XML/截图/哈希与审阅包见outputs/local-hardening/2026-11-17/README.md及candidate/checks。后续补完整K/S、新知识库/文档、资料生命周期/迁移历史与剩余恢复窗口；真实岗位/人工答案/逐服务准入仍需落实。

#### 服务适配与合成准入（2026-11-18，当前执行）

- 当前授权：真实签认/人工答案先列待办，持续推进独立本地工程；真实授权/企业外发/部署不在本轮范围。上一冻结候选202源文件/190 artifact核对无漂移，`outputs/service-adapters/2026-11-18/baseline.json`固定HEAD/治理/前态，不重置原任务勾选和修复记录。
- IMPLEMENTED：三类协议适配和安全传输缝、0008本地服务修订/自检/变更预检/调用账本、`/workspace/services`真实API管理页。网络只接受受控回环端点，固定fixture、模型白名单、凭据/代码/CA绑定；默认SQLite仍隔离，真实企业准入没有创建。
- 关键恢复：意图落账先于网络；同键重放不重派发，未claim的pending仅首次执行；batch_id先于上传持久化，未知有远端ID只查询，未知无ID保留待核对；Profile→Session/User→test attempt/lease围栏防旧作业或旧预检放行。安全停用不依赖健康/凭据/成功测试。
- VERIFIED_LOCAL：最终404项合并全绿（0失败/错误/跳过），127份受测源码/配置无漂移；本机HTTP及HTTPS严格CA/IP校验、隔离PG/CAS、三类Chrome服务配置→修订→自检→预检→启停/原batch恢复均通过。当前前端类型/16客户端/构建通过；另两案真子进程中断证明生成无ID保持unknown且不重发、解析已有batch只查询完成，租约到期采用可控时钟前进121秒，不冒充真实等待。默认SQLite、原34勾选/17FR/89清单及3原件身份保持；上一190封存artifact保留，代码/构建/当前命令/XML/截图/清理及审阅包由本轮candidate/checks绑定。构建依赖既有high仍保留（lock未变复用同日审计）。
- TODO_REAL_ACCEPTANCE：岗位签认、手册版本/关键内容确认、人工答案及真实逐服务准入；不阻塞独立代码/管理流程/恢复工程。真实A–F保持NOT_RUN，T019/T024/T027等仅新增本地子集证据，不勾选整项或宣称闭环。

#### 维护写提交权限围栏（2026-11-18，上一阶段）

- 授权：沿当前用户非环境无需确认继续的本地自主开发范围；247候选已封存，201源文件和247 artifact核对不变，建立outputs/local-write-authorization/2026-11-18/baseline.json。默认SQLite及真实授权/部署/外发边界不变。
- 状态VERIFIED_LOCAL / BLOCKED_REAL_ACCEPTANCE：已真实PG独立SA HTTP停用判别两案旧维护仍200/完成回执，现共享提交围栏按业务flush→KB→AuthSession→User→DM grant，重验当前会话/原有范围，末尾不commit心跳。当前253项合并全绿（0失败/错/跳过），111份受测文件零漂移；六维护次序、十二交付竞争及三角色Chrome子流程均在当前源码通过，六维护/十二交付/六Chrome库cleanup齐备。初修after统计字段短暂无Lock的5绿1红保留，修改观测SQL谓词后全聚合重跑通过，未重试业务或放宽断言。
- 续跑：代码与证据已冻结并完成最终聚合/审阅包；旧247候选的封存artifact不覆盖。所有修前错误/来源身份与cleanup保留。真实A–F的签认/适用确认/人工答案已获用户明确答复“目前尚未具备这些记录”，与逐服务准入分别列为真实验收前提；本地独立工作继续，不从默认选项或缺回复推断。

#### 自主续推：草稿隔离与事务提交（2026-11-18，当前）

- 授权：用户要求自主按依赖推进至闭环，非环境无需手动确认继续；原本地/合成/默认SQLite隔离、无真实授权/部署/外发限制继续。基准198源文件/462封存artifact零漂移，HEAD不变；baseline保留。
- 动作：SPEC9/D-013明确Agent保守默认并实施首版上传者/明确单文档DM隔离，7项定向及26相邻通过。父将旧版本/解析/纠错/核验和发布撤回重放统一活动Doc锁，实际PG4红→4绿并加两同ETag一胜一412。修前测试收集命名错误保留，不算产品缺陷。
- 交付：正式答案在Task CAS后持Doc/KB/User/grant及租约围栏再复核证据，6类变更各双向12PG证明合法提交次序；之前混合源码/无效审批fixture红报告仅逐案解释，最终当前源码以聚合及冻结12记录为准。close_run/worker未改，不扩到未观察窗口。
- 当前验证：完整聚合247项全绿（0失败/错误/跳过），110份受测文件零漂移；客户端/构建因web源和制品字节完全相同按原证据复用，full audit4 high仍失败，runtime omit-dev audit0。聚合数量/失败/源码漂移和封存身份统一见outputs/local-continuation/2026-11-18/README.md及checks/candidate，定向不重复累加。
- 真实门槛：真实A–F未执行；实际岗位/版本适用确认/专业复核、人工参考答案和逐服务数据范围/账号/TLS/费用/留存及真实服务契约仍缺，见real-acceptance-readiness.json。已具备的本地工作自主推进，不要求用户再批准继续；缺失事实保持缺失，不能由Agent生成真实签认。原34T/17FR/正式89清单不升级。

#### 创建、资料生命周期与恢复（2026-11-18，当前）

- 授权/依赖：当前用户要求先创建/生命周期，再剩余并发恢复；仅本地合成/明确获准样本，不改真实授权/部署，默认SQLite隔离。HEAD不变，起点188文件/构建无漂移，基准见outputs/local-lifecycle/2026-11-18/baseline.json。
- 实施：本人知识库登记和独立等待授权；新文档仅KB级完整DM范围创建，不自授/不分类确认/不发布；来源白名单登记、稳定私有文件安装与原键核对恢复。生命周期暂停/真实依据消解、历史/撤回原文与正式问答资格分离、删除影响/理由/CAS/原键回执及回收站最小元数据/白名单历史；永久清理与恢复未开放。
- 真实缺陷及修复：新增上传末尾会话函数commit导致401仍提交业务，红例后改无写重验并验证回滚/同键恢复；实际类别最终复核。PG墓碑后hold仍201真红，统一Doc→Version/子记录父行fence后404且零新增hold/outbox/receipt。保留旧失败/测试工具错误与独立复核记录，不修改验收标准。
- 运行证据：PG整11绿，34随机库清理、当前源码绑定；实际子进程文件安装后kill、删除/上传/发布/hold锁窗口覆盖。真实Chrome三角色走新建/草稿/暂停/删除未知重放/回收站/撤权。最终222项合并检查全绿（0失败/错误/跳过）、16项客户端及类型/构建通过，107份受测文件无漂移；当前受测身份统一看本轮aggregate/checks/candidate，不累计定向数量。
- 证据保全：旧固定路径重跑导致覆盖风险，local-stack旧候选12份截图当前哈希不匹配、289份PNG内未找到原始副本；该目录无事前比对且前轮也重跑，首次覆盖时点UNKNOWN，保留旧候选/JUnit并记录KNOWN ISSUE。其余hardening已封存artifact恢复并与旧hash一致，旧测试已改独立证据根。
- 已知/待确认：npm audit新上游picomatch漏洞4 high节点且无补丁，保留失败，不作安全通过；未知分类草稿人员隔离待用户选择，未引入新授权规则。默认SQLite保留隔离、历史影响UNKNOWN。原34T/17FR/89份清单及真实A–F不升级，首期整体IN_PROGRESS。
- 续跑：先落实未知草稿隔离决定及兼容依赖修补，再按真实缺口验证在途撤权/答案提交及其他中断窗口；完整K/S、保留/恢复和真实业务签认/人工答案/逐服务准入未完成。源码/网页构建/修前后XML/截图/最终审阅包见outputs/local-lifecycle/2026-11-18/README.md。此为本地子集证据，不宣称设备查询闭环完成。

### DOC-STATUS-001：统一状态与修改入口（T031 部分）

- 授权与范围：2026-11-16 用户明确要求统一修改入口、对齐实际情况；修改开发文档和 AGENTS 路由，保留既有代码/资料/授权/配置。
- 验收：STATUS 统一项目进度；tasks 统一任务与周期；旧设计路径可跳转；概览、角色、架构与 Memory 不再声明过期整项目状态；候选、真实检查结果、未验证项及下一步可定位。
- 状态：DONE（仅此文档入口任务）。统一状态路由、兼容链接、过期描述和当前证据已对齐，首期应用及 T031 整项仍未完成。
- 候选：Git HEAD `0e2d4611dffebd9b24f933a5cf3e8c602bc9f6dd` 加当前未提交工作区；文件哈希和检查记录见 `outputs/status-sync/2026-11-16/candidate.json`。
- 根因与实质改动：多处重复进度快照与两份首期设计造成漂移；建立汇总/任务/原始证据职责，旧规划改兼容指针，修正 plan 与 tasks 同号不同义的问题；历史勾选/通过记录保留取代说明。
- 修复周期：本范围第 1 个完整周期已通过合并文档检查；没有从命令次数折算历史轮次。应用回归只作本次现状取证，没有进行应用实质修复，不消耗应用修复周期，也不重置历史预算。
- 检查证据：本地服务回归 `services.xml` 为 4 通过 / 3 失败、退出码 1；该失败作为应用真实状态登记。文档复验检查 49 份 Markdown、290 个本地链接、51 个章节链接和 31 个任务 ID，通过 JSON 格式及 Memory 结构校验；结果与候选绑定在 `document-checks.json` / `candidate.json`，只证明本次文档范围。
- 下一步：本次入口收敛结束；后续任务从 STATUS 的“下一步与依赖”恢复，先核对候选文件哈希，应用任务保持其自身状态。

### 原有三项应用回归续跑位置（尚未执行修复）

历史说明：以下为当时续跑记录；后续代码阶段已补齐严格夹具并通过原断言，当前候选由BASELINE-FIRST-001最新检查点承接，保留原失败证据。

- 关联任务：T007/T012/T013/T020/T025；现有 `src/server/test_services.py` 的正式查询、待复核提示、撤回证据三项失败。
- 初始证据：`outputs/status-sync/2026-11-16/services.xml`；当时缺少正式查询证据，另有`access_limited`与预期待复核警告不符，根因尚未定位。后续CACHE-PERM-002第4节记录夹具缺少必填授权维度；2026-11-17静态核对_ready_fixture与当前完整授权要求相符，作为下一步定向复验的已知线索，保留原失败记录。
- 下一项可执行动作：进入获准代码阶段后，先补齐该共享夹具的真实授权维度并重验原三条断言；失败仍在才沿授权过滤/证据生成继续定位。另按T008修正CACHE-PERM-002已复现的幂等回执未完成缺陷，保留严格生产规则及既有失败证据；不通过降低断言凑通过。
- 周期衔接：本次无应用修复周期。未取得足够历史实质修复/复验记录，不能宣称历史预算为 0；恢复时按治理要求回溯已有证据，保留对应关系。

### CACHE-PERM-001：已保存历史查询的当前权限复核（T013/T014/T025 局部）

- 授权：2026-11-16 用户要求实际调查并修复一个受控缓存与权限问题。范围为本地已保存查询结果（持久化快照）的读取门禁、针对性测试及契约/状态记录；不新增缓存服务，不修改真实资料/授权，不调用外部服务。目标：版本仅被替代时仍可按当前权限查看历史结果，撤权不能借旧结果绕过。
- 依赖与规范：数据库设计 8.3 明确 superseded 历史产物可按当前权限查阅；API 8.2 要求保留快照并返回当前状态。运行依赖使用现有 FastAPI/SQLite/pytest/httpx，无新增依赖。旧三项服务回归独立保留，不把其失败算作本问题已执行修复周期。
- 状态：DONE（IMPLEMENTED / VERIFIED，仅本局部任务）；不勾选 T013/T014/T025 整项或首期验收。
- 复现：5 个参数化 HTTP 检查，修复前 3 失败、2 通过；effective 时两个账号均成功，只改变为 superseded 后所有者返回 403、分享接收者返回 404。withdrawn/draft 拒绝通过。
- 根因与修复：`evidence_is_readable` 错用只允许 effective 的 `version_is_formal`，把现行检索资格等同于历史读取资格；改为允许 effective/superseded，仍检查适用性、内容核验和当前文档 ACL；任务所有者/分享/到期/撤销检查沿用。新查询仍排除 superseded，不改历史正文，不改浏览器响应缓存策略。
- 验证：新增 5 项全部通过，涵盖所有者/接收者、结果/证据列表/证据详情/产物列表、两种 API 路径、历史状态/正文保留、新检索排除历史版本、资料撤权及分享撤销后无正文、no-store。与旧服务回归合并共 12 项，9 通过/3 失败；旧三项失败名称与先前证据一致，未修改其夹具或生产授权规则以凑通过。
- 环境：受限环境中的 TestClient 卡在 Python asyncio 创建本机 socketpair（20 秒栈定位），中止两次启动后在获准的沙箱外完成同一合成数据测试。它是测试启动限制，不是业务失败；不消耗完整修复周期。未使用真实浏览器或生产服务，未验证已下载字节追回。
- 修复周期：此明确局部问题完成第 1 个完整周期；已检查现有任务与本地验证记录，未发现该具体 superseded 误拒绝问题的历史实质修复周期，不重置其他范围预算。
- 候选与证据：HEAD `0e2d4611dffebd9b24f933a5cf3e8c602bc9f6dd` 加未提交工作区，治理与候选哈希、Git 状态在 `outputs/cache-permission/2026-11-16/baseline.json` / `candidate.json`；修复前后 JUnit 为 `before.xml` / `after.xml`。隔离数据均为合成，产物被 Git 忽略。
- 遗留与下一步：本局部已验收即停止。原三项失败的夹具未提供必填授权维度，查询在进入证据生成前即被拒绝；本次新增夹具补全合成维度后正例成立，旧失败仍需另行按原检查点修复验证。完整 A–F/浏览器/真实资料验收继续保持未验证。

### CACHE-PERM-002：原始缓存与授权症状调查（T007/T012/T013/T014/T025 局部）

- 授权与目标：2026-11-16 用户明确要求独立调查旧版本、撤权、不同范围相同答案及清缓存症状，保持 CACHE-PERM-001，先建立因果关系再做必要修复。写入限本地服务/测试和开发记录；仅合成隔离数据，不改真实资料/权限，不调用外部服务。
- 状态：DONE（本次调查及已复现机制的修复）；原始事故关联与清缓存症状 UNVERIFIED。详细路径、键/复用边界、不变量、竞争解释、七类最终报告见 [调查记录](artifacts/acceptance/CACHE-PERM-002.md)。不勾选对应任务整项，不将 CACHE-PERM-001 当作原问题通过证据。
- 复现与根因：restricted 未禁止知识库继承，导致直接授权撤销后仍读到正文；认证后的 User/会话未在最终输出前刷新，发生在途撤销仍返回；所选证据未在保存前校验，发布/撤权交错后仍写旧答案。仅对象刷新能改变判定的对照支持 ORM 请求内过期机制；未发现网页共享答案缓存，未证实用户的清缓存操作。
- 实质修复：共享资料门禁遵循继承模式并刷新当前授权对象；长查询输出前重验会话；保存前重核候选权限、现行版本、内容核验/型号和活动索引来源。保留 superseded 历史读取与 no-store，不新增缓存或基础设施。
- 周期与证据：保留 CACHE-PERM-001 的第 1 个周期；CACHE-PERM-002 第 1 个完整周期已通过适用的 11 项检查。合并必要旧回归共 23 项、20 通过/3 项既有失败。修复前 11 项为 1 通过/10 失败；候选/治理/Git 哈希、命令和 JUnit 位于 `outputs/cache-permission-002/2026-11-16/`（baseline.json、candidate.json、before-extra.xml、cycle-1.xml）。未从调查次数或启动尝试折算修复周期。
- 独立发现与限制：同键重放 409/回执 processing 的幂等误拒绝已复现但未修；旧原型 session_id 历史/商品名召回缺少网页权限接入、页面失败保留旧显示为源码观察，未作运行时安全声明。原三项服务失败保持。没有浏览器、生产并发、实际撤权管理端点或真实事故材料的完整验收。
- 下一步：本授权范围验收后停止。关联原事件最少需要部署候选、请求/任务标识及所清理的缓存对象；其他独立缺陷按单独任务处理，不追加本次修复周期或扩展原始完成声明。


### DOC-USER-001：普通使用者讨论结果入档（T031 部分）

- 授权与范围：2026-11-17 用户要求继续逐角色讨论并将上述结果写入对应文档；本次只改需求、角色、契约、候选数据/API、计划/任务、验收指南及状态/决策指针，不实施应用、不调用外部服务、不改业务资料或授权。
- 确认边界：持续对话和已确认条件记忆、新工作新对话及历史出处保留、默认授权范围检索并可手动缩小、型号/文件名资料查阅、单条回答分享及接收者权限；整个对话分享后置。布局、原文并排和快捷提问等仍标为建议，异常恢复及视觉待继续确认。
- 验收：旧必选资料库约定被替代；需求编号/候选契约/实施差距一致，新增能力不勾选完成；保留既有缓存修复和失败证据；相关链接、Memory 与文档差异检查通过，应用文件哈希保持基线。
- 状态：DONE（仅本轮已确认讨论结果的文档同步）；普通使用者完整设计/视觉、实现及首期验收继续未完成。
- 周期：本次文档范围第1个完整修订周期已通过合并文档检查；不计入或重置既有应用修复预算。
- 证据：`outputs/ordinary-user-design/2026-11-17/baseline.json` 保存 HEAD、治理、文档及应用基准；最终候选与文档检查结果同目录保存。本轮不重跑应用回归或将历史通过结果换日期。
- 验证结果：文档链接/章节、16项需求与33个任务映射、Memory和Diff检查通过；既有缓存任务记录与应用文件保持基线。新增应用测试0项，未执行应用或浏览器验收；checks.json / candidate.json 保存结果和候选。初检中的2处文件尾空行已清理，词语检查改为识别“一条回答”的等义表述，不修改业务条件来迎合检查。
- 下一步：继续普通使用者的缺项、冲突、服务异常和恢复交互，再进入视觉预览及其他角色讨论。


### DOC-USER-002：资料问题反馈确认入档（T031 部分）

- 授权与范围：2026-11-17 用户确认员工在当前对话/手册页反馈、知识管理员后续处理、反馈不等于业务确认或自动发布，并要求同步文档。只修改相关开发文档；管理角色的完整界面与分派继续待讨论，不实施应用。
- 验收：FR-017 在项目/feature需求一致，候选 UI/API/数据库、T034与验收指南关联；不默认附带整段对话或借受理扩权，不改变资料三态门禁；既有缓存及DOC-USER-001检查点保持，应用文件不变。
- 状态：DONE（仅本轮反馈确认入档）；反馈功能、普通使用者整体设计及应用验收仍未完成。
- 周期：本次增量文档范围第1个完整修订周期通过合并文档检查；初检的等义文字匹配误报已改为人工核对实际候选门禁，不降低业务要求；未执行应用修复，不重置任何既有预算。
- 证据：`outputs/ordinary-user-design/2026-11-17/feedback/baseline.json` 绑定 Git/治理/文档及应用；检查和最终候选同目录保存，不覆盖前次记录。
- 检查结果：相关链接/原锚点、17项需求/34个任务映射、Memory与Diff检查通过，应用文件和历史检查点保持基线；未新增应用测试或执行运行验收。checks.json / candidate.json 保留本轮证据，文档检查不替代功能验收。
- 下一步：确认员工回访反馈进度/答复的方式，再汇总普通使用者完整页面结构；不无边界追加功能。


### DOC-USER-003：反馈回访确认与普通使用者结构稿（T031 部分）

- 授权：2026-11-17 用户确认原对话/手册页查看反馈进度与管理员答复，资料更新提示重新查询、保留旧回答；要求同步文档并整理完整页面结构稿/后端关系继续确认。
- 范围：相关文档与隔离示例结构预览，不修改应用、真实资料、授权或外部服务；信息架构/布局及视觉样本仍为 proposed，不记录为 approved。
- 验收：FR-017与角色/候选API/数据库/任务/验收指南一致；结构稿覆盖登录、对话、资料查阅、原文、单条分享、反馈回访及异常状态，明确前后端关系与差距；文档核对及可取得的预览检查如实记录。
- 状态：DONE（本轮需求入档、结构稿与隔离预览交付）；主要信息架构/布局等待用户确认，应用实现与视觉/业务验收未完成。
- 周期：本交付范围第1个完整修订周期已通过合并文档及隔离预览检查；预览环境探测不计为应用修复周期，不重置既有预算。
- 证据：`outputs/ordinary-user-design/2026-11-17/structure/` 中 baseline、candidate、文档检查与预览结果；保留此前全部记录，新增能力保持未实现/未验收。
- 预览记录：仅合成示例，覆盖对话/目录/原文/单条分享/反馈答复/重新查询及窄容器重排；本地查看了对话、反馈和深色截图。初次视觉观察发现本机 Chromium 120 不支持 light-dark() 导致失色，增加同外观的明暗兼容后合并复验通过；早先受限环境的 Playwright 启动探测已中止，不计应用修复周期。没有独立设计评审、人类布局/视觉批准或应用 A–F 验收。
- 合并检查：15份相关文档、127个本地链接、56个章节链接、17项需求/34个任务映射、Memory及Diff通过；应用文件与历史检查点保持基线。隔离预览有0个页面脚本错误/0次网络请求；截图与检查只覆盖示例，不代表真实系统通过。
- 下一步：请用户确认“两主入口、原位任务/反馈、按需原文”的完整信息结构；未确认前只做隔离设计，不进入全面应用实现。

#### 2026-11-17 用户范围更正（延续 DOC-USER-003）

- 新证据与诊断：用户明确要求答案结果为主、附加证据链接跳转，并指出此前结构只覆盖 RAG，遗漏数值计算与表格修改。SPEC 1.1 早已记录真实计算、已有 Excel 回填/另存、多项目暂停和已有结果确认要求；原因是将首期查询链路误当完整角色模型，不是用户新增了计算需求。
- 更正：整体办公任务/工具/成果关系在既有角色与 SPEC 维护；首期 UI 明确只覆盖查询，取代原稿“完整”定位及并排原文建议；API13.5/数据库13.4集中标注计算、表格、验证及持久化缺口。未新增查询 FR/任务或修改首期实施顺序，不增加通用框架或应用代码。
- 状态：本轮确认结果入档与模型纠偏 DONE（文档范围）；此前 DONE 仅指当时入档/示例交付，不能证明完整角色设计成立。完整页面和视觉候选尚待重整与讨论；历史预览/检查保留其有限范围，不作为新版默认依据。
- 周期：沿用同一范围和第1个完整周期记录，第2个周期已开展范围纠正；完整页面重整及相应验证尚未完成，不提前计为又一个完整修订周期，不重置既有预算。
- 本轮验收：结果优先/证据链接与整体工具链范围在权威文档一致；保留已有公式、单位、表格保护/确认及权限规则，查询子流程仍与完整任务区分；本地链接/锚点、Memory与Diff检查通过，应用/实验及旧任务检查点不变。仅执行文档检查，不重跑应用或历史预览。
- 证据与下一步：增量基线、候选及检查在 `outputs/ordinary-user-design/2026-11-17/scope-correction/`；下一步讨论文件提交、计算/表格成果在对话中的预览与打开、长任务恢复，之后再形成覆盖完整任务的视觉候选。现有单条回答分享不自动扩为文件/整项任务分享。
- 合并检查结果：9份文档的141个本地链接、60个章节链接及17项查询需求映射通过；Memory结构与Diff检查通过，44份应用/实验文件和既有任务检查点保持基线。未执行新应用测试、预览或外部调用；检查仅证明本轮文档关系，不证明计算、Excel处理或完整视觉已交付。

#### 2026-11-17 先设计整体Agent工具体系（延续 DOC-USER-003）

- 用户调整：先设计工具、构筑完整体系，并先列需要完成的Agent工具，不再按单个界面临时添加能力；本轮交付为整体候选目录，不实施应用。
- 实际设计：在既有架构文档第6节集中维护20项核心工具、Agent/工具/执行层边界、管理能力调用范围及三条代表完整链路；角色与API仅引用，不复制工具定义。工具接入共用权限、版本、确认、验证、检查点、幂等和有限恢复要求。
- 状态：本轮候选目录交付 DONE（文档范围）；5份文档的61个本地链接、26个章节链接、20项工具编号与Diff检查通过，44份应用/实验文件及既有任务检查点保持基线。20项不是20个已实现工具或独立服务，签名/依赖/验收矩阵仍待细化。整体页面重整及视觉验证保持未完成，第2个完整周期未结算，不重置前次预算。
- 证据：`outputs/ordinary-user-design/2026-11-17/tool-system/` 绑定本轮前后文件及应用基线；保留全部既有检查点与历史证据，不重跑应用/预览或外部服务。
- 下一步：按同一目录完成工具契约、依赖和验收设计，然后安排实现批次并映射页面。工具设计的完整性与首期查询优先的实施顺序分别维护，不把本轮讨论解释成全部工具可用或开发授权。

### DESIGN-TOOLS-001：20项Agent工具统一设计（跨场景设计，不扩大首期开发任务）

- 授权：用户2026-11-17要求完成20项输入/输出、权限、依赖和验收标准，核实现有/待开发并同步文档，明确先不写代码。范围为SPEC、架构、API、数据库、PLAN、STATUS及本检查点；不实施代码、迁移、安装、资料处理或外部调用。
- 基准：HEAD `0e2d4611dffebd9b24f933a5cf3e8c602bc9f6dd`；工作区已有未提交内容，实际前后hash见`outputs/agent-tool-contracts/2026-11-17/`。保留DOC-USER-003及所有修复/失败证据，本设计交付不清零其UI周期。
- 交付：API14唯一维护公共类型、权限/操作、20项工具契约及AC-T01–20；数据库14集中办公/公式/确认/变更/交付/单条回答ACL；架构6.5维护依赖，PLAN维护五条组合验收，STATUS维护20项源码映射和实际状态。现状7项有应用基础、2项仅实验、11项需新增，20项均未按统一工具契约注册/验收。
- 状态：DONE（仅20项统一设计及文档交付）；独立复核的正式回答权限绕过、分享集合并发修订两处重要问题已修正并经定向复核闭合；不标工具或首期任务完成，不以设计输出授权后续开发。
- 验收：20项五维完整、一一对应；依赖与权限/确认/持久化/恢复无实质冲突；现状有源码或历史证据；本地链接/锚点、需求/任务边界及Diff有效，受保护源代码/实验/原验收记录保持基准。文档检查不代表真实引擎、权限或业务验收。
- 周期：设计交付第1个完整修订周期已完成：源代码/规则核对→集中设计及复核→合并修正权限/确认/集合版本等问题→合并文档核对；没有产品修复或测试重跑，不按命令/评论计轮，既有预算不变。
- 检查结果：7份文档的20项五维契约/目录/依赖/现状映射、138个本地链接、54个章节链接、17项首期需求映射及Diff通过；62份受保护代码/实验/验收文件、既有任务检查点及治理文件保持基准。review.json保留独立文档复核的发现/闭合，document-checks.json与candidate.json绑定最终候选；没有运行工具、迁移或真实业务验收。
- 下一步：设计已可评审；真实公式/输入/单元格映射/精度容差、模板、引擎支持矩阵及外发/留存等按实际任务配置后才能实施/验收。等待用户后续开发或界面方向指令，本轮不进入代码。

#### DOC-USER-003续：基于20项工具重整完整界面与对接

- 授权：2026-11-17 用户明确要求四类工作完整界面/后端、结果优先/证据链接并同步文档，先不写代码。只修改10份相关文档（含新增设计文档），不制作应用或预览代码，不处理真实业务文件、不调用外部服务。
- 交付范围：完整页面/交互集中在`doc/design/普通使用者界面与对接方案.md`；候选对话/资料查阅/任务与成果三入口、U00–U09、四类结果、项目级补充/覆盖确认、partial/历史/恢复和UI-01–08；API15维护页面投影与连接，数据库15维护消息/资源/上传回执/预览/输入章节定位，角色/SPEC/首期契约/索引只引用唯一设计。
- 边界：原两入口及旧查询侧栏示例不作新版默认；导航/高保真仍未批准；表格默认为只读预览、下载可编辑文件和对话续改，在线单元格编辑未确认；只分享一条回答，不扩大资料发布或业务确认。工具目录保持20项，现状7应用基础/2仅实验/11新增未变，首期FR及实现范围不扩大。
- 状态：DONE（仅本轮功能/交互及后端对接方案文档）；10页/子视图、20工具映射及8类页面验收已形成。四点定向文档复核未见重要矛盾：分享隔离、输入结构/原件边界、GET只读与补齐/新任务、内部产物下载隔离。完整高保真、导航批准和真实运行仍未完成，不把文字/Mermaid当视觉验收。
- 周期：延续DOC-USER-003第2个整体周期，补齐完整四类页面关系；尚无完整视觉候选/批准，周期不提前结算，不用新编号重置历史预算。本轮文档增量检查与整体视觉/运行验收分别记录。
- 验收：T01–T20与页面/后端一一覆盖；状态/版本/输入确认/权限和恢复不矛盾；链接/锚点、工具/FR及索引关系通过；受保护源代码/实验/原验收记录、旧检查点及治理保持基线；如实记录尚无新视觉/业务验收。
- 证据：`outputs/ordinary-user-design/2026-11-17/full-workspace/`的baseline/candidate/document-checks/review；不覆盖前轮证据。
- 合并检查：10份文档的180个本地链接、70个章节链接、U00–U09/T01–T20/UI-01–08及17项首期需求映射、Diff检查通过；62份受保护代码/实验/原验收文件、旧检查点和治理未变。未执行新页面、预览、迁移、provider或业务测试；review仅为四点边界一致性，不宣称全量独立设计/视觉评审。
- 下一步：候选方案已交付评审，按用户反馈确认导航/交互及后续完整视觉方向，不据本轮讨论开始代码。

### DESIGN-MEMORY-001：四层产品记忆统一设计

- 授权：用户2026-11-17确认四层，要求完整设计数据、读写/更新、权限/失效、20工具及四类工作对接并同步文档；个人偏好后置，明确先不写代码。范围为11份主题/需求/界面/计划/状态及开发Memory文档，不实施产品代码、迁移、真实资料处理或外部调用。
- 基准：HEAD `0e2d4611dffebd9b24f933a5cf3e8c602bc9f6dd`；未提交工作区保留，前后hash见`outputs/memory-design/2026-11-17/`。本次是记忆设计交付，不重置DOC-USER-003视觉周期或既有缓存/工具修复预算。
- 交付：架构7维护四层/失效/20工具/四链路，数据库16维护事实作用域与快照/依赖/检查点，API16维护MemoryBundle/FactBinding/更新/恢复语义，PLAN定义MEM-01–12；SPEC和员工方案引用，开发Memory仅记录已确认方向和权威链接。
- 状态：DONE（仅四层记忆设计及11份文档同步）；核心三节已做独立只读设计一致性复核，未见已确认重大矛盾；最终文档结构/映射、Memory结构和Diff通过。没有产品运行或真实记忆验收，7应用基础/2仅实验/11新增的工具状态不变。
- 验收：四层各有结构/读写/权限/失效且无重复事实库；20工具与四链路映射完整；当前/冻结/历史及候选/确认/批准分开；来源/撤权/清理/作用域/缓存/恢复一致；链接、Memory结构、Diff通过；受保护代码/实验/旧验收记录、旧检查点与治理保持基准。
- 验证记录：11份文档的207个本地链接、93个章节链接、四层/20工具/四链路/MEM-01–12及17项首期需求映射检查通过；`python -m src.memory.validate`与Diff通过。62份受保护代码/实验/旧验收文件、此前检查点保持基准；开发Memory校验不作为产品运行证据。
- 治理衔接：中断后按用户新全局治理2.1恢复；只补最终核对，未重跑产品测试或新派Agent。STATUS/架构中的旧全局三轮停止条款已替换，历史修订/失败记录保留，不把它们当当前任意轮数上限。
- 证据与下一步：baseline/candidate/document-checks/review及Memory校验在上述输出目录保存；设计已交付供评审，当前不自行开始代码、外发或个人偏好层。

### DESIGN-KNOWLEDGE-001：知识管理员完整界面与后端设计

- 授权与范围：2026-11-17用户要求知识管理员上传、解析核验、版本管理、发布撤回和员工反馈的完整界面/后端设计，明确业务确认/系统授权边界并衔接20工具及四层记忆，明确先不写代码。更新12份主题/职责/需求/契约/计划/状态及开发Memory文档；不新增应用代码/迁移、真实资料处理、外部调用或浏览器原型。
- 基准：HEAD `0e2d4611dffebd9b24f933a5cf3e8c602bc9f6dd`，保留全部既有未提交改动；文档前态在`outputs/knowledge-admin-design/2026-11-17/before/`。源码身份沿用前任务candidate的protected_baseline，本轮最终比对，不重做无关产品审计或测试。
- 交付：新增知识管理员完整方案K00–K10/五流程及20工具/四层映射；API17定义管理读取、确认/核验/预检、范围受限反馈和恢复；数据库17定义记录/版本等级及事务；架构8说明复用和四类工作影响；PLAN定义KM-01–12，其余入口同步。用户同日已确认发布前业务适用确认/内容核验/所需关键专业确认齐全，索引就绪后才能正式查询；三入口和实际协调分派仍待评审，已确认职责不重开。
- 状态：DONE（仅完整文档设计与同步）；五流程、20工具/四层/四类员工链路及KM-01–12已覆盖；发布门槛已确认，其余导航/视觉和实际分派安排仍待确认，无产品运行验收。
- 只读实现差距：直接PATCH确认/核验标记、发布按latest_run、未知生效时间回填、撤回混写适用状态，以及版本等级/历史读取映射均登记在STATUS，未修改代码。T017–T022/T034和既有A–F状态不升级；FR-001–017/T001–T034清单不扩写或勾选。
- 验证与复核：一次有界独立权限/发布设计复核指出确认/核验读取、反馈内部事件/维护关联、文档与版本等级作用域3项缺口，已补入对应契约；修订后复核确认3项文档缺口闭合；12份文档的255个本地链接、111个章节链接及K00–K10/20工具/四层/五流程/KM-01–12映射通过，17项首期FR/34项任务勾选和原检查点保持；62份受保护源码/实验/旧验收文件未变，Memory结构与Git Diff检查通过。用户同日确认发布门槛后新增开发Memory D-009，scope-addendum保存增补前态；运行其结构校验但不冒充产品证据。
- 续跑/终点：本轮文档一致性、修订后有界复核及no-code核对已通过，设计交付转入用户逐页讨论，不自行进入代码或系统管理员完整设计；未发生业务/权限/发布运行验收。证据统一在上述输出目录，不覆盖既有工具/记忆/缓存检查点。


### DESIGN-PI-001：Pi核心基底接入与改造设计

- 授权与范围：2026-11-17用户确定采用Pi核心基底，要求结合整体架构、20工具和四层记忆设计复用/改造及实施顺序，明确不改代码。本轮只修改设计/入口/计划/状态/决策和本检查点；不安装、迁移、运行框架、调用企业外部服务或改业务资料。
- 基准：HEAD `0e2d4611dffebd9b24f933a5cf3e8c602bc9f6dd`；保留既有未提交和其他设计内容。`outputs/pi-integration-design/2026-11-17/baseline.json`及before保存文档前态、治理和67份产品/实验/历史证据基准；不重置既有修复/视觉周期。
- 交付：新增Pi接入方案，架构9/API18/数据库18/PLAN集中接入/桥接/绑定及PI-00–07、PI-AC01–12；20工具编号/契约和四层业务权威保持，新增受控SDK会话与模型出口映射。Pi方向已定，具体接入细节待审阅。
- 状态：DONE（仅具体接入/改造和实施顺序的设计文档交付）；SDK主路线、20工具/四层映射、模型出口与恢复边界已形成并经设计复核，具体方案待用户审阅；20工具、T任务和A–F运行状态不升级，SDK候选没有试运行。
- 关键依据：官方发布候选v1.5.2源码`b4afdddf44e059c67ccf5097e5317d913521b470`的SDK/ModelRuntime/设置/结束边界已核查，证据在sdk-evidence.md；扩展异常不能充当强制外发门禁。具体包/Node组合和制品仍需PI-00验证。
- 本轮验收：20工具/四层/整体角色与治理覆盖；主/辅助模型出口、非终态/确认/恢复/副作用未知和完成门禁一致；唯一维护位置/链接有效；独立builder问题已处理，受保护代码/既有证据不变；不把文档或Memory通过当作业务运行通过。
- 证据与下一步：本轮baseline、SDK核查、review、document-checks/candidate统一在上述outputs目录；完成设计复核后交付用户审阅，未获后续授权不开始PI实施、安装或真实外发。

- 合并复核与检查：独立builder检查提出1项payload授权绑定缺口，已用后端完整投影→锁定provider封闭转换→最终许可/一次消费的两阶段契约修正，定向复核闭合。16份文档的20工具/四层/PI-00–07与PI-AC01–12、链接/章节、Memory与Diff检查通过；67份受保护产品/实验/历史证据未变，未执行产品/浏览器/SDK测试。候选与具体计数以document-checks.json/candidate.json为准。
- 并行文档变更：同一工作区知识管理员设计完成了DESIGN-KNOWLEDGE-001收尾并新增Memory D-009，保留其全部更新；本任务Pi决定使用D-010，避免编号冲突。此前工具/记忆/缓存检查点内容未改，不把另一项设计的结果算作Pi运行证据。
- 周期：本设计第1个完整修订周期已完成（设计/来源核对→独立复核→修正→合并文档验证），既有应用修复和视觉周期/预算保持；本轮终点为设计交付，不启动PI-00实施。


#### Pi Agent 核查整改实施任务

2026-11-18用户批准PI-R01–06文档整改，并确认首期增加**有证据约束的组织答案**。本清单细化原T任务，不重建已实现SDK/Worker/8工具/许可账本，不新增顶层任务或业务源码。本轮不执行以下任务；未来实施/迁移/安装/真实配置或外发仍需对应授权。完整阶段在[项目PLAN](../../doc/planning/PLAN.md#pi接入实施顺序与验收)，技术落点在[feature Pi Plan](plan.md#pi-agent-剩余实施-plan2026-11-18六项核查整改待审核)。

规则沿PDF子任务：代码、验证、真实准入和文档任务分开；代码交付标IMPLEMENTED，受影响验证通过才VERIFIED，不能用模型声称或旧合成报告勾选。每项先核对当前候选/治理/输入修订，保留真实失败/原回执；缺外部事实只阻断消费它的任务。凡提到API18/DB18.1均为本轮目标契约，不表示端点/字段已实现。未提及的12项员工工具及PI-04–06仍不启用。

##### 制品与正式模型出口（PI-00/01剩余）

- [X] **T024-PI-01｜运行制品、profile与桥接契约补齐**（工程，父T024/T008；PI-R01/04）
  - 目标/依据：复用已运行SDK/Worker，固定实际制品、工具/资源/settings和bridge版本及能力；Pi方案3/5、API18.1/18.2、PI-AC01/03/05/07。
  - 直接依赖：T008。
  - 运行边界：既有T008任务/双租约/回执及SDK1.5.2构建基础；明确本地实施授权，不重装或重建框架。
  - 模块/交付：扩`worker.runtime_artifacts`、`runtime_models.py`、`runtime_gate.py`、`runtime-host.ts/pi00-session.ts`和必要安全迁移；校验实际运行制品/受控adapter与锁文件、Node/bridge/settings/resource/tool能力匹配。现有JSON-lines子集不冒充目标JSON-RPC2.0全方法，版本/握手、abort及安全事件按API18的本次启用能力实现并显式拒未知；不新建`src/server/agent/`。
  - 验收/验证：配置不可原位改写，制品/工具/schema变化要求新profile；只读GET不执行；无环境默认工具/扩展/凭据继承，等待/取消后无继续动作；运行验证由T029-PI-01负责。
  - 停止：代码候选交付到IMPLEMENTED；兼容性不明安全拒启动，不把缺方法或仅版本字符串匹配写成完整PI-00通过。

- [X] **T024-PI-02｜正式生成服务的受控出口能力**（工程，父T024；PI-R04）
  - 目标/依据：在保持私有网关/逐用途许可的前提下补合成到正式profile的技术路径；Pi方案5、API18.3/19.4、FR-012、PI-AC02/08。
  - 直接依赖：T024-PI-01。
  - 运行边界：T024-PI-01；既有ServiceUsePin/调用账本/预算、服务选择与凭据/TLS机制。目标协议/能力边界明确；生产provider未选时只交兼容候选，不创建真实配置。
  - 模块/交付：扩`runtime_gate.py/runtime_api.py`、`service_use.py/service_admission.py`和`controlled-provider.ts`的固定adapter/profile转换。正式profile取服务端批准的model/API family、上下文/输出限制、能力与tokenizer，完整绑定最终请求和一次许可；凭据仅后端，允许出口/重定向/TLS/用量未知/超时/未知派发边界明确。
  - 验收/验证：不能只删synthetic断言或改baseURL；不支持协议/图能力明确拒绝；主/辅助用途独立许可、失效停用不回退、未知不重复付费；OS/网络出口限制有可配置可验证落点。T029-PI-01取技术证据。
  - 停止：只交代码，不取得企业外发资格；正式路径门禁不完整仍禁用，不把回环合成上游当真实模型。

- [X] **T029-PI-01｜制品、桥接与出口定向验证**（验证，父T029；PI-R01/04）
  - 目标/依据：证明新增profile/协议/用途边界成立，PI-AC01/02/03/05/07/08，SC-003/004。
  - 直接依赖：T024-PI-01、T024-PI-02。
  - 运行边界：T024-PI-01/02代码交付；隔离DB/实际SDK构建与回环上游的运行授权。
  - 模块/交付：复用`src/agent_runtime/test/pi00.test.mjs`、`test_runtime_gate.py/test_worker.py/test_pi_service_use.py/test_local_queue.py`的相关节点，仅补新增制品/握手/真实adapter形状断言与报告，不修业务代码。
  - 验收/验证：不兼容制品/schema拒启；重复派发/取消/租约失效不能提交或再次外发；主/辅助许可/预算和最终payload一致，未知调用原回执恢复；不把节点全部重跑当默认要求。
  - 停止：目标断言通过后给后续技术任务使用；失败返回对应工程项。结果只为工程证据，不解除真实模型/资料许可。

- [ ] **T024-PI-03｜正式模型逐用途准入及真实协议验证**（真实配置/验证，父T024；EXTERNAL_PENDING；PI-R04）
  - 目标/依据：验证实际供应商/模型、许可和网络，而非只补账号；API18.3/19.4、FR-012、PI-AC02/12。
  - 直接依赖：T029-PI-01。
  - 运行边界：T029-PI-01；实际批准的provider/model/数据范围、凭据/费用配额/留存/TLS与允许出口及责任人。资料范围仅限本次明确获准输入，summary/compaction分别登记。
  - 模块/交付：复用现有服务配置/预检/选用及私有出口，不写业务代码；记录不可变服务修订、真实adapter/tool-call/响应终态、上下文/输出上限、实际用量或未知值、安全停用和原请求身份。
  - 验收/验证：用最小获准输入真实调用并确认工具协议/返回可解析、预算限制、未知结果恢复及当前权限校验；不故意制造重复付费，不将成功主回答当辅助用途许可；缺图能力如实记录。真实企业正文须先有相应全部门禁，不能用准入测试绕开。
  - 停止：逐用途证据与范围成立才可在该范围供联合验收使用；账号/网络/许可不齐保持EXTERNAL_PENDING，不自动换模型、禁TLS或上云发资料。

##### PDF来源与首期组织答案（PI-02/03）

- [X] **T012-PI-01｜child／region证据到Pi模型投影的受检消费**（工程，父T012/T013；PI-R02）
  - 目标/依据：把PDF已受检支持组完整带入工具和模型出口；SPEC10.5/10.6、API18.5/24、DB22.5/7.2、PI-AC02/04/08/09。
  - 直接依赖：T012-PDF-01、T021-PDF-02、T024-PI-01。
  - 运行边界：`T012-PDF-01`的Chunk/支持组/Evidence接口和`T021-PDF-02`完整代际启用接口已交付；T024-PI-01。不要求PDF P6最终验收才能写适配，但跨模块验证须等T029-PDF-05。
  - 模块/交付：改`runtime_gate._assemble/_current_projection`、`execution._current_evidence/model_tool_results`、`runtime_api.py`及必要schema。消费精确Evidence文本区间/region和有源关系，保留body/context/condition/warning/header/definition角色与顺序；依赖绑定Run/IR/Chunk/关系/QC/审核/发布/代际/profile，不重新拼整Block或重做切分。
  - 验收/验证：ready_partial/过期/缺成员拒绝；进入模型未最终引用的来源也登记；完整支持组超预算减少候选或拒答，不漏条件/单位/警告；无获准图能力不推断原图；旧Block路径与新child路径显式分开。T029-PI-02验证。
  - 停止：代码交验，坏来源/未知资格不进正式L1；新接口未具备只做隔离契约准备，不伪造Chunk/模型输入。

- [X] **T013-PI-01｜有证据约束的组织答案与逐声明核验**（工程，父T013；PI-R06，用户已确认首期范围）
  - 目标/依据：从“拼接摘录”扩展为模型组织候选、逐声明核验后正式交付；SPEC“首期组织答案口径”、API18.5、FR-007/008、PI-AC09/12。
  - 直接依赖：T012-PI-01。
  - 运行边界：T012-PI-01；既有T14/T16/QueryAnswerSnapshot及当前资格检查；组织答案口径已由用户确认，不再待选。代码/真实生成调用仍需各自授权与准入。
  - 模块/交付：扩`execution.py`、`runtime_gate.py`、`execution_models.py`、`schemas.py`和查询结果投影，保留现有摘录供原文查看。建立独立AnswerCandidate/声明与支持组/校验报告引用，模型候选不能直接写formal；T14核对claim→Evidence/原区/关键结构及完整支持，T16只冻结通过的组织答案。
  - 验收/验证：首版严格消费API18.5的answer-plan/1：固定标题码、完整fact_group/procedure_group，服务器renderer取正文，禁止组内步骤重排及自由文字formal；没有新增答案人工签批链。每条实质声明有确切支持组，不混型号/版本/条件；数字单位/公式/步骤顺序/警告从已核验结构或有源原句受控呈现，不补值。引用存在、字面相似或另一模型自评不足以证明改写语义；无法确认的声明待核验/拒答或送人工，不把删掉问题声明后的片段标完整成功。原PDF/IR/Chunk不改，业务复核独立。T029-PI-02做技术验证，T029-PI-05做真实对照。
  - 停止：组织/支持核验代码交付；候选、partial和人工待核验均不标完整正式答案，旧摘录不作为新目标已完成或静默生成失败回退。

- [X] **T025-PI-01｜精确来源绑定与旧会话安全恢复**（工程，父T025/T008；PI-R05）
  - 目标/依据：修复仅Version级恢复粒度不足的问题；Pi方案5/7/8、DB18.1、API18.5、PI-AC04/07/08/11。
  - 直接依赖：T012-PI-01、T024-PI-01。
  - 运行边界：T012-PI-01、T024-PI-01；既有RuntimeJournal/lease/历史快照基础。
  - 模块/交付：扩`worker._save_journal/_restore_journal`、`runtime_models.py/execution_models.py`与必要迁移；存整份受检依赖manifest/来源指纹及runtime兼容身份，完整覆盖实际投影/工具结果/摘要依赖，不以最终引用或version_ids替代。
  - 验收/验证：同Version新Run/Chunk/审核或代际变化重新判断旧分支；来源不兼容隔离JSONL并由有效L2/L3重建，不删除旧证据或重放工具；迁移旧日志缺绑定时保守隔离，不补造等价性。T029-PI-02/04验证。
  - 停止：代码交验；无法证明可用不恢复旧分支，不因重新授权/换session ID复活旧许可、预算或已完成动作。

- [ ] **T029-PI-02｜PDF→Pi、声明支持与同版恢复验证**（验证，父T029；PI-R02/05/06）
  - 目标/依据：验证跨模块消费和错误放行风险；SPEC10.6/10.7、PI-AC02/04/08/09、SC-001/002/004。
  - 直接依赖：T012-PI-01、T013-PI-01、T025-PI-01、T029-PDF-05。
  - 运行边界：T012-PI-01、T013-PI-01、T025-PI-01；T029-PDF-05对应技术能力通过；固定PDF支持组/组织答案人工参考用于真实语义判断，合成仅验证技术门禁。
  - 模块/交付：复用`test_runtime_gate.py/test_execution.py/test_worker.py/test_pi_service_use.py`，补child区间/region、关系/QC指纹、声明支持及旧日志测试；不重跑MinerU或重复PDF索引测试。
  - 验收/验证：缺单位/表头/条件/警告、错关系/跨版本证据、ready_partial、删漏支持组/预算截断不能通过；每个实际发送来源可追溯；同版重绑使旧分支按规则隔离，旧回答不换引用；模型自由新增数值/动作或伪造支持被拒/待核验，引用有ID却不支持结论不能算通过。
  - 停止：技术断言通过交后续；未有人工作证的语义结果不称业务验收，失败回对应工程项，不无限扩Mock或负例。

##### 独立摘要／压缩（PI-02剩余）

- [X] **T032-PI-01｜真实业务宿主的受控辅助用途**（工程，父T032/T024；PI-R03）
  - 目标/依据：让SDK压缩/摘要真正接入业务投影而非仅探针；Pi方案5/7、API18.5、PI-AC02/04/05/08。
  - 直接依赖：T024-PI-01、T024-PI-02、T012-PI-01、T025-PI-01。
  - 运行边界：T024-PI-01/02、T012-PI-01、T025-PI-01；用途/配置与上下文预算契约已明确，自动压缩继续关闭至对应验证通过。
  - 模块/交付：扩`runtime-host.ts/controlled-provider.ts/pi00-session.ts`、`worker.py/runtime_gate.py/runtime_models.py`；安全空闲点显式operation、后端专用输入/指令、SDK压缩调用及返回产物回收。受控provider仍不接收任意SDK旧上下文；保存来源/覆盖、规则/模型/profile身份和当前有效性，独立登记摘要候选，L2 confirmed不被写入。
  - 验收/验证：不是把answer换purpose标签；实际辅助输入/输出路径可观察，summary/compaction分别许可/预算，关键条件/单位/警告/支持组不丢失；失败保留原会话并明确限制，失效产物不进后续L1。T029-PI-03验证。
  - 停止：代码交验；未验证前保持关闭，不把PI-00固定响应或私有端点可用当业务压缩完成。

- [ ] **T029-PI-03｜辅助用途完整业务路径验证**（验证，父T029；PI-R03）
  - 目标/依据：验证真正宿主调用及摘要依赖/关键条件保全，PI-AC02/04/05/08；不是重复探针协议测试。
  - 直接依赖：T032-PI-01、T029-PI-01、T029-PI-02。
  - 运行边界：T032-PI-01、T029-PI-01/02；隔离运行授权。真实provider辅助用途结果还须T024-PI-03该用途准入。
  - 模块/交付：在现有SDK/worker/gate测试中补最小显式summary/compaction业务回路和后续answer观察，记录实际operation/输入选择/派生产物/调用账本。
  - 验收/验证：确认请求确属辅助任务且产物真正被受检L1使用；多来源含未引用来源也受撤权，条件/否定/单位/警告/未完成状态保全，旧分支/候选语义不确定不复用，取消/用途失效无后续调用。压缩内容正确性用人工或确定性关键项对照，模型自评不放行。
  - 停止：通过对应范围才可启该用途；仅合成通过标VERIFIED_LOCAL_SYNTHETIC，真实辅助能力仍待准入/人工样本，不自动启自动压缩或重试。

##### 生命周期、联合验收与移交

- [ ] **T010-PI-01｜私有正文、日志与升级恢复治理**（工程，父T010/T026；PI-R05）
  - 目标/依据：让内部派生数据受真实留存/清理和升级边界治理；DB18.1/19.4、Pi方案7/8/13、FR-011、PI-AC08/11。
  - 直接依赖：T025-PI-01、T032-PI-01。
  - 运行边界：合成副本治理可按获准隔离试验规则实现/验证，不等待真实保留期。真实日志入场与清理仍复用T010-PDF-01已确定的试点窗口/存储责任，并由有权者在既有operations记录明确覆盖Pi工作日志/快照/摘要/模型响应/恢复副本。未确认策略不得实施真实清理；全量备份恢复能力复用T026相关范围，不另造平台。
  - 模块/交付：扩`operations.py`、`worker.py`、`runtime_models.py/execution_models.py`及现有生命周期/恢复集成入口；为working JSONL、FileObject快照、辅助产物、`ModelCallAdmission.response_body`登记用途/引用/状态及受控清理关联。升级使用旧profile固定或显式迁移，先阻断失效读取/恢复，再按获准策略处理正文；恢复先追平撤权/删除/停用。
  - 验收/验证：可列全私有正文副本及依赖，不只删网页消息；无主/中断文件有私有对账，清理失败不重新开放；依法需留证时保留受控依据，原PDF/ParseRun证据不被Pi清理级联删除。T029-PI-04验证。
  - 停止：治理代码候选交验，缺真实窗口或恢复能力保持该活动待定；不新增公开raw日志下载或自动purge，不把隔离与物理删除混为一谈。

- [ ] **T029-PI-04｜升级、清理与恢复定向验证**（验证，父T029/T026；PI-R05）
  - 目标/依据：证明恢复不复活来源/许可/副作用，PI-AC07/08/11、SC-003/004/007。
  - 直接依赖：T010-PI-01、T025-PI-01、T032-PI-01。
  - 运行边界：T010-PI-01、T025-PI-01、T032-PI-01；隔离一致DB＋私有文件备份/恢复能力及明确试验授权/保留规则。真实运维演练另需真实责任与T026门禁。
  - 模块/交付：复用runtime/worker/生命周期检查及既有恢复设施，记录升级旧任务/日志、调用回执与私有正文处置结果，不建新测试框架或重做全平台备份功能。
  - 验收/验证：SDK/profile/来源变化后兼容性明确；损坏或缺依赖日志隔离；提交中断后原call核对无重复付费/交付；清理覆盖工作日志/快照/响应/摘要；恢复后的撤权/删除/停用与unknown预算不回退，原始PDF与历史引用保留其合法边界。
  - 停止：适用断言通过后供试点，局部缺能力只阻断恢复/清理验证，不声称全生命周期通过；失败返回原工程项。

- [ ] **T029-PI-05｜首期组织答案与真实Pi联合验收**（真实验收，父T029；EXTERNAL_PENDING；PI-R01–06）
  - 目标/依据：证明实际Pi、PDF、组织答案与员工查询闭环；PI-AC09/12、FR-007/013/014、SC-001–007。
  - 直接依赖：T029-PI-01、T029-PI-02、T029-PI-03、T029-PI-04、T024-PI-03、T029-PDF-06、T029-PDF-07。
  - 运行边界：T029-PI-01/02/03/04及T024-PI-03所用真实用途通过；T029-PDF-06的G1实际运行及人工参考、适用/职责/访问依据。设备整体声明另消费T029-PDF-07真实三角色A–F，G1不替代它。
  - 模块/交付：扩用现有quickstart/验收入口，**复用PDF真实运行包和同一组问题，不重复整套执行**；补实际SDK/模型修订、组织答案候选/每条claim支持组/T14报告/正式快照与当前用途的证据链。
  - 验收/验证：组织答案每条事实逐原页人工核对，关键数值/单位/步骤/条件/警告完整、无错关系和无依据解释；模型声称完成不能跳T14/T16；缺证据/版本冲突/失权/模型异常/待核验明确可见，非摘录能力冒充。实际检索/引用/可答覆盖、上下文/资源/费用未知及人工成本复用T030-PDF-01方法，不编阈值/100%承诺。
  - 停止：只对真实通过范围关闭；组织答案未通过可展示有标记原文参考但不得静默以摘录宣称目标已完成，模拟批准/账号/模型不算真实验收；缺前置保持EXTERNAL_PENDING，不启PI-04–06。

- [ ] **T031-PI-01｜Pi实施证据与既有文档收敛**（文档，父T031；PI-R01）
  - 目标/依据：把已实现/已验证/待定准确映射回现有权威文档和12项AC；PI-AC01–12、FR-014、项目宪章。
  - 直接依赖：T029-PI-05。
  - 运行边界：T029-PI-05的实际结论及相应T030指标，PI-04–06未实施仍明确DEFERRED；可先记录独立阶段结果，不靠本项补运行证据。
  - 模块/交付：原地更新Pi方案、SPEC/Plan/tasks/STATUS、API/数据库实现映射、现有验收与指标入口，不增会话报告、Memory或第二套任务索引。
  - 验收/验证：逐AC引用当前候选/范围/输入/环境及结果，局部合成与真实签认分开；父T未覆盖部分和后续工具不关闭；链接/依赖及实际源码映射审查。本项不改代码、不重跑业务测试。
  - 停止：适用Pi阶段文档与证据一致后移交审核，不宣称20工具/完整办公平台或真实部署全部完成，不自动转入后续实现。

##### PI-AC与任务覆盖责任

下表是静态覆盖映射，不代表通过；直接依赖只写在上述各项“前置”。PI-04–06的通用计算/Excel/报告和多工作项完整能力继续按原阶段设计，需另行细化后才能开发，不能用本次查询子集关闭它们。

| 验收条款 | 本次责任任务 | 当前证据边界／剩余范围 |
| --- | --- | --- |
| PI-AC01 | T024-PI-01、T029-PI-01、T031-PI-01 | 已有固定SDK/探针；实际profile/制品兼容与新来源升级待验 |
| PI-AC02 | T024-PI-02/03、T012-PI-01、T032-PI-01、T029-PI-01/02/03 | 现有许可/调用账本及合成主路径可复用；真实出口/辅助链路不推定通过 |
| PI-AC03 | T024-PI-01、T029-PI-01 | 8项查询子集映射；其余12项按原PI批次另验，不宣称20全可执行 |
| PI-AC04 | T012-PI-01、T025-PI-01、T032-PI-01、T029-PI-02/03 | 查询L1/L2/L3/L4及来源；全部MEM/办公工作项仍待后续 |
| PI-AC05 | T024-PI-01、T032-PI-01、T029-PI-01/03 | 顺序调用/等待已有基础；新增辅助回调/停止边界待验 |
| PI-AC06 | T029-PI-01、T029-PI-05 | 复用缺型号真实响应恢复；多工作项/表格计划确认不在本次关闭 |
| PI-AC07 | T025-PI-01、T010-PI-01、T029-PI-01/04 | 当前回执/lease可复用，升级/日志/私有产物中断窗口待验 |
| PI-AC08 | T012-PI-01、T025-PI-01、T010-PI-01、T029-PI-02/03/04/05 | 当前权限基础不降低；新child/region/摘要/恢复全部受检 |
| PI-AC09 | T013-PI-01、T029-PI-02/05 | 摘录T14/T16已存在；组织答案逐声明支持与完整交付待实现验收 |
| PI-AC10 | T029-PI-05、T031-PI-01 | 复用HTTP单回答分享/反馈权限与三角色证据；SDK T19/T20未启用，不假验收 |
| PI-AC11 | T024-PI-01、T025-PI-01、T010-PI-01、T029-PI-04 | 版本/来源/私有正文及恢复重验，真实保留/恢复政策待落实 |
| PI-AC12 | T024-PI-03、T029-PI-05、T031-PI-01 | 真实服务/组织答案/查询范围；公式计算、Excel、报告不是本次已验能力 |

#### PLAN-PI-001：六项核查整改文档检查点（2026-11-18）

- 授权：用户同意PI-R01–06补入既有Plan/tasks，随后明确首期增加有证据约束组织答案。本轮只修改现有文档，不开发、DDL、安装、改实际服务或运行模型/业务测试。
- 交付：14项父T子任务与12项PI-AC映射，区分已有8工具/SDK/Worker/账本、确需改造与未来工具；同步PDF→Pi、正式出口、辅助用途、日志来源/生命周期及组织答案契约。来源、依赖、验证与停止条件各自明确，原34任务/23项PDF子任务身份和勾选保持。
- 当前前置：生成供应商/模型与逐用途真实准入、实际样本/业务职责/人工参考、内部日志与响应留存/恢复窗口待落实；PDF来源工程按对应接口与P5验证前置衔接，后续计算/表格/报告仍不启用。组织答案口径已确认，不再列待用户选择。
- 验证：DONE / VERIFIED_DOCS（仅本轮六项规划整改）。9份既有文档、14项Pi任务字段及12项PI-AC覆盖责任已核对，PDF/Pi联合依赖无环，327条本地链接/锚点有效；原34任务与23项PDF子任务身份/勾选及171个源码/依赖/治理/原件/默认SQLite保护对象未变，PDF SPEC10/数据库22/API24原正文保持。精确前态/候选与检查保存在`outputs/pi-plan-revision/2026-11-18/`；未运行模型/产品测试/DDL，14项实施任务均未勾选，不表示问题已在代码修复。
- 停止：交付后等待文档/计划审核，不从本次同意或过去本地代码授权自动转入本清单实施。

### DESIGN-SYSTEM-001：系统管理员完整界面与后端设计

- 授权与范围：2026-11-17用户要求账号与角色、资料授权、模型解析配置、任务监控、审计和备份恢复完整界面/后端设计，明确业务/知识/系统职责并衔接20工具与四层；明确先不写代码。范围为14份主题/角色/需求/计划/状态/原生UI及开发Memory文档，不新增产品代码/迁移、安装SDK、处理真实资料、外发、变更真实授权或执行恢复。
- 基准：HEAD `0e2d4611dffebd9b24f933a5cf3e8c602bc9f6dd`，适用用户全局治理2.1与当前AGENTS；保留Pi/知识管理员及所有既有未提交工作。`outputs/system-admin-design/2026-11-17/baseline.json`及before保存13份文档前态和67份受保护产品/实验/历史证据；用户确认恢复流程后scope-addendum另保存开发Memory前态，不改写原基准。
- 交付：新增S00–S12/六流程及20工具/四层/四员工链路/Pi衔接；API19管理投影/审批登记/预检/服务秘密/控制/备份恢复契约，数据库19统一逻辑记录、事务及恢复账本，架构10和模型/Pi方案引用，PLAN定义SA-01–14；其余入口同步，首期FR-001–017/T001–T034不扩写或勾选。
- 用户确认：同日明确采用先隔离验证完整性、追平当前撤权/删除/停用、验证通过且获准人员确认后切生产的设计流程，记录D-011；六入口、细分职责、实际供应商/外发/费用、RPO/RTO/保留期/密钥托管和窗口仍待审阅或企业落实，不推断真实恢复授权。
- 状态：DONE（仅完整设计与14份文档同步）；S00–S12、六流程、20工具/四层/四链路/Pi及SA-01–14已覆盖，恢复流程已获确认。导航/实际企业参数仍待落实，无产品运行或视觉验收。
- 独立复核：有界builder/授权恢复只读复核提出旧恢复点外副作用/去重/预算账本保全缺口；UI3.6/API19.6/数据库19.5和SA-13已补执行前落账、稳定调用核对、预算保留、未知动作/目标fence及范围不明禁执行，定向复核闭合。未验证实际漏洞或执行恢复，复核不能替代运行验收。
- 实现差距：角色任命依据、管理员approve与外部真实审批分离、审计白名单、读写运维能力、真实配置/逐服务准入/备份恢复均登记STATUS；本轮不修应用。现有人工provider/drill记录不当作真实费用/恢复证据，历史3项失败及T023–T027/A–F/PI状态不升级。
- 验证与终点：14份文档的304个本地链接、133个章节链接及S00–S12/20工具/四层/六流程/四链路/SA-01–14检查通过；原17项FR、34项任务勾选、旧检查点和API/数据库17–18核心章节保持，67份受保护产品/实验/历史证据未变；Memory结构与Diff通过。初始章节保全检查复用旧偏移造成误报，已按各候选实际标题定位校正，仅重验该项通过，无产品/文档修复。证据在上述outputs目录；仅设计交付，继续跨角色审阅/企业参数落实，不自行开始编码、上线、权限变更或生产恢复。

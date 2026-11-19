# 首期需求—任务—验收—证据映射

本文件由 `python -m src.tools.project_docs --write-traceability` 生成；唯一映射源为[traceability.json](traceability.json)。请修改JSON后重生成。任务状态与直接依赖只在[tasks](tasks.md#dependencies)，入场规则在[baseline](baseline.md)，项目进度在[STATUS](../../doc/planning/STATUS.md)。

EV关联包含静态记录、局部运行及明确缺口，不表示需求或整任务通过。下列证据摘要是带日期的历史索引；原运行报告和当前源码身份必须另行核对。忽略目录缺失时只能判证据未取得，不能借本表补成通过。

## 功能需求映射

| 需求与契约依据 | 任务 / 成功标准 | 验收场景 | 证据关联 | 关键断言 | 剩余证据或能力 |
| --- | --- | --- | --- | --- | --- |
| FR-001 89份清单、来源、身份与访问范围；[API5](../../doc/api/接口设计.md#5-知识库上传与版本管理)、[API17.1](../../doc/api/接口设计.md#171-权限与管理读取投影)、[API20](../../doc/api/接口设计.md#20-本地创建与资料生命周期实现映射2026-11-18)、[DB5](../../doc/database/数据库设计.md#5-文件与文档版本)、[DB17.1](../../doc/database/数据库设计.md#171-版本身份状态与来源)、[DB20](../../doc/database/数据库设计.md#20-本地创建墓碑与恢复的事务映射2026-11-18) | T001、T002、T017、T018、T028、T031 / SC-001、SC-002、SC-006 | [A](quickstart.md#场景-a正常查询)、[B](quickstart.md#场景-b资料或型号不足)、[C](quickstart.md#场景-c版本冲突)、[LOCAL-CREATE](../../doc/development/资料创建与生命周期验证.md) | [EV-01](#ev-01)、[EV-02](#ev-02)、[EV-06](#ev-06) | 文件身份、来源和版本可回溯；同hash不自动合并来源或权限；正式查询仅使用满足当前资格的资料子集 | 89份实际来源/版本/访问依据及首批适用确认；候选格式和3原件身份不能替代业务签认 |
| FR-002 平台账号、会话及停用恢复；[API4](../../doc/api/接口设计.md#4-当前用户用户管理与授权接口)、[API19.2](../../doc/api/接口设计.md#192-账号角色与真实授权审批登记)、[DB4](../../doc/database/数据库设计.md#4-用户角色与资料权限)、[DB19.1](../../doc/database/数据库设计.md#191-任命审批与有效授权) | T004、T005、T006、T023、T027、T031 / SC-003、SC-005、SC-007 | [D](quickstart.md#场景-d权限和分享)、[F](quickstart.md#场景-f服务异常)、[LOCAL-QUERY](../../doc/development/本地查询执行与验证.md) | [EV-03](#ev-03)、[EV-04](#ev-04)、[EV-09](#ev-09) | 身份来自服务端会话；停用/登出/过期阻断相应访问；初始化、重置与角色操作保留依据和会话失效记录 | 真实任命、账号和角色依据；完整账号/会话/角色期限与管理范围矩阵 |
| FR-003 完整授权元组与不跨记录拼接；[API3](../../doc/api/接口设计.md#3-权限缩写与公共数据结构)、[API4](../../doc/api/接口设计.md#4-当前用户用户管理与授权接口)、[API17.1](../../doc/api/接口设计.md#171-权限与管理读取投影)、[API19.3](../../doc/api/接口设计.md#193-授权与规则变更)、[API22](../../doc/api/接口设计.md#22-文档维护提交时授权围栏2026-11-18)、[DB4.2](../../doc/database/数据库设计.md#42-权限的计算方式)、[DB19.1](../../doc/database/数据库设计.md#191-任命审批与有效授权) | T004、T007、T014、T023、T025、T031 / SC-002、SC-004 | [D](quickstart.md#场景-d权限和分享)、[LOCAL-CREATE](../../doc/development/资料创建与生命周期验证.md)、[LOCAL-QUERY](../../doc/development/本地查询执行与验证.md) | [EV-03](#ev-03)、[EV-04](#ev-04)、[EV-07](#ev-07)、[EV-10](#ev-10) | 单条当前有效授权及其真实批准依据覆盖完整请求元组；撤销/停用与提交竞争按事务先后判定；分享不补资料权限 | 真实企业权限/任命及完整管理矩阵；尚未实现的真实grant撤回HTTP不得用直接测试行更新替代验收 |
| FR-004 适用、核验、发布三态与选用；[API5](../../doc/api/接口设计.md#5-知识库上传与版本管理)、[API17.2](../../doc/api/接口设计.md#172-确认依据专业核验与保护性暂停)、[API17.3](../../doc/api/接口设计.md#173-发布预检执行和索引衔接)、[API17.6](../../doc/api/接口设计.md#176-待确认原文参考开放2026-11-17用户确认)、[DB5](../../doc/database/数据库设计.md#5-文件与文档版本)、[DB17.2](../../doc/database/数据库设计.md#172-确认核验及保护性暂停)、[DB17.3](../../doc/database/数据库设计.md#173-发布撤回及失效事务)、[DB17.6](../../doc/database/数据库设计.md#176-草稿原件参考可见性) | T018、T020、T021、T022、T031 / SC-001、SC-002、SC-004 | [A](quickstart.md#场景-a正常查询)、[B](quickstart.md#场景-b资料或型号不足)、[C](quickstart.md#场景-c版本冲突)、[E](quickstart.md#场景-e解析质量) | [EV-05](#ev-05)、[EV-07](#ev-07) | 正式资格由当前适用确认、核验集、精确解析/切分、发布及活动索引共同决定；参考开放不改变正式资格；未知生效日保持未知 | 真实适用/专业确认和冲突裁决；在真实样本上核对完整状态组合 |
| FR-005 上传、版本幂等与中断恢复；[API2.2](../../doc/api/接口设计.md#22-幂等与并发更新)、[API5](../../doc/api/接口设计.md#5-知识库上传与版本管理)、[API20](../../doc/api/接口设计.md#20-本地创建与资料生命周期实现映射2026-11-18)、[API21](../../doc/api/接口设计.md#21-本地事务护栏增量2026-11-18)、[DB5](../../doc/database/数据库设计.md#5-文件与文档版本)、[DB8.4](../../doc/database/数据库设计.md#84-接口设计增补http-请求回执)、[DB20](../../doc/database/数据库设计.md#20-本地创建墓碑与恢复的事务映射2026-11-18) | T004、T008、T010、T017、T018、T031 / SC-003、SC-004 | [F](quickstart.md#场景-f服务异常)、[LOCAL-CREATE](../../doc/development/资料创建与生命周期验证.md)、[LOCAL-MIGRATION](../../doc/development/安全迁移与事故核查.md) | [EV-06](#ev-06)、[EV-07](#ev-07)、[EV-13](#ev-13) | 文件/来源校验、业务、回执和审计在对应提交边界一致；中断后同键核对不重复可见版本，拒绝后不能暗中留业务写入 | 临时文件保留/最终清理策略及完整历史迁移边界；获准真实上传样本和规模验证 |
| FR-006 复杂PDF结构、定位与不可信内容；[API6](../../doc/api/接口设计.md#6-解析校验与切分选择)、[API17.2](../../doc/api/接口设计.md#172-确认依据专业核验与保护性暂停)、[API19.4](../../doc/api/接口设计.md#194-服务凭据测试与激活)、[DB6](../../doc/database/数据库设计.md#6-解析结果与检索片段)、[DB17.2](../../doc/database/数据库设计.md#172-确认核验及保护性暂停) | T019、T020、T028、T031 / SC-001、SC-002、SC-006 | [E](quickstart.md#场景-e解析质量)、[LOCAL-SERVICE](../../doc/development/服务适配与本地准入.md) | [EV-05](#ev-05)、[EV-14](#ev-14)、[EV-17](#ev-17) | 解析保留实际页/章节、关键警告、图注及表格脚注，歧义待核验；文档内容不成为系统指令；畸形ZIP/未知输出格式拒绝 | 实际供应商准入/格式兼容及获准文字、图文、扫描PDF的原页人工核验；原ZIP留存/清理与所有崩溃窗口；合成业务接线不证明真实解析质量 |
| FR-007 当前授权范围、型号、版本和索引检索；[API7](../../doc/api/接口设计.md#7-索引构建与启用)、[API8](../../doc/api/接口设计.md#8-知识检索与问答任务)、[API13.1](../../doc/api/接口设计.md#131-操作与端点映射)、[API18](../../doc/api/接口设计.md#18-pi宿主与执行桥接契约)、[API18.5](../../doc/api/接口设计.md#185-pi核查整改的输入候选与正式出口契约)、[DB7](../../doc/database/数据库设计.md#7-向量配置索引代际与物理索引)、[DB8](../../doc/database/数据库设计.md#8-任务重试证据与产物)、[DB13](../../doc/database/数据库设计.md#13-对话消息与条件快照候选设计)、[DB18](../../doc/database/数据库设计.md#18-pi会话与运行绑定设计) | T011、T012、T013、T019、T021、T031、T032、T033 / SC-001、SC-002、SC-004 | [A](quickstart.md#场景-a正常查询)、[B](quickstart.md#场景-b资料或型号不足)、[C](quickstart.md#场景-c版本冲突)、[D](quickstart.md#场景-d权限和分享)、[LOCAL-QUERY](../../doc/development/本地查询执行与验证.md) | [EV-05](#ev-05)、[EV-08](#ev-08)、[EV-09](#ev-09)、[EV-11](#ev-11) | 默认授权范围或手动缩小范围均不扩大权限；当前适用/已发布选用版本、活动代际及精确型号过滤，证据不足不猜测；answer按受限候选和完整支持组交付；模型自由改写不能自行放行为formal | 新服务修订与解析/嵌入/Pi选用、逐用途许可的业务接线；pgvector不因合成向量检查而成为默认语义检索；真实召回与引用质量；PDF child到Pi组织答案的实际实现、逐声明人工参考与真实模型准入 |
| FR-008 任务、步骤、等待输入与恢复；[API8](../../doc/api/接口设计.md#8-知识检索与问答任务)、[API9](../../doc/api/接口设计.md#9-任务状态分享与文件结果)、[API18](../../doc/api/接口设计.md#18-pi宿主与执行桥接契约)、[API21](../../doc/api/接口设计.md#21-本地事务护栏增量2026-11-18)、[DB8](../../doc/database/数据库设计.md#8-任务重试证据与产物)、[DB13](../../doc/database/数据库设计.md#13-对话消息与条件快照候选设计)、[DB18](../../doc/database/数据库设计.md#18-pi会话与运行绑定设计) | T008、T011、T013、T016、T031、T032 / SC-003、SC-005 | [B](quickstart.md#场景-b资料或型号不足)、[F](quickstart.md#场景-f服务异常)、[LOCAL-QUEUE](../../doc/development/本地队列接入.md)、[LOCAL-QUERY](../../doc/development/本地查询执行与验证.md) | [EV-08](#ev-08)、[EV-12](#ev-12)、[EV-15](#ev-15)、[EV-17](#ev-17) | 任务与条件/来源/回执绑定，刷新和缺型号补充恢复原任务；租约/attempt与取消/撤权生效，未知调用不盲目重发 | 未覆盖的关闭/续租、任意多资源与更长故障窗口；真实环境任务及当前许可失效全过程验证 |
| FR-009 单条回答分享与接收者再次鉴权；[API9](../../doc/api/接口设计.md#9-任务状态分享与文件结果)、[API13.2](../../doc/api/接口设计.md#132-上下文与证据)、[API18.4](../../doc/api/接口设计.md#184-网页投影与完成)、[DB8.3](../../doc/database/数据库设计.md#83-结果与分享的权限)、[DB13.1](../../doc/database/数据库设计.md#131-与已有任务和分享的关系) | T007、T014、T031、T032 / SC-001、SC-004、SC-005 | [A](quickstart.md#场景-a正常查询)、[D](quickstart.md#场景-d权限和分享)、[LOCAL-QUERY](../../doc/development/本地查询执行与验证.md) | [EV-03](#ev-03)、[EV-08](#ev-08)、[EV-10](#ev-10) | 分享绑定确切回答快照及条件/版本/出处；接收者当前资料/操作授权、期限和撤销再次校验，不分享整段对话 | 真实账号/资料授权及分享过期/撤权矩阵；跨版本历史引用的业务可理解性核对 |
| FR-010 审计与日志脱敏；[API4](../../doc/api/接口设计.md#4-当前用户用户管理与授权接口)、[API17](../../doc/api/接口设计.md#17-知识管理员完整工作区对接设计)、[API19.5](../../doc/api/接口设计.md#195-受控任务操作与审计)、[API20](../../doc/api/接口设计.md#20-本地创建与资料生命周期实现映射2026-11-18)、[API22](../../doc/api/接口设计.md#22-文档维护提交时授权围栏2026-11-18)、[DB4](../../doc/database/数据库设计.md#4-用户角色与资料权限)、[DB8](../../doc/database/数据库设计.md#8-任务重试证据与产物)、[DB19.3](../../doc/database/数据库设计.md#193-变更控制与审计)、[DB20](../../doc/database/数据库设计.md#20-本地创建墓碑与恢复的事务映射2026-11-18) | T004、T009、T023、T026、T031 / SC-004、SC-007 | [D](quickstart.md#场景-d权限和分享)、[F](quickstart.md#场景-f服务异常)、[LOCAL-CREATE](../../doc/development/资料创建与生命周期验证.md)、[LOCAL-SERVICE](../../doc/development/服务适配与本地准入.md) | [EV-03](#ev-03)、[EV-04](#ev-04)、[EV-06](#ev-06)、[EV-07](#ev-07)、[EV-14](#ev-14) | 关键操作、拒绝/恢复与业务提交具可归属审计；运维不返回正文/凭据/签名URL，人工provider记录不冒充可信调用或账单 | 完整审计白名单、运维读写分离及真实主体归属；企业日志留存/访问策略与全面敏感字段核对 |
| FR-011 留存、逻辑删除、失效与恢复；[API19.6](../../doc/api/接口设计.md#196-备份策略任务与恢复切换)、[API20](../../doc/api/接口设计.md#20-本地创建与资料生命周期实现映射2026-11-18)、[API21](../../doc/api/接口设计.md#21-本地事务护栏增量2026-11-18)、[DB11](../../doc/database/数据库设计.md#11-更新删除与历史保留)、[DB19.4](../../doc/database/数据库设计.md#194-一致备份与密钥边界)、[DB19.5](../../doc/database/数据库设计.md#195-现行治理记录与恢复切换)、[DB20](../../doc/database/数据库设计.md#20-本地创建墓碑与恢复的事务映射2026-11-18) | T004、T010、T021、T025、T026、T031 / SC-003、SC-004、SC-007 | [D](quickstart.md#场景-d权限和分享)、[F](quickstart.md#场景-f服务异常)、[LOCAL-CREATE](../../doc/development/资料创建与生命周期验证.md)、[LOCAL-MIGRATION](../../doc/development/安全迁移与事故核查.md) | [EV-07](#ev-07)、[EV-10](#ev-10)、[EV-12](#ev-12)、[EV-13](#ev-13) | 撤权/撤回/删除影响索引、缓存、原文、结果及后续外发；恢复必须追平当前治理与副作用账本，不能复活旧权限 | 保留期限、最终清理、恢复和一致备份真实演练；默认SQLite历史影响UNKNOWN；本地保护不等于可恢复该库 |
| FR-012 逐服务许可、预算、用途和协议隔离；[API18.3](../../doc/api/接口设计.md#183-私有模型转发与外发校验)、[API19.4](../../doc/api/接口设计.md#194-服务凭据测试与激活)、[DB18](../../doc/database/数据库设计.md#18-pi会话与运行绑定设计)、[DB19.2](../../doc/database/数据库设计.md#192-规则服务与秘密) | T003、T009、T019、T024、T027、T031 / SC-003、SC-006、SC-007 | [F](quickstart.md#场景-f服务异常)、[LOCAL-SERVICE](../../doc/development/服务适配与本地准入.md) | [EV-01](#ev-01)、[EV-14](#ev-14)、[EV-15](#ev-15)、[EV-16](#ev-16)、[EV-17](#ev-17) | 配置/代码/模型/凭据/网络/CA和用途一致，派发前落账、当前许可检查；禁用/撤销阻断未发送请求，未知用量/费用保持未知，恢复不自动换供应商 | 企业secret与真实逐用途数据/账号/费用/留存准入、许可续期及结算/健康；本地选用、精确证明和未知账本不产生真实服务许可 |
| FR-013 三角色桌面网页与可理解异常；[API13](../../doc/api/接口设计.md#13-普通使用者对话与资料查阅候选设计)、[API15](../../doc/api/接口设计.md#15-普通使用者完整工作区对接设计)、[API17](../../doc/api/接口设计.md#17-知识管理员完整工作区对接设计)、[API19](../../doc/api/接口设计.md#19-系统管理员完整工作区对接设计)、[API20](../../doc/api/接口设计.md#20-本地创建与资料生命周期实现映射2026-11-18)、[DB13](../../doc/database/数据库设计.md#13-对话消息与条件快照候选设计)、[DB15](../../doc/database/数据库设计.md#15-普通使用者页面关联与输入来源补充)、[DB17](../../doc/database/数据库设计.md#17-知识管理员维护记录与一致性设计)、[DB19](../../doc/database/数据库设计.md#19-系统治理配置与恢复记录设计)、[DB20](../../doc/database/数据库设计.md#20-本地创建墓碑与恢复的事务映射2026-11-18) | T015、T016、T022、T027、T031、T033、T034 / SC-002、SC-003、SC-005 | [A](quickstart.md#场景-a正常查询)、[B](quickstart.md#场景-b资料或型号不足)、[D](quickstart.md#场景-d权限和分享)、[F](quickstart.md#场景-f服务异常)、[FEEDBACK](quickstart.md#反馈回访与更新后重新查询补充2026-11-17未执行) | [EV-04](#ev-04)、[EV-05](#ev-05)、[EV-07](#ev-07)、[EV-09](#ev-09)、[EV-16](#ev-16)、[EV-18](#ev-18) | 当前角色只见允许的动作/正文，失败可理解且未知操作恢复原回执；查询、维护、审批与服务配置的本地入口实际可操作，明确桌面支持范围 | 完整首期管理职责和FR-017两端交互；真实使用者可用性及企业浏览器/桌面视口矩阵 |
| FR-014 人工黄金样本与业务指标；[API12](../../doc/api/接口设计.md#12-联调验收与未决事项)、[DB12](../../doc/database/数据库设计.md#12-实施与验收边界) | T001、T002、T028、T029、T030、T031 / SC-001、SC-002、SC-006、SC-007 | [A](quickstart.md#场景-a正常查询)、[B](quickstart.md#场景-b资料或型号不足)、[C](quickstart.md#场景-c版本冲突)、[D](quickstart.md#场景-d权限和分享)、[E](quickstart.md#场景-e解析质量)、[F](quickstart.md#场景-f服务异常)、[FEEDBACK](quickstart.md#反馈回访与更新后重新查询补充2026-11-17未执行) | [EV-01](#ev-01)、[EV-02](#ev-02) | 参考答案和关键事实有人工/业务复核、原文位置与负面案例；指标定义样本/分母/方法后记录真实基线，阈值经确认才判达标 | 真实签认、适用确认、人工答案和阈值均TODO；没有真实A–F运行或业务指标通过证据 |
| FR-015 持续对话、条件与历史回答；[API13.1](../../doc/api/接口设计.md#131-操作与端点映射)、[API13.2](../../doc/api/接口设计.md#132-上下文与证据)、[API18.1](../../doc/api/接口设计.md#181-身份会话和运行绑定)、[DB13](../../doc/database/数据库设计.md#13-对话消息与条件快照候选设计)、[DB18](../../doc/database/数据库设计.md#18-pi会话与运行绑定设计) | T011、T015、T016、T031、T032 / SC-001、SC-003、SC-005 | [A](quickstart.md#场景-a正常查询)、[B](quickstart.md#场景-b资料或型号不足)、[D](quickstart.md#场景-d权限和分享)、[LOCAL-QUERY](../../doc/development/本地查询执行与验证.md) | [EV-08](#ev-08)、[EV-09](#ev-09)、[EV-10](#ev-10) | 已确认型号/条件随相应对话/任务修订持久化，缺项恢复不改写旧回答；新建/恢复对话和历史上下文读取重新鉴权，单条分享不暴露其他消息 | 完整多轮/新建/历史访问与任意修订组合；真实用户确认交互和当前权限下的模型上下文核对 |
| FR-016 授权手册查阅与精确版本参考开放；[API13.1](../../doc/api/接口设计.md#131-操作与端点映射)、[API17.6](../../doc/api/接口设计.md#176-待确认原文参考开放2026-11-17用户确认)、[API20](../../doc/api/接口设计.md#20-本地创建与资料生命周期实现映射2026-11-18)、[DB13.2](../../doc/database/数据库设计.md#132-直接资料查阅)、[DB17.6](../../doc/database/数据库设计.md#176-草稿原件参考可见性)、[DB20](../../doc/database/数据库设计.md#20-本地创建墓碑与恢复的事务映射2026-11-18) | T007、T013、T015、T018、T021、T031、T033 / SC-001、SC-002、SC-004、SC-005 | [A](quickstart.md#场景-a正常查询)、[B](quickstart.md#场景-b资料或型号不足)、[D](quickstart.md#场景-d权限和分享)、[LOCAL-QUERY](../../doc/development/本地查询执行与验证.md) | [EV-03](#ev-03)、[EV-05](#ev-05)、[EV-07](#ev-07)、[EV-09](#ev-09) | 按型号/文件名在当前授权范围查阅并打开对应版本原文；未发布草稿默认隔离；DM显式开放的精确版本仅供当前DR人工参考 | 授权跨库/型号别名/历史与参考状态完整矩阵；真实手册版本、出处和员工可理解性验收 |
| FR-017 资料反馈、受理回访及更新后重新查询；[API13.4](../../doc/api/接口设计.md#134-资料问题反馈候选设计)、[API17.4](../../doc/api/接口设计.md#174-反馈受理与四层维护投影)、[DB13.3](../../doc/database/数据库设计.md#133-资料问题反馈候选设计)、[DB17.4](../../doc/database/数据库设计.md#174-反馈范围分派与答复) | T015、T016、T022、T031、T034 / SC-002、SC-004、SC-005 | [B](quickstart.md#场景-b资料或型号不足)、[D](quickstart.md#场景-d权限和分享)、[FEEDBACK](quickstart.md#反馈回访与更新后重新查询补充2026-11-17未执行) | [EV-18](#ev-18)、[EV-02](#ev-02) | 对话/手册原入口提交并回访，附带内容按当前权限最小化；受理不授予整段对话/资料权限；答复不替代核验/确认/发布；可用更新后重新查询产生新回答，旧回答及其条件/来源保留 | 完整分派/移交与真实岗位受理、真实资料和浏览器验收 |

## 成功标准与判定方法

| 标准 | 任务 | 场景 / 证据 | 判定与缺口 |
| --- | --- | --- | --- |
| SC-001 关键事实、当前资格和定位可复核 | T002、T012、T013、T019、T020、T021、T028、T029、T031、T032、T033 | [A](quickstart.md#场景-a正常查询)、[C](quickstart.md#场景-c版本冲突)、[E](quickstart.md#场景-e解析质量) / [EV-05](#ev-05)、[EV-08](#ev-08)、[EV-09](#ev-09)、[EV-11](#ev-11)、[EV-02](#ev-02) | 逐条对照人工参考，确认型号/适用版本/当前权限/发布和原页定位实际支持关键事实；证据缺失不可判正式成功；真实人工基线与A/C/E未运行；合成来源绑定不替代业务正确性 |
| SC-002 缺失、冲突、待核验、越权及异常不冒充正式结果 | T007、T012、T013、T016、T019、T020、T021、T025、T028、T029、T031、T032、T033、T034 | [B](quickstart.md#场景-b资料或型号不足)、[C](quickstart.md#场景-c版本冲突)、[D](quickstart.md#场景-d权限和分享)、[E](quickstart.md#场景-e解析质量)、[F](quickstart.md#场景-f服务异常)、[FEEDBACK](quickstart.md#反馈回访与更新后重新查询补充2026-11-17未执行) / [EV-03](#ev-03)、[EV-05](#ev-05)、[EV-08](#ev-08)、[EV-14](#ev-14)、[EV-18](#ev-18)、[EV-02](#ev-02) | 正负样本得到正确可见状态/限制与可行动提示；无依据内容不混作正式答案；反馈不改变发布资格；真实负例、复杂PDF歧义及反馈真实运行覆盖 |
| SC-003 持久任务、一次副作用和恢复 | T006、T008、T011、T014、T016、T017、T018、T025、T029、T031、T032 | [B](quickstart.md#场景-b资料或型号不足)、[F](quickstart.md#场景-f服务异常)、[LOCAL-CREATE](../../doc/development/资料创建与生命周期验证.md)、[LOCAL-QUEUE](../../doc/development/本地队列接入.md)、[LOCAL-SERVICE](../../doc/development/服务适配与本地准入.md) / [EV-06](#ev-06)、[EV-08](#ev-08)、[EV-12](#ev-12)、[EV-13](#ev-13)、[EV-15](#ev-15)、[EV-17](#ev-17) | 刷新、撤权、取消、重试和进程中断可追踪；已提交不重复产物，未知调用先核对且预算/身份保留；未覆盖故障窗口及真实环境端到端恢复；受控时钟不能标为真实等待 |
| SC-004 撤权/撤回/删除的当前失效与恢复隔离 | T007、T010、T014、T018、T021、T025、T026、T029、T031、T032、T033、T034 | [D](quickstart.md#场景-d权限和分享)、[F](quickstart.md#场景-f服务异常)、[LOCAL-CREATE](../../doc/development/资料创建与生命周期验证.md)、[LOCAL-MIGRATION](../../doc/development/安全迁移与事故核查.md) / [EV-03](#ev-03)、[EV-07](#ev-07)、[EV-10](#ev-10)、[EV-11](#ev-11)、[EV-13](#ev-13)、[EV-18](#ev-18) | 当前读/用及最终提交检查受影响来源，索引/缓存/预览/下载/后续外发均失效；恢复追平治理不复活旧许可；完整永久清理/一致恢复/反馈引用和真实权限矩阵；已下载内容无法追回 |
| SC-005 真实桌面用户与管理职责闭环 | T005、T015、T016、T022、T023、T027、T029、T031、T032、T033、T034 | [A](quickstart.md#场景-a正常查询)、[B](quickstart.md#场景-b资料或型号不足)、[D](quickstart.md#场景-d权限和分享)、[F](quickstart.md#场景-f服务异常)、[FEEDBACK](quickstart.md#反馈回访与更新后重新查询补充2026-11-17未执行) / [EV-04](#ev-04)、[EV-05](#ev-05)、[EV-07](#ev-07)、[EV-09](#ev-09)、[EV-16](#ev-16)、[EV-18](#ev-18)、[EV-02](#ev-02) | 实际三角色用获准资料完成持续对话、新建/恢复对话、默认或缩小范围查询、手册查阅、出处核对、单条分享及首期管理任务和错误恢复，记录浏览器/视口及人工意见；真实角色/资料与人工可用性核对，反馈完整分派及剩余管理职责 |
| SC-006 人工样本与质量/资源/费用口径 | T002、T024、T028、T030、T031 | [A](quickstart.md#场景-a正常查询)、[E](quickstart.md#场景-e解析质量)、[F](quickstart.md#场景-f服务异常) / [EV-01](#ev-01)、[EV-02](#ev-02)、[EV-14](#ev-14)、[EV-16](#ev-16) | 样本、分母、计算方法、计时边界、上下文数量和供应商费用单位可复核，实际值与已确认阈值比较；未知不补零；真实人工样本、实际供应商用量/费用、业务阈值均未齐；协议/维度自检非语义质量 |
| SC-007 最小审计、恢复和删除演练 | T003、T006、T009、T010、T026、T030、T031 | [D](quickstart.md#场景-d权限和分享)、[F](quickstart.md#场景-f服务异常)、[LOCAL-CREATE](../../doc/development/资料创建与生命周期验证.md)、[LOCAL-MIGRATION](../../doc/development/安全迁移与事故核查.md) / [EV-03](#ev-03)、[EV-07](#ev-07)、[EV-12](#ev-12)、[EV-13](#ev-13)、[EV-17](#ev-17)、[EV-02](#ev-02) | 有实际审计、脱敏诊断及获准备份恢复/删除演练，恢复后当前权限和索引重核，未知副作用不盲目回放；真实保留/RPO/RTO/备份窗口和一致恢复/最终清理证据；默认SQLite事故影响仍UNKNOWN |

## 任务反向覆盖

| 任务 | 需求 | 成功标准 |
| --- | --- | --- |
| T001 | FR-001、FR-014 | 通过关联FR核对 |
| T002 | FR-001、FR-014 | SC-001、SC-006 |
| T003 | FR-012 | SC-007 |
| T004 | FR-002、FR-003、FR-005、FR-010、FR-011 | 通过关联FR核对 |
| T005 | FR-002 | SC-005 |
| T006 | FR-002 | SC-003、SC-007 |
| T007 | FR-003、FR-009、FR-016 | SC-002、SC-004 |
| T008 | FR-005、FR-008 | SC-003 |
| T009 | FR-010、FR-012 | SC-007 |
| T010 | FR-005、FR-011 | SC-004、SC-007 |
| T011 | FR-007、FR-008、FR-015 | SC-003 |
| T012 | FR-007 | SC-001、SC-002 |
| T013 | FR-007、FR-008、FR-016 | SC-001、SC-002 |
| T014 | FR-003、FR-009 | SC-003、SC-004 |
| T015 | FR-013、FR-015、FR-016、FR-017 | SC-005 |
| T016 | FR-008、FR-013、FR-015、FR-017 | SC-002、SC-003、SC-005 |
| T017 | FR-001、FR-005 | SC-003 |
| T018 | FR-001、FR-004、FR-005、FR-016 | SC-003、SC-004 |
| T019 | FR-006、FR-007、FR-012 | SC-001、SC-002 |
| T020 | FR-004、FR-006 | SC-001、SC-002 |
| T021 | FR-004、FR-007、FR-011、FR-016 | SC-001、SC-002、SC-004 |
| T022 | FR-004、FR-013、FR-017 | SC-005 |
| T023 | FR-002、FR-003、FR-010 | SC-005 |
| T024 | FR-012 | SC-006 |
| T025 | FR-003、FR-011 | SC-002、SC-003、SC-004 |
| T026 | FR-010、FR-011 | SC-004、SC-007 |
| T027 | FR-002、FR-012、FR-013 | SC-005 |
| T028 | FR-001、FR-006、FR-014 | SC-001、SC-002、SC-006 |
| T029 | FR-014 | SC-001、SC-002、SC-003、SC-004、SC-005 |
| T030 | FR-014 | SC-006、SC-007 |
| T031 | FR-001、FR-002、FR-003、FR-004、FR-005、FR-006、FR-007、FR-008、FR-009、FR-010、FR-011、FR-012、FR-013、FR-014、FR-015、FR-016、FR-017 | SC-001、SC-002、SC-003、SC-004、SC-005、SC-006、SC-007 |
| T032 | FR-007、FR-008、FR-009、FR-015 | SC-001、SC-002、SC-003、SC-004、SC-005 |
| T033 | FR-007、FR-013、FR-016 | SC-001、SC-002、SC-004、SC-005 |
| T034 | FR-013、FR-017 | SC-002、SC-004、SC-005 |

PDF/Pi子任务沿用父任务FR/SC；身份、父任务、依赖与验收保留在tasks正文，检查器校验全部子任务及跨专项依赖，不另维护第二张状态表。

## 证据目录与限制

历史盘点：2026-11-18。详细运行身份见原[证据盘点](../../outputs/service-use/2026-11-18/evidence-inventory.json)；以下为非敏感索引，不能替代本机原始证据或当前候选复验。

### EV-01

需求入口、清单与验收格式；证据类别 `static_artifact`。

支持范围：入口、清单/参考答案格式及运行登记字段均可定位。正式范围清单有89份记录，登记为pending / unreviewed / draft。

限制/缺口：需获准的黄金样本、逐版本适用性与关键内容复核、人工答案及回归基线。清单入选不赋予发布资格或外部处理许可。

来源：[feature.json（功能目录入口）](../../.specify/feature.json)、[README.md（权威文档导航）](README.md)、[formal-scope-manifest.json（正式范围清单的登记状态）](artifacts/formal-scope-manifest.json)、[inventory.schema.json（清单字段约束）](artifacts/inventory.schema.json)、[inventory.example.json（清单格式示例）](artifacts/inventory.example.json)、[reference-answer.schema.json（人工答案记录格式）](artifacts/reference-answer.schema.json)、[reference-answer.example.json（参考答案格式示例）](artifacts/reference-answer.example.json)、[operations.md（受控本地环境和外部服务登记字段）](operations.md)

### EV-02

正式A–F与人工指标的待办；证据类别 `static_artifact`。

支持范围：正式A–F验收记录仍为NOT_RUN，反馈补充也未运行。检索/引用/时延/上下文/费用以及备份恢复和删除演练没有业务基线。

限制/缺口：需实际岗位及签认、获准文字/图文/扫描PDF、版本适用与关键内容确认、人工参考答案及冲突/否定样本。逐服务真实调用另需供应商、数据范围、账号、网络/TLS、额度/费用和留存许可。上述真实待办约束真实验收或调用，独立本地开发可继续。

来源：[quickstart.md（A–F及反馈扩展验收指南）](quickstart.md)、[README.md（验收记录结构）](artifacts/acceptance/README.md)、[status.md（A–F未验证状态）](artifacts/acceptance/status.md)、[README.md（指标记录结构）](artifacts/metrics/README.md)、[status.md（人工指标未建立状态）](artifacts/metrics/status.md)、[real-acceptance-readiness.json（真实材料与逐服务准入待办）](../../outputs/service-adapters/2026-11-18/real-acceptance-readiness.json)

### EV-03

账号会话、完整授权和当前访问；证据类别 `local_synthetic_runtime`。

支持范围：合成测试验证口令摘要、完整管理组合、审批范围不符拒绝、已受理任务在登出后的持续性、读取时察觉停用或会话撤销。撤销直接授权后不回退到继承范围。

限制/缺口：初始管理员并发、凭据重置和全量会话轮换/超时矩阵，以及真实三角色账号与撤权矩阵仍需专项证据。

来源：[security.py（口令与令牌摘要）](../../src/server/security.py)、[app.py（平台账号、会话管理和HTTP鉴权入口）](../../src/server/app.py)、[services.py（会话与权限组合复核）](../../src/server/services.py)、[models.py（账号、会话和授权记录）](../../src/server/models.py)

历史节点示例：`src/server/test_services.py::test_password_digest_is_verifiable_without_plaintext`；`src/server/test_cache_permission_investigation.py::test_direct_revocation_cannot_fall_back_to_inherited_access`；`src/server/test_cache_permission_investigation.py::test_in_flight_request_observes_committed_account_revocation[account-/result]`。

### EV-04

审批登记、材料核查和撤销；证据类别 `local_synthetic_browser`。

支持范围：隔离环境的管理端走通待核材料关联、核查、撤销及角色拒绝。服务测试覆盖当前依据、范围、期限和越范围替换拒绝。

限制/缺口：需要真实外部审批权源/岗位与批准链。完整账号角色、授权预检、审计及系统管理员S系列页面仍有验收缺口。

来源：[governance_api.py（材料核查与审批动作入口）](../../src/server/governance_api.py)、[governance.py（当前授权基于真实依据与有效期的门禁）](../../src/server/governance.py)、[governance_models.py（审批记录）](../../src/server/governance_models.py)、[AdminOperations.tsx（管理员浏览器工作区）](../../src/web/features/admin/AdminOperations.tsx)、[registration.tsx（登记和核查界面）](../../src/web/features/admin/registration.tsx)

历史节点示例：`src/server/test_workspace_browser.py::test_react_admin_pending_proof_verification_revocation_and_role_refusal`；`src/server/test_governance.py::test_admin_cannot_create_external_approval_fact`；`src/server/test_governance.py::test_approval_evidence_authority_scope_and_expiry_are_current_gates`。

### EV-05

业务确认、核验批次、参考开放和发布；证据类别 `local_synthetic_browser`。

支持范围：Chrome覆盖合成版本上传、确认/复核、预检、正式发布/索引及撤回或失权；服务测试验证警告覆盖、选用批次、定位变更后失效、未知生效日期、精确草稿人读参考及当前DR复核。

限制/缺口：须用获准真实版本核对适用性、并行版本、扫描歧义和业务/专业确认。完整K00–K10与企业审批流程仍待验收。

来源：[governance.py（真实确认门禁、批次选用、发布和参考开放规则）](../../src/server/governance.py)、[governance_api.py（核验、预检与状态入口）](../../src/server/governance_api.py)、[providers.py（解析与原文定位边界）](../../src/server/parsing/providers.py)、[KnowledgeAdmin.tsx（知识管理员工作区）](../../src/web/features/knowledge/KnowledgeAdmin.tsx)、[confirmations.tsx（确认与核验界面）](../../src/web/features/knowledge/confirmations.tsx)、[publication.tsx（预检和发布界面）](../../src/web/features/knowledge/publication.tsx)

历史节点示例：`src/server/test_workspace_browser.py::test_react_manager_review_confirmation_publication_and_access_loss`；`src/server/test_governance.py::test_review_coverage_cannot_omit_warning_or_use_wrong_batch`；`src/server/test_governance.py::test_reference_open_is_exact_human_inline_only_and_revocation_is_immediate`。

### EV-06

新知识库和文档登记、上传与恢复；证据类别 `local_synthetic_runtime`。

支持范围：本地受控登记不自授权限，文件及类别门禁失败无可见文档；上传大小失败清理临时文件，最终范围失效回滚版本与回执。并发版本号不同，覆盖文件先落地后进程终止时精确复用以及内容冲突拒绝。

限制/缺口：须在目标企业存储和真实样本验证批量/中断、保留和清理策略。有限进程终止窗口不覆盖全部故障模型。

来源：[lifecycle_api.py（知识库和文档登记入口）](../../src/server/lifecycle_api.py)、[document_lifecycle.py（候选资料生命周期）](../../src/server/document_lifecycle.py)、[app.py（上传和版本事务）](../../src/server/app.py)、[services.py（受控原件存储与上传逻辑）](../../src/server/services.py)、[creation.tsx（登记页面实现位置）](../../src/web/features/knowledge/creation.tsx)

历史节点示例：`src/server/test_creation.py::test_new_kb_registration_never_creates_business_grants`；`src/server/test_creation.py::test_unapproved_category_and_non_pdf_do_not_create_visible_document`；`src/server/test_services.py::test_upload_size_failure_cleans_temporary_file`。

### EV-07

暂停、逻辑删除、原件隔离及维护竞态；证据类别 `local_synthetic_browser`。

支持范围：Chrome及本地服务覆盖新PDF、暂停后显式解除、逻辑删除未知响应使用原回执、回收站受限元数据/撤权。合成竞态验证删除、停用、撤权对维护和交付的部分窗口；保留的人读原件与正式查询资格分离。

限制/缺口：最终清理与恢复未开放，留存时限与一致性备份未验证。治理史和清屏证据不能覆盖全部派生数据、外发和恢复后的权限索引核对。

来源：[document_lifecycle.py（暂停、解限、逻辑删除及历史元数据）](../../src/server/document_lifecycle.py)、[lifecycle_api.py（生命周期HTTP入口）](../../src/server/lifecycle_api.py)、[operations.py（逻辑删除及依赖失效）](../../src/server/operations.py)、[app.py（版本/维护/原件路径和会话闸门）](../../src/server/app.py)、[lifecycle.tsx（生命周期浏览器视图）](../../src/web/features/knowledge/lifecycle.tsx)

历史节点示例：`src/server/test_lifecycle_browser.py::test_lifecycle_manager_registration_new_pdf_and_explicit_hold_resolution`；`src/server/test_lifecycle_browser.py::test_lifecycle_delete_unknown_response_metadata_retention_and_revocation`；`src/server/test_original_lifecycle.py::test_held_original_can_be_reviewed_but_never_used_as_formal_answer`。

### EV-08

任务、对话条件、证据和真实本地SDK；证据类别 `local_synthetic_runtime`。

支持范围：真实本地Node/Pi SDK接回环合成模型，保留出处和警告，正式回答来自验证后的证据；缺型号补充恢复同任务，上游失败可见且不重复调用。覆盖确认条件修订、作用域隔离、取消/撤权门禁和短暂提示词与持久摘要依赖边界。

限制/缺口：仅有八个查询工具入口和部分对话/任务子集。无真实模型/资料正确性、完整20工具/四层产品记忆、正式桌面持续对话或所有版本冲突场景验收。

来源：[execution.py（任务、条件修订、工具动作和证据提交）](../../src/server/execution.py)、[execution_api.py（查询运行工具和员工补充）](../../src/server/execution_api.py)、[execution_models.py（对话、输入和动作记录）](../../src/server/execution_models.py)、[runtime_gate.py（模型出口与预算/许可围栏）](../../src/server/runtime_gate.py)、[worker.py（原任务和Node运行器）](../../src/server/worker.py)、[runtime-host.ts（受控Pi SDK运行时）](../../src/agent_runtime/src/runtime-host.ts)、[controlled-provider.ts（合成协议模型出口）](../../src/agent_runtime/src/controlled-provider.ts)

历史节点示例：`src/server/test_worker.py::test_real_sdk_worker_query_preserves_pdf_warning_and_enforces_final_answer`；`src/server/test_worker.py::test_missing_model_resumes_original_task_through_real_worker`；`src/server/test_worker.py::test_real_sdk_upstream_failure_remains_visible_and_is_not_retried`。

### EV-09

桌面查询恢复、状态和原文核对；证据类别 `local_synthetic_browser`。

支持范围：真实Chrome在React工作区通过单次任务恢复、等待型号、刷新结果和精确PDF页访问；撤权清除引文且原件访问拒绝。旧参考页另验证1366×900、离线恢复、三角色对合成原件的访问分界。

限制/缺口：引用打开已观察，仍需跨库按型号/文件名查阅、正式React桌面持续/新建对话、版本冲突与完整错误状态、浏览器兼容及布局验收。旧参考页离线行为不外推至整个React界面。

来源：[App.tsx（正式React查询工作区）](../../src/web/features/query/App.tsx)、[api.ts（请求及权限变化处理）](../../src/web/api.ts)、[query.js（历史静态参考页用于独立Chrome场景）](../../src/server/static/query.js)、[app.py（查询、结果和精确版本原件入口）](../../src/server/app.py)

历史节点示例：`src/server/test_workspace_browser.py::test_react_query_unknown_response_resume_input_result_and_revocation`；`src/server/test_query_browser.py::test_local_browser_wait_resume_restore_citations_and_roles`。

### EV-10

单条回答分享与历史读取；证据类别 `local_synthetic_runtime`。

支持范围：本地API将分享绑定确切完成快照，接收者无需看到私有输入或条件记录且仍须拥有当前原文权限；客户端写入门禁、撤销的幂等重放和新执行不追改旧分享已覆盖。

限制/缺口：需要真实三角色的分享、到期、撤销和浏览器全流程，含发出方/接收方的完整操作授权矩阵。

来源：[app.py（单回答分享HTTP入口）](../../src/server/app.py)、[execution.py（当前交付和绑定历史回答）](../../src/server/execution.py)、[services.py（当前证据读取和来源门禁）](../../src/server/services.py)、[execution_models.py（回答绑定对象）](../../src/server/execution_models.py)

历史节点示例：`src/server/test_answer_share.py::test_recipient_reads_only_answer_with_current_dr_and_cannot_read_owner_input_or_conditions`；`src/server/test_answer_share.py::test_existing_share_does_not_grant_source_access_after_dr_is_revoked`；`src/server/test_answer_share.py::test_share_put_requires_csrf_etag_idempotency_and_replay_cannot_undo_revocation`。

### EV-11

本地向量索引、旧内容和当前访问；证据类别 `local_synthetic_runtime`。

支持范围：隔离PG/pgvector验证1024维读写和余弦运算、合成正式来源最近邻查询与当前授权、模型标识绑定、错误向量及旧索引不使用新内容。

限制/缺口：合成索引存取与现有业务关键词路径各自受测。新BGE-M3适配器还需受控接入业务索引，检索召回/重排和资料规模未用黄金样本测量。

来源：[vector_store.py（本地PG向量存取与当前来源校验）](../../src/server/vector_store.py)、[0007_pgvector.py（向量存储迁移）](../../src/server/migrations/versions/0007_pgvector.py)、[services.py（查询与索引状态）](../../src/server/services.py)

历史节点示例：`src/server/test_local_postgres.py::test_vector_1024_roundtrip_and_cosine_operator`；`src/server/test_local_postgres.py::test_synthetic_formal_source_upsert_nearest_and_current_acl`；`src/server/test_services.py::test_stale_index_does_not_answer_from_new_content`。

### EV-12

任务队列、租约和投递恢复；证据类别 `local_synthetic_runtime`。

支持范围：真实本地PG/RabbitMQ/Celery消费合成原任务；信封仅任务和派发ID，过期租约、双竞争发布者、已提交取消及一个进程中断窗口保留身份、回执和未知预算。

限制/缺口：不覆盖Broker确认所有不确定窗口、多机、高可用、故障转移、容量或企业备份恢复。此组中的进程用例属于404条JUnit，EV-17为独立报告。

来源：[queue_runtime.py（Celery/RabbitMQ稳定ID信封）](../../src/server/queue_runtime.py)、[worker.py（查询Worker）](../../src/server/worker.py)、[execution.py（任务与outbox事务）](../../src/server/execution.py)、[runtime_gate.py（调用去重、预算与双租约）](../../src/server/runtime_gate.py)

历史节点示例：`src/server/test_local_queue.py::test_postgres_and_rabbitmq_celery_worker_completes_original_task`；`src/server/test_local_queue.py::test_publisher_claims_database_rows_and_emits_stable_id_only_envelope`；`src/server/test_postgres_recovery.py::test_expired_session_and_step_leases_fence_validated_old_worker`。

### EV-13

迁移隔离、受控升级和事故库保护；证据类别 `local_synthetic_runtime`。

支持范围：验证默认SQLite和未明确目标拒绝、合成SQLite备份保全与提交不确定后的状态、显式隔离PG的迁移/服务修订CAS。封存核对记录事故文件未再变更，历史影响仍UNKNOWN。 运维模块另可定位任务和供应商的脱敏投影。

限制/缺口：事故库缺事前可靠证据。显式隔离库迁移与SQLite保全用例无法替代既有非空PG停写审阅升级，也未证明一致性备份恢复与恢复后权限/索引复核。 端到端审计与脱敏诊断完备性仍需专项验证。

来源：[migrate.py（显式计划/校验与回执入口）](../../src/server/migrate.py)、[migration_safety.py（迁移前态、目标锁和备份记录）](../../src/server/migration_safety.py)、[db.py（事故SQLite连接保护）](../../src/server/db.py)、[env.py（禁止隐式目标）](../../src/server/migrations/env.py)、[0008_service_admission.py（服务管理迁移）](../../src/server/migrations/versions/0008_service_admission.py)、[operations.py（脱敏任务/供应商投影与逻辑删除入口，静态定位）](../../src/server/operations.py)、[checks.json（封存本地阶段及事故库已核对状态）](../../outputs/service-adapters/2026-11-18/checks.json)

历史节点示例：`src/server/test_migration_safety.py::test_accident_command_is_refused_without_opening_default_database`；`src/server/test_migration_safety.py::test_existing_sqlite_is_backed_up_and_rows_and_triggers_survive`；`src/server/test_migration_safety.py::test_lost_commit_ack_requires_inspection_instead_of_refusal_or_retry`。

### EV-14

三类适配与合成业务解析/向量衔接；证据类别 `local_synthetic_runtime`。

支持范围：精确输入/用途/修订许可接入业务解析和嵌入，固定v4+legacy协议、原ZIP私有保留、畸形隔离、1024维向量原子落PG；HTTP/HTTPS门禁和来源警告仍需人工核验。

限制/缺口：仅合成、小批次、旧格式固定协议；真实供应商、获准复杂PDF/语义质量/企业准入未验收。

来源：[mineru.py（MinerU V4和legacy Content List V1边界）](../../src/server/parsing/mineru.py)、[inference_adapters.py（OpenAI兼容生成/BGE-M3响应验证）](../../src/server/inference_adapters.py)、[provider_transport.py（协议传输界面）](../../src/server/provider_transport.py)、[service_admission.py（绑定端点/模型/凭据/CA及固定样本验证）](../../src/server/service_admission.py)、[synthetic_provider.py（确定性回环服务夹具）](../../src/server/synthetic_provider.py)、[service_use.py（本轮业务用途增量）](../../src/server/service_use.py)、[parse_jobs.py（本轮业务用途增量）](../../src/server/parse_jobs.py)、[embedding_jobs.py（本轮业务用途增量）](../../src/server/embedding_jobs.py)

历史节点示例：`src/server/test_embedding_service_use.py::test_http_acceptance_pinned_worker_pgvector_and_unchanged_keyword_index`；`src/server/test_embedding_service_use.py::test_unknown_post_cannot_be_resubmitted_with_new_http_key_or_worker_attempt`；`src/server/test_embedding_service_use.py::test_changed_review_snapshot_blocks_provider_write`。

### EV-15

逐用途许可、固定修订、预算与Pi账本；证据类别 `local_synthetic_runtime`。

支持范围：五用途选用和固定pin、永久撤销epoch、精确上下文证明、当前权限/租约、调用/预算CAS；真实Pi1.5.2按固定修订派发，未知不重发；迁移表定义指纹及PG最后预算争用验证。

限制/缺口：不是企业secret/真实批准/计费健康或许可续期；SDK自动摘要/压缩全流程未验证；费用未知，预算不代表真实账单。

来源：[service_admission.py（持久调用意图及受约束执行）](../../src/server/service_admission.py)、[service_api.py（需当前账号会话的服务管理API）](../../src/server/service_api.py)、[service_models.py（配置修订、自检、预检和尝试账本）](../../src/server/service_models.py)、[service_use_models.py（本轮业务用途增量）](../../src/server/service_use_models.py)、[runtime_gate.py（本轮业务用途增量）](../../src/server/runtime_gate.py)、[migration_safety.py（本轮业务用途增量）](../../src/server/migration_safety.py)、[service_use_api.py（本轮业务用途增量）](../../src/server/service_use_api.py)

历史节点示例：`src/server/test_pi_service_use.py::test_selected_revision_is_pinned_through_real_sdk_worker`；`src/server/test_pi_service_use.py::test_selected_purpose_and_input_are_each_required`；`src/server/test_pi_service_use.py::test_enabled_auxiliary_purpose_requires_separate_pin_and_exact_proof`。

### EV-16

实际桌面管理页用途选用与原请求恢复；证据类别 `local_synthetic_browser`。

支持范围：实际Chrome/PG验证逐用途预检不改状态、选用确认丢响应后按原key/If-Match/body恢复、缺凭据安全停用；16客户端/类型/构建独立通过。

限制/缺口：桌面合成子流程，不代表全系统管理员职责及真实A–F。构建链既有high未消失，不部署。

来源：[ServiceProfiles.tsx（服务配置界面）](../../src/web/features/admin/ServiceProfiles.tsx)、[AdminOperations.tsx（管理员入口）](../../src/web/features/admin/AdminOperations.tsx)、[api.ts（真实客户端API）](../../src/web/api.ts)、[api.test.mjs（前端客户端测试来源）](../../src/web/test/api.test.mjs)、[build-checks.json（前端三项命令结束码）](../../outputs/service-adapters/2026-11-18/build-checks.json)、[build.log（16项客户端检查结果）](../../outputs/service-adapters/2026-11-18/build.log)、[web-source-manifest.json（前端源码绑定）](../../outputs/service-adapters/2026-11-18/web-source-manifest.json)、[web-build-manifest.json（构建产物文件绑定）](../../outputs/service-adapters/2026-11-18/web-build-manifest.json)

历史节点示例：`src/server/test_service_browser.py::test_react_services_protocol_tests_previews_activation_recovery_and_safe_stop`。

### EV-17

业务解析进程退出、原batch恢复及源作业防重；证据类别 `local_synthetic_process_recovery`。

支持范围：当前业务解析子进程实际退出、batch在PUT前由独立DB连接观察已提交；受控时钟推进121秒后只GET原batch，POST/PUT各一次；新key与PG并发不能绕过未决源作业。

限制/缺口：进程退出与受控时钟推进分别标明，不声称真实等待121秒、覆盖所有崩溃窗口/HA；原ZIP可能留私有孤儿待清理。旧两案独立实测只按原身份保留。

来源：[parse_jobs.py（本轮业务用途增量）](../../src/server/parse_jobs.py)、[parse_worker.py（本轮业务用途增量）](../../src/server/parse_worker.py)、[test_parse_service_use.py（本轮业务用途增量）](../../src/server/test_parse_service_use.py)

历史节点示例：`src/server/test_parse_service_use.py::test_selected_parse_accepts_one_business_run_and_emits_unreviewed_source_blocks[sqlite]`；`src/server/test_parse_service_use.py::test_selected_parse_accepts_one_business_run_and_emits_unreviewed_source_blocks[postgres]`；`src/server/test_parse_service_use.py::test_other_resource_attestation_and_changed_original_cannot_send[sqlite]`。

### EV-18

FR-017反馈与回访的设计契约；证据类别 `source_inspection`。

支持范围：静态材料给出对话/手册入口、权限检查、最小附带内容、受理/回访、资料更新后重查并保留旧回答及独立业务门禁。当前快照无FR-017端到端或对应工具运行证据。

限制/缺口：提交/受理/答复、分派、资料更新提示、独立原回答与重查、范围与撤权、脱敏审计及合成串联仍需实现和验证；真实岗位由业务确认。

来源：[spec.md（FR-017和独立业务门禁）](spec.md)、[ui.md（员工提交及原入口回访的候选UI）](contracts/ui.md)、[quickstart.md（反馈扩展场景及未执行）](quickstart.md)、[tasks.md（T034的实施和验收范围）](tasks.md)、[接口设计.md（13.4、17节候选HTTP/受理契约）](../../doc/api/接口设计.md)、[数据库设计.md（13.3、17节候选持久化）](../../doc/database/数据库设计.md)、[execution.py（核查运行工具注册/别名和启用集）](../../src/server/execution.py)、[runtime-host.ts（核查SDK工具白名单）](../../src/agent_runtime/src/runtime-host.ts)

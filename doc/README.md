# 开发文档索引

按用途分类存放；本文件只作主题索引。项目进度统一见 [项目状态与续跑入口](planning/STATUS.md)；主题文档描述需求与设计，不独立维护当前进度。

| 文件夹 | 用途 | 文档 |
| --- | --- | --- |
| `requirements/` | 产品目标、业务范围与验收要求 | [SPEC.md](requirements/SPEC.md) |
| PDF处理专项 | 八阶段架构、IR、切分/QC与最小评测的主题规范；实施与真实验证状态统一从STATUS进入 | [SPEC10](requirements/SPEC.md#10-pdf-文档处理架构-spec2026-11-18待审核)、[IR数据契约](database/数据库设计.md#22-document-ir-数据契约候选01)、[MinerU固定版本审计](architecture/模型与解析方案.md#11-mineru-版本能力与输出契约审计)、[实施依赖](../specs/001-device-operation-maintenance-query/tasks.md#pdf-processing-实施任务p0p6) |
| `roles/` | 各角色职责、可见范围与权限边界 | [普通使用者](roles/普通使用者.md)、[知识管理员](roles/知识管理员.md)、[系统管理员](roles/系统管理员.md) |
| `architecture/` | 技术选型、组件职责与迁移设计 | [前后端技术方案](architecture/前后端技术方案.md)、[模型与解析方案](architecture/模型与解析方案.md)、[Pi接入与改造](architecture/Pi-Agent接入与改造方案.md) |
| `design/` | 完整用户页面、流程与对接方案 | [普通使用者界面与对接](design/普通使用者界面与对接方案.md)、[知识管理员界面与对接](design/知识管理员界面与对接方案.md)、[系统管理员界面与对接](design/系统管理员界面与对接方案.md) |
| `database/` | 表结构、关系、约束与数据生命周期 | [数据库设计](database/数据库设计.md) |
| `api/` | 端点、请求响应、权限与异步契约 | [接口设计](api/接口设计.md) |
| `planning/` | 项目状态、实施顺序与业务前置条件 | [STATUS.md](planning/STATUS.md)、[PLAN.md](planning/PLAN.md)、[开工前待确定项](planning/开工前待确定项.md) |
| `development/` | 已实现路径、命令与验证边界 | [本地查询执行与验证](development/本地查询执行与验证.md)、[安全迁移与事故核查](development/安全迁移与事故核查.md)、[创建与生命周期](development/资料创建与生命周期验证.md)、[服务适配与准入](development/服务适配与本地准入.md)、[React前端](../src/web/README.md)、[PostgreSQL](development/本地PostgreSQL接入.md)、[队列](development/本地队列接入.md) |
| `../specs/` | 原生 Spec-Kit feature 规格、计划和任务 | [首期 feature](../specs/001-device-operation-maintenance-query/spec.md)、[准备基线](../specs/001-device-operation-maintenance-query/baseline.md)、[需求追踪](../specs/001-device-operation-maintenance-query/traceability.md)、[任务](../specs/001-device-operation-maintenance-query/tasks.md) |
| `memory/` | 跨会话项目理解、决策演进与当前状态 | [Harness Memory 入口](memory/README.md) |

## 文档维护

从README→STATUS→tasks恢复当前动作；事实所有者、改动影响矩阵及生成命令统一见[baseline第7节](../specs/001-device-operation-maintenance-query/baseline.md#7-唯一入口与文档维护)。FR/SC正文只在feature spec，项目SPEC拥有业务细则；旧兼容目录只跳转。开工、文档变更和交付运行`python -m src.tools.project_docs`，改traceability.json后用`--write-traceability`重生成投影。静态通过不核准业务、外发或历史证据继续有效。

## 当前状态

查询或修改进度从 [项目状态](planning/STATUS.md) 进入；具体任务在 [首期任务清单](../specs/001-device-operation-maintenance-query/tasks.md) 维护。旧 `planning/first-release-device-query/` 路径仅保留兼容导航，首期设计和场景统一在原生 feature 目录维护。

## 角色需求

三个角色各有独立文件，继续保留在 `roles/`；它们定义职责与权限要求，企业实际授权和实现验收见项目状态及对应证据。此节作为既有角色索引链接的稳定入口。

项目概览见 [项目 README](../README.md)，协作约定见 [AGENTS.md](../AGENTS.md)。

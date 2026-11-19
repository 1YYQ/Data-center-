# 数据中心

持续沉淀企业文档，通过大模型接口获取、理解和整合授权资料，辅助解决实际办公问题。

## 要解决的问题

机器操作手册、计算公式手册等资料反复输入 AI，增加上下文占用，也使版本、公式条件与结果来源难以管理。本项目计划将资料统一入库，按任务检索相关片段，结合程序计算生成可核验的产物。

## 预期流程

资料沉淀与版本权限管理 → 按工作问题检索依据 → 理解与整合内容 → 按需调用计算等工具 → 交付并核验办公成果。

RAG 不保证消除幻觉；证据缺失、冲突与解析歧义须显式处理。

## 当前状态

进度、实际检查结果、未验证范围与下一步统一维护在 **[项目状态与续跑入口](doc/planning/STATUS.md)**。本 README 保留项目定位、运行方式和文档导航，不重复维护进度快照。

首期目标为 [设备操作维护查询](doc/requirements/SPEC.md#13-首期场景设备操作与维护资料查询)：按型号查找有权且已确认适用的资料，核对说明、版本与出处，并明确反馈缺失和冲突。计算填表、多资料对比及报告随后推进。

## 本地运行与检查

先按[安全迁移](doc/development/安全迁移与事故核查.md)显式准备隔离库，应用启动只检查结构；事故默认SQLite已隔离。服务入口为 `python run.py`（或 `uvicorn src.server.app:app`）；配置见 [运行边界](specs/001-device-operation-maintenance-query/operations.md)。默认本地实现与正式技术路线的差距、最近核对结果见项目状态，不据启动命令推断验收通过。

已配置的持久合成试点使用`python -m src.server.pilot serve --port 8879`，包含网页和查询Worker；首次准备、三角色凭据、服务准入及每日备份命令见[持久试点运行入口](doc/development/本地查询执行与验证.md#持久隔离试点入口)。

- 本地服务层回归：`python -m pytest src/server/test_services.py -q`。
- 会话记忆结构校验：`python -m src.memory.validate`。
- 数据库迁移：使用 `python -m src.server.migrate plan/apply` 显式目标与前态；完整命令见[安全迁移](doc/development/安全迁移与事故核查.md)。裸Alembic被拒绝。
- React网页：在 `src/web` 执行 `npm ci`、`npm run typecheck`、`npm run build`；FastAPI提供 `/workspace/query`、`/workspace/manage`、`/workspace/access-review`，详见[前端运行说明](src/web/README.md)。

## 文档入口

工作从[项目状态与续跑入口](doc/planning/STATUS.md)开始，沿其指针进入当前任务；全部主题文档从[doc/README.md](doc/README.md)查找。规范所有权和变更联动见[开发准备基线](specs/001-device-operation-maintenance-query/baseline.md#7-唯一入口与文档维护)，Agent约定见[AGENTS.md](AGENTS.md)。README和Memory不另存“当前进度/下一任务”。

开工、文档变更和交付时运行离线结构检查：

```powershell
python -m src.tools.project_docs
```

需求追踪只改[traceability.json](specs/001-device-operation-maintenance-query/traceability.json)，再运行`python -m src.tools.project_docs --write-traceability`。检查不联网、不读取企业原件/数据库、不执行产品测试；通过不等于方案业务确认或真实验收。

## 项目结构

```text
data-center/
├── AGENTS.md       # Agent 工作约定
├── README.md       # 项目概览
├── doc/            # 开发文档
│   ├── README.md   # 文档入口
│   ├── requirements/ # 产品需求（SPEC）
│   ├── planning/   # 实施计划与待确定项
│   ├── roles/      # 角色需求，每个角色一个文件
│   ├── architecture/ # 技术方案与迁移设计
│   ├── database/   # 数据库设计
│   └── api/        # 接口设计
├── src/            # 开发代码
│   ├── server/     # 首期 FastAPI、模型、服务、迁移与静态查询页
│   ├── tools/      # 89 份 PDF 非敏感清单生成工具
│   ├── web/        # React/TypeScript/Vite 三角色工作区
│   ├── memory/     # Harness Memory 结构校验
│   └── verify/     # 通用 Harness Verify 模块
├── .verify/        # 任务契约与 Verify 使用说明
├── verify.py       # Verify CLI 入口
├── .gitattributes
└── .gitignore
```

开发文档从 [doc/README.md](doc/README.md) 进入。目录职责见 [AGENTS.md](AGENTS.md#目录约定)。`data/` 和 `outputs/` 按需在本地创建，不纳入版本管理。

## Verify 验收入口

SPEC → PLAN → IMPLEMENT 之后，可用 `python verify.py --task <任务 ID>` 触发只读验收。
任务契约位于 `.verify/tasks/`，会读取 AGENTS.md、SPEC、PLAN 和 Contract，优先复用带源码
哈希的现有证据，必要时执行契约中声明的项目检查。输出 `PASS`、`FAIL`、`BLOCKED` 或
`INCONCLUSIVE`；只有 `PASS` 返回退出码 0。完整格式与样例见 [.verify/README.md](.verify/README.md)。

## 下一步

从 [项目状态的下一步](doc/planning/STATUS.md#下一步与依赖) 进入相应任务检查点；恢复时核对当前工作区和证据，按已授权范围推进。

## 本地数据约定

真实手册与业务输入放在 `data/`，生成产物放在 `outputs/`；这些目录以及索引、日志和环境密钥默认被 Git 忽略。忽略规则不是数据防泄漏机制，提交前仍需检查暂存内容。

仓库仅存放项目代码、文档与经过确认的非敏感样本。已确定的模型、数据库、前后端及配套基线见技术方案；具体版本、首期输入格式集合与接入细节仍需结合获准样本及企业约束验证，后续办公成果的交付要求见 SPEC。

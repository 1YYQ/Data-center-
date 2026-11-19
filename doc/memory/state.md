# 跨会话状态指针与 Memory 历史证据

入口更新日期：2026-11-19。项目阶段、实际检查结果、依赖和下一步统一读取 [项目状态](../planning/STATUS.md)；具体执行只从[首期tasks顶部当前调度](../../specs/001-device-operation-maintenance-query/tasks.md#当前调度与本次审查preflight-001)进入；旧检查点只作历史证据。规范所有权、开工准入与维护规则见[baseline](../../specs/001-device-operation-maintenance-query/baseline.md#7-唯一入口与文档维护)，离线结构检查为`python -m src.tools.project_docs`，不能替代业务/运行验收。不能从本文件推断已授权修复、实现或调用外部服务。Pi核心基底的当前方向见 [D-010](decisions.md#d-010pi作为核心agent基底)，具体接入设计及开发边界以STATUS和对应方案为准。

以下四节保留 **2026-11-15 的 Memory 模块历史快照**，不是整项目当前状态。旧结果不自动覆盖新候选；当前检查以 STATUS 的绑定证据为准。

## 已实现

- 已建立 Harness Memory 入口、长期理解、决策演进和当前状态文件，并在根 `AGENTS.md` 增加启动、使用和沉淀规则。
  - 依据：[`AGENTS.md`](../../AGENTS.md)、本目录 [`README.md`](README.md)
  - 有效范围：本仓库的 Agent 协作和文档治理
  - 日期：2026-11-15
- 已加入无外部依赖的结构校验命令 `python -m src.memory.validate`。
  - 依据：[`src/memory/validate.py`](../../src/memory/validate.py)
  - 有效范围：Memory 文件的确定性结构检查，不代表语义或业务验收
  - 日期：2026-11-15

## 已验证

- 校验器能够检查必需文件、标题、状态枚举、来源字段和本地 Markdown 链接，并在当前 Memory 内容上通过。
  - 依据：命令 `python -m src.memory.validate` 的本次运行输出，见本文件“验证记录”
  - 有效范围：当前工作区的 Memory 文档结构
  - 日期：2026-11-15
- 既有 Verify 入口 `python verify.py --task harness-memory --output outputs/verify/harness-memory/report.json` 通过，结构校验和 Python 编译检查均为 `PASS`。
  - 依据：`outputs/verify/harness-memory/report.json`（本地忽略产物）及 `.verify/tasks/harness-memory.json`
  - 有效范围：本次 Memory 变更声明的文件范围和确定性检查
  - 日期：2026-11-15
- 在临时隔离副本中只保留 `AGENTS.md`、项目概览、相关 SPEC/PLAN/架构来源、设计文档、Memory 文件和校验器，仍能通过校验并找到首期目标与“Memory 不是规范、状态可能未验证”的边界说明。
  - 依据：临时目录中的 `python -m src.memory.validate --root <temp-root>` 输出及关键材料断言
  - 有效范围：持久化材料的最小集合可被单独读取；不证明新 Agent 的语义理解或真实跨会话运行
  - 日期：2026-11-15

## 未验证

- 尚未启动真正独立于当前对话的新 Agent 会话；冷启动结论目前只由仓库材料和受控读取演练支持。
  - 依据：本次任务执行边界；未发现独立会话运行记录
  - 有效范围：跨会话恢复的真实运行体验
  - 日期：2026-11-15
- 尚未将 Memory 接入实际应用运行时或自动提交钩子；启动和更新依赖 Agent 遵循 `AGENTS.md`。
  - 依据：[`README.md`](../../README.md)、当前 `src/` 目录
  - 有效范围：自动化接入、运行时强制性和多 Agent 并发更新
  - 日期：2026-11-15

## 阻塞

- 当前无影响本模块落地的已知外部阻塞。
  - 依据：本次仓库内实现与验证未依赖外部服务
  - 有效范围：Memory 文件和校验器
  - 日期：2026-11-15

## 验证记录（2026-11-15 历史）

2026-11-15 记录的执行结果如下，保留历史范围，不代表本次重跑：

```text
python -m src.memory.validate
```

结果：通过。输出为 `Memory validation passed: 4 files, required fields, statuses, and local links checked`。

```text
python verify.py --task harness-memory --output outputs/verify/harness-memory/report.json
```

结果：通过。`memory-structure`、`memory-compile` 和验收项 `A1` 均为 `PASS`。

另以临时仓库副本把三个决策状态改为非法值，运行 `python -m src.memory.validate --root <temp-root>`；结果按预期以退出码 1 拒绝并报告 3 个状态错误。该负例证明结构校验能阻止未知状态被当作有效决策，但不证明记忆内容的语义正确性。

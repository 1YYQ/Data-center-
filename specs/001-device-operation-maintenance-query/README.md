# 首期设备操作维护查询

本目录是首期需求、计划、契约、任务和验收场景的维护入口。当前进度与候选证据统一见 [项目状态](../../doc/planning/STATUS.md)，具体执行见 [tasks.md](tasks.md)；`doc/planning/first-release-device-query/` 仅保留兼容链接。

2026-11-18开发准备入口：[分阶段基线](baseline.md)、[FR/SC—任务—验收—证据映射](traceability.md)、[映射源JSON](traceability.json)、[任务直接依赖与能力分段](tasks.md#dependencies)。它们组织现有需求和证据，不新增任务编号、不产生真实签认/外发/部署授权。

实现位于 `src/server/` 和 `src/web/`，配置边界见 [operations.md](operations.md)。配置项、接口定义或代码存在不能证明正式数据库、独立任务进程、外部 provider 或浏览器任务已通过验收。

文档所有权与开工检查唯一见[baseline维护规则](baseline.md#7-唯一入口与文档维护)；当前动作从[tasks顶部](tasks.md#当前调度与本次审查preflight-001)读取。追踪JSON是源，Markdown由`python -m src.tools.project_docs --write-traceability`生成；普通检查使用不带参数的同一命令。

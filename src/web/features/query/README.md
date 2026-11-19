# 查询界面

正式 React/Vite 入口为 `/workspace/query`，支持查询受理、缺型号补充、原任务轮询/刷新恢复、结果与警告、鉴权原文定位。GET 只读；任务由显式 Worker 执行，Pi 路径需要已构建的 Node 运行时与受控配置。页面恢复同一受理任务，不因刷新新建查询。

构建和统一请求边界见[工作区说明](../../README.md)。`src/server/static/` 原参考页保留用于旧回归；旧 Chrome 证据不自动覆盖 React 页面。新运行证据由[项目状态](../../../../doc/planning/STATUS.md)集中引用，不等于真实三角色 A–F 验收。

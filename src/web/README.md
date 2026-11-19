# React 工作区

正式 React/TypeScript/Vite 源码。生产构建基础路径为 `/workspace/`，由 FastAPI 同源提供：

- `/workspace/query`：查询受理、缺型号补充、原任务轮询与刷新恢复、结果/警告/鉴权原文链接。
- `/workspace/manage`：现有文档新版本 PDF 上传、本地解析请求与真实批次状态；精确批次逐块核验、专业确认与核验集；真实确认登记、材料上传/关联/核查；参考开放、发布/撤回预检、回执和本地关键词索引。
- `/workspace/access-review`：系统外审批事实登记、材料上传/关联、匹配职责核查及外部撤销登记。
- `/workspace/services`：具备 `system.configure` 的系统管理员可配置生成、向量和解析服务的本地测试修订，运行固定合成自检并预检启停。当前企业准入未配置，业务派发关闭。

沿用原参考页的信息层级与样式：资料和证据优先，表单按维护步骤组织，中文正文与定位可读；浅色底、蓝色操作和琥珀警告保持一致。使用原生表单控件，审批登记对话框使用 Ant Design Modal 处理焦点、键盘和遮罩行为。工作区链接随当前角色或配置能力显示，受保护的操作仍由服务端鉴权。完整 K/S 工作区设计与验收见 [项目状态](../../doc/planning/STATUS.md)。

从仓库根目录运行（Node 22.20.0 或符合 engines 的更高版本）：

```powershell
npm --prefix src/web ci --cache outputs/npm-cache
npm --prefix src/web run typecheck
npm --prefix src/web test
npm --prefix src/web run build
```

开发服务器只绑定回环，代理到 `127.0.0.1:8000` 的现有 API；启动前必须按后端说明显式选择和初始化获准的隔离环境。前端构建不连接数据库、不初始化账号、不持有模型或服务密钥。生产不使用 Vite 开发服务器。`dist/`、`node_modules/` 和 `outputs/` 均忽略，不提交业务资料。

依赖精确锁定于 `package-lock.json`。React/DOM 19.2.0、TypeScript 5.9.3、Vite 7.3.8、Ant Design 6.7.1；Vite 的 Node 约束为 `^20.19.0 || >=22.12.0`，Ant Design 6 的 React/DOM peer 为 `>=18.0.0`。约束核对依据安装包元数据与 [Vite 官方说明](https://vite.dev/guide/)、[React TypeScript 文档](https://react.dev/learn/typescript)、[Ant Design 官方说明](https://ant.design/docs/react/introduce/)。

`api.ts` 统一同源 Cookie、CSRF、no-store、HTTP 错误与会话 epoch。401/403/404 清除页面受保护投影；旧请求结果不能在新身份下回填。变更命令按账号/方法/路径保留原幂等键、输入摘要及 If-Match；未知结果不静默重发，刷新后可显式恢复原 JSON 操作回执，文件上传须选择相同文件和来源重试。未知后再遇拒绝不会推断原变更未执行，也不会换新键。恢复仍由服务端复核当前权限。浏览器 tab 存储保存登录 CSRF、自己的任务恢复条件及待核对命令，不缓存结果正文。

原生 Node 测试只验证客户端 HTTP/存储边界，包括未知结果重放、拒绝后的键保留、CSRF、ETag 与身份切换 fence；不替代浏览器或业务验收。页面和后端边界以[项目状态](../../doc/planning/STATUS.md)为准；源码和构建不能证明真实三角色 A–F、真实岗位签认、复杂 PDF 或外部服务准入完成。

新版本上传 API 接收版本标识、型号和 PDF；业务依据页以独立来源 GET/PATCH 登记真实来源渠道、说明及可选接收时间/单位/定位，未知时间保持空。缺少来源渠道/说明时，发布预检会如实阻断。新知识库创建仅保存本人登记元数据，等待按既有审批配置管理范围，不自动授予权限。新文档 PDF 上传仅在真实知识库级管理范围内开放，来源随草稿登记，分类和业务确认仍需后续核对。生命周期入口通过独立元数据 API 查看在用资料/回收站、版本保护性暂停与真实消解依据、逻辑删除影响和治理历史。删除须理由与确认，采用文档 ETag 和稳定幂等；删除成功清除原选文档投影，回收站不调用原文、普通版本和确认接口。原件按策略保留不证明物理完整性，未配置保留期，永久清理和恢复未开放。真实外部解析及完整企业生命周期验收仍保留边界。管理页刷新保留所选精确版本；没有选定版本时优先当前发布版，上传后只根据服务端返回 ID 选中新草稿。


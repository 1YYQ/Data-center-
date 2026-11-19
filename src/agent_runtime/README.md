# Pi 本地受控宿主与探针

固定`@earendil-works/pi-coding-agent` / `pi-ai` **1.5.2**，源码候选`b4afdddf44e059c67ccf5097e5317d913521b470`，发布包许可MIT；锁文件保存制品完整性，不据同版本号声称源码构建字节一致。当前真实SDK已由Python Worker连接本地FastAPI模型关口及查询工具；上游固定为回环合成服务，无真实provider配置入口。

在本目录运行，要求Node >=22.19.0、npm及仓库Python依赖；`PI00_PYTHON`可指定探针Python解释器。已安装依赖时直接构建/测试，无需重复安装。

```powershell
npm run typecheck
npm run build
npm test
```

首次公共依赖安装可用`npm ci --ignore-scripts`，不会执行安装脚本。当前环境Node22.20.0/npm10.9.3，SDK探针8项运行检查通过；Worker和浏览器的独立证据、完整命令见[本地执行说明](../../doc/development/本地查询执行与验证.md)及[STATUS](../../doc/planning/STATUS.md)。

| 文件 | 作用与边界 |
| --- | --- |
| `src/pi00-session.ts` | 共用真实SDK构建器；显式工具/资源/内存设置，不发现用户全局配置，无编码默认工具；保留原finishTurn，工具批次等待/受理/交付后停止。探针与本地受控provider分别准入 |
| `src/controlled-provider.ts` | 固定原生openai-completions adapter；使用服务端实际投影，忽略SDK旧历史和任意transport/payload覆写；正文hash绑定许可后一次派发，禁止重定向/自动重试 |
| `src/runtime-host.ts` | 有界stdio JSON-lines，可信父进程提供run配置；仅启用8项查询工具。实际tool.invoke走Python账本，SDK settled不代表业务完成 |
| `src/pi00-wire.ts`、`test/` | 早期独立协议探针：前5项官方faux响应，后3项真实本地HTTP覆盖主回答/压缩/摘要、篡改/重放/撤销、失败不重试；进程内许可夹具不充当产品持久账本 |
| `../server/worker.py` | 显式本地outbox消费、步骤/会话双租约、Node进程/心跳、受控工具回调、私有日志快照与恢复；业务结果须通过T14/T16才可交付 |

受控宿主仍是本地查询参考实现。目标Celery/RabbitMQ、PostgreSQL、正式前端/管理流程、完整20工具/四层、真实模型、OS级出口隔离及留存/恢复尚未完整验证。主/摘要/压缩共享许可协议，但完整业务辅助流程不能从探针8项推导通过。真实三角色A–F、复杂手册和人工参考答案未验收，不能宣称首期闭环完成。

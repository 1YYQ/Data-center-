# Verify

`verify.py` 是 SPEC → PLAN → IMPLEMENT 之后的只读验收入口。每个任务在
`.verify/tasks/<task-id>.json` 声明治理文件、SPEC、PLAN、Contract、变更范围、
验收条件和项目已有检查。验证器读取并哈希这些来源，分析 Git 变更，优先复用
清单中状态为 `PASS` 且源码哈希仍匹配的证据；需要时执行任务契约中的命令。

```powershell
python verify.py --task T001-optics --output outputs/verify/T001-optics/report.json
python verify.py --task verify-module
python verify.py --task T001-optics-failure
```

退出码 `0` 只表示 `PASS`；`FAIL`、`BLOCKED` 和 `INCONCLUSIVE` 均为非零。
验证器不会修改实现或任务标准。项目命令可能按其自身契约生成测试产物，因此
需要严格只读审查时使用 `--check-only` 或只配置可复用 evidence 检查。

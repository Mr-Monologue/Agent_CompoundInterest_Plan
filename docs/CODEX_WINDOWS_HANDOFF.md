# Windows Codex 一次性交接

用户已选择 Windows 本机 Codex 作为开发与日常投资的统一入口，目的是省去 Hermes
与 Codex 之间反复转述。先实现现有 Core 的直接接入，再单独处理后台调度迁移。

## 交接包

Git bundle 包含长期 `feature` 上尚未发布的提交：既有 v0.31.8 零投入周能力，以及本次
Codex 接入与回归修复。基线为 `a7f9ffe6c10489ffefe33c834adf035a055f3657`。
不要把本包视为正式 Release；不包含数据库、Token 或生产配置。

接收后先核对当前仓库为 `Mr-Monologue/Agent_CompoundInterest_Plan`，读取 `AGENTS.md`。
检查本地修改、四条长期分支及远端状态，保留所有已有工作。用 `git bundle verify` 校验
附件，再从 bundle 获取 `refs/heads/feature` 到 `FETCH_HEAD`，核对提交差异与祖先关系。
缺少前置提交时先获取远端基线。只在确认无冲突和不丢失改动后，将缺少的提交接入长期
`feature`；不得强推、重写已发布历史或自动重置工作树。

## 本机要完成的工作

1. 阅读 `docs/CODEX_WINDOWS.md`，在独立开发检出使用锁定的 Python 3.11 依赖运行测试、
   Ruff、mypy、构建及 PowerShell 语法检查。重点验证实际 Windows STDIO 子进程。
2. 只读定位现有运行目录、Core 版本、`/ready`、托管任务和当前 Codex 配置。
   不初始化新账本，不把测试数据写入生产，不把尚未连接说成连接成功。
3. 核验发布权限和 CI；按 `feature -> develop -> release -> main` 与 Tag 流程发布。
   上一轮云端源码推送曾被自动审批拒绝，原因是要求对推送再明确授权；本包只是本地
   工作的交接，不代表绕过审批或宣称已发布。遵循当前环境的授权与审批结果。
4. 明确目标版本并取得相应生产升级确认后，使用已有备份、迁移及回滚流程安装，传入
   `-SkipHermes`；检查更新任务保留该选项。不要直接在正式安装目录开发或执行测试。
5. 在已升级运行目录运行 `connect-codex-windows.ps1 -CheckOnly`，通过后注册 Codex MCP。
   脚本使用当前用户的 Codex CLI，备份已有配置，不修改其他用户的设置。
6. 重启本机 Codex 会话，检查 MCP 实际可用，读取默认组合与工作台，确认仍为原有事实。
   首次业务验收只读；用户未确认时，不冻结计划、提交申购、记账或更改策略。

## 已完成验证（Linux / Python 3.11.16）

- 新接入与已有 MCP/Windows 契约相关测试：36 项通过。
- 全套 pytest：初次 274 通过、2 失败。两项失败分别是迁移版本断言误写为两个值，及
  文档安全规则断言没有处理换行；仅修正断言后 `pytest --lf` 两项均通过。
- Ruff 通过；mypy 检查 35 个源文件通过；wheel/sdist 构建通过。
- 真实 MCP STDIO 握手使用临时假 Core，覆盖成功、未就绪、版本不匹配；只产生
  `/health` 和 `/ready` 的 GET 请求，没有创建数据库或投资事实。
- 尚未完成 Windows 实机安装、Codex CLI 注册、生产升级及双平台 GitHub CI。

## 保留的范围边界

连接 Codex 不自动迁移 Hermes Cron，不卸载 Hermes，不停旧任务，不重复创建新调度。
生产数据与草稿/明确确认/提交规则保持原样；开发授权不等于交易事实写入授权。
完成接入后，后续查询、业务操作和故障排查由本机 Codex 直接处理，无需继续中转。

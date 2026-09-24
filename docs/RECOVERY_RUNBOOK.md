# 升级失败恢复：代码兼容与快照权限分开

适用范围：现有 Windows 安装与权限。不新增服务或运维平台。普通修复沿当前有效阶段授权；阶段结束后须有新的具体授权。

## 可以验证什么

自动代码回退只接受一个窄范围：旧快照代码与当前安装的全部 `src` 文件相同（仅版本标签可变），项目配置与锁定依赖语义一致，数据库结构及迁移版本一致，当前库完整性/外键检查通过，旧代码在当前库隔离副本上 doctor 为 PASS。版本文件中任何其他代码变化仍阻断。当前实现的版本号只用于元数据，不能借此允许未来按版本切换投资规则。

数据内容不要求与快照相同：新的备份审计、申购、交易或其他事实保留在当前库中。检查结果会分别返回代码兼容结论、当前数据是否与快照不同，以及快照恢复需另行评审。兼容 PASS 不是快照恢复授权，即使内容相同也不自动授予。

未知业务代码/依赖差异、新增表/索引、仅改数据的迁移版本、完整性失败、旧 doctor 失败均阻断。这里没有“新增迁移默认兼容”规则，也没有全表豁免。

## 失败后按顺序处理

1. 保留当前数据库及 `-wal`/`-shm`，不要复制旧库、删除边车或运行 downgrade。读取安装目录 `data/update-state.json` 与 `logs/updater.log`：确认安装是否开始、Core 是否停止、兼容阻断原因及已验证快照路径。记录实际状态，不把来源字段或已保存快照当成恢复成功。
2. 先为当前状态生成新的独立备份。若当前 CLI 可用，在安装目录运行下面的 backup；若 CLI/环境不能启动，停止恢复并修复执行环境或安排受审查的一致性备份，不拿旧快照替代当前现场。记录新备份可读检查及哈希，不输出 `.env`、令牌或配置内容。
3. 对现有快照运行完整性验证，再运行兼容检查。后者只在私有快照目录创建临时库，旧代码诊断不会打开生产库写入；源数据库仅只读技术检查，不作为投资查询接口。
4. **兼容已验证：**现有升级器的失败分支只恢复代码和原运行配置、依赖及任务定义，保留当前数据库；不运行数据库迁移。重启后必须验证旧版健康/就绪、业务 HTTP、调度心跳和事实不变性，才记恢复成功。
5. **兼容阻断：**保留现场，Core 停止状态明确显示。根据日志中列出的源码/依赖文件、结构或迁移差异，优先在开发仓库修复后前滚到经过双平台 CI 的版本，使用现有升级器安装并核验。需要旧代码时，先在当前库副本建立该版本的明确兼容证据并审查差异，不能删除检查绕过阻断。
6. **确需恢复旧数据库：**另行列出将失去的全部新增事实、来源与核对方法，先保存当前一致性备份，再获得具体恢复确认。无法证明不会覆盖新事实时不得操作。旧代码能读当前库不是恢复旧库的理由。

维护者命令示例（安装路径需先核实；标识由维护者保存，不要求用户复制）：

```powershell
$InstallRoot = 'C:\investor\value-dca-agent'
$State = Get-Content "$InstallRoot\data\update-state.json" -Raw | ConvertFrom-Json
. "$InstallRoot\runtime\windows\update-safety.ps1"
Assert-RecoverySnapshot $State.rollback_snapshot "$InstallRoot\.venv\Scripts\python.exe"
# 检查应使用当前已审查版本的 helper，旧版本 helper 的签名不同。
& "$InstallRoot\.venv\Scripts\python.exe" "$InstallRoot\runtime\windows\rollback-preflight.py" $State.rollback_snapshot "$InstallRoot\data\investor.db" $InstallRoot
# 先选择新的备份文件名；不要覆盖已有备份。
Push-Location $InstallRoot
try { & .\.venv\Scripts\investor.exe db backup --output 'backups\recovery-current-UNIQUE.db' }
finally { Pop-Location }
```

修复版发布且备份验证后，用已审查的新版 `runtime/windows/update-value-dca.ps1 -InstallDir <安装目录> -SkipHermes -AllowManualUpdate` 前滚。不要直接调用旧版升级器的数据库覆盖分支，不通过故意制造生产失败来触发回退。

## 验收边界

自动化测试覆盖审计/新事实/WAL保留、未知源码/依赖/结构/迁移阻断、旧代码诊断与版本文件代码漂移。真实备份副本隔离诊断不等于生产回滚演练。生产本轮仅正常升级和查询/预览验收；无实际灾难恢复。

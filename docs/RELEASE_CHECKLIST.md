# Release Checklist

## 本地验证

- [ ] 版本号、运行时版本、锁文件和 `release-manifest.json` 一致。
- [ ] 数据库迁移可从上一正式版本升级，空状态正常，既有事实不变。
- [ ] Ruff、mypy、完整 pytest 通过。
- [ ] 受 Git 管理的 PowerShell 脚本解析通过。
- [ ] wheel 和 sdist 构建通过。
- [ ] 工作树不包含密钥、Token、数据库、生产日志或个人投资事实。

## GitHub 治理

- [ ] `feature -> develop` PR 的 Ubuntu、Windows CI 通过并合并。
- [ ] `develop -> release` PR 的 Ubuntu、Windows CI 通过并合并。
- [ ] Tag 精确指向 release 提交，GitHub Release 已创建。
- [ ] `release -> main` PR 的 Ubuntu、Windows CI 通过并合并。
- [ ] develop、release、main 和 Tag 的 Git tree 一致。
- [ ] 工作树干净且没有未推送提交。

## 生产边界

- [ ] GitHub 发布完成后停止。
- [ ] 获得当前版本的 Windows/Hermes 生产升级授权后才执行升级。
- [ ] 升级后只读验证 `/ready`、schema、Hermes 工具、持仓、交易、计划和策略未被迁移自动改变。

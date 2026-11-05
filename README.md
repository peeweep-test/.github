# .github

The github tools

## 同步模板文件

同步模板文件到组织的所有仓库中, 可以修改 repos 目录下的配置文件, [文档](https://github.com/linuxdeepin/action-sync)

## 开关仓库功能

开启或关闭仓库功能, 如 wiki/project/issue, 可以修改 organization.yaml, [文档](https://github.com/linuxdeepin/action-organization-manager)

## 团队

添加成员, 添加关联的仓库、项目并修改权限,可以修改 teams.yaml, [文档](https://github.com/linuxdeepin/action-teams-manager)

## 静态代码检查

开启或关闭仓库功能, 如 wiki/project/issue, 可以修改 organization.yaml, [文档](https://github.com/linuxdeepin/action-cppcheck)

## 工作流模板

### backup-to-gitlab

备份仓库到gitee和内网的gitlab

### call-build-deb

编译构建检查

### call-chatOps

合并机器人

### call-commitlint

commit信息检查

### cppcheck

cpp代码静态检查

### call-verify-group-version

校验 DDE 组成员 PR 的 `debian/changelog` 版本。普通代码 PR 不改版本时通过；改版 PR 须匹配
同组织 `dde` 仓库的原始发布记录，包括组目标版本、成员仓库、自动化分支、目标分支和 PR 创建时间。
首轮成员起始版本可以不同，目标仍须高于各成员当前版本。

成员使用 `workflow-templates/call-verify-group-version.yml`，通过 `pull_request_target` 调用
`verify-group-version.yml`。工作流只读取 PR 文件，不检出或执行 PR 代码；使用本仓库的
`GITHUB_TOKEN` 把 `group-version-guard` 状态写到 PR 的 head 提交，不需要 GitHub App Secret。

`repos/linuxdeepin/*.json` 已为 36 个组成员配置此模板。上线前先完成 DDE 发布协调器和
成员首用准备，包括 go-lib 的发布入口和 dde-clipboard 的包名、epoch 迁移，再合入本工作流及模板配置。
默认分支上的入口文件接入后，给所有成员发布目标分支
启用 `group-version-guard` 必需检查，来源选择 GitHub Actions；同时要求 PR、分支保持最新，
禁止直接推送、强推和绕过。工作流本身不能阻止未受保护分支上的直接推送。

现有模板同步通过 SSH 直接推送。成员启用上述保护后，后续模板更新需要通过 PR 合入，
不可给模板同步或发布机器人设置绕过来继续直接推送。

本地验证：`python3 -I -B .github/tests/test_verify_group_version.py -v`。

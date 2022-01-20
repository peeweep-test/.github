# 说明

这个仓库用于提供组织的 Github Action 模板，并且在模板变化后同步文件到其他仓库

# 同步配置

在 repos 中创建任意文件，文件内容格式为 JSON,例子如下

```json
[
  {
    "src": "workflow-templates/check.yml",
    "dest": "peeweep-test/test-action/.github/workflows/check.yml"
  }
]
```

以上配置将 workflow-templates/check.yml 同步到 peeweep-test/test-action 仓库的 .github/workflows 目录下。

虽然配置可写在 repos 目录下任意位置，但为了便于维护，建议以仓库路径为文件名

例如同步到 peeweep-test/test-action 仓库的配置文件建议放置到 repos/peeweep-test/test-action.json

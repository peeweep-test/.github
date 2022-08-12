# GitHub Action 集成

## 说明

这个仓库用于提供组织的 Github Action 模板，并且在模板变化后同步到其他仓库

## 目录结构

```txt
├── .github
│   └── workflows
│       ├── teams-check.yml 自动触发的Action，用于在发起的PR 或push 中, 配置文件或Action模板变动时，检查yaml 的合法性
│       ├── teams-update.yml 自动触发的Action，用于在配置文件或Action模板变动时，自动修改Teams 成员
│       ├── jenkins-bridge.yml 可重用的Action文件，在check.yml中引用
│       └── sync.yml  自动触发的Action，用于在配置文件或Action模板变动时，自动同步到其他仓库中
├── go.mod
├── go.sum
├── main.go
├── README.md
├── teams.yaml 配置Teams
├── repos 同步配置，目录下文件发生变动，读取变动的配置，并触发该配置的变动
│   └── peeweep-test
│       └── test-action.json
└── workflow-templates  Action模板，在GitHub添加Action时可选择，修改目录下文件，会触发所有配置同步
    ├── check.properties.json
    └── check.yml
```

## 同步配置

在 repos 中创建任意文件，文件内容格式为 JSON,例子如下

```json
[
  {
    "src": "workflow-templates/check.yml",
    "dest": "peeweep-test/test-action/.github/workflows/check.yml",
    // 可选项，默认同步文件到所有分支
    "brache": ["main"]
  }
]
```

以上配置将 workflow-templates/check.yml 同步到 peeweep-test/test-action 仓库的 .github/workflows 目录下。

虽然配置可写在 repos 目录下任意位置，但为了便于维护，建议以仓库路径为文件名

例如同步到 peeweep-test/test-action 仓库的配置文件建议放置到 repos/peeweep-test/test-action.json

## Teams 配置

```
teams:
  admin: # team 名
    members: # team 成员
      - peeweep
  dtkcore:
    members:
      - peeweep
  reviewer:
    members:
      - myml
      - peeweep
```

成员在team 中的权限默认为 member

如果成员同时是组织的maintainer, 会自动升级为maintainer

## TODO

- [x] 同步流程手动触发会无权限同步文件


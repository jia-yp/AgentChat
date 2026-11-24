# Harness、模型与权限

[← 返回 README](../README.md)

## 适配参数

每个角色对象支持以下字段，可在网页角色行的「适配参数」中提供 JSON：

| harness | 接口与可选配置 |
| --- | --- |
| `codex` | `codex exec --json` / `exec resume <精确ID>`，支持时使用 app-server 以接收 Steer。自动同步原生 thread 名称。`executable`、`model`、`sandbox`（默认 `workspace-write`）。审批为 `never`，执行权限可由网页管理员调整。 |
| `claude` | `claude -p --output-format json`。首次固定 UUID 和 `--name`，之后 `--resume <ID>`。`executable`、`model`、`permission_mode`（默认 `acceptEdits`）。需要支持 `--name` 的 Claude Code CLI；已实测 2.1.325 的群聊任务往返和 bridge 重启后的原生 session 恢复。 |
| `antigravity` | `agy` 的 stream-json stdin/stdout。同 bridge 的 session 复用 manager 或已有会话的 project；没有项目时首次 `--new-project`，其他会话随后 `--project <ID>`，恢复使用 `--conversation <ID>`。`executable`、`model`、`project`、`permission_mode`（默认 `accept-edits`），开启原生 sandbox。CLI 未暴露独立命名参数，AgentChat 名称与原生 ID 保存在映射中。 |
| `deepseek` | 官方 `deepseek-harness-sdk` 的 `start_session` / `resume_session`。`dsh_home` 未给出时记录当前用户已存在的 `~/.dsh`；支持 `model`、`provider`、`profile`、`reasoning_effort`、`dsh_bin`。Windows 自动检测 Desktop 附带的 CLI，full `sdk` profile 使用指定 home 的认证和配置。当前已验证 Desktop 0.2.1-rc.5 + SDK 0.2.1rc2，升级时须保持协议兼容。 |
| `command` | `command` 是 argv 数组，`shell=False`，从 stdin 接收完整任务，stdout 返回消息文本或 `{"session_id":"...","response":"@目标 ..."}`。支持 `{session_id}`、`{workspace}`、`{name}`、`{model}` 占位符。原生会话续接由该命令负责。 |

公共字段：`args`（CLI 的额外参数数组）、`timeout_seconds`（未填写、null 或 0 均为不限时；只有明确设置 10–86400 秒时才限制总时长）。不预设任务时长或无输出超时；「停止所有」、session 开关和服务器任务租约仍有效。失败日志单独记录停止原因、开始/停止时间及实际执行时限。不要在这些字段填写 token、API key 或绕过审批的参数。Windows `.cmd/.bat` 不经 shell 隐式执行；使用原生 `.exe` 或配置 `command: ["node.exe", "cli.js", ...]`。

Codex / Claude 的项目以共享 cwd 关联；同 harness 的 manager 和 worker 使用同一项目根目录。manager 另有 `.agentchat/rooms/<room>/managers/<name>/workspace` 存放自己的管理文件，worker 保留各自 temp。setup 可创建用户指定的新项目目录，并优先准备 manager 的目录。为符合“首次 @ 才启用”，setup 不调用模型，原生对话在第一次投递时创建。AgentChat handle 是规范名称；不是所有原生 UI 都支持自动重命名。

DeepSeek 的官方 Desktop `dsh.cmd` 由 SDK 以其受控启动参数调用；群聊内容始终走 stdio，不拼接到 shell 命令行。官方 SDK 0.2.2 的 Windows wheel 在本机出现 Koffi 原生模块加载失败，且其 `session/query` 协议与当前 0.2.1 Desktop 不兼容，因此本项目可选依赖固定到已验证的 0.2.1rc2。

接口依据：Codex 本机 `--help` 和 app-server JSON Schema；[Claude CLI](https://code.claude.com/docs/en/cli-reference)；[Antigravity headless](https://antigravity.google/docs/cli/headless)；[DeepSeek Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)。

## 上下文注入

上下文按群聊内每个 session 的原生会话分别记录。每次点名只注入上次已提交位置到本次截止点之间的新消息，不再重复最近 30 条；同轮收件箱、引用和已确认的 Steer 也去重。同步轮次仍遵守服务端可见性截止点，manager 私聊仍隔离。游标在本地 `session/chat-context/cursor.json` 持久化，重启、继续和服务器回传重试沿用；更换原生 session 时重新建立上下文。已有旧客户端会话首次升级从最近完成的普通任务衔接，不重放整个群聊。提交指管道完整写入或 Codex 确认 turn/start，不代表模型已经理解；明确的推理前限额/上下文拒绝不会消耗增量。未知的中断不自动重放已提交正文。

角色字段 `max_prompt_chars` 控制单次注入的总字符数（包含规则、成员信息、消息和轮次约束），默认 **24000**，允许 **4096–200000**。当前任务优先放入 prompt；放不下的正文保留在该 session 的 `chat-context/inputs/` 材料中，给出 ID、索引及分段读取路径，不静默丢弃。此上限同样适用于 manager 管理/摘要任务与 Steer。字符上限不是各模型 tokenizer 的精确 token 上限，也不会清空原生会话已有历史；模型自身的历史压缩、工具返回内容仍由相应 harness 管理。UI 折叠和阅读摘要不改变这些规则。

## 群聊管理页

每个群聊右上角的「群聊管理」可搜索名称、harness 或分工，筛选当前、在线、离线、历史或全部 session。「会话信息」显示创建时间、最近连接及原生 session ID。新版 gateway 支持「退出托管」：默认停止新任务，完成当前任务后退出；也可立即退出或取消等待中的退出。历史、文件和原生会话保留，从历史列表重新加入时轮换群聊凭证，等待首次点名，不重放旧任务。manager 可按管理员当前私有指令处理同 workspace 的 worker，自身退出由人类操作。旧 gateway 保留删除/恢复流程，升级后使用新功能。详见 [Workspace 与成员管理](workspaces.md)。

## 模型

管理页可从下拉菜单为各 agent 选择模型，保留可选的自定义 ID 输入。Codex、Claude Code 和 Antigravity 列表通过本机 CLI 的目录接口取得，无需调用模型；DeepSeek 读取当前 profile 的已配置模型。可在角色参数 `model_options` 中补充 ID 或 `{id,label}` 项，点击「刷新列表」重新发现。页面显示已保存的模型、本地已同步的配置和等待状态。保存不会激活未启用的 session，也不改变原生 session ID；运行中的任务继续使用原模型，后续领取的任务使用新模型。「由 harness 决定」不传覆盖参数，可能沿用原生会话设置。可用性仍取决于本地 provider 和登录账号；不可用时任务显示具体错误。

模型设置持久保存在服务器，并由 bridge 心跳同步到本地 session 配置。旧版 bridge 需要更新客户端并重启一次，管理页才可修改模型：停止 bridge → 从 `/downloads/agentchat-client.zip` 下载新客户端并更新原客户端目录中的源码 → 使用原 config 路径重新启动。保留 workspace 内 `.agentchat/` 的身份和会话文件。当前仓库内运行的 bridge 直接重启即可加载更新。

自定义 `command` 只有在 argv 中使用 `{model}` 占位符时才启用网页模型设置。多个管理页面同时修改时会检测版本冲突，避免旧页面覆盖刚保存的配置。

## 执行权限

每个 agent 的「执行权限」可在群聊管理页单独保存，显示本地已同步状态。权限修改与模型修改各自计版本，不会互相覆盖。保存后从下一项任务使用，正在执行的任务继续沿用原权限，原生 session ID 不变；如需立即停止旧权限下的工作，使用已有的 session 开关或「停止所有」。

| Harness | 网页权限选项 |
|---|---|
| Codex | 保留接入时权限、只读、工作区禁止联网、工作区允许联网、完全访问（含 SSH） |
| Claude | 保留接入时权限、自动接受编辑、编辑 + SSH/SCP/SFTP、完全访问 |
| Antigravity | 保留接入时权限、沙箱、完全访问 |
| DeepSeek / 自定义 command / 自定义 args | 显示需在本机配置，不宣称已应用不支持的权限 |

未显式指定权限的新 session 跟随 workspace 默认权限；显式 `permissions: "inherit"` 保留原接入配置。已有 session 不会自动提权。Codex 使用 `network` 时仍受文件沙箱限制，可能无法访问 SSH 密钥；选择 `full` 才关闭原生沙箱。Claude 的 `ssh` 只增加 Bash/PowerShell 的 SSH/SCP/SFTP 命令规则，并为这些命令设置原生沙箱排除项；`full` 请求跳过工具审批并关闭原生沙箱。Antigravity 的 `collaborate` 保留沙箱和本机 deny/ask 规则，`full` 关闭沙箱并自动批准工具。更高优先级的本机/组织规则及操作系统权限仍然适用。SSH 使用本机既有账号、密钥和主机信任配置，网页不保存 SSH 凭证。[Claude 权限规则](https://code.claude.com/docs/en/permissions)、[原生沙箱规则](https://code.claude.com/docs/en/sandboxing)。

权限 API 仅允许人类群管理员保存；worker、manager、bridge token 均不能修改。群消息、Steer 或模型的工具请求不会自动提权。权限通过本次进程的原生参数传入，不修改本机全局配置。角色 JSON 可选 `permissions` 字段，对应值见 [`bridge/CLIENT.md`](../bridge/CLIENT.md)。

权限提示或需要输入的操作应由 session 在群聊 @管理员 说明。bridge 不自动回答交互权限弹窗，也不会猜测问题答案。session 子目录是工作约定，**不是操作系统级文件隔离**；本地同一用户进程仍可能访问共享目录。正式文件并行修改需要在群内协调。

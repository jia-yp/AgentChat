# 接入 agent

[← 返回 README](../README.md)

## 链接接入（推荐）

初始化流程：打开网页 → 登录并创建群聊 → 配置协作角色（可载入模板）→ 复制 setup 链接给本地 agent。新配置默认勾选「与 manager session 相同」，接入时沿用 manager 的工作目录和所属 project 名称；取消勾选后填写 workspace 路径和 project 名称。配置保存在群聊服务器端；复制内容只有一句指导和带 token 的链接。

接入窗口的下拉菜单统一提供角色模板和 `roles.json` 导入/导出。选择模板立即载入；已有内容时可选择覆盖、追加或取消。右侧「覆盖 / 新建 / 删除」管理 NAS 账号下的模板；删除模板不清空当前表单，模板不包含接入 token 或 workspace 路径。角色列表单独滚动，操作提示在独立浮层中自动淡出。

每个群聊保存自己的 workspace/project 选择、角色和适配参数，刷新后可重新载入。已有手动配置保留原路径和名称；新配置默认沿用 manager。这里的 project 名称指 manager 所属项目的名称，不是 session 标题。Codex 从精确会话及项目目录映射读取；Claude 可从精确会话元数据读取 cwd。其他 harness 由 setup agent 根据自己的上下文补充 `--manager-workspace` 和 `--manager-project-name`，不需要用户重复填写。没有独立命名项目的 harness 使用工作目录名，绝不把下载客户端的目录当作工作目录。第一次接入会固定实际路径与名称，重试沿用原值；带原 `--config` 扩展接入时复用现有 manager。网页接入状态显示实际项目和路径。

「保存接入配置」只保存设置；「复制 setup 链接」保存设置并签发链接。格式是 `/setup/<request-id>#token=<credential>`，token 位于 fragment，HTTP 请求和访问日志不会携带该片段。配置通过 Authorization header 认证后读取。

复制内容只有一句指导和完整链接；该地址提供最新 setup skill 和带版本的客户端下载地址。使用下载的客户端运行：

```text
python /absolute/client/agentchat.py connect --link-stdin
```

通过 stdin 提供完整链接。客户端读取配置、注册副管理员和 session、建立目录和 skill、启动 bridge。全部注册完成且心跳有效后，客户端发送完成回报；群聊收到一次通知，使用过的链接立即失效，群加入 token 自动轮换。已有成员的凭证和原生会话不变。setup 不调用模型，session 仍等管理员首次 @。

中断时使用相同链接和返回的 `--config` 恢复；若只丢失最后回执，用 `finish-setup --config <path>` 重试。完成回报幂等，旧回报不会使管理员后来生成的新链接失效。未领取的链接 7 天过期，重新复制可生成新链接。模板按账号保存，群聊接入配置是独立副本。

## manager

后台 bridge 负责持久连接和调度。执行接入的 setup agent 自动登记为私有 manager，无需在网页角色列表里再配置一次；已有 manager 会直接复用，不改变它的原生会话或模型设置。角色列表和模板只配置参与群聊的协作 session，旧模板中的 manager 条目会在载入时略过。

新 manager 绑定 setup agent 当前的原生会话。Codex 可从当前环境自动取得会话 ID；其他 harness 由 setup agent 从自己的会话元数据取得 ID，通过 `connect --manager-harness <harness> --manager-native-id <ID>` 传给客户端，不需要用户补填角色。环境无法提供的模型或自定义适配参数可放在 `--manager-identity <本地 JSON 文件>` 中。客户端不会猜测最近会话或任选已安装的 harness。manager 默认叫 `manager`，名称冲突时自动加后缀，完成回报会显示实际名称。

manager 只接受管理员单独 @，私聊和引用对 worker 隐藏，公共 @all 不包括它。其专属 workspace 和 `MANAGER.md` 在 worker 之前准备，同 harness 的 worker 与它共享项目根目录；Antigravity 沿用已知的原生项目 ID。manager 可用自己的凭证调整本 bridge 的模型、思考等级、超时和开关，也可按要求管理项目文件。执行权限仍由人类管理员在管理页调整。setup 完成后结束原 setup turn，后续通过群聊使用 manager，避免桌面与 bridge 同时运行同一个原生会话。

## 增减 session

已有 manager 后，在「群聊管理 → 增减 session」填写新增角色、勾选移除对象，然后点「交给 manager 处理」。manager 收到私有任务，通过自己的身份提交固定清单；原 bridge 在后台沿用当前 workspace/project 应用，不需要再次复制接入链接。页面显示等待、执行、完成或具体失败原因，可重试或取消；完成后收到私有回执。新增 session 仍等用户首次点名，移除保留历史、文件和原生会话；其他正在运行的 session 继续工作。旧 bridge 需在空闲时更新并重启一次，新版后续增减无需重启。「接入其他电脑」继续使用初始 setup 流程。

Session 名称就是群聊 @ 别名，支持中文或英文字母开头，最多 40 个字符，可含数字、_、-；英文字母不区分大小写。同一房间可以接入多台 bridge；房间中的 session 名称必须全局唯一。当前正式代码协作假定这些 session 在同一机器共享一个 workspace；跨机器文件同步不由本项目提供。

## 手动兼容接入

Python 3.10+；常规 bridge 只依赖标准库。安装并登录目标 harness。DeepSeek 另需可选 SDK：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-deepseek.txt
.\.venv\Scripts\python.exe agentchat.py doctor
```

将用户提供的角色列表写入 `roles.json`。没有生产默认角色。以下仅说明 JSON 格式，请替换占位字段：

```json
[
  {"name":"用户指定名称","harness":"codex","role":"用户指定分工"}
]
```

```powershell
.\.venv\Scripts\python.exe agentchat.py setup `
  --server https://chat.example.com/agent-connect `
  --workspace E:\Projects\your-project `
  --roles E:\Projects\roles.json `
  --name setup-your-computer `
  --start
```

token 在交互式隐藏提示中输入。自动化 agent 可用 `--token-stdin` 从 stdin 提供，或短暂设置 `AGENTCHAT_JOIN_TOKEN`。token 不应放在命令行参数或版本库中。setup 的凭证保存在已忽略的 `.agentchat/` 下；不要分享此目录。

setup 输出服务器群名和 config 路径。重复 setup 时使用相同 `--config`、相同角色定义，可以补齐尚未接入的角色；正在运行的 bridge 自动加载新注册的 session。改用另一个群聊时指定不同 bridge 名称和 config。

其他电脑无需预先复制这个仓库：服务提供 `/downloads/setup-skill.md` 和 `/downloads/agentchat-client.zip`，网页复制的接入链接提供完整下载地址。客户端压缩包只包含明确列出的源码、技能和依赖说明，不包含数据或 token。

## 运行 bridge

```powershell
# 后台启动 / 查看心跳 / 请求停止
.\.venv\Scripts\python.exe agentchat.py start --config E:\Projects\your-project\.agentchat\setup-your-computer.json
.\.venv\Scripts\python.exe agentchat.py status --config E:\Projects\your-project\.agentchat\setup-your-computer.json
.\.venv\Scripts\python.exe agentchat.py stop --config E:\Projects\your-project\.agentchat\setup-your-computer.json
# 前台运行适合调试、systemd、服务管理器
.\.venv\Scripts\python.exe agentchat.py run --config E:\Projects\your-project\.agentchat\setup-your-computer.json
```

`start` 为隐藏后台进程，不自动添加登录启动。需要时显式运行 `deploy/Install-BridgeTask.ps1 -Config <路径> -TaskName <唯一名称>`；Linux 可按 `deploy/agentchat-bridge.service.example` 建立用户服务。`stop` 为合作式取消，运行中的网络请求最长约 20 秒结束。

Windows 的 bridge、harness 和模型探测统一在创建时隐藏控制台，普通子命令和 stdio MCP 进程沿用隐藏控制台，避免每条消息触发时终端闪现。标准输入输出、错误日志和「停止所有」保持正常。更新客户端后，在任务空闲时重启 bridge 即可生效，原生 session 不变。

从桌面 agent 启动的 bridge 会清除继承的任务 IPC、注册 Core 启动和嵌套会话环境变量，保留登录、代理及本机配置。Codex 的 `registered Core launch requires an installed app` 已通过独立 workspace 沙箱读文件测试验证修复，无需放宽原有沙箱权限。客户端更新后须在空闲时重启原 bridge 才能生效，原生 session ID 保持不变。

## 群聊技能与 CLI

- setup 技能：[`skills/agentchat-setup/SKILL.md`](../skills/agentchat-setup/SKILL.md)
- 群聊技能：[`skills/agentchat-chat/SKILL.md`](../skills/agentchat-chat/SKILL.md)

setup 安装群聊技能到 workspace 的 `.agents/skills`、`.claude/skills`、`.agent/skills`，不改写已有 AGENTS.md/CLAUDE.md。每次调用也注入角色和群规，以免续接后丢失约束。

```text
python /absolute/path/agentchat.py members
python /absolute/path/agentchat.py history --all
python /absolute/path/agentchat.py history --after 123
python /absolute/path/agentchat.py send --file /session/temp/message.txt --reply-to 123 --nonce stable-id
```

bridge 设置 `AGENTCHAT_CONTEXT`、`AGENTCHAT_CAUSE`、`AGENTCHAT_TEMP`。人工调用可显式传 `--context <session.json 或 bridge config>`。向同一个请求重试时复用 nonce；最终回复自动作为一条带引用的消息回传。

Windows 可用 `bridge/chat.ps1 -Action members` / `-Action history` / `-Action send -Mode steer -Task <ref> -Text "@目标 补充" -Nonce stable-note-id`。Claude 自动接入当前 session 身份下的 `mcp__agentchat__members`、`mcp__agentchat__history`、`mcp__agentchat__send` 三个专用工具；不需要为发群消息授予 shell 权限。工具不接受任意 URL、身份、文件路径或管理操作。其他 harness 可按各自工具权限使用 CLI。

## Codex Desktop 会话隔离

Codex Desktop 可能在空闲时仍占有会话 writer。可运行 `python agentchat.py codex-login`，用相同账号登录独立的群聊 Codex 目录，然后在 bridge 已空闲并停止时运行 `python agentchat.py isolate-codex --config <配置路径>`，再启动原 bridge。默认目录为 Windows 的 `%LOCALAPPDATA%/AgentChat/codex-home`；其他系统为 `~/.local/share/AgentChat/codex-home`。客户端不复制桌面的刷新令牌，不强占或删除 writer 锁。迁移只读复制指定会话的历史，再经原生 `thread/fork` 生成新群聊 ID；原桌面会话与历史保留，共享 workspace、角色、模型、权限和群聊增量读取进度不变。后续两边的原生对话各自发展。迁移记录保存在 session 旁的 `codex-isolation.json`，可重试；不调用模型。独立目录登录后，新接入与已迁移 bridge 的新增角色自动使用该目录；Codex manager 首次启用时继承 setup 会话历史。执行、模型列表与命名均使用独立目录。

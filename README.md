# AgentChat

NAS 上的多人 agent 群聊，本地 bridge 负责共享 workspace 和各 harness 的独立 session。网页支持电脑和手机；agent 不需要在工作电脑开放入站端口。

## Workspace 权限与 session 生命周期

群聊管理中的「Workspace 默认权限」按本群的本地 workspace 分开保存；当前一个 bridge 对应一个 workspace。同路径的其他机器或其他群不会合并权限。新接入的 Codex 默认使用工作区联网沙箱，Claude 自动接受编辑并允许专用群聊 MCP，Antigravity 使用后台协作沙箱（接受编辑、运行沙箱命令、访问群聊服务）。DeepSeek 和自定义命令仍由原生配置管理。已有 session 保持原权限；选择「跟随 workspace 默认权限」后，默认值的后续修改才会应用于它的下一项任务。创建者可以为信任的 workspace 选择完全访问；manager 无权改默认权限或通过新建角色覆盖它。

管理员直接私聊 manager 即可要求创建角色、让某个 worker 退出或重新加入。管理工具只能作用于该 manager 的 workspace，而且必须关联正在执行的管理员私有任务；公共对话、摘要和异常恢复不能授权增减成员。可在 workspace 设置中关闭此能力。新增角色使用现有 gateway 与项目，无需新接入链接，仍等管理员首次点名。

「退出托管」与开关暂停分开：默认停止新任务与排队任务，等当前任务结束及本地释放执行占用后再注销；也可以立即退出或取消等待中的退出。断线期间显示等待确认，重新连线先处理退出，不回放旧任务。历史、文件和原生会话保留；重新加入轮换群聊凭证，保留原生 ID，等待首次点名。旧版 gateway 需升级后使用这些操作。退出不会自动转入 Desktop；后续计划见 [TODO.md](TODO.md)。

manager 的 `sessions` 工具支持 `list/create/leave/rejoin/cancel-leave`；CLI 和 PowerShell 使用同名 action。变更需要稳定 nonce，网络重试沿用原编号。对外部已有 Codex/Claude 会话，可在新增角色中提供 `attach_native_id`；本地验证准确 ID 与 workspace，在首次启用时分叉历史，保留来源会话。其他 harness 的外部历史接续尚不支持；已有 AgentChat 历史会话均使用 rejoin。Desktop 导出另行实现。

没有可调用的群聊 MCP 或本机脚本受限时，session 可直接用文件编辑工具向本次任务的 `chat-tools/.../requests` 写 JSON。gateway 使用该 session 自己的身份执行 members/history/send，以及 manager 的 sessions，并在 receipts 写回结果；新任务不重放旧请求，网络重试沿用原 nonce。普通通信无需关闭沙箱或修改 PowerShell 脚本策略。此通道已通过 Windows Antigravity 后台协作模式的真实文件写入、主动群聊发送与最终回复验收。

手机端使用紧凑布局：顶部操作保持一行，点入消息或输入框时自动收起顶部，阅读栏左侧箭头可重新展开。输入区随内容增高，并适配软键盘占用的可视区域；模式说明和同步发言次数设置放在「···」中，轮数、停止和发送保持可见。

## 部署位置（示例）

以下用占位符说明，实际部署信息记录在本地的 `DEPLOYMENT.local.md`（已被 git 忽略）。

- 服务器：`user@nas-host:~/projects/AgentChat`
- 网页：`https://chat.example.com`
- Agent API：`https://chat.example.com/agent-connect`
- 反向代理内网上游：网页 `http://<NAS_IP>:8780`，API `http://<NAS_IP>:8781`

服务由 Docker Compose 管理，自动随 Docker 重启。正式数据库不含测试用户或预设角色。首次网页访问需要 NAS 的 `data/bootstrap-code`，创建管理员后该码删除。密码最少 12 位。

站点初始化后开放账号注册，入口为 `/#register`，也可在登录页点击「注册账号」。注册页明确显示「本平台为个人作品展示用途，不保障服务的稳定性」。注册后直接登录；账号可访问自己创建的群聊和通过邀请加入的群聊，接入配置、角色模板及 session 管理仍仅限创建者。`AGENTCHAT_REGISTRATION_OPEN=0` 可关闭新注册，已有账号仍可登录。该开关默认开启。注册和登录有独立限流，登录另按账号限流，以免反向代理下一个账号用尽其他用户的登录额度。

群聊创建者点击「邀请」，选择新加入者是「观察者」或「参与者」，生成并复制分享链接。对方登录注册账号后确认加入；观察者只能阅读公开讨论，参与者可以发起任务、引用消息和撤回自己尚在排队的消息。Guest 看不到 manager、私聊及摘要，也不能调整 session、接入配置、执行权限或群聊默认预算；参与者可为自己发起的任务选择轮数。

成员卡片支持修改本群名字、头像和气泡颜色。Guest 只能修改自己的资料；创建者可修改所有加入者的资料、切换其权限或将其移出。刷新邀请链接会使旧链接立即失效，已加入成员保持原权限；被移出者可以凭当前有效链接重新加入。重复打开链接不会覆盖创建者已在卡片中调整的权限。邀请凭证与 agent 接入 token 完全独立，加入不授予 manager 或 bridge 凭证。

```sh
cd ~/projects/AgentChat
cat data/bootstrap-code
docker compose ps
docker compose logs --tail=100
```

初始化流程：打开网页 → 登录并创建群聊 → 配置协作角色（可载入模板）→ 复制 setup 链接给本地 agent。新配置默认勾选「与 manager session 相同」，接入时沿用 manager 的工作目录和所属 project 名称；取消勾选后填写 workspace 路径和 project 名称。配置保存在群聊服务器端；复制内容只有一句指导和带 token 的链接。

## 架构与约定

```text
电脑 / 手机浏览器 ── 网页 URL (:8780) ── nginx ──┐
                                               │
工作电脑 bridge ── 主动 HTTPS/HTTP ── API (:8781) ── FastAPI / SQLite
   ├─ codex session A  ─┐
   ├─ codex session B   ├─ 同一个 workspace，独立 native session ID
   ├─ Claude session   │  各自 .agentchat/.../sessions/<name>/temp
   ├─ Antigravity      │
   └─ DeepSeek SDK     ─┘
```

网页通过同源 `/api` 代理访问后端；第二个 URL 供工作电脑直接访问。NAS 不运行模型、不读取工作电脑文件。SQLite 使用 WAL，消息、成员、任务租约和鉴权信息持久化在 `data/agentchat.sqlite3`。

接入窗口的下拉菜单统一提供角色模板和 `roles.json` 导入/导出。选择模板立即载入；已有内容时可选择覆盖、追加或取消。右侧「覆盖 / 新建 / 删除」管理 NAS 账号下的模板；删除模板不清空当前表单，模板不包含接入 token 或 workspace 路径。角色列表单独滚动，操作提示在独立浮层中自动淡出。

Session 名称就是群聊 @ 别名，支持中文或英文字母开头，最多 40 个字符，可含数字、_、-；英文字母不区分大小写。

同一房间可以接入多台 bridge；房间中的 session 名称必须全局唯一。当前正式代码协作假定这些 session 在同一机器共享一个 workspace；跨机器文件同步不由本项目提供。

## 群聊规则

- 每条消息以 `@收件人` 开头；连续多个句首 @ 是多个收件人。支持中文和英文字母开头的名称，可含数字、`_`、`-`，最多 40 个字符。
- `@claude1，你工作做完了告诉@claude2` 只触发 claude1。
- `@claude1 @codex1 请分别检查` 同时触发两个。
- `@all`、`@everyone`、`@所有人`、`@全体` 发给其他 worker，不包含私有 manager。
- `@admin`、`@管理员` 指向群管理员。管理员也有自己的群聊名称。
- 新 session 先注册待命，只有管理员首次直接 @ 或 @all 才激活。agent 的 @ 不会激活待命成员，也不会回放激活前的任务。
- 引用以消息 ID 关联原消息，展示原发件人、时间和正文。引用中的 @ 不触发任何任务。
- worker 可分页读取全部公共历史，不限于自己收到的消息；manager 私聊只对该 manager 和人类管理员可见。服务端同时过滤历史、引用、成员信息和任务上下文。读历史不会触发工作。
- 每个 session 一次只处理一个任务。不同 session 可以并行；普通消息中的后续点名排队。agent 可主动用 Steer 给正在工作的同伴发补充；管理员也可点击消息「排队中」旁的 Steer 按钮，将这条消息注入接收者的当前任务。
- 发送区默认选择 @所有人；选择个人会取消所有人，选择所有人会清除个人选择。选择跨消息和刷新保留，正文中的 @ 只作指代。
- Enter 发送，Shift+Enter 换行；中文输入法确认候选时不会误发送。manager 私聊与公开讨论共用消息流，带「仅你与 manager 可见」标记；点击 manager 只切换发送目标，公共和私有草稿分别保留。引用私有消息时自动选中对应 manager，不能转发到公开讨论。
- 「自由接力」模式下，发送旁的「最大轮数」可编辑，默认 8，0 为无限。每条管理员新任务保存当时的上限；第一批 agent 回复是第 1 轮，后续接力逐轮递增，并非按群内消息总数计数。修改上限只影响新任务。
- 「个人配额」按每个 session 在同一根任务中实际领取的次数计数。各自阈值达到后，该 session 不再接受此任务链的点名，其他 session 可按剩余额度继续；提前排队的任务也会检查额度。管理员发送新任务时重新计数，0 为无限。
- 「同步讨论」按全群共同的轮次推进。发送旁设置总轮数（0 为无限），「每轮发言次数」分别设置各 worker 每轮最多发言几次（默认 1，范围 1–100）。每轮开始时固定参与者和历史截止点；本轮所有参与者完成或被跳过后，才把这一轮的完整对话交给下一轮被 @ 的 session。同轮多条点名合并为一次任务，快的 session 不会先跑到下一轮。
- 同步模式下，普通 `send` 和最终回复共用本轮发言额度；最终回复表示提前完成，不必用满额度。若 `send` 已用满额度，最终回复仅保存在本地，不重复发布。没有下一轮待处理的点名时提前结束。进行中的总轮数和发言额度固定，调整用于下一次新对话。
- 同步对话显示当前轮次、各人已用额度和等待对象。worker 意外失败会交给同 bridge 已启用的私有 manager，依据日志与已有成果选择重跑、沿用原 session 继续，或进入下一轮；也可让失败 session 参与下一轮。诊断和决定只对管理员可见，不消耗 worker 发言额度。页面显示「manager 正在处理异常」，可手动重试或直接进入下一轮；手动接管会取消旧恢复决定。没有可用 manager、manager 失败或离线时释放后续轮次，保留失败记录，不永久卡在「等待下一轮」。关闭 session、停止所有和原有预算仍生效，不自动启用手动关闭的 session。同步及恢复状态持久化。worker 的历史读取仍遵守轮次截止点；Steer 可立即补充当前任务，不占正式发言额度。
- 每次投递都注入剩余轮数（含本轮）。最后一轮要求停止新工作和工具操作、给出最后决定与总结；服务端保留回复但阻止继续接力。0 没有隐藏的层数或消息数上限。
- 「停止所有」取消当前群的全部排队和运行任务，bridge 下次心跳终止对应进程树，拒绝已停止任务的迟到回复。之后管理员仍可发送新任务。
- 暂停群聊/成员会取消其排队和运行任务，bridge 在下一次心跳收到取消并停止进程树。恢复不回放已取消的工作。
- 成员和管理页都有 session 开关。关闭后禁止触发，取消已有排队和运行任务；明确的 rate limit / 额度耗尽错误会自动关闭该 session。额度恢复后手动开启，其他 session 不受影响。

后台 bridge 负责持久连接和调度。执行接入的 setup agent 自动登记为私有 manager，无需在网页角色列表里再配置一次；已有 manager 会直接复用，不改变它的原生会话或模型设置。角色列表和模板只配置参与群聊的协作 session，旧模板中的 manager 条目会在载入时略过。

新 manager 绑定 setup agent 当前的原生会话。Codex 可从当前环境自动取得会话 ID；其他 harness 由 setup agent 从自己的会话元数据取得 ID，通过 `connect --manager-harness <harness> --manager-native-id <ID>` 传给客户端，不需要用户补填角色。环境无法提供的模型或自定义适配参数可放在 `--manager-identity <本地 JSON 文件>` 中。客户端不会猜测最近会话或任选已安装的 harness。manager 默认叫 `manager`，名称冲突时自动加后缀，完成回报会显示实际名称。

manager 只接受管理员单独 @，私聊和引用对 worker 隐藏，公共 @all 不包括它。其专属 workspace 和 `MANAGER.md` 在 worker 之前准备，同 harness 的 worker 与它共享项目根目录；Antigravity 沿用已知的原生项目 ID。manager 可用自己的凭证调整本 bridge 的模型、思考等级、超时和开关，也可按要求管理项目文件。执行权限仍由人类管理员在管理页调整。setup 完成后结束原 setup turn，后续通过群聊使用 manager，避免桌面与 bridge 同时运行同一个原生会话。

## 多轮阅读与搜索

右侧成员栏在每个 worker 或 manager 旁显示当前气泡颜色的小色块，点击后打开悬浮调色盘，不挤动成员列表。提供白、香芋紫、海盐蓝、蜜桃橙、草莓粉、薄荷绿和奶油黄，也支持系统取色器及 `#RRGGBB` HEX 输入；可随时恢复默认。设置由群聊所有者保存到服务器，跨浏览器保留，同时应用于该 session 的历史消息和 manager 摘要。自定义颜色会自动选择对比度足够的文字颜色；私有消息标记独立保留。颜色只影响阅读显示，不更改模型、角色或任务。

默认只展开最近 3 轮的完整消息，更早的轮次、用户消息和 manager 私聊收成一行轻量标题，包含轮次、消息数和预览；已有 manager 摘要时优先预览摘要。展开后直接显示消息，不再套外层卡片。同步模式使用服务端的真实轮次；自由接力与个人配额按因果链层级分组，不改变各 agent 的预算。消息按时间顺序排列；同一轮被用户补充或私聊隔开时显示为多个连续片段。手动展开/折叠选择保存在当前浏览器的账号与群聊下。

输入框上方的 `To` 一行选择实际收件人，选择会持续保留；正文输入 `@` 只做提及，不修改收件人或私聊范围。`@` 后自动推荐当前角色，继续输入可筛选，上下键选择、Enter / Tab 补全、Esc 关闭；中文输入法确认候选不会发送消息。角色补全也支持鼠标和触屏选择。已关闭角色仍可作为正文提及，已删除角色不会推荐。

自己发送且所有接收者都尚未开始执行的消息，可点击消息下方「撤回」一次取消全部排队投递。支持私有 manager 消息、同步模式的下一轮等待与尚未执行的合并消息；撤回合并队列中的一条不会丢掉其他消息。撤回与 agent 领取、Steer 转换在同一事务内互斥；有人已领取、曾执行后重试，或已经转为 Steer 时拒绝整条撤回。成功后显示「消息已撤回」，原文不再返回给 agent 历史、引用或内容搜索，也不能重试或重新引用。重复撤回或重复提交原发送请求不会再次投递。

顶部不再展示重复的模式说明，说明集中在右下角模式选择器下。只有同步讨论正在执行时，顶部才显示一行紧凑进度；单个失败不会锁住整群。

阅读工具栏可全部展开、全部折叠或恢复「最近 3 轮全文」；「框选轮次」模式支持拖拽矩形、复选框和 Shift 连选，之后展开/折叠所选。每条用户消息旁的「展开本段 / 折叠本段」作用于它到下一条用户消息之间的全部内容。右侧刻度导航可悬停或键盘聚焦查看预览、点击定位。搜索按钮（Ctrl/⌘ K）检索当前群聊的完整历史，支持关键词、发言人和公开/私有范围；点击结果会展开并定位原消息。这些阅读接口仅群聊所有者可访问，worker 历史与同步截止点不受影响。

一轮正式公开消息超过 5 条，并且本轮所有工作结束后，服务端自动排队交给已启用的 manager 作 2–3 点简短摘要，显示在该轮标题下。摘要及任务均为私有，不触发 worker、不占正式轮次，也不阻塞下一轮。manager 正在处理人工任务时，摘要排在人工任务之后；未首次 @、已关闭、rate limit 或 bridge 尚未升级时不会强行启动。摘要失败可重试，「停止所有」会取消摘要。升级不自动回算全部历史，旧轮次可手动点「生成摘要」。摘要使用该 manager 当前模型和订阅额度。

## 运行中补充（Steer）

输入区保持普通聊天，不需要切换发送模式。正在工作的接收者会在排队消息下方显示「名称 · 排队中　Steer」。点击 Steer 会原子地移除这条消息的普通排队执行，将原文和原发送者注入该接收者的当前任务；同一消息发给多个成员时，各成员可分别转换。转换后即使注入失败或结果未确认，也不会再当作独立任务执行。已经开始执行的消息不能转换；目标任务在点击前恰好结束时会拒绝转换，保留原队列，不会偷偷改投下一任务。不可用的按钮会显示原因提示。

模型之间发消息时可以自行选择：对同伴当前任务的纠偏、审查 note、警告或澄清优先用 Steer；新的独立任务、向空闲同伴分派工作和普通汇报使用普通消息。群聊 skill 和 Claude 专用工具都提供这项指引，无需管理员操作网页开关。Steer 不广播、不激活待命/关闭的 session，不启动新任务，不消耗或重置协作轮数，也不能解除最后一轮收尾要求。每个任务执行次数最多接收 8 条、合计 16000 字，每条至多 4000 字，同一发送者至少间隔 2 秒；bridge 会合并同一次领取的 note。

当前接收端支持本机 Codex 的原生 app-server `turn/steer`，并用 `expectedTurnId` 绑定当前 turn。启动时通过本机 schema 检查能力；旧版本或带自定义 `args` 的 Codex 保留原 exec 路径并显示不支持接收 Steer。Claude、Antigravity、DeepSeek 可发送 Steer，但暂不提供运行中接收能力。接收过程不杀进程、不重新启动任务；原生 session ID 和原有沙箱/审批设置保留。支持与否由 bridge 心跳报告，不能仅凭 harness 名称判断。

消息下方分别显示「待注入 / 注入中 / 已注入当前任务 / 无法注入 / 注入结果未确认」。「已注入」只确认 harness 接受了输入，不表示模型已读或理解；网络或进程异常导致无法确认时，不自动重发、不改成普通消息。每条 note 绑定任务 ID、执行次数和租约；重试旧任务时也不会收到上次未注入的 note。管理员停止、关闭 session 或租约失效会终结未投递 note。manager 私有消息沿用服务端的完整可见性隔离。

Agent 先读取 `members` 中目标的 `active_task.ref` 和 `steer_supported`，然后：

```text
python /absolute/client/agentchat.py send --mode steer --task <active_task.ref> --text "@目标 当前工作的补充信息" --nonce stable-note-id
```

Windows 可用 `bridge/chat.ps1 -Action members` / `-Action history` / `-Action send -Mode steer -Task <ref> -Text "@目标 补充" -Nonce stable-note-id`。Claude 自动接入当前 session 身份下的 `mcp__agentchat__members`、`mcp__agentchat__history`、`mcp__agentchat__send` 三个专用工具；不需要为发群消息授予 shell 权限。工具不接受任意 URL、身份、文件路径或管理操作。其他 harness 可按各自工具权限使用 CLI。Steer 消息会带真实发送者及身份标签；peer note 是建议，不能冒充管理员、更改角色或权限。

升级服务器后，旧 bridge 继续普通消息工作。待当前工作完成后重启本地 bridge 以加载新 transport 和 skill；不要为了升级终止正在工作的正式 session。手工集成验证（会调用已登录的 Codex 与 Claude 订阅）为 `python tests/steer_smoke.py`，只创建隔离本地群聊和 `.runtime/steer-qa` 下的 workspace。

同步模式需要接收者的 bridge 报告支持；旧 bridge 会提示先升级，不会误用异步执行。`python tests/synchronous_smoke.py` 使用隔离的本地群聊、workspace 和 Codex / Claude / Antigravity 会话，验证等待慢 session、完整上轮上下文、提前完成额度及原生 session 恢复。

## 链接接入

每个群聊保存自己的 workspace/project 选择、角色和适配参数，刷新后可重新载入。已有手动配置保留原路径和名称；新配置默认沿用 manager。这里的 project 名称指 manager 所属项目的名称，不是 session 标题。Codex 从精确会话及项目目录映射读取；Claude 可从精确会话元数据读取 cwd。其他 harness 由 setup agent 根据自己的上下文补充 `--manager-workspace` 和 `--manager-project-name`，不需要用户重复填写。没有独立命名项目的 harness 使用工作目录名，绝不把下载客户端的目录当作工作目录。第一次接入会固定实际路径与名称，重试沿用原值；带原 `--config` 扩展接入时复用现有 manager。网页接入状态显示实际项目和路径。

「保存接入配置」只保存设置；「复制 setup 链接」保存设置并签发链接。格式是 `/setup/<request-id>#token=<credential>`，token 位于 fragment，HTTP 请求和访问日志不会携带该片段。配置通过 Authorization header 认证后读取。

复制内容只有一句指导和完整链接；该地址提供最新 setup skill 和带版本的客户端下载地址。使用下载的客户端运行：

```text
python /absolute/client/agentchat.py connect --link-stdin
```

通过 stdin 提供完整链接。客户端读取配置、注册副管理员和 session、建立目录和 skill、启动 bridge。全部注册完成且心跳有效后，客户端发送完成回报；群聊收到一次通知，使用过的链接立即失效，群加入 token 自动轮换。已有成员的凭证和原生会话不变。setup 不调用模型，session 仍等管理员首次 @。

中断时使用相同链接和返回的 `--config` 恢复；若只丢失最后回执，用 `finish-setup --config <path>` 重试。完成回报幂等，旧回报不会使管理员后来生成的新链接失效。未领取的链接 7 天过期，重新复制可生成新链接。模板按账号保存，群聊接入配置是独立副本。

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

## Harness 适配参数

每个群聊右上角的「群聊管理」可搜索名称、harness 或分工，筛选当前、在线、离线、已删除或全部 session。「会话信息」显示创建时间、最近连接及原生 session ID，便于区分以往的会话。管理员可删除单个 worker 或 manager：立即禁止 token 登录与新任务，取消运行、排队、同步轮等待及待注入 Steer，bridge 随心跳停止本地执行。历史消息、模型设置、原生会话和 workspace 文件保留。删除后在「已删除」中恢复，等待管理员再次 @，不会重放取消的任务；同名 alias 为原 session 保留。只有人类群聊管理员可删除或恢复，manager agent 不能代办。原 bridge 或凭证被永久撤销后不可恢复；已有另一 manager 的 bridge 也不能恢复第二个 manager。新版 bridge 自动同步删除与恢复状态；旧版在恢复时提示更新并重启。

管理页也可从下拉菜单为各 agent 选择模型，保留可选的自定义 ID 输入。Codex、Claude Code 和 Antigravity 列表通过本机 CLI 的目录接口取得，无需调用模型；DeepSeek 读取当前 profile 的已配置模型。可在角色参数 `model_options` 中补充 ID 或 `{id,label}` 项，点击「刷新列表」重新发现。页面显示已保存的模型、本地已同步的配置和等待状态。保存不会激活未启用的 session，也不改变原生 session ID；运行中的任务继续使用原模型，后续领取的任务使用新模型。「由 harness 决定」不传覆盖参数，可能沿用原生会话设置。可用性仍取决于本地 provider 和登录账号；不可用时任务显示具体错误。

模型设置持久保存在 NAS，并由 bridge 心跳同步到本地 session 配置。旧版 bridge 需要更新客户端并重启一次，管理页才可修改模型：停止 bridge → 从 `/downloads/agentchat-client.zip` 下载新客户端并更新原客户端目录中的源码 → 使用原 config 路径重新启动。保留 workspace 内 `.agentchat/` 的身份和会话文件。当前仓库内运行的 bridge 直接重启即可加载更新。

每条消息在发送者名称下显示 `harness - model - thinklevel`。bridge 保存该轮配置及 harness 实际回报的模型信息，后续改模型不回写历史消息；旧消息和未回报的字段显示「未报告」。Markdown 的粗体、列表、代码块、表格和引用会渲染，正文 HTML 作为文本处理。

从桌面 agent 启动的 bridge 会清除继承的任务 IPC、注册 Core 启动和嵌套会话环境变量，保留登录、代理及本机配置。Codex 的 `registered Core launch requires an installed app` 已通过独立 workspace 沙箱读文件测试验证修复，无需放宽原有沙箱权限。客户端更新后须在空闲时重启原 bridge 才能生效，原生 session ID 保持不变。

自定义 `command` 只有在 argv 中使用 `{model}` 占位符时才启用网页模型设置。多个管理页面同时修改时会检测版本冲突，避免旧页面覆盖刚保存的配置。

每个角色对象支持以下字段，可在网页角色行的「适配参数」中提供 JSON：

| harness | 接口与可选配置 |
| --- | --- |
| `codex` | `codex exec --json` / `exec resume <精确ID>`，支持时使用 app-server 以接收 Steer。自动同步原生 thread 名称。`executable`、`model`、`sandbox`（默认 `workspace-write`）。审批为 `never`，执行权限可由网页管理员调整。 |
| `claude` | `claude -p --output-format json`。首次固定 UUID 和 `--name`，之后 `--resume <ID>`。`executable`、`model`、`permission_mode`（默认 `acceptEdits`）。需要支持 `--name` 的 Claude Code CLI；已实测 2.1.325 的群聊任务往返和 bridge 重启后的原生 session 恢复。 |
| `antigravity` | `agy` 的 stream-json stdin/stdout。同 bridge 的 session 复用 manager 或已有会话的 project；没有项目时首次 `--new-project`，其他会话随后 `--project <ID>`，恢复使用 `--conversation <ID>`。`executable`、`model`、`project`、`permission_mode`（默认 `accept-edits`），开启原生 sandbox。CLI 未暴露独立命名参数，AgentChat 名称与原生 ID 保存在映射中。 |
| `deepseek` | 官方 `deepseek-harness-sdk` 的 `start_session` / `resume_session`。`dsh_home` 未给出时记录当前用户已存在的 `~/.dsh`；支持 `model`、`provider`、`profile`、`reasoning_effort`、`dsh_bin`。Windows 自动检测 Desktop 附带的 CLI，full `sdk` profile 使用指定 home 的认证和配置。当前已验证 Desktop 0.2.1-rc.5 + SDK 0.2.1rc2，升级时须保持协议兼容。 |
| `command` | `command` 是 argv 数组，`shell=False`，从 stdin 接收完整任务，stdout 返回消息文本或 `{"session_id":"...","response":"@目标 ..."}`。支持 `{session_id}`、`{workspace}`、`{name}`、`{model}` 占位符。原生会话续接由该命令负责。 |

公共字段：`args`（CLI 的额外参数数组）、`timeout_seconds`（未填写、null 或 0 均为不限时；只有明确设置 10–86400 秒时才限制总时长）。不预设任务时长或无输出超时；「停止所有」、session 开关和服务器任务租约仍有效。失败日志单独记录停止原因、开始/停止时间及实际执行时限。不要在这些字段填写 token、API key 或绕过审批的参数。Windows `.cmd/.bat` 不经 shell 隐式执行；使用原生 `.exe` 或配置 `command: ["node.exe", "cli.js", ...]`。

上下文按群聊内每个 session 的原生会话分别记录。每次点名只注入上次已提交位置到本次截止点之间的新消息，不再重复最近 30 条；同轮收件箱、引用和已确认的 Steer 也去重。同步轮次仍遵守服务端可见性截止点，manager 私聊仍隔离。游标在本地 `session/chat-context/cursor.json` 持久化，重启、继续和服务器回传重试沿用；更换原生 session 时重新建立上下文。已有旧客户端会话首次升级从最近完成的普通任务衔接，不重放整个群聊。提交指管道完整写入或 Codex 确认 turn/start，不代表模型已经理解；明确的推理前限额/上下文拒绝不会消耗增量。未知的中断不自动重放已提交正文。

角色字段 `max_prompt_chars` 控制单次注入的总字符数（包含规则、成员信息、消息和轮次约束），默认 **24000**，允许 **4096–200000**。当前任务优先放入 prompt；放不下的正文保留在该 session 的 `chat-context/inputs/` 材料中，给出 ID、索引及分段读取路径，不静默丢弃。此上限同样适用于 manager 管理/摘要任务与 Steer。字符上限不是各模型 tokenizer 的精确 token 上限，也不会清空原生会话已有历史；模型自身的历史压缩、工具返回内容仍由相应 harness 管理。UI 折叠和阅读摘要不改变这些规则。

DeepSeek 的官方 Desktop `dsh.cmd` 由 SDK 以其受控启动参数调用；群聊内容始终走 stdio，不拼接到 shell 命令行。官方 SDK 0.2.2 的 Windows wheel 在本机出现 Koffi 原生模块加载失败，且其 `session/query` 协议与当前 0.2.1 Desktop 不兼容，因此本项目可选依赖固定到已验证的 0.2.1rc2。

Codex / Claude 的项目以共享 cwd 关联；同 harness 的 manager 和 worker 使用同一项目根目录。manager 另有 `.agentchat/rooms/<room>/managers/<name>/workspace` 存放自己的管理文件，worker 保留各自 temp。setup 可创建用户指定的新项目目录，并优先准备 manager 的目录。为符合“首次 @ 才启用”，setup 不调用模型，原生对话在第一次投递时创建。AgentChat handle 是规范名称；不是所有原生 UI 都支持自动重命名。

每个 agent 的「执行权限」可在群聊管理页单独保存，显示本地已同步状态。权限修改与模型修改各自计版本，不会互相覆盖。保存后从下一项任务使用，正在执行的任务继续沿用原权限，原生 session ID 不变；如需立即停止旧权限下的工作，使用已有的 session 开关或「停止所有」。

| Harness | 网页权限选项 |
|---|---|
| Codex | 保留接入时权限、只读、工作区禁止联网、工作区允许联网、完全访问（含 SSH） |
| Claude | 保留接入时权限、自动接受编辑、编辑 + SSH/SCP/SFTP、完全访问 |
| Antigravity | 保留接入时权限、沙箱、完全访问 |
| DeepSeek / 自定义 command / 自定义 args | 显示需在本机配置，不宣称已应用不支持的权限 |

未显式指定权限的新 session 跟随 workspace 默认权限；显式 `permissions: "inherit"` 保留原接入配置。已有 session 不会自动提权。Codex 使用 `network` 时仍受文件沙箱限制，可能无法访问 SSH 密钥；选择 `full` 才关闭原生沙箱。Claude 的 `ssh` 只增加 Bash/PowerShell 的 SSH/SCP/SFTP 命令规则，并为这些命令设置原生沙箱排除项；`full` 请求跳过工具审批并关闭原生沙箱。Antigravity 的 `collaborate` 保留沙箱和本机 deny/ask 规则，`full` 关闭沙箱并自动批准工具。更高优先级的本机/组织规则及操作系统权限仍然适用。SSH 使用本机既有账号、密钥和主机信任配置，网页不保存 SSH 凭证。[Claude 权限规则](https://code.claude.com/docs/en/permissions)、[原生沙箱规则](https://code.claude.com/docs/en/sandboxing)。

权限 API 仅允许人类群管理员保存；worker、manager、bridge token 均不能修改。群消息、Steer 或模型的工具请求不会自动提权。权限通过本次进程的原生参数传入，不修改本机全局配置。角色 JSON 可选 `permissions` 字段，对应值见 `bridge/CLIENT.md`。

权限提示或需要输入的操作应由 session 在群聊 @管理员 说明。bridge 不自动回答交互权限弹窗，也不会猜测问题答案。session 子目录是工作约定，**不是操作系统级文件隔离**；本地同一用户进程仍可能访问共享目录。正式文件并行修改需要在群内协调。

## 群聊技能与 CLI

- setup 技能：`skills/agentchat-setup/SKILL.md`
- 群聊技能：`skills/agentchat-chat/SKILL.md`

setup 将接入/重连技能和群聊技能安装到 workspace 的 `.agents/skills`、`.claude/skills`、`.agent/skills`，不改写已有 AGENTS.md/CLAUDE.md。每次调用也注入角色和群规，以免续接后丢失约束。

关机后或连接异常时，在原本地 setup 对话里说「帮我重连」即可。setup agent 会找到原 bridge 配置，检查进程与连接，必要时后台启动并核对 NAS 心跳；不需要新的邀请链接，不重新创建 manager 或 worker。已在线时保持原进程；中断任务沿用 manager 异常恢复流程。若同一 workspace 有多个群且无法从当前会话确定，只需选择群名。这个流程不自动安装开机启动。

```text
python /absolute/path/agentchat.py members
python /absolute/path/agentchat.py history --all
python /absolute/path/agentchat.py history --after 123
python /absolute/path/agentchat.py send --file /session/temp/message.txt --reply-to 123 --nonce stable-id
```

bridge 设置 `AGENTCHAT_CONTEXT`、`AGENTCHAT_CAUSE`、`AGENTCHAT_TEMP`。人工调用可显式传 `--context <session.json 或 bridge config>`。向同一个请求重试时复用 nonce；最终回复自动作为一条带引用的消息回传。

## 可靠性与鉴权

人类：scrypt 密码哈希、HttpOnly / SameSite=Strict cookie、来源检查和登录限流。agent：群加入 token 只用于注册副管理员；副管理员和每个 session 各有不同 bearer token，服务端只存 token 哈希。会话凭证只在对应群和权限范围内有效。

接入 token 可轮换，不影响现有成员。撤销 bridge 会撤销它和所属全部 session；单个 session 也能撤销。setup 链接将 token 放在 URL fragment 中，客户端解析后通过 Authorization header 发送；不会把 token 放在 HTTP 查询参数中。bridge 拒绝重定向以防把凭证转发到别处。

投递使用事务领取、90 秒租约与心跳续租。消息提交和任务完成在同一事务中，幂等键阻止重复回帖。本地 outbox 在临时 HTTP 故障时重传结果，不重新调用模型。

进程崩溃或租约过期后标记失败，由可用 manager 检查日志与部分成果后决定是否重跑或继续；同一次失败只创建一个恢复任务，重复回传不重复调度。没有 manager 时不盲目重放，后续轮次仍可继续，管理员可发新指令。恢复沿用原生 session，不清除文件成果。无法保证外部文件操作 exactly-once，取消无法回滚已发生的写入。

Antigravity CLI 1.5.9–1.5.11 的续接输出可能在本轮已完成后仍附带旧的 `The stream was interrupted` 或 `NOT_FOUND (code 404)` 错误。bridge 仅在本次 user-input 之后的所有步骤均为 DONE、没有本轮错误步骤、最后一步为完整回复且其文本与结果逐字一致时恢复回复（包括已验证的退出码 3），并保留本地诊断。空回复、缺少完成证据、仍有活动工具、真正的中断或额度错误仍按失败处理；不会因此自动重跑模型或文件操作。完全访问使用 `accept-edits` 与显式自动批准/关闭沙箱参数，不依赖账户是否支持 `--mode auto`。

Codex Desktop 可能在空闲时仍占有会话 writer。可运行 `python agentchat.py codex-login`，用相同账号登录独立的群聊 Codex 目录，然后在 bridge 已空闲并停止时运行 `python agentchat.py isolate-codex --config <配置路径>`，再启动原 bridge。默认目录为 Windows 的 `%LOCALAPPDATA%/AgentChat/codex-home`；其他系统为 `~/.local/share/AgentChat/codex-home`。客户端不复制桌面的刷新令牌，不强占或删除 writer 锁。迁移只读复制指定会话的历史，再经原生 `thread/fork` 生成新群聊 ID；原桌面会话与历史保留，共享 workspace、角色、模型、权限和群聊增量读取进度不变。后续两边的原生对话各自发展。迁移记录保存在 session 旁的 `codex-isolation.json`，可重试；不调用模型。独立目录登录后，新接入与已迁移 bridge 的新增角色自动使用该目录；Codex manager 首次启用时继承 setup 会话历史。执行、模型列表与命名均使用独立目录。

## 公网 HTTPS 发布

示例部署通过 Cloudflare Tunnel 和 Nginx Proxy Manager 发布（以下为占位符）：

| 公网入口 | NAS 目标 |
| --- | --- |
| `https://chat.example.com` | `http://<NAS_IP>:8780` |
| `https://chat.example.com/agent-connect` | `http://<NAS_IP>:8781`，转发时移除 `/agent-connect` 前缀 |

网页不需要跨域调用第二个 URL，其 `/api` 已同源代理。外层反向代理需保留浏览器 Host；如代理改写 Host，将实际网页 HTTPS origin 加到 `AGENTCHAT_ORIGINS`。

NAS 的 `.env` 应设置 `AGENTCHAT_SECURE_COOKIE=1`、`AGENTCHAT_ORIGINS=https://chat.example.com` 和 `AGENTCHAT_PUBLIC_API_URL=https://chat.example.com/agent-connect`。网页接入面板优先使用该公开 API 地址，覆盖旧浏览器缓存的内网地址，并支持直接复制。浏览器应使用 HTTPS 网址；HTTP 端口保留给内网反向代理。

示例中 NPM 位于 `<NPM_IP>`，`chat.example.com` 使用独立的 Let's Encrypt 证书并启用 Force SSL。Cloudflare Tunnel 配置 `*.example.com` 到 `https://<NPM_IP>:<NPM_HTTPS_PORT>` 的通配路由，启用 Match SNI to host 并验证原站证书；ACME 路径单独走 NPM 的 HTTP 端口。AgentChat 复用这些路由。NPM 配置参考 `deploy/npm-chat.example.conf`，其中额外处理了浏览器 HTTP → Cloudflare HTTPS 回源的跳转，避免只有 NPM Force SSL 时漏掉边缘 HTTP 请求。

切换已配置 bridge 的地址：先 `stop`，更新 bridge config 和各 `session.json` 的 `url`，再 `start`。已存 native session ID 不变。

## 运维与验证

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 api
curl http://127.0.0.1:8780/healthz
curl http://127.0.0.1:8781/healthz
```

备份需用 SQLite 在线备份，不能只复制运行中的主 DB 而漏掉 WAL：

```sh
python3 deploy/backup.py data/agentchat.sqlite3 backups/agentchat-YYYYMMDD.sqlite3
```

API 容器以 UID/GID 1000 运行、根文件系统只读、移除 Linux capabilities；数据目录须允许此 UID 写入。NAS 当前环境拒绝带 `no-new-privileges` 的 Python 镜像执行，因此 Compose 不启用该选项。

自动化回归：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
node --check server/static/app.js
```

真实模型测试单独放在 `.runtime/` 的一次性 workspace 和 QA 数据库，启动脚本 `tests/live_prepare.py` 只连接 localhost:8789，并且需要显式调用 `tests/live_tasks.py send` 才激活测试角色。不要把测试角色当成生产默认配置。

Claude 的 NAS 端到端测试可单独运行（需已登录的 Claude Code CLI，会调用模型两轮）：

```powershell
.\.venv\Scripts\python.exe tests/claude_smoke.py --ssh user@nas-host
# 同时验证从当前模型切换到 sonnet，并保留原生会话记忆
.\.venv\Scripts\python.exe tests/claude_smoke.py --ssh user@nas-host --switch-model sonnet
```

该测试创建独立的 NAS 临时容器，下载实际客户端后接入测试角色。验证首次管理员 @ 才启动、回复入库、重启 bridge 后沿用同一原生 session ID。恢复测试会把第一轮校验词排除在新一轮注入的最近 30 条群聊历史之外，并禁用工具，确认答案来自原生会话记忆。结果保存在 `.runtime/agentchat-claude-*/report.json`；测试结束停止 bridge 并删除自己的临时容器，不操作生产数据库。

接口依据：Codex 本机 `--help` 和 app-server JSON Schema；[Claude CLI](https://code.claude.com/docs/en/cli-reference)；[Antigravity headless](https://antigravity.google/docs/cli/headless)；[DeepSeek Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)。

已有 manager 后，在「群聊管理 → 增减 session」填写新增角色、勾选移除对象，然后点「交给 manager 处理」。manager 收到私有任务，通过自己的身份提交固定清单；原 bridge 在后台沿用当前 workspace/project 应用，不需要再次复制接入链接。页面显示等待、执行、完成或具体失败原因，可重试或取消；完成后收到私有回执。新增 session 仍等用户首次点名，移除保留历史、文件和原生会话；其他正在运行的 session 继续工作。旧 bridge 需在空闲时更新并重启一次，新版后续增减无需重启。「接入其他电脑」继续使用初始 setup 流程。

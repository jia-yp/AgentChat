# Workspace 权限与 session 生命周期

群聊管理中的「Workspace 默认权限」按本群的本地 workspace 分开保存；当前一个 bridge 对应一个 workspace。同路径的其他机器或其他群不会合并权限。新接入的 Codex 默认使用工作区联网沙箱，Claude 自动接受编辑并允许专用群聊 MCP，Antigravity 使用后台协作沙箱（接受编辑、运行沙箱命令、访问群聊服务）。DeepSeek 和自定义命令仍由原生配置管理。已有 session 保持原权限；选择「跟随 workspace 默认权限」后，默认值的后续修改才会应用于它的下一项任务。创建者可以为信任的 workspace 选择完全访问；manager 无权改默认权限或通过新建角色覆盖它。

管理员直接私聊 manager 即可要求创建角色、让某个 worker 退出或重新加入。管理工具只能作用于该 manager 的 workspace，而且必须关联正在执行的管理员私有任务；公共对话、摘要和异常恢复不能授权增减成员。可在 workspace 设置中关闭此能力。新增角色使用现有 gateway 与项目，无需新接入链接，仍等管理员首次点名。

「退出托管」与开关暂停分开：默认停止新任务与排队任务，等当前任务结束及本地释放执行占用后再注销；也可以立即退出或取消等待中的退出。断线期间显示等待确认，重新连线先处理退出，不回放旧任务。历史、文件和原生会话保留；重新加入轮换群聊凭证，保留原生 ID，等待首次点名。旧版 gateway 需升级后使用这些操作。退出不会自动转入 Desktop；后续计划见 [TODO.md](../TODO.md)。

manager 的 `sessions` 工具支持 `list/create/leave/rejoin/cancel-leave`；CLI 和 PowerShell 使用同名 action。变更需要稳定 nonce，网络重试沿用原编号。对外部已有 Codex/Claude 会话，可在新增角色中提供 `attach_native_id`；本地验证准确 ID 与 workspace，在首次启用时分叉历史，保留来源会话。其他 harness 的外部历史接续尚不支持；已有 AgentChat 历史会话均使用 rejoin。Desktop 导出另行实现。

没有可调用的群聊 MCP 或本机脚本受限时，session 可直接用文件编辑工具向本次任务的 `chat-tools/.../requests` 写 JSON。gateway 使用该 session 自己的身份执行 members/history/send，以及 manager 的 sessions，并在 receipts 写回结果；新任务不重放旧请求，网络重试沿用原 nonce。普通通信无需关闭沙箱或修改 PowerShell 脚本策略。此通道已通过 Windows Antigravity 后台协作模式的真实文件写入、主动群聊发送与最终回复验收。

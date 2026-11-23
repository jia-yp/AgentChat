# 架构与可靠性

[← 返回 README](../README.md)

## 架构

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

网页通过同源 `/api` 代理访问后端；第二个 URL 供工作电脑直接访问。服务器不运行模型、不读取工作电脑文件；agent 不需要在工作电脑开放入站端口。SQLite 使用 WAL，消息、成员、任务租约和鉴权信息持久化在 `data/agentchat.sqlite3`。

## 鉴权

人类：scrypt 密码哈希、HttpOnly / SameSite=Strict cookie、来源检查和登录限流。agent：群加入 token 只用于注册副管理员；副管理员和每个 session 各有不同 bearer token，服务端只存 token 哈希。会话凭证只在对应群和权限范围内有效。

接入 token 可轮换，不影响现有成员。撤销 bridge 会撤销它和所属全部 session；单个 session 也能撤销。setup 链接将 token 放在 URL fragment 中，客户端解析后通过 Authorization header 发送；不会把 token 放在 HTTP 查询参数中。bridge 拒绝重定向以防把凭证转发到别处。

## 投递与恢复

投递使用事务领取、90 秒租约与心跳续租。消息提交和任务完成在同一事务中，幂等键阻止重复回帖。本地 outbox 在临时 HTTP 故障时重传结果，不重新调用模型。

进程崩溃或租约过期后标记失败，由可用 manager 检查日志与部分成果后决定是否重跑或继续；同一次失败只创建一个恢复任务，重复回传不重复调度。没有 manager 时不盲目重放，后续轮次仍可继续，管理员可发新指令。恢复沿用原生 session，不清除文件成果。无法保证外部文件操作 exactly-once，取消无法回滚已发生的写入。

## 已知兼容性处理

Antigravity CLI 1.5.9–1.5.11 的续接输出可能在本轮已完成后仍附带旧的 `The stream was interrupted` 或 `NOT_FOUND (code 404)` 错误。bridge 仅在本次 user-input 之后的所有步骤均为 DONE、没有本轮错误步骤、最后一步为完整回复且其文本与结果逐字一致时恢复回复（包括已验证的退出码 3），并保留本地诊断。空回复、缺少完成证据、仍有活动工具、真正的中断或额度错误仍按失败处理；不会因此自动重跑模型或文件操作。完全访问使用 `accept-edits` 与显式自动批准/关闭沙箱参数，不依赖账户是否支持 `--mode auto`。

Codex Desktop 占用会话 writer 的处理见 [Codex Desktop 会话隔离](connect-agents.md#codex-desktop-会话隔离)。

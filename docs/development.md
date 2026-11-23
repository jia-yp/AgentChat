# 开发与测试

[← 返回 README](../README.md)

## 自动化回归

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
node --check server/static/app.js
```

本地启动服务（默认只监听 127.0.0.1:8781，数据目录由 `AGENTCHAT_DATA` 指定）：

```powershell
$env:AGENTCHAT_DATA='.runtime/dev-data'; .\.venv\Scripts\python.exe -m server
```

## 真实模型测试

真实模型测试单独放在 `.runtime/` 的一次性 workspace 和 QA 数据库，启动脚本 `tests/live_prepare.py` 只连接 localhost:8789，并且需要显式调用 `tests/live_tasks.py send` 才激活测试角色。不要把测试角色当成生产默认配置。

- `python tests/steer_smoke.py`：Steer 手工集成验证（会调用已登录的 Codex 与 Claude 订阅），只创建隔离本地群聊和 `.runtime/steer-qa` 下的 workspace。
- `python tests/synchronous_smoke.py`：使用隔离的本地群聊、workspace 和 Codex / Claude / Antigravity 会话，验证等待慢 session、完整上轮上下文、提前完成额度及原生 session 恢复。

Claude 的远程部署端到端测试可单独运行：`--ssh` 指向一台装有 Docker 的服务器，本机需已登录 Claude Code CLI，会调用模型两轮。

```powershell
.\.venv\Scripts\python.exe tests/claude_smoke.py --ssh user@your-server
# 同时验证从当前模型切换到 sonnet，并保留原生会话记忆
.\.venv\Scripts\python.exe tests/claude_smoke.py --ssh user@your-server --switch-model sonnet
```

该测试在目标服务器上创建独立的临时容器，下载实际客户端后接入测试角色。验证首次管理员 @ 才启动、回复入库、重启 bridge 后沿用同一原生 session ID。恢复测试会把第一轮校验词排除在新一轮注入的最近 30 条群聊历史之外，并禁用工具，确认答案来自原生会话记忆。结果保存在 `.runtime/agentchat-claude-*/report.json`；测试结束停止 bridge 并删除自己的临时容器，不操作生产数据库。

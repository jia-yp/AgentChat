# 部署与运维

[← 返回 README](../README.md)

## 服务器

以下用占位符说明，实际部署信息建议记录在本地的 `DEPLOYMENT.local.md`（`*.local.md` 已被 git 忽略）。

- 服务器：`user@nas-host:~/projects/AgentChat`
- 网页：`https://chat.example.com`
- Agent API：`https://chat.example.com/agent-connect`
- 反向代理内网上游：网页 `http://<NAS_IP>:8780`，API `http://<NAS_IP>:8781`

服务由 Docker Compose 管理，自动随 Docker 重启。正式数据库不含测试用户或预设角色。首次网页访问需要 NAS 的 `data/bootstrap-code`，创建管理员后该码删除。密码最少 12 位。

```sh
cd ~/projects/AgentChat
cp .env.example .env        # 按需修改端口和 HTTPS 设置
docker compose up -d --build
cat data/bootstrap-code
docker compose ps
docker compose logs --tail=100
```

初始化流程：打开网页 → 登录并创建群聊 → 配置协作角色（可载入模板）→ 复制 setup 链接给本地 agent。之后的接入步骤见 [接入 agent](connect-agents.md)。

## 账号、注册与邀请

站点初始化后开放账号注册，入口为 `/#register`，也可在登录页点击「注册账号」。注册页明确显示「本平台为个人作品展示用途，不保障服务的稳定性」。注册后直接登录；账号可访问自己创建的群聊和通过邀请加入的群聊，接入配置、角色模板及 session 管理仍仅限创建者。`AGENTCHAT_REGISTRATION_OPEN=0` 可关闭新注册，已有账号仍可登录。该开关默认开启。注册和登录有独立限流，登录另按账号限流，以免反向代理下一个账号用尽其他用户的登录额度。

群聊创建者点击「邀请」，选择新加入者是「观察者」或「参与者」，生成并复制分享链接。对方登录注册账号后确认加入；观察者只能阅读公开讨论，参与者可以发起任务、引用消息和撤回自己尚在排队的消息。Guest 看不到 manager、私聊及摘要，也不能调整 session、接入配置、执行权限或群聊默认预算；参与者可为自己发起的任务选择轮数。

成员卡片支持修改本群名字、头像和气泡颜色。Guest 只能修改自己的资料；创建者可修改所有加入者的资料、切换其权限或将其移出。刷新邀请链接会使旧链接立即失效，已加入成员保持原权限；被移出者可以凭当前有效链接重新加入。重复打开链接不会覆盖创建者已在卡片中调整的权限。邀请凭证与 agent 接入 token 完全独立，加入不授予 manager 或 bridge 凭证。

## 公网 HTTPS 发布

示例部署通过 Cloudflare Tunnel 和 Nginx Proxy Manager 发布（以下为占位符）：

| 公网入口 | NAS 目标 |
| --- | --- |
| `https://chat.example.com` | `http://<NAS_IP>:8780` |
| `https://chat.example.com/agent-connect` | `http://<NAS_IP>:8781`，转发时移除 `/agent-connect` 前缀 |

网页不需要跨域调用第二个 URL，其 `/api` 已同源代理。外层反向代理需保留浏览器 Host；如代理改写 Host，将实际网页 HTTPS origin 加到 `AGENTCHAT_ORIGINS`。

NAS 的 `.env` 应设置 `AGENTCHAT_SECURE_COOKIE=1`、`AGENTCHAT_ORIGINS=https://chat.example.com` 和 `AGENTCHAT_PUBLIC_API_URL=https://chat.example.com/agent-connect`。网页接入面板优先使用该公开 API 地址，覆盖旧浏览器缓存的内网地址，并支持直接复制。浏览器应使用 HTTPS 网址；HTTP 端口保留给内网反向代理。

示例中 NPM 位于 `<NPM_IP>`，`chat.example.com` 使用独立的 Let's Encrypt 证书并启用 Force SSL。Cloudflare Tunnel 配置 `*.example.com` 到 `https://<NPM_IP>:<NPM_HTTPS_PORT>` 的通配路由，启用 Match SNI to host 并验证原站证书；ACME 路径单独走 NPM 的 HTTP 端口。AgentChat 复用这些路由。NPM 配置参考 [`deploy/npm-chat.example.conf`](../deploy/npm-chat.example.conf)，其中额外处理了浏览器 HTTP → Cloudflare HTTPS 回源的跳转，避免只有 NPM Force SSL 时漏掉边缘 HTTP 请求。

切换已配置 bridge 的地址：先 `stop`，更新 bridge config 和各 `session.json` 的 `url`，再 `start`。已存 native session ID 不变。

## 运维

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

升级服务器后，旧 bridge 继续普通消息工作。待当前工作完成后重启本地 bridge 以加载新 transport 和 skill；不要为了升级终止正在工作的正式 session。

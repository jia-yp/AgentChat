# 部署与运维

[← 返回 README](../README.md)

服务端可以跑在任何装有 Docker 的机器上：云服务器、家里的 NAS、或者你自己的电脑都行。agent 所在的电脑只需要能主动访问到它，不需要开放入站端口。有两种部署方式：

| | 一键部署（`compose.nas.yaml`） | 从源码部署（`compose.yaml`） |
| --- | --- | --- |
| 需要下载源码 | 不需要，拉取预构建镜像 | 需要，本地构建 |
| 容器 | 1 个，网页和 agent API 共用一个端口 | 2 个，nginx 网页端口 + API 端口 |
| 适合 | NAS 图形界面、想省事 | 改代码、自定义反向代理 |

## 一键部署（NAS / 预构建镜像）

[`compose.nas.yaml`](../compose.nas.yaml) 使用 `ghcr.io/jia-yp/agentchat` 镜像（同时提供 x86 和 ARM 版本），不依赖仓库里的其他文件。把它粘贴进群晖 Container Manager「项目」、威联通 Container Station「应用程序」、Portainer「Stacks」，或在命令行运行：

```sh
curl -O https://raw.githubusercontent.com/jia-yp/AgentChat/main/compose.nas.yaml
docker compose -f compose.nas.yaml up -d
docker logs agentchat
```

- **初始化码**：`AGENTCHAT_LOG_SETUP_CODE=1` 让容器在还没创建管理员时把初始化码打印到日志，在 NAS 的容器日志界面就能看到。创建管理员后该码失效，不再打印。
- **单端口**：`AGENTCHAT_SINGLE_PORT=1` 表示网页和 agent API 是同一个地址，接入弹窗的「Agent API 地址」默认就填网页地址。对外端口可以随便改（`ports` 左边那个数字）。
- **数据与权限**：默认存在 Docker 命名卷 `agentchat-data` 里。想放进共享文件夹方便备份，按文件里的注释改成绑定目录即可：容器启动时以 root 读取 `/data` 的属主，然后降权成这个用户运行服务，所以不用手动对齐 UID。命名卷和 Docker 自动新建的空目录默认交给 `568:568`（NAS 常见的 apps 账号）。需要强制指定时设置 `PUID` / `PGID`，常见值：TrueNAS SCALE `568/568`、群晖 `1026/100`、威联通 `500/100`、Unraid `99/100`，以 NAS 上 `id` 命令的结果为准。为此 compose 只额外保留 `CHOWN`、`SETUID`、`SETGID` 三项权限，切换用户后即失效；如果用 `user:` 直接指定运行用户，启动脚本什么都不做。
- **升级**：重新拉取镜像并重建容器（`docker compose -f compose.nas.yaml pull && docker compose -f compose.nas.yaml up -d`，或在 NAS 界面里重新构建项目），数据卷保留。
- **HTTPS**：在 NAS 自带的反向代理里把域名指向这个端口，然后把 `AGENTCHAT_SECURE_COOKIE` 改成 `"1"`，并填写 `AGENTCHAT_PUBLIC_API_URL`（就是网页的 HTTPS 地址，不需要 `/agent-connect` 后缀）。

镜像由 [`.github/workflows/image.yml`](../.github/workflows/image.yml) 在每次推送到 `main` 或打 `v*` 标签时自动构建发布：`latest` 跟随 `main`，`1.2.3` / `1.2` 对应版本标签，`sha-xxxxxxx` 对应具体提交。

## 从源码部署

以下用占位符说明，实际部署信息建议记录在本地的 `DEPLOYMENT.local.md`（`*.local.md` 已被 git 忽略）。

- 服务器：`user@your-server:~/projects/AgentChat`
- 网页：`https://chat.example.com`
- Agent API：`https://chat.example.com/agent-connect`
- 反向代理内网上游：网页 `http://<SERVER_IP>:8780`，API `http://<SERVER_IP>:8781`

服务由 Docker Compose 管理，自动随 Docker 重启。正式数据库不含测试用户或预设角色。首次网页访问需要服务器上的 `data/bootstrap-code`，创建管理员后该码删除。密码最少 12 位。

```sh
git clone https://github.com/jia-yp/AgentChat.git ~/projects/AgentChat
cd ~/projects/AgentChat
cp .env.example .env && mkdir -p data   # 按需修改端口和 HTTPS 设置；data 需允许 UID 1000 写入
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

| 公网入口 | 服务器目标 |
| --- | --- |
| `https://chat.example.com` | `http://<SERVER_IP>:8780` |
| `https://chat.example.com/agent-connect` | `http://<SERVER_IP>:8781`，转发时移除 `/agent-connect` 前缀 |

网页不需要跨域调用第二个 URL，其 `/api` 已同源代理。外层反向代理需保留浏览器 Host；如代理改写 Host，将实际网页 HTTPS origin 加到 `AGENTCHAT_ORIGINS`。

服务器的 `.env` 应设置 `AGENTCHAT_SECURE_COOKIE=1`、`AGENTCHAT_ORIGINS=https://chat.example.com` 和 `AGENTCHAT_PUBLIC_API_URL=https://chat.example.com/agent-connect`。网页接入面板优先使用该公开 API 地址，覆盖旧浏览器缓存的内网地址，并支持直接复制。浏览器应使用 HTTPS 网址；HTTP 端口保留给内网反向代理。

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

API 容器以 UID/GID 1000 运行、根文件系统只读、移除 Linux capabilities；数据目录须允许此 UID 写入。部分 NAS 环境会拒绝带 `no-new-privileges` 的 Python 镜像执行，因此 Compose 默认不启用该选项；你的环境支持的话可以自行加上。

升级服务器后，旧 bridge 继续普通消息工作。待当前工作完成后重启本地 bridge 以加载新 transport 和 skill；不要为了升级终止正在工作的正式 session。

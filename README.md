# Codex Anywhere

通过统一网页远程使用自己电脑上的 Codex。管理中心使用 Python + Flask；每台 Windows、macOS 或 Linux 电脑运行 Python 节点，由节点启动本机的 `codex app-server --listen stdio://`。浏览器关闭或网络暂时中断后，节点仍继续运行。

```text
手机 / 电脑浏览器
       │ HTTPS / WebSocket
Flask 管理中心 ── 数据库、图片、历史、引用快照
       │ 节点主动建立 WebSocket
Python 节点 ── stdio JSON-RPC ── 本机 Codex ── 项目与工具
```

当前是 v0.1：单管理员、单管理中心进程。包含设备配对、项目根目录限制、会话列表、流式消息、历史、图片上传与显示、任务与网页引用、审批/问题交互、补充要求和中断。Codex 登录状态、项目文件和实际执行保留在节点。

本项目围绕 Codex 原生 Thread / Turn / Item 协议实现，不包含通用 Agent 适配层。协议约定见 [docs/protocol.md](docs/protocol.md)。

## 开发环境初始化

需要 Python 3.11+（建议 3.12）、Node.js 22.12+（建议 24）、npm 和 Git。运行节点的电脑还需安装 Codex CLI，并在同一个系统用户下完成登录。管理中心本身不需要 Codex。

仓库中的 `.venv`、`.env`、运行数据和前端依赖均已加入 Git 忽略列表。

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\codex-anywhere.exe init
Set-Location web
npm ci
npm run build
Set-Location ..
.\.venv\Scripts\codex-anywhere.exe server --host 127.0.0.1 --port 5000
```

macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/codex-anywhere init
cd web
npm ci
npm run build
cd ..
.venv/bin/codex-anywhere server --host 127.0.0.1 --port 5000
```

`init` 会创建 `.env` 并生成随机会话密钥和管理员密码；已有配置不会被无意覆盖。用生成的管理员账号登录 `http://127.0.0.1:5000`。不要提交或分享 `.env`。

本地默认使用 SQLite，首次启动自动创建表。配置项见 [.env.example](.env.example)：

| 配置 | 用途 |
| --- | --- |
| `SECRET_KEY` | Flask 会话签名密钥，需要长期保存 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 单管理员登录凭据 |
| `DATABASE_URL` | SQLite 或 PostgreSQL SQLAlchemy 连接串 |
| `ASSET_DIR` | 图片文件存储目录 |
| `COOKIE_SECURE` | HTTPS 部署时设为 `true` |
| `PUBLIC_URL` | 浏览器访问的完整外部 Origin，例如 `https://codex.example.com`；用于 WebSocket 来源校验 |

前端修改后重新运行 `npm run build`。使用 `npm run dev` 热更新开发时，将 `.env` 中的 `PUBLIC_URL` 设为实际 Vite 地址（例如 `http://localhost:5173`），重启 Flask 后从该地址访问。前后端开发、构建和检查脚本以 [web/package.json](web/package.json) 为准。

## 连接工作电脑

先在工作电脑检查 Codex：

```text
codex --version
codex login
```

在管理网页创建设备配对码，然后在工作电脑运行。`--root` 指定允许创建任务的目录，可重复传入。不要把系统盘、用户整个主目录作为默认授权范围。

Windows 示例：

```powershell
.\.venv\Scripts\codex-anywhere.exe node pair --server http://127.0.0.1:5000 --code YOUR_CODE --name windows-workstation --root F:\projects --state-dir .node-state
.\.venv\Scripts\codex-anywhere.exe node run --state-dir .node-state --codex-bin codex
```

macOS / Linux 示例：

```bash
.venv/bin/codex-anywhere node pair --server https://codex.example.com --code YOUR_CODE --name mac-workstation --root "$HOME/projects" --state-dir .node-state
.venv/bin/codex-anywhere node run --state-dir .node-state --codex-bin codex
```

配对码有过期时间且只能使用一次。节点凭据保存在 `--state-dir`，该目录也保存 SQLite 指令状态与事件发件箱，应只允许运行节点的用户读取。服务端地址使用管理中心的外部地址，不是工作电脑自身的 `localhost`。

HTTP 默认仅允许回环地址。隔离局域网中临时调试明文连接时可显式加 `--allow-insecure`；实际远程部署使用 HTTPS。节点主动连接管理中心，无需在工作电脑开放入站端口。

Codex 输出位于项目目录之外的图片，需要显式授权额外的工件根目录：

```bash
.venv/bin/codex-anywhere node run --state-dir .node-state --asset-root /absolute/path/to/codex/artifacts
```

`--asset-root` 可重复传入。它只扩大图片读取范围，不会扩大新任务的工作目录范围。相对图片路径按任务工作目录解析；路径校验仍受实际根目录约束。

## 使用聊天与引用

选择在线设备、填写其允许根目录内的工作目录后新建任务。消息发送到节点上的 Codex，网页展示回答、工具执行以及需要处理的审批或问题。任务执行中可以补充要求或中断。

- 图片：上传有效图片，或查看 Codex 生成、读取和工具返回的图片。上传限制为每张 20 MiB，图片通过需要登录的附件接口访问。
- 任务引用：选择已接入的任务，可引用具体消息。中心保存引用时的快照，将引用内容作为明确的上下文交给 Codex；卡片可跳转到原任务。
- 网页引用：提供 HTTP/HTTPS URL、标题和摘录，引用卡片打开原网页。中心不会抓取任意 URL；模型可见内容来自用户提交的摘录。Codex 自己搜索时的来源展示取决于其返回的数据。

引用快照不会随着被引用任务继续对话而悄悄变化。暂不自动导入 ChatGPT Web 聊天或接管官方桌面端 / IDE 正在运行的进程。

## Docker + PostgreSQL 部署

管理中心使用 Docker Compose，节点仍运行在各工作电脑上。先在仓库生成 `.env`，设置独立的数据库密码：

```bash
.venv/bin/codex-anywhere init
# 编辑 .env：HTTPS 部署设 COOKIE_SECURE=true 和 PUBLIC_URL=https://codex.example.com。
docker compose up -d --build
docker compose logs -f hub
```

镜像使用多阶段构建打包前端，Python 服务使用非 root 用户。入口脚本先执行 `codex-anywhere db-upgrade`，再启动 Gunicorn。数据库数据和图片分别存储在命名卷 `postgres_data`、`asset_data`。

管理端口仅发布到 `127.0.0.1:5000`。将 [deploy/Caddyfile.example](deploy/Caddyfile.example) 中的域名与邮箱替换为实际值，使用宿主机 Caddy 代理此地址。代理必须支持 WebSocket 升级并保持原始 Host / Origin；Caddy 的 `reverse_proxy` 支持 WebSocket。HTTPS 上线后将 `COOKIE_SECURE=true`、`PUBLIC_URL=https://你的域名`，并用 HTTPS 地址重新登录及配对。`PUBLIC_URL` 明确指定允许的浏览器来源，应用不信任任意转发头来推断来源。

使用其他反向代理时，为 `/ws/client` 和 `/ws/node` 保留长连接，不要缓存 API 或聊天事件。不要通过任意可控请求头重写鉴权依据。

**保持 Gunicorn 单 worker，禁止 `--preload`。** 当前节点连接、浏览器订阅与指令路由位于进程内，线程数用于支持并发 WebSocket。多个 worker 或多个管理中心实例需要额外的跨进程路由，目前没有实现。

升级前备份数据库、图片卷与 `.env`。单独升级数据库时运行：

```bash
.venv/bin/codex-anywhere db-upgrade
```

`docker compose down` 保留命名卷；删除卷会删除历史和图片。数据库与图片必须一起备份才能完整恢复对话中的工件。

## 断线、重试与状态

节点将观察到的事件先写入 SQLite 发件箱，管理中心提交数据库后确认；重连后补传未确认事件，中心按事件 ID 去重。浏览器按事件游标重新订阅并读取已持久化历史。

每条指令都有幂等 ID。已完成的重复投递返回缓存结果。如果节点进程在指令发出后崩溃，无法确定结果的指令标记为 `unknown`，不会自动重复执行；用户应先检查任务实际结果，再决定是否发起新的操作。断网时也不会承诺恢复尚未被节点观察、落盘的流式片段。

Codex 进程重启会生成新的运行实例 ID，旧实例的审批不能继续提交。断开网页连接不会终止节点或 Codex；停止节点进程则会停止它管理的 Codex 子进程。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check codex_anywhere tests
Set-Location web
npm run build
```

HTTP 测试覆盖登录/CSRF、配对、目录范围、引用、附件访问与输入校验、指令幂等。节点测试使用模拟 JSON-RPC 进程；这些检查不等同于所有版本 Codex 与所有操作系统的实际验收。

实际验收建议：手机登录 → 选择工作电脑项目 → 上传截图并引用已有任务 → 处理审批 → 接收图片 → 关闭网页后任务继续 → 重新打开恢复历史。另需测试节点断网重连和运行中进程重启。

## 当前边界

- 单管理员、单管理中心进程；没有多租户、团队 RBAC 或多实例指令路由。
- 节点以命令行前台进程运行；系统后台服务安装、自动更新与桌面托盘不在 v0.1 范围内。
- Codex App Server 协议会随 CLI 版本变化。上线前固定并验证实际使用的版本，不自动升级 CLI。
- 本系统新建、管理自己的 Codex 任务；不保证共享官方客户端的实时运行状态。
- 默认保留 Codex 的请求审批和工作区写入沙箱。具体沙箱能力取决于 Codex 与操作系统。
- 网页摘录由用户提供；没有任意 URL 抓取、ChatGPT 私有聊天自动同步或 Codex Cloud 任务接管。

规划中的后续工作包括跨版本协议兼容测试、系统服务安装、团队权限、工件生命周期以及多实例部署。

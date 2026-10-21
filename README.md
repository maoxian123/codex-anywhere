# Codex Anywhere

通过统一网页远程使用自己电脑上的 Codex。管理中心使用 Python + Flask；每台 Windows、macOS 或 Linux 电脑运行 Python 节点，由节点启动本机的 `codex app-server --listen stdio://`。浏览器关闭或网络暂时中断后，节点仍继续运行。

```text
手机 / 电脑浏览器
       │ HTTPS / WebSocket
Flask 管理中心 ── 数据库、图片、历史、引用快照
       │ 节点主动建立 WebSocket
Python 节点 ── stdio JSON-RPC ── 本机 Codex ── 项目与工具
```

当前应用版本为 v0.1，使用 v2 项目与历史协议：单管理员、单管理中心进程。包含设备配对、按设备和目录组织的项目、本机已有 Codex 对话导入、流式消息、图片上传与显示、任务与网页引用、审批/问题交互、补充要求和中断。Codex 登录状态、项目文件和实际执行保留在节点。

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

在管理网页创建设备配对码，然后在工作电脑运行。配对无需预先指定项目目录；网页可以浏览或选择节点系统账号有权访问的任意绝对目录。

Windows 示例：

```powershell
.\.venv\Scripts\codex-anywhere.exe node pair --server http://127.0.0.1:5000 --code YOUR_CODE --name windows-workstation --state-dir .node-state
.\.venv\Scripts\codex-anywhere.exe node run --state-dir .node-state --codex-bin codex
```

macOS / Linux 示例：

```bash
.venv/bin/codex-anywhere node pair --server https://codex.example.com --code YOUR_CODE --name mac-workstation --state-dir .node-state
.venv/bin/codex-anywhere node run --state-dir .node-state --codex-bin codex
```

配对码有过期时间且只能使用一次。节点凭据保存在 `--state-dir`，该目录也保存 SQLite 指令状态与事件发件箱，应只允许运行节点的用户读取。服务端地址使用管理中心的外部地址，不是工作电脑自身的 `localhost`。

HTTP 默认仅允许回环地址。隔离局域网中临时调试明文连接时可显式加 `--allow-insecure`；实际远程部署使用 HTTPS。节点主动连接管理中心，无需在工作电脑开放入站端口。

项目目录与图片工件均不设目录白名单，也没有额外的工件目录参数。相对图片路径按任务工作目录解析；绝对图片路径可位于该账号有权读取的任意目录。Codex 执行仍使用原生 `on-request` 审批与 `workspace-write` 沙箱。

## 项目与本机已有对话

项目由“设备 + 规范化后的目录”确定，同一路径在不同设备上属于不同项目，Windows 路径按大小写不敏感处理。选择在线设备后，可以浏览其主目录、磁盘和子目录，或直接输入绝对路径。选择已有目录即可打开项目；需要新目录时显式选择创建，节点才会创建该目录。新建任务可以选择项目，直接填写工作目录也会自动归入对应项目。

在设备的已有对话目录中，可以搜索并分页查看 Codex CLI、IDE、桌面/App Server 及子任务的本地历史，也可以切换归档筛选或按工作目录过滤。选中对话后导入，中心保存原会话的消息、工具结果和顺序，并按原工作目录组织到项目中。导入期间任务显示为导入中，完成后才能继续发送消息；正在运行或等待交互的已管理任务不能被重新导入覆盖。

导入只读取本机 Codex 历史，不会恢复执行或发起模型推理。用户之后发送消息时，节点恢复同一个原生会话 ID，保留其上下文。相同设备上的同一原生会话重复导入会更新同一条管理记录。导入历史中的用户图片、工具图片和本地图片链接使用同一工件通道；HTTP/HTTPS 图片链接保留为链接，不在后台自动下载。

已有对话列表和导入依赖运行节点的系统账号所使用的 Codex 配置与本地历史。导入保留当时读取的记录，不保证与官方客户端实时双向同步，也不接管其他客户端正在运行的进程。

## 使用聊天与引用

选择在线设备和项目后新建任务，或继续已完成导入的本机对话。消息发送到节点上的 Codex，网页展示回答、工具执行以及需要处理的审批或问题。任务执行中可以补充要求或中断。

- 图片：上传有效图片，或查看 Codex 生成、读取和工具返回的图片。每张图片最多 20 MiB、2500 万像素；节点对每项工具或消息输出最多提取 16 张、合计 40 MiB，超出部分跳过并记录日志。图片通过需要登录的附件接口访问。回答中已经嵌入的图片不会在同条消息的图库重复展示；其他附件与工具图片仍保留在图库中。
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

### 从旧版升级到 v2

管理中心、网页构建和所有节点都需要一起升级。停止旧管理中心和节点，备份数据库、图片、`.env` 与节点状态目录；更新代码并重新安装 Python 依赖、构建前端，然后执行 `codex-anywhere db-upgrade`，再启动管理中心和节点。Docker 部署重新构建镜像，入口会先运行同一迁移命令。

迁移会将已有管理任务按设备和目录归入项目，并添加导入状态和稳定的历史顺序。节点继续使用原状态目录，保留设备 ID、令牌与指令日志，无需重新配对；旧配置中的 `roots` 字段在加载时移除。旧版目录相关 CLI 选项已删除，启动命令使用上方示例。升级不会自动读取或导入整台电脑的 Codex 历史；已有对话由用户在网页选择后导入。

## 断线、重试与状态

节点将观察到的事件先写入 SQLite 发件箱，管理中心提交数据库后确认；重连后补传未确认事件，中心按事件 ID 去重。浏览器按事件游标重新订阅并读取已持久化历史。

每条指令都有幂等 ID。已完成的重复投递返回缓存结果。如果节点进程在指令发出后崩溃，无法确定结果的指令标记为 `unknown`，不会自动重复执行；用户应先检查任务实际结果，再决定是否发起新的操作。断网时也不会承诺恢复尚未被节点观察、落盘的流式片段。

Codex 进程重启会生成新的运行实例 ID，旧实例的审批不能继续提交。断开网页连接不会终止节点或 Codex；停止节点进程则会停止它管理的 Codex 子进程。

执行错误及其附加说明会保留在界面中，成功的后台列表刷新不会将错误提示清除。任务最近一次错误持久化保存，重新打开任务仍可查看；开始新的执行后按最新任务状态更新。登录会话过期时返回登录页，重新登录后可继续读取历史。

Codex 声明会自动重试的临时错误不会将任务标为失败。迟到的旧指令结果不会覆盖新轮次；多个审批同时等待时，任务会保持等待，直到所有请求处理完毕。补充要求中的原文和引用也会绑定到对应消息。

注销会同时撤销当前会话的 Cookie 和已有网页长连接。管理中心重启后需要重新登录；节点运行不依赖浏览器登录会话。缺失或越界附件会返回明确错误，可重新上传后再发送。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check codex_anywhere tests scripts
Set-Location web
npm test
npm run build
```

HTTP 与节点测试覆盖登录/CSRF、配对、目录浏览与创建、项目归组、已有对话列表和只读导入、引用、附件访问与输入校验、指令幂等。前端组件测试覆盖发送/补充/中断、离线草稿、会话过期与重连、引用、图片展示和错误保留。常规 `pytest` 的节点测试使用模拟 JSON-RPC 进程，CI 不调用真实模型，也不消耗模型额度；这些检查不等同于所有版本 Codex 与所有操作系统的实际验收。

### 使用真实 Codex 验证

[scripts/live_smoke.py](scripts/live_smoke.py) 可重复运行真实端到端测试。它使用当前系统用户下已经安装并登录的 Codex CLI，**会发起真实推理并消耗模型额度**。脚本启动隔离的临时管理中心（默认 `http://127.0.0.1:5010`）、节点和测试工作目录，不使用日常项目；运行前确保该端口空闲。

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe scripts/live_smoke.py --hold --timeout 180
```

macOS / Linux：

```bash
.venv/bin/python scripts/live_smoke.py --hold --timeout 180
```

检查范围包括：

1. 真实模型问答与回答回传。
2. 上传图片并由模型识别。
3. 工具图片上传与回传。
4. 大于 1 MiB 的图片传输。
5. 任务引用与用户提供的网页摘录进入模型上下文。
6. 网络断线后恢复连接，并保持同一 Codex 进程 PID。
7. 运行中补充要求（steer）。
8. 中断当前执行（interrupt）。
9. Codex 原生网页搜索。

每次运行的报告保存在 `instance/live-tests/<id>/report.json`，具体结果以该次报告为准。`--hold` 会在检查后保留测试服务，便于打开网页继续检查图片、引用与历史；按 `Ctrl+C` 结束测试并停止测试进程。该脚本需手动执行，常规测试与 CI 不会自动运行它。

已完成的 Windows / Codex 0.163.0 实测及测试边界见 [2026-10-20 验收记录](docs/verification-2026-10-20.md)。

项目与历史导入的最新检查见 [2026-10-21 验收记录](docs/verification-2026-10-21.md)。可用以下命令单独复测已有测试会话的导入、图片和原会话续聊；最后一步会发起一次真实推理：

```powershell
.\.venv\Scripts\python.exe scripts/live_smoke.py --projects-only --import-native-id EXISTING_NATIVE_THREAD_ID --port 5011
```

可选 `--expected-context 历史校验串` 验证续聊能回忆原会话上下文。请选择用于验收的会话；该步骤会向原生会话追加一轮消息。

实际验收建议：手机登录 → 选择工作电脑项目 → 上传截图并引用已有任务 → 处理审批 → 接收图片 → 关闭网页后任务继续 → 重新打开恢复历史。另需测试节点断网重连和运行中进程重启。

## 当前边界

- 单管理员、单管理中心进程；没有多租户、团队 RBAC 或多实例指令路由。
- 节点以命令行前台进程运行；系统后台服务安装、自动更新与桌面托盘不在 v0.1 范围内。
- Codex App Server 协议会随 CLI 版本变化。上线前固定并验证实际使用的版本，不自动升级 CLI。
- 支持新建任务以及选择、导入本机已有 Codex 对话；不保证共享官方客户端的实时运行状态。
- 默认保留 Codex 的请求审批和工作区写入沙箱。具体沙箱能力取决于 Codex 与操作系统。
- 网页摘录由用户提供；没有任意 URL 抓取、ChatGPT 私有聊天自动同步或 Codex Cloud 任务接管。

规划中的后续工作包括跨版本协议兼容测试、系统服务安装、团队权限、工件生命周期以及多实例部署。

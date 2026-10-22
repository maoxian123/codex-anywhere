# 生产服务器更新

用户的长期要求：代码更新在本地开发阶段完成测试，提交并推送后同步更新这台服务器并重启现有服务。生产服务器只做部署和运行健康确认，不运行测试。更新时先核对实际部署状态；若部署方式发生变化，先查清再操作。

## 已确认的部署结构

| 项目 | 当前配置 |
| --- | --- |
| SSH | `root@140.143.132.32:22`；已配置的本机可直接 `ssh 140.143.132.32` |
| 源码 | `/root/codex-anywhere`，`main` 分支，`origin` 指向 GitHub 仓库 |
| Python 运行环境 | `/opt/codex-anywhere/.venv`，安装构建后的项目 wheel |
| 前端运行文件 | `/opt/codex-anywhere/web/dist`，环境变量 `WEB_DIST` 指向此目录 |
| systemd 服务 | `codex-anywhere.service` |
| 服务账户 | `User=codex-anywhere`、`Group=codex-anywhere` |
| 工作目录 | `/var/lib/codex-anywhere` |
| 环境配置 | `/etc/codex-anywhere.env` |
| 启动前迁移 | `ExecStartPre=/opt/codex-anywhere/.venv/bin/python -m codex_anywhere db-upgrade` |
| 应用进程 | Gunicorn，1 个 worker、64 个 threads、timeout 120 秒，监听 `127.0.0.1:5001` |
| 外部入口 | Nginx，`https://140.143.132.32:5000`，反向代理到本机 5001 端口 |
| 数据库 | `sqlite:////var/lib/codex-anywhere/codex-anywhere.db` |
| 附件 | `/var/lib/codex-anywhere/assets` |
| 健康端点 | `/healthz` |

只在源码目录执行 `git pull` 不会更新运行中的 Python 包或前端。服务器没有 Node.js/npm；前端必须在本地构建后上传。生产配置、SSH 私钥、数据库、附件、虚拟环境和生成文件均不得提交到 Git。

## 更新流程

1. **本地验证并确定版本。** 按改动完成必要验证：Windows 使用 `.venv/Scripts/python.exe -m pytest` 和 `.venv/Scripts/python.exe -m ruff check codex_anywhere tests`；Linux 使用 `.venv/bin/python`。前端在 `web/` 执行相应测试及 `npm run build`。同一已验证版本没有代码变化时不重复测试。确认本地工作区干净，记录准备部署的提交 SHA，并将提交推送到 `origin/main`。
2. **本地生成发布文件。** 为该提交生成纯 Python wheel：在项目根目录运行 `.venv/Scripts/python.exe -m pip wheel --no-deps --wheel-dir <本地发布目录> .`，保留默认的构建隔离。将前端打包：`tar -czf <本地发布目录>/frontend.tar.gz -C web/dist .`。通过 `scp` 上传到服务器上的独立临时发布目录，并核对文件校验值和提交 SHA。不要上传 Windows 虚拟环境。
3. **只读核对服务器。** 检查 `/root/codex-anywhere` 的分支、`git status`、当前提交，读取 `systemctl cat codex-anywhere.service` 和 Nginx 的相关配置，确认仍使用上述布局。不要在输出中暴露环境文件内的密钥。工作区有改动或分支分叉时保留现有数据，先查清；正常更新使用 `git pull --ff-only origin main`，确认结果与目标 SHA 一致。
4. **准备回滚备份。** 在 root 专用目录 `/root/codex-anywhere-backups/<时间戳>` 保存旧提交信息、`/opt/codex-anywhere`、`/etc/codex-anywhere.env`、当前 systemd unit 及相关 Nginx 配置。备份目录限制为 root 可访问。部署包和初步备份准备好后短暂停止 `codex-anywhere.service`，确认已停止，再完整备份 `/var/lib/codex-anywhere`；数据库和可能存在的 SQLite WAL/SHM 文件必须一起保存，附件也包含在内。
5. **替换运行文件。** 在现有 `/opt/codex-anywhere/.venv` 内使用 `bin/python -m pip install --no-deps --force-reinstall <上传的项目wheel>`。将上传的前端构建替换到 `/opt/codex-anywhere/web/dist`，保持服务账户可读取目录和文件。不要更改现有环境配置、数据路径、服务账户或进程管理方式。普通代码更新无须重启 Nginx。
6. **启动并确认健康。** `systemctl start codex-anywhere.service`（未经过停服阶段时使用 `restart`）会先执行现有 `ExecStartPre` 数据迁移，再启动 Gunicorn。检查服务状态、近期日志、`http://127.0.0.1:5001/healthz` 和外部 HTTPS 入口；确认首页与引用的静态文件可以返回。核对安装包、前端文件和源码对应同一发布版本，向用户报告部署的 SHA、服务状态及备份位置。

当前发布若未改依赖，复用服务器现有依赖即可。未来 `pyproject.toml` 或 `requirements.lock` 的运行依赖发生变化时，在替换代码前按锁文件为 Linux 运行环境安装所需依赖，核对 Python 版本、平台条件和可用的 Linux wheel；不要把 Windows 平台的 wheel 或虚拟环境当成 Linux 运行环境复制上去。`--no-deps` 只负责安装项目包，不能代替依赖更新。

## 生产确认与失败恢复

生产允许使用 `systemctl`、近期服务日志、`/healthz`、首页和静态文件响应等部署健康检查。禁止在服务器执行 `pytest`、`ruff`、`npm test`、真实推理脚本或其他开发测试；不要为确认部署而发送 Codex 消息、创建会话或改动用户业务数据。服务器启动时现有数据库迁移属于正常部署流程。

若安装、迁移或启动失败，保留错误日志和本轮发布文件，避免反复重启或自动重复未知结果的操作。停服后依据备份恢复旧运行包、前端和必要配置；迁移已经修改数据时，先判断兼容性，必要时连同停服时备份的完整数据目录一起恢复，再启动旧版本。不要将旧代码直接配上不兼容的新数据库，也不要在回滚过程中覆盖未备份的数据。


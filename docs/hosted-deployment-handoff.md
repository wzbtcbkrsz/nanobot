# 托管部署交接：启动流程与 PikPak 持久化

> [!NOTE]
> 本文是自包含的运维交接文档，可整篇复制给其他 agent。文中所有仓库、镜像、组织标识均为占位符（`<owner>` / `<repo>`），**不包含任何凭据、token、真实域名、实例 ID、提交哈希、镜像 digest 或环境专有路径**。替换占位符并填入自己的 secrets 后即可使用。详见文末「敏感数据说明」。

## 1. 适用范围与目标

- 目标：把 nanobot 作为 **Koyeb Web 服务**运行（对外 HTTP/WebSocket、通过平台健康检查），并在免费 tier（无持久卷）上用 PikPak 网盘持久化 config / sessions / memory / WebUI 历史。
- 仓库与镜像：`github.com/<owner>/<repo>`（`main` 分支）、`ghcr.io/<owner>/<repo>:latest`。请按实际 fork 替换占位符。
- 镜像构建：`.github/workflows/docker-image.yml`，**仅手动 dispatch**（`push_image=true`、`tag_latest=true`）。Koyeb 不会自动重新拉取 `:latest`；每次推送镜像后需手动 Redeploy，并核对实例实际 digest 与最新构建一致（平台对 `:latest` 有缓存）。
- 核心约束：网关必须在健康检查宽限期内绑定 `0.0.0.0:8765`；WebUI 资源打包在镜像内；provider / model / workspace 的安全默认值不能被持久化的旧配置破坏。
- 核心结论：**启动关键路径上不做任何 PikPak 网络操作**；状态在网关起来后由后台任务恢复，恢复完成后再重启一次网关；所有回传由「启动完成」闸门控制。

## 2. 关键文件

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 从 `rclone/rclone:1.76.0` COPY 静态 rclone；`USER root` + `HOME=/home/nanobot`；`ENTRYPOINT ["entrypoint.sh"]`；`CMD ["webui","--yes","--no-open"]`；`ENV PIKPAK_SYNC_INTERVAL=2700`；`EXPOSE 18790 8765` |
| `entrypoint.sh` | 启动总控：平台模板判定 → PikPak bootstrap → 降权 re-exec → config 播种 → 网关启动/重启 → 退出收尾 |
| `pikpak-storage.sh` | 被 source 的 POSIX sh 函数库（登录、拉取、上传、闸门、legacy 软链） |
| `koyeb-config.json` / `render-config.json` | 平台配置模板，`${VAR}` 引用在启动时由 nanobot 解析 |
| `koyeb-compose.yaml` | Koyeb 描述符：WEB service、8765/http、route `/`、TCP 健康检查、env/secrets |
| `scripts/merge_platform_config.py` | 恢复 `config.json` 后，把平台字段强制覆盖回模板值、保留用户设置；Dockerfile COPY 到 `/app/` |
| `docker-compose.yml` / `.env.example` | 本地 Compose 运行 |
| `docs/deployment.md` | 部署与排障文档 |
| `tests/test_docker_config.py`、`tests/scripts/test_merge_platform_config.py` | 契约测试 |
| `scripts/ci_scope.py`、`tests/scripts/test_ci_scope.py` | CI 路径规则（新增脚本/测试要归入 DOCKER 规则；`docs/*` 归为 documentation only） |

## 3. 环境变量

| 变量 | 说明 |
|---|---|
| `KOYEB_PUBLIC_DOMAIN` | Koyeb 自动注入，用来判定「hosted web service」；也可显式设 `KOYEB=true` |
| `RENDER` | `true` 时选 Render 模板 |
| `PORT` | Koyeb 自动注入，值为最低暴露端口（8765）；模板里 `websocket.port = ${PORT}` |
| `ANTHROPIC_API_KEY` | LLM key，模板中以 `${ANTHROPIC_API_KEY}` 引用 |
| `NANOBOT_WEB_TOKEN` | WebUI 登录口令，模板中 `tokenIssueSecret=${NANOBOT_WEB_TOKEN}` |
| `PIKPAK_USER` / `PIKPAK_PASSWORD` | **两者都设置才启用 PikPak**；用平台 secrets 注入 |
| `PIKPAK_SYNC_INTERVAL` | 周期上传间隔，默认 `2700`（45 分钟） |
| `PIKPAK_PULL_TIMEOUT` | 后台拉取硬上限，默认 `120` 秒 |
| `PIKPAK_LOCAL_ROOT` / `PIKPAK_REMOTE_PATH` | 默认 `/data/nanobot-home` / `nanobot/.nanobot` |
| `PIKPAK_SYNC_LOOP` | `0` 关闭周期上传 |
| `NANOBOT_LOG_LEVEL=WARNING` | 镜像默认，INFO 级 `WebSocket server listening` 会被吞掉 |
| `NANOBOT_MERGE_SCRIPT` | 可选，覆盖合并脚本路径（测试用） |

`koyeb-compose.yaml` 中使用 `{{secret.xxx}}` 占位引用，且 `no_deploy_on_push: true`，推送 commit 不会自动重建服务。缺 `ANTHROPIC_API_KEY` 或 `NANOBOT_WEB_TOKEN` 会启动报 `missing environment variable reference(s)`。

## 4. 平台配置模板要点

```json
{
  "agents": {"defaults": {"model": "anthropic/claude-opus-4-8", "provider": "auto"}},
  "providers": {"anthropic": {"apiKey": "${ANTHROPIC_API_KEY}"}},
  "gateway": {"host": "127.0.0.1", "port": 18790},
  "channels": {"websocket": {
    "enabled": true, "host": "0.0.0.0", "port": "${PORT}",
    "publicWsUrl": "wss://${KOYEB_PUBLIC_DOMAIN}/",
    "tokenIssueSecret": "${NANOBOT_WEB_TOKEN}", "websocketRequiresToken": true
  }},
  "tools": {"restrictToWorkspace": true, "webuiAllowRemotePackageInstall": false, "my": {"allowSet": false}}
}
```

- `gateway` 是内部 API（`127.0.0.1:18790`），`channels.websocket` 才是对外端口（`0.0.0.0:8765`）。
- Koyeb 在边缘终止 TLS 且**不转发 `x-forwarded-proto`**，所以 `publicWsUrl` 必须钉死 `wss://`，否则 HTTPS 页面会去连明文 `ws://`。
- 模板中的 `${VAR}` 引用在启动时由 nanobot 的配置加载器解析，因此密钥以环境变量注入、不落盘到模板文件。

## 5. 启动流程（逐步）

0. 镜像 `ENTRYPOINT entrypoint.sh`，默认 `CMD ["webui","--yes","--no-open"]`。这个默认 CMD 是交互式桌面启动器，会把网关绑在 `127.0.0.1` 并作为后台子进程，导致外部 TCP 健康检查失败。
1. **平台模板判定（必须在 bootstrap 之前）**：`RENDER=true` → `render-config.json`；`KOYEB=true` 或 `KOYEB_PUBLIC_DOMAIN` 非空 → `koyeb-config.json`。命中平台模板时 `export PIKPAK_DEFER_PULL=1`（走「延迟拉取」流程）。`template_dir` 默认 `/app`，可用 `NANOBOT_CONFIG_TEMPLATE_DIR` 覆盖。
2. **PikPak 可选启用**（两个凭据都在时）：source `pikpak-storage.sh`；`NANOBOT_PIKPAK_BOOTSTRAPPED` 为空时调 `pikpak_bootstrap()`，失败 `exit 1`。bootstrap 只做本地准备（路径、mkdir、重置闸门、export 变量、legacy 软链）；**平台模式不做任何网络操作**；本地 Compose 模式在此同步 login+pull，失败 fail-closed。
3. **降权 re-exec**：root 启动时 `chown` 数据目录后 `exec setpriv --reuid=nanobot --regid=nanobot --init-groups "$0" "$@"`。`NANOBOT_PIKPAK_BOOTSTRAPPED=1` 保证 bootstrap 只跑一次；所有 `export` 的变量通过 exec 环境继承到第二遍。
4. **config 播种**（平台模式）：`config="$HOME/.nanobot/config.json"`，**仅当文件不存在时** `cp` 模板；若 argv 恰为 `webui --yes --no-open` 则归一化成 `gateway`；追加 `--config "$config"`。本地 Compose 走 `python -m nanobot.cli.docker_bootstrap` 生成初始 config。
5. **启动网关①**：`pikpak_start_sync_loop`（周期上传循环，此刻闸门未开，调用是 no-op）→ `nanobot "$@" &` → 设置 TERM/INT trap。此时 8765 立即可用，健康检查通过。
6. **后台状态恢复**（仅 `PIKPAK_DEFER_PULL=1`）：`pikpak_start_deferred_pull` 起后台子 shell（`sleep 2` 让网关①先绑定 → `pikpak_login` → `pikpak_pull_state`），任一失败 `exit 1`；主 shell `wait "$PIKPAK_PULL_PID"` 记录 `pull_ok`；运行 `python /app/merge_platform_config.py <模板> <config>` 合并并记录 `merge_ok`，失败则回退 `cp` 模板；非关停且网关①仍活着时 `kill -TERM` + `wait` → 重新 `nanobot "$@" &`（**网关②，这才是真正完成的启动**）；仅当 `pull_ok=1 && merge_ok=1` 时调 `pikpak_mark_ready()` 打开上传闸门。
7. **本地/Compose 模式**（非 defer）：bootstrap 已同步拉取完成，直接 `pikpak_mark_ready()`。
8. **运行期**：主 shell `wait` 网关②；周期循环每 2700 秒调 `pikpak_sync_up`（闸门开时才真正上传）。
9. **关停**：SIGTERM → trap kill 网关 → `pikpak_final_sync()`（kill 周期循环 PID → 一次 `pikpak_sync_up`，同样受闸门）→ `exit`。

## 6. PikPak 持久化机制

### 6.1 数据布局

- `pikpak_bootstrap` 把 `HOME` **export 成 `PIKPAK_LOCAL_ROOT`（`/data/nanobot-home`）**；nanobot 全部状态由 `$HOME/.nanobot` 派生（`nanobot/config/paths.py`）。
- `PIKPAK_DATA_DIR = /data/nanobot-home/.nanobot`；`PIKPAK_REMOTE_SPEC = pikpak:nanobot/.nanobot`。
- `PIKPAK_CONF_DIR = /tmp/nanobot-pikpak`，内含 `rclone.conf`（`RCLONE_CONFIG` 指向）、`cache/`、`sync.log`、`startup-complete`（闸门标记）。
- `pikpak_link_legacy_home()` 把 `/home/nanobot/.nanobot` 软链到 `$PIKPAK_DATA_DIR`（仅当该目录为空，绝不覆盖已有卷）。
- 覆盖：`config.json`、sessions、memory、cron、media、WebUI 历史；**`logs/` 双向排除**（每次重启重生成且无限增长）。

### 6.2 函数表（`pikpak-storage.sh`）

| 函数 | 行为 |
|---|---|
| `pikpak_login()` | `rclone obscure` 密码 → `rclone config create pikpak pikpak user ... pass ...` 真实登录（device id / captcha / OAuth）→ `rclone mkdir` |
| `pikpak_pull_state()` | `timeout 120 rclone copy remote→local --exclude "/logs/**" --retries 3 --timeout 60s --transfers 4 --checkers 8` |
| `pikpak_sync_up()` | **先检查闸门**；`rclone copy local→remote --exclude "/logs/**" ...`（`config.json` 不排除） |
| `pikpak_start_deferred_pull()` | 后台子 shell 执行 login+pull，失败 `exit 1` |
| `pikpak_mark_ready()` | `: > $PIKPAK_READY_FILE` 打开闸门 |
| `pikpak_start_sync_loop()` | 后台 `while: sleep 2700; pikpak_sync_up` |
| `pikpak_final_sync()` | 退出前最后一次上传 |

### 6.3 rclone 语义（重要）

- 免费 tier 无 `/dev/fuse` → 用 `rclone copy` 而非 mount。
- `copy` 是**文件级增量**（按 size+mtime 比对，只传变化文件）、**只增不删**、**同名整文件覆盖**；每次仍需全目录列举比对；远端不保留历史版本，覆盖即覆盖。
- 必须 `rclone config create` 真实登录，只用 `RCLONE_CONFIG_PIKPAK_*` 环境变量会报 `empty token found - please run rclone config reconnect`。
- 用 `copy` 不用 `sync`：本地临时故障不会清空远端；代价是本地删除的项下次拉取会「复活」。

### 6.4 上传闸门（防止配置被覆盖的核心）

- 用**标记文件**而非环境变量：后台同步子 shell 在启动早期就 fork 了，父 shell 之后 export 的值传不进去。
- `pikpak_bootstrap` 每次启动先 `rm -f startup-complete`（上传默认关闭）。
- `pikpak_sync_up()` 开头：`[ -f "$PIKPAK_READY_FILE" ] || { log "upload skipped: startup not complete"; return 0; }`。
- 只有「拉取成功 → 合并成功 → 重启成功」后才开闸；任一失败或收到关停信号，闸门永不打开 → **整个实例生命周期不回传任何数据**。周期上传和 final_sync 都走这个函数，所以启动完成前完全不写远端。

### 6.5 配置合并（`scripts/merge_platform_config.py`）

- `merge(template, restored)`：`_overlay(template, restored)` = 以恢复的配置为准、用模板补全缺失键；然后按 `PLATFORM_PATHS` 白名单强制写回模板值。
- 白名单：`gateway.host`；`channels.websocket.{enabled,host,port,publicWsUrl,tokenIssueSecret,websocketRequiresToken}`；`tools.{restrictToWorkspace,webuiAllowRemotePackageInstall,my.allowSet}`。
- **不碰** `agents.defaults.model` 和 `providers.*` → 用户在 WebUI 改的模型 / provider 能恢复。
- 原子写入（`.tmp` + `os.replace`）；合并失败时 entrypoint 回退 `cp` 模板，但**不打开闸门**。

## 7. 关键设计决策与历史问题（迁移时务必保留）

1. **默认 CMD 陷阱**：`webui --yes --no-open` 绑定 `127.0.0.1` 且网关是后台子进程，外部 TCP 健康检查必失败。entrypoint 对精确默认 argv 归一化为 `gateway`；显式 command 优先。
2. **健康检查宽限期 vs 启动拉取**：早期把 pull 放在启动关键路径，PikPak 卡住会吃满宽限期 → 探测失败 → 重启 → 再拉 → 死亡螺旋。现在启动零 PikPak 网络操作；descriptor 仍保留 `grace_period: 300`、`restart_limit: 5` 作为余量（重启②会短暂断开端口）。
3. **配置被模板反向覆盖（曾导致 WebUI 配的模型全丢）**：上传侧不排除 `config.json`，而某几版启动把本地 config 重置成模板，周期 / final 上传就把模板推上去覆盖远端用户配置。修法 = 上传闸门 + 合并脚本。教训：**以模板启动的半成品实例绝不能写远端**。
4. **从「排除恢复 config」改回「恢复 + 合并」**：曾为防旧配置绑回 `127.0.0.1` 而排除 `config.json`，结果丢了用户设置，且上传不对称反而加速覆盖。现在是「恢复 + 白名单强制覆盖平台字段」。
5. **降权 re-exec**：`NANOBOT_PIKPAK_BOOTSTRAPPED` 防止 bootstrap 跑两次；所有变量必须 `export` 才能跨 exec 和传给后台子 shell。
6. **日志排除**：`--exclude "/logs/**"` 在 pull 和 push 两侧都要有。
7. **最终一致性**：45 分钟窗口；优雅关闭有 `final_sync`，硬杀 / 重建会丢最近 45 分钟写入；SQLite 是运行中复制，只能当单实例备份，**只跑一个 replica**。
8. **descriptor 不自动生效**：Koyeb 服务拉的是预构建 `:latest`，`koyeb-compose.yaml` 里的 env / health check 改动不会自动应用；控制台参数要手动改，镜像要手动 redeploy 并**核对 digest**。
9. **测试必须以非 root 跑**：用例带 `@pytest.mark.skipif(geteuid()==0)`，root 会走 setpriv 路径导致 PikPak / seeding 用例被 skip。

## 8. 验证与运维

```bash
# shell 语法
sh -n pikpak-storage.sh && sh -n entrypoint.sh

# 非 root 跑关键契约测试（PikPak / 闸门 / 合并 / 重启）
PYTHONDONTWRITEBYTECODE=1 setpriv --reuid=65534 --regid=65534 --clear-groups \
  python -m pytest tests/test_docker_config.py -q -p no:cacheprovider

# root 跑全套
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/test_docker_config.py tests/scripts/test_ci_scope.py \
  tests/scripts/test_merge_platform_config.py -q -p no:cacheprovider

# 重建并推送镜像（手动 dispatch，云端构建）
gh workflow run docker-image.yml --ref main -f push_image=true -f tag_latest=true
gh run watch <run-id> --exit-status
```

**成功启动的日志标志**：`[entrypoint] initializing /data/nanobot-home/.nanobot/config.json from koyeb-config.json` → `Using config: ...` → `[pikpak] deferred PikPak pull started (pid N)` → `[pikpak] state restore finished — restarting the gateway` → 第二次 `Using config: ...`（网关②，完整状态）。`[pikpak] pulling ...` 在后台任务的 `sync.log` 里，不在主日志；`WebSocket server listening` 是 INFO 级，被 `WARNING` 日志级别吞掉。

**异常标志**：`TCP health check failed on port 8765`、`[pikpak] warning: ... timed out after 120s`、`upload skipped: startup not complete` 之后长时间没有后续上传。`final upload before exit` 表示走了优雅关闭。

## 9. 迁移到新 agent / 新部署的最小步骤

1. 准备平台 secrets：`anthropic-api-key`、`nanobot-web-token`（`openssl rand -hex 32`）、`pikpak-user`、`pikpak-password`。
2. 用 `koyeb compose ./koyeb-compose.yaml` 或在控制台建 **Web** 服务：镜像 `ghcr.io/<owner>/<repo>:latest`，entrypoint `/usr/local/bin/entrypoint.sh`，command `gateway`，端口 `8765`/http，route `/`，TCP 健康检查（`grace_period: 300`、`interval: 30`、`timeout: 5`、`restart_limit: 5`）。
3. 注入第 3 节的环境变量；确认 `KOYEB_PUBLIC_DOMAIN` 与 `PORT` 由平台自动提供。
4. 推送镜像后**手动 Redeploy**，并核对实例实际 digest 与最新构建一致（平台对 `:latest` 有缓存）。
5. 用公网域名 + `NANOBOT_WEB_TOKEN` 登录 WebUI（不要用 `webui` 打印的 loopback 链接）。
6. 若实例反复重启：先删掉 `PIKPAK_USER` / `PIKPAK_PASSWORD` 两个 env 隔离问题；再检查 PikPak 里是否残留旧的 `config.json`（合并脚本会保护平台字段，但旧数据仍可能干扰）。
7. 若需清理远端历史日志：`rclone purge pikpak:nanobot/.nanobot/logs`。

## 10. 敏感数据说明

本文刻意**不包含**以下内容，复制与分享时无需再脱敏；部署时请自行在平台 secrets 中提供：

- 任何 API key、WebUI token、PikPak 账号密码、`rclone.conf` 中的 OAuth token 或 device id 的实际值（文中只有变量名与 `${VAR}` / `{{secret.xxx}}` 占位符）。
- 真实 Koyeb 域名、组织 ID、实例 ID、chat_id / session_id、GitHub owner / 仓库名（统一为 `<owner>` / `<repo>`）、提交哈希、镜像 digest、workflow run id。
- 任何宿主机 / 容器内专有路径（如 NFS 挂载根、工作区绝对路径）、内部主机名、日志片段中的真实错误体。
- 用户在 WebUI 里配置过的具体模型名、provider base_url、自定义密钥等值。

替换占位符、填入 secrets 后即可使用；**不要把填好真实值的副本提交回仓库或贴到公开 issue**。`.env` 已被 git 忽略并排除在镜像构建之外，凭据只应通过平台 secrets 注入。

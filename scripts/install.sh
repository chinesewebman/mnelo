#!/usr/bin/env bash
# mnelo/install.sh — one-command install for local-first memory layer
#
# 用法 (在刚 clone 出来的 mnelo 目录里跑):
#   bash scripts/install.sh                    # 默认装到 ~/.hermes/memory
#   LIVE_ROOT=~/.mnelo bash scripts/install.sh # 装到新位置
#
# 步骤:
#   1. 检查 Python 3.9+ / git / curl
#   2. 创建 venv (如果没)
#   3. pip install -r requirements.txt
#   4. (可选) 下载 bge-small-zh-v1.5 模型 (~92 MB, 避免首次 recall 冷启动)
#   5. python scripts/init_db.py
#   6. 装服务守护: macOS → launchd plist + launchctl load
#      Linux → systemd unit (root 系统级 / 非 root 用户级 + linger)
#   7. 跑 health_check.py 验证
#
# 设计原则:
#   - idempotent: 可重复跑, 已装的步骤会跳过
#   - 失败早退 (set -euo pipefail)
#   - 文件权限 0600/0700 (P0 安全, 防其他 user 读 KG / schema / config)
#
set -euo pipefail

umask 077  # [P0-1] 默认 0600/0700, 防止其他本地 user 读 mnelo 数据

# ---- 配置 ----
LIVE_ROOT="${LIVE_ROOT:-$HOME/.hermes/memory}"
MNELO_HOME="$(dirname "$LIVE_ROOT")"
PLIST_LABEL="ai.mnelo.mcp"
PLIST_SRC="$(cd "$(dirname "$0")/.." && pwd)/scripts/launchd/${PLIST_LABEL}.plist"
PLIST_DST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
VENV_DIR="$LIVE_ROOT/.venv"
PY_BIN="${PY_BIN:-python3}"

# ---- 颜色 (CI 环境无 TTY 时降级) ----
if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; NC=''
fi
log()  { echo -e "[install] $*"; }
warn() { echo -e "[install] ${YELLOW}WARN${NC}: $*"; }
err()  { echo -e "[install] ${RED}ERROR${NC}: $*" >&2; }
ok()   { echo -e "[install] ${GREEN}OK${NC}: $*"; }

# ---- 1. 依赖检查 ----
log "检查依赖..."
command -v "$PY_BIN" >/dev/null 2>&1 || { err "需要 $PY_BIN (Python 3.9+)"; exit 1; }
PY_VERSION="$($PY_BIN -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
log "Python $PY_VERSION"
command -v git >/dev/null 2>&1 || { err "需要 git"; exit 1; }

# ---- 2. 创建 LIVE_ROOT ----
log "准备 live 目录: $LIVE_ROOT"
mkdir -p "$LIVE_ROOT/api" "$LIVE_ROOT/scripts" "$LIVE_ROOT/logs"
chmod 700 "$LIVE_ROOT"

# ---- 3. 复制 repo 文件到 live ----
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
log "同步 repo → live (top-level .py/.sql/.sh)..."
for f in "$REPO_ROOT"/*.py "$REPO_ROOT"/*.sql "$REPO_ROOT"/*.sh; do
    [ -f "$f" ] || continue
    base="$(basename "$f")"
    [ "$base" = "install.sh" ] && continue  # 不复制自己
    cp "$f" "$LIVE_ROOT/$base"
    chmod 600 "$LIVE_ROOT/$base"
done

# ---- 4. 复制 api/ + scripts/ ----
if [ -d "$REPO_ROOT/api" ]; then
    log "复制 api/ ..."
    cp -r "$REPO_ROOT/api/." "$LIVE_ROOT/api/"
    find "$LIVE_ROOT/api" -type f -name "*.py" -exec chmod 600 {} \;
fi
log "复制 scripts/ ..."
cp "$REPO_ROOT/scripts/"*.py "$LIVE_ROOT/scripts/" 2>/dev/null || true
chmod 600 "$LIVE_ROOT/scripts/"*.py 2>/dev/null || true
# [2026-08-29] wrapper scripts/mnelo_cli.py 复制过去后还要 +x (install 走 cp 默认保留
# 来源权限, 但来源是 git checkout = 100644, install 完默认 600 — wrapper 需要 +x 才能
# 直接当命令跑). 显式 chmod 755 让 install 后 wrapper 在 LIVE_ROOT/scripts/ 直接能跑.
if [ -f "$LIVE_ROOT/scripts/mnelo_cli.py" ]; then
    chmod 755 "$LIVE_ROOT/scripts/mnelo_cli.py"
    log "wrapper 已装 +x: $LIVE_ROOT/scripts/mnelo_cli.py"
fi

# ---- 5. venv ----
if [ ! -d "$VENV_DIR" ]; then
    log "创建 venv: $VENV_DIR"
    "$PY_BIN" -m venv "$VENV_DIR"
fi
VENV_PY="$VENV_DIR/bin/python3"
log "venv python: $VENV_PY"

# ---- 6. pip install ----
log "pip install -r requirements.txt ..."
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r "$REPO_ROOT/requirements.txt"

# ---- 7. init_db ----
if [ ! -f "$LIVE_ROOT/memory.db" ]; then
    log "初始化数据库..."
    "$VENV_PY" "$LIVE_ROOT/scripts/init_db.py"
    ok "数据库已建: $LIVE_ROOT/memory.db"
else
    log "数据库已存在, 跳过 init_db (想重置就 rm memory.db 再跑)"
fi

# ---- 8. (可选) 预下载 embedder 模型 ----
if [ "${SKIP_MODEL_DOWNLOAD:-0}" != "1" ]; then
    log "预下载 bge-small-zh 模型 (~92 MB, 避免首次 recall 冷启动)..."
    "$VENV_PY" -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-zh-v1.5')" 2>&1 | tail -3 || \
        warn "模型预下载失败, 首次 recall 时会按需下载"
else
    log "跳过模型下载 (SKIP_MODEL_DOWNLOAD=1)"
fi

# ---- 9. auth token ----
TOKEN_FILE="$HOME/.config/mnelo/auth_token"
if [ ! -f "$TOKEN_FILE" ]; then
    log "生成 auth token: $TOKEN_FILE"
    mkdir -p "$(dirname "$TOKEN_FILE")"
    chmod 700 "$(dirname "$TOKEN_FILE")"
    "$VENV_PY" -c "import secrets; print(secrets.token_urlsafe(48))" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    ok "token 已生成"
else
    log "token 已存在: $TOKEN_FILE"
fi
# ---- 9.5 listen mode (决定 mcp_server --host) ----
# [8/8 Tailscale multi-agent] 主人拍板 mnelo 支持 multi-agent 远程调用.
# 询问主人监听模式, 决定 plist/systemd 的 --host 参数.
BIND_HOST="127.0.0.1"  # 保守默认
if [ -t 0 ] && [ "${MNELO_INSTALL_NONINTERACTIVE:-0}" != "1" ]; then
    log "mcp_server 监听模式决定 (--host 参数)..."
    echo "  mnelo 只在这台机器本机用吗？还是其它 Tailscale 节点也要连？"
    echo "    1) 单机本地 (loopback, --host 127.0.0.1) — 推荐默认"
    echo "    2) 多机 / 多 agent (Tailscale mesh, --host 0.0.0.0)"
    echo "       需 Tailscale 已在 mesh, 变 plist --host 0.0.0.0"
    echo "       白名单策略不变 (loopback + 100.64.0.0/10 CGNAT 接受, LAN/公网/IPv6 拒绝)"
    read -r -p "  选择 [1/2] (Enter=1): " listen_mode
    case "$listen_mode" in
        2)
            BIND_HOST="0.0.0.0"
            ok "多 agent Tailscale mesh 模式: --host 0.0.0.0"
            warn "确认 Tailscale 已在 mesh, 后续可用 setup_tailscale_mnelo.sh 验证"
            ;;
        *)
            BIND_HOST="127.0.0.1"
            ok "单机本地模式: --host 127.0.0.1"
            ;;
    esac
else
    log "非交互模式 或 MNELO_INSTALL_NONINTERACTIVE=1 — 使用默认 listen mode: $BIND_HOST"
fi
export BIND_HOST

# ---- 10. 服务安装: macOS → launchd plist / Linux → systemd unit ----
if [ "$(uname -s)" = "Darwin" ]; then
    if [ -f "$PLIST_SRC" ]; then
        log "装 launchd plist: $PLIST_DST"
        mkdir -p "$(dirname "$PLIST_DST")"
        # 替换 plist 里的 LIVE_ROOT / VENV_PY / BIND_HOST 占位符
        sed -e "s|__LIVE_ROOT__|$LIVE_ROOT|g" \
            -e "s|__VENV_PY__|$VENV_PY|g" \
            -e "s|__VENV_DIR__|$VENV_DIR|g" \
            -e "s|__MNELO_HOME__|$MNELO_HOME|g" \
            -e "s|__BIND_HOST__|$BIND_HOST|g" \
            "$PLIST_SRC" > "$PLIST_DST"
        chmod 644 "$PLIST_DST"

        # 先 unload (如果已存在) 再 load, 防 duplicate
        launchctl unload "$PLIST_DST" 2>/dev/null || true
        launchctl load "$PLIST_DST"
        ok "plist 已装 + launchd load 成功"
        log "日志: tail -f $MNELO_HOME/logs/mnelo.mcp.log"
    else
        warn "plist 模板不存在: $PLIST_SRC (跳过 launchd 装)"
    fi
else
    # Linux: systemd unit (有 systemd) — 自动守护 + 崩溃自拉起 + 开机自启
    if command -v systemctl >/dev/null 2>&1; then
        SYSTEMD_SRC="$REPO_ROOT/scripts/systemd/mnelo-mcp.service"
        if [ -f "$SYSTEMD_SRC" ]; then
            if [ "$(id -u)" -eq 0 ]; then
                # root → 系统级 unit (随系统自启, 服务独立于登录会话)
                SD_DST="/etc/systemd/system/mnelo-mcp.service"
                SD_SCOPE="system"
                SD_CTL="systemctl"
                WANTED_BY="multi-user.target"
                USER_LINE="User=$(id -un)"
            else
                # 非 root → 用户级 unit + linger (SSH 登出后服务仍存活)
                SD_DST="$HOME/.config/systemd/user/mnelo-mcp.service"
                SD_SCOPE="user"
                SD_CTL="systemctl --user"
                WANTED_BY="default.target"
                USER_LINE="# User= (user unit 不允许指定 — 自动以当前用户跑)"
            fi
            log "装 systemd unit: $SD_DST ($SD_SCOPE scope)"
            mkdir -p "$(dirname "$SD_DST")"
            sed -e "s|__LIVE_ROOT__|$LIVE_ROOT|g" \
                -e "s|__VENV_PY__|$VENV_PY|g" \
                -e "s|__MNELO_HOME__|$MNELO_HOME|g" \
                -e "s|__USER_LINE__|$USER_LINE|g" \
                -e "s|__WANTED_BY__|$WANTED_BY|g" \
                -e "s|__BIND_HOST__|$BIND_HOST|g" \
                "$SYSTEMD_SRC" > "$SD_DST"
            chmod 644 "$SD_DST"

            # 用户级 unit 要连 user manager — SSH 非登录 shell 里 XDG_RUNTIME_DIR 可能没设
            if [ "$SD_SCOPE" = "user" ]; then
                export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
            fi
            $SD_CTL daemon-reload 2>/dev/null || true
            if $SD_CTL enable --now mnelo-mcp 2>/dev/null; then
                ok "systemd unit 已装 + 启动 ($SD_SCOPE)"
                log "管理: $SD_CTL status mnelo-mcp  / 日志: journalctl -u mnelo-mcp -f"
            else
                warn "unit 文件已写: $SD_DST — 但 systemctl 不可用/失败, 稍后手动: $SD_CTL enable --now mnelo-mcp"
            fi

            # 用户级: enable-linger 让服务在登出后继续跑 (需 root 执行)
            if [ "$SD_SCOPE" = "user" ]; then
                if loginctl enable-linger "$(id -un)" 2>/dev/null; then
                    ok "linger 已启用 — SSH 登出后服务仍存活"
                else
                    warn "loginctl enable-linger 失败 — 用户服务在登出后可能停止; 用 root 跑: sudo loginctl enable-linger $(id -un)"
                fi
            fi
        else
            warn "systemd 模板不存在: $SYSTEMD_SRC (跳过; 手动跑见 docs/RUNBOOK.md §5.2)"
        fi
    else
        log "无 systemctl (非 systemd 发行版) — 手动跑: MNELO_MEMORY_DIR=$LIVE_ROOT $VENV_PY $LIVE_ROOT/mcp_server.py --transport streamable-http"
    fi
fi

# ---- 11. health check ----
log "跑 health_check.py 验证..."
"$VENV_PY" "$LIVE_ROOT/scripts/health_check.py" || warn "health_check 失败, 但 install 已完成"

# ---- 12. backup 配置 (TASKS_BACKUP_RESTORE A4) ----
if [ -t 0 ] && [ "${MNELO_MEMORY_BACKUP_SKIP_PROMPT:-0}" != "1" ]; then
    log "配置自动备份..."
    read -r -p "启用定期备份? [Y/n] " enable_backup
    case "$enable_backup" in
        [Nn]*)
            ok "跳过备份配置"
            ;;
        *)
            echo "  快照位置:"
            echo "    1) $LIVE_ROOT/snapshots/                             (本地默认, 不出机器)"
            echo "    2) 自定义路径                                        (NAS/同步盘/git 仓库等, 异地同步自行保证)"
            echo "       ⚠️  快照含 KG/PII — 自定义/异地路径请确认目标安全; 若进 git 仓库务必 PRIVATE"
            echo "    3) 交由 backup_db 默认推导                           (不写 snapshot_dir, 用 \$LIVE_ROOT/snapshots)"
            read -r -p "  Choose [1/2/3] (Enter=1): " loc_choice
            case "$loc_choice" in
                2)
                    read -r -p "    输入快照目录绝对路径: " custom_path
                    SNAP_DIR="${custom_path:-$LIVE_ROOT/snapshots}" ;;
                3)
                    SNAP_DIR="" ;;
                *)
                    SNAP_DIR="$LIVE_ROOT/snapshots" ;;
            esac

            read -r -p "保留最近多少份全备? (老的自动删) [30]: " ret_count
            RETENTION="${ret_count:-30}"
            if ! [[ "$RETENTION" =~ ^[0-9]+$ ]] || [ "$RETENTION" -lt 1 ]; then
                RETENTION=30
            fi

            # 写 config.toml [backup]
            CONFIG_TOML="$LIVE_ROOT/config.toml"
            [ -f "$CONFIG_TOML" ] || cp "$REPO_ROOT/config.toml.example" "$CONFIG_TOML"
            chmod 600 "$CONFIG_TOML"
            # 删旧 [backup] section (idempotent), 清掉 sed 残留的 .bak
            sed -i.bak '/^\[backup\]/,/^retention = /d' "$CONFIG_TOML" 2>/dev/null || true
            rm -f "$CONFIG_TOML.bak"
            # [8/5 fix] snapshot_dir 仅在用户选了自定义/默认时写入 (选项 3 → 空, 走 backup_db 推导)
            {
                echo ""
                echo "[backup]"
                echo "enabled = true"
                if [ -n "$SNAP_DIR" ]; then
                    echo "snapshot_dir = \"$SNAP_DIR\""
                fi
                echo "schedule = \"wed+sun\""
                echo "retention = $RETENTION"
            } >> "$CONFIG_TOML"
            chmod 600 "$CONFIG_TOML"
            ok "backup config 已写: snapshot_dir=${SNAP_DIR:-<backup_db 默认推导>} retention=$RETENTION"

            # 装 backup plist (macOS)
            if [ "$(uname -s)" = "Darwin" ]; then
                BACKUP_PLIST_SRC="$REPO_ROOT/scripts/launchd/ai.mnelo.backup.plist"
                BACKUP_PLIST_DST="$HOME/Library/LaunchAgents/ai.mnelo.backup.plist"
                if [ -f "$BACKUP_PLIST_SRC" ]; then
                    sed -e "s|__LIVE_ROOT__|$LIVE_ROOT|g" \
                        -e "s|__VENV_PY__|$VENV_PY|g" \
                        -e "s|__MNELO_HOME__|$MNELO_HOME|g" \
                        "$BACKUP_PLIST_SRC" > "$BACKUP_PLIST_DST"
                    chmod 644 "$BACKUP_PLIST_DST"
                    launchctl unload "$BACKUP_PLIST_DST" 2>/dev/null || true
                    launchctl load "$BACKUP_PLIST_DST"
                    ok "backup plist 已装 (周三+周日 03:00 自动跑)"
                    log "备份日志: tail -f $MNELO_HOME/logs/mnelo.backup.log"
                else
                    warn "backup plist 模板不存在: $BACKUP_PLIST_SRC (跳过)"
                fi
            else
                # [8/5 fix] Linux/VPS 原实现不装任何调度 — 补 crontab 周三+周日 03:00.
                # 内联 MNELO_MEMORY_DIR, 因为 cron 环境不 source ~/.profile.
                CRON_BACKUP="MNELO_MEMORY_DIR=$LIVE_ROOT $VENV_PY $REPO_ROOT/scripts/backup_db.py --scheduled"
                if crontab -l 2>/dev/null | grep -q "backup_db.py --scheduled"; then
                    ok "backup cron 已存在, 跳过"
                else
                    mkdir -p "$LIVE_ROOT/logs"
                    ( crontab -l 2>/dev/null | grep -v "backup_db.py"; \
                      echo "0 3 * * 0,3 $CRON_BACKUP >> $LIVE_ROOT/logs/mnelo.backup.log 2>&1" ) | crontab -
                    ok "backup cron 已装 (周三+周日 03:00)"
                    log "备份日志: tail -f $LIVE_ROOT/logs/mnelo.backup.log"
                fi
            fi
            ;;
    esac
else
    log "非交互模式 或 MNELO_MEMORY_BACKUP_SKIP_PROMPT=1 — 跳过备份询问 (交互终端才会提示)"
fi

# [8/6 M5.1] 装 loop_tick cron (DESIGN §8 推进机制二期)
            if [ "$(uname -s)" = "Darwin" ]; then
                LOOP_TICK_PLIST_SRC="$REPO_ROOT/scripts/launchd/ai.mnelo.loop_tick.plist"
                LOOP_TICK_PLIST_DST="$HOME/Library/LaunchAgents/ai.mnelo.loop_tick.plist"
                if [ -f "$LOOP_TICK_PLIST_SRC" ]; then
                    sed -e "s|__LIVE_ROOT__|$LIVE_ROOT|g" \
                        -e "s|__VENV_PY__|$VENV_PY|g" \
                        -e "s|__VENV_DIR__|$VENV_DIR|g" \
                        -e "s|__MNELO_HOME__|$MNELO_HOME|g" \
                        "$LOOP_TICK_PLIST_SRC" > "$LOOP_TICK_PLIST_DST"
                    chmod 644 "$LOOP_TICK_PLIST_DST"
                    if launchctl list 2>/dev/null | grep -q "ai.mnelo.loop_tick"; then
                        launchctl unload "$LOOP_TICK_PLIST_DST" 2>/dev/null || true
                    fi
                    if launchctl load "$LOOP_TICK_PLIST_DST" 2>/dev/null; then
                        ok "loop_tick plist 已装 (每 30 分钟跑)"
                        log "tick 日志: tail -f $MNELO_HOME/logs/mnelo.loop_tick.log"
                    else
                        warn "loop_tick plist load 失败 (手动: launchctl load $LOOP_TICK_PLIST_DST)"
                    fi
                else
                    warn "loop_tick plist 模板不存在: $LOOP_TICK_PLIST_SRC (跳过)"
                fi
            else
                # Linux: 装 crontab 每 30 分钟跑
                CRON_LOOP_TICK="MNELO_MEMORY_DIR=$LIVE_ROOT MNELO_MEMORY_SEARCH_BACKEND=usearch $VENV_PY $REPO_ROOT/scripts/mnelo_loop_tick_cron.py --threshold 0"
                if crontab -l 2>/dev/null | grep -q "mnelo_loop_tick_cron.py"; then
                    ok "loop_tick cron 已存在, 跳过"
                else
                    mkdir -p "$LIVE_ROOT/logs"
                    ( crontab -l 2>/dev/null | grep -v "mnelo_loop_tick_cron.py"; \
                      echo "*/30 * * * * $CRON_LOOP_TICK >> $LIVE_ROOT/logs/mnelo.loop_tick.log 2>&1" ) | crontab -
                    ok "loop_tick cron 已装 (每 30 分钟)"
                fi
            fi

ok "✅ install 完成"
log "测试:"
log "  echo '{\"jsonrpc\":\"2.0\",\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"clientInfo\":{\"name\":\"test\",\"version\":\"1.0\"}},\"id\":1}' | $VENV_PY $LIVE_ROOT/mcp_server.py --transport stdio"
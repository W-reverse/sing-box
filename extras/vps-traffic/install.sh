#!/usr/bin/env bash
# Install/manage only this extension; never invokes or modifies the sing-box installer.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
TEST_ROOT=${SB_TRAFFIC_TEST_ROOT:-}
if [[ $TEST_ROOT ]]; then
    prefix=${TEST_ROOT%/}
    HOME_DIR=${SB_TRAFFIC_HOME:-$prefix/opt/sing-box-traffic}
    DATA_DIR=${SB_TRAFFIC_DATA:-$prefix/etc/sing-box-traffic}
    BIN_DIR=${SB_TRAFFIC_BIN_DIR:-$prefix/usr/local/bin}
    SYSTEMD_DIR=${SB_TRAFFIC_SYSTEMD_DIR:-$prefix/etc/systemd/system}
    OPENRC_DIR=${SB_TRAFFIC_OPENRC_DIR:-$prefix/etc/init.d}
    RUN_DIR=${SB_TRAFFIC_RUN_DIR:-$prefix/run}
    ROOT_DIR=${SB_TRAFFIC_ROOT:-$prefix/etc/sing-box}
else
    HOME_DIR=${SB_TRAFFIC_HOME:-/opt/sing-box-traffic}
    DATA_DIR=${SB_TRAFFIC_DATA:-/etc/sing-box-traffic}
    BIN_DIR=${SB_TRAFFIC_BIN_DIR:-/usr/local/bin}
    SYSTEMD_DIR=${SB_TRAFFIC_SYSTEMD_DIR:-/etc/systemd/system}
    OPENRC_DIR=${SB_TRAFFIC_OPENRC_DIR:-/etc/init.d}
    RUN_DIR=${SB_TRAFFIC_RUN_DIR:-/run}
    ROOT_DIR=${SB_TRAFFIC_ROOT:-/etc/sing-box}
fi
COMMAND_LINK=$BIN_DIR/sb-traffic
SERVICE_NAME=sing-box-traffic

fail() { printf 'sb-traffic install: %s\n' "$*" >&2; exit 1; }
need_root() { [[ $TEST_ROOT || ${EUID:-$(id -u)} -eq 0 ]] || fail '请使用 sudo/root 执行安装和服务管理'; }

check_deps() {
    (( BASH_VERSINFO[0] >= 4 )) || fail '需要 Bash >= 4；请显式用现代 Bash 运行安装脚本'
    local py=${PYTHON:-python3}
    command -v "$py" >/dev/null 2>&1 || fail '缺少 Python 3.8+；请先安装系统 Python'
    "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' || fail '需要 Python 3.8 或更新版本'
    command -v jq >/dev/null 2>&1 || fail '缺少 jq；请使用系统包管理器安装 jq'
    [[ -r $ROOT_DIR/sh/src/core.sh && -d $ROOT_DIR/conf ]] || fail "未找到 sing-box 脚本/配置：$ROOT_DIR（需先安装主项目）"
}

backend_from_env() {
    case ${SB_TRAFFIC_INIT:-auto} in
        systemd) printf systemd ;;
        openrc) printf openrc ;;
        auto)
            if command -v systemctl >/dev/null 2>&1; then printf systemd
            elif command -v rc-service >/dev/null 2>&1 && command -v rc-update >/dev/null 2>&1; then printf openrc
            else fail '未检测到 systemd 或 OpenRC；无法管理独立服务' ; fi
            ;;
        *) fail 'SB_TRAFFIC_INIT 只能为 systemd 或 openrc' ;;
    esac
}

install_service_files() {
    local backend=$1
    if [[ $backend == systemd ]]; then
        mkdir -p "$SYSTEMD_DIR"
        cat >"$SYSTEMD_DIR/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Independent sing-box VPS traffic accounting and subscription
After=network.target

[Service]
Type=simple
ExecStart=$PYTHON_BIN $HOME_DIR/traffic.py serve
Restart=on-failure
RestartSec=5s
User=root
Group=root
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadOnlyPaths=$ROOT_DIR
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF
        chmod 0644 "$SYSTEMD_DIR/$SERVICE_NAME.service"
        systemctl daemon-reload
    else
        mkdir -p "$OPENRC_DIR" "$RUN_DIR"
        cat >"$OPENRC_DIR/$SERVICE_NAME" <<EOF
#!/sbin/openrc-run
name="sing-box-traffic"
description="Independent sing-box traffic accounting and subscription"
command="$PYTHON_BIN"
command_args="$HOME_DIR/traffic.py serve"
command_background="yes"
pidfile="$RUN_DIR/sing-box-traffic.pid"
output_log="/var/log/sing-box-traffic.log"
error_log="/var/log/sing-box-traffic.log"
EOF
        chmod 0755 "$OPENRC_DIR/$SERVICE_NAME"
    fi
    printf '%s\n' "$backend" >"$HOME_DIR/service-backend"
    chmod 0600 "$HOME_DIR/service-backend"
}

install_extension() {
    need_root
    check_deps
    PYTHON_BIN=$(command -v "${PYTHON:-python3}")
    local backend
    backend=$(backend_from_env)
    if [[ -e $COMMAND_LINK && ! -L $COMMAND_LINK ]]; then fail "$COMMAND_LINK 已存在且不是本扩展 symlink，拒绝覆盖"; fi
    if [[ -L $COMMAND_LINK && $(readlink "$COMMAND_LINK") != "$HOME_DIR/sb-traffic" ]]; then
        fail "$COMMAND_LINK 指向其他程序，拒绝替换"
    fi
    mkdir -p "$HOME_DIR" "$DATA_DIR" "$BIN_DIR"
    chmod 0700 "$DATA_DIR"
    # Replacement is limited to files shipped by this extension; persistent data stays in DATA_DIR.
    for file in traffic.py menu.py publish.py adapter.sh install.sh; do
        install -m 0755 "$SCRIPT_DIR/$file" "$HOME_DIR/$file"
    done
    cat >"$HOME_DIR/sb-traffic" <<EOF
#!/usr/bin/env bash
exec "$PYTHON_BIN" "$HOME_DIR/traffic.py" "\$@"
EOF
    chmod 0755 "$HOME_DIR/sb-traffic"
    ln -sfn "$HOME_DIR/sb-traffic" "$COMMAND_LINK"
    install_service_files "$backend"
    printf '已安装 sb-traffic（独立于 sing-box）；配置目录：%s\n' "$DATA_DIR"
    printf '后台服务已安装但未启用；先 configure，再执行 sb-traffic enable。\n'
}

service_action() {
    need_root
    local action=$1 backend
    [[ -r $HOME_DIR/service-backend ]] || fail '扩展服务尚未安装'
    backend=$(cat "$HOME_DIR/service-backend")
    case $action in
        enable)
            if [[ $backend == systemd ]]; then systemctl enable --now "$SERVICE_NAME.service"
            else rc-update add "$SERVICE_NAME" default && rc-service "$SERVICE_NAME" start; fi
            ;;
        restart)
            if [[ $backend == systemd ]]; then systemctl restart "$SERVICE_NAME.service"
            else rc-service "$SERVICE_NAME" restart; fi
            ;;
        disable)
            if [[ $backend == systemd ]]; then
                systemctl disable --now "$SERVICE_NAME.service"
            else
                if [[ -e $RUN_DIR/$SERVICE_NAME.pid ]]; then
                    rc-service "$SERVICE_NAME" stop || fail '停止服务失败'
                    rm -f "$RUN_DIR/$SERVICE_NAME.pid"
                fi
                rc-update del "$SERVICE_NAME" default || true
            fi
            ;;
        *) fail 'service action 只能是 enable、disable 或 restart' ;;
    esac
}

uninstall_extension() {
    need_root
    local purge=0 backend=
    [[ ${1:-} == --purge ]] && purge=1
    if [[ -r $HOME_DIR/service-backend ]]; then
        backend=$(cat "$HOME_DIR/service-backend")
        if [[ $backend == systemd ]]; then
            systemctl disable --now "$SERVICE_NAME.service" || fail '停止/禁用服务失败；未继续卸载'
            rm -f "$SYSTEMD_DIR/$SERVICE_NAME.service"
            systemctl daemon-reload
        elif [[ $backend == openrc ]]; then
            if [[ -e $RUN_DIR/$SERVICE_NAME.pid ]]; then
                rc-service "$SERVICE_NAME" stop || fail '停止服务失败；未继续卸载'
                rm -f "$RUN_DIR/$SERVICE_NAME.pid"
            fi
            rc-update del "$SERVICE_NAME" default || true
            rm -f "$OPENRC_DIR/$SERVICE_NAME"
        fi
    fi
    if [[ -L $COMMAND_LINK && $(readlink "$COMMAND_LINK") == "$HOME_DIR/sb-traffic" ]]; then rm -f "$COMMAND_LINK"; fi
    rm -rf -- "$HOME_DIR"
    if (( purge )); then rm -rf -- "$DATA_DIR"; else printf '保留统计数据：%s\n' "$DATA_DIR"; fi
    printf 'sb-traffic 已卸载；sing-box 未修改。\n'
}

case ${1:-install} in
    install) install_extension ;;
    service) shift; [[ $# -eq 1 ]] || fail '用法: install.sh service enable|disable|restart'; service_action "$1" ;;
    uninstall) shift; [[ $# -le 1 ]] || fail '用法: install.sh uninstall [--purge]'; uninstall_extension "${1:-}" ;;
    *) fail '用法: install.sh [install|service enable|service disable|uninstall [--purge]]' ;;
esac

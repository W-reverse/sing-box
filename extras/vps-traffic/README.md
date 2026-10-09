# sing-box VPS 流量统计扩展（独立安装）

`sb-traffic` 是独立的流量计量、节点改名和远程订阅工具。适配器只 source 主项目的 `sh/src/core.sh`，复用其协议链接生成函数，不 source `init.sh`。为接入原终端菜单，主项目在 `src/core.sh` 的 `is_main_menu()` 增加一个小入口；不替换现有命令 symlink，不修改代理配置或主服务。统计实现、交互子菜单和测试仍集中在本目录。

## 要求与安装

- Linux Bash 4+、Python 3.8+、`jq`；原 sing-box 脚本和配置需已存在。Hysteria2 链接沿用上游证书指纹逻辑，需要 `openssl` 和主项目 `bin/tls.cer`。
- 安装/更新时只复制扩展文件，需 root 权限（主安装器会在安装结束时自动调用本安装器；缺少 Python 时只告警，不影响主安装）：

```sh
sudo bash extras/vps-traffic/install.sh
```


生产 Linux 使用 `/bin/bash`，安装器会拒绝 Bash<4；不会自动安装依赖，也不下载未知二进制。默认读取 `/etc/sing-box`，程序装在 `/opt/sing-box-traffic`，数据保存在权限 0700 的 `/etc/sing-box-traffic`。主项目在其他目录时，安装器预检可设 `SB_TRAFFIC_ROOT=/实际/路径`，并在首次 `configure` 使用 `--root /实际/路径` 将路径持久保存。安装器创建自己的 `sing-box-traffic` systemd 或 OpenRC 服务模板，但默认不启用服务。macOS 本机 Bash 3.2 不满足要求；本地开发/测试明确用现代 Bash，不在 macOS 安装生产服务。

安装器重复运行用于更新，不覆盖数据、累计用量或 token。只运行此扩展目录中的安装器；不要调用主项目安装器来更新本扩展。更新扩展代码必须先 `sb-traffic disable`，再重装扩展，最后 `sb-traffic enable`；否则已运行的旧 Python 进程仍使用旧代码。

## 原终端菜单与交互配置

安装本扩展，并确保 VPS 上运行的是包含菜单入口的本 fork 脚本后，运行：

```sh
sudo sing-box
# 在原主菜单末尾选择“流量统计”（当前为第 11 项）

# 也可直接进入相同子菜单：
sudo sb-traffic menu
```

子菜单提供：查看用量、配置额度/网卡/重置日/时区、校准已用总量、选择节点重命名、启用/停用统计服务、查看订阅地址、生成 Caddy 配置片段和轮换令牌。配置向导按提示填写；已有值可回车保留，输入 `!` 可取消；保存前需确认。改变计量口径或账期时会要求重新输入当前已用总量，避免静默清零。停用与令牌轮换需确认。输入 `0`、EOF 或 Ctrl-C 退出流量子菜单；不停止后台统计服务。

原主菜单的既有编号不变；“流量统计”动态追加在末尾，不占用固定编号。未安装扩展时只显示安装指引，不会自动下载或修改系统。独立安装器仍不会改写现有 `/etc/sing-box/sh/src/core.sh`，因此旧 VPS 需要先部署本 fork 的菜单入口版本；单独更新扩展不会凭空改动原主菜单。

### 与上游合并

为满足菜单整合与安装简化，本版本把改动限制在最小的几处：上游已有文件仅 3 处被修改——`src/core.sh` 的菜单入口（13 行）、`install.sh`（脚本来源指向本 fork，并在安装结束时自动安装扩展）、`src/init.sh`（同步 `is_sh_repo`）。上游若也修改同一位置，仍可能需要处理冲突，不能保证永久零冲突；统计实现与其余上游代码保持隔离。

`is_sh_repo` 已指向本 fork，`sing-box update sh` 不会再用上游代码覆盖 `core.sh`。但 `sing-box reinstall` 会先卸载（清空 `/etc/sing-box`，含 `conf/`）再重跑安装器，请先 `sb-traffic disable`，重装后再 `sb-traffic enable`；`/opt/sing-box-traffic`、统计数据和 `sb-traffic menu` 不受影响。合并上游后重跑本目录测试。

## 初次配置与计量口径

先从 VPS 确认真正的公网出口网卡（例如 `ip -br link`），再显式配置：

```sh
sudo sb-traffic configure \
  --interface eth0 --quota 1TB --reset-day 15 --utc-offset +08:00 \
  --mode out --name '香港 VPS' --address 203.0.113.10 --used 120GB
sudo sb-traffic status
sudo sb-traffic enable
```

首次配置必须给 `--interface`、`--quota`、`--address`。`--used` 是配置时本周期已经用掉的**总量**，用于校准安装前的用量，不是追加值；可省略（按 0 开始）。地址由用户明确提供，适用于不能从配置推导地址的节点；扩展不会查询外网地址。IPv6 地址请输入不带方括号的裸地址；写入分享 URI 时由上游逻辑负责加括号。

`--mode in|out|both` 分别计 RX、TX、RX+TX，默认 `out`。按 `/sys/class/net/<interface>/statistics` 每 60 秒采样，服务启动即采样并在收到 SIGTERM 时保存。初始网卡计数作为基线，后续计数差分累计。该值是**整台 VPS 该网卡**流量，包含非 sing-box 流量；不会自动合并容器网桥，不能视作服务商账单的精确替代。网卡计数器回退、重建、关机期间流量或长时间停采均有不可恢复误差；`status` 会报告可检测到的回退/重启/跨月估算告警，可用 `set-used` 校准。

额度可用非负十进制和 `B/KB/MB/GB/TB`（1000 进制）或 `KiB/MiB/GiB/TiB`（1024 进制），额度必须大于 0。统计整数 byte；超额允许显示，剩余量不会小于零。

账期按固定 UTC 偏移的每月指定日 00:00 重置，29–31 日在短月取月底，支持闰年和跨年；它不是 IANA 时区，不自动处理夏令时。若采样跨越重置时刻，扩展按采样时间比例估算新账期部分并给出告警；跨多个账期只保留当前账期的比例估算。

```sh
sudo sb-traffic status [--json]
sudo sb-traffic set-used 250GB             # 校准本周期总已用量
sudo sb-traffic configure --quota 2TB      # 更新额度，不清空累计
sudo sb-traffic configure --name '新名字' --address 203.0.113.20
# 修改网卡/方向/重置日/UTC 偏移必须同时给 --used，作为新统计锚点：
sudo sb-traffic configure --interface ens3 --mode both --used 80GB
sudo sb-traffic configure --port 18081      # 然后 disable 再 enable，或使用服务管理器 restart
```

## 节点订阅与 Sub-Store

```sh
sudo sb-traffic subscription             # 即时输出纯 URI，不含 ANSI
sudo sb-traffic rename 'VLESS-REALITY-443.json' '香港 VPS · Reality'
sudo sb-traffic url                      # 回环 token URL
sudo sb-traffic rotate-token             # 撤销旧 URL
sudo sb-traffic caddy-config sub.example.com
```

每个 sing-box 配置文件对应一个原有真实节点；不会新增提示节点。默认显示名为“VPS 名 · 配置文件名（不含 `.json`）”，包含端口/域名的配置名可区分节点。`rename` 对**精确文件名**设置持久映射，允许中文、空格、`#`、`%` 和引号；不接受控制字符或路径穿越。普通 URI 只替换 fragment 名称，VMess 只更改解码 JSON 的 `ps` 字段，其余连接参数不改。名称从原始上游 URI/配置重新生成，不会叠加旧的流量后缀。配置增删改将在服务下一次刷新时生效。所有节点共享这台 VPS 的同一额度和已用量，余额会附在名称后；例如 `香港 VPS · Reality | 剩余 880.00 GB`。

Sub-Store 中为**每台 VPS 各自**新增远程 URL 订阅，再聚合这些订阅；同一 VPS 的所有节点共享其额度/用量显示，不要把不同 VPS 的额度混成一个值或把账期伪装成 expire。改名后检查 Sub-Store 的名称过滤/后缀规则和固定选择；部分客户端可能把新名称视为新节点。额度、节点或名称变化后，刷新 Sub-Store 源缓存并在客户端更新/刷新该订阅，客户端才会看到新余额或节点名。

订阅服务只绑定 `127.0.0.1:<port>`（默认 18080）。`url` 显示的是本机 URL，仅用于本机或隧道；它是 token bearer 凭据，不应公开粘贴或写入日志。HTTP 仅提供 token 路径下 GET/HEAD，错误 token/路径返回 404，写请求不执行管理操作，响应 `Cache-Control: no-store`。请求只读后台缓存，不会启动 shell；状态损坏、导出失败或缓存超过 125 秒时返回 503。后台约每秒检查状态；token 轮换通常约 1 秒生效，但若正被最长 60 秒的节点导出阻塞，旧 token 可能要约 61 秒才撤销，另受系统调度影响，不承诺硬实时。节点配置全量导出每 60 秒刷新一次（或服务启动/状态变化时刷新）。紧急撤销请依次执行 `sb-traffic disable`、`sb-traffic rotate-token`、`sb-traffic enable`；更改监听端口也需重启服务。

推荐使用独立域名的 Caddy HTTPS 站点，并且只反代订阅路径。`caddy-config` 只打印片段，不写入现有 Caddyfile、不改防火墙/证书/代理路径、不占公网 HTTP 端口。对现有上游 Caddy，先确认主配置已导入 `/etc/caddy/sites/*.conf`，再创建独立站点文件；先检查并拒绝覆盖同名文件。验证配置后重启 Caddy（systemd 或 OpenRC），不要依赖 reload。
```sh
set -euo pipefail
sudo grep -F 'import /etc/caddy/sites/*.conf' /etc/caddy/Caddyfile
target=/etc/caddy/sites/sub.example.com.conf
sudo test ! -e "$target" || { echo "拒绝覆盖已有文件：$target" >&2; exit 1; }
sudo install -d -m 0755 /etc/caddy/sites
sudo sb-traffic caddy-config sub.example.com | sudo tee "$target" >/dev/null
sudo caddy validate --config /etc/caddy/Caddyfile
# systemd:
sudo systemctl restart caddy
# OpenRC instead:
# sudo rc-service caddy restart
```

部署远程 Sub-Store 源时，保留 `url` 输出的 `/sub/<token>` 路径，仅将 `http://127.0.0.1:<port>` 替换为 `https://sub.example.com`；不要公开本机 token URL，也不要将 token 服务直接暴露为公网明文 HTTP。没有 Caddy 时，先按 Caddy 官方安装文档部署；本扩展不会自动安装 Caddy 或修改代理配置。没有公网域名时，可通过 SSH 本地转发等安全隧道访问回环服务。

## 服务、卸载和回滚

```sh
sudo sb-traffic disable             # 停止并禁用独立服务；数据保留
sudo sb-traffic enable              # 启用并立即启动
sudo sb-traffic uninstall           # 移除本扩展服务/命令/程序，默认保留数据
sudo sb-traffic uninstall --purge   # 显式删除扩展数据和 token
```

systemd/OpenRC 操作失败返回非零。扩展卸载不会卸载或修改 sing-box；主项目卸载也不会自动清理本扩展。**移除主项目或其配置前，应先 `sb-traffic disable`；若以后不再使用扩展，再单独卸载它。**更新前可备份 `/etc/sing-box-traffic`（目录 0700，状态和锁 0600，包含 token）。回滚时重新运行此前版本扩展的 `install.sh`；默认卸载不删数据。`--purge` 不可恢复。

## 本地测试

从仓库根目录运行；测试不会操作宿主机服务，也不会部署 VPS。`unittest discover` 会运行全部 `test_*.py`，包括流量模型、命令行验收、交互子菜单和原主菜单集成测试。

Linux：
```sh
for file in src/core.sh extras/vps-traffic/adapter.sh extras/vps-traffic/install.sh; do bash -n "$file" || break; done
PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PI_SCRATCH_DIR" python3 -m unittest discover -s extras/vps-traffic/tests -v
```

macOS 本机 Bash 3.2 不满足要求；使用 Homebrew Bash/Python（`/opt/homebrew/bin`）：
```sh
for file in src/core.sh extras/vps-traffic/adapter.sh extras/vps-traffic/install.sh; do /opt/homebrew/bin/bash -n "$file" || break; done
PATH=/opt/homebrew/bin:$PATH PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PI_SCRATCH_DIR" python3 -m unittest discover -s extras/vps-traffic/tests -v
```

集成测试 source 仓库内实际 `src/core.sh`，用临时配置验证 VMess、VLESS Reality、Hysteria2、TUIC、Trojan、SS2022、AnyTLS、Socks、IPv6、Caddy 非标准端口和逐配置隔离。systemd/OpenRC 的安装与生命周期通过 mock 命令和临时根目录检查，不会调用宿主服务管理器。

未在真实 VPS、真实 systemd/OpenRC 守护进程、公网 HTTPS/Caddy 或真实 Sub-Store/客户端上部署验证；这也不能验证服务商账单计量口径。适配器耦合上游 `core.sh` 字段/函数，未来上游接口变化可能需要兼容性维护；本扩展不承诺永远无冲突或免维护。

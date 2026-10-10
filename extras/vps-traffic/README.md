# sing-box VPS 流量统计扩展（独立安装）

`sb-traffic` 是独立的流量计量、节点改名和远程订阅工具。适配器只 source 主项目的 `sh/src/core.sh`，复用其协议链接生成函数，不 source `init.sh`。为接入原终端菜单，主项目在 `src/core.sh` 的 `is_main_menu()` 增加一个小入口；不替换现有命令 symlink，不修改代理配置或主服务。统计实现、交互子菜单和测试仍集中在本目录。

## 要求与安装

- Linux Bash 4+、Python 3.8+、`jq`；原 sing-box 脚本和配置需已存在。Hysteria2 链接沿用上游证书指纹逻辑，需要 `openssl` 和主项目 `bin/tls.cer`。
- 安装/更新时只复制扩展文件，需 root 权限（主安装器会在安装结束时自动调用本安装器；缺少 Python 时只告警，不影响主安装）：

```sh
sudo bash extras/vps-traffic/install.sh
```


生产 Linux 使用 `/bin/bash`，安装器会拒绝 Bash<4；扩展安装器不自动安装依赖。菜单 10 的域名部署会复用 Caddy，缺少时尝试使用现有系统软件源安装 `caddy`（apt-get/dnf/yum/apk），不添加第三方软件源或下载未知二进制。默认读取 `/etc/sing-box`，程序装在 `/opt/sing-box-traffic`，数据保存在权限 0700 的 `/etc/sing-box-traffic`。主项目在其他目录时，安装器预检可设 `SB_TRAFFIC_ROOT=/实际/路径`，并在首次 `configure` 使用 `--root /实际/路径` 将路径持久保存。安装器创建自己的 systemd/OpenRC 服务模板，默认不启用；域名部署会启用并重启该服务。macOS 仅用于开发测试，不部署生产服务。

安装器重复运行用于更新，不覆盖数据、累计用量或 token。只运行此扩展目录中的安装器；不要调用主项目安装器来更新本扩展。更新扩展代码必须先 `sb-traffic disable`，再重装扩展，最后 `sb-traffic enable`；否则已运行的旧 Python 进程仍使用旧代码。

## 原终端菜单与交互配置

安装本扩展，并确保 VPS 上运行的是包含菜单入口的本 fork 脚本后，运行：

```sh
sudo sing-box
# 在原主菜单末尾选择“流量统计”（当前为第 11 项）

# 也可直接进入相同子菜单：
sudo sb-traffic menu
```

子菜单提供：查看用量、配置额度/网卡/重置日/时区、校准已用总量、节点重命名、启停统计服务、查看订阅地址、生成 Caddy 片段、轮换令牌，以及 **10. 配置订阅域名（自动 HTTPS 反代）**。配置向导先自动检测网卡、VPS 地址、名称和时区；通常只需填写额度，其他值可在高级项调整。回车保留已有值，`!` 取消，保存配置需确认。改变计量口径或账期必须重新输入已用总量。尚未配置时，依赖状态的菜单项（包括域名部署）会当场提供配置向导。域名部署只需输入域名，提交即执行；执行前会提示安装/启用服务和 Caddy 重启的可能影响。`0`、EOF 或 Ctrl-C 退出菜单，不停止后台服务。

原主菜单的既有编号不变；“流量统计”动态追加在末尾，不占用固定编号。未安装扩展时只显示安装指引，不会自动下载或修改系统。独立安装器仍不会改写现有 `/etc/sing-box/sh/src/core.sh`，因此旧 VPS 需要先部署本 fork 的菜单入口版本；单独更新扩展不会凭空改动原主菜单。

### 与上游合并

为满足菜单整合与安装简化，本版本把改动限制在最小的几处：上游已有文件仅 3 处被修改——`src/core.sh` 的菜单入口（13 行）、`install.sh`（脚本来源指向本 fork，并在安装结束时自动安装扩展）、`src/init.sh`（同步 `is_sh_repo`）。上游若也修改同一位置，仍可能需要处理冲突，不能保证永久零冲突；统计实现与其余上游代码保持隔离。

`is_sh_repo` 已指向本 fork，`sing-box update sh` 不会再用上游代码覆盖 `core.sh`。但 `sing-box reinstall` 会先卸载（清空 `/etc/sing-box`，含 `conf/`）再重跑安装器，请先 `sb-traffic disable`，重装后再 `sb-traffic enable`；`/opt/sing-box-traffic`、统计数据和 `sb-traffic menu` 不受影响。合并上游后重跑本目录测试。

## 初次配置与计量口径

配置只有额度是必填；网卡、VPS 地址、名称、账期都有自动检测或默认值：

```sh
sudo sb-traffic configure --quota 1TB
sudo sb-traffic status
sudo sb-traffic enable

# 需要覆盖自动检测结果时再显式指定
sudo sb-traffic configure --quota 1TB --reset-day 15 --utc-offset +08:00 \
  --mode out --name '香港 VPS' --address 203.0.113.10
```

首次配置只有 `--quota` 必填。`--interface` 省略时取默认路由网卡；`--address` 省略时用与上游脚本相同的查询（`one.one.one.one/cdn-cgi/trace`，优先 IPv4）取公网地址；两者都检测失败才会报错要求显式指定，设置 `SB_TRAFFIC_OFFLINE=1` 可完全禁止该查询。`--name` 默认取主机名（`localhost` 之类会回退为 `VPS`），`--reset-day` 默认 1，`--utc-offset` 默认本机时区。`--used` 是配置时本周期已经用掉的**总量**，用于校准安装前的用量，不是追加值；可省略（按 0 开始）。IPv6 地址请输入不带方括号的裸地址；写入分享 URI 时由上游逻辑负责加括号。

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
sudo sb-traffic url                      # 配置域名后输出公网 HTTPS 地址
sudo sb-traffic url --local              # 仅用于本机诊断
sudo sb-traffic domain sub.example.com   # 与菜单 10 相同的自动部署流程
sudo sb-traffic rotate-token             # 撤销旧 URL
sudo sb-traffic caddy-config sub.example.com
```

每个 sing-box 配置文件对应一个原有真实节点；不会新增提示节点。默认显示名为“VPS 名 · 配置文件名（不含 `.json`）”，包含端口/域名的配置名可区分节点。`rename` 对**精确文件名**设置持久映射，允许中文、空格、`#`、`%` 和引号；不接受控制字符或路径穿越。普通 URI 只替换 fragment 名称，VMess 只更改解码 JSON 的 `ps` 字段，其余连接参数不改。名称从原始上游 URI/配置重新生成，不会叠加旧的流量后缀。配置增删改将在服务下一次刷新时生效。所有节点共享这台 VPS 的同一额度和已用量，余额会附在名称后；例如 `香港 VPS · Reality | 剩余 880.00 GB`。

Sub-Store 中为**每台 VPS 各自**新增远程 URL 订阅，再聚合这些订阅；同一 VPS 的所有节点共享其额度/用量显示，不要把不同 VPS 的额度混成一个值或把账期伪装成 expire。改名后检查 Sub-Store 的名称过滤/后缀规则和固定选择；部分客户端可能把新名称视为新节点。额度、节点或名称变化后，刷新 Sub-Store 源缓存并在客户端更新/刷新该订阅，客户端才会看到新余额或节点名。

### 一次输入域名，自动发布 HTTPS 订阅

1. 在 DNS 中创建独立子域名，如 `sub.example.com`，将所有 A/AAAA 记录指向这台 VPS。**使用 DNS-only/直连解析**；自动部署会拒绝解析到其他服务器或 CDN 的域名。
2. 放行 TCP 80/443。已有服务占用时不能强行抢占；若 Caddy 已在使用这些端口，会复用它。原 Caddy 使用非标准 HTTPS 端口时，新站点与输出 URL 会沿用该端口；证书验证仍需公网 80 或 443 转发到 Caddy，订阅端口也需放行。
3. 进入 `sudo sb-traffic menu`，选择 **10. 配置订阅域名**，输入纯域名（不含 `https://`、端口或路径）。CLI 等价命令为 `sudo sb-traffic domain sub.example.com`。
4. 程序自动检查 DNS、复用/安装 Caddy、启用并重启独立统计服务、部署反代、等待证书，并核对公网与本机订阅内容。看到“HTTPS 订阅已验证”后，将输出的地址填入远程 Sub-Store。

部署只管理 `/etc/caddy/sites/sb-traffic.conf`；主 `/etc/caddy/Caddyfile` 已有覆盖该文件的 import 时不改，否则仅追加一行 import。不改已有节点站点或代理配置。其他站点已经使用相同/匹配的通配域名时拒绝部署；已有同名文件、被手工修改的托管文件、符号链接也拒绝覆盖。Caddy 服务必须使用 `/etc/caddy/Caddyfile`，systemd/OpenRC 均支持；自定义配置路径或容器 Caddy 不自动接管。系统软件源没有 Caddy 时给出错误，请按官方安装文档安装后重试。

配置通过 `caddy validate` 后才应用：管理接口开启时热重载，原项目的 `admin off` 则短暂重启 Caddy（其代理站点可能短暂中断）。重复执行不会叠加 import；更换域名会替换本扩展的旧站点。更改统计监听端口后，重跑菜单 10 更新反代。轮换 token 保留域名，但需要更新 Sub-Store 中的订阅 URL。

配置或服务应用失败（以及应用前 Ctrl-C）会恢复原文件；若恢复服务本身失败会明确报错。软件包安装与开机启用状态、独立统计服务的启动不回滚。HTTPS 证书/连通性检查约等候 60 秒；未通过时保留反代让 Caddy 后续重试，返回失败并标记“尚未验证”，**不会冒充公网已可用**。排查 DNS、云防火墙、80/443 占用和 Caddy 日志后再运行菜单 10。VPS 自测通过不能保证远程 Sub-Store 所在网络可访问，仍应从该环境实际刷新订阅。

### 本机服务与手动配置

统计服务仍仅监听 `127.0.0.1:<port>`（默认 18080），公网只通过 HTTPS 反代访问。`url` 在配置域名后显示公网地址，未配置时明确警告仅为本机地址；`url --local` 始终输出回环地址。地址中的 token 是 bearer 凭据，请勿公开。HTTP 只提供 token 路径下 GET/HEAD，错误 token/路径返回 404；请求只读缓存，不执行 shell；导出失败或缓存超过 125 秒返回 503。采样及节点导出通常每 60 秒刷新；轮换 token 通常约 1 秒、导出阻塞时约 61 秒生效。紧急撤销请依次 disable、rotate-token、enable。

菜单 8 / `caddy-config` 仍可只打印配置片段，供高级用户手动集成，不会自动保存公网域名。自动部署的 Caddy 站点不包含 token，仅转发 `/sub/*`，其他路径返回 404。不要将回环 token 服务直接暴露成公网明文 HTTP。

## 服务、卸载和回滚

```sh
sudo sb-traffic disable             # 停止并禁用独立服务；数据保留
sudo sb-traffic enable              # 启用并立即启动
sudo sb-traffic uninstall           # 移除本扩展服务/命令/程序，默认保留数据
sudo sb-traffic uninstall --purge   # 显式删除扩展数据和 token
```

systemd/OpenRC 操作失败返回非零。扩展卸载不会卸载或修改 sing-box；主项目卸载也不会自动清理本扩展。**移除主项目或其配置前，应先 `sb-traffic disable`；若以后不再使用扩展，再单独卸载它。**更新前可备份 `/etc/sing-box-traffic`（目录 0700，状态和锁 0600，包含 token）。回滚时重新运行此前版本扩展的 `install.sh`；默认卸载不删数据。`--purge` 不可恢复。

扩展卸载不会卸载共享 Caddy，也不会自动删除反代站点。若曾通过菜单 10 部署，停用/卸载后该入口会不可用；永久移除时请自行删除 `/etc/caddy/sites/sb-traffic.conf` 及主配置里仅为此文件追加的 import，并验证、重载/重启 Caddy。不要删除其他站点的 import。

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

可选的真实 Caddy 配置校验（不会启动服务器、申请公网证书或改宿主服务）：
```sh
SB_TRAFFIC_CADDY_TEST_BIN=/实际/caddy/路径 PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s extras/vps-traffic/tests -p test_publish.py -v
```

未在真实 VPS、真实 systemd/OpenRC 守护进程、公网 HTTPS/Caddy 或真实 Sub-Store/客户端上部署验证；这也不能验证服务商账单计量口径。适配器耦合上游 `core.sh` 字段/函数，未来上游接口变化可能需要兼容性维护；本扩展不承诺永远无冲突或免维护。

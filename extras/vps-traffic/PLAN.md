# VPS 流量统计独立扩展实施方案

基线：W-reverse/sing-box main `2d78583`。方案修订：2026-10-09。保持原作者署名与 GPL-3.0。

## 首要约束：方便合并上游

**最小侵入，方便合并上游**：根据后续“整合原终端菜单”的要求，允许仅在 `src/core.sh` 的 `is_main_menu()` 增加一个连续的小入口，动态追加“流量统计”并调用 `sb-traffic menu`。其余实现/文档/测试集中在 `extras/vps-traffic/`。不改 sing-box.sh、版本号、release 工作流，不替换原命令 symlink，不给原卸载器加钩子。这一修订取代首轮“已有文件零修改”的约束。

**修订（安装简化，2026-10-09 晚）**：为让安装方式与上游完全一致，放开原“不改主安装器 / 不改更新源”的限制——`install.sh` 与 `src/init.sh` 的 `is_sh_repo` 改指向本 fork（脚本代码取自本 fork 的 release），且主安装器在安装结束时自动调用扩展安装器（缺 Python 等失败仅告警，不中断主安装）。上游已有文件的改动由 1 处增至 3 处，仍各自连续，可单独剔除。

**修订（交互简化，2026-10-09 晚）**：配置向导原先逐个询问网卡/额度/重置日/时区/方向/名称/地址/端口，且缺少必填项时会中止并把用户推回命令行。现改为：优先自动检测（默认路由网卡、公网地址、主机名、本机时区），基本流程只要求额度，其余可在“手动调整高级项”里改；缺失值继续追问而不是中止；未配置时选择依赖配置的功能在菜单内询问是否进入向导。自动检测失败不影响可用性，只是回退为手动填写。

独立命令 `sb-traffic`；独立安装目录 `/opt/sing-box-traffic`；数据目录 `/etc/sing-box-traffic`（0700）；独立服务 `sing-box-traffic`；可显式指定 sing-box 根目录（默认 `/etc/sing-box`），通过隔离适配器读取其配置并复用其生成 URL 的逻辑。扩展安装、更新、卸载与主项目分开；卸载扩展默认保留数据，purge 显式删除。主项目卸载前应停用扩展，文档明确此顺序，不能为了自动钩子侵入原代码。

Git 合并目标是降低冲突，不承诺永远零兼容维护；上游配置和函数接口变化由小适配层及兼容测试兜底。扩展从 fork 的 extras 目录安装，上游脚本更新不会覆盖 /opt 和独立数据目录；后续扩展更新重新执行其 install.sh，必须保留累计和 token。

## 用户目标

每个真实可连接节点名显示“原/自定义节点名 | 剩余 820.50 GB”，不增加提示节点。每台 VPS 独立配置额度、公网网卡、统计方向、每月重置日及账单时区；手动设置本周期已用总量。多 VPS 各提供 URL 订阅，在 Sub-Store 聚合，不混用不同 VPS 的额度。

已通读全部原仓库源码；原项目没有订阅服务或持久计量。Sub-Store URI fragment 解码和 VMess ps 字段均可保留名称，无需改 Sub-Store。不要把 reset date 当作订阅 expire，不伪造多 VPS 聚合额度头。

## 架构

1. Python 3 标准库模块（明确最低版本，建议3.8+），负责 byte 计量、持久化、账期、CLI、修改链接名称、回环 HTTP 订阅服务；无 pip 依赖。
2. Bash 适配器只 source `/etc/sing-box/sh/src/core.sh`，设置明确环境后导出链接。**后台导出不得调用常规 init**（它会探测网络、创建证书、改服务）。逐配置 subshell 隔离原脚本全局变量。与上游的耦合限定为此适配层和一个菜单入口。
3. 扩展自己的 install.sh/管理入口负责依赖检测、复制、权限、命令 symlink、systemd/OpenRC 模板和启停/卸载；不下载执行原脚本，不操作真实本地 Linux 系统进行测试。
4. 独立 README.md/测试；不修改上游 CI，提供从仓库根执行的完整检查命令。
5. `menu.py` 实现中文循环子菜单，复用原 CLI 和参数验证；原主菜单末尾动态追加入口，原编号不变。未安装时只提示，不自动安装；独立安装器不替 VPS 改写主脚本，管理员须部署带入口的 fork。纯上游更新覆盖菜单入口后，独立命令仍可用。

## CLI 合约

- 安装：`sudo bash extras/vps-traffic/install.sh`，先检查 Bash>=4、Python>=最低版本、jq 和已有 sing-box 脚本存在；缺依赖给出明确安装建议，不默认下载未知二进制。复制仅本扩展文件，保证重复安装不覆盖数据，可测试的路径注入只用于安装器测试。
- `sb-traffic configure --quota 1TB [--interface eth0] [--reset-day 15] [--utc-offset +08:00] [--mode out] [--name '香港 VPS'] [--address 203.0.113.10] [--used 120GB]`。首次必填只有 quota；interface 缺省取默认路由网卡，address 缺省用与上游相同的公网 IP 查询（`SB_TRAFFIC_OFFLINE=1` 可禁止），两者检测失败才报错要求显式指定；name 缺省取主机名，reset-day 缺省 1，utc-offset 缺省本机时区。默认统计方向 out，输出确认口径；支持 in/out/both。`--port` 默认18080仅绑定127.0.0.1；变更提示 restart 或再次 enable。
- `sb-traffic status [--json]` 显示额度、已用、剩余、账期起止、方向/网卡、采样时间、超额/误差告警。
- `sb-traffic set-used 250GB` 将已用总量设为250GB，在同一事务更新计数器基线，不是追加250GB。允许超额；剩余不为负。
- configure 可更新 quota/name/address 而不丢累计。改变 interface/mode/reset-day/utc-offset 必须同时明确 --used，避免静默重算历史；重锚到新账期和新网卡。
- `sb-traffic rename 'VLESS-REALITY-443.json' '香港 VPS · Reality'` 精确配置文件名映射，默认使用 VPS名+配置名（含端口/域名）防重复。允许中文、空格、#、%和引号，拒绝换行/控制字符；不可路径穿越。
- `sb-traffic enable/disable` 管理独立服务；disable 保留数据，不修改代理服务。失败返回非零，不误报成功。
- `sb-traffic subscription` 输出即时纯URI订阅（无ANSI）；`sb-traffic url` 显示本机 token URL；`sb-traffic rotate-token` 使旧URL失效；`sb-traffic caddy-config sub.example.com` 只输出HTTPS反代片段，不修改现有配置或擅自占端口。
- `sb-traffic uninstall [--purge]` 停用并清理自身服务/命令/程序；默认保留统计数据，purge才删除。原项目卸载不会自动删除此独立扩展，须文档说明。
- `sb-traffic menu`：交互式查看用量、配置向导、校准已用、选择文件重命名、启停服务、显示订阅 URL、生成 Caddy 片段、轮换 token；向导先自动检测网卡/地址/名称/时区，通常只需额度即可完成，其余值可回车保留或经“手动调整高级项”修改；未配置时选择依赖配置的功能会当场询问是否进入向导，不把用户推向命令行。配置可取消并确认后写入，停用/轮换需确认。0/EOF/Ctrl-C 退出子菜单；不提供卸载/清空数据选项。

## 计量与状态

- 从 `/sys/class/net/<interface>/statistics/{rx_bytes,tx_bytes}` 读取单个明确公网网卡，读 boot_id；in=RX，out=TX，both=RX+TX。拒绝lo/路径穿越；不自动累计容器网桥。是整机网卡口径，包含非代理流量，不等于服务商API账单。
- 每60秒采样，启动即采样，SIGTERM尽力保存。后台无需订阅请求即可计量。配置/手动校准时同步采样。
- `/etc/sing-box-traffic`0700、状态及锁0600；配置和统计存一个事务文档；fcntl.flock独立lock文件+原子临时文件fsync/replace，失败清理临时文件。进程内/跨进程并发安全；损坏状态必须报错，不能静默清零。
- 初次以网卡现值为基线，--used校准安装前历史；同boot差分，重启保留累计并计入新boot当前计数；单计数器回退按当前值计入并警告不可恢复缺口，不负数/重复累计。网卡缺失不破坏旧状态，不虚假显示满额。
- 当地每月指定日00:00重置，固定UTC偏移默认+00:00，29-31遇短月取月底，覆盖闰年/跨年；明确不是IANA/DST自动时区。跨界采样按时间比例分摊并告警估算；跨多月仅保留当前账期估算。长时间暂停、断电、网卡重建产生无法恢复的缺口，说明校准办法。
- quota/used支持非负十进制+明确B/KB/MB/GB/TB（1000）和KiB/MiB/GiB/TiB（1024）；整数byte存储，quota>0，拒绝负数/NaN/异常大数/非法日期。remaining=max(0,quota-used)，显示两位GB。

## 导出与订阅

- 适配器输出JSON records（filename/url）；通过精确 is_config_file 指定配置，不regex误选/交互；每个配置隔离变量，跳过Direct无URL，错误输出stderr且全批失败而非静默少节点。全程不get_ip：配置address显式给出。读取原Caddy全局和站点非标准端口，不丢TLS/SNI/path/pbk等参数。不要为统计另写全套协议转换器。
- 普通URI只改#后片段（UTF-8 percent encoding），VMess解码JSON只改ps再base64，连接字段不变。原始名称/映射永不带累计叠加的动态后缀。所有真实节点共享本VPS额度，无额外节点；配置增删改于下次后台刷新生效。
- `serve`常驻采样和刷新缓存，仅127.0.0.1:18080。HTTP请求只读缓存，不执行shell/管理操作。随机>=256bit token路径/sub/<token>，constant-time比较；错误token/未知路径404，无目录/状态暴露，GET/HEAD，禁止写请求，Cache-Control:no-store。不记录token/凭据。采样/导出错误或缓存过期503，不冒充新数据。
- token轮换和配置/手动校准变更需使服务及时刷新（每次请求轻量检查文件版本/事件，或独立watcher短轮询等），文档说明明确最大延迟；不能宣称立即生效却等60秒且仍接受旧token。若采用短轮询，严格测试/说明这段窗口。
- 独立域名Caddy HTTPS反代仅订阅路径。只生成配置片段，由用户选择现有Caddy集成；说明Caddy未安装的部署步骤，以及原脚本Caddy `admin off` 需validate后restart而非reload。不覆盖现有文件/代理路径/证书/防火墙；没有公网域名可通过安全隧道访问回环服务，不默认公网HTTP。
- Sub-Store每VPS新增远程URL订阅再聚合，不继续粘贴静态URI；保留后缀，重命名/过滤避免误伤，按需刷新源缓存。客户端更新后显示最新；改名可能影响记忆选择/固定名称规则。多个VPS按各自账期配置。

## 验收

1. 基线已有文件仅 `src/core.sh` 一个连续菜单入口块允许修改；其余原文件不动，新增实现限定 extras/vps-traffic。原帮助/版本/发布不变。菜单测试覆盖原编号/分发不变、动态末尾编号、重复调用不累加、未安装仅提示，以及交互配置/取消/校准/重命名/危险操作确认。
2. unittest：单位/方向/同boot/重启/回退/缺网卡/损坏状态/重复采样/并发校准/持久化权限/超额/更改口径；每月正常/月末/闰年/跨年/时区/跨周期，完整精确断言。
3. 链接：真实适配器测试VMess、VLESS Reality、Hysteria2、TUIC、Trojan、SS2022、AnyTLS、Socks，多配置隔离、IPv6/非标准Caddy端口、中文空格特殊名、参数不变、不叠后缀、无额外节点、多VPS余额不同。
4. 安装/升级/enable/disable/卸载模板用mock命令和临时根测试，不影响宿主系统；服务失败应非零，数据保留；所有shell bash -n。
5. 本地HTTP GET/HEAD/错误token/未知路径/503/no-store/轮换/并发；关闭测试服务。
6. 尽可能用实际Sub-Store parser和ClashMeta producer验证最终名称、协议参数和数量；没有VPS访问不能声称已验证线上TLS、真实账单或systemd/OpenRC运行。文档完整列出安装校准启用HTTPS导入Sub-Store及升级回滚步骤。
7. 不commit/push，不部署用户VPS。executor实现后由主agent验收，具体缺陷定向修复并复测，不无限扩展范围。

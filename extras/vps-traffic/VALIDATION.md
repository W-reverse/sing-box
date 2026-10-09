# 本地验收记录

验收日期：2026-10-09。

## 基线与交付范围

- 主仓库：`W-reverse/sing-box`，基线 `2d78583b5aecccb0da0148a816bc1495a284509f`。
- 新增实现位于 `extras/vps-traffic/`；对上游已有文件的改动共 3 处：`src/core.sh`（菜单入口 13 行）、`install.sh`（脚本来源指向本 fork，并在安装结束时自动安装扩展，失败仅告警）、`src/init.sh`（同步 `is_sh_repo`）。
- 不包装原 `sing-box` 命令；版本号与发布流程保持不变。`is_sh_repo` 改为指向本 fork，`sing-box update sh` 因此从本 fork 的 release 获取脚本。
- 后两项属于同日“简化安装”修订（在首次验收之后加入）。该修订已重跑 40 项 unittest 与 Debian 12 容器内的完整安装流程，均通过。
- 独立 `sb-traffic` 命令、`/opt/sing-box-traffic` 程序、`/etc/sing-box-traffic` 数据和 `sing-box-traffic` 服务。
- 本轮没有 commit、push，也没有连接或部署用户 VPS。

## 已运行检查

| 检查 | 结果 |
| --- | --- |
| Python 3.9.6 全量 unittest discovery | 40 项通过（含菜单整合） |
| Python 3.14.7 全量 unittest discovery | 40 项通过（含菜单整合） |
| Bash 5.3 对仓库所有 `.sh` 执行 `bash -n` | 通过 |
| 原有跟踪文件差异检查 | 仅 `src/core.sh` 一个入口块，13 行新增 |
| 扩展目录 `__pycache__` / `.pyc` 检查 | 无遗留 |
| 实际 Sub-Store 解析器及 Clash Meta 导出器 | 8 类协议通过 |

复测命令（仓库根目录；临时目录需已存在）：

```bash
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/你的临时目录 \
  python3 -m unittest discover -s extras/vps-traffic/tests -v
bash -n extras/vps-traffic/install.sh
bash -n extras/vps-traffic/adapter.sh
bash -n src/core.sh
git diff --name-only HEAD
# 本地未提交状态下，已有跟踪文件应仅显示 src/core.sh；另检查具体 diff。
```

macOS 开发环境需将 Bash 4+ 放在 PATH 前面；本轮使用 `/opt/homebrew/bin/bash`。生产安装目标仍为 Linux。

## 验收重点

- 使用符合真实 Linux 布局的 `/sys/class/net` 和 `/proc/sys/kernel/random/boot_id` 模拟目录。
- 通过真正的 CLI 子进程验证 `configure → status --json → set-used → status --json`，不是只调用内部函数。
- 已用 120GB / 总额 1TB 时，节点名称自动显示 `香港 VPS · Reality | 剩余 880.00 GB`；手动改为已用 250GB 后剩余 750GB。
- 自定义名称也附带余额；VMess `ps` 和其他 URI fragment 正确编码；不叠加后缀、不新增提示节点；不同 VPS 的余额分别计算，超额剩余归零。
- 覆盖计量方向、同 boot 差分、重启与回退、月底/闰年/跨年/时区、跨账期估算、并发校准、数据权限、损坏状态拒绝、失效旧网卡迁移。
- HTTP GET/HEAD、错误 token、轮换、缓存过期/导出失败、拒绝写操作及服务停止有测试。
- systemd/OpenRC 安装、更新保留数据、启停、失败返回值及卸载清理使用 mock 命令验证。
- 交互菜单：首次配置、已有配置留空保留、改变口径强制校准、中文空格名称、负 UTC 偏移、精确文件选择、取消与确认、EOF/Ctrl-C/0 退出均有测试；实际 CLI 子进程验证向导保存、保留用量和校准结果。
- 原主菜单：原编号和分发不变、动态末尾选项、重复进入不累加、未安装只提示、进入真实 Python 子菜单均有测试。macOS 的 BSD grep 不支持原脚本数字校验的 GNU 正则写法，因此端到端测试仅在 macOS 替换该数字校验辅助函数；生产主脚本未因此改动。

## Sub-Store 兼容性实测

外部测试仓库：`sub-store-org/Sub-Store`，提交 `a3e6106`，package 版本 `2.42.3`。在会话临时目录中安装其测试依赖并直接调用真实 URI parsers 和 Clash Meta producer，未将其依赖加入本扩展。

完整链路：

1. 用上游真实 `src/core.sh` 和本扩展 `adapter.sh` 导出虚拟测试配置的原始 URI。
2. 通过本扩展 `named_subscription` 计算并追加 `剩余 880.00 GB`。
3. 交给 Sub-Store 解析并导出 Clash Meta。
4. 对比名称、所有非名称连接字段，以及节点总数。

通过协议：VMess WS、VLESS Reality、Hysteria2、TUIC、Trojan、Shadowsocks 2022、AnyTLS、Socks。另用合成 URI 验证中文、空格、`#`、引号和 `%` 名称编码。使用的均为测试地址和凭据，不是用户的线上订阅。

## 尚未实机验证与使用限制

- 未在真实 VPS 上运行 systemd/OpenRC 服务、申请公网证书或验证 Caddy HTTPS；没有做真实代理连通性测试，也没有验证用户自己的 Sub-Store 重命名规则和客户端更新行为。
- 网卡统计不是服务商账单 API；出入方向、单位和账单重置时间必须按各 VPS 套餐设置。停采、重启缺口及跨账期分摊可能有误差，用手动校准修正。
- 节点余额随后台采样和订阅刷新更新，不是客户端实时仪表盘；Sub-Store 缓存会影响刷新时效，名称变化可能影响固定节点选择。
- 时区为固定 UTC 偏移，不自动处理夏令时。令牌轮换延迟和紧急撤销步骤见 README。
- 最小菜单入口能降低 merge 冲突范围，但不保证永远无冲突。上游同段菜单改动可能需要保留此入口，内部接口变更也可能需要更新 `adapter.sh`；纯上游脚本覆盖入口后仍可用 `sb-traffic menu`。合并上游后应重新运行测试。

本地验收通过；生产部署及真实账单校准是后续上线步骤。

"""Chinese interactive menu for the sb-traffic command-line interface."""
from __future__ import annotations

import pathlib
from typing import Any, Callable, Dict, List, Optional


class _Cancelled(Exception):
    pass


def _read(input_fn: Callable[[str], str], prompt: str) -> str:
    value = input_fn(prompt).strip()
    if value == "!":
        raise _Cancelled()
    return value


def _ask(input_fn: Callable[[str], str], output_fn: Callable[[str], Any],
         label: str, current: Optional[str] = None,
         default: Optional[str] = None) -> Optional[str]:
    shown = current if current is not None else default
    prompt = label + (" [" + shown + "]" if shown is not None else "") + ": "
    value = _read(input_fn, prompt)
    if value:
        return value
    return default if current is None else None


def _invoke(api: Any, args: List[str], output_fn: Callable[[str], Any]) -> None:
    try:
        result = api.main(args)
    except SystemExit:
        output_fn("命令参数无效；请检查输入后重试。")
    except Exception as exc:
        output_fn("操作失败：" + str(exc))
    else:
        if result not in (None, 0):
            output_fn("操作未成功；请根据上方提示处理。")


def _configure(api: Any, input_fn: Callable[[str], str],
               output_fn: Callable[[str], Any]) -> None:
    state = api.read_state()
    old = state.get("config", {}) if state else {}
    initial = not bool(state)

    interface = _ask(input_fn, output_fn, "网卡名称", old.get("interface"))
    quota = _ask(input_fn, output_fn, "月度额度（如 1TB）",
                 (str(old["quota_bytes"]) + "B") if "quota_bytes" in old else None)
    reset_day = _ask(input_fn, output_fn, "每月重置日",
                     str(old["reset_day"]) if "reset_day" in old else None,
                     "1" if initial else None)
    utc_offset = _ask(input_fn, output_fn, "固定 UTC 偏移（如 +08:00）",
                      api.offset_text(old["utc_offset_seconds"]) if "utc_offset_seconds" in old else None,
                      "+00:00" if initial else None)
    mode = _ask(input_fn, output_fn, "统计方向（in / out / both）", old.get("mode"),
                "out" if initial else None)
    name = _ask(input_fn, output_fn, "VPS 名称", old.get("name"), "VPS" if initial else None)
    address = _ask(input_fn, output_fn, "VPS 地址", old.get("address"))
    port = _ask(input_fn, output_fn, "可选监听端口", str(old["port"]) if "port" in old else None)

    if initial and (not interface or not quota or not address):
        output_fn("首次配置必须填写网卡、月度额度和 VPS 地址；未写入配置。")
        return

    args = ["configure"]
    fields = (("--interface", interface), ("--quota", quota),
              ("--reset-day", reset_day), ("--utc-offset", utc_offset),
              ("--mode", mode), ("--name", name), ("--address", address),
              ("--port", port))
    for flag, value in fields:
        if value is not None:
            # argparse otherwise treats values such as -03:30 as new options.
            if value.startswith("-"):
                args.append(flag + "=" + value)
            else:
                args.extend((flag, value))

    used: Optional[str]
    if initial:
        used = _ask(input_fn, output_fn, "当前已用总量", default="0")
        if used is None:
            used = "0"
        args.extend(("--used", used))
    else:
        changed = False
        try:
            changed = (
                (interface if interface is not None else old["interface"]) != old["interface"]
                or (mode if mode is not None else old["mode"]) != old["mode"]
                or int(reset_day if reset_day is not None else old["reset_day"]) != int(old["reset_day"])
                or api.parse_offset(utc_offset if utc_offset is not None
                                    else api.offset_text(old["utc_offset_seconds"]))
                != int(old["utc_offset_seconds"])
            )
        except (KeyError, TypeError, ValueError):
            # Let the existing configure command report malformed values.
            changed = True
        used = _read(input_fn, "当前已用总量（不变更计量口径时留空以保留历史）: ")
        if changed and not used:
            while not used:
                output_fn("计量口径已变化，必须输入当前已用总量；输入 ! 可取消。")
                used = _read(input_fn, "当前已用总量: ")
        if used:
            args.extend(("--used", used))

    try:
        confirm = _read(input_fn, "确认保存配置？输入 y 执行，其它输入取消：")
    except _Cancelled:
        output_fn("已取消配置；未写入。")
        return
    if confirm != "y":
        output_fn("已取消配置；未写入。")
        return
    _invoke(api, args, output_fn)


def _rename(api: Any, input_fn: Callable[[str], str],
            output_fn: Callable[[str], Any]) -> None:
    state = api.read_state()
    if not state:
        output_fn("尚未配置。")
        return
    config = state["config"]
    root = pathlib.Path(config.get("root") or api.singbox_root())
    conf_dir = root / "conf"
    files = sorted((path for path in conf_dir.glob("*.json")
                    if path.is_file() and not path.is_symlink()), key=lambda path: path.name)
    if not files:
        output_fn("未找到可选择的普通 JSON 配置文件。")
        return
    for number, path in enumerate(files, 1):
        output_fn(f"{number}. {path.name}")
    selection = _read(input_fn, "选择文件编号（! 取消）：")
    try:
        index = int(selection)
    except ValueError:
        output_fn("编号无效；未修改。")
        return
    if index < 1 or index > len(files):
        output_fn("编号超出范围；未修改。")
        return
    label = _read(input_fn, "新的订阅显示名称（! 取消）：")
    if not label:
        output_fn("名称不能为空；未修改。")
        return
    _invoke(api, ["rename", files[index - 1].name, label], output_fn)


def _dispatch(choice: str, api: Any, input_fn: Callable[[str], str],
              output_fn: Callable[[str], Any]) -> None:
    if choice == "1":
        _invoke(api, ["status"], output_fn)
    elif choice == "2":
        _configure(api, input_fn, output_fn)
    elif choice == "3":
        value = _read(input_fn, "校准为本周期已用总量（如 120GB，! 取消）：")
        if value:
            _invoke(api, ["set-used", value], output_fn)
        else:
            output_fn("用量不能为空；未修改。")
    elif choice == "4":
        _rename(api, input_fn, output_fn)
    elif choice in ("5", "6"):
        action = "enable" if choice == "5" else "disable"
        if choice == "6":
            confirm = _read(input_fn, "停用流量统计服务？输入 y 确认：")
            if confirm != "y":
                output_fn("已取消；未执行停用。")
                return
        _invoke(api, [action], output_fn)
    elif choice == "7":
        _invoke(api, ["url"], output_fn)
    elif choice == "8":
        domain = _read(input_fn, "Caddy 使用的域名（! 取消）：")
        if domain:
            _invoke(api, ["caddy-config", domain], output_fn)
        else:
            output_fn("域名不能为空；未生成片段。")
    elif choice == "9":
        output_fn("警告：轮换后需要更新订阅 URL；旧 token 通常约 1 秒、导出阻塞时约 61 秒后撤销。")
        output_fn("紧急撤销请先停用统计服务，轮换后再启用；不承诺硬实时撤销。")
        confirm = _read(input_fn, "仍要轮换？输入 y 确认：")
        if confirm == "y":
            _invoke(api, ["rotate-token"], output_fn)
        else:
            output_fn("已取消；token 未轮换。")


def run_menu(api: Any, input_fn: Optional[Callable[[str], str]] = None,
             output_fn: Optional[Callable[[str], Any]] = None) -> int:
    """Run the menu; API calls are argument lists, never shell command strings."""
    input_fn = input if input_fn is None else input_fn
    output_fn = print if output_fn is None else output_fn
    while True:
        output_fn("\n=== VPS 流量统计管理 ===")
        output_fn("1. 查看用量")
        output_fn("2. 配置网卡 / 额度 / 重置日 / 时区 / 方向 / VPS 名称 / 地址 / 端口")
        output_fn("3. 校准已用总量")
        output_fn("4. 重命名真实配置文件的订阅显示名")
        output_fn("5. 启用服务")
        output_fn("6. 停用服务")
        output_fn("7. 显示订阅 URL")
        output_fn("8. 生成 Caddy 片段")
        output_fn("9. 轮换 token")
        output_fn("0. 退出")
        try:
            choice = input_fn("请选择：").strip()
        except (EOFError, KeyboardInterrupt):
            output_fn("已退出菜单。")
            return 0
        if choice == "0":
            output_fn("已退出菜单。")
            return 0
        if choice not in {str(number) for number in range(1, 10)}:
            output_fn("选项无效，请输入 0–9。")
            continue
        try:
            _dispatch(choice, api, input_fn, output_fn)
        except _Cancelled:
            output_fn("已取消配置；未写入。" if choice == "2" else "已取消；未执行操作。")
        except (EOFError, KeyboardInterrupt):
            output_fn("已退出菜单。")
            return 0
        except Exception as exc:
            output_fn("操作失败：" + str(exc))

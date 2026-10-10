#!/usr/bin/env python3
"""Independent sing-box VPS traffic accounting and loopback subscription service."""
from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import datetime as dt
import decimal
import fcntl
import hashlib
import hmac
import http.server
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

VERSION = "1.0.0"
MIN_PYTHON = (3, 8)
DEFAULT_HOME = "/opt/sing-box-traffic"
DEFAULT_DATA = "/etc/sing-box-traffic"
DEFAULT_ROOT = "/etc/sing-box"
DEFAULT_PORT = 18080
MAX_BYTES = 10 ** 24
UNITS = {
    "B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
    "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4,
}
SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d*)?|\.\d+)\s*([A-Za-z]+)?\s*$")
OFFSET_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class TrafficError(Exception):
    pass


def env_path(name: str, default: str) -> pathlib.Path:
    return pathlib.Path(os.environ.get(name, default)).expanduser()


def home_dir() -> pathlib.Path:
    return env_path("SB_TRAFFIC_HOME", DEFAULT_HOME)


def data_dir() -> pathlib.Path:
    return env_path("SB_TRAFFIC_DATA", DEFAULT_DATA)


def singbox_root() -> pathlib.Path:
    return env_path("SB_TRAFFIC_ROOT", DEFAULT_ROOT)


def state_path() -> pathlib.Path:
    return data_dir() / "state.json"


def lock_path() -> pathlib.Path:
    return data_dir() / "state.lock"


def ensure_private_dir(path: pathlib.Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


@contextlib.contextmanager
def locked_state():
    ensure_private_dir(data_dir())
    fd = os.open(str(lock_path()), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_state() -> Dict[str, Any]:
    path = state_path()
    try:
        with path.open("r", encoding="utf-8") as stream:
            state = json.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise TrafficError("统计状态损坏或不可读；为避免静默清零，已停止操作") from exc
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise TrafficError("统计状态格式/版本无效；为避免静默清零，已停止操作")
    config = state.get("config")
    required = ("interface", "quota_bytes", "address", "mode", "reset_day", "utc_offset_seconds", "name", "port")
    try:
        valid = (isinstance(config, dict) and all(key in config for key in required)
                 and isinstance(config.get("interface"), str)
                 and isinstance(config.get("address"), str)
                 and isinstance(config.get("name"), str)
                 and isinstance(config.get("root", ""), str)
                 and config.get("mode") in ("in", "out", "both")
                 and type(config.get("quota_bytes")) is int
                 and 0 < config["quota_bytes"] <= MAX_BYTES
                 and type(config.get("reset_day")) is int and 1 <= config["reset_day"] <= 31
                 and type(config.get("utc_offset_seconds")) is int
                 and abs(config["utc_offset_seconds"]) <= 14 * 3600
                 and type(config.get("port")) is int and 1 <= config["port"] <= 65535
                 and type(state.get("used_bytes")) is int and state["used_bytes"] >= 0
                 and type(state.get("sampled_at")) in (int, float)
                 and math.isfinite(float(state["sampled_at"]))
                 and isinstance(state.get("token"), str)
                 and re.fullmatch(r"[A-Za-z0-9_-]{43}", state["token"]) is not None)
    except (KeyError, TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise TrafficError("统计状态字段无效；为避免静默清零，已停止操作")
    counter, boot_id, period_start = (state.get("counter"), state.get("boot_id"), state.get("period_start"))
    if type(counter) is not int or counter < 0 or not isinstance(boot_id, str) or not boot_id:
        raise TrafficError("统计状态计数器或 boot_id 无效；为避免静默清零，已停止操作")
    if type(period_start) not in (int, float) or not math.isfinite(float(period_start)):
        raise TrafficError("统计账期基线无效；为避免静默清零，已停止操作")
    publication = state.get("publication")
    if publication is not None:
        try:
            publication_valid = (
                isinstance(publication, dict)
                and validate_domain(publication["domain"]) == publication["domain"]
                and type(publication["https_port"]) is int and 1 <= publication["https_port"] <= 65535
                and type(publication["backend_port"]) is int and 1 <= publication["backend_port"] <= 65535
                and type(publication["verified"]) is bool)
        except (KeyError, TypeError, TrafficError):
            publication_valid = False
        if not publication_valid:
            raise TrafficError("订阅域名状态无效；旧统计未更改")
    renames, warnings = state.get("renames", {}), state.get("warnings", [])
    if (not isinstance(renames, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in renames.items())
            or not isinstance(warnings, list) or any(not isinstance(v, str) for v in warnings)):
        raise TrafficError("统计状态字段无效；为避免静默清零，已停止操作")
    return state


def write_state(state: Dict[str, Any]) -> None:
    ensure_private_dir(data_dir())
    destination = state_path()
    fd, temp_name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=str(data_dir()))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, destination)
        os.chmod(destination, 0o600)
        dir_fd = os.open(str(data_dir()), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


def parse_size(text: str, *, positive: bool = False) -> int:
    match = SIZE_RE.fullmatch(text)
    if not match:
        raise TrafficError("容量格式无效，请使用非负数字和 B/KB/MB/GB/TB 或 KiB/MiB/GiB/TiB")
    unit = (match.group(2) or "B").upper()
    if unit not in UNITS:
        raise TrafficError("不支持的容量单位: " + unit)
    try:
        value = decimal.Decimal(match.group(1)) * UNITS[unit]
        number = int(value.quantize(decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP))
    except (decimal.InvalidOperation, ValueError, OverflowError) as exc:
        raise TrafficError("容量数值无效") from exc
    if number < 0 or number > MAX_BYTES or (positive and number == 0):
        raise TrafficError("容量超出允许范围")
    return number


def format_size(value: int) -> str:
    return f"{value / (1000 ** 3):.2f} GB"


def parse_offset(value: str) -> int:
    match = OFFSET_RE.fullmatch(value)
    if not match:
        raise TrafficError("UTC 偏移必须为 +HH:MM 或 -HH:MM，例如 +08:00")
    hours, minutes = int(match.group(2)), int(match.group(3))
    if minutes > 59 or hours > 14 or (hours == 14 and minutes != 0):
        raise TrafficError("UTC 偏移范围须为 -14:00 至 +14:00")
    seconds = (hours * 60 + minutes) * 60
    return seconds if match.group(1) == "+" else -seconds


def offset_text(seconds: int) -> str:
    sign = "+" if seconds >= 0 else "-"
    minutes = abs(seconds) // 60
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def zone_for(config: Dict[str, Any]) -> dt.timezone:
    return dt.timezone(dt.timedelta(seconds=int(config["utc_offset_seconds"])))


def month_days(year: int, month: int) -> int:
    if month == 12:
        next_month = dt.date(year + 1, 1, 1)
    else:
        next_month = dt.date(year, month + 1, 1)
    return (next_month - dt.timedelta(days=1)).day


def reset_boundary(year: int, month: int, day: int, zone: dt.timezone) -> dt.datetime:
    return dt.datetime(year, month, min(day, month_days(year, month)), tzinfo=zone)


def billing_period(now: float, config: Dict[str, Any]) -> Tuple[float, float]:
    zone = zone_for(config)
    local = dt.datetime.fromtimestamp(now, zone)
    this = reset_boundary(local.year, local.month, int(config["reset_day"]), zone)
    if local < this:
        if local.month == 1:
            year, month = local.year - 1, 12
        else:
            year, month = local.year, local.month - 1
        start = reset_boundary(year, month, int(config["reset_day"]), zone)
        end = this
    else:
        start = this
        if local.month == 12:
            year, month = local.year + 1, 1
        else:
            year, month = local.year, local.month + 1
        end = reset_boundary(year, month, int(config["reset_day"]), zone)
    return start.timestamp(), end.timestamp()


def period_display(epoch: float, config: Dict[str, Any]) -> str:
    return dt.datetime.fromtimestamp(epoch, zone_for(config)).isoformat(timespec="seconds")


def read_counter(interface: str) -> Tuple[int, int, str]:
    if interface == "lo" or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", interface) or interface in (".", ".."):
        raise TrafficError("网卡名无效；lo 和路径字符不允许")
    sys_root = pathlib.Path(os.environ.get("SB_TRAFFIC_SYS_ROOT", "/sys"))
    proc_root = pathlib.Path(os.environ.get("SB_TRAFFIC_PROC_ROOT", "/proc"))
    base = sys_root / "class" / "net" / interface / "statistics"
    try:
        rx = int((base / "rx_bytes").read_text().strip())
        tx = int((base / "tx_bytes").read_text().strip())
        boot_id = (proc_root / "sys" / "kernel" / "random" / "boot_id").read_text().strip()
    except (OSError, ValueError) as exc:
        raise TrafficError(f"无法读取网卡 {interface} 计数器或 boot_id；旧统计未更改") from exc
    if min(rx, tx) < 0 or not boot_id:
        raise TrafficError("网卡计数器或 boot_id 内容无效")
    return rx, tx, boot_id

def selected_counter(rx: int, tx: int, mode: str) -> int:
    if mode == "in":
        return rx
    if mode == "out":
        return tx
    if mode == "both":
        return rx + tx
    raise TrafficError("统计方向无效")


def _latest_overlap_fraction(old_time: float, now: float, current_start: float) -> float:
    if now <= old_time:
        return 1.0
    overlap = max(0.0, now - max(old_time, current_start))
    return min(1.0, overlap / (now - old_time))


def sample_state(state: Dict[str, Any], now: Optional[float] = None) -> None:
    now = time.time() if now is None else float(now)
    config = state["config"]
    rx, tx, boot_id = read_counter(config["interface"])
    counter = selected_counter(rx, tx, config["mode"])
    start, _end = billing_period(now, config)
    old_period = state.get("period_start")
    warnings = list(state.get("warnings", []))
    used = int(state.get("used_bytes", 0))
    previous_counter = state.get("counter")
    previous_boot = state.get("boot_id")
    previous_time = state.get("sampled_at")
    if old_period is None:
        state["period_start"] = start
        state["used_bytes"] = used
    elif float(old_period) != start:
        delta = 0
        if previous_counter is not None and previous_boot == boot_id and counter >= int(previous_counter):
            delta = counter - int(previous_counter)
        elif previous_counter is not None:
            # After a reboot or counter reset, only the current counter is observable.
            delta = counter
            warnings.append("跨账期且网卡/boot 基线变化，存在不可恢复计量缺口")
        if previous_time is not None and delta:
            fraction = _latest_overlap_fraction(float(previous_time), now, start)
            state["used_bytes"] = int(decimal.Decimal(delta * fraction).quantize(decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP))
            warnings.append("采样跨越月度重置边界，按时间比例估算本周期流量")
        else:
            state["used_bytes"] = 0
        state["period_start"] = start
    elif previous_counter is None:
        state["counter"] = counter
    elif previous_boot != boot_id:
        state["used_bytes"] = int(state.get("used_bytes", 0)) + counter
        warnings.append("检测到系统重启，已将新 boot 网卡计数加入累计")
    elif counter >= int(previous_counter):
        state["used_bytes"] = int(state.get("used_bytes", 0)) + counter - int(previous_counter)
    else:
        state["used_bytes"] = int(state.get("used_bytes", 0)) + counter
        warnings.append("网卡计数器回退/重建；已按当前值计入，回退缺口不可恢复")
    state["counter"] = counter
    state["boot_id"] = boot_id
    state["sampled_at"] = now
    state["warnings"] = list(dict.fromkeys(warnings))[-20:]


def current_state(sample: bool = False) -> Dict[str, Any]:
    with locked_state():
        state = read_state()
        if not state:
            raise TrafficError("尚未配置；请先运行 sb-traffic configure")
        if sample:
            sample_state(state)
            write_state(state)
        return state


def validate_name(name: str) -> str:
    if not name or CONTROL_RE.search(name) or len(name.encode("utf-8")) > 1024:
        raise TrafficError("名称不能为空、不能含控制字符，且 UTF-8 长度不能超过 1024 字节")
    return name


TRACE_HOST = "one.one.one.one"
TRACE_PATH = "/cdn-cgi/trace"


def proc_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("SB_TRAFFIC_PROC_ROOT", "/proc"))


def _route_interface_from_ip() -> Optional[str]:
    try:
        result = subprocess.run(["ip", "-o", "route", "show", "default"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    for line in result.stdout.splitlines():
        match = re.search(r"\bdev\s+(\S+)", line)
        if match and match.group(1) != "lo":
            return match.group(1)
    return None


def detect_interface() -> Optional[str]:
    """Best-effort default-route interface; None when it cannot be determined."""
    candidates: List[Tuple[int, str]] = []
    try:
        text = (proc_root() / "net" / "route").read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()[1:]
    except OSError:
        lines = []
    for line in lines:
        fields = line.split()
        if len(fields) < 8 or fields[0] == "lo" or fields[1] != "00000000":
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", fields[0]):
            continue
        try:
            if not int(fields[3], 16) & 0x1:  # RTF_UP
                continue
            candidates.append((int(fields[6]), fields[0]))
        except ValueError:
            continue
    if candidates:
        return min(candidates)[1]
    return _route_interface_from_ip()


def _fetch_trace(family: int, timeout: float = 6.0) -> Optional[str]:
    """GET the upstream IP-echo endpoint over one explicit address family."""
    try:
        infos = socket.getaddrinfo(TRACE_HOST, 443, family, socket.SOCK_STREAM)
    except OSError:
        return None
    payload = (f"GET {TRACE_PATH} HTTP/1.1\r\nHost: {TRACE_HOST}\r\n"
               "User-Agent: sb-traffic\r\nConnection: close\r\n\r\n").encode("ascii")
    context = ssl.create_default_context()
    for af, socktype, proto, _canonical, sockaddr in infos:
        chunks: List[bytes] = []
        try:
            with socket.socket(af, socktype, proto) as raw:
                raw.settimeout(timeout)
                with context.wrap_socket(raw, server_hostname=TRACE_HOST) as tls:
                    tls.connect(sockaddr)
                    tls.sendall(payload)
                    total = 0
                    while total < 8192:
                        data = tls.recv(4096)
                        if not data:
                            break
                        chunks.append(data)
                        total += len(data)
        except (OSError, ssl.SSLError):
            continue
        body = b"".join(chunks).decode("utf-8", "replace")
        return body.split("\r\n\r\n", 1)[-1]
    return None


def detect_address(fetch: Optional[Callable[[int], Optional[str]]] = None) -> Optional[str]:
    """Public address of this host, using the same lookup as the upstream script.

    IPv4 is preferred; IPv6 is used only when no IPv4 address is reported.
    """
    if fetch is None and os.environ.get("SB_TRAFFIC_OFFLINE"):
        return None
    fetch = _fetch_trace if fetch is None else fetch
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            body = fetch(family)
        except Exception:
            body = None
        for line in (body or "").splitlines():
            if line.startswith("ip="):
                value = line[3:].strip()
                if value and not CONTROL_RE.search(value) and len(value) <= 253:
                    return value
    return None


def default_name() -> str:
    try:
        name = socket.gethostname().strip()
    except OSError:
        name = ""
    if not name or name.split(".")[0].lower() in ("localhost", "local"):
        return "VPS"
    return name


def local_utc_offset() -> int:
    offset = dt.datetime.now().astimezone().utcoffset()
    return int(offset.total_seconds()) if offset else 0

def configure(args: argparse.Namespace) -> None:
    with locked_state():
        state = read_state()
        old = state.get("config", {})
        old_port = old.get("port")
        initial = not bool(state)
        interface = args.interface or old.get("interface") or detect_interface()
        quota_text = args.quota
        address = args.address or old.get("address") or detect_address()
        if initial and not quota_text:
            raise TrafficError("请用 --quota 指定月度额度（如 --quota 1TB），或在交互菜单中配置")
        if not interface:
            raise TrafficError("无法自动检测默认网卡；请用 --interface 指定")
        if not address:
            raise TrafficError("无法自动检测公网地址；请用 --address 指定（VPS 的 IP 或域名）")
        if interface == "lo" or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", interface):
            raise TrafficError("网卡名无效")
        new_quota = parse_size(quota_text, positive=True) if quota_text else int(old["quota_bytes"])
        mode = args.mode or old.get("mode", "out")
        reset_day = args.reset_day if args.reset_day is not None else old.get("reset_day", 1)
        offset = parse_offset(args.utc_offset) if args.utc_offset is not None else int(old.get("utc_offset_seconds", 0))
        if mode not in ("in", "out", "both"):
            raise TrafficError("--mode 只接受 in、out、both")
        if not 1 <= int(reset_day) <= 31:
            raise TrafficError("--reset-day 必须在 1..31 之间")
        name = validate_name(args.name if args.name is not None else old.get("name", "VPS"))
        port = args.port if args.port is not None else int(old.get("port", DEFAULT_PORT))
        if not 1 <= int(port) <= 65535:
            raise TrafficError("--port 必须在 1..65535 之间")
        if not address or CONTROL_RE.search(address) or len(address) > 253:
            raise TrafficError("--address 必须提供有效地址")
        root = str(pathlib.Path(args.root).expanduser()) if args.root else str(old.get("root", singbox_root()))
        new_config = {"interface": interface, "quota_bytes": new_quota, "address": address,
                      "mode": mode, "reset_day": int(reset_day), "utc_offset_seconds": offset,
                      "name": name, "port": int(port), "root": root}
        reanchor = initial or any(new_config[k] != old.get(k) for k in ("interface", "mode", "reset_day", "utc_offset_seconds"))
        if not initial and reanchor and args.used is None:
            raise TrafficError("更改 interface/mode/reset-day/utc-offset 必须同时指定 --used 以重锚")
        if initial:
            used = parse_size(args.used, positive=False) if args.used is not None else 0
            rx, tx, boot = read_counter(interface)
            counter = selected_counter(rx, tx, mode)
            start, _ = billing_period(time.time(), new_config)
            state = {"schema": 1, "config": new_config, "used_bytes": used,
                     "counter": counter, "boot_id": boot, "sampled_at": time.time(),
                     "period_start": start, "token": secrets.token_urlsafe(32),
                     "renames": {}, "warnings": []}
        else:
            if reanchor:
                used = parse_size(args.used, positive=False)
                rx, tx, boot = read_counter(interface)
                state["used_bytes"] = used
                state["counter"] = selected_counter(rx, tx, mode)
                state["boot_id"] = boot
                state["sampled_at"] = time.time()
                state["period_start"] = billing_period(state["sampled_at"], new_config)[0]
                state["warnings"] = []
            else:
                sample_state(state)
                if args.used is not None:
                    state["used_bytes"] = parse_size(args.used)
            state["config"] = new_config
        write_state(state)
    print(f"配置已保存：{interface} / {mode}，额度 {format_size(new_quota)}，账期每月 {reset_day} 日；网卡统计包含该网卡全部流量。")
    print("固定 UTC 偏移：" + offset_text(offset) + ("（不自动处理夏令时）" if offset else ""))
    print("首次启用服务前请确认 --address 及计量口径正确。")
    if old_port is not None and int(old_port) != int(port):
        print("监听端口已更改；需执行 sb-traffic disable 后再 enable，或由服务管理器 restart。")
        if state.get("publication"):
            print("请重新运行菜单 10 配置订阅域名，更新反代的后端端口。")


def set_used(value: str) -> None:
    amount = parse_size(value)
    with locked_state():
        state = read_state()
        if not state:
            raise TrafficError("尚未配置")
        sample_state(state)
        state["used_bytes"] = amount
        state["warnings"] = []
        write_state(state)
    print("本周期已用总量已校准为 " + format_size(amount))


def status_dict(state: Dict[str, Any]) -> Dict[str, Any]:
    config = state["config"]
    quota = int(config["quota_bytes"])
    used = int(state["used_bytes"])
    start, end = billing_period(float(state.get("sampled_at", time.time())), config)
    return {"name": config["name"], "address": config["address"], "interface": config["interface"],
            "mode": config["mode"], "quota_bytes": quota, "used_bytes": used,
            "remaining_bytes": max(0, quota - used), "over_quota": used > quota,
            "period_start": period_display(start, config), "period_end": period_display(end, config),
            "reset_day": config["reset_day"], "utc_offset": offset_text(config["utc_offset_seconds"]),
            "sampled_at": dt.datetime.fromtimestamp(float(state["sampled_at"]), dt.timezone.utc).isoformat(timespec="seconds"),
            "warnings": list(state.get("warnings", []))}


def print_status(json_output: bool) -> None:
    state = current_state(sample=True)
    info = status_dict(state)
    if json_output:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return
    print(f"{info['name']} ({info['address']})")
    print(f"已用 {format_size(info['used_bytes'])} / {format_size(info['quota_bytes'])}；剩余 {format_size(info['remaining_bytes'])}")
    print(f"账期 {info['period_start']} 至 {info['period_end']}；{info['interface']} / {info['mode']}；采样 {info['sampled_at']}")
    if info["over_quota"]:
        print("警告：本周期已超额。")
    for warning in info["warnings"]:
        print("警告：" + warning)


def adapter_path() -> pathlib.Path:
    return home_dir() / "adapter.sh"


def export_records(state: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    state = state if state is not None else current_state(sample=False)
    config = state["config"]
    adapter = adapter_path()
    if not adapter.is_file():
        raise TrafficError(f"找不到适配器：{adapter}（请安装扩展）")
    env = os.environ.copy()
    env.update({"SB_TRAFFIC_ROOT": config.get("root", str(singbox_root())), "SB_TRAFFIC_ADDRESS": config["address"]})
    try:
        result = subprocess.run(["/usr/bin/env", "bash", str(adapter)], env=env, check=False,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TrafficError(f"运行 sing-box 链接适配器失败：{exc}") from exc
    if result.returncode:
        raise TrafficError("链接导出失败：" + result.stderr.strip())
    try:
        records = json.loads(result.stdout)
    except ValueError as exc:
        raise TrafficError("适配器输出不是有效 JSON") from exc
    if not isinstance(records, list) or any(not isinstance(x, dict) or not x.get("filename") or not x.get("url") for x in records):
        raise TrafficError("适配器输出 records 格式无效")
    return records


def set_fragment(url: str, name: str) -> str:
    name = validate_name(name)
    if url.startswith("vmess://"):
        raw = url[len("vmess://"):]
        try:
            payload = base64.urlsafe_b64decode(raw + "=" * ((-len(raw)) % 4))
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise TrafficError("无法解析适配器产生的 VMess URI") from exc
        if not isinstance(data, dict):
            raise TrafficError("VMess URI payload 不是 JSON 对象")
        data["ps"] = name
        encoded = base64.b64encode(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii")
        return "vmess://" + encoded
    parts = urllib.parse.urlsplit(url)
    if not parts.scheme:
        raise TrafficError("适配器输出了无效 URI")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query,
                                    urllib.parse.quote(name, safe="", encoding="utf-8")))


def named_subscription(records: Optional[List[Dict[str, str]]] = None,
                       state: Optional[Dict[str, Any]] = None) -> List[str]:
    state = state if state is not None else current_state(sample=False)
    records = records if records is not None else export_records(state)
    config = state["config"]
    mappings = state.get("renames", {})
    urls: List[str] = []
    for record in records:
        filename = record["filename"]
        base_label = mappings.get(filename)
        if base_label is None:
            stem = pathlib.PurePosixPath(filename).stem
            base_label = f"{config['name']} · {stem}"
        balance = max(0, int(config["quota_bytes"]) - int(state["used_bytes"]))
        label = f"{base_label} | 剩余 {format_size(balance)}"
        urls.append(set_fragment(record["url"], label))
    if not urls:
        raise TrafficError("未导出任何可连接节点；拒绝发布空订阅")
    return urls


def subscription() -> None:
    current_state(sample=True)
    print("\n".join(named_subscription()))


def rename(filename: str, label: str) -> None:
    if pathlib.PurePath(filename).name != filename or filename in (".", "..") or CONTROL_RE.search(filename):
        raise TrafficError("配置文件名必须为精确 basename，不能路径穿越")
    validate_name(label)
    with locked_state():
        state = read_state()
        if not state:
            raise TrafficError("尚未配置")
        conf = pathlib.Path(state["config"].get("root", str(singbox_root()))) / "conf" / filename
        if not conf.is_file() or conf.is_symlink():
            raise TrafficError(f"配置文件不存在或不是普通文件：{filename}")
        mappings = state.setdefault("renames", {})
        mappings[filename] = label
        write_state(state)
    print(f"已将 {filename} 显示为 {label}")


def token_url(local: bool = False) -> str:
    state = current_state(sample=False)
    publication = state.get("publication")
    if publication and not local:
        port = publication["https_port"]
        suffix = "" if port == 443 else f":{port}"
        return f"https://{publication['domain']}{suffix}/sub/{state['token']}"
    return f"http://127.0.0.1:{state['config']['port']}/sub/{state['token']}"


def print_url(local: bool = False) -> None:
    state = current_state(sample=False)
    publication = state.get("publication")
    if not local:
        if not publication:
            print("尚未配置公网域名；下面仅为本机地址。请在菜单 10 配置订阅域名。", file=sys.stderr)
        elif publication["backend_port"] != state["config"]["port"]:
            print("订阅端口已变化；请重跑菜单 10 更新反代，当前公网地址可能不可用。", file=sys.stderr)
        elif not publication["verified"]:
            print("反代已配置，但 HTTPS 尚未验证可用；请检查 DNS/证书，重跑菜单 10。", file=sys.stderr)
    print(token_url(local=local))


def rotate_token() -> None:
    with locked_state():
        state = read_state()
        if not state:
            raise TrafficError("尚未配置")
        state["token"] = secrets.token_urlsafe(32)
        write_state(state)
    print("旧 token 通常约 1 秒后撤销；若后台正在导出节点，可能需要最多约 61 秒。")
    print("紧急撤销请依次 disable 服务、rotate-token，再 enable 服务。")
    print(token_url())


def validate_domain(value: str) -> str:
    if not isinstance(value, str):
        raise TrafficError("请输入有效的公网域名")
    value = value.strip().lower()
    if value.endswith("."):
        value = value[:-1]
    labels = value.split(".")
    if (len(value) > 253 or len(labels) < 2 or labels[-1].isdigit()
            or labels[-1] in ("localhost", "local", "internal") or value.endswith(".home.arpa")
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
        raise TrafficError("请输入纯域名，如 sub.example.com；不要带协议、端口、路径、IP 或通配符")
    return value


def configure_domain(value: str) -> None:
    import importlib.util
    path = pathlib.Path(__file__).with_name("publish.py")
    spec = importlib.util.spec_from_file_location("sb_traffic_publish", path)
    if spec is None or spec.loader is None:
        raise TrafficError("无法加载域名部署模块，请更新流量扩展")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        verified = module.deploy(sys.modules[__name__], value)
    except module.PublicationError as exc:
        raise TrafficError(str(exc)) from exc
    if not verified:
        raise TrafficError("反代已部署，但 HTTPS 未验证通过；修复 DNS/端口/证书后可重试，未宣称公网可用")


def caddy_config(domain: str) -> None:
    domain = validate_domain(domain)
    state = current_state(sample=False)
    port = state["config"]["port"]
    print(f"{domain} {{\n\t@traffic path /sub/*\n\treverse_proxy @traffic 127.0.0.1:{port}\n}}")


class SubscriptionHandler(http.server.BaseHTTPRequestHandler):
    server_version = "sb-traffic"
    sys_version = ""

    def _respond(self, body: bytes, status: int, head: bool = False) -> None:
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _auth(self) -> Tuple[Dict[str, Any], bool]:
        shared = self.server.shared  # type: ignore[attr-defined]
        path = urllib.parse.urlsplit(self.path).path
        supplied = path[len("/sub/"):] if path.startswith("/sub/") else ""
        token = shared["token"]
        matches = hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))
        return shared, bool(matches and path == "/sub/" + token)

    def do_GET(self) -> None:
        shared, authorized = self._auth()
        if not authorized:
            self._respond(b"Not found\n", 404)
        elif shared.get("error") or time.time() - shared.get("updated", 0) > 125:
            self._respond(b"Subscription temporarily unavailable\n", 503)
        else:
            self._respond(shared["body"], 200)

    def do_HEAD(self) -> None:
        shared, authorized = self._auth()
        if not authorized:
            self._respond(b"Not found\n", 404, head=True)
        elif shared.get("error") or time.time() - shared.get("updated", 0) > 125:
            self._respond(b"Subscription temporarily unavailable\n", 503, head=True)
        else:
            self._respond(shared["body"], 200, head=True)

    def _reject_write(self) -> None:
        _shared, authorized = self._auth()
        if authorized:
            self._respond(b"Method not allowed\n", 405)
        else:
            self._respond(b"Not found\n", 404)

    do_POST = _reject_write
    do_PUT = _reject_write
    do_DELETE = _reject_write
    do_PATCH = _reject_write
    do_OPTIONS = _reject_write
    do_TRACE = _reject_write

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never log request paths: the path contains the bearer token.
        sys.stderr.write("sb-traffic HTTP request handled\n")


class LoopbackServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(stop_event: Optional[threading.Event] = None) -> None:
    stop = stop_event or threading.Event()
    state = current_state(sample=False)
    initial = state["config"]
    server = LoopbackServer(("127.0.0.1", int(initial["port"])), SubscriptionHandler)
    shared: Dict[str, Any] = {"token": state["token"], "body": b"", "updated": 0.0, "error": None}
    server.shared = shared  # type: ignore[attr-defined]
    signature: Optional[Tuple[int, int]] = None
    last_sample = 0.0
    last_refresh = 0.0

    def refresh(force: bool = False) -> None:
        nonlocal signature, last_sample, last_refresh
        try:
            with locked_state():
                current = read_state()
                if not current:
                    raise TrafficError("配置已删除")
                stat = state_path().stat()
                sig = (stat.st_mtime_ns, stat.st_size)
                now = time.time()
                if force or now - last_sample >= 60:
                    sample_state(current, now)
                    write_state(current)
                    stat = state_path().stat()
                    sig = (stat.st_mtime_ns, stat.st_size)
                    last_sample = now
                changed = force or sig != signature or now - last_refresh >= 60
                if changed:
                    # Revoke a rotated token immediately; while export runs return 503.
                    shared["token"] = current["token"]
                    shared["error"] = "refresh pending"
            if changed:
                records = export_records(current)
                urls = named_subscription(records, current)
                shared["body"] = ("\n".join(urls) + "\n").encode("utf-8")
                shared["updated"] = now
                shared["error"] = None
                signature = sig
                last_refresh = now
        except Exception as exc:
            shared["error"] = str(exc)

    refresh(force=True)

    def poll() -> None:
        while not stop.wait(1.0):
            refresh()
        server.shutdown()

    thread = threading.Thread(target=poll, name="sb-traffic-refresh", daemon=True)
    thread.start()
    old_handlers: Dict[int, Any] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError):
            old_handlers[signum] = signal.signal(signum, lambda _s, _f: stop.set())
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stop.set()
        try:
            with locked_state():
                final_state = read_state()
                if final_state:
                    sample_state(final_state)
                    write_state(final_state)
        except Exception:
            sys.stderr.write("sb-traffic: final traffic sample failed\n")
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def run_service(action: str) -> None:
    script = home_dir() / "install.sh"
    if not script.is_file():
        raise TrafficError("扩展安装入口不存在；请先安装")
    result = subprocess.run(["/usr/bin/env", "bash", str(script), "service", action], check=False)
    if result.returncode:
        raise TrafficError(f"服务操作失败（退出码 {result.returncode}）")


def uninstall(purge: bool) -> None:
    script = home_dir() / "install.sh"
    if not script.is_file():
        raise TrafficError("扩展安装入口不存在")
    args = ["/usr/bin/env", "bash", str(script), "uninstall"]
    if purge:
        args.append("--purge")
    result = subprocess.run(args, check=False)
    if result.returncode:
        raise TrafficError(f"卸载失败（退出码 {result.returncode}）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sb-traffic", description="独立 VPS 网卡流量统计与 sing-box 节点订阅")
    parser.add_argument("--version", action="version", version=f"sb-traffic {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    config = sub.add_parser("configure", help="配置网卡、额度和月账期")
    config.add_argument("--interface")
    config.add_argument("--quota")
    config.add_argument("--reset-day", type=int)
    config.add_argument("--utc-offset")
    config.add_argument("--mode", choices=("in", "out", "both"))
    config.add_argument("--name")
    config.add_argument("--address")
    config.add_argument("--used")
    config.add_argument("--port", type=int)
    config.add_argument("--root", help="sing-box 安装根目录；默认保留既有配置或使用 SB_TRAFFIC_ROOT")
    status = sub.add_parser("status", help="显示当前账期用量")
    status.add_argument("--json", action="store_true")
    used = sub.add_parser("set-used", help="校准本周期已用总量")
    used.add_argument("value")
    ren = sub.add_parser("rename", help="为精确配置文件名设置订阅显示名")
    ren.add_argument("filename")
    ren.add_argument("name")
    sub.add_parser("enable", help="启用独立后台服务")
    sub.add_parser("disable", help="停用独立后台服务")
    sub.add_parser("subscription", help="输出当前纯 URI 订阅")
    url = sub.add_parser("url", help="显示公网订阅地址；未配置域名时为本机地址")
    url.add_argument("--local", action="store_true", help="仅显示回环诊断地址")
    sub.add_parser("rotate-token", help="使旧订阅 URL 失效")
    caddy = sub.add_parser("caddy-config", help="输出独立 HTTPS 反代片段")
    caddy.add_argument("domain")
    domain = sub.add_parser("domain", help="自动部署 Caddy HTTPS 订阅反代")
    domain.add_argument("name", help="独立订阅子域名，如 sub.example.com")
    sub.add_parser("menu", help="打开中文交互菜单")
    sub.add_parser("serve", help=argparse.SUPPRESS)
    un = sub.add_parser("uninstall", help="卸载扩展（默认保留数据）")
    un.add_argument("--purge", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if sys.version_info < MIN_PYTHON:
        print("sb-traffic 需要 Python 3.8 或更新版本", file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            configure(args)
        elif args.command == "status":
            print_status(args.json)
        elif args.command == "set-used":
            set_used(args.value)
        elif args.command == "rename":
            rename(args.filename, args.name)
        elif args.command == "enable":
            run_service("enable")
        elif args.command == "disable":
            run_service("disable")
        elif args.command == "subscription":
            subscription()
        elif args.command == "url":
            print_url(local=args.local)
        elif args.command == "rotate-token":
            rotate_token()
        elif args.command == "caddy-config":
            caddy_config(args.domain)
        elif args.command == "domain":
            configure_domain(args.name)
        elif args.command == "menu":
            import importlib.util
            menu_path = pathlib.Path(__file__).with_name("menu.py")
            menu_spec = importlib.util.spec_from_file_location("sb_traffic_menu", menu_path)
            if menu_spec is None or menu_spec.loader is None:
                raise TrafficError("无法加载交互菜单")
            menu_module = importlib.util.module_from_spec(menu_spec)
            menu_spec.loader.exec_module(menu_module)
            return menu_module.run_menu(sys.modules[__name__])
        elif args.command == "serve":
            serve()
        elif args.command == "uninstall":
            uninstall(args.purge)
        return 0
    except (TrafficError, OSError, subprocess.SubprocessError) as exc:
        print(f"sb-traffic: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

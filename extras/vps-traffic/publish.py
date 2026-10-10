"""Deploy the extension's HTTPS subscription site without replacing existing sites."""
from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import hashlib
import ipaddress
import json
import os
import pathlib
import shlex
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

MARKER = "# Managed by sb-traffic; do not edit.\n"
MAX_BODY = 4 * 1024 * 1024


class PublicationError(Exception):
    pass


def run(args: List[str], *, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                env=dict(os.environ, DEBIAN_FRONTEND="noninteractive"))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublicationError(f"无法运行 {args[0]}，或命令超时") from exc
    if check and result.returncode:
        # Commands here never contain the subscription token.
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise PublicationError(f"{args[0]} 执行失败：{detail}")
    return result


def caddy_binary() -> str:
    found = shutil.which("caddy")
    if not found and pathlib.Path("/usr/local/bin/caddy").is_file():
        found = "/usr/local/bin/caddy"
    if found:
        return found
    print("未安装 Caddy；尝试使用系统软件源安装（不添加第三方软件源）。")
    if shutil.which("apt-get"):
        run(["apt-get", "update"], timeout=600)
        run(["apt-get", "install", "-y", "caddy"], timeout=600)
    elif shutil.which("dnf"):
        run(["dnf", "install", "-y", "caddy"], timeout=600)
    elif shutil.which("yum"):
        run(["yum", "install", "-y", "caddy"], timeout=600)
    elif shutil.which("apk"):
        run(["apk", "add", "caddy"], timeout=600)
    else:
        raise PublicationError("没有受支持的包管理器；请按 Caddy 官方文档安装后重试")
    found = shutil.which("caddy")
    if not found:
        raise PublicationError("系统软件源未能提供 Caddy；请按官方文档安装后重试")
    return found


def backend() -> str:
    selected = os.environ.get("SB_TRAFFIC_INIT", "auto")
    if selected == "systemd" or (selected == "auto" and pathlib.Path("/run/systemd/system").is_dir()):
        return "systemd"
    if selected == "openrc" or (selected == "auto" and shutil.which("rc-service")):
        return "openrc"
    raise PublicationError("需要正在运行的 systemd 或 OpenRC 来管理 Caddy")


def service_action(init: str, action: str, *, check: bool = True) -> subprocess.CompletedProcess:
    if init == "systemd":
        args = ["systemctl", action, "caddy.service"]
    elif action == "enable":
        args = ["rc-update", "add", "caddy", "default"]
    else:
        args = ["rc-service", "caddy", "status" if action == "is-active" else action]
    return run(args, check=check)


def check_service_config(init: str, main: pathlib.Path) -> None:
    if init == "systemd":
        definition = run(["systemctl", "show", "caddy.service", "--property=ExecStart", "--value"]).stdout
    else:
        root = pathlib.Path(os.environ.get("SB_TRAFFIC_TEST_ROOT", "/"))
        service = root / "etc/init.d/caddy"
        try:
            definition = service.read_text(encoding="utf-8")
            settings = root / "etc/conf.d/caddy"
            if settings.is_file():
                definition += "\n" + settings.read_text(encoding="utf-8")
        except OSError as exc:
            raise PublicationError("未找到 Caddy OpenRC 服务；请先安装 Caddy 服务") from exc
    if not re.search(r"(?<![\w/])" + re.escape(str(main)) + r"(?![\w./-])", definition):
        raise PublicationError(f"Caddy 服务未使用 {main}；拒绝修改未知配置，请检查服务的 --config 参数")


def site_text(domain: str, public_port: int, backend_port: int) -> str:
    return (MARKER + f"https://{domain}:{public_port} {{\n"
            "\thandle /sub/* {\n"
            f"\t\treverse_proxy 127.0.0.1:{backend_port}\n"
            "\t}\n\thandle {\n\t\trespond 404\n\t}\n}\n")

def snapshot(path: pathlib.Path) -> Optional[tuple]:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PublicationError(f"拒绝修改符号链接或非普通文件：{path}")
    if not path.exists():
        return None
    info = path.stat()
    return path.read_bytes(), info.st_mode & 0o777, info.st_uid, info.st_gid


def atomic_write(path: pathlib.Path, body: bytes, mode: int = 0o644,
                 owner: Optional[tuple] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, temporary = tempfile.mkstemp(prefix=".sb-traffic-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            current = os.fstat(stream.fileno())
            if owner is not None and owner != (current.st_uid, current.st_gid):
                os.fchown(stream.fileno(), owner[0], owner[1])
            os.fchmod(stream.fileno(), mode)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def restore(path: pathlib.Path, previous: Optional[tuple]) -> None:
    if previous is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    else:
        atomic_write(path, previous[0], previous[1], owner=previous[2:])


def import_site(main: str, main_path: pathlib.Path, site: pathlib.Path) -> str:
    for line in main.splitlines():
        try:
            fields = shlex.split(line, comments=True)
        except ValueError:
            continue  # Caddy's validator, not us, diagnoses existing syntax.
        if len(fields) == 2 and fields[0] == "import":
            pattern = pathlib.Path(fields[1])
            if not pattern.is_absolute():
                pattern = main_path.parent / pattern
            if fnmatch.fnmatchcase(str(site), str(pattern)):
                return main
    return main + ("\n" if main and not main.endswith("\n") else "") + f'import "{site}"\n'


def adapt(binary: str, main: pathlib.Path) -> Dict[str, Any]:
    result = run([binary, "adapt", "--config", str(main), "--adapter", "caddyfile"])
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise PublicationError("Caddy adapt 没有输出有效 JSON") from exc


def hosts(config: Any):
    if isinstance(config, dict):
        for key, value in config.items():
            if key == "host" and isinstance(value, list):
                yield from (x for x in value if isinstance(x, str))
            else:
                yield from hosts(value)
    elif isinstance(config, list):
        for value in config:
            yield from hosts(value)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a bearer token to a redirected URL.


def fetch(url: str) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=5) as response:
        if response.status != 200:
            raise PublicationError("订阅接口没有返回 HTTP 200")
        body = response.read(MAX_BODY + 1)
    if not body or len(body) > MAX_BODY:
        raise PublicationError("订阅内容为空或过大")
    return body


def wait_local(url: str) -> None:
    for attempt in range(5):
        try:
            fetch(url)
            return
        except (OSError, urllib.error.URLError, PublicationError):
            if attempt < 4:
                time.sleep(1)
    raise PublicationError("本机订阅服务不可用；请检查节点导出或 sing-box-traffic 服务日志")


def verify_https(public_url: str, local_url: str, *, wait: float = 60) -> bool:
    deadline = time.monotonic() + wait
    while True:
        try:
            expected = fetch(local_url)
            actual = fetch(public_url)
            if hashlib.sha256(actual).digest() == hashlib.sha256(expected).digest():
                return True
        except (OSError, urllib.error.URLError, PublicationError):
            pass  # Do not print HTTP exceptions: they may contain the token.
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)


def deploy(api: Any, value: str) -> bool:
    domain = api.validate_domain(value)
    if not os.environ.get("SB_TRAFFIC_TEST_ROOT") and (sys.platform != "linux" or os.geteuid() != 0):
        raise PublicationError("域名自动部署只支持 Linux VPS，请使用 sudo/root 执行")
    state = api.current_state(sample=False)  # Fail before package/service changes if not configured.
    try:
        resolved = socket.getaddrinfo(domain, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise PublicationError("域名尚未解析；请先将 A/AAAA 记录指向这台 VPS，再重试") from exc
    if not resolved:
        raise PublicationError("域名没有可用的 A/AAAA 记录")
    known = set()
    candidates = [state["config"]["address"], api.detect_address()]
    for address in candidates:
        if not address:
            continue
        try:
            known.add(str(ipaddress.ip_address(address)))
        except ValueError:
            # A node hostname may be a CDN or another machine, not a local IP.
            continue
    if shutil.which("ip"):
        result = run(["ip", "-j", "address", "show", "dev", state["config"]["interface"]], check=False)
        with contextlib.suppress(ValueError, TypeError, KeyError):
            for interface in json.loads(result.stdout):
                known.update(info["local"] for info in interface.get("addr_info", []) if "local" in info)
    if not {item[4][0] for item in resolved}.issubset(known):
        raise PublicationError("域名 A/AAAA 与本机地址不匹配；请使用直连本 VPS 的独立订阅域名，修正所有解析后重试")
    print("域名已解析；请确保所有 A/AAAA 记录都指向本机，并放行证书验证所需的 80/443 端口。")
    binary = caddy_binary()
    init = backend()
    root = pathlib.Path(os.environ.get("SB_TRAFFIC_TEST_ROOT", "/"))
    main = root / "etc/caddy/Caddyfile"
    site = root / "etc/caddy/sites/sb-traffic.conf"
    check_service_config(init, main)
    api.ensure_private_dir(api.data_dir())
    with (api.data_dir() / "publication.lock").open("a+b") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = api.current_state(sample=False)
        previous = state.get("publication", {})
        saved_main, saved_site = snapshot(main), snapshot(site)
        if saved_site is not None:
            expected = site_text(previous.get("domain", ""), previous.get("https_port", 443),
                                 previous.get("backend_port", 18080)).encode()
            if not previous or saved_site[0] != expected:
                raise PublicationError(f"{site} 已存在且不是本扩展的原始配置；拒绝覆盖")
        was_active = service_action(init, "is-active", check=False).returncode == 0
        applied = False
        publication_saved = False
        try:
            # Temporarily exclude our OLD site from conflict/port detection. The
            # running Caddy is not reloaded until the complete candidate validates.
            if saved_site is not None:
                atomic_write(site, MARKER.encode())
            if saved_main is None:
                atomic_write(main, b"# Caddy subscription sites\n")
            base = adapt(binary, main)
            if any(fnmatch.fnmatchcase(domain, host.lower()) for host in hosts(base)):
                raise PublicationError("该域名已被其他 Caddy 站点使用；请使用独立订阅子域名")
            port = int(base.get("apps", {}).get("http", {}).get("https_port", 443))
            if not 1 <= port <= 65535:
                raise PublicationError("现有 Caddy HTTPS 端口无效")
            atomic_write(site, site_text(domain, port, state["config"]["port"]).encode())
            original = saved_main[0].decode("utf-8") if saved_main else "# Caddy subscription sites\n"
            candidate = import_site(original, main, site)
            atomic_write(main, candidate.encode(), saved_main[1] if saved_main else 0o644,
                         owner=saved_main[2:] if saved_main else None)
            run([binary, "validate", "--config", str(main), "--adapter", "caddyfile"])
            adapted = adapt(binary, main)
            if domain not in set(hosts(adapted)):
                raise PublicationError("Caddy 未加载订阅站点；请检查主配置的 import")
            print("Caddy 配置验证通过；启用并重启独立流量统计服务。")
            api.run_service("enable")
            api.run_service("restart")
            local_url = api.token_url(local=True)
            wait_local(local_url)
            service_action(init, "enable")
            applied = True  # An unsuccessful start/reload also needs rollback.
            if not was_active:
                service_action(init, "start")
            elif adapted.get("admin", {}).get("disabled", False):
                print("现有 Caddy 使用 admin off；将短暂重启 Caddy，其他站点可能短暂中断。")
                service_action(init, "restart")
            else:
                run([binary, "reload", "--config", str(main), "--adapter", "caddyfile"])
            with api.locked_state():
                latest = api.read_state()
                if (not latest or latest["config"]["port"] != state["config"]["port"]
                        or latest["token"] != state["token"]):
                    raise PublicationError("部署过程中订阅端口或令牌已变化，请重试")
                latest["publication"] = {"domain": domain, "https_port": port,
                                          "backend_port": state["config"]["port"], "verified": False}
                api.write_state(latest)
                publication_saved = True
        except BaseException as exc:  # Ctrl-C must also restore files before leaving the menu.
            restore(main, saved_main)
            restore(site, saved_site)
            if applied:
                try:
                    if was_active:
                        # Restore even if an admin-off restart left Caddy stopped.
                        service_action(init, "restart")
                    else:
                        service_action(init, "stop")
                except PublicationError as rollback_error:
                    raise PublicationError(f"部署失败，配置已恢复，但 Caddy 恢复运行失败：{rollback_error}") from exc
            raise
        # TLS issuance is asynchronous. Keep a validated deployed site on timeout
        # so Caddy can retry issuance, but never call the public endpoint verified.
        if publication_saved:
            print("反代已部署，正在等待证书并核对 HTTPS 订阅内容（约 60 秒）……")
            public_url = api.token_url()
            verified = verify_https(public_url, local_url)
            if verified:
                with api.locked_state():
                    latest = api.read_state()
                    if (latest.get("publication", {}).get("domain") == domain
                            and latest["token"] == state["token"]
                            and latest["config"]["port"] == state["config"]["port"]):
                        latest["publication"]["verified"] = True
                        api.write_state(latest)
                    else:
                        verified = False
            print("HTTPS 订阅已验证，可填入远程 Sub-Store：" if verified else "反代已配置，HTTPS 尚未验证；订阅地址：")
            print(public_url)
            if not verified:
                print("请检查域名 A/AAAA、80/443 的防火墙/占用、Caddy 证书日志；配置已保留，可稍后重跑菜单 10。")
            return verified
    return False

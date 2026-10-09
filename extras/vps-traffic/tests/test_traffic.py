import base64
import contextlib
import datetime as dt
import http.client
import importlib.util
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
MODULE_PATH = HERE.parent / "traffic.py"
spec = importlib.util.spec_from_file_location("traffic", MODULE_PATH)
traffic = importlib.util.module_from_spec(spec)
sys.modules["traffic"] = traffic
spec.loader.exec_module(traffic)
BASH = "/opt/homebrew/bin/bash" if pathlib.Path("/opt/homebrew/bin/bash").exists() else "/bin/bash"


def epoch(value):
    return dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc).timestamp()


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sb-traffic-test-")
        self.base = pathlib.Path(self.temp.name)
        self.data = self.base / "data"
        self.root = self.base / "sing-box"
        self.sys = self.base / "sys"
        self.proc = self.base / "proc"
        self.home = self.base / "home"
        self.conf = self.root / "conf"
        self.conf.mkdir(parents=True)
        (self.root / "sh/src").mkdir(parents=True)
        (self.sys / "class/net/eth0/statistics").mkdir(parents=True)
        (self.proc / "sys/kernel/random").mkdir(parents=True)
        (self.sys / "class/net/eth0/statistics/rx_bytes").write_text("100\n")
        (self.sys / "class/net/eth0/statistics/tx_bytes").write_text("200\n")
        (self.proc / "sys/kernel/random/boot_id").write_text("boot-a\n")
        self.env_patch = mock.patch.dict(os.environ, {
            "SB_TRAFFIC_DATA": str(self.data), "SB_TRAFFIC_ROOT": str(self.root),
            "SB_TRAFFIC_SYS_ROOT": str(self.sys), "SB_TRAFFIC_PROC_ROOT": str(self.proc),
            "SB_TRAFFIC_HOME": str(self.home),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def tearDown(self):
        self.temp.cleanup()

    def counters(self, rx=None, tx=None, boot=None):
        stats = self.sys / "class/net/eth0/statistics"
        if rx is not None:
            (stats / "rx_bytes").write_text(f"{rx}\n")
        if tx is not None:
            (stats / "tx_bytes").write_text(f"{tx}\n")
        if boot is not None:
            (self.proc / "sys/kernel/random/boot_id").write_text(boot + "\n")

    def configured_state(self, *, used=0, mode="out", reset_day=1, offset="+00:00", now=None):
        now = now if now is not None else epoch("2024-01-15T12:00:00")
        config = {"interface": "eth0", "quota_bytes": 1000, "address": "203.0.113.10",
                  "mode": mode, "reset_day": reset_day, "utc_offset_seconds": traffic.parse_offset(offset),
                  "name": "测试 VPS", "port": 18080, "root": str(self.root)}
        start, _ = traffic.billing_period(now, config)
        return {"schema": 1, "config": config, "used_bytes": used,
                "counter": traffic.selected_counter(100, 200, mode), "boot_id": "boot-a",
                "sampled_at": now, "period_start": start,
                "token": "A" * 43, "renames": {}, "warnings": []}


class UnitsAndCalendarTests(IsolatedCase):
    def test_decimal_and_binary_units_and_rejections(self):
        self.assertEqual(traffic.parse_size("1.25GB"), 1_250_000_000)
        self.assertEqual(traffic.parse_size("1GiB"), 1_073_741_824)
        self.assertEqual(traffic.parse_size("0.5 B"), 1)
        self.assertEqual(traffic.parse_size("2TB"), 2_000_000_000_000)
        for value in ("-1GB", "NaN", "Infinity", "1XB", "1e9", "1.2.3GB"):
            with self.subTest(value=value), self.assertRaises(traffic.TrafficError):
                traffic.parse_size(value)
        with self.assertRaises(traffic.TrafficError):
            traffic.parse_size("0GB", positive=True)
        self.assertEqual(traffic.parse_offset("+08:00"), 28_800)
        self.assertEqual(traffic.parse_offset("-03:30"), -12_600)
        for value in ("8", "+14:01", "+01:60", "-15:00"):
            with self.subTest(offset=value), self.assertRaises(traffic.TrafficError):
                traffic.parse_offset(value)

    def test_month_end_leap_year_cross_year_and_offset(self):
        cfg = self.configured_state(reset_day=31)["config"]
        start, end = traffic.billing_period(epoch("2024-02-28T12:00:00"), cfg)
        self.assertEqual(traffic.period_display(start, cfg), "2024-01-31T00:00:00+00:00")
        self.assertEqual(traffic.period_display(end, cfg), "2024-02-29T00:00:00+00:00")
        start, end = traffic.billing_period(epoch("2024-03-01T00:00:00"), cfg)
        self.assertEqual(traffic.period_display(start, cfg), "2024-02-29T00:00:00+00:00")
        self.assertEqual(traffic.period_display(end, cfg), "2024-03-31T00:00:00+00:00")
        cfg = self.configured_state(reset_day=15, offset="+08:00")["config"]
        start, end = traffic.billing_period(epoch("2024-12-14T16:00:00"), cfg)
        self.assertEqual(traffic.period_display(start, cfg), "2024-12-15T00:00:00+08:00")
        self.assertEqual(traffic.period_display(end, cfg), "2025-01-15T00:00:00+08:00")


class SamplingTests(IsolatedCase):
    def test_directions_repeat_samples_restart_and_counter_rollback(self):
        for mode in ("in", "out", "both"):
            self.counters(rx=100, tx=200, boot="boot-a")
            state = self.configured_state(mode=mode, used=10)
            traffic.sample_state(state, state["sampled_at"] + 1)
            self.assertEqual(state["used_bytes"], 10)
            self.counters(rx=115, tx=230)
            traffic.sample_state(state, state["sampled_at"] + 60)
            increment = 15 if mode == "in" else (30 if mode == "out" else 45)
            self.assertEqual(state["used_bytes"], 10 + increment)
            traffic.sample_state(state, state["sampled_at"] + 60)
            self.assertEqual(state["used_bytes"], 10 + increment)
        state = self.configured_state(used=77)
        self.counters(rx=20, tx=31, boot="boot-b")
        traffic.sample_state(state, state["sampled_at"] + 60)
        self.assertEqual(state["used_bytes"], 77 + 31)
        self.assertIn("重启", " ".join(state["warnings"]))
        self.counters(rx=25, tx=4)
        traffic.sample_state(state, state["sampled_at"] + 60)
        self.assertEqual(state["used_bytes"], 77 + 31 + 4)
        self.assertIn("回退", " ".join(state["warnings"]))

    def test_cross_period_proportional_estimate_and_multi_month(self):
        before = epoch("2024-01-31T12:00:00")
        state = self.configured_state(reset_day=1, now=before)
        state["counter"] = 200
        self.counters(tx=2600)
        traffic.sample_state(state, epoch("2024-02-01T12:00:00"))
        self.assertEqual(state["used_bytes"], 1200)
        self.assertTrue(any("比例估算" in x for x in state["warnings"]))
        before = epoch("2024-01-01T00:00:00")
        state = self.configured_state(reset_day=1, now=before)
        state["counter"] = 0
        self.counters(tx=3100)
        traffic.sample_state(state, epoch("2024-03-01T00:00:00"))
        self.assertEqual(state["used_bytes"], 0)
        self.assertTrue(state["warnings"])

    def test_missing_interface_does_not_mutate_old_state(self):
        state = self.configured_state()
        original = json.loads(json.dumps(state))
        with mock.patch.dict(os.environ, {"SB_TRAFFIC_SYS_ROOT": str(self.base / "absent")}):
            with self.assertRaises(traffic.TrafficError):
                traffic.sample_state(state, state["sampled_at"] + 5)
        self.assertEqual(state, original)

    def test_state_corruption_permissions_and_failed_temp_cleanup(self):
        with traffic.locked_state():
            traffic.write_state(self.configured_state())
        self.assertEqual(self.data.stat().st_mode & 0o777, 0o700)
        self.assertEqual(traffic.state_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(traffic.lock_path().stat().st_mode & 0o777, 0o600)
        with self.assertRaises(OSError), mock.patch.object(traffic.os, "replace", side_effect=OSError("simulated")):
            traffic.write_state(self.configured_state())
        self.assertFalse(any(child.name.endswith(".tmp") for child in self.data.iterdir()))
        traffic.state_path().write_text("{broken")
        with self.assertRaises(traffic.TrafficError):
            traffic.current_state()
        traffic.state_path().write_text("{}")
        with self.assertRaises(traffic.TrafficError):
            traffic.current_state()
        traffic.state_path().write_text('{"schema":1}')
        with self.assertRaises(traffic.TrafficError):
            traffic.current_state()

    def test_calibration_is_transactional_and_configuration_changes_require_used(self):
        args = traffic.build_parser().parse_args([
            "configure", "--interface", "eth0", "--quota", "1TB", "--address", "203.0.113.5",
            "--reset-day", "15", "--mode", "out", "--name", "香港 VPS", "--used", "120GB",
        ])
        traffic.configure(args)
        before = traffic.read_state()
        self.assertEqual(before["used_bytes"], 120_000_000_000)
        self.assertEqual(before["config"]["quota_bytes"], 1_000_000_000_000)
        bad = traffic.build_parser().parse_args(["configure", "--interface", "ens3"])
        with self.assertRaises(traffic.TrafficError):
            traffic.configure(bad)
        self.assertEqual(traffic.read_state(), before)
        traffic.set_used("250GB")
        after = traffic.read_state()
        self.assertEqual(after["used_bytes"], 250_000_000_000)
        self.assertEqual(after["counter"], 200)

    def test_persisted_state_requires_valid_counter_boot_period_and_sample_time(self):
        valid = self.configured_state()
        traffic.write_state(valid)
        for field, value in (("counter", None), ("boot_id", None), ("period_start", None),
                             ("sampled_at", None), ("sampled_at", float("inf"))):
            candidate = dict(valid)
            candidate[field] = value
            payload = json.dumps(candidate)
            traffic.state_path().write_text(payload)
            with self.subTest(field=field, value=value), self.assertRaises(traffic.TrafficError):
                traffic.read_state()
            self.assertEqual(traffic.state_path().read_text(), payload)
        for field in ("counter", "boot_id", "period_start"):
            candidate = dict(valid)
            del candidate[field]
            payload = json.dumps(candidate)
            traffic.state_path().write_text(payload)
            with self.subTest(missing=field), self.assertRaises(traffic.TrafficError):
                traffic.read_state()
            self.assertEqual(traffic.state_path().read_text(), payload)

    def test_reanchor_does_not_sample_a_removed_old_interface(self):
        state = self.configured_state(used=77)
        state["config"]["interface"] = "removed0"
        traffic.write_state(state)
        stats = self.sys / "class/net/eth1/statistics"
        stats.mkdir(parents=True)
        (stats / "rx_bytes").write_text("300\n")
        (stats / "tx_bytes").write_text("900\n")
        args = traffic.build_parser().parse_args(["configure", "--interface", "eth1", "--used", "10GB"])
        traffic.configure(args)
        migrated = traffic.read_state()
        self.assertEqual(migrated["config"]["interface"], "eth1")
        self.assertEqual(migrated["used_bytes"], 10_000_000_000)
        self.assertEqual(migrated["counter"], 900)

    def test_main_dispatches_configure_status_and_set_used_separately(self):
        with mock.patch.object(traffic, "configure") as configure, mock.patch.object(traffic, "print_status") as print_status, mock.patch.object(traffic, "set_used") as set_used:
            self.assertEqual(traffic.main(["configure"]), 0)
            configure.assert_called_once()
            print_status.assert_not_called()
            self.assertEqual(traffic.main(["status", "--json"]), 0)
            print_status.assert_called_once_with(True)
            self.assertEqual(traffic.main(["set-used", "25GB"]), 0)
            set_used.assert_called_once_with("25GB")
    def test_cross_process_calibration_lock(self):
        traffic.write_state(self.configured_state())
        code = "import sys;sys.path.insert(0,%r);import traffic;traffic.set_used(sys.argv[1])" % str(MODULE_PATH.parent)
        workers = []
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for value in ("101B", "202B", "303B", "404B"):
            workers.append(subprocess.Popen([sys.executable, "-c", code, value], env=env,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        for process in workers:
            _out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, err.decode())
        state = traffic.read_state()
        self.assertIn(state["used_bytes"], (101, 202, 303, 404))
        self.assertEqual(state["counter"], 200)


class LinkTests(IsolatedCase):
    def test_fragment_changes_only_label_for_normal_uri_and_vmess(self):
        uri = "vless://id@[2001:db8::1]:443?security=reality&pbk=abc#old%20name"
        changed = traffic.set_fragment(uri, "香港 VPS · #1% 'quoted'")
        parts = __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(changed)
        self.assertEqual((parts.scheme, parts.netloc, parts.path, parts.query),
                         ("vless", "id@[2001:db8::1]:443", "", "security=reality&pbk=abc"))
        self.assertEqual(__import__("urllib.parse", fromlist=["unquote"]).unquote(parts.fragment), "香港 VPS · #1% 'quoted'")
        vmess_obj = {"v": "2", "ps": "original", "add": "203.0.113.4", "port": "8443", "id": "abc", "net": "ws", "path": "/x"}
        uri = "vmess://" + base64.b64encode(json.dumps(vmess_obj).encode()).decode()
        output = traffic.set_fragment(uri, "已命名")
        parsed = json.loads(base64.b64decode(output[8:]))
        self.assertEqual(parsed["ps"], "已命名")
        self.assertEqual({k: v for k, v in parsed.items() if k != "ps"}, {k: v for k, v in vmess_obj.items() if k != "ps"})
        self.assertEqual(traffic.set_fragment(output, "第二次"), traffic.set_fragment(output, "第二次"))

    def test_subscription_uses_vps_prefix_exact_mapping_balance_and_no_accumulating_suffix(self):
        state = self.configured_state(used=120_000_000_000)
        state["config"]["quota_bytes"] = 1_000_000_000_000
        traffic.write_state(state)
        vmess = "vmess://" + base64.b64encode(b'{"v":"2","ps":"raw","add":"203.0.113.1"}').decode()
        records = [{"filename": "VLESS REALITY #1.json", "url": "vless://id@host:443?security=reality#raw"},
                   {"filename": "VMess 443.json", "url": vmess}]
        first = traffic.named_subscription(records)
        state = traffic.read_state()
        parse = __import__("urllib.parse", fromlist=["urlsplit", "unquote"])
        self.assertEqual(parse.unquote(parse.urlsplit(first[0]).fragment),
                         "测试 VPS · VLESS REALITY #1 | 剩余 880.00 GB")
        self.assertEqual(json.loads(base64.b64decode(first[1][8:]))["ps"],
                         "测试 VPS · VMess 443 | 剩余 880.00 GB")
        state["renames"]["VMess 443.json"] = "固定 #1"
        traffic.write_state(state)
        again = traffic.named_subscription(records)
        self.assertEqual(json.loads(base64.b64decode(again[1][8:]))["ps"],
                         "固定 #1 | 剩余 880.00 GB")
        self.assertEqual(traffic.named_subscription(records), again)
        state["renames"]["VMess 443.json"] = "x" * 200
        traffic.write_state(state)
        long_named = traffic.named_subscription(records)
        self.assertEqual(json.loads(base64.b64decode(long_named[1][8:]))["ps"],
                         "x" * 200 + " | 剩余 880.00 GB")
        with self.assertRaises(traffic.TrafficError):
            traffic.set_fragment("nonsense", "bad")
        with self.assertRaises(traffic.TrafficError):
            traffic.validate_name("bad\nname")


class HttpTests(IsolatedCase):
    def test_get_head_404_405_503_token_rotation_and_no_store(self):
        shared = {"token": "secret-token", "body": b"vless://x@y#n\n", "updated": time.time(), "error": None}
        server = traffic.LoopbackServer(("127.0.0.1", 0), traffic.SubscriptionHandler)
        server.shared = shared
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=2)))
        port = server.server_address[1]

        def request(method, path):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request(method, path)
            response = conn.getresponse()
            result = response.status, dict(response.getheaders()), response.read()
            conn.close()
            return result

        status, headers, body = request("GET", "/sub/secret-token")
        self.assertEqual((status, body), (200, shared["body"]))
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, headers, body = request("HEAD", "/sub/secret-token")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(headers["Content-Length"]), len(shared["body"]))
        self.assertEqual(request("GET", "/sub/wrong")[0], 404)
        self.assertEqual(request("GET", "/unknown")[0], 404)
        self.assertEqual(request("POST", "/sub/secret-token")[0], 405)
        shared["token"] = "rotated-token"
        self.assertEqual(request("GET", "/sub/secret-token")[0], 404)
        self.assertEqual(request("GET", "/sub/rotated-token")[0], 200)
        shared["error"] = "adapter failed"
        self.assertEqual(request("GET", "/sub/rotated-token")[0], 503)
        shared["error"] = None
        shared["updated"] = time.time() - 130
        self.assertEqual(request("GET", "/sub/rotated-token")[0], 503)


class ServeLifecycleTests(IsolatedCase):
    def test_background_cache_token_rotation_and_clean_stop(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        state = self.configured_state()
        state["config"]["port"] = port
        old_token = "B" * 43
        state["token"] = old_token
        with traffic.locked_state():
            traffic.write_state(state)
        records = [{"filename": "node.json", "url": "vless://id@host:443?security=tls#old"}]
        stop = threading.Event()
        failures = []

        def run_server():
            try:
                traffic.serve(stop)
            except Exception as exc:
                failures.append(exc)

        def request(token):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/sub/" + token)
            response = conn.getresponse()
            data = response.read()
            result = response.status, data
            conn.close()
            return result

        worker = threading.Thread(target=run_server, daemon=True)
        with mock.patch.object(traffic, "export_records", return_value=records):
            worker.start()
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    status, body = request(old_token)
                    if status == 200:
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("serve did not publish initial cached subscription")
            self.assertIn(b"vless://", body)
            traffic.rotate_token()
            new_token = traffic.read_state()["token"]
            deadline = time.time() + 5
            while time.time() < deadline:
                old_status, _ = request(old_token)
                new_status, new_body = request(new_token)
                if old_status == 404 and new_status == 200:
                    break
                time.sleep(0.1)
            else:
                self.fail("background token rotation was not applied")
            self.assertNotEqual(request(old_token)[0], 200)
            self.assertEqual(new_status, 200)
            self.assertIn(b"vless://", new_body)
            stop.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "serve did not stop after its lifecycle event")
        self.assertFalse(failures, failures)
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sb-traffic-install-test-")
        self.base = pathlib.Path(self.temp.name)
        self.root = self.base / "etc/sing-box"
        (self.root / "sh/src").mkdir(parents=True)
        (self.root / "sh/src/core.sh").write_text("# test baseline\n")
        (self.root / "conf").mkdir()
        self.mockbin = self.base / "mockbin"
        self.mockbin.mkdir()
        self.log = self.base / "commands.log"
        self.script = HERE.parent / "install.sh"
        self.env = os.environ.copy()
        self.env.update({"SB_TRAFFIC_TEST_ROOT": str(self.base), "SB_TRAFFIC_ROOT": str(self.root),
                         "SB_TRAFFIC_INIT": "systemd", "PYTHON": "/usr/bin/python3",
                         "MOCK_LOG": str(self.log), "PATH": f"{self.mockbin}:/opt/homebrew/bin:/usr/bin:/bin",
                         "PYTHONDONTWRITEBYTECODE": "1"})
        self.mock("systemctl")

    def tearDown(self):
        self.temp.cleanup()

    def mock(self, name):
        target = self.mockbin / name
        target.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$MOCK_LOG\"\nfor arg do\n  [ \"${MOCK_FAIL:-}\" != \"$arg\" ] || exit 1\ndone\nexit 0\n")
        target.chmod(0o755)

    def run_installer(self, *args, env=None, check=True):
        result = subprocess.run([BASH, str(self.script), *args], env=env or self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
        if check and result.returncode:
            self.fail(f"installer exit {result.returncode}: {result.stderr} {result.stdout}")
        return result

    def test_systemd_install_upgrade_enable_fail_disable_uninstall_preserve_purge(self):
        self.run_installer()
        home = self.base / "opt/sing-box-traffic"
        data = self.base / "etc/sing-box-traffic"
        self.assertTrue((home / "traffic.py").is_file())
        self.assertEqual((data.stat().st_mode & 0o777), 0o700)
        unit = (self.base / "etc/systemd/system/sing-box-traffic.service").read_text()
        self.assertIn("ExecStart=/usr/bin/python3", unit)
        self.assertIn("127.0.0.1", (HERE.parent / "traffic.py").read_text())
        sentinel = data / "sentinel"
        sentinel.write_text("preserve")
        self.run_installer()  # Upgrade does not replace data.
        self.assertEqual(sentinel.read_text(), "preserve")
        self.run_installer("service", "enable")
        failed_env = dict(self.env, MOCK_FAIL="disable")
        self.assertNotEqual(self.run_installer("service", "disable", env=failed_env, check=False).returncode, 0)
        self.run_installer("service", "disable")
        self.run_installer("uninstall")
        self.assertFalse((self.base / "etc/systemd/system/sing-box-traffic.service").exists())
        self.assertEqual(self.log.read_text().splitlines()[-1], "daemon-reload")
        self.assertTrue(sentinel.is_file())
        self.assertFalse(home.exists())
        self.assertFalse((self.base / "usr/local/bin/sb-traffic").exists())
        self.run_installer()
        self.run_installer("uninstall", "--purge")
        self.assertFalse(data.exists())
        self.assertEqual((self.root / "sh/src/core.sh").read_text(), "# test baseline\n")

    def test_openrc_install_and_lifecycle_mock(self):
        self.env["SB_TRAFFIC_INIT"] = "openrc"
        self.env["PATH"] = f"{self.mockbin}:/opt/homebrew/bin:/usr/bin:/bin"
        self.mock("rc-service")
        self.mock("rc-update")
        self.run_installer()
        init = self.base / "etc/init.d/sing-box-traffic"
        self.assertTrue(init.is_file())
        self.assertTrue(init.stat().st_mode & 0o111)
        self.run_installer("service", "enable")
        pidfile = self.base / "run/sing-box-traffic.pid"
        self.assertTrue(pidfile.parent.is_dir())
        pidfile.write_text("123\n")
        failed_env = dict(self.env, MOCK_FAIL="stop")
        self.assertNotEqual(self.run_installer("service", "disable", env=failed_env, check=False).returncode, 0)
        self.assertTrue(pidfile.is_file())
        self.run_installer("service", "disable")
        self.assertFalse(pidfile.exists())
        self.run_installer("uninstall")
        self.assertFalse(init.exists())
        self.assertFalse(init.exists())


class CoreAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sb-traffic-core-adapter-")
        self.base = pathlib.Path(self.temp.name)
        self.root = self.base / "sb"
        (self.root / "sh/src").mkdir(parents=True)
        (self.root / "conf").mkdir()
        shutil.copy2(ROOT / "src/core.sh", self.root / "sh/src/core.sh")
        (self.root / "bin").mkdir()
        subprocess.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout",
                        str(self.base / "key.pem"), "-out", str(self.root / "bin/tls.cer"), "-subj", "/CN=adapter.test",
                        "-days", "1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        self.caddy = self.base / "caddy"
        self.caddy.mkdir()
        self.caddyfile = self.base / "Caddyfile"
        self.caddyfile.write_text("{\n  https_port 8443\n}\n")
        self.adapter = HERE.parent / "adapter.sh"

    def tearDown(self):
        self.temp.cleanup()

    def put(self, filename, proto, port, **kwargs):
        inbound = {"type": proto, "listen_port": port}
        if "users" in kwargs:
            inbound["users"] = kwargs.pop("users")
        inbound.update(kwargs)
        (self.root / "conf" / filename).write_text(json.dumps({"inbounds": [inbound], "outbounds": [{"tag": "direct", "type": "direct"}, {"tag": "public_key_PUBKEY", "type": "direct"}]}))

    def test_actual_upstream_adapter_exports_protocols_isolation_ipv6_and_caddy_port(self):
        ident = "12345678-1234-1234-1234-123456789abc"
        self.put("VMess WS edge.json", "vmess", 443, users=[{"uuid": ident}], transport={"type": "ws", "path": "/vm", "headers": {"host": "edge.example"}})
        self.put("VLESS Reality.json", "vless", 8443, users=[{"uuid": ident}], tls={"server_name": "sni.example", "reality": {"private_key": "PRIV"}})
        self.put("Hysteria2.json", "hysteria2", 443, users=[{"password": "hypw"}])
        self.put("TUIC.json", "tuic", 443, users=[{"uuid": ident, "password": "tuicpw"}])
        self.put("Trojan.json", "trojan", 443, users=[{"password": "trojanpw"}])
        self.put("SS2022.json", "shadowsocks", 443, method="2022-blake3-aes-128-gcm", password="ss-secret")
        self.put("AnyTLS.json", "anytls", 443, users=[{"password": "anypw"}])
        self.put("Socks.json", "socks", 1080, users=[{"username": "u", "password": "p"}])
        self.put("dynamic-port-test-link.json", "vmess", 2222, users=[{"uuid": ident}])
        (self.caddy / "edge.example.conf").write_text("edge.example:18443 {\n reverse_proxy /path 127.0.0.1:443\n}\n")
        env = dict(os.environ, SB_TRAFFIC_ROOT=str(self.root), SB_TRAFFIC_ADDRESS="2001:db8::5",
                   SB_TRAFFIC_CADDY_CONF=str(self.caddy), SB_TRAFFIC_CADDYFILE=str(self.caddyfile),
                   PATH=f"/opt/homebrew/bin:/usr/bin:/bin", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([BASH, str(self.adapter)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("\n"), 1, repr(result.stdout))
        records = json.loads(result.stdout)
        self.assertEqual(len(records), 8)
        urls = {r["filename"]: r["url"] for r in records}
        self.assertIn("vmess://", urls["VMess WS edge.json"])
        self.assertIn("[2001:db8::5]", urls["VLESS Reality.json"])
        for filename, protocol in (("Hysteria2.json", "hysteria2://"), ("TUIC.json", "tuic://"),
                                   ("Trojan.json", "trojan://"), ("SS2022.json", "ss://"),
                                   ("AnyTLS.json", "anytls://"), ("Socks.json", "socks://")):
            self.assertTrue(urls[filename].startswith(protocol), (filename, urls[filename]))
        vmess_data = json.loads(base64.b64decode(urls["VMess WS edge.json"][8:]))
        self.assertEqual(vmess_data["host"], "edge.example")
        self.assertEqual(vmess_data["path"], "/vm")
        self.assertEqual(vmess_data["port"], "18443")
        self.assertNotIn("dynamic-port-test-link.json", urls)
        # Every record came from the named config and no config-level fields leaked.
        self.assertIn("trojanpw", urls["Trojan.json"])
        self.assertNotIn("anypw", urls["Trojan.json"])


def suite():
    return unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])


if __name__ == "__main__":
    unittest.main(verbosity=2)

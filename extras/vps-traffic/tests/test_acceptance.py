"""Acceptance regressions: user-visible behavior, not just helper functions."""
import base64
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit

EXT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("traffic_acceptance_module", EXT / "traffic.py")
traffic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(traffic)
BASH = shutil.which("bash") or "/bin/bash"
if pathlib.Path("/opt/homebrew/bin/bash").exists():
    BASH = "/opt/homebrew/bin/bash"


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="traffic-acceptance-")
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        self.sysroot = self.base / "sys"
        self.procroot = self.base / "proc"
        self.data = self.base / "data"
        self.root = self.base / "sing-box"
        (self.root / "conf").mkdir(parents=True)
        self.add_interface("eth0")
        boot = self.procroot / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text("test-boot\n")
        # Real Linux has /sys/class/net and /proc/sys/kernel/random/boot_id,
        # NOT /sys/kernel/random/boot_id.
        env = {"SB_TRAFFIC_SYS_ROOT": str(self.sysroot),
               "SB_TRAFFIC_PROC_ROOT": str(self.procroot),
               "SB_TRAFFIC_DATA": str(self.data), "SB_TRAFFIC_ROOT": str(self.root),
               "PYTHONDONTWRITEBYTECODE": "1"}
        self.patch = mock.patch.dict(os.environ, env)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def add_interface(self, interface):
        stats = self.sysroot / "class/net" / interface / "statistics"
        stats.mkdir(parents=True)
        (stats / "rx_bytes").write_text("100\n")
        (stats / "tx_bytes").write_text("200\n")

    def run_cli(self, *args):
        result = subprocess.run([sys.executable, "-B", str(EXT / "traffic.py"), *args],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def configure(self):
        return self.run_cli("configure", "--interface", "eth0", "--quota", "1TB",
                            "--used", "120GB", "--address", "203.0.113.10", "--name", "香港 VPS")

    def test_linux_standard_counter_and_boot_paths(self):
        self.assertEqual(traffic.read_counter("eth0"), (100, 200, "test-boot"))
        self.assertFalse((self.sysroot / "kernel/random/boot_id").exists())

    def test_actual_cli_calibration_and_status(self):
        self.configure()
        initial = json.loads(self.run_cli("status", "--json"))
        self.assertEqual(initial["used_bytes"], 120_000_000_000)
        self.assertEqual(initial["remaining_bytes"], 880_000_000_000)
        self.run_cli("set-used", "250GB")
        calibrated = json.loads(self.run_cli("status", "--json"))
        self.assertEqual(calibrated["used_bytes"], 250_000_000_000)
        self.assertEqual(calibrated["remaining_bytes"], 750_000_000_000)
        self.run_cli("configure", "--quota", "2TB")
        resized = json.loads(self.run_cli("status", "--json"))
        self.assertEqual(resized["used_bytes"], 250_000_000_000)
        self.assertEqual(resized["remaining_bytes"], 1_750_000_000_000)

    def test_real_node_names_include_correct_vps_balance(self):
        vmess = {"v": 2, "ps": "original", "add": "203.0.113.10", "id": "test", "port": "443"}
        records = [{"filename": "Reality.json", "url": "vless://test@203.0.113.10:443?security=reality#old"},
                   {"filename": "VMess.json", "url": "vmess://" + base64.b64encode(json.dumps(vmess).encode()).decode()}]
        state = {"config": {"name": "香港 VPS", "quota_bytes": 1_000_000_000_000},
                 "used_bytes": 120_000_000_000, "renames": {"VMess.json": "香港 · VMess #1"}}
        result = traffic.named_subscription(records, state)
        self.assertEqual(len(result), len(records))
        self.assertEqual(unquote(urlsplit(result[0]).fragment), "香港 VPS · Reality | 剩余 880.00 GB")
        after = json.loads(base64.b64decode(result[1][8:]))
        self.assertEqual(after.pop("ps"), "香港 · VMess #1 | 剩余 880.00 GB")
        self.assertEqual(after, {k: v for k, v in vmess.items() if k != "ps"})
        self.assertEqual(traffic.named_subscription(records, state), result)
        state["config"]["name"] = "美国 VPS"
        state["used_bytes"] = 300_000_000_000
        other = traffic.named_subscription(records, state)
        self.assertEqual(unquote(urlsplit(other[0]).fragment), "美国 VPS · Reality | 剩余 700.00 GB")
        state["used_bytes"] = 2_000_000_000_000
        self.assertIn("剩余 0.00 GB", unquote(urlsplit(traffic.named_subscription(records, state)[0]).fragment))

    def test_reanchor_from_removed_interface(self):
        self.configure()
        shutil.rmtree(self.sysroot / "class/net/eth0")
        self.add_interface("ens3")
        self.run_cli("configure", "--interface", "ens3", "--used", "80GB")
        actual = json.loads(self.run_cli("status", "--json"))
        self.assertEqual(actual["interface"], "ens3")
        self.assertEqual(actual["used_bytes"], 80_000_000_000)

    def test_valid_json_missing_accounting_fields_is_rejected(self):
        self.configure()
        original = traffic.read_state()
        for missing in ("counter", "boot_id", "period_start", "sampled_at", "used_bytes"):
            damaged = dict(original)
            del damaged[missing]
            traffic.state_path().write_text(json.dumps(damaged))
            with self.subTest(missing=missing), self.assertRaises(traffic.TrafficError):
                traffic.read_state()
        traffic.state_path().write_text(json.dumps(original))

    def test_systemd_uninstall_removes_unit_and_reloads(self):
        prefix = self.base / "install-root"
        root = prefix / "etc/sing-box"
        (root / "sh/src").mkdir(parents=True)
        (root / "conf").mkdir()
        (root / "sh/src/core.sh").write_text("# fixture\n")
        mockbin = prefix / "mockbin"
        mockbin.mkdir()
        log = prefix / "systemctl.log"
        systemctl = mockbin / "systemctl"
        systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$MOCK_LOG"\n')
        systemctl.chmod(0o755)
        env = dict(os.environ, SB_TRAFFIC_TEST_ROOT=str(prefix), SB_TRAFFIC_ROOT=str(root),
                   SB_TRAFFIC_DATA=str(prefix / "etc/sing-box-traffic"),
                   SB_TRAFFIC_HOME=str(prefix / "opt/sing-box-traffic"), SB_TRAFFIC_INIT="systemd",
                   MOCK_LOG=str(log), PYTHON=sys.executable,
                   PATH=str(mockbin) + os.pathsep + str(pathlib.Path(BASH).parent) + os.pathsep + os.environ["PATH"])
        def install(*args):
            p = subprocess.run([BASH, str(EXT / "install.sh"), *args], env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(p.returncode, 0, p.stderr)
        install()
        unit = prefix / "etc/systemd/system/sing-box-traffic.service"
        self.assertTrue(unit.is_file())
        log.write_text("")
        install("uninstall")
        self.assertFalse(unit.exists(), "uninstall left a stale systemd service pointing at deleted files")
        self.assertIn("daemon-reload", log.read_text())
        self.assertTrue((prefix / "etc/sing-box-traffic").exists())


if __name__ == "__main__":
    unittest.main()

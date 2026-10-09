"""End-to-end terminal input tests using the real menu and CLI."""
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

EXT = pathlib.Path(__file__).resolve().parents[1]
ROOT = EXT.parents[1]
BASH = "/opt/homebrew/bin/bash" if pathlib.Path("/opt/homebrew/bin/bash").exists() else shutil.which("bash")


class MenuEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="traffic-menu-e2e-")
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        stats = self.base / "sys/class/net/eth0/statistics"
        stats.mkdir(parents=True)
        (stats / "rx_bytes").write_text("100")
        (stats / "tx_bytes").write_text("200")
        boot = self.base / "proc/sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text("menu-test-boot")
        self.env = dict(os.environ, SB_TRAFFIC_DATA=str(self.base / "data"),
                        SB_TRAFFIC_SYS_ROOT=str(self.base / "sys"),
                        SB_TRAFFIC_PROC_ROOT=str(self.base / "proc"),
                        SB_TRAFFIC_ROOT=str(self.base / "sing-box"),
                        SB_TRAFFIC_OFFLINE="1",
                        SB_TRAFFIC_HOME=str(EXT), PYTHONDONTWRITEBYTECODE="1")

    def run_cli(self, args, inputs=None):
        result = subprocess.run([sys.executable, "-B", str(EXT / "traffic.py"), *args],
                                input=None if inputs is None else "\n".join(inputs) + "\n",
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def test_real_wizard_negative_timezone_preservation_and_calibration(self):
        result = self.run_cli(["menu"], ["2", "1TB", "y", "eth0", "203.0.113.10",
                                              "加拿大 VPS 名称", "15", "-03:30", "out", "",
                                              "120GB", "y", "0"])
        self.assertIn("配置已保存", result.stdout)
        self.assertNotIn("操作未成功", result.stdout)
        current = json.loads(self.run_cli(["status", "--json"]).stdout)
        self.assertEqual(current["utc_offset"], "-03:30")
        self.assertEqual(current["name"], "加拿大 VPS 名称")
        self.assertEqual(current["used_bytes"], 120_000_000_000)
        self.run_cli(["menu"], ["2", "", "", "", "y", "0"])
        unchanged = json.loads(self.run_cli(["status", "--json"]).stdout)
        self.assertEqual(unchanged["used_bytes"], current["used_bytes"])
        self.assertEqual(unchanged["utc_offset"], current["utc_offset"])
        self.run_cli(["menu"], ["3", "250GB", "0"])
        calibrated = json.loads(self.run_cli(["status", "--json"]).stdout)
        self.assertEqual(calibrated["remaining_bytes"], 750_000_000_000)

    def test_real_upstream_prompt_opens_real_submenu(self):
        binary = self.base / "sb-traffic"
        binary.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -B " +
                          shlex.quote(str(EXT / "traffic.py")) + ' "$@"\n')
        binary.chmod(0o755)
        env = dict(self.env, PATH=str(self.base) + os.pathsep + str(pathlib.Path(BASH).parent) +
                   os.pathsep + os.environ["PATH"])
        script = 'source "$1"; is_core_name=sing-box; '
        if sys.platform == "darwin":
            # Upstream's GNU grep ERE uses ?+, rejected by BSD grep. Keep the
            # real ask/main-menu functions; shim only this numeric predicate.
            script += 'is_test() { [[ $1 == number && $2 =~ ^[1-9][0-9]*$ ]] && printf "%s\\n" "$2"; }; '
        script += 'is_main_menu'
        result = subprocess.run([BASH, "-c", script, "menu-e2e", str(ROOT / "src/core.sh")],
                                input="11\n0\n", env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("VPS 流量统计管理", result.stdout)
        self.assertIn("已退出菜单", result.stdout)
        self.assertFalse((self.base / "data/state.json").exists())


if __name__ == "__main__":
    unittest.main()

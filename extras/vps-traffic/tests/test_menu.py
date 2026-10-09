import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

EXT = pathlib.Path(__file__).resolve().parents[1]
MENU_PATH = EXT / "menu.py"
TRAFFIC_PATH = EXT / "traffic.py"
MENU_SPEC = importlib.util.spec_from_file_location("traffic_menu_test_module", MENU_PATH)
menu = importlib.util.module_from_spec(MENU_SPEC)
MENU_SPEC.loader.exec_module(menu)
BASH = "/opt/homebrew/bin/bash" if pathlib.Path("/opt/homebrew/bin/bash").exists() else shutil.which("bash")


class FakeAPI:
    def __init__(self, state=None):
        self.state = state or {}
        self.calls = []

    def read_state(self):
        return self.state

    def singbox_root(self):
        return pathlib.Path("/nonexistent")

    @staticmethod
    def offset_text(seconds):
        sign = "+" if seconds >= 0 else "-"
        minutes = abs(seconds) // 60
        return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"

    @staticmethod
    def parse_offset(value):
        sign = -1 if value.startswith("-") else 1
        hours, minutes = map(int, value[1:].split(":"))
        return sign * (hours * 60 + minutes) * 60

    def main(self, args):
        self.calls.append(args)
        return 0


def configured_state(root=None):
    config = {"interface": "eth0", "quota_bytes": 1_000_000_000_000,
              "address": "203.0.113.10", "mode": "out", "reset_day": 1,
              "utc_offset_seconds": 0, "name": "测试 VPS", "port": 18080,
              "root": str(root or "/nonexistent")}
    return {"schema": 1, "config": config, "used_bytes": 10}


class MenuTests(unittest.TestCase):
    def run_menu(self, responses, state=None):
        api = FakeAPI(state)
        output = []
        iterator = iter(responses)

        def read(_prompt):
            response = next(iterator)
            if isinstance(response, BaseException):
                raise response
            return response

        result = menu.run_menu(api, read, output.append)
        return result, api, output

    def test_zero_eof_and_ctrl_c_exit_cleanly(self):
        self.assertEqual(self.run_menu(["0"])[0], 0)
        self.assertEqual(self.run_menu([EOFError()])[0], 0)
        self.assertEqual(self.run_menu([KeyboardInterrupt()])[0], 0)

    def test_invalid_option_recovers_to_menu(self):
        result, api, output = self.run_menu(["not a number", "0"])
        self.assertEqual(result, 0)
        self.assertEqual(api.calls, [])
        self.assertTrue(any("选项无效" in line for line in output))

    def test_first_configuration_passes_chinese_name_as_one_argument(self):
        result, api, _output = self.run_menu(
            ["2", "eth0", "1TB", "", "", "", "香港 VPS 节点", "203.0.113.10", "", "", "y", "0"])
        self.assertEqual(result, 0)
        self.assertEqual(api.calls, [["configure", "--interface", "eth0", "--quota", "1TB",
                                     "--reset-day", "1", "--utc-offset", "+00:00", "--mode", "out",
                                     "--name", "香港 VPS 节点", "--address", "203.0.113.10", "--used", "0"]])

    def test_existing_blank_configuration_preserves_values_and_omits_used(self):
        result, api, _output = self.run_menu(["2"] + [""] * 9 + ["y", "0"], configured_state())
        self.assertEqual(result, 0)
        self.assertEqual(api.calls, [["configure"]])
        self.assertNotIn("--used", api.calls[0])

    def test_measurement_change_requires_used_value(self):
        result, api, _output = self.run_menu(
            ["2", "eth1"] + [""] * 7 + ["55GB", "y", "0"], configured_state())
        self.assertEqual(result, 0)
        self.assertEqual(api.calls, [["configure", "--interface", "eth1", "--used", "55GB"]])

    def test_cancel_configuration_does_not_call_api(self):
        result, api, output = self.run_menu(["2", "!", "0"])
        self.assertEqual(result, 0)
        self.assertEqual(api.calls, [])
        self.assertTrue(any("已取消配置" in line for line in output))

    def test_rejected_disable_and_token_rotation_have_no_effect(self):
        result, api, _output = self.run_menu(["6", "n", "9", "n", "0"])
        self.assertEqual(result, 0)
        self.assertEqual(api.calls, [])

    def test_rename_uses_selected_exact_basename(self):
        with tempfile.TemporaryDirectory(prefix="sb-menu-test-") as temp:
            conf = pathlib.Path(temp) / "conf"
            conf.mkdir()
            filename = "North America node.json"
            (conf / filename).write_text("{}", encoding="utf-8")
            result, api, _output = self.run_menu(["4", "1", "香港 节点", "0"], configured_state(temp))
        self.assertEqual(result, 0)
        self.assertEqual(api.calls, [["rename", filename, "香港 节点"]])

    def test_actual_traffic_menu_command_accepts_zero(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-B", str(TRAFFIC_PATH), "menu"],
                                input="0\n", capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0. 退出", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_install_copies_menu_into_temporary_root(self):
        with tempfile.TemporaryDirectory(prefix="sb-menu-install-test-") as temp:
            base = pathlib.Path(temp)
            root = base / "etc/sing-box"
            (root / "sh/src").mkdir(parents=True)
            (root / "sh/src/core.sh").write_text("# isolated test\n", encoding="utf-8")
            (root / "conf").mkdir()
            mockbin = base / "mockbin"
            mockbin.mkdir()
            for command in ("jq", "systemctl"):
                target = mockbin / command
                target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                target.chmod(0o755)
            env = dict(os.environ, SB_TRAFFIC_TEST_ROOT=str(base), SB_TRAFFIC_ROOT=str(root),
                       SB_TRAFFIC_INIT="systemd", PYTHON="/usr/bin/python3",
                       PATH=f"{mockbin}:/opt/homebrew/bin:/usr/bin:/bin",
                       PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([BASH, str(EXT / "install.sh")], env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = base / "opt/sing-box-traffic/menu.py"
            self.assertTrue(installed.is_file())
            self.assertEqual(installed.read_bytes(), MENU_PATH.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)

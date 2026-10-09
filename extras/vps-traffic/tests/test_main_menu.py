"""Exercise the real upstream menu with only side-effectful actions mocked."""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
BASH = "/opt/homebrew/bin/bash" if pathlib.Path("/opt/homebrew/bin/bash").exists() else shutil.which("bash")


class MainMenuIntegrationTests(unittest.TestCase):
    def exercise(self, reply="11", installed=True, extra=False, repeat=False):
        with tempfile.TemporaryDirectory(prefix="traffic-main-menu-") as temp:
            stub = pathlib.Path(temp) / "sb-traffic"
            stub.write_text('#!/bin/sh\nprintf "TRAFFIC:%s\\n" "$*"\n')
            stub.chmod(0o755)
            env = dict(os.environ, PATH=temp + os.pathsep + str(pathlib.Path(BASH).parent) + os.pathsep + os.environ["PATH"],
                       TEST_REPLY=reply, TEST_MISSING="0" if installed else "1",
                       TEST_EXTRA="1" if extra else "0", TEST_REPEAT="1" if repeat else "0")
            script = r'''
source "$1"
msg() { printf '%s\n' "$*"; }
msg_ul() { printf '%s' "$*"; }
ask() { printf 'MENU:%s\n' "${mainmenu[@]}"; REPLY=$TEST_REPLY; }
add() { printf 'ORIGINAL:add\n'; }
info() { printf 'ORIGINAL:info\n'; }
load() { :; }
show_help() { printf 'ORIGINAL:help\n'; }
about() { printf 'ORIGINAL:about\n'; }
if [[ $TEST_EXTRA == 1 ]]; then mainmenu+=("未来上游选项"); fi
if [[ $TEST_MISSING == 1 ]]; then
    command() {
        if [[ $1 == -v && $2 == sb-traffic ]]; then return 1; fi
        builtin command "$@"
    }
fi
is_main_menu
if [[ $TEST_REPEAT == 1 ]]; then is_main_menu; fi
printf 'GLOBAL_COUNT:%s\n' "${#mainmenu[@]}"
'''
            result = subprocess.run([BASH, "-c", script, "menu-test", str(ROOT / "src/core.sh")],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

    def test_installed_extension_called_with_menu_and_original_numbers_preserved(self):
        output = self.exercise()
        entries = [line[5:] for line in output.splitlines() if line.startswith("MENU:")]
        self.assertEqual(entries, ["添加配置", "更改配置", "查看配置", "删除配置", "运行管理",
                                   "更新", "卸载", "帮助", "其他", "关于", "流量统计"])
        self.assertIn("TRAFFIC:menu", output)
        self.assertIn("GLOBAL_COUNT:10", output)

    def test_missing_extension_only_prints_install_instructions(self):
        output = self.exercise(installed=False)
        self.assertIn("尚未安装", output)
        self.assertIn("sudo bash extras/vps-traffic/install.sh", output)
        self.assertNotIn("TRAFFIC:", output)

    def test_original_menu_dispatches_still_work(self):
        for option, action in (("1", "add"), ("3", "info"), ("8", "help"), ("10", "about")):
            with self.subTest(option=option):
                output = self.exercise(reply=option)
                self.assertIn("ORIGINAL:" + action, output)
                self.assertNotIn("TRAFFIC:", output)

    def test_dynamic_index_and_no_menu_accumulation(self):
        output = self.exercise(reply="12", extra=True, repeat=True)
        self.assertEqual(output.count("TRAFFIC:menu"), 2)
        self.assertEqual(output.count("MENU:流量统计"), 2)
        self.assertIn("GLOBAL_COUNT:11", output)


if __name__ == "__main__":
    unittest.main()

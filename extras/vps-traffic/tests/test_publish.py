"""Publication regressions: isolated files/services plus optional real Caddy validation."""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

EXT = pathlib.Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, EXT / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


traffic = load("traffic_publication_test", "traffic.py")
publish = load("publish_test", "publish.py")


class PublicationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="sb-publication-")
        self.addCleanup(temp.cleanup)
        self.base = pathlib.Path(temp.name)
        self.main = self.base / "etc/caddy/Caddyfile"
        self.site = self.base / "etc/caddy/sites/sb-traffic.conf"
        self.main.parent.mkdir(parents=True)
        self.original = b"# Keep existing sites\n{\n admin off\n}\nexisting.example.net {\n respond ok\n}\n"
        self.main.write_bytes(self.original)
        self.env = mock.patch.dict(os.environ, {"SB_TRAFFIC_TEST_ROOT": str(self.base),
                                               "SB_TRAFFIC_DATA": str(self.base / "data")})
        self.env.start()
        self.addCleanup(self.env.stop)
        traffic.write_state({"schema": 1, "config": {"interface": "eth0", "quota_bytes": 10**12,
                              "address": "203.0.113.10", "mode": "out", "reset_day": 1,
                              "utc_offset_seconds": 0, "name": "测试 VPS", "port": 18080},
                             "used_bytes": 120, "counter": 200, "boot_id": "test", "sampled_at": 1,
                             "period_start": 0, "token": "A" * 43, "renames": {}, "warnings": []})
        self.base_json = {"admin": {"disabled": True}, "apps": {"http": {"servers": {
            "srv0": {"routes": [{"match": [{"host": ["existing.example.net"]}]}]}}}}}
        self.commands = []
        self.active = True
        self.fail_validate = False
        self.fail_reload = False

    def fake_run(self, args, **kwargs):
        self.commands.append(args)
        if args[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(args, 0, f"caddy run --config {self.main}", "")
        if args[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(args, 0 if self.active else 3, "", "")
        if len(args) > 1 and args[1] == "validate" and self.fail_validate:
            raise publish.PublicationError("invalid candidate")
        if len(args) > 1 and args[1] in ("reload", "restart") and self.fail_reload:
            self.fail_reload = False
            raise publish.PublicationError("service failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_adapt(self, _binary, _main):
        result = json.loads(json.dumps(self.base_json))
        if self.site.exists():
            for line in self.site.read_text().splitlines():
                if line.startswith("https://"):
                    host = line.split("https://", 1)[1].split(":", 1)[0]
                    result["apps"]["http"]["servers"]["srv0"]["routes"].append({"match": [{"host": [host]}]})
        return result

    @contextlib.contextmanager
    def isolated(self, verified=True):
        with mock.patch.object(publish, "caddy_binary", return_value="caddy"), \
             mock.patch.object(publish, "backend", return_value="systemd"), \
             mock.patch.object(publish.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("203.0.113.10", 0))]), \
             mock.patch.object(publish, "run", side_effect=self.fake_run), \
             mock.patch.object(publish, "adapt", side_effect=self.fake_adapt), \
             mock.patch.object(publish, "wait_local"), \
             mock.patch.object(publish, "verify_https", return_value=verified), \
             mock.patch.object(traffic, "detect_address", return_value="203.0.113.10"), \
             mock.patch.object(traffic, "run_service") as service, contextlib.redirect_stdout(io.StringIO()) as output:
            yield service, output

    def test_domain_validation_rejects_injection_ip_and_invalid_labels(self):
        self.assertEqual(traffic.validate_domain(" Sub.Example.com. "), "sub.example.com")
        for value in ("https://example.com", "example.com:443", "example.com/sub", "*.example.com",
                      "127.0.0.1", "[::1]", "localhost", "foo..com", "-bad.com", "bad-.com",
                      "foo.com\nrespond 200", "x" * 64 + ".com", "example.com;id", "foo.local", "foo.internal", None):
            with self.subTest(value=value), self.assertRaises(traffic.TrafficError):
                traffic.validate_domain(value)

    def test_public_url_local_override_and_warning_before_configuration(self):
        self.assertTrue(traffic.token_url().startswith("http://127.0.0.1:18080/"))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(traffic.main(["url"]), 0)
        self.assertIn("仅为本机地址", errors.getvalue())
        state = traffic.read_state()
        state["publication"] = {"domain": "sub.example.com", "https_port": 8443,
                                "backend_port": 18080, "verified": True}
        traffic.write_state(state)
        self.assertEqual(traffic.token_url(), "https://sub.example.com:8443/sub/" + "A" * 43)
        self.assertEqual(traffic.token_url(local=True), "http://127.0.0.1:18080/sub/" + "A" * 43)
        state["publication"]["https_port"] = 443
        traffic.write_state(state)
        self.assertEqual(traffic.token_url(), "https://sub.example.com/sub/" + "A" * 43)

    def test_success_preserves_other_sites_persists_domain_and_enables_services(self):
        with self.isolated() as (service, output):
            self.assertTrue(publish.deploy(traffic, "sub.example.com"))
        self.assertTrue(self.main.read_bytes().startswith(self.original))
        self.assertIn("handle /sub/*", self.site.read_text())
        self.assertIn("reverse_proxy 127.0.0.1:18080", self.site.read_text())
        self.assertNotIn("A" * 43, self.site.read_text())
        self.assertEqual(traffic.read_state()["used_bytes"], 120)
        self.assertTrue(traffic.read_state()["publication"]["verified"])
        self.assertEqual(service.call_args_list, [mock.call("enable"), mock.call("restart")])
        self.assertIn(["systemctl", "restart", "caddy.service"], self.commands)
        self.assertIn("HTTPS 订阅已验证", output.getvalue())

    def test_existing_import_is_not_duplicated_and_repeat_is_idempotent(self):
        text = self.original + f"import {self.site.parent}/*.conf\n".encode()
        self.main.write_bytes(text)
        with self.isolated():
            publish.deploy(traffic, "sub.example.com")
            first = self.site.read_bytes()
            publish.deploy(traffic, "sub.example.com")
        self.assertEqual(self.main.read_bytes(), text)
        self.assertEqual(self.site.read_bytes(), first)

    def test_domain_change_replaces_only_owned_site(self):
        with self.isolated():
            publish.deploy(traffic, "old.example.com")
            publish.deploy(traffic, "new.example.com")
        self.assertNotIn("old.example.com", self.site.read_text())
        self.assertIn("new.example.com", self.site.read_text())
        self.assertEqual(traffic.read_state()["publication"]["domain"], "new.example.com")
        self.assertEqual(self.main.read_text().count("import"), 1)

    def test_nonstandard_https_port_is_honored(self):
        self.base_json["apps"]["http"]["https_port"] = 8443
        with self.isolated():
            publish.deploy(traffic, "sub.example.com")
        self.assertIn("https://sub.example.com:8443", self.site.read_text())
        self.assertTrue(traffic.token_url().startswith("https://sub.example.com:8443/"))

    def test_admin_enabled_reloads_without_restart(self):
        self.base_json["admin"] = {}
        with self.isolated():
            publish.deploy(traffic, "sub.example.com")
        self.assertTrue(any(x[:2] == ["caddy", "reload"] for x in self.commands))
        self.assertNotIn(["systemctl", "restart", "caddy.service"], self.commands)

    def test_inactive_service_is_started(self):
        self.active = False
        with self.isolated():
            publish.deploy(traffic, "sub.example.com")
        self.assertIn(["systemctl", "start", "caddy.service"], self.commands)

    def test_validation_failure_restores_files_and_does_not_touch_services(self):
        self.fail_validate = True
        before = traffic.state_path().read_bytes()
        with self.isolated() as (service, _output), self.assertRaises(publish.PublicationError):
            publish.deploy(traffic, "sub.example.com")
        self.assertEqual(self.main.read_bytes(), self.original)
        self.assertFalse(self.site.exists())
        self.assertEqual(traffic.state_path().read_bytes(), before)
        service.assert_not_called()
        self.assertNotIn(["systemctl", "restart", "caddy.service"], self.commands)

    def test_failed_update_restores_old_domain_files_and_state(self):
        with self.isolated():
            publish.deploy(traffic, "old.example.com")
        previous_main, previous_site = self.main.read_bytes(), self.site.read_bytes()
        previous_state = traffic.state_path().read_bytes()
        self.fail_reload = True
        with self.isolated(), self.assertRaises(publish.PublicationError):
            publish.deploy(traffic, "new.example.com")
        self.assertEqual(self.main.read_bytes(), previous_main)
        self.assertEqual(self.site.read_bytes(), previous_site)
        self.assertEqual(traffic.state_path().read_bytes(), previous_state)
        self.assertEqual(self.commands[-1], ["systemctl", "restart", "caddy.service"])

    def test_unverified_https_is_retained_but_not_reported_ready(self):
        with self.isolated(verified=False) as (_service, output):
            self.assertFalse(publish.deploy(traffic, "sub.example.com"))
        self.assertTrue(self.site.is_file())
        self.assertFalse(traffic.read_state()["publication"]["verified"])
        self.assertIn("HTTPS 尚未验证", output.getvalue())
        self.assertNotIn("HTTPS 订阅已验证", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            traffic.print_url()
        self.assertIn("尚未验证", errors.getvalue())

    def test_conflicting_exact_or_wildcard_domain_is_rejected(self):
        for host in ("sub.example.com", "*.example.com"):
            self.base_json["apps"]["http"]["servers"]["srv0"]["routes"] = [{"match": [{"host": [host]}]}]
            with self.isolated(), self.assertRaisesRegex(publish.PublicationError, "其他 Caddy 站点"):
                publish.deploy(traffic, "sub.example.com")
            self.assertEqual(self.main.read_bytes(), self.original)
            self.assertFalse(self.site.exists())

    def test_refuses_unowned_or_manually_modified_site(self):
        self.site.parent.mkdir(parents=True)
        self.site.write_text("# user site\n")
        with self.isolated(), self.assertRaisesRegex(publish.PublicationError, "拒绝覆盖"):
            publish.deploy(traffic, "sub.example.com")
        self.assertEqual(self.site.read_text(), "# user site\n")

    def test_symbolic_link_main_is_refused(self):
        original = self.main.with_name("original")
        self.main.rename(original)
        self.main.symlink_to(original)
        with self.isolated(), self.assertRaisesRegex(publish.PublicationError, "符号链接"):
            publish.deploy(traffic, "sub.example.com")
        self.assertEqual(original.read_bytes(), self.original)

    def test_dns_failure_has_no_file_or_service_side_effect(self):
        with self.isolated() as (service, _output), \
             mock.patch.object(publish.socket, "getaddrinfo", side_effect=socket.gaierror()), \
             self.assertRaisesRegex(publish.PublicationError, "尚未解析"):
            publish.deploy(traffic, "sub.example.com")
        self.assertEqual(self.commands, [])
        service.assert_not_called()
        self.assertFalse(self.site.exists())

    def test_dns_pointing_elsewhere_is_rejected_before_services_or_install(self):
        with self.isolated() as (service, _output), \
             mock.patch.object(publish.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("203.0.113.99", 0))]), \
             self.assertRaisesRegex(publish.PublicationError, "本机地址不匹配"):
            publish.deploy(traffic, "sub.example.com")
        self.assertTrue(all(command[:2] == ["ip", "-j"] for command in self.commands))
        service.assert_not_called()
        self.assertFalse(self.site.exists())

    def test_remote_node_hostname_does_not_become_a_trusted_local_ip(self):
        state = traffic.read_state()
        state["config"]["address"] = "remote-node.example.net"
        traffic.write_state(state)
        with self.isolated() as (service, _output), \
             mock.patch.object(publish.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("203.0.113.99", 0))]) as resolve, \
             self.assertRaisesRegex(publish.PublicationError, "本机地址不匹配"):
            publish.deploy(traffic, "sub.example.com")
        resolve.assert_called_once_with("sub.example.com", None, type=socket.SOCK_STREAM)
        service.assert_not_called()
        self.assertFalse(self.site.exists())

    def test_main_permissions_and_ownership_are_preserved(self):
        self.main.chmod(0o640)
        before = self.main.stat()
        with self.isolated():
            publish.deploy(traffic, "sub.example.com")
        after = self.main.stat()
        self.assertEqual((before.st_uid, before.st_gid, before.st_mode & 0o777),
                         (after.st_uid, after.st_gid, after.st_mode & 0o777))

    def test_atomic_writer_restores_original_owner_when_different(self):
        with mock.patch.object(publish.os, "fchown") as chown:
            publish.atomic_write(self.main, b"test\n", owner=(os.geteuid() + 1, os.getegid() + 1))
        self.assertEqual(chown.call_args.args[1:], (os.geteuid() + 1, os.getegid() + 1))

    def test_interrupt_during_validation_restores_files(self):
        with self.isolated(), mock.patch.object(publish, "adapt", side_effect=KeyboardInterrupt()), \
             self.assertRaises(KeyboardInterrupt):
            publish.deploy(traffic, "sub.example.com")
        self.assertEqual(self.main.read_bytes(), self.original)
        self.assertFalse(self.site.exists())

    def test_openrc_service_reads_config_from_conf_d(self):
        service = self.base / "etc/init.d/caddy"
        service.parent.mkdir(parents=True)
        service.write_text('command_args="$caddy_opts"\n')
        config = self.base / "etc/conf.d/caddy"
        config.parent.mkdir(parents=True)
        config.write_text(f'caddy_opts="--config {self.main}"\n')
        publish.check_service_config("openrc", self.main)

    def test_service_using_another_config_is_refused(self):
        with mock.patch.object(publish, "run", return_value=subprocess.CompletedProcess([], 0, "--config /other", "")), \
             self.assertRaisesRegex(publish.PublicationError, "未知配置"):
            publish.check_service_config("systemd", self.main)

    def test_cli_domain_dispatch_and_failed_verification_exit_code(self):
        with mock.patch.object(traffic, "configure_domain") as deploy:
            self.assertEqual(traffic.main(["domain", "sub.example.com"]), 0)
        deploy.assert_called_once_with("sub.example.com")
        fake = mock.Mock()
        fake.PublicationError = publish.PublicationError
        fake.deploy.return_value = False
        with mock.patch("importlib.util.module_from_spec", return_value=fake), \
             mock.patch("importlib.util.spec_from_file_location", return_value=mock.Mock()), \
             contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(traffic.main(["domain", "sub.example.com"]), 1)
        self.assertIn("HTTPS 未验证通过", error.getvalue())

    def test_malformed_publication_is_not_silently_accepted(self):
        state = traffic.read_state()
        state["publication"] = {"domain": "evil.com\nrespond 200", "https_port": 443,
                                "backend_port": 18080, "verified": True}
        traffic.write_state(state)
        with self.assertRaises(traffic.TrafficError):
            traffic.read_state()

    def test_token_rotation_keeps_public_domain(self):
        with self.isolated():
            publish.deploy(traffic, "sub.example.com")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            traffic.rotate_token()
        self.assertIn("https://sub.example.com/sub/", output.getvalue())
        self.assertNotEqual(traffic.read_state()["token"], "A" * 43)

    def test_port_change_warns_publication_needs_update(self):
        with self.isolated():
            publish.deploy(traffic, "sub.example.com")
        state = traffic.read_state()
        state["config"]["port"] = 18081
        traffic.write_state(state)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as output:
            traffic.print_url()
        self.assertIn("端口已变化", output.getvalue())

    def test_import_matching_relative_glob(self):
        text = "import sites/*.conf\n"
        self.assertEqual(publish.import_site(text, self.main, self.site), text)

    def test_missing_main_is_created(self):
        self.main.unlink()
        with self.isolated():
            publish.deploy(traffic, "sub.example.com")
        self.assertTrue(self.main.exists())
        self.assertTrue(self.site.exists())

    def test_package_manager_install_uses_only_configured_sources(self):
        def which(command):
            return "/usr/bin/apt-get" if command == "apt-get" else None
        with mock.patch.object(publish.shutil, "which", side_effect=which), \
             mock.patch.object(publish.pathlib.Path, "is_file", return_value=False), \
             mock.patch.object(publish, "run") as command, contextlib.redirect_stdout(io.StringIO()), \
             self.assertRaises(publish.PublicationError):
            publish.caddy_binary()
        self.assertEqual(command.call_args_list, [mock.call(["apt-get", "update"], timeout=600),
                                                 mock.call(["apt-get", "install", "-y", "caddy"], timeout=600)])

    def test_openrc_service_actions_use_correct_commands(self):
        with mock.patch.object(publish, "run") as command:
            publish.service_action("openrc", "enable")
            publish.service_action("openrc", "is-active", check=False)
            publish.service_action("openrc", "restart")
        self.assertEqual(command.call_args_list, [mock.call(["rc-update", "add", "caddy", "default"], check=True),
            mock.call(["rc-service", "caddy", "status"], check=False),
            mock.call(["rc-service", "caddy", "restart"], check=True)])

    def test_https_verification_requires_same_content_and_hides_errors(self):
        with mock.patch.object(publish, "fetch", side_effect=[b"node-a", b"node-b"]):
            self.assertFalse(publish.verify_https("https://public/sub/secret", "http://local/sub/secret", wait=0))
        with mock.patch.object(publish, "fetch", return_value=b"same"):
            self.assertTrue(publish.verify_https("https://public", "http://local", wait=0))
        with mock.patch.object(publish, "fetch", side_effect=OSError("secret")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(publish.verify_https("https://public", "http://local", wait=0))
        self.assertNotIn("secret", output.getvalue())


@unittest.skipUnless(os.environ.get("SB_TRAFFIC_CADDY_TEST_BIN"), "set SB_TRAFFIC_CADDY_TEST_BIN for real Caddy validation")
class RealCaddyTests(unittest.TestCase):
    def test_real_adapter_validates_existing_upstream_and_new_subscription_site(self):
        with tempfile.TemporaryDirectory(prefix="sb-real-caddy-") as temporary:
            base = pathlib.Path(temporary)
            main, site = base / "Caddyfile", base / "sites/sb-traffic.conf"
            site.parent.mkdir()
            binary = os.environ["SB_TRAFFIC_CADDY_TEST_BIN"]
            env = dict(os.environ, XDG_DATA_HOME=str(base / "data"), XDG_CONFIG_HOME=str(base / "config"))
            for port in (443, 8443):
                text = f"{{\n admin off\n http_port 80\n https_port {port}\n}}\nexisting.example.net:{port} {{\n respond ok\n}}\n"
                main.write_text(publish.import_site(text, main, site))
                site.write_text(publish.site_text("sub.example.com", port, 18080))
                for action in ("adapt", "validate"):
                    result = subprocess.run([binary, action, "--config", str(main), "--adapter", "caddyfile"],
                                            env=env, capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if action == "adapt":
                        adapted = json.loads(result.stdout)
                        self.assertIn("sub.example.com", set(publish.hosts(adapted)))
                        self.assertTrue(adapted["admin"]["disabled"])


if __name__ == "__main__":
    unittest.main()

"""Auto-detection helpers used by the wizard: default route, public address, name, offset."""
import importlib.util
import os
import pathlib
import socket
import tempfile
import unittest
from unittest import mock

EXT = pathlib.Path(__file__).resolve().parents[1]
TRAFFIC_PATH = EXT / "traffic.py"

SPEC = importlib.util.spec_from_file_location("traffic_detect_test_module", TRAFFIC_PATH)
traffic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(traffic)

ROUTE_HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
DEFAULT_ROUTE = "eth0\t00000000\t0102A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"


class DetectInterfaceTests(unittest.TestCase):
    def route_root(self, body):
        temp = tempfile.TemporaryDirectory(prefix="sb-route-")
        self.addCleanup(temp.cleanup)
        path = pathlib.Path(temp.name) / "net" / "route"
        path.parent.mkdir(parents=True)
        path.write_text(body, encoding="utf-8")
        return temp.name

    def test_picks_default_route_ignoring_loopback_and_non_default(self):
        root = self.route_root(
            ROUTE_HEADER
            + "eth1\t0002A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
            + "eth0\t00000000\t0102A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
            + "lo\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n")
        with mock.patch.dict(os.environ, {"SB_TRAFFIC_PROC_ROOT": root}):
            self.assertEqual(traffic.detect_interface(), "eth0")

    def test_lower_metric_wins_among_default_routes(self):
        root = self.route_root(
            ROUTE_HEADER
            + "eth9\t00000000\t0102A8C0\t0003\t0\t0\t500\t00000000\t0\t0\t0\n"
            + "eth2\t00000000\t0102A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n")
        with mock.patch.dict(os.environ, {"SB_TRAFFIC_PROC_ROOT": root}):
            self.assertEqual(traffic.detect_interface(), "eth2")

    def test_route_without_up_flag_is_ignored(self):
        root = self.route_root(
            ROUTE_HEADER + "eth7\t00000000\t0102A8C0\t0002\t0\t0\t0\t00000000\t0\t0\t0\n")
        with mock.patch.dict(os.environ, {"SB_TRAFFIC_PROC_ROOT": root}):
            with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
                self.assertIsNone(traffic.detect_interface())

    def test_missing_proc_and_missing_ip_command_returns_none(self):
        with mock.patch.dict(os.environ, {"SB_TRAFFIC_PROC_ROOT": "/nonexistent-sb-proc"}):
            with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
                self.assertIsNone(traffic.detect_interface())


class DetectAddressTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("SB_TRAFFIC_OFFLINE", None)
        self.addCleanup(lambda: os.environ.pop("SB_TRAFFIC_OFFLINE", None))

    def test_ipv4_is_preferred_over_ipv6(self):
        seen = []

        def fetch(family):
            seen.append(family)
            return "fl=abc\nip=203.0.113.9\nts=1\n" if family == socket.AF_INET else "ip=2001:db8::1\n"

        self.assertEqual(traffic.detect_address(fetch), "203.0.113.9")
        self.assertEqual(seen, [socket.AF_INET])

    def test_ipv6_is_used_when_ipv4_reports_nothing(self):
        def fetch(family):
            return None if family == socket.AF_INET else "ip=2001:db8::1\n"

        self.assertEqual(traffic.detect_address(fetch), "2001:db8::1")

    def test_offline_switch_skips_the_network_entirely(self):
        with mock.patch.dict(os.environ, {"SB_TRAFFIC_OFFLINE": "1"}):
            with mock.patch.object(traffic, "_fetch_trace",
                                   side_effect=AssertionError("must not touch the network")):
                self.assertIsNone(traffic.detect_address())

    def test_fetch_failures_are_swallowed(self):
        def fetch(_family):
            raise OSError("network down")

        self.assertIsNone(traffic.detect_address(fetch))

    def test_control_characters_are_rejected(self):
        self.assertIsNone(traffic.detect_address(lambda _family: "ip=bad\x01value\n"))


class NameAndOffsetTests(unittest.TestCase):
    def test_localhost_style_hostname_falls_back_to_vps(self):
        for hostname in ("localhost", "localhost.localdomain", "   ", ""):
            with mock.patch.object(traffic.socket, "gethostname", return_value=hostname):
                self.assertEqual(traffic.default_name(), "VPS")

    def test_meaningful_hostname_is_used(self):
        with mock.patch.object(traffic.socket, "gethostname", return_value="hk-node-1"):
            self.assertEqual(traffic.default_name(), "hk-node-1")

    def test_local_utc_offset_is_whole_minutes(self):
        offset = traffic.local_utc_offset()
        self.assertIsInstance(offset, int)
        self.assertEqual(offset % 60, 0)


class ConfigureAutoDetectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sb-configure-auto-")
        self.addCleanup(self.temp.cleanup)
        base = pathlib.Path(self.temp.name)
        for interface in ("eth0", "eth1"):
            stats = base / "sys/class/net" / interface / "statistics"
            stats.mkdir(parents=True)
            (stats / "rx_bytes").write_text("100", encoding="utf-8")
            (stats / "tx_bytes").write_text("200", encoding="utf-8")
        boot = base / "proc/sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text("boot-x", encoding="utf-8")
        route = base / "proc/net/route"
        route.parent.mkdir(parents=True)
        route.write_text(ROUTE_HEADER + DEFAULT_ROUTE, encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "SB_TRAFFIC_DATA": str(base / "data"),
            "SB_TRAFFIC_SYS_ROOT": str(base / "sys"),
            "SB_TRAFFIC_PROC_ROOT": str(base / "proc"),
            "SB_TRAFFIC_ROOT": str(base / "sing-box"),
            "SB_TRAFFIC_OFFLINE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_interface_is_autodetected_without_a_flag(self):
        self.assertEqual(traffic.main(["configure", "--quota", "1TB",
                                       "--address", "203.0.113.10"]), 0)
        self.assertEqual(traffic.read_state()["config"]["interface"], "eth0")

    def test_explicit_interface_still_wins_over_detection(self):
        self.assertEqual(traffic.main(["configure", "--interface", "eth1", "--quota", "1TB",
                                       "--address", "203.0.113.10"]), 0)
        self.assertEqual(traffic.read_state()["config"]["interface"], "eth1")

    def test_undetectable_address_raises_a_clear_error_and_writes_nothing(self):
        self.assertEqual(traffic.main(["configure", "--quota", "1TB"]), 1)
        self.assertFalse(traffic.state_path().exists())

    def test_quota_is_still_mandatory_on_first_configure(self):
        self.assertEqual(traffic.main(["configure", "--address", "203.0.113.10"]), 1)
        self.assertFalse(traffic.state_path().exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

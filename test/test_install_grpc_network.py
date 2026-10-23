"""宿主机网络服务安装参数与权限边界。"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import json
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "script"))
import install_grpc_network as install


class InstallTests(unittest.TestCase):
    def test_install_readiness_requires_persisted_and_applied_state(self):
        class Connection:
            state = dict(persistent=True, applied=False)
            def __init__(self, *_args, **_kwargs):
                pass
            def request(self, *_args):
                pass
            def getresponse(self):
                return SimpleNamespace(status=200, read=lambda _limit: json.dumps(self.state).encode())
            def close(self):
                pass
        with mock.patch.object(install.http.client, "HTTPConnection", Connection), mock.patch.object(install.time, "sleep"):
            with self.assertRaises(ValueError):
                install.wait_ready("/unused/socket")
            Connection.state = dict(persistent=True, applied=True)
            install.wait_ready("/unused/socket")

    def test_installer_only_allows_bounded_network_values(self):
        args = install.parser().parse_args(["--enable"])
        self.assertTrue(install.configuration(args)["enabled"])
        for flag, value in [("--port", "0"), ("--port", "50001"), ("--mss", "1500"), ("--uid", "0"),
                            ("--external-interface", "eth0;id"), ("--work-dir", "/"),
                            ("--work-dir", "relative")]:
            with self.subTest(flag=flag, value=value), self.assertRaises(ValueError):
                install.configuration(install.parser().parse_args([flag, value]))

    def test_unit_runs_in_host_network_and_bounds_root_capabilities(self):
        unit = install.unit_source("/home/platform work", 10001)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertIn("--cleanup", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("CAP_NET_ADMIN", unit)
        self.assertNotIn("PrivateNetwork", unit)
        self.assertNotIn("CAP_SYS_ADMIN", unit)
        self.assertNotIn("docker.sock", unit)
        self.assertIn("'/home/platform work/runtime/network-control/control.sock'", unit)

    def test_atomic_install_rejects_link_and_preserves_old_content_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            install.atomic_write(path, b"original")
            self.assertEqual(path.read_bytes(), b"original")
            if hasattr(Path, "symlink_to"):
                link = Path(directory) / "link"
                try:
                    link.symlink_to(path)
                except OSError:
                    return
                with self.assertRaises(ValueError):
                    install.atomic_write(link, b"changed")
                self.assertEqual(path.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()

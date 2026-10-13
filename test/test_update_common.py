import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "script"))
import update_common as common


def manifest(channel="ci", version=None):
    version = version or "0.0.1-ci.20261013t080000.12.1+sha.abcdef1"
    root = "https://updates.example/platform/" + channel + "/"
    return {"schema_version": 1, "project": "mskdsp-platform", "channel": channel,
            "version": version, "commit": "abcdef1" + "0" * 33,
            "build_time": "2026-10-13T08:00:00Z", "published_at": "2026-10-13T08:00:00Z",
            "platforms": {"linux-amd64": {"binary_url": root + "v/program.tar.gz",
                "binary_sha256": "0" * 64, "binary_size": 128}}}


class CommonTests(unittest.TestCase):
    def test_download_checksum_size_and_redirect_boundary(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.endswith("redirect"):
                    self.send_response(302)
                    self.send_header("Location", "/stable/binary")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "4")
                    self.end_headers()
                    self.wfile.write(b"test")
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        root = f"http://127.0.0.1:{server.server_port}/ci/"
        transport = common.Transport(allow_loopback_http=True)
        entry = {"binary_url": root + "binary", "binary_size": 4, "binary_sha256": hashlib.sha256(b"test").hexdigest()}
        with tempfile.TemporaryDirectory() as temp:
            path = common.download_artifact(entry, temp, root + "latest.json", transport)
            self.assertEqual(path.read_bytes(), b"test")
            path.unlink()
            for changed in ({**entry, "binary_size": 3}, {**entry, "binary_size": 5},
                            {**entry, "binary_sha256": "0" * 64}, {**entry, "binary_url": root + "redirect"}):
                with self.subTest(changed=changed), self.assertRaises(common.UpdateError):
                    common.download_artifact(changed, temp, root + "latest.json", transport)
                self.assertEqual(list(Path(temp).iterdir()), [])

    @unittest.skipUnless(common.fcntl is not None, "实际文件锁仅在 Linux 运行")
    def test_file_lock_rejects_competing_process_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            with common.file_lock(temp):
                with self.assertRaises(common.UpdateError):
                    with common.file_lock(temp):
                        pass

    def test_transport_rejects_escape_and_http_by_default(self):
        transport = common.Transport()
        root = "https://updates.example/platform/ci/latest.json"
        for url in ("http://updates.example/file", "https://u:p@updates.example/file",
                    "https://updates.example/%2e%2e/file", "https://updates.example/file?q=1",
                    "https://evil.example/platform/ci/file", "https://updates.example/platform/stable/file"):
            with self.subTest(url=url), self.assertRaises(common.UpdateError):
                transport.validate_url(url, root)
        local = common.Transport(allow_loopback_http=True)
        local.validate_url("http://127.0.0.1:1234/ci/latest.json")
        with self.assertRaises(common.UpdateError):
            local.validate_url("http://example.com/ci/latest.json")

    def test_manifest_identity_channel_and_monotonic_versions(self):
        data = manifest()
        url = "https://updates.example/platform/ci/latest.json"
        self.assertEqual(common.validate_manifest(data, url)["binary_size"], 128)
        self.assertFalse(common.needs_update(data, data))
        for key, value in (("project", "slavesim"), ("commit", "1" * 40), ("channel", "stable")):
            changed = {**data, key: value}
            with self.assertRaises(common.UpdateError):
                common.validate_manifest(changed, url)
        with self.assertRaises(common.UpdateError):
            common.needs_update(data, {**data, "version": "0.0.1-ci.20261014t080000.1.1+sha.abcdef1"})
        with self.assertRaises(common.UpdateError):
            common.needs_update(data, {**data, "channel": "beta"})
        with self.assertRaises(common.UpdateError):
            common.needs_update(data, {**data, "build_time": "2026-10-13T08:01:00Z"})

    def test_archive_rejects_everything_except_one_root_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for names in (("../mskdsp-platform",), ("mskdsp-platform", "mskdsp-platform"),
                          ("mskdsp-platform", "README"), ("dir/mskdsp-platform",)):
                archive = folder / "archive.tar.gz"
                with tarfile.open(archive, "w:gz") as output:
                    for name in names:
                        item = tarfile.TarInfo(name)
                        item.size = 4
                        output.addfile(item, io.BytesIO(b"test"))
                with self.subTest(names=names), self.assertRaises(common.UpdateError):
                    common.extract_binary(archive, folder / "out")
                if (folder / "out").exists():
                    import shutil
                    shutil.rmtree(folder / "out")
            for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE):
                with tarfile.open(archive, "w:gz") as output:
                    item = tarfile.TarInfo("mskdsp-platform")
                    item.type = kind
                    item.linkname = "other"
                    output.addfile(item)
                with self.assertRaises(common.UpdateError):
                    common.extract_binary(archive, folder / "out")

    def test_elf_architecture_and_atomic_state(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "binary"
            header = bytearray(64)
            header[:6] = b"\x7fELF\x02\x01"
            header[18:20] = (62).to_bytes(2, "little")
            path.write_bytes(header)
            common.verify_elf(path, "linux-amd64")
            header[18:20] = (183).to_bytes(2, "little")
            path.write_bytes(header)
            with self.assertRaises(common.UpdateError):
                common.verify_elf(path, "linux-amd64")
            state = Path(temp) / "state.json"
            common.atomic_json(state, {"version": "测试"})
            self.assertEqual(json.loads(state.read_text(encoding="utf-8")), {"version": "测试"})
            self.assertEqual(list(Path(temp).glob(".state.json.*")), [])


if __name__ == "__main__":
    unittest.main()

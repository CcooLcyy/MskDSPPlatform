"""验证一次性 R2 清理只作用于授权前缀，并在变化和失败时留下证据。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from script import retire_legacy_r2 as retire


def item(key, size=1, etag='"abc"'):
    return {"Key": key, "Size": size, "ETag": etag, "LastModified": "2026-11-06T01:00:00.123Z"}


class FakeR2:
    def __init__(self, objects):
        self.objects = {value["Key"]: dict(value) for value in objects}
        self.calls = []
        self.fail_delete = None
        self.changed_head = None

    def __call__(self, operation, *arguments):
        self.calls.append((operation, arguments))
        if operation == "list-objects-v2":
            return {"IsTruncated": False, "Contents": list(self.objects.values())}
        key = arguments[arguments.index("--key") + 1]
        if operation == "head-object":
            value = self.objects[key]
            return {"ContentLength": value["Size"], "ETag": "changed" if key == self.changed_head else value["ETag"]}
        if operation == "delete-object":
            if key == self.fail_delete:
                raise retire.RetirementError("模拟删除失败")
            del self.objects[key]
            return {}
        raise AssertionError(operation)


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def test_exact_prefixes_keep_similar_names_and_other_products(self):
        targets = [item(prefix + "ci/package.bin") for prefix in retire.PREFIXES]
        keep = [item("mskdsp-platform/stable/latest.json"), item("mskdsp-upper-other/a"),
                item("mskdsp-lower-backup/a"), item("mskdsp-upper")]
        api = FakeR2(targets + keep)
        retire.snapshot(self.root, api)
        retire.apply(self.root, api)
        self.assertEqual(set(api.objects), {value["Key"] for value in keep})
        result = json.loads((self.root / "result.json").read_text())
        self.assertEqual((result["status"], result["deleted"], result["preserved"]), ("success", 3, 4))
        self.assertTrue(all(op["status"] == "verified_absent" for op in result["operations"]))

    def test_pagination_collects_every_page(self):
        calls = []
        pages = [{"IsTruncated": True, "Contents": [item("a")], "NextContinuationToken": "next"},
                 {"IsTruncated": False, "Contents": [item("b")]}]
        def api(operation, *args):
            calls.append(args)
            return pages.pop(0)
        self.assertEqual([value["Key"] for value in retire.list_objects(api)], ["a", "b"])
        self.assertIn("--continuation-token", calls[1])
        self.assertIn("next", calls[1])

    def test_repeated_page_token_is_rejected(self):
        page = {"IsTruncated": True, "NextContinuationToken": "same"}
        with self.assertRaises(retire.RetirementError):
            retire.list_objects(lambda *args: page)

    def test_missing_token_and_malformed_records_are_rejected(self):
        for page in ({"IsTruncated": True}, {"IsTruncated": "false"},
                     {"IsTruncated": False, "Contents": [{"Key": "incomplete"}]}):
            with self.subTest(page=page), self.assertRaises(retire.RetirementError):
                retire.list_objects(lambda *args: page)

    def test_duplicate_keys_are_rejected(self):
        page = {"IsTruncated": False, "Contents": [item("a"), item("a")]}
        with self.assertRaises(retire.RetirementError):
            retire.list_objects(lambda *args: page)

    def test_tampered_manifest_is_rejected_before_api_call(self):
        api = FakeR2([item("mskdsp-upper/a")])
        retire.snapshot(self.root, api)
        (self.root / "inventory.json").write_text("{}")
        api.calls.clear()
        with self.assertRaises(retire.RetirementError):
            retire.apply(self.root, api)
        self.assertEqual(api.calls, [])

    def test_forged_bucket_with_matching_digest_is_rejected(self):
        api = FakeR2([item("mskdsp-upper/a")])
        retire.snapshot(self.root, api)
        path = self.root / "inventory.json"
        data = json.loads(path.read_text())
        data["bucket"] = "other-bucket"
        retire.write_json(path, data)
        (self.root / "inventory.sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest())
        with self.assertRaises(retire.RetirementError):
            retire.apply(self.root, api)
        self.assertFalse(any(op == "delete-object" for op, _ in api.calls))

    def test_bucket_change_before_deletion_stops_all_deletes(self):
        api = FakeR2([item("mskdsp-upper/a")])
        retire.snapshot(self.root, api)
        api.objects["mskdsp-upper/new"] = item("mskdsp-upper/new")
        with self.assertRaises(retire.RetirementError):
            retire.apply(self.root, api)
        self.assertFalse(any(op == "delete-object" for op, _ in api.calls))

    def test_head_change_stops_delete_and_preserves_object(self):
        api = FakeR2([item("mskdsp-upper/a")])
        retire.snapshot(self.root, api)
        api.changed_head = "mskdsp-upper/a"
        with self.assertRaises(retire.RetirementError):
            retire.apply(self.root, api)
        self.assertIn("mskdsp-upper/a", api.objects)
        self.assertFalse(any(op == "delete-object" for op, _ in api.calls))

    def test_partial_failure_records_completed_and_uncertain_operations(self):
        api = FakeR2([item("mskdsp-upper/a"), item("mskdsp-upper/b"), item("mskdsp-upper/c")])
        retire.snapshot(self.root, api)
        api.fail_delete = "mskdsp-upper/b"
        with self.assertRaises(retire.RetirementError):
            retire.apply(self.root, api)
        result = json.loads((self.root / "result.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual([op["status"] for op in result["operations"]], ["delete_acknowledged", "deleting"])
        self.assertEqual(set(api.objects), {"mskdsp-upper/b", "mskdsp-upper/c"})

    def test_wrong_account_is_rejected_without_starting_cli(self):
        with patch.dict(retire.os.environ, {"R2_ACCOUNT_ID": "wrong", "AWS_ACCESS_KEY_ID": "key", "AWS_SECRET_ACCESS_KEY": "secret"}), patch.object(retire.subprocess, "run") as run:
            with self.assertRaises(retire.RetirementError):
                retire.aws("list-objects-v2")
            run.assert_not_called()

    def test_delete_acknowledgement_without_removal_is_not_success(self):
        inner = FakeR2([item("mskdsp-upper/a")])
        def api(operation, *args):
            return {} if operation == "delete-object" else inner(operation, *args)
        retire.snapshot(self.root, api)
        with self.assertRaises(retire.RetirementError):
            retire.apply(self.root, api)
        result = json.loads((self.root / "result.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertIn("mskdsp-upper/a", inner.objects)

    def test_non_target_concurrent_change_is_reported_without_deleting_it(self):
        inner = FakeR2([item("mskdsp-upper/a"), item("other/keep")])
        def api(operation, *args):
            response = inner(operation, *args)
            if operation == "delete-object":
                inner.objects["other/keep"]["ETag"] = "changed"
            return response
        retire.snapshot(self.root, api)
        with self.assertRaises(retire.RetirementError):
            retire.apply(self.root, api)
        deletes = [args[args.index("--key") + 1] for op, args in inner.calls if op == "delete-object"]
        self.assertEqual(deletes, ["mskdsp-upper/a"])
        self.assertIn("other/keep", inner.objects)

    def test_empty_target_set_keeps_other_objects(self):
        api = FakeR2([item("mskdsp-platform/stable/latest.json")])
        retire.snapshot(self.root, api)
        retire.apply(self.root, api)
        self.assertFalse(any(op == "delete-object" for op, _ in api.calls))
        result = json.loads((self.root / "result.json").read_text())
        self.assertEqual((result["status"], result["deleted"], result["preserved"]), ("success", 0, 1))


if __name__ == "__main__":
    unittest.main()

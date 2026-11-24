# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Cua AI, Inc.
"""Pure filesystem/mocked-process tests; never build or run a native driver."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "workon_patch", Path(__file__).resolve().parents[1] / "workon_cua_patch_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
SHA = "a" * 40


class PatchReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "source"
        self.rust = self.root / "libs/cua-driver/rust"
        self.rust.mkdir(parents=True)
        self.output = Path(self.directory.name) / "output"
        (self.rust / "Cargo.toml").write_text(f'[workspace.package]\nversion="{release.VERSION}"\n')
        (self.root / "LICENSE.md").write_text("MIT license fixture")
        (self.rust / "THIRD_PARTY_NOTICES.md").write_text("notices fixture")
        for target in ("aarch64-apple-darwin", "x86_64-apple-darwin", "workon-universal"):
            directory = self.rust / "target" / target
            if target != "workon-universal":
                directory /= "release"
            directory.mkdir(parents=True)
            for name in ("cua-driver", "cua-cursor-theme"):
                (directory / name).write_bytes(f"native fixture {name} {target} {SHA}".encode())
        self.calls = []
        self.thin_overrides = {}
        self.runner = patch.object(release, "run", side_effect=self.fake_run)
        self.runner.start()
        self.addCleanup(self.runner.stop)

    def fake_run(self, *args, cwd=None):
        self.calls.append(args)
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return SHA
        if args[:3] == ("git", "status", "--porcelain"):
            return ""
        if args[:3] == ("git", "merge-base", "--is-ancestor"):
            return ""
        if args[:3] == ("git", "diff", "--binary"):
            return "diff fixture"
        if args[:2] == ("lipo", "-archs"):
            return "x86_64 arm64"
        if args[0] == "lipo" and "-thin" in args:
            name = Path(args[1]).name
            arch = args[args.index("-thin") + 1]
            target = {"arm64": "aarch64-apple-darwin", "x86_64": "x86_64-apple-darwin"}[arch]
            data = self.thin_overrides.get((name, arch), (self.rust / "target" / target / "release" / name).read_bytes())
            Path(args[args.index("-output") + 1]).write_bytes(data)
            return ""
        if args[-1] == "--version":
            return f"cua-driver {release.VERSION}"
        if args[:3] == ("gh", "release", "create"):
            self.assertIn("--draft", args)
            self.assertIn("--prerelease", args)
            self.assertIn("--latest=false", args)
            self.assertNotIn("--clobber", args)
            return "draft url"
        if args[:3] == ("gh", "release", "download"):
            directory = Path(args[args.index("--dir") + 1])
            for name in release.FILES:
                (directory / name).write_bytes((self.output / name).read_bytes())
            return ""
        self.fail(f"unexpected command: {args}")

    def prepare(self):
        release.prepare(self.root, SHA, self.output)

    def test_prepares_only_exact_payload_with_matching_receipt(self):
        self.prepare()
        self.assertEqual({p.name for p in self.output.iterdir()}, set(release.FILES))
        receipt = json.loads((self.output / "receipt.json").read_text())
        self.assertEqual(receipt["sourceCommit"], SHA)
        self.assertEqual(receipt["upstreamCommit"], release.UPSTREAM)
        self.assertEqual(receipt["asset"]["sha256"], release.digest(self.output / release.ASSET))
        self.assertTrue(receipt["embeddedSourceShaVerifiedInEachArchitecture"])
        self.assertEqual(set(receipt["universalSliceSha256"]), {
            "cua-driver/arm64", "cua-driver/x86_64", "cua-cursor-theme/arm64", "cua-cursor-theme/x86_64"})
        with tarfile.open(self.output / release.ASSET) as archive:
            self.assertEqual(set(archive.getnames()), {"cua-driver", "cua-cursor-theme", "LICENSE.md", "THIRD_PARTY_NOTICES.md"})
        # Only --version executes the fixture binary; no tools/App/permissions calls.
        self.assertEqual([c[-1] for c in self.calls if str(c[0]).endswith("/cua-driver")], ["--version"])

    def test_source_requires_exact_sha_clean_checkout_and_version(self):
        with self.assertRaisesRegex(ValueError, "40-character"):
            release.validate_source(self.root, "main")
        with self.assertRaisesRegex(ValueError, "does not match"):
            release.validate_source(self.root, "b" * 40)
        original = self.fake_run
        with patch.object(release, "run", side_effect=lambda *a, **kw: " M dirty" if a[:2] == ("git", "status") else original(*a, **kw)):
            with self.assertRaisesRegex(ValueError, "clean"):
                release.validate_source(self.root, SHA)
        (self.rust / "Cargo.toml").write_text('[workspace.package]\nversion="0.52.12"\n')
        with self.assertRaisesRegex(ValueError, "version"):
            release.validate_source(self.root, SHA)

    def test_source_requires_both_known_ancestors(self):
        release.validate_source(self.root, SHA)
        ancestors = [c[3] for c in self.calls if c[:3] == ("git", "merge-base", "--is-ancestor")]
        self.assertEqual(ancestors, [release.UPSTREAM, release.SHARING_PATCH])

    def test_refuses_output_replacement(self):
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, "replace"):
            self.prepare()

    def test_refuses_missing_source_sha_in_either_architecture(self):
        (self.rust / "target/x86_64-apple-darwin/release/cua-driver").write_bytes(b"stale driver")
        with self.assertRaisesRegex(ValueError, "embedded source SHA"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_refuses_stale_universal_slices_for_driver_and_theme_on_both_arches(self):
        for name in ("cua-driver", "cua-cursor-theme"):
            for arch in ("arm64", "x86_64"):
                with self.subTest(name=name, arch=arch):
                    self.thin_overrides = {(name, arch): f"same version {release.VERSION}, stale source".encode()}
                    with self.assertRaisesRegex(ValueError, "universal slice differs"):
                        self.prepare()
                    self.assertFalse(self.output.exists())
        self.thin_overrides = {}

    def test_refuses_non_universal_binary_or_wrong_reported_version(self):
        original = self.fake_run
        for bad, error in [("arm64", "universal"), ("version", "unexpected version")]:
            def fake(*args, **kwargs):
                if args[:2] == ("lipo", "-archs") and bad == "arm64":
                    return "arm64"
                if args[-1] == "--version":
                    return "cua-driver 0.52.12"
                return original(*args, **kwargs)
            with patch.object(release, "run", side_effect=fake):
                with self.assertRaisesRegex(ValueError, error):
                    self.prepare()

    def test_draft_refuses_wrong_repository_and_tampering_before_network(self):
        self.prepare()
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "trycua/cua"}):
            with self.assertRaisesRegex(ValueError, "exact WorkOn fork"):
                release.draft(self.root, SHA, self.output)
        (self.output / release.ASSET).write_bytes(b"changed")
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": release.REPOSITORY}), patch.object(release, "existing") as remote:
            with self.assertRaisesRegex(ValueError, "archive changed"):
                release.draft(self.root, SHA, self.output)
            remote.assert_not_called()

    def test_draft_refuses_existing_release_without_overwriting_assets(self):
        self.prepare()
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": release.REPOSITORY}), patch.object(release, "existing", side_effect=[{}, [{"id": 7, "tag_name": release.TAG}], {"id": 7, "tag_name": release.TAG, "draft": True, "assets": []}]):
            with self.assertRaisesRegex(ValueError, "release already exists"):
                release.draft(self.root, SHA, self.output)
        self.assertFalse(any(c[:3] == ("gh", "release", "create") for c in self.calls))

    def test_draft_refuses_tag_with_wrong_source_or_annotated_type(self):
        self.prepare()
        for tag in [{"object": {"type": "commit", "sha": "b" * 40}}, {"object": {"type": "tag", "sha": SHA}}]:
            with patch.dict(os.environ, {"GITHUB_REPOSITORY": release.REPOSITORY}), patch.object(release, "existing", side_effect=[{}, [], tag]):
                with self.assertRaisesRegex(ValueError, "existing tag"):
                    release.draft(self.root, SHA, self.output)

    def test_creates_draft_only_and_verifies_downloaded_payload(self):
        self.prepare()
        observed = {"id": 7, "tag_name": release.TAG, "draft": True, "prerelease": True, "target_commitish": SHA,
                    "assets": [{"name": name} for name in release.FILES]}
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": release.REPOSITORY}), patch.object(release, "existing", side_effect=[{}, [], None, [{"id": 7, "tag_name": release.TAG}], observed]):
            release.draft(self.root, SHA, self.output)
        self.assertFalse((self.output / ".draft-notes.tmp").exists())
        self.assertTrue(any(c[:3] == ("gh", "release", "download") for c in self.calls))

    def test_draft_lookup_finds_pending_tag_without_published_tag_endpoint(self):
        draft = {"id": 7, "tag_name": release.TAG, "draft": True}
        with patch.object(release, "existing", side_effect=[[draft], draft]) as api:
            self.assertEqual(release.release_by_pending_tag(), draft)
        self.assertEqual([c.args[0] for c in api.call_args_list], [
            f"repos/{release.REPOSITORY}/releases?per_page=100&page=1",
            f"repos/{release.REPOSITORY}/releases/7"])

    def test_draft_lookup_paginates_and_rejects_duplicate_pending_tags(self):
        draft = {"id": 7, "tag_name": release.TAG, "draft": True}
        unrelated = [{"id": n + 100, "tag_name": f"unrelated-{n}"} for n in range(100)]
        with patch.object(release, "existing", side_effect=[unrelated, [draft], draft]) as api:
            self.assertEqual(release.release_by_pending_tag(), draft)
            self.assertIn("page=2", api.call_args_list[1].args[0])
        first_page = [draft] + unrelated[:99]
        with patch.object(release, "existing", side_effect=[first_page, [dict(draft, id=8)]]):
            with self.assertRaisesRegex(ValueError, "multiple releases"):
                release.release_by_pending_tag()

    def test_draft_lookup_rejects_unreadable_or_changed_id_metadata(self):
        for rows in [None, {}, "bad response"]:
            with patch.object(release, "existing", return_value=rows):
                with self.assertRaisesRegex(ValueError, "listing unreadable"):
                    release.release_by_pending_tag()
        row = {"id": 7, "tag_name": release.TAG}
        for observed in [None, {}, dict(row, id=8), dict(row, tag_name="other")]:
            with patch.object(release, "existing", side_effect=[[row], observed]):
                with self.assertRaisesRegex(ValueError, "ID metadata"):
                    release.release_by_pending_tag()
        for status in [403, 500]:
            response = subprocess.CompletedProcess([], 1, f"HTTP/2.0 {status} Error\n\n{{}}", "error")
            with patch.object(subprocess, "run", return_value=response):
                with self.assertRaisesRegex(ValueError, "read failed"):
                    release.release_by_pending_tag()

    def test_http_absence_is_distinct_from_auth_and_transport_failure(self):
        for code, stdout, expected in [(1, "HTTP/2.0 404 Not Found\n\n{}", None), (0, 'HTTP/2.0 200 OK\n\n{"draft":true}', {"draft": True})]:
            with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, stdout, "")):
                self.assertEqual(release.existing("example"), expected)
        for stdout in ["", "HTTP/2.0 403 Forbidden\n\n{}", "HTTP/2.0 500 Server Error\n\n{}"]:
            with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout, "failure")):
                with self.assertRaisesRegex(ValueError, "read failed"):
                    release.existing("example")


if __name__ == "__main__":
    unittest.main()

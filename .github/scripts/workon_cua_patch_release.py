#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Cua AI, Inc.
"""Prepare this one WorkOn fork patch; optionally attach it to a draft only.

Never starts a daemon/App, probes permissions, signs, notarizes, or publishes.

Publisher-only follow-up: the already published workon.1 assets remain built
from 95b17744b951c6ceca1f729ce3327118acb52d3f. This lookup fix does not rewrite
those assets. A future workflow run builds its newly supplied clean source SHA;
the fixed workon.1 tag/asset guards still refuse replacing the existing release.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

REPOSITORY = "SYZhenYuZhao/cua"
VERSION = "0.52.12-workon.1"
TAG = f"workon-cua-v{VERSION}"
ASSET = f"cua-driver-rs-{VERSION}-darwin-universal-binary.tar.gz"
UPSTREAM = "182598bdb1d040fb2fcbf408b2e5e768f59889c4"
SHARING_PATCH = "f5e64d02c165611b3815c5b7e6162a9912accdb7"
FILES = (ASSET, "source.patch", "receipt.json", "SHA256SUMS")


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True, timeout=120).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_source(root, source_sha):
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("source_sha must be an exact lowercase 40-character commit")
    if run("git", "rev-parse", "HEAD", cwd=root) != source_sha:
        raise ValueError("checkout does not match source_sha")
    if run("git", "status", "--porcelain", "--untracked-files=all", cwd=root):
        raise ValueError("source checkout must be clean")
    for ancestor in (UPSTREAM, SHARING_PATCH):
        run("git", "merge-base", "--is-ancestor", ancestor, source_sha, cwd=root)
    cargo = (root / "libs/cua-driver/rust/Cargo.toml").read_text()
    package = re.search(r"(?ms)^\[workspace\.package\]\s*\n(.*?)(?=^\[|\Z)", cargo)
    versions = re.findall(r'^version\s*=\s*"([^"\n]+)"\s*$', package.group(1), re.MULTILINE) if package else []
    if versions != [VERSION]:
        raise ValueError("workspace version does not match this patch release")


def prepare(root, source_sha, output):
    validate_source(root, source_sha)
    if output.exists():
        raise ValueError("refusing to replace an existing output directory")
    rust = root / "libs/cua-driver/rust"
    universal = rust / "target/workon-universal"
    for name in ("cua-driver", "cua-cursor-theme"):
        archs = set(run("lipo", "-archs", str(universal / name)).split())
        if archs != {"arm64", "x86_64"}:
            raise ValueError(f"{name} is not an arm64+x86_64 universal binary")
    # Prove that lipo packaged the exact current thin binaries, including the
    # theme executable. Version/architecture labels alone admit stale sources.
    slice_hashes = {}
    targets = (("arm64", "aarch64-apple-darwin"), ("x86_64", "x86_64-apple-darwin"))
    with tempfile.TemporaryDirectory(prefix="workon-cua-slices-") as directory:
        for name in ("cua-driver", "cua-cursor-theme"):
            for arch, target in targets:
                thin = rust / "target" / target / "release" / name
                extracted = Path(directory) / f"{name}-{arch}"
                run("lipo", str(universal / name), "-thin", arch, "-output", str(extracted))
                if digest(extracted) != digest(thin):
                    raise ValueError(f"{name} {arch} universal slice differs from current thin binary")
                slice_hashes[f"{name}/{arch}"] = digest(extracted)
                if name == "cua-driver" and source_sha.encode("ascii") not in thin.read_bytes():
                    raise ValueError(f"{target} driver lacks the embedded source SHA")
    # --version exits in CLI parsing before AppKit or tool dispatch.
    reported_version = run(str(universal / "cua-driver"), "--version")
    if reported_version != f"cua-driver {VERSION}":
        raise ValueError("universal driver reports an unexpected version")
    output.mkdir(parents=True)
    members = {
        "cua-driver": universal / "cua-driver",
        "cua-cursor-theme": universal / "cua-cursor-theme",
        "LICENSE.md": root / "LICENSE.md",
        "THIRD_PARTY_NOTICES.md": rust / "THIRD_PARTY_NOTICES.md",
    }
    with tarfile.open(output / ASSET, "w:gz") as archive:
        for name, path in members.items():
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"missing/nonregular archive input: {name}")
            archive.add(path, arcname=name, recursive=False)
    (output / "source.patch").write_text(run("git", "diff", "--binary", UPSTREAM, source_sha, cwd=root) + "\n")
    receipt = {
        "schemaVersion": 1, "repository": REPOSITORY, "version": VERSION,
        "tag": TAG, "sourceCommit": source_sha, "upstreamCommit": UPSTREAM,
        "sharingPatchCommit": SHARING_PATCH,
        "compare": f"https://github.com/{REPOSITORY}/compare/{UPSTREAM}...{source_sha}",
        "asset": {"name": ASSET, "bytes": (output / ASSET).stat().st_size,
                  "sha256": digest(output / ASSET)},
        "files": {name: digest(path) for name, path in members.items()},
        "sourcePatchSha256": digest(output / "source.patch"),
        "rustToolchain": "1.97.1", "deploymentTarget": "13.0",
        "architectures": ["arm64", "x86_64"],
        "binaryVersionOutput": reported_version,
        "embeddedSourceShaVerifiedInEachArchitecture": True,
        "universalSliceSha256": slice_hashes,
        "signing": "No release signing or notarization performed; fork development artifact",
        "verification": {
            "publisherChecks": ["clean exact source", "both universal architectures",
                                "exact universal/thin binary slices", "per-architecture embedded source SHA",
                                "reported Cargo version"],
            "testEvidence": "Workflow logs record aux_window::tests; preparation does not itself run tests",
            "workflowRun": (f"https://github.com/{REPOSITORY}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                            if os.environ.get("GITHUB_RUN_ID", "").isdigit() else None),
            "desktopAcceptance": "not performed",
        },
        "limitations": ["System sharing-indicator classification patch only",
                        "Window restore/readiness and real-device acceptance remain unverified",
                        "Does not install or replace any authorized Helper"],
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    names = FILES[:-1]
    (output / "SHA256SUMS").write_text("".join(f"{digest(output / name)}  {name}\n" for name in names))
    validate_source(root, source_sha)


def existing(endpoint):
    response = subprocess.run(["gh", "api", endpoint, "--include"], capture_output=True, text=True, timeout=30)
    status = re.search(r"^HTTP/\S+ (\d+)", response.stdout, re.MULTILINE)
    if status and status.group(1) == "404":
        return None
    if response.returncode != 0 or not status or status.group(1) != "200":
        raise ValueError("GitHub read failed; refusing to infer that a release/tag is absent")
    return json.loads(response.stdout[response.stdout.index("\n\n") + 2:])


def release_by_pending_tag():
    """REST tag lookup covers published releases, not pending draft tags.

    Enumerate every page so an existing draft cannot be mistaken for absence;
    reject ambiguity, then read the selected immutable release ID.
    """
    matches = []
    page = 1
    while True:
        rows = existing(f"repos/{REPOSITORY}/releases?per_page=100&page={page}")
        if not isinstance(rows, list):
            raise ValueError("release listing unreadable; cannot prove absence")
        matches.extend(row for row in rows if row.get("tag_name") == TAG)
        if len(matches) > 1:
            raise ValueError("multiple releases have this pending tag; reconcile manually")
        if len(rows) < 100:
            break
        page += 1
    if not matches:
        return None
    release_id = matches[0].get("id")
    if type(release_id) is not int or release_id <= 0:
        raise ValueError("release listing has no valid release ID")
    observed = existing(f"repos/{REPOSITORY}/releases/{release_id}")
    if not isinstance(observed, dict) or observed.get("id") != release_id or observed.get("tag_name") != TAG:
        raise ValueError("release ID metadata did not match pending tag; reconcile manually")
    return observed


def draft(root, source_sha, output):
    validate_source(root, source_sha)
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("draft writes are restricted to the exact WorkOn fork")
    if {p.name for p in output.iterdir()} != set(FILES):
        raise ValueError("draft payload must contain exactly the prepared files")
    receipt = json.loads((output / "receipt.json").read_text())
    if (receipt.get("sourceCommit"), receipt.get("repository"), receipt.get("tag")) != (source_sha, REPOSITORY, TAG):
        raise ValueError("receipt source/repository/tag mismatch")
    if receipt["asset"] != {"name": ASSET, "bytes": (output / ASSET).stat().st_size, "sha256": digest(output / ASSET)}:
        raise ValueError("archive changed after preparation")
    expected = "".join(f"{digest(output / name)}  {name}\n" for name in FILES[:-1])
    if (output / "SHA256SUMS").read_text() != expected:
        raise ValueError("prepared checksums do not match payload")
    # Verify repository visibility before interpreting a ref's 404 as absence.
    if existing(f"repos/{REPOSITORY}") is None:
        raise ValueError("fork repository is not readable")
    if release_by_pending_tag() is not None:
        raise ValueError("release already exists; refusing to overwrite any assets")
    tag = existing(f"repos/{REPOSITORY}/git/ref/tags/{TAG}")
    if tag is not None and (tag["object"]["type"] != "commit" or tag["object"]["sha"] != source_sha):
        raise ValueError("existing tag is not the exact source commit")
    notes = output / ".draft-notes.tmp"
    notes.write_text(f"WorkOn fork development patch {VERSION}.\n\nSource: {source_sha}\nUpstream: {UPSTREAM}\n\nSharing-indicator classification only. No release signing/notarization, installation, permission probes, or real-device acceptance. This release stays draft.\n")
    try:
        run("gh", "release", "create", TAG, "--repo", REPOSITORY, "--target", source_sha,
            "--draft", "--prerelease", "--latest=false", "--title", f"WorkOn CUA patch {VERSION}",
            "--notes-file", str(notes), *(str(output / name) for name in FILES))
    finally:
        notes.unlink()
    observed = release_by_pending_tag()
    if not observed or observed.get("draft") is not True or observed.get("prerelease") is not True:
        raise ValueError("draft state was not verified; manual reconciliation required")
    if observed.get("target_commitish") != source_sha:
        raise ValueError("draft target differs from the prepared source; reconcile manually")
    if {a["name"] for a in observed.get("assets", [])} != set(FILES):
        raise ValueError("draft assets incomplete; manual reconciliation required, do not blindly retry")
    with tempfile.TemporaryDirectory(prefix="workon-cua-draft-check-") as directory:
        run("gh", "release", "download", TAG, "--repo", REPOSITORY, "--dir", directory,
            *(arg for name in FILES for arg in ("--pattern", name)))
        for name in FILES:
            if digest(Path(directory) / name) != digest(output / name):
                raise ValueError("draft downloaded bytes differ; reconcile manually")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "draft"))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    {"prepare": prepare, "draft": draft}[args.mode](root, args.source_sha, args.output.resolve())


if __name__ == "__main__":
    main()

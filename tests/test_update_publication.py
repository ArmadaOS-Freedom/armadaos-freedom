"""Offline tests for signed update-channel publication.

The publisher writes immutable release assets and then a signed channel branch;
these tests use throwaway keys and local fixtures, and never touch the network.
"""

import base64
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import test_update_channel_release as cr_fixture  # noqa: E402
import test_update_package_contract as pkg_fixture  # noqa: E402
from updates import system_manager  # noqa: E402
from updates.channel_release import make_channel  # noqa: E402
from updates.package_contract import validate_package  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "update_publication", ROOT / ".github/scripts/update-publication.py")
publication = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(publication)

REPOSITORY = "ArmadaOS-Freedom/armadaos-freedom"
PUBLISHED_AT = "2026-04-19T12:00:00Z"


def _release_url(tag, name):
    return f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"


def _part_manifest(pkg):
    return {
        "schemaVersion": 1,
        "format": "raw-split",
        "archive": pkg["archive"],
        "partSizeBytes": 4096,
        "parts": pkg["parts"],
    }


class PackageArtifact:
    """A signed package artifact laid out like the EDL Actions artifact."""

    def __init__(self, root, *, upgrade=True):
        self.root = Path(root)
        base = cr_fixture._upgrade_package_dict() if upgrade else pkg_fixture._base_package()
        archive_name = f"ArmadaOS-Freedom-{base['version']}.zip"
        chunks = [b"P" * 4096, b"Q" * 1024]
        parts = [
            {
                "fileName": f"{archive_name}.{index:03d}",
                "bytes": len(chunk),
                "sha256": sha256(chunk).hexdigest(),
            }
            for index, chunk in enumerate(chunks, 1)
        ]
        base["archive"] = {
            "fileName": archive_name,
            "bytes": sum(len(chunk) for chunk in chunks),
            "sha256": sha256(b"".join(chunks)).hexdigest(),
        }
        base["parts"] = parts
        self.package = validate_package(base)
        self.archive_name = archive_name
        self.directory = self.root / "output" / "edl" / self.package["version"]
        self.directory.mkdir(parents=True)
        for chunk, part in zip(chunks, parts):
            (self.directory / part["fileName"]).write_bytes(chunk)
        self.document = json.dumps(
            self.package, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.descriptor = self.directory / f"{archive_name}.update.json"
        self.descriptor.write_bytes(self.document)
        self.parts_manifest = self.directory / f"{archive_name}.parts.json"
        self.parts_manifest.write_text(
            json.dumps(_part_manifest(self.package), indent=2) + "\n", encoding="utf-8")
        (self.directory / "SHA256SUMS").write_text(
            "".join(
                f"{part['sha256']}  {part['fileName']}\n"
                for part in self.package["parts"]
            ),
            encoding="utf-8",
        )
        (self.directory / "README.zh-en.txt").write_text("package\n", encoding="utf-8")
        self.signature = self.directory / f"{archive_name}.update.json.sig"

    def sign(self, private_key):
        self.signature.write_bytes(cr_fixture._sign_bytes(self.document, private_key))
        return self


class FakeGitHub:
    """In-memory stand-in for the GitHub git-data and contents APIs."""

    def __init__(self, repository=REPOSITORY, existing=None, conflict_on=0):
        self.repository = repository
        self.blobs = {}
        self.trees = {}
        self.commits = {}
        self.tree_files = {}
        self.head = None
        self.ref_writes = []
        self.conflict_on = conflict_on
        self.patch_attempts = 0
        self.file_reads = []
        self._counter = 0
        if existing is not None:
            entries = {}
            for name, data in existing.items():
                sha = self._identifier("blob")
                self.blobs[sha] = data
                entries[name] = sha
            tree_sha = self._identifier("tree")
            self.tree_files[tree_sha] = dict(entries)
            self.trees[tree_sha] = {
                "sha": tree_sha,
                "tree": [
                    {"path": name, "sha": sha, "type": "blob"}
                    for name, sha in sorted(entries.items())
                ],
            }
            self.head = self._identifier("commit")
            self.commits[self.head] = {
                "sha": self.head, "tree": {"sha": tree_sha}, "parents": [],
            }

    def _identifier(self, prefix):
        self._counter += 1
        return f"{prefix}{self._counter:04d}"

    def branch_head(self, branch):
        if branch != "update-channels":
            raise AssertionError(branch)
        return self.head

    def get_json(self, path):
        identifier = path.rsplit("/", 1)[1]
        if "/git/commits/" in path:
            return self.commits[identifier]
        if "/git/trees/" in path:
            return self.trees[identifier]
        raise AssertionError(path)

    def get_blob(self, sha):
        return self.blobs[sha]

    def post_json(self, path, payload):
        if path.endswith("/git/blobs"):
            data = base64.b64decode(payload["content"])
            sha = self._identifier("blob")
            self.blobs[sha] = data
            return {"sha": sha}
        if path.endswith("/git/trees"):
            entries = dict(self.tree_files.get(payload.get("base_tree"), {}))
            for entry in payload["tree"]:
                entries[entry["path"]] = entry["sha"]
            sha = self._identifier("tree")
            self.tree_files[sha] = entries
            self.trees[sha] = {
                "sha": sha,
                "tree": [
                    {"path": name, "sha": blob, "type": "blob"}
                    for name, blob in sorted(entries.items())
                ],
            }
            return {"sha": sha}
        if path.endswith("/git/commits"):
            sha = self._identifier("commit")
            # The real API returns a commit whose tree and parents are objects,
            # not the bare digests accepted by the create call.
            self.commits[sha] = {
                "sha": sha,
                "message": payload.get("message"),
                "tree": {"sha": payload["tree"]},
                "parents": [{"sha": parent} for parent in payload.get("parents", [])],
            }
            return {"sha": sha}
        if path.endswith("/git/refs"):
            self.ref_writes.append(("create", payload))
            self.head = payload["sha"]
            return {"ref": payload["ref"], "object": {"sha": payload["sha"]}}
        raise AssertionError(path)

    def patch_json(self, path, payload):
        self.patch_attempts += 1
        self.ref_writes.append(("update", payload))
        if self.patch_attempts == self.conflict_on:
            raise publication.GitHubConflict("the branch moved")
        self.head = payload["sha"]
        return {"ref": "refs/heads/update-channels", "object": {"sha": payload["sha"]}}

    def tree_of(self, commit_sha):
        tree_sha = self.commits[commit_sha]["tree"]["sha"]
        return {name: self.blobs[blob] for name, blob in self.tree_files[tree_sha].items()}


class FakeChannelSource:
    """Stand-in for the GitHub reads used by channel fetch and asset hashing."""

    def __init__(self, files, downloads, head="commit-1"):
        self.files = dict(files)
        self.downloads = dict(downloads)
        self.head = head
        self.head_reads = 0
        self.file_reads = []

    def branch_head(self, branch):
        self.head_reads += 1
        return self.head

    def get_file(self, path, ref):
        self.file_reads.append((path, ref))
        try:
            return self.files[path]
        except KeyError:
            raise publication.PublicationError(f"{path} does not exist on {ref}") from None

    def get_url_bytes(self, url, limit, label):
        try:
            data = self.downloads[url]
        except KeyError:
            raise publication.PublicationError(f"{label} is unavailable") from None
        if len(data) > limit:
            raise publication.PublicationError(f"{label} exceeds {limit} bytes")
        return data


class FakeReleaseAssets:
    """Stand-in for the streaming download used by verify_release_assets."""

    def __init__(self, contents):
        self.contents = dict(contents)
        self.streamed = []

    def stream_sha256(self, url, *, expected_bytes, label):
        self.streamed.append(label)
        data = self.contents[url]
        if len(data) != expected_bytes:
            raise publication.PublicationError(
                f"{label} is {len(data)} bytes, not the verified {expected_bytes}")
        return sha256(data).hexdigest()


class PublicationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        keydir = Path(cls._tmpdir.name)
        cls.priv, cls.pub = cr_fixture._generate_keypair(keydir, "trusted")
        cls.other_priv, cls.other_pub = cr_fixture._generate_keypair(keydir, "other")

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def artifact(self, upgrade=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return PackageArtifact(tmp.name, upgrade=upgrade).sign(self.priv)

    def test_shipped_channels_config_is_what_the_service_reads(self):
        path = ROOT / "system_files/etc/armada/updates/channels.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(config), {"schemaVersion", "channels"})
        self.assertEqual(config["schemaVersion"], 1)
        self.assertEqual(set(config["channels"]), {"stable", "beta", "testing"})
        for channel in ("stable", "beta", "testing"):
            selected = system_manager._configuration(path, channel)
            base = (
                "https://raw.githubusercontent.com/"
                f"{REPOSITORY}/update-channels/{channel}.json"
            )
            self.assertEqual(selected["url"], base)
            self.assertEqual(selected["signatureUrl"], base + ".sig")

    def test_verified_package_plan_lists_only_release_assets(self):
        artifact = self.artifact()
        plan = publication.verify_package_directory(
            artifact.root, trusted_key=self.pub)
        names = [asset["name"] for asset in plan["assets"]]
        self.assertEqual(
            names,
            [
                *(part["fileName"] for part in artifact.package["parts"]),
                f"{artifact.archive_name}.parts.json",
                f"{artifact.archive_name}.update.json",
                f"{artifact.archive_name}.update.json.sig",
            ],
        )
        self.assertNotIn("SHA256SUMS", names)
        self.assertFalse(any(name.startswith("README") for name in names))
        self.assertEqual(plan["releaseTag"], artifact.package["releaseId"])
        self.assertEqual(plan["ociDigest"], artifact.package["ociDigest"])
        self.assertEqual(plan["sourceRevision"], artifact.package["sourceRevision"])
        self.assertEqual(plan["totalBytes"], sum(asset["bytes"] for asset in plan["assets"]))
        descriptor_asset = next(
            asset for asset in plan["assets"] if asset["name"].endswith(".update.json"))
        self.assertEqual(descriptor_asset["sha256"], sha256(artifact.document).hexdigest())
        urls = publication.descriptor_urls(
            REPOSITORY, plan["releaseTag"], artifact.archive_name)
        self.assertEqual(
            urls["descriptorUrl"],
            _release_url(plan["releaseTag"], f"{artifact.archive_name}.update.json"),
        )
        self.assertEqual(
            urls["signatureUrl"],
            _release_url(plan["releaseTag"], f"{artifact.archive_name}.update.json.sig"),
        )

    def test_verify_package_rejects_tampered_or_incomplete_artifacts(self):
        def check(mutator, message):
            artifact = self.artifact()
            mutator(artifact)
            with self.assertRaisesRegex(publication.PublicationError, message):
                publication.verify_package_directory(artifact.root, trusted_key=self.pub)

        def flip_part(artifact):
            part = artifact.directory / artifact.package["parts"][0]["fileName"]
            part.write_bytes(part.read_bytes() + b"x")

        def drop_checksum(artifact):
            (artifact.directory / "SHA256SUMS").write_text("", encoding="utf-8")

        def rewrite_parts_manifest(artifact):
            manifest = _part_manifest(artifact.package)
            manifest["parts"] = deepcopy(manifest["parts"])[:-1]
            artifact.parts_manifest.write_text(json.dumps(manifest), encoding="utf-8")

        def add_stray_file(artifact):
            (artifact.directory / "notes.txt").write_text("extra", encoding="utf-8")

        def remove_descriptor(artifact):
            artifact.descriptor.unlink()

        check(flip_part, "parts verification failed")
        check(drop_checksum, "SHA256SUMS")
        check(rewrite_parts_manifest, "parts manifest parts")
        check(add_stray_file, "must not become release assets")
        check(remove_descriptor, "expected exactly one")

        artifact = self.artifact(upgrade=False)
        with self.assertRaisesRegex(publication.PublicationError, "offline-upgrade"):
            publication.verify_package_directory(artifact.root, trusted_key=self.pub)

        artifact = self.artifact()
        artifact.signature.write_bytes(
            cr_fixture._sign_bytes(artifact.document, self.other_priv))
        with self.assertRaisesRegex(publication.PublicationError, "verification failed"):
            publication.verify_package_directory(artifact.root, trusted_key=self.pub)

        artifact = self.artifact()
        with self.assertRaisesRegex(publication.PublicationError, "verification failed"):
            publication.verify_package_directory(artifact.root, trusted_key=self.other_pub)

    def test_release_tag_safety(self):
        self.assertEqual(publication.release_tag("2026.04.19-rc1"), "2026.04.19-rc1")
        for value in (
            "", "testing", "beta", "stable", "latest", "1..0", "1.0.",
            "1.0.lock", "-1.0", "1.0/2", "a" * 129, None, 5,
        ):
            with self.assertRaises(publication.PublicationError, msg=repr(value)):
                publication.release_tag(value)

    def _plan_assets(self):
        return [
            {"name": "pkg.zip.001", "bytes": 10, "sha256": "a" * 64},
            {"name": "pkg.zip.002", "bytes": 5, "sha256": "b" * 64},
        ]

    def _release(self, assets, *, present=True, draft=True):
        return {"present": present, "isDraft": draft, "assets": assets}

    def test_release_state_distinguishes_missing_empty_draft_and_conflict(self):
        assets = self._plan_assets()
        verified = {"pkg.zip.001": "a" * 64, "pkg.zip.002": "b" * 64}

        # No release at all is the only case that may create one.
        absent = publication.release_state(assets, {"present": False})
        self.assertEqual(absent["state"], "absent")
        self.assertEqual(absent["missing"], ["pkg.zip.001", "pkg.zip.002"])

        # An existing draft with no assets is resumable, not absent.
        empty = publication.release_state(assets, self._release([]))
        self.assertEqual(empty["state"], "incomplete")
        self.assertEqual(empty["missing"], ["pkg.zip.001", "pkg.zip.002"])

        # A partial draft resumes only the missing assets.
        partial = publication.release_state(
            assets,
            self._release([{"name": "pkg.zip.001", "size": 10}]),
            hashes={"pkg.zip.001": "a" * 64},
        )
        self.assertEqual(partial["state"], "incomplete")
        self.assertEqual(partial["missing"], ["pkg.zip.002"])

        # Same size, different bytes is never matching.
        tampered = publication.release_state(
            assets,
            self._release([{"name": "pkg.zip.001", "size": 10}]),
            hashes={"pkg.zip.001": "c" * 64},
        )
        self.assertEqual(tampered["state"], "conflict")

        wrong_size = publication.release_state(
            assets,
            self._release([{"name": "pkg.zip.001", "size": 11}]),
            hashes={"pkg.zip.001": "a" * 64},
        )
        self.assertEqual(wrong_size["state"], "conflict")

        # A public release is never extended.
        public_partial = publication.release_state(
            assets,
            self._release([{"name": "pkg.zip.001", "size": 10}], draft=False),
            hashes={"pkg.zip.001": "a" * 64},
        )
        self.assertEqual(public_partial["state"], "conflict")

        complete = [
            {"name": "pkg.zip.001", "size": 10},
            {"name": "pkg.zip.002", "size": 5},
        ]
        matching = publication.release_state(
            assets, self._release(complete, draft=False), hashes=verified)
        self.assertEqual(matching["state"], "matching")

        # Present and same size but unhashed is not "matching".
        unverified = publication.release_state(
            assets, self._release(complete, draft=False))
        self.assertEqual(unverified["state"], "unverified")
        self.assertEqual(
            unverified["unverified"], ["pkg.zip.001", "pkg.zip.002"])

        extra = publication.release_state(
            assets,
            self._release(complete + [{"name": "extra.bin", "size": 1}], draft=False),
            hashes=verified,
        )
        self.assertEqual(extra["state"], "conflict")
        self.assertEqual(extra["unexpected"], ["extra.bin"])

    def test_remote_assets_use_the_api_digest_or_bounded_streaming(self):
        first = b"P" * 10
        second = b"Q" * 5
        assets = [
            {"name": "pkg.zip.001", "bytes": len(first),
             "sha256": sha256(first).hexdigest()},
            {"name": "pkg.zip.002", "bytes": len(second),
             "sha256": sha256(second).hexdigest()},
        ]
        plan = {"assets": assets}
        release = self._release([
            {"name": "pkg.zip.001", "size": len(first),
             "digest": "sha256:" + sha256(first).hexdigest(),
             "url": "https://api.github.com/repos/x/releases/assets/1"},
            {"name": "pkg.zip.002", "size": len(second), "digest": None,
             "browser_download_url": "https://github.com/x/releases/download/t/pkg.zip.002"},
        ])
        source = FakeReleaseAssets(
            {"https://github.com/x/releases/download/t/pkg.zip.002": second})
        hashes = publication.verify_release_assets(source, plan, release)
        self.assertEqual(hashes["pkg.zip.001"], sha256(first).hexdigest())
        self.assertEqual(hashes["pkg.zip.002"], sha256(second).hexdigest())
        # The asset with an API digest is not downloaded again.
        self.assertEqual(source.streamed, ["pkg.zip.002"])

        # A same-size replacement is detected by streaming the bytes.
        changed = FakeReleaseAssets(
            {"https://github.com/x/releases/download/t/pkg.zip.002": b"R" * 5})
        changed_hashes = publication.verify_release_assets(
            changed, plan,
            self._release([
                {"name": "pkg.zip.002", "size": len(second), "digest": None,
                 "browser_download_url":
                     "https://github.com/x/releases/download/t/pkg.zip.002"},
            ]),
        )
        self.assertEqual(
            publication.release_state(
                assets,
                self._release([{"name": "pkg.zip.002", "size": 5}]),
                hashes=changed_hashes,
            )["state"],
            "conflict",
        )

        oversized = FakeReleaseAssets(
            {"https://github.com/x/releases/download/t/pkg.zip.002": b"R" * 6})
        with self.assertRaises(publication.PublicationError):
            publication.verify_release_assets(
                oversized, plan,
                self._release([
                    {"name": "pkg.zip.002", "size": 5, "digest": None,
                     "browser_download_url":
                         "https://github.com/x/releases/download/t/pkg.zip.002"},
                ]),
            )

    def test_run_identity_requires_a_successful_expected_build(self):
        run = {
            "id": 7,
            "status": "completed",
            "conclusion": "success",
            "event": "push",
            "head_sha": "a" * 40,
            "head_branch": "main",
            "repository": {"full_name": REPOSITORY},
            "head_repository": {"full_name": REPOSITORY},
            "path": ".github/workflows/build.yml@refs/heads/main",
        }
        artifacts = {"artifacts": [
            {"name": "edl-package-7", "expired": False, "size_in_bytes": 4096}]}
        verified = publication.verify_package_run(
            run, artifacts, run_id=7, repository=REPOSITORY)
        self.assertEqual(verified["artifact"], "edl-package-7")
        self.assertEqual(verified["workflow"], ".github/workflows/build.yml")

        mutations = [
            ("conclusion", "failure"),
            ("status", "in_progress"),
            ("path", ".github/workflows/pr.yml@refs/heads/main"),
            ("repository", {"full_name": "Someone/else"}),
            ("head_repository", {"full_name": "Someone/else"}),
        ]
        for field, value in mutations:
            mutated = deepcopy(run)
            mutated[field] = value
            with self.assertRaises(publication.PublicationError, msg=field):
                publication.verify_package_run(
                    mutated, artifacts, run_id=7, repository=REPOSITORY)
        with self.assertRaises(publication.PublicationError):
            publication.verify_package_run(
                run, artifacts, run_id=8, repository=REPOSITORY)
        with self.assertRaises(publication.PublicationError):
            publication.verify_package_run(
                run, {"artifacts": []}, run_id=7, repository=REPOSITORY)
        with self.assertRaises(publication.PublicationError):
            publication.verify_package_run(
                run,
                {"artifacts": [
                    {"name": "edl-package-7", "expired": True, "size_in_bytes": 1}]},
                run_id=7, repository=REPOSITORY)
        with self.assertRaises(publication.PublicationError):
            publication.verify_package_run(
                run, {"artifacts": [
                    {"name": "edl-package-7", "expired": False,
                     "size_in_bytes": 4096}]},
                run_id=7, repository="Someone/else")

        plan = {"sourceRevision": "a" * 40}
        publication.require_run_identity(verified, plan)
        with self.assertRaises(publication.PublicationError):
            publication.require_run_identity(verified, {"sourceRevision": "b" * 40})
        dispatch = dict(verified, workflow=".github/workflows/build-edl-package.yml")
        publication.require_run_identity(dispatch, {"sourceRevision": "b" * 40})

    def test_commit_channel_files_creates_one_commit_for_both_files(self):
        github = FakeGitHub()
        files = {"testing.json": b"{}\n", "testing.json.sig": b"c2ln\n"}
        result = publication.commit_channel_files(github, files, "Publish testing")
        self.assertEqual(result["state"], "committed")
        self.assertEqual([entry[0] for entry in github.ref_writes], ["create"])
        self.assertEqual(github.tree_of(result["commit"]), files)
        self.assertEqual(github.commits[result["commit"]]["parents"], [])

        with self.assertRaises(publication.PublicationError):
            publication.commit_channel_files(github, {"../escape.json": b"x"}, "msg")
        with self.assertRaises(publication.PublicationError):
            publication.commit_channel_files(github, {"notes.txt": b"x"}, "msg")
        with self.assertRaises(publication.PublicationError):
            publication.commit_channel_files(github, {}, "msg")

    def test_commit_channel_files_never_forces_and_retries_conflicts(self):
        github = FakeGitHub(existing={"stable.json": b"old\n"}, conflict_on=1)
        files = {"stable.json": b"new\n", "stable.json.sig": b"c2ln\n"}
        result = publication.commit_channel_files(github, files, "Publish stable")
        self.assertEqual(result["state"], "committed")
        self.assertEqual(github.patch_attempts, 2)
        updates = [payload for action, payload in github.ref_writes if action == "update"]
        self.assertTrue(updates)
        for payload in updates:
            self.assertIs(payload["force"], False)
        self.assertEqual(github.tree_of(result["commit"]), files)

        unchanged = FakeGitHub(existing=files)
        again = publication.commit_channel_files(unchanged, files, "Publish stable")
        self.assertEqual(again["state"], "unchanged")
        self.assertEqual(unchanged.ref_writes, [])

    def _signed_channel(self, artifact, channel="testing"):
        descriptor_url = _release_url(
            artifact.package["releaseId"],
            f"{artifact.archive_name}.update.json",
        )
        signature_url = descriptor_url + ".sig"
        document = make_channel(
            artifact.document,
            artifact.signature.read_bytes(),
            channel=channel,
            descriptor_url=descriptor_url,
            signature_url=signature_url,
            published_at=PUBLISHED_AT,
            expected_oci_digest=artifact.package["ociDigest"],
            trusted_key=self.pub,
        )
        channel_bytes = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
        channel_signature = cr_fixture._sign_bytes(channel_bytes, self.priv)
        return channel_bytes, channel_signature, descriptor_url, signature_url

    def _published_channel(self, artifact, channel="testing"):
        channel_bytes, channel_signature, descriptor_url, signature_url = (
            self._signed_channel(artifact, channel))
        source = FakeChannelSource(
            {
                f"{channel}.json": channel_bytes,
                f"{channel}.json.sig": channel_signature,
            },
            {
                descriptor_url: artifact.document,
                signature_url: artifact.signature.read_bytes(),
            },
        )
        return source, descriptor_url, signature_url

    def test_fetch_channel_binds_signed_descriptor_and_identity(self):
        artifact = self.artifact()
        source, descriptor_url, signature_url = self._published_channel(artifact)
        workdir = Path(artifact.root) / "promote"
        result = publication.fetch_published_channel(
            source,
            channel="testing",
            trusted_key=self.pub,
            expected_digest=artifact.package["ociDigest"],
            expected_revision=artifact.package["sourceRevision"],
            workdir=workdir,
        )
        self.assertEqual(result["releaseId"], artifact.package["releaseId"])
        self.assertEqual(result["descriptorUrl"], descriptor_url)
        self.assertEqual(result["signatureUrl"], signature_url)
        self.assertEqual(Path(result["descriptorPath"]).read_bytes(), artifact.document)
        self.assertEqual(
            Path(result["signaturePath"]).read_bytes(),
            artifact.signature.read_bytes(),
        )
        # The document and its signature were read from one immutable commit.
        self.assertEqual(source.head_reads, 1)
        self.assertEqual({ref for _, ref in source.file_reads}, {source.head})

        with self.assertRaisesRegex(publication.PublicationError, "resolved source digest"):
            publication.fetch_published_channel(
                source,
                channel="testing",
                trusted_key=self.pub,
                expected_digest="sha256:" + "0" * 64,
                expected_revision=artifact.package["sourceRevision"],
                workdir=workdir,
            )
        with self.assertRaisesRegex(publication.PublicationError, "revision"):
            publication.fetch_published_channel(
                source,
                channel="testing",
                trusted_key=self.pub,
                expected_digest=artifact.package["ociDigest"],
                expected_revision="b" * 40,
                workdir=workdir,
            )
        with self.assertRaisesRegex(publication.PublicationError, "does not exist"):
            publication.fetch_published_channel(
                source,
                channel="beta",
                trusted_key=self.pub,
                expected_digest=artifact.package["ociDigest"],
                expected_revision=artifact.package["sourceRevision"],
                workdir=workdir,
            )
        with self.assertRaises(publication.PublicationError):
            publication.fetch_published_channel(
                source,
                channel="testing",
                trusted_key=self.other_pub,
                expected_digest=artifact.package["ociDigest"],
                expected_revision=artifact.package["sourceRevision"],
                workdir=workdir,
            )

    def test_fetch_channel_requires_a_published_branch(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = FakeChannelSource({}, {}, head=None)
        with self.assertRaisesRegex(publication.PublicationError, "no published channel"):
            publication.fetch_published_channel(
                source,
                channel="testing",
                trusted_key=self.pub,
                expected_digest="sha256:" + "0" * 64,
                expected_revision="a" * 40,
                workdir=Path(tmp.name),
            )
        self.assertEqual(source.head_reads, 1)
        self.assertEqual(source.file_reads, [])

    def test_commit_channel_requires_a_verified_signature(self):
        artifact = self.artifact()
        good, good_signature, _, _ = self._signed_channel(artifact)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        document = directory / "testing.json"
        signature = directory / "testing.json.sig"

        github = FakeGitHub()

        def run(document_bytes, signature_bytes):
            document.write_bytes(document_bytes)
            signature.write_bytes(signature_bytes)
            return publication.command_commit_channel(
                type("Args", (), {
                    "channel": "testing",
                    "channel_file": document,
                    "channel_signature": signature,
                    "repository": REPOSITORY,
                    "branch": "update-channels",
                    "trusted_key": self.pub,
                    "out": directory / "committed.json",
                })()
            )

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "fixture"}):
            with mock.patch.object(
                    publication, "GitHub", lambda repository, token: github):
                with self.assertRaisesRegex(
                        publication.PublicationError, "verification failed"):
                    run(b'{"not": "a channel"}\n', b"c2ln\n")
                with self.assertRaisesRegex(
                        publication.PublicationError, "verification failed"):
                    run(good, cr_fixture._sign_bytes(good, self.other_priv))
                self.assertEqual(run(good, good_signature), 0)

        self.assertEqual([entry[0] for entry in github.ref_writes], ["create"])


class WorkflowConsistencyTest(unittest.TestCase):
    def test_publication_workflow_uses_the_package_artifact(self):
        text = (ROOT / ".github/workflows/publish-update-channel.yml").read_text(
            encoding="utf-8")
        self.assertIn("edl-package-", text)
        self.assertIn("gh run download", text)
        self.assertIn(".github/scripts/update-publication.py", text)
        self.assertIn(".github/scripts/publish-signed-channel.sh", text)
        self.assertIn("--draft=false", text)
        self.assertIn("update-channels", text)
        self.assertNotIn("force: true", text)
        self.assertNotIn("--force", text)

    def test_promotion_verifies_the_channel_before_tagging(self):
        text = (ROOT / ".github/workflows/promote.yml").read_text(encoding="utf-8")
        self.assertIn("fetch-channel", text)
        self.assertLess(text.index("fetch-channel"), text.index("Publish release tags"))
        self.assertIn(".github/scripts/publish-signed-channel.sh", text)
        self.assertIn("update-channels", text)
        self.assertIn("target", text)

    def test_publication_and_promotion_share_one_concurrency_group(self):
        for name in ("publish-update-channel.yml", "promote.yml"):
            text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
            self.assertIn("\n  group: update-channels\n", text, name)
            self.assertIn("\n  cancel-in-progress: false\n", text, name)

    def test_signing_secrets_are_checked_before_any_mutation(self):
        publish = (ROOT / ".github/workflows/publish-update-channel.yml").read_text(
            encoding="utf-8")
        self.assertLess(
            publish.index("Check signing secrets"),
            publish.index("Reconcile the package release"),
            "the packaging release must not be created before the secrets exist",
        )
        promote = (ROOT / ".github/workflows/promote.yml").read_text(encoding="utf-8")
        self.assertLess(
            promote.index("Check signing secrets"),
            promote.index("Publish release tags"),
            "no tag may be written before the signing secrets are checked",
        )

    def test_draft_release_resume_uses_verified_assets_only(self):
        text = (ROOT / ".github/workflows/publish-update-channel.yml").read_text(
            encoding="utf-8")
        self.assertIn("release-view", text)
        self.assertIn("--verify-remote", text)
        self.assertIn("gh release upload", text)
        # Assets are enumerated through the API and hashed from the signed
        # plan; they are never re-downloaded by pattern or overwritten.
        self.assertNotIn("gh release download", text)
        self.assertNotIn("--clobber", text)
        self.assertLess(
            text.index("Reconcile the package release"),
            text.index("Verify the published release"),
            "the public release must be re-verified before the channel moves",
        )

    def test_no_shell_interpolation_of_workflow_expressions(self):
        for name in ("promote.yml", "publish-update-channel.yml"):
            text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                if "${{" not in line:
                    continue
                stripped = line.strip()
                if stripped.startswith("- name:"):
                    # Display name of a step or job, never a shell command.
                    continue
                key, sep, _value = stripped.partition(":")
                self.assertTrue(
                    sep and key and " " not in key and stripped != key,
                    f"{name}:{number} interpolates an expression in a command: {line}",
                )

    def test_publication_helpers_never_force_push(self):
        for name in ("update-publication.py", "publish-signed-channel.sh"):
            text = (ROOT / ".github/scripts" / name).read_text(encoding="utf-8")
            self.assertNotIn("--force", text)
            self.assertNotIn("force\": true", text)




class RedirectAuthenticationTests(unittest.TestCase):
    def test_same_origin_retains_auth_cross_origin_and_downgrade_remove_it(self):
        source = "https://api.github.com/repos/example/assets/1"
        handler = publication.AuthenticatedRedirectHandler()
        for target, expected in (
            ("https://api.github.com/redirected", "Bearer synthetic"),
            ("https://api.github.com:443/redirected", "Bearer synthetic"),
            ("https://objects.githubusercontent.com/asset", None),
            ("https://example.invalid/asset", None),
            ("http://api.github.com/asset", None),
            ("https://api.github.com:444/asset", None),
        ):
            with self.subTest(target=target):
                request = publication.urllib.request.Request(source)
                request.add_header("Authorization", "Bearer synthetic")
                redirected = handler.redirect_request(request, None, 302, "Found", {}, target)
                self.assertEqual(redirected.get_header("Authorization"), expected)
                self.assertEqual(request.get_header("Authorization"), "Bearer synthetic")

    def test_redirect_chain_does_not_restore_dropped_auth(self):
        handler = publication.AuthenticatedRedirectHandler()
        request = publication.urllib.request.Request("https://api.github.com/assets/1")
        request.add_header("Authorization", "Bearer synthetic")
        first = handler.redirect_request(request, None, 302, "Found", {}, "https://objects.githubusercontent.com/asset")
        second = handler.redirect_request(first, None, 302, "Found", {}, "https://api.github.com/another")
        self.assertIsNone(second.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()

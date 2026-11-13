"""Offline tests for signed update-channel publication.

The publisher writes immutable release assets and then a signed channel branch;
these tests use throwaway keys and local fixtures, and never touch the network.
"""

import base64
import importlib.util
import json
import os
import shlex
import sys
import tempfile
import unittest
import urllib.parse
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
from updates.package_contract import load_package, validate_package  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "update_publication", ROOT / ".github/scripts/update-publication.py")
publication = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(publication)

REPOSITORY = "ArmadaOS-Freedom/armadaos-freedom"
PUBLISHED_AT = "2026-04-19T12:00:00Z"
RELEASE_TAG = "2026.04.19-1"


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


class ReleaseListingAPI:
    """Stand-in for the GitHub transport that serves paginated release listings.

    It serves only the list endpoint.  The published-only
    ``releases/tags/{tag}`` lookup that cannot see drafts is rejected, so a
    regression to that implementation fails instead of quietly passing.
    """

    def __init__(self, pages):
        self.pages = dict(pages)
        self.requests = []

    def __call__(self, method, url, payload=None, authenticated=True):
        self.requests.append((method, url))
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        if (method != "GET"
                or not parsed.path.endswith(f"/repos/{REPOSITORY}/releases")
                or query.get("per_page") != ["100"]):
            raise AssertionError(url)
        page = int(query["page"][0])
        return self.pages.get(page, _listing_page())


def _listing_page(*releases):
    """A 200 response body holding the releases on one listing page."""
    return 200, json.dumps(list(releases)).encode("utf-8")


def _release_entry(tag, *, release_id=4242, draft=True, assets=()):
    """One release as the list endpoint reports it."""
    return {
        "id": release_id,
        "tag_name": tag,
        "draft": draft,
        "target_commitish": "a" * 40,
        "assets": list(assets),
    }


def _json_response(payload, status=200):
    return status, json.dumps(payload).encode("utf-8")


class RefTransport:
    """Stand-in for the GitHub transport serving git refs and tag objects.

    Unknown paths are the API's genuine 404, so a test can return an auth or
    server status for a known path and prove it is not read as a missing tag.
    """

    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.requests = []

    def __call__(self, method, url, payload=None,
                 accept="application/vnd.github+json", authenticated=True):
        self.requests.append((method, url))
        path = urllib.parse.urlsplit(url).path
        return self.responses.get(path, (404, b'{"message": "Not Found"}'))


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

    def test_signed_install_only_artifact_is_not_publishable(self):
        # The install variant signs the same descriptor class as the release
        # variant, but its operations stay install-only. Publication must
        # reject it even though the signature itself verifies.
        artifact = self.artifact(upgrade=False)
        package = load_package(
            artifact.document,
            artifact.signature.read_bytes(),
            trusted_key=self.pub,
        )
        self.assertEqual(package["operations"], ["install"])
        with self.assertRaisesRegex(publication.PublicationError, "offline-upgrade"):
            publication.verify_package_directory(
                artifact.root, trusted_key=self.pub)

    def test_release_tag_safety(self):
        self.assertEqual(publication.release_tag("2026.04.19-rc1"), "2026.04.19-rc1")
        for value in (
            "", "testing", "beta", "stable", "latest", "1..0", "1.0.",
            "1.0.lock", "-1.0", "1.0/2", "a" * 129, None, 5,
        ):
            with self.assertRaises(publication.PublicationError, msg=repr(value)):
                publication.release_tag(value)

    def test_promotion_version_is_a_docker_tag_or_empty(self):
        for value in ("", "1.0", "2026.04.19-1", "V1_0", "_internal", "x" * 128):
            self.assertEqual(publication.promotion_tag(value), value, repr(value))
        for value in (
            "testing", "beta", "stable", "latest",
            "1.0/2", "-1.0", ".1", "1 0", "1.0\n", "a" * 129, "1.0:2",
            None, 5,
        ):
            with self.assertRaises(publication.PublicationError, msg=repr(value)):
                publication.promotion_tag(value)

    def _plan_assets(self):
        return [
            {"name": "pkg.zip.001", "bytes": 10, "sha256": "a" * 64},
            {"name": "pkg.zip.002", "bytes": 5, "sha256": "b" * 64},
        ]

    def _release(self, assets, *, present=True, draft=True):
        return {"present": present, "isDraft": draft, "assets": assets}

    def _view(self, pages, *, tag=RELEASE_TAG):
        """Run release-view itself against a fake paginated listing transport."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "release.json"
        api = ReleaseListingAPI(pages)
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "fixture"}):
            with mock.patch.object(publication.GitHub, "_request", api):
                code = publication.command_release_view(type("Args", (), {
                    "repository": REPOSITORY,
                    "tag": tag,
                    "out": out,
                })())
        self.assertEqual(code, 0)
        return json.loads(out.read_text(encoding="utf-8")), api

    def _view_failure(self, pages, *, tag=RELEASE_TAG):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "release.json"
        api = ReleaseListingAPI(pages)
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "fixture"}):
            with mock.patch.object(publication.GitHub, "_request", api):
                with self.assertRaises(publication.PublicationError):
                    publication.command_release_view(type("Args", (), {
                        "repository": REPOSITORY,
                        "tag": tag,
                        "out": out,
                    })())
        self.assertFalse(out.exists(), "no release metadata may be written")
        return api

    def _fillers(self, count=100):
        return [
            {"tag_name": f"unrelated-{index:03d}", "id": index}
            for index in range(count)
        ]

    def test_release_view_finds_an_empty_draft_and_resumes_it(self):
        viewed, api = self._view({1: _listing_page(_release_entry(RELEASE_TAG))})
        self.assertEqual(viewed, {
            "present": True,
            "tag": RELEASE_TAG,
            "releaseId": 4242,
            "isDraft": True,
            "targetCommitish": "a" * 40,
            "assets": [],
        })
        self.assertEqual(api.requests, [(
            "GET",
            "https://api.github.com/repos/"
            f"{REPOSITORY}/releases?per_page=100&page=1",
        )])
        # The empty draft stays resumable: no second draft is created.
        state = publication.release_state(self._plan_assets(), viewed)
        self.assertEqual(state["state"], "incomplete")
        self.assertEqual(state["missing"], ["pkg.zip.001", "pkg.zip.002"])

    def test_release_view_keeps_partial_draft_assets(self):
        uploaded = {
            "name": "pkg.zip.001",
            "size": 10,
            "digest": "sha256:" + "a" * 64,
            "state": "uploaded",
            "url": "https://api.github.com/repos/x/releases/assets/1",
            "browser_download_url":
                "https://github.com/x/releases/download/tag/pkg.zip.001",
        }
        viewed, _ = self._view({1: _listing_page(
            _release_entry(RELEASE_TAG, assets=[uploaded]))})
        self.assertEqual(viewed["assets"], [uploaded])
        state = publication.release_state(
            self._plan_assets(), viewed, hashes={"pkg.zip.001": "a" * 64})
        self.assertEqual(state, {"state": "incomplete", "missing": ["pkg.zip.002"]})

    def test_release_view_reports_a_published_release(self):
        published = [
            {"name": "pkg.zip.001", "size": 10, "digest": "sha256:" + "a" * 64},
            {"name": "pkg.zip.002", "size": 5, "digest": "sha256:" + "b" * 64},
        ]
        viewed, _ = self._view({1: _listing_page(
            _release_entry(RELEASE_TAG, draft=False, assets=published))})
        self.assertTrue(viewed["present"])
        self.assertFalse(viewed["isDraft"])
        self.assertEqual(viewed["releaseId"], 4242)
        state = publication.release_state(
            self._plan_assets(), viewed,
            hashes={"pkg.zip.001": "a" * 64, "pkg.zip.002": "b" * 64},
        )
        self.assertEqual(state["state"], "matching")

    def test_release_view_reads_every_page_until_a_short_page(self):
        viewed, api = self._view({
            1: _listing_page(*self._fillers()),
            2: _listing_page(_release_entry(RELEASE_TAG)),
        })
        self.assertTrue(viewed["present"])
        self.assertEqual(viewed["releaseId"], 4242)
        self.assertEqual([url for _, url in api.requests], [
            "https://api.github.com/repos/"
            f"{REPOSITORY}/releases?per_page=100&page=1",
            "https://api.github.com/repos/"
            f"{REPOSITORY}/releases?per_page=100&page=2",
        ])

    def test_release_view_rejects_duplicate_tags_on_one_page(self):
        self._view_failure({1: _listing_page(
            _release_entry(RELEASE_TAG, release_id=1),
            _release_entry(RELEASE_TAG, release_id=2),
        )})

    def test_release_view_rejects_duplicate_tags_across_pages(self):
        # The matching tag is already found on the first page, but a second
        # release with it on a later page makes the choice ambiguous.
        self._view_failure({
            1: _listing_page(
                *self._fillers(99), _release_entry(RELEASE_TAG, release_id=1)),
            2: _listing_page(
                _release_entry(RELEASE_TAG, release_id=2, draft=False)),
        })

    def test_release_view_reports_absent_only_after_the_last_page(self):
        viewed, api = self._view({
            1: _listing_page(*self._fillers()),
            2: _listing_page({"tag_name": "unrelated-999", "id": 999}),
        })
        self.assertEqual(viewed, {"present": False, "tag": RELEASE_TAG})
        self.assertEqual(len(api.requests), 2)

    def test_release_view_rejects_malformed_listings(self):
        for body in (
            json.dumps({"releases": []}).encode("utf-8"),
            json.dumps([RELEASE_TAG]).encode("utf-8"),
            b"not json",
        ):
            with self.subTest(body=body):
                self._view_failure({1: (200, body)})

    def test_release_view_propagates_api_errors_including_404(self):
        for status in (404, 403, 500):
            with self.subTest(status=status):
                api = self._view_failure({1: (status, b'{"message": "denied"}')})
                self.assertTrue(api.requests)
        # A failure after a page of unrelated releases is still an error, not
        # a release that does not exist.
        api = self._view_failure({
            1: _listing_page(*self._fillers()),
            2: (502, b""),
        })
        self.assertEqual(len(api.requests), 2)

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

    def test_remote_assets_prefer_the_api_url_over_the_browser_url(self):
        data = b"P" * 10
        plan = {"assets": [
            {"name": "pkg.zip.001", "bytes": len(data),
             "sha256": sha256(data).hexdigest()},
        ]}
        api_url = "https://api.github.com/repos/x/releases/assets/1"
        web_url = "https://github.com/x/releases/download/t/pkg.zip.001"
        asset = {
            "name": "pkg.zip.001", "size": len(data), "digest": None,
            "url": api_url, "browser_download_url": web_url,
        }

        # Only the API URL resolves here: preferring the web URL would fail.
        source = FakeReleaseAssets({api_url: data})
        hashes = publication.verify_release_assets(
            source, plan, self._release([asset]))
        self.assertEqual(hashes["pkg.zip.001"], sha256(data).hexdigest())
        self.assertEqual(source.streamed, ["pkg.zip.001"])

        # A browser-only asset is still downloadable.
        browser_only = FakeReleaseAssets({web_url: data})
        hashes = publication.verify_release_assets(
            browser_only, plan,
            self._release([{"name": "pkg.zip.001", "size": len(data),
                            "browser_download_url": web_url}]))
        self.assertEqual(hashes["pkg.zip.001"], sha256(data).hexdigest())
        self.assertEqual(browser_only.streamed, ["pkg.zip.001"])

        # A present API digest is used as-is, even when both URLs are listed.
        digested = FakeReleaseAssets({})
        hashes = publication.verify_release_assets(
            digested, plan,
            self._release([
                dict(asset, digest="sha256:" + sha256(data).hexdigest())]))
        self.assertEqual(hashes["pkg.zip.001"], sha256(data).hexdigest())
        self.assertEqual(digested.streamed, [])

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
            {"name": "edl-package-7", "expired": False, "size_in_bytes": 4096},
            {"name": "edl-install-package-7", "expired": False,
             "size_in_bytes": 8192},
        ]}
        verified = publication.verify_package_run(
            run, artifacts, run_id=7, repository=REPOSITORY)
        self.assertEqual(verified["artifact"], "edl-package-7")
        self.assertEqual(verified["workflow"], ".github/workflows/build.yml")

        # The install-only artifact never substitutes for the release package.
        with self.assertRaises(publication.PublicationError):
            publication.verify_package_run(
                run,
                {"artifacts": [
                    {"name": "edl-install-package-7", "expired": False,
                     "size_in_bytes": 8192}]},
                run_id=7, repository=REPOSITORY)

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

    def _signed_channel(self, artifact, channel="testing", *, withdrawn=False):
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
            withdrawn=withdrawn,
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

    def test_fetch_channel_returns_the_signed_digest_without_a_tag_reference(self):
        # Promotion of published A must still work after a later build B
        # re-points the source tag: the signed channel, not the tag, selects
        # the digest. No expected value is supplied here, exactly as promotion
        # calls it, so nothing from a moving tag can enter the decision.
        artifact = self.artifact()
        source, descriptor_url, signature_url = self._published_channel(artifact)
        workdir = Path(artifact.root) / "promote-signed"
        result = publication.fetch_published_channel(
            source,
            channel="testing",
            trusted_key=self.pub,
            workdir=workdir,
        )
        self.assertEqual(result["ociDigest"], artifact.package["ociDigest"])
        self.assertEqual(result["sourceRevision"], artifact.package["sourceRevision"])
        self.assertEqual(result["descriptorUrl"], descriptor_url)
        self.assertEqual(result["signatureUrl"], signature_url)

        # An explicitly supplied tag-derived digest is still checked, which is
        # why promotion must not pass one: here B's digest rejects signed A.
        with self.assertRaisesRegex(publication.PublicationError, "resolved source digest"):
            publication.fetch_published_channel(
                source,
                channel="testing",
                trusted_key=self.pub,
                expected_digest="sha256:" + "1" * 64,
                workdir=Path(artifact.root) / "promote-other",
            )

    def test_fetch_channel_rejects_a_withdrawn_channel_before_downloading(self):
        artifact = self.artifact()
        for channel in ("testing", "beta"):
            with self.subTest(channel=channel):
                channel_bytes, channel_signature, _, _ = self._signed_channel(
                    artifact, channel, withdrawn=True)
                # No downloads are staged: reaching one would already fail.
                source = FakeChannelSource(
                    {
                        f"{channel}.json": channel_bytes,
                        f"{channel}.json.sig": channel_signature,
                    },
                    {},
                )
                workdir = Path(artifact.root) / f"withdrawn-{channel}"
                with self.assertRaisesRegex(
                        publication.PublicationError, "withdrawn"):
                    publication.fetch_published_channel(
                        source,
                        channel=channel,
                        trusted_key=self.pub,
                        expected_digest=artifact.package["ociDigest"],
                        expected_revision=artifact.package["sourceRevision"],
                        workdir=workdir,
                    )
                # Only the signed channel pair was read, and nothing was written.
                self.assertEqual(source.file_reads, [
                    (f"{channel}.json", source.head),
                    (f"{channel}.json.sig", source.head),
                ])
                self.assertFalse(workdir.exists())

        # Signature verification still comes first: a forged withdrawn document
        # fails verification rather than being reported as withdrawn.
        channel_bytes, _, _, _ = self._signed_channel(
            artifact, "testing", withdrawn=True)
        forged = FakeChannelSource(
            {
                "testing.json": channel_bytes,
                "testing.json.sig": cr_fixture._sign_bytes(
                    channel_bytes, self.other_priv),
            },
            {},
        )
        with self.assertRaisesRegex(
                publication.PublicationError, "verification failed"):
            publication.fetch_published_channel(
                forged,
                channel="testing",
                trusted_key=self.pub,
                expected_digest=artifact.package["ociDigest"],
                expected_revision=artifact.package["sourceRevision"],
                workdir=Path(artifact.root) / "forged-withdrawn",
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


class ReleaseBindingTest(unittest.TestCase):
    """Offline checks that a release target is the verified source revision."""

    SOURCE = "a" * 40
    OTHER = "b" * 40
    TAG_OBJECT = "c" * 40

    def _ref_path(self, ref):
        return f"/repos/{REPOSITORY}/git/ref/{ref}"

    def _tag_path(self, sha):
        return f"/repos/{REPOSITORY}/git/tags/{sha}"

    def _binding(self, responses, release, *, source=SOURCE, tag=RELEASE_TAG):
        transport = RefTransport(responses)
        github = publication.GitHub(REPOSITORY, "fixture")
        with mock.patch.object(publication.GitHub, "_request", transport):
            result = publication.release_binding(
                github, tag=tag, release=release, source_revision=source)
        return result, transport

    def _tag_ref(self, sha, *, kind="commit"):
        return {self._ref_path(f"tags/{RELEASE_TAG}"): _json_response(
            {"object": {"type": kind, "sha": sha}})}

    def test_matching_annotated_tag_is_peeled_to_its_commit(self):
        responses = self._tag_ref(self.TAG_OBJECT, kind="tag")
        responses[self._tag_path(self.TAG_OBJECT)] = _json_response(
            {"object": {"type": "commit", "sha": self.SOURCE}})
        result, transport = self._binding(
            responses,
            {"present": True, "isDraft": True, "targetCommitish": self.OTHER},
        )
        self.assertTrue(result["tagExists"])
        self.assertEqual(result["tagCommit"], self.SOURCE)
        self.assertEqual(result["targetCommit"], self.SOURCE)
        self.assertEqual(len(transport.requests), 2)

    def test_matching_lightweight_tag_is_its_commit(self):
        result, _ = self._binding(
            self._tag_ref(self.SOURCE),
            {"present": True, "isDraft": False},
        )
        self.assertTrue(result["tagExists"])
        self.assertEqual(result["tagCommit"], self.SOURCE)

    def test_an_existing_tag_at_another_commit_is_never_retargeted(self):
        for index, release in enumerate((
            {"present": False},
            {"present": True, "isDraft": True, "targetCommitish": self.SOURCE},
            {"present": True, "isDraft": False},
        )):
            with self.subTest(case=index):
                with self.assertRaisesRegex(
                        publication.PublicationError, "refusing to retarget"):
                    self._binding(self._tag_ref(self.OTHER), release)

    def test_a_draft_without_a_tag_is_bound_through_target_commitish(self):
        result, _ = self._binding(
            {self._ref_path("heads/main"): _json_response(
                {"object": {"type": "commit", "sha": self.SOURCE}})},
            {"present": True, "isDraft": True, "targetCommitish": "main"},
        )
        self.assertFalse(result["tagExists"])
        self.assertEqual(result["draftTarget"], "main")
        self.assertEqual(result["targetCommit"], self.SOURCE)

        # A full SHA needs no ref lookup and must match exactly.
        exact, transport = self._binding(
            {},
            {"present": True, "isDraft": True, "targetCommitish": self.SOURCE},
        )
        self.assertEqual(exact["targetCommit"], self.SOURCE)
        self.assertEqual(
            [url for _, url in transport.requests],
            [f"https://api.github.com/repos/{REPOSITORY}/git/ref/tags/{RELEASE_TAG}"],
        )

        with self.assertRaisesRegex(
                publication.PublicationError, "draft release"):
            self._binding(
                {},
                {"present": True, "isDraft": True,
                 "targetCommitish": self.OTHER},
            )
        with self.assertRaisesRegex(
                publication.PublicationError, "target_commitish"):
            self._binding(
                {}, {"present": True, "isDraft": True, "targetCommitish": ""})

    def test_missing_tag_is_only_a_genuine_404(self):
        result, _ = self._binding({}, {"present": False})
        self.assertEqual(result, {
            "tag": RELEASE_TAG,
            "tagExists": False,
            "tagCommit": None,
            "targetCommit": None,
            "sourceRevision": self.SOURCE,
        })
        # A permission or server failure is never "the tag does not exist".
        for status in (401, 403, 500, 502):
            with self.subTest(status=status):
                responses = {
                    self._ref_path(f"tags/{RELEASE_TAG}"):
                        (status, b'{"message": "denied"}'),
                }
                with self.assertRaisesRegex(
                        publication.PublicationError,
                        f"failed with HTTP {status}"):
                    self._binding(responses, {"present": False})

    def test_a_published_release_without_a_tag_is_rejected(self):
        with self.assertRaisesRegex(publication.PublicationError, "has no git tag"):
            self._binding({}, {"present": True, "isDraft": False})

    def test_the_source_revision_must_be_an_exact_commit_sha(self):
        for value in ("a" * 39, "A" * 40, "main", None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                        publication.PublicationError, "exact lowercase commit SHA"):
                    self._binding({}, {"present": False}, source=value)


class WorkflowConsistencyTest(unittest.TestCase):
    def test_edl_package_workflow_builds_two_signed_variants(self):
        text = (ROOT / ".github/workflows/build-edl-package.yml").read_text(
            encoding="utf-8")
        # Both variants must run even if one fails, each on its own runner.
        self.assertIn("fail-fast: false", text)
        self.assertIn("matrix:", text)
        self.assertIn("variant: release", text)
        self.assertIn("variant: install", text)
        self.assertIn("artifact: edl-package", text)
        self.assertIn("artifact: edl-install-package", text)
        self.assertIn("name: ${{ matrix.artifact }}-${{ github.run_id }}", text)
        self.assertIn(
            "group: edl-package-${{ matrix.variant }}-${{ github.ref }}", text)
        # The selected profile and the expected operation set travel with the
        # variant; the signing step signs and verifies once per runner.
        self.assertIn("EDL_PACKAGE_PROFILE: ${{ matrix.profile }}", text)
        self.assertIn("EXPECT_OFFLINE_UPGRADE: ${{ matrix.offline_upgrade }}", text)
        self.assertEqual(text.count("cosign sign-blob"), 1)
        self.assertIn("the signed descriptor is not the expected", text)

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
        # The install-only variant is never consumed by publication.
        self.assertNotIn("edl-install-package-", text)

    def test_promotion_verifies_the_channel_before_tagging(self):
        text = (ROOT / ".github/workflows/promote.yml").read_text(encoding="utf-8")
        self.assertIn("fetch-channel", text)
        self.assertLess(text.index("fetch-channel"), text.index("Publish release tags"))
        self.assertIn(".github/scripts/publish-signed-channel.sh", text)
        self.assertIn("update-channels", text)
        self.assertIn("target", text)

    def test_promotion_uses_the_signed_digest_not_the_moving_source_tag(self):
        text = (ROOT / ".github/workflows/promote.yml").read_text(encoding="utf-8")
        channel = text.index("Verify the published source channel")
        image = text.index("Verify the source image by digest")
        tags = text.index("Publish release tags")
        # The signed channel is read first and supplies the digest; the image
        # is then inspected by that digest before any tag is written.
        self.assertLess(channel, image)
        self.assertLess(image, tags)
        self.assertNotIn("SOURCE_TAG", text)
        self.assertNotIn('"docker://${REPO}:${', text)
        self.assertNotIn("--expected-oci-digest", text)
        self.assertIn('skopeo inspect "docker://${REPO}@${DIGEST}"', text)
        self.assertIn('cosign verify --key "${ARMADA_SIGNING_PUBLIC_KEY}" "${REPO}@${DIGEST}"', text)

    @staticmethod
    def _step_text(text, name):
        marker = f"- name: {name}\n"
        start = text.index(marker) + len(marker)
        rest = text[start:]
        end = rest.find("\n      - ")
        return rest if end == -1 else rest[:end]

    def test_removed_recovery_harnesses_are_not_invoked(self):
        removed = (
            "update-recovery-deployment-test.py",
            "update-recovery-mounts-test.py",
            "update-recovery-context-test.py",
        )
        for name in ("build.yml", "pr.yml"):
            text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
            for stale in removed:
                self.assertNotIn(stale, text, f"{name} invokes removed {stale}")

    def test_installed_runtime_offline_tests_run_through_discovery(self):
        discovered = {
            path.name for path in (ROOT / "tests").glob("test_update_*.py")
        }
        self.assertTrue(
            {"test_update_offline_prepare.py", "test_update_offline_execute.py"}
            <= discovered,
            "the maintained installed-runtime offline tests must match discovery",
        )
        for name in ("build.yml", "pr.yml"):
            text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
            self.assertIn("-p 'test_update_*.py'", text, name)

    def test_promotion_version_guard_runs_before_any_write(self):
        text = (ROOT / ".github/workflows/promote.yml").read_text(encoding="utf-8")
        guard = text.index("Validate release version tag")
        self.assertIn("check-version", text)
        self.assertIn('--version "${VERSION}"', text)
        self.assertLess(guard, text.index("Publish release tags"))
        self.assertLess(guard, text.index("Publish the target update channel"))

    def test_signing_key_proof_precedes_every_mutation(self):
        cases = (
            ("build.yml", "Push container to GHCR"),
            ("promote.yml", "Publish release tags"),
            ("publish-update-channel.yml", "Reconcile the package release"),
        )
        for name, mutation in cases:
            with self.subTest(workflow=name):
                text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
                install = text.index("Install Cosign")
                proof = text.index(
                    "Prove the signing key matches the trusted public key")
                self.assertLess(install, proof)
                self.assertLess(proof, text.index(mutation))
                self.assertIn("cosign-release: 'v2.6.3'", text)
                self.assertIn(
                    "run: bash .github/scripts/verify-signing-key.sh", text)
                # sign-blob with --tlog-upload=false leaves no Rekor entry, so
                # a default verify-blob then fails offline. The proof must not
                # sign or verify a blob at all.
                self.assertNotIn("sign-blob", text)
                self.assertNotIn("verify-blob", text)

    def test_signing_key_proof_compares_public_keys_locally(self):
        text = (ROOT / ".github/scripts/verify-signing-key.sh").read_text(
            encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        # cosign derives the public half from the private key; nothing is
        # signed, so no transparency-log entry is needed or expected.
        self.assertIn(
            "cosign public-key --key env://COSIGN_PRIVATE_KEY", text)
        self.assertIn("--outfile", text)
        self.assertIn("openssl pkey -pubin", text)
        self.assertEqual(text.count("openssl pkey -pubin"), 2)
        self.assertIn("-outform DER", text)
        self.assertIn("cmp -s", text)
        self.assertIn("workdir=\"$(mktemp -d", text)
        self.assertIn("trap cleanup EXIT", text)
        self.assertIn("umask 077", text)
        self.assertIn("chmod 700", text)
        self.assertIn("chmod 600", text)
        # The private key is read from the environment, never written to a
        # file or echoed.
        self.assertNotIn('> "${COSIGN_PRIVATE_KEY}"', text)
        self.assertNotIn('echo "${COSIGN_PRIVATE_KEY}', text)
        self.assertNotIn("sign-blob", text)
        self.assertNotIn("verify-blob", text)

    def test_historical_release_creation_uses_a_scoped_token(self):
        text = (ROOT / ".github/workflows/publish-update-channel.yml").read_text(
            encoding="utf-8")
        # The dedicated credential is optional: a historical build first tries
        # the built-in token, and the dedicated token is supplied to release
        # creation and draft publication only when it exists and the build is
        # historical.
        self.assertIn("historical=false", text)
        self.assertIn(
            'if [ "${historical}" = true ] && [ -n "${ARMADA_RELEASE_TOKEN:-}" ]',
            text)
        self.assertIn('create_token="${ARMADA_RELEASE_TOKEN}"', text)
        self.assertIn('GH_TOKEN="${create_token}" gh release create', text)
        # GitHub's REST update-a-release docs require Workflows write when the
        # existing target differs in workflow files, so both draft-publication
        # edits use the same scoped token.
        self.assertEqual(
            text.count('GH_TOKEN="${create_token}" gh release edit'), 2)
        self.assertIn("update-a-release", text)
        # No pre-emptive requirement to hold the privileged secret; asset
        # upload, reads, and channel publication keep the default token.
        self.assertNotIn('if [ -z "${ARMADA_RELEASE_TOKEN:-}" ]; then', text)
        self.assertNotIn('GH_TOKEN="${create_token}" gh release upload', text)
        self.assertNotIn('GH_TOKEN="${create_token}" GITHUB_TOKEN=', text)
        # The tag target stays the verified build revision; main is never a
        # fallback target.
        self.assertIn('--target "${SOURCE_REVISION}"', text)
        self.assertNotIn("--target main", text)
        # A permissions failure points at the documented remedy without
        # retargeting or changing any secret.
        self.assertIn("docs/edl-package.md", text)
        self.assertIn("Contents and Workflows write", text)
        for step in (
            "Check out source",
            "Verify package build run and artifact",
            "Download package artifact",
            "Verify signed package and parts",
        ):
            self.assertNotIn("ARMADA_RELEASE_TOKEN", self._step_text(text, step), step)

    def test_resumed_draft_publication_binds_the_verified_revision(self):
        text = (ROOT / ".github/workflows/publish-update-channel.yml").read_text(
            encoding="utf-8")
        step = self._step_text(text, "Reconcile the package release")
        self.assertIn(
            "SOURCE_REVISION: ${{ steps.package.outputs.revision }}", step)
        # Both publication edits bind the tag target to the verified revision.
        edits = [
            line for line in step.replace("\\\n", " ").splitlines()
            if "gh release edit" in line and "--draft=false" in line
            and not line.lstrip().startswith("#")
        ]
        self.assertEqual(len(edits), 2)
        for line in edits:
            argv = shlex.split(line)
            self.assertIn(
                ("--target", "${SOURCE_REVISION}"), list(zip(argv, argv[1:])))

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

    def test_release_reconciliation_binds_the_tag_before_any_mutation(self):
        text = (ROOT / ".github/workflows/publish-update-channel.yml").read_text(
            encoding="utf-8")
        view = text.index("release-view")
        binding = text.index("release-binding")
        self.assertLess(view, binding)
        for mutation in (
            "gh release create",
            "gh release upload",
            "gh release edit",
        ):
            self.assertLess(
                binding, text.index(mutation),
                f"{mutation} must not run before the tag/draft target is bound")
        # The public release is bound again after publication, before the
        # channel document is written.
        verify = text.index("Verify the published release before the channel changes")
        second = text.index("release-binding", verify)
        self.assertLess(second, text.index("Publish signed update channel"))

    def test_docs_describe_the_release_binding_and_token_scope(self):
        english = (ROOT / "docs/edl-package.md").read_text(encoding="utf-8")
        chinese = (ROOT / "docs/edl-package.zh-CN.md").read_text(encoding="utf-8")
        for text in (english, chinese):
            self.assertIn("target_commitish", text)
            self.assertIn("SOURCE_REVISION", text)
            self.assertIn("404", text)
        self.assertIn("update-a-release", english)
        self.assertIn("`gh release edit --draft=false` paths", english)
        self.assertIn("两个公开草稿的 `gh release edit --draft=false` 路径", chinese)

    def test_docs_distinguish_release_and_install_only_artifacts(self):
        english = (ROOT / "docs/edl-package.md").read_text(encoding="utf-8")
        chinese = (ROOT / "docs/edl-package.zh-CN.md").read_text(encoding="utf-8")
        for text in (english, chinese):
            self.assertIn("edl-package-", text)
            self.assertIn("edl-install-package-", text)
            self.assertIn("EDL_PACKAGE_PROFILE", text)
        self.assertIn("install-only", english)
        self.assertIn("仅首次安装", chinese)

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

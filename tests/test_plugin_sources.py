"""RED tests for build_files/resolve-plugin-sources.py.

Every future OS image build must resolve the newest available version of every
bundled plugin, so these tests pin the resolver contract: releases follow the
latest GitHub release, the git plugin follows the branch tip but installs the
immutable commit it was resolved to, the in-tree plugin reports the version that
is actually in the source tree, and a source that cannot be resolved leaves the
previous lock and report untouched instead of reusing them.

They are expected to fail (NotImplementedError) until the resolver lands. They
use only in-memory archives, injected API/git hooks and directories under
TMPDIR or TEST_ARTIFACT_ROOT; nothing here touches the network or a real
repository.
"""

import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

HELPER_PATH = Path(__file__).resolve().parents[1] / "build_files" / "resolve-plugin-sources.py"

LOCK_RELATIVE = Path("decky") / "bundled-plugins" / "plugins.lock.json"
REPORT_RELATIVE = Path("decky") / "bundled-plugins" / "plugins.resolved.json"
ASSETS_RELATIVE = Path("decky") / "bundled-plugins" / "assets"
CONFIG_RELATIVE = Path("decky") / "plugin-sources.json"

SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
FREEDOM_CONTROL_COMMIT = "89abcdef0123456789abcdef0123456789abcdef"
STORE_VERSION = "2.4.1"

LSFG_REPOSITORY = "mydanyi/decky-lsfg-vk-arm64"
CLASH_REPOSITORY = "chenx-dust/DeckyClash"
FREEDOM_CONTROL_REPOSITORY = "ArmadaOS-Freedom/freedom-control"

LEGACY_ARCHIVE = "decky/bundled-plugins/assets/LSFG-VK-ARM64-0.14.4-r16-full.zip"
LEGACY_BYTES = b"legacy r16 archive from a previous build\n"

LOCK_KEYS = {"name", "directory", "version", "url", "sha256", "archive"}


def _load_helper():
    spec = importlib.util.spec_from_file_location(
        "resolve_plugin_sources_helper", HELPER_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load helper from %s" % HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


def _temp_directory():
    for variable in ("TEST_ARTIFACT_ROOT", "TMPDIR"):
        root = os.environ.get(variable)
        if root:
            if not Path(root).is_dir():
                raise RuntimeError(
                    "%s is not an existing directory: %s" % (variable, root)
                )
            return tempfile.TemporaryDirectory(dir=root)
    return tempfile.TemporaryDirectory()


def _config():
    return {
        "schemaVersion": 1,
        "releases": [
            {
                "name": "LSFG-VK ARM64",
                "directory": "LSFG-VK ARM64",
                "repository": LSFG_REPOSITORY,
                "assetPattern": "LSFG-VK-ARM64-*-full.zip",
            },
            {
                "name": "DeckyClash",
                "directory": "DeckyClash",
                "repository": CLASH_REPOSITORY,
                "assetPattern": "DeckyClash-full-linux-arm64.zip",
            },
        ],
        "git": [
            {
                "name": "Freedom Control",
                "directory": "armada-control",
                "path": "decky/freedom-control",
                "repository": FREEDOM_CONTROL_REPOSITORY,
                "branch": "main",
                "packagePath": "decky/armada-control/package.json",
            },
        ],
        "inTree": [
            {
                "name": "Armada Store",
                "directory": "armada-store",
                "packagePath": "decky/armada-store/package.json",
            },
        ],
    }


def _plugin_zip(directory, version):
    files = {
        "plugin.json": json.dumps({"name": directory, "version": version}),
        "package.json": json.dumps({"name": directory, "version": version}),
        "main.py": "# fixture main\n",
        "dist/index.js": "// fixture bundle\n",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for relative, text in files.items():
            entry = zipfile.ZipInfo("%s/%s" % (directory, relative), (2026, 1, 1, 0, 0, 0))
            archive.writestr(entry, text)
    return buffer.getvalue()


def _asset_url(repository, tag, name):
    return "https://github.com/%s/releases/download/%s/%s" % (repository, tag, name)


def _release(repository, tag, blobs):
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-10-25T00:00:00Z",
        "assets": [
            {
                "name": name,
                "size": len(blob),
                "digest": "sha256:" + hashlib.sha256(blob).hexdigest(),
                "browser_download_url": _asset_url(repository, tag, name),
            }
            for name, blob in blobs.items()
        ],
    }


def _plugin(report, name):
    for entry in report["plugins"]:
        if entry.get("name") == name:
            return entry
    raise AssertionError("report has no plugin named %r" % name)


class _FakeGitHub:
    """Serves the release endpoint and one branch-tip commit per repository."""

    def __init__(self):
        self.releases = {}
        self.blobs = {}
        self.commits = {}
        self.offline = set()
        self.json_calls = []
        self.byte_calls = []

    def publish(self, repository, tag, blobs):
        payload = _release(repository, tag, blobs)
        self.releases[repository] = payload
        for name, blob in blobs.items():
            self.blobs[_asset_url(repository, tag, name)] = blob
        return payload

    def _check_offline(self, repository, url):
        if repository in self.offline:
            raise RuntimeError("GitHub API unavailable (503): %s" % url)

    def fetch_json(self, url):
        self.json_calls.append(url)
        for repository in self.commits:
            if "/repos/%s/" % repository not in url:
                continue
            self._check_offline(repository, url)
            if "/commits/" in url:
                return {"sha": self.commits[repository]}
        for repository, payload in self.releases.items():
            if "/repos/%s/" % repository not in url:
                continue
            self._check_offline(repository, url)
            if "/releases/latest" in url:
                return payload
            if "/releases" in url:
                return [payload]
        raise AssertionError("unexpected API URL: %s" % url)

    def fetch_bytes(self, url):
        self.byte_calls.append(url)
        try:
            return self.blobs[url]
        except KeyError:
            raise FileNotFoundError("no fixture asset for %s" % url)


class _FakeGit:
    """Simulates clone/fetch/checkout by SHA over a fixture plugin tree."""

    def __init__(self, source_commit, versions, package_path):
        self.source_commit = source_commit
        self.versions = dict(versions)
        self.package_path = package_path
        self.calls = []
        self.heads = {}

    def _repository(self, argv):
        if "-C" in argv:
            index = argv.index("-C")
            if index + 1 < len(argv):
                return argv[index + 1]
        return None

    def _checkout(self, repository, sha):
        if sha not in self.versions:
            raise RuntimeError("fake git: no such commit %r" % (sha,))
        base = Path(repository)
        target = base / self.package_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"name": "Freedom Control", "version": self.versions[sha]}),
            encoding="utf-8",
        )
        (target.parent / "plugin.json").write_text(
            json.dumps({"name": "Freedom Control"}), encoding="utf-8"
        )
        (target.parent / "main.py").write_text("# fixture main\n", encoding="utf-8")
        (target.parent / "dist").mkdir(exist_ok=True)
        (target.parent / "dist" / "index.js").write_text(
            "// fixture bundle\n", encoding="utf-8"
        )

    def __call__(self, argv):
        argv = [str(item) for item in argv]
        self.calls.append(argv)
        repository = self._repository(argv)

        if "clone" in argv:
            destination = argv[-1]
            (Path(destination) / Path(self.package_path).parent).mkdir(
                parents=True, exist_ok=True
            )
            self.heads[destination] = ""
            return ""

        if "checkout" in argv or "switch" in argv:
            keyword = "checkout" if "checkout" in argv else "switch"
            operands = [
                item
                for item in argv[argv.index(keyword) + 1:]
                if not item.startswith("-")
            ]
            sha = operands[-1] if operands else ""
            if repository is None and self.heads:
                repository = list(self.heads)[-1]
            if repository is None:
                raise RuntimeError("fake git: checkout without a work tree: %r" % (argv,))
            self.heads[repository] = sha
            self._checkout(repository, sha)
            return ""

        if "rev-parse" in argv:
            if repository is not None and self.heads.get(repository):
                return self.heads[repository]
            return self.source_commit

        if "show" in argv or "cat-file" in argv:
            spec = argv[-1]
            sha = spec.split(":", 1)[0]
            if sha in self.versions:
                return json.dumps(
                    {"name": "Freedom Control", "version": self.versions[sha]}
                )
            raise RuntimeError("fake git: unknown object %r" % (spec,))

        return ""


class PluginSourceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = _temp_directory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        self.source = self.root / "repo"
        (self.source / ASSETS_RELATIVE).mkdir(parents=True)
        store = self.source / "decky" / "armada-store"
        store.mkdir(parents=True)
        (store / "package.json").write_text(
            json.dumps({"name": "armada-store", "version": STORE_VERSION}),
            encoding="utf-8",
        )
        (self.source / CONFIG_RELATIVE).write_text(
            json.dumps(_config(), indent=2), encoding="utf-8"
        )

        self.github = _FakeGitHub()
        self.github.commits[FREEDOM_CONTROL_REPOSITORY] = FREEDOM_CONTROL_COMMIT
        self.git = _FakeGit(
            SOURCE_COMMIT,
            {FREEDOM_CONTROL_COMMIT: "0.9.4"},
            "decky/armada-control/package.json",
        )

    # -- helpers ---------------------------------------------------------
    def _resolve(self, **kwargs):
        options = {
            "fetch_json": self.github.fetch_json,
            "fetch_bytes": self.github.fetch_bytes,
            "run_git": self.git,
        }
        options.update(kwargs)
        return helper.resolve_plugin_sources(str(self.source), **options)

    def _expect_failure(self):
        try:
            self._resolve()
        except NotImplementedError:
            raise
        except Exception:
            return
        self.fail("resolver reported success for a source it cannot resolve")

    def _seed_legacy_lock(self):
        (self.source / LEGACY_ARCHIVE).write_bytes(LEGACY_BYTES)
        lock = [
            {
                "name": "LSFG-VK ARM64",
                "directory": "LSFG-VK ARM64",
                "version": "0.14.4-r16",
                "url": _asset_url(
                    LSFG_REPOSITORY,
                    "v0.14.4-r16",
                    "LSFG-VK-ARM64-0.14.4-r16-full.zip",
                ),
                "sha256": hashlib.sha256(LEGACY_BYTES).hexdigest(),
                "archive": LEGACY_ARCHIVE,
            },
            {
                "name": "DeckyClash",
                "directory": "DeckyClash",
                "version": "1.3.5",
                "url": _asset_url(
                    CLASH_REPOSITORY, "v1.3.5", "DeckyClash-full-linux-arm64.zip"
                ),
                "sha256": hashlib.sha256(b"legacy clash archive\n").hexdigest(),
            },
        ]
        (self.source / LOCK_RELATIVE).write_text(
            json.dumps(lock, indent=2) + "\n", encoding="utf-8"
        )

    def _seed_prior_report(self):
        report = {
            "schemaVersion": 1,
            "sourceCommit": "f" * 40,
            "lockSha256": "e" * 64,
            "plugins": [],
        }
        (self.source / REPORT_RELATIVE).write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )

    def _publish_r17(self):
        self.github.publish(
            LSFG_REPOSITORY,
            "v0.14.5-r17",
            {"LSFG-VK-ARM64-0.14.5-r17-full.zip": _plugin_zip("LSFG-VK ARM64", "0.14.5-r17")},
        )
        self.github.publish(
            CLASH_REPOSITORY,
            "v1.4.0-r17",
            {"DeckyClash-full-linux-arm64.zip": _plugin_zip("DeckyClash", "1.4.0-r17")},
        )

    def _publish_r18(self):
        self.github.publish(
            LSFG_REPOSITORY,
            "v0.14.6-r18",
            {"LSFG-VK-ARM64-0.14.6-r18-full.zip": _plugin_zip("LSFG-VK ARM64", "0.14.6-r18")},
        )

    def _lock(self):
        return json.loads((self.source / LOCK_RELATIVE).read_text(encoding="utf-8"))

    def _validate(self, *, installed_root=None):
        options = {}
        if installed_root is not None:
            options["installed_root"] = str(installed_root)
        return helper.validate_resolved_plugin_sources(str(self.source), **options)

    def _expect_validation_failure(self, *, installed_root=None):
        """Accept only a normal validation failure, never a stub success.

        NotImplementedError is re-raised first so a still-stubbed validator
        fails loudly (RED) instead of slipping through an assertRaises.
        """
        try:
            self._validate(installed_root=installed_root)
        except NotImplementedError:
            raise
        except helper.ResolveError as error:
            return error
        self.fail("validator accepted a resolution it must reject")

    def _rewrite_json(self, relative, document):
        payload = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        (self.source / relative).write_bytes(payload)
        return payload

    # -- cases -----------------------------------------------------------
    def test_legacy_archive_is_not_reused_when_latest_release_moves_on(self):
        self._seed_legacy_lock()
        self._publish_r17()

        report = self._resolve()

        lock = {entry["name"]: entry for entry in self._lock()}
        lsfg = lock["LSFG-VK ARM64"]
        self.assertEqual(lsfg["version"], "0.14.5-r17")
        self.assertNotEqual(lsfg["archive"], LEGACY_ARCHIVE)
        self.assertTrue(lsfg["archive"].startswith("decky/bundled-plugins/assets/"))

        payload = (self.source / lsfg["archive"]).read_bytes()
        self.assertEqual(payload, _plugin_zip("LSFG-VK ARM64", "0.14.5-r17"))
        self.assertEqual(hashlib.sha256(payload).hexdigest(), lsfg["sha256"])

        entry = _plugin(report, "LSFG-VK ARM64")
        self.assertEqual(entry["kind"], "release")
        self.assertEqual(entry["version"], "0.14.5-r17")
        self.assertEqual(entry["sha256"], lsfg["sha256"])
        self.assertEqual(entry["releaseTag"], "v0.14.5-r17")

    def test_production_catalogue_resolves_with_legacy_lsfg_lock(self):
        """A legacy LSFG lock/archive must not survive resolution of the real catalogue."""
        production = (HELPER_PATH.parent.parent / CONFIG_RELATIVE).read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            json.loads((self.source / CONFIG_RELATIVE).read_text(encoding="utf-8"))[
                "releases"
            ][0]["repository"],
            LSFG_REPOSITORY,
            "fixture config must start with the LSFG release",
        )
        (self.source / CONFIG_RELATIVE).write_text(production, encoding="utf-8")
        self._publish_r17()
        self._seed_legacy_lock()
        self.github.offline.add(LSFG_REPOSITORY)
        self._resolve()
        urls = self.github.json_calls + self.github.byte_calls
        self.assertFalse(
            any(LSFG_REPOSITORY in url for url in urls),
            "resolver must not contact the private LSFG repository: %r" % (urls,),
        )
        lock = self._lock()
        self.assertEqual(
            [entry["name"] for entry in lock],
            ["DeckyClash"],
            "lock must list only DeckyClash after the LSFG release is removed",
        )
        for entry in lock:
            self.assertEqual(set(entry), LOCK_KEYS)
        report = json.loads(
            (self.source / REPORT_RELATIVE).read_text(encoding="utf-8")
        )
        self.assertEqual(
            [entry["name"] for entry in report["plugins"]],
            ["DeckyClash", "Freedom Control", "Armada Store"],
        )
        self._validate()

    def test_every_bundled_release_is_updated_and_reported(self):
        self._seed_legacy_lock()
        self._publish_r17()

        report = self._resolve()

        versions = {entry["name"]: entry["version"] for entry in report["plugins"]}
        self.assertEqual(versions["LSFG-VK ARM64"], "0.14.5-r17")
        self.assertEqual(versions["DeckyClash"], "1.4.0-r17")

        lock = self._lock()
        self.assertEqual([entry["name"] for entry in lock], ["LSFG-VK ARM64", "DeckyClash"])
        self.assertEqual([entry["version"] for entry in lock], ["0.14.5-r17", "1.4.0-r17"])
        for entry in lock:
            self.assertEqual(set(entry), LOCK_KEYS)
            self.assertTrue(entry["archive"].startswith("decky/bundled-plugins/assets/"))
            self.assertEqual(
                (self.source / entry["archive"]).read_bytes(),
                _plugin_zip(entry["directory"], entry["version"]),
            )

        clash = _plugin(report, "DeckyClash")
        self.assertEqual(clash["kind"], "release")
        self.assertEqual(clash["releaseTag"], "v1.4.0-r17")
        self.assertEqual(clash["repository"], CLASH_REPOSITORY)

    def test_rerun_refreshes_a_lock_resolved_from_an_older_latest(self):
        self._seed_legacy_lock()
        self._publish_r17()
        first = self._resolve()
        first_lock = (self.source / LOCK_RELATIVE).read_bytes()

        self._publish_r18()
        second = self._resolve()
        second_lock = (self.source / LOCK_RELATIVE).read_bytes()

        self.assertNotEqual(first_lock, second_lock)
        lock = {entry["name"]: entry for entry in self._lock()}
        self.assertEqual(lock["LSFG-VK ARM64"]["version"], "0.14.6-r18")
        self.assertNotEqual(
            lock["LSFG-VK ARM64"]["sha256"], _plugin(first, "LSFG-VK ARM64")["sha256"]
        )
        stale_archive = _plugin(first, "LSFG-VK ARM64")["archive"]
        self.assertNotIn(stale_archive, second_lock.decode("utf-8"))
        self.assertEqual(
            second["lockSha256"], hashlib.sha256(second_lock).hexdigest()
        )

    def test_api_failure_preserves_prior_lock_and_report(self):
        self._seed_legacy_lock()
        self._seed_prior_report()
        lock_before = (self.source / LOCK_RELATIVE).read_bytes()
        report_before = (self.source / REPORT_RELATIVE).read_bytes()

        self.github.publish(
            LSFG_REPOSITORY,
            "v0.14.5-r17",
            {"LSFG-VK-ARM64-0.14.5-r17-full.zip": _plugin_zip("LSFG-VK ARM64", "0.14.5-r17")},
        )
        self.github.offline.add(CLASH_REPOSITORY)

        self._expect_failure()

        self.assertEqual(lock_before, (self.source / LOCK_RELATIVE).read_bytes())
        self.assertEqual(report_before, (self.source / REPORT_RELATIVE).read_bytes())

    def test_missing_release_asset_preserves_lock_and_never_falls_back(self):
        self._seed_legacy_lock()
        lock_before = (self.source / LOCK_RELATIVE).read_bytes()

        self.github.publish(
            LSFG_REPOSITORY,
            "v0.14.5-r17",
            {"LSFG-VK-ARM64-0.14.5-r17-full.zip": _plugin_zip("LSFG-VK ARM64", "0.14.5-r17")},
        )
        self.github.publish(
            CLASH_REPOSITORY,
            "v1.4.0-r17",
            {"DeckyClash-linux-x86_64.zip": _plugin_zip("DeckyClash", "1.4.0-r17")},
        )

        self._expect_failure()

        self.assertEqual(lock_before, (self.source / LOCK_RELATIVE).read_bytes())
        self.assertEqual((self.source / LEGACY_ARCHIVE).read_bytes(), LEGACY_BYTES)
        self.assertIn(LEGACY_ARCHIVE, lock_before.decode("utf-8"))

    def test_hash_mismatch_preserves_lock_and_writes_no_asset(self):
        self._seed_legacy_lock()
        self._seed_prior_report()
        lock_before = (self.source / LOCK_RELATIVE).read_bytes()
        report_before = (self.source / REPORT_RELATIVE).read_bytes()

        blob = _plugin_zip("LSFG-VK ARM64", "0.14.5-r17")
        self.github.publish(
            LSFG_REPOSITORY,
            "v0.14.5-r17",
            {"LSFG-VK-ARM64-0.14.5-r17-full.zip": blob},
        )
        self.github.blobs[
            _asset_url(
                LSFG_REPOSITORY, "v0.14.5-r17", "LSFG-VK-ARM64-0.14.5-r17-full.zip"
            )
        ] = blob + b"tampered in transit\n"
        self.github.publish(
            CLASH_REPOSITORY,
            "v1.4.0-r17",
            {"DeckyClash-full-linux-arm64.zip": _plugin_zip("DeckyClash", "1.4.0-r17")},
        )

        self._expect_failure()

        self.assertEqual(lock_before, (self.source / LOCK_RELATIVE).read_bytes())
        self.assertEqual(report_before, (self.source / REPORT_RELATIVE).read_bytes())
        self.assertEqual(
            sorted(path.name for path in (self.source / ASSETS_RELATIVE).iterdir()),
            ["LSFG-VK-ARM64-0.14.4-r16-full.zip"],
        )

    def test_git_plugin_is_installed_from_the_immutable_resolved_commit(self):
        self._seed_legacy_lock()
        stale = self.source / "decky" / "freedom-control" / "decky" / "armada-control"
        stale.mkdir(parents=True)
        (stale / "package.json").write_text(
            json.dumps({"name": "Freedom Control", "version": "0.9.0"}),
            encoding="utf-8",
        )
        self._publish_r17()

        report = self._resolve()

        entry = _plugin(report, "Freedom Control")
        self.assertEqual(entry["kind"], "git")
        self.assertEqual(entry["directory"], "armada-control")
        self.assertEqual(entry["repository"], FREEDOM_CONTROL_REPOSITORY)
        self.assertEqual(entry["branch"], "main")
        self.assertEqual(entry["commit"], FREEDOM_CONTROL_COMMIT)
        self.assertEqual(entry["version"], "0.9.4")

        commit_calls = [url for url in self.github.json_calls if "/commits/" in url]
        self.assertEqual(len(commit_calls), 1)
        self.assertIn(
            "/repos/%s/commits/main" % FREEDOM_CONTROL_REPOSITORY, commit_calls[0]
        )

        checkouts = [argv for argv in self.git.calls if "checkout" in argv]
        self.assertTrue(checkouts, "resolver never checked out the resolved commit")
        for argv in checkouts:
            self.assertIn(FREEDOM_CONTROL_COMMIT, argv)
            self.assertNotIn("main", argv)

    def test_in_tree_plugin_records_the_actual_source_version(self):
        self._seed_legacy_lock()
        self._publish_r17()

        report = self._resolve()

        entry = _plugin(report, "Armada Store")
        self.assertEqual(entry["kind"], "in-tree")
        self.assertEqual(entry["directory"], "armada-store")
        self.assertEqual(entry["version"], STORE_VERSION)
        self.assertEqual(entry["commit"], SOURCE_COMMIT)
        self.assertEqual(
            [lock_entry["name"] for lock_entry in self._lock()],
            ["LSFG-VK ARM64", "DeckyClash"],
        )

    def test_report_matches_the_written_lock_bytes(self):
        self._seed_legacy_lock()
        self._publish_r17()

        report = self._resolve()
        lock_bytes = (self.source / LOCK_RELATIVE).read_bytes()

        self.assertEqual(report["schemaVersion"], 1)
        self.assertEqual(report["sourceCommit"], SOURCE_COMMIT)
        self.assertRegex(report["sourceCommit"], r"^[0-9a-f]{40}$")
        self.assertEqual(report["lockSha256"], hashlib.sha256(lock_bytes).hexdigest())
        self.assertEqual(
            [entry["name"] for entry in report["plugins"]],
            ["LSFG-VK ARM64", "DeckyClash", "Freedom Control", "Armada Store"],
        )
        self.assertIsInstance(self._lock(), list)
        self.assertEqual(
            json.loads((self.source / REPORT_RELATIVE).read_text(encoding="utf-8")),
            report,
        )

    # -- validation of the generated report against the checkout ----------
    def test_fresh_checkout_needs_no_tracked_lock_report_or_assets(self):
        (self.source / ASSETS_RELATIVE).rmdir()
        self.assertFalse((self.source / LOCK_RELATIVE).exists())
        self.assertFalse((self.source / REPORT_RELATIVE).exists())
        self._publish_r17()

        report = self._resolve()

        self.assertTrue((self.source / LOCK_RELATIVE).is_file())
        self.assertTrue((self.source / REPORT_RELATIVE).is_file())
        lock = {entry["name"]: entry for entry in self._lock()}
        self.assertEqual(
            sorted(path.name for path in (self.source / ASSETS_RELATIVE).iterdir()),
            sorted(Path(entry["archive"]).name for entry in lock.values()),
        )
        versions = {entry["name"]: entry["version"] for entry in report["plugins"]}
        self.assertEqual(
            versions,
            {
                "LSFG-VK ARM64": "0.14.5-r17",
                "DeckyClash": "1.4.0-r17",
                "Freedom Control": "0.9.4",
                "Armada Store": STORE_VERSION,
            },
        )
        for entry in lock.values():
            archive = self.source / entry["archive"]
            self.assertEqual(
                archive.read_bytes(), _plugin_zip(entry["directory"], entry["version"])
            )

    def test_invalid_source_commit_is_rejected(self):
        self._publish_r17()

        for value in ("0" * 40, SOURCE_COMMIT.upper(), SOURCE_COMMIT[:12]):
            with self.subTest(source_commit=value):
                self.git.source_commit = value
                self._expect_failure()

    def test_validator_accepts_a_fresh_resolution_and_returns_the_lock(self):
        self._publish_r17()
        self._resolve()
        lock = self._lock()

        self.assertEqual(len(lock), 2)
        validated = self._validate()
        self.assertEqual(validated, lock)
        self.assertEqual(
            [entry["version"] for entry in validated],
            ["0.14.5-r17", "1.4.0-r17"],
        )

    def test_validator_rejects_a_missing_report(self):
        self._seed_legacy_lock()

        error = self._expect_validation_failure()

        self.assertIn("resolve-plugin-sources.py", str(error))

    def test_validator_rejects_lock_bytes_changed_after_resolution(self):
        self._publish_r17()
        report = self._resolve()
        lock_bytes = (self.source / LOCK_RELATIVE).read_bytes()

        lock = self._lock()
        lock[0]["version"] = "0.14.4-r16"
        changed = self._rewrite_json(LOCK_RELATIVE, lock)

        self.assertNotEqual(changed, lock_bytes)
        self.assertEqual(report["lockSha256"], hashlib.sha256(lock_bytes).hexdigest())
        self._expect_validation_failure()

    def test_validator_rejects_archive_whose_internal_version_is_stale(self):
        self._publish_r17()
        report = self._resolve()
        lock = self._lock()

        entry = next(item for item in lock if item["name"] == "LSFG-VK ARM64")
        stale = _plugin_zip("LSFG-VK ARM64", "0.13.0-r15")
        (self.source / entry["archive"]).write_bytes(stale)

        # Re-seal every outer hash around the stale archive so only the version
        # inside the ZIP can still disagree with the entry and the report.
        entry["sha256"] = hashlib.sha256(stale).hexdigest()
        lock_bytes = self._rewrite_json(LOCK_RELATIVE, lock)
        report["lockSha256"] = hashlib.sha256(lock_bytes).hexdigest()
        _plugin(report, "LSFG-VK ARM64")["sha256"] = entry["sha256"]
        self._rewrite_json(REPORT_RELATIVE, report)

        self.assertEqual(entry["version"], "0.14.5-r17")
        self.assertEqual(_plugin(report, "LSFG-VK ARM64")["version"], "0.14.5-r17")
        self._expect_validation_failure()

    def test_validator_rejects_git_plugin_version_drifting_from_report(self):
        self._publish_r17()
        report = self._resolve()
        self.assertEqual(_plugin(report, "Freedom Control")["version"], "0.9.4")

        package = (
            self.source
            / "decky"
            / "freedom-control"
            / "decky"
            / "armada-control"
            / "package.json"
        )
        package.write_text(
            json.dumps({"name": "Freedom Control", "version": "0.9.0"}),
            encoding="utf-8",
        )

        self._expect_validation_failure()

    def test_validator_checks_the_versions_installed_under_installed_root(self):
        self._publish_r17()
        report = self._resolve()
        installed = self.root / "installed"
        versions = {
            entry["directory"]: entry["version"] for entry in report["plugins"]
        }

        for directory in ("armada-control", "armada-store"):
            target = installed / directory
            target.mkdir(parents=True)
            (target / "package.json").write_text(
                json.dumps({"name": directory, "version": versions[directory]}),
                encoding="utf-8",
            )

        self.assertEqual(self._validate(installed_root=installed), self._lock())

        (installed / "armada-control" / "package.json").write_text(
            json.dumps({"name": "armada-control", "version": "0.9.0"}),
            encoding="utf-8",
        )
        self._expect_validation_failure(installed_root=installed)

    def test_validator_rejects_a_report_missing_a_declared_source(self):
        self._publish_r17()
        report = self._resolve()

        report["plugins"] = [
            entry for entry in report["plugins"] if entry["name"] != "Armada Store"
        ]
        self._rewrite_json(REPORT_RELATIVE, report)

        # The lock bytes are unchanged, so only the missing coverage can fail.
        self.assertEqual(
            report["lockSha256"],
            hashlib.sha256((self.source / LOCK_RELATIVE).read_bytes()).hexdigest(),
        )
        self._expect_validation_failure()


if __name__ == "__main__":
    unittest.main()

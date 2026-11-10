#!/usr/bin/env python3
"""Verify a completed package build and publish its signed update channel.

Every check here reuses the installed verifier contract: the descriptor and
channel are authenticated with updates.signature, the split parts are verified
with updates.package_media.verify_parts, and the channel/package identity
binding uses updates.channel_release. Publication never builds, retags, or
falls back to another source, and it never replaces an asset that already
exists with different content.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

from updates.catalog import CatalogError, load_channel  # noqa: E402
from updates.channel_release import bind_package  # noqa: E402
from updates.package_contract import PackageError, load_package  # noqa: E402
from updates.package_media import verify_parts  # noqa: E402
from updates.signature import (  # noqa: E402
    MAX_DOCUMENT_BYTES,
    MAX_SIGNATURE_BYTES,
)

CHANNELS = ("stable", "beta", "testing")
CHANNEL_BRANCH = "update-channels"
CANONICAL_REPOSITORY = "ArmadaOS-Freedom/armadaos-freedom"
PACKAGE_WORKFLOWS = (
    ".github/workflows/build-edl-package.yml",
    ".github/workflows/build.yml",
)
BUILD_IMAGES_WORKFLOW = ".github/workflows/build.yml"
API_ROOT = "https://api.github.com"
DEFAULT_TRUSTED_KEY = ROOT / "system_files/etc/pki/containers/armadaos-freedom.pub"
MAX_REMOTE_BYTES = 64 * 1024 * 1024
CHECKSUM_CHUNK_BYTES = 1024 * 1024
RELEASE_PAGE_SIZE = 100
_NETWORK_TIMEOUT = 60
_GITHUB_HOSTS = frozenset((
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
))
_SAFE_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z", re.ASCII)
_RESERVED_TAGS = frozenset(("latest",) + CHANNELS)
_DESCRIPTOR_SUFFIX = ".zip.update.json"
_PACKAGE_SIDECARS = ("SHA256SUMS",)


def _is_github_host(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return host in _GITHUB_HOSTS


class AuthenticatedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            def origin(url):
                parsed = urllib.parse.urlsplit(url)
                return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
            if origin(req.full_url) != origin(newurl):
                redirected.remove_header("Authorization")
        return redirected


def open_request(request):
    return urllib.request.build_opener(AuthenticatedRedirectHandler()).open(
        request, timeout=_NETWORK_TIMEOUT)


class PublicationError(RuntimeError):
    """A publication precondition is not met; nothing may be published."""


class GitHubConflict(PublicationError):
    """The channel branch moved while this run was preparing its commit."""


def _strict_object(pairs):
    hook = dict(pairs)
    if len(hook) != len(pairs):
        raise PublicationError("duplicate JSON object key")
    return hook


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise PublicationError(f"{label} exceeds {limit} bytes")
    return data


def _require_channel(channel) -> str:
    if not isinstance(channel, str) or channel not in CHANNELS:
        raise PublicationError("channel must be stable, beta, or testing")
    return channel


def release_tag(release_id) -> str:
    """Return releaseId as an immutable, git-safe release tag."""
    if not isinstance(release_id, str) or not _SAFE_TAG.fullmatch(release_id):
        raise PublicationError(
            "releaseId is not a safe release tag: expected 1-128 characters of "
            "[A-Za-z0-9._+-] starting with an alphanumeric"
        )
    if release_id in _RESERVED_TAGS:
        raise PublicationError("releaseId must not be a floating channel name")
    if ".." in release_id or release_id.endswith((".", ".lock")):
        raise PublicationError("releaseId is not a safe release tag")
    return release_id


def release_asset_names(package: dict) -> list:
    """Asset names in upload order: parts, parts manifest, descriptor, signature."""
    archive = package["archive"]["fileName"]
    names = [part["fileName"] for part in package["parts"]]
    if not names:
        raise PublicationError("package declares no split parts")
    names += [archive + ".parts.json", archive + ".update.json", archive + ".update.json.sig"]
    return names


def _verify_parts_manifest(path: Path, package: dict) -> None:
    if not path.is_file():
        raise PublicationError(f"parts manifest is missing: {path.name}")
    try:
        manifest = json.loads(
            _read_bounded(path, MAX_DOCUMENT_BYTES, "parts manifest"),
            object_pairs_hook=_strict_object,
        )
    except ValueError as exc:
        raise PublicationError(f"parts manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schemaVersion", "format", "archive", "partSizeBytes", "parts"
    }:
        raise PublicationError("parts manifest has unexpected fields")
    if type(manifest["schemaVersion"]) is not int or manifest["schemaVersion"] != 1:
        raise PublicationError("parts manifest schemaVersion must be integer 1")
    if manifest["format"] != "raw-split":
        raise PublicationError("parts manifest format must be raw-split")
    if manifest["archive"] != package["archive"]:
        raise PublicationError("parts manifest archive does not match the descriptor")
    if manifest["parts"] != package["parts"]:
        raise PublicationError("parts manifest parts do not match the descriptor")
    if type(manifest["partSizeBytes"]) is not int or manifest["partSizeBytes"] <= 0:
        raise PublicationError("parts manifest partSizeBytes must be a positive integer")


def _verify_sha256sums(path: Path, package: dict) -> None:
    if not path.is_file():
        return
    try:
        text = _read_bounded(path, MAX_DOCUMENT_BYTES, "SHA256SUMS").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublicationError("SHA256SUMS is not valid UTF-8") from exc
    expected = {
        f"{part['sha256']}  {part['fileName']}" for part in package["parts"]
    }
    recorded = {line.strip() for line in text.splitlines() if line.strip()}
    if recorded != expected:
        raise PublicationError("SHA256SUMS does not match the descriptor parts")


def verify_package_directory(directory, *, trusted_key=DEFAULT_TRUSTED_KEY) -> dict:
    """Authenticate a downloaded package artifact and return its release plan."""
    root = Path(directory)
    if not root.is_dir():
        raise PublicationError(f"package directory does not exist: {directory}")
    descriptors = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.name.endswith(_DESCRIPTOR_SUFFIX)
    )
    if len(descriptors) != 1:
        raise PublicationError(
            f"expected exactly one *{_DESCRIPTOR_SUFFIX} in {directory}, "
            f"found {len(descriptors)}"
        )
    descriptor = descriptors[0]
    package_dir = descriptor.parent
    archive_name = descriptor.name[: -len(".update.json")]
    signature = package_dir / (descriptor.name + ".sig")
    parts_manifest = package_dir / (archive_name + ".parts.json")

    if not signature.is_file():
        raise PublicationError(f"descriptor signature is missing: {signature.name}")
    document = _read_bounded(descriptor, MAX_DOCUMENT_BYTES, "descriptor")
    signature_bytes = _read_bounded(signature, MAX_SIGNATURE_BYTES, "descriptor signature")
    try:
        package = load_package(document, signature_bytes, trusted_key=trusted_key)
    except PackageError as exc:
        raise PublicationError(f"descriptor verification failed: {exc}") from exc
    if "offline-upgrade" not in package["operations"]:
        raise PublicationError("package does not declare the offline-upgrade operation")
    if package["archive"]["fileName"] != archive_name:
        raise PublicationError("descriptor name does not match its archive fileName")

    try:
        verify_parts(package_dir, package)
    except PackageError as exc:
        raise PublicationError(f"package parts verification failed: {exc}") from exc
    _verify_parts_manifest(parts_manifest, package)
    _verify_sha256sums(package_dir / "SHA256SUMS", package)

    names = release_asset_names(package)
    parts_by_name = {part["fileName"]: part for part in package["parts"]}
    assets = []
    for name in names:
        path = package_dir / name
        if not path.is_file():
            raise PublicationError(f"package asset is missing: {name}")
        if name in parts_by_name:
            size = parts_by_name[name]["bytes"]
            digest = parts_by_name[name]["sha256"]
        elif name.endswith(".zip.update.json"):
            size, digest = len(document), _sha256(document)
        elif name.endswith(".zip.update.json.sig"):
            size, digest = len(signature_bytes), _sha256(signature_bytes)
        else:
            raw = _read_bounded(path, MAX_DOCUMENT_BYTES, name)
            size, digest = len(raw), _sha256(raw)
        assets.append({"name": name, "path": str(path), "bytes": size, "sha256": digest})

    allowed = set(names) | set(_PACKAGE_SIDECARS)
    allowed |= {path.name for path in package_dir.iterdir() if path.name.startswith("README")}
    stray = sorted(
        str(path.relative_to(package_dir)) for path in package_dir.rglob("*")
        if path.is_file() and (path.parent != package_dir or path.name not in allowed)
    )
    if stray:
        raise PublicationError(
            "package directory contains files that must not become release "
            f"assets: {', '.join(stray)}"
        )
    stray = sorted(
        str(path.relative_to(root)) for path in root.rglob("*")
        if path.is_file() and package_dir not in path.parents
    )
    if stray:
        raise PublicationError(
            f"package artifact contains files outside {package_dir.name}: {', '.join(stray)}"
        )

    return {
        "packageDir": str(package_dir),
        "descriptor": str(descriptor),
        "descriptorSignature": str(signature),
        "partsManifest": str(parts_manifest),
        "archive": package["archive"],
        "releaseTag": release_tag(package["releaseId"]),
        "releaseId": package["releaseId"],
        "version": package["version"],
        "sourceRevision": package["sourceRevision"],
        "ociDigest": package["ociDigest"],
        "ostreeCommit": package["ostreeCommit"],
        "supportedModels": package["supportedModels"],
        "minimumUpdaterVersion": package["minimumUpdaterVersion"],
        "operations": package["operations"],
        "assets": assets,
        "totalBytes": sum(asset["bytes"] for asset in assets),
    }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def descriptor_urls(repository: str, tag: str, archive_name: str) -> dict:
    """Return the immutable release asset URLs recorded in the channel."""
    require_repository(repository)
    release_tag(tag)
    if not isinstance(archive_name, str) or not archive_name.endswith(".zip"):
        raise PublicationError("archive fileName must end in .zip")
    base = f"https://github.com/{repository}/releases/download/{tag}"
    return {
        "tag": tag,
        "descriptorUrl": f"{base}/{archive_name}.update.json",
        "signatureUrl": f"{base}/{archive_name}.update.json.sig",
    }


def require_repository(repository: str) -> str:
    if not isinstance(repository, str) or repository.lower() != CANONICAL_REPOSITORY.lower():
        raise PublicationError(
            f"this publisher only serves {CANONICAL_REPOSITORY}, not {repository!r}"
        )
    return repository


def verify_package_run(run, artifacts, *, run_id, repository) -> dict:
    """Check that the artifact comes from the expected completed build run."""
    require_repository(repository)
    if type(run_id) is not int or run_id <= 0:
        raise PublicationError("run id must be a positive integer")
    if not isinstance(run, dict):
        raise PublicationError("run metadata must be an object")
    if run.get("id") != run_id:
        raise PublicationError("run id does not match the requested run")
    if run.get("status") != "completed":
        raise PublicationError("the package build run is not completed")
    if run.get("conclusion") != "success":
        raise PublicationError("the package build run did not succeed")
    for field in ("repository", "head_repository"):
        value = run.get(field)
        name = value.get("full_name") if isinstance(value, dict) else None
        if not isinstance(name, str) or name.lower() != repository.lower():
            raise PublicationError(
                f"the package build run does not belong to {repository} ({field})"
            )
    path = str(run.get("path") or "").split("@", 1)[0]
    if path not in PACKAGE_WORKFLOWS:
        raise PublicationError(
            "the package build run is not Build EDL package or Build images"
        )
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get("artifacts"), list):
        raise PublicationError("artifact metadata must list artifacts")
    expected = f"edl-package-{run_id}"
    matches = [
        item for item in artifacts["artifacts"]
        if isinstance(item, dict) and item.get("name") == expected
    ]
    if len(matches) != 1:
        raise PublicationError(f"expected exactly one {expected} artifact in the run")
    artifact = matches[0]
    if artifact.get("expired") is True:
        raise PublicationError(f"the {expected} artifact has expired")
    size = artifact.get("size_in_bytes")
    if type(size) is not int or size <= 0:
        raise PublicationError(f"the {expected} artifact is empty")
    return {
        "runId": run_id,
        "workflow": path,
        "event": run.get("event"),
        "headSha": run.get("head_sha"),
        "headBranch": run.get("head_branch"),
        "artifact": expected,
        "artifactBytes": size,
    }


def require_run_identity(run_info: dict, plan: dict) -> None:
    """Reject a package whose revision is not the revision that run built."""
    if not isinstance(run_info, dict) or not isinstance(plan, dict):
        raise PublicationError("run and package metadata are required")
    if run_info.get("workflow") == BUILD_IMAGES_WORKFLOW:
        if run_info.get("headSha") != plan.get("sourceRevision"):
            raise PublicationError(
                "the package source revision is not the revision Build images ran on"
            )


def _plan_asset_map(assets) -> dict:
    """Return name -> (bytes, sha256) for the verified package assets."""
    if not isinstance(assets, list) or not assets:
        raise PublicationError("package assets are required")
    local = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise PublicationError("package assets must be objects")
        name = asset.get("name")
        size = asset.get("bytes")
        digest = asset.get("sha256")
        if not isinstance(name, str) or not name:
            raise PublicationError("asset names must be nonempty strings")
        if name in local:
            raise PublicationError(f"duplicate asset name: {name}")
        if type(size) is not int or size <= 0:
            raise PublicationError(f"{name} must record a positive size")
        if not isinstance(digest, str) or len(digest) != 64:
            raise PublicationError(f"{name} must record a SHA256 digest")
        local[name] = (size, digest)
    return local


def _release_asset_list(release):
    """Return the release asset list, or None when the release does not exist."""
    if not isinstance(release, dict) or not isinstance(release.get("present"), bool):
        raise PublicationError("release metadata must state whether the release exists")
    if not release["present"]:
        return None
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise PublicationError("release metadata must list its assets")
    return assets


def release_state(assets, release, *, hashes=None) -> dict:
    """Classify an existing release against the verified package assets.

    ``release`` is ``{"present": bool, "isDraft": bool, "assets": [...]}`` as
    produced by ``release-view``; ``hashes`` carries the SHA256 of every remote
    asset that was verified.  A release that does not exist is the only
    ``absent`` case: an existing draft, even with no assets at all, is
    ``incomplete`` so it is resumed instead of recreated.
    """
    local = _plan_asset_map(assets)
    remote_list = _release_asset_list(release)
    if remote_list is None:
        return {"state": "absent", "missing": sorted(local)}
    draft = release.get("isDraft") is True
    verified = hashes if isinstance(hashes, dict) else {}

    remote = {}
    for asset in remote_list:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise PublicationError("release assets must be named objects")
        name = asset["name"]
        if name in remote:
            raise PublicationError(f"the release lists {name} twice")
        remote[name] = asset
    unexpected = sorted(set(remote) - set(local))
    if unexpected:
        return {"state": "conflict", "missing": [], "unexpected": unexpected}
    for name in sorted(remote):
        size, digest = local[name]
        if remote[name].get("size") != size:
            return {"state": "conflict", "missing": []}
        seen = verified.get(name)
        if seen is not None and seen != digest:
            return {"state": "conflict", "missing": []}

    missing = sorted(set(local) - set(remote))
    if missing:
        if not draft:
            # A public release must match the verified package exactly; missing
            # assets are never repaired on a published release.
            return {"state": "conflict", "missing": []}
        return {"state": "incomplete", "missing": missing}

    unverified = sorted(name for name in remote if name not in verified)
    if unverified:
        return {"state": "unverified", "missing": [], "unverified": unverified}
    return {"state": "matching", "missing": []}


def _api_digest(asset) -> str | None:
    """Return the verified SHA256 the API reports, or None when unavailable."""
    digest = asset.get("digest") if isinstance(asset, dict) else None
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return None
    value = digest[len("sha256:"):].strip().lower()
    if len(value) != 64:
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return value


def verify_release_assets(github, plan: dict, release: dict) -> dict:
    """Return name -> SHA256 for every plan asset already on the release.

    Only assets that are actually present are read.  A server-computed
    ``sha256:`` digest is used when the API reports one; otherwise the asset is
    downloaded and hashed in bounded chunks, so a same-size replacement is
    still rejected.  Missing assets are not fetched and are reported by
    ``release_state`` instead.
    """
    local = _plan_asset_map(plan["assets"])
    remote_list = _release_asset_list(release)
    if remote_list is None:
        return {}
    hashes = {}
    for asset in remote_list:
        name = asset.get("name") if isinstance(asset, dict) else None
        if name not in local:
            continue
        size, _expected = local[name]
        digest = _api_digest(asset)
        if digest is None:
            url = asset.get("url") or asset.get("browser_download_url")
            if not isinstance(url, str):
                raise PublicationError(f"{name} has no downloadable location")
            digest = github.stream_sha256(url, expected_bytes=size, label=name)
        hashes[name] = digest
    return hashes


class GitHub:
    """Minimal authenticated GitHub API client with no third-party dependency."""

    def __init__(self, repository: str, token: str):
        self.repository = require_repository(repository)
        if not isinstance(token, str) or not token:
            raise PublicationError("a GitHub token is required")
        self._token = token

    def _request(self, method, url, payload=None,
                 accept="application/vnd.github+json", authenticated=True):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", accept)
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "armada-update-publication")
        if authenticated:
            request.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with open_request(request) as response:
                return response.status, response.read(MAX_REMOTE_BYTES)
        except urllib.error.HTTPError as exc:
            body = exc.read(64 * 1024)
            if exc.code in (404, 409, 422):
                return exc.code, body
            raise PublicationError(
                f"GitHub API {method} {url.split('?')[0]} failed with {exc.code}"
            ) from None
        except (urllib.error.URLError, OSError) as exc:
            raise PublicationError(
                f"GitHub API {method} {url.split('?')[0]} failed: {type(exc).__name__}"
            ) from None

    def _api(self, path: str) -> str:
        return API_ROOT + path if path.startswith("/") else path

    def _decode(self, status, body, label, *, optional=False):
        if status == 404 and optional:
            return None
        if status in (409, 422):
            raise GitHubConflict(f"{label}: the channel branch is out of date")
        if status < 200 or status >= 300:
            raise PublicationError(f"{label} failed with HTTP {status}")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PublicationError(f"{label} returned invalid JSON") from exc

    def get_json(self, path: str):
        return self._decode(*self._request("GET", self._api(path)), f"GET {path}")

    def get_optional_json(self, path: str):
        status, body = self._request("GET", self._api(path))
        return self._decode(status, body, f"GET {path}", optional=True)

    def post_json(self, path: str, payload: dict):
        return self._decode(
            *self._request("POST", self._api(path), payload), f"POST {path}"
        )

    def patch_json(self, path: str, payload: dict):
        return self._decode(
            *self._request("PATCH", self._api(path), payload), f"PATCH {path}"
        )

    def get_file(self, path: str, ref: str) -> bytes:
        status, body = self._request(
            "GET",
            self._api(f"/repos/{self.repository}/contents/{path}?ref={ref}"),
            accept="application/vnd.github.raw",
        )
        if status == 404:
            raise PublicationError(f"{path} does not exist on {ref}")
        if status < 200 or status >= 300:
            raise PublicationError(f"reading {path} on {ref} failed with HTTP {status}")
        return body

    def get_blob(self, sha: str) -> bytes:
        payload = self.get_json(f"/repos/{self.repository}/git/blobs/{sha}")
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            raise PublicationError("unexpected blob encoding")
        try:
            return base64.b64decode(payload["content"], validate=False)
        except (KeyError, ValueError) as exc:
            raise PublicationError("unexpected blob payload") from exc

    def branch_head(self, branch: str) -> str | None:
        """Return the current commit of a branch, or None when it does not exist."""
        head = self.get_optional_json(f"/repos/{self.repository}/git/ref/heads/{branch}")
        if head is None:
            return None
        sha = head.get("object", {}).get("sha") if isinstance(head, dict) else None
        if not isinstance(sha, str) or not sha:
            raise PublicationError("the channel branch has no commit")
        return sha

    def get_url_bytes(self, url: str, limit: int, label: str) -> bytes:
        if not isinstance(url, str) or not url.startswith("https://"):
            raise PublicationError(f"{label} must be an HTTPS URL")
        status, body = self._request(
            "GET", url, accept="application/octet-stream",
            authenticated=_is_github_host(url),
        )
        if status < 200 or status >= 300:
            raise PublicationError(f"{label} download failed with HTTP {status}")
        if len(body) > limit:
            raise PublicationError(f"{label} exceeds {limit} bytes")
        return body

    def stream_sha256(self, url: str, *, expected_bytes: int, label: str) -> str:
        """Hash one remote asset in bounded chunks without keeping it in memory."""
        if not isinstance(url, str) or not url.startswith("https://"):
            raise PublicationError(f"{label} must be an HTTPS URL")
        request = urllib.request.Request(url, method="GET")
        request.add_header("Accept", "application/octet-stream")
        request.add_header("User-Agent", "armada-update-publication")
        if _is_github_host(url):
            request.add_header("Authorization", f"Bearer {self._token}")
        hasher = hashlib.sha256()
        total = 0
        try:
            with open_request(request) as response:
                status = getattr(response, "status", 200)
                if status < 200 or status >= 300:
                    raise PublicationError(
                        f"{label} download failed with HTTP {status}")
                while True:
                    chunk = response.read(CHECKSUM_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected_bytes:
                        raise PublicationError(
                            f"{label} is larger than the verified package records")
                    hasher.update(chunk)
        except urllib.error.HTTPError as exc:
            raise PublicationError(
                f"{label} download failed with HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise PublicationError(
                f"{label} download failed: {type(exc).__name__}") from None
        if total != expected_bytes:
            raise PublicationError(
                f"{label} is {total} bytes, not the verified {expected_bytes}")
        return hasher.hexdigest()


def _files_unchanged(github, head: str, files: dict) -> bool:
    commit = github.get_json(f"/repos/{github.repository}/git/commits/{head}")
    tree_sha = commit.get("tree", {}).get("sha")
    if not isinstance(tree_sha, str):
        return False
    tree = github.get_json(f"/repos/{github.repository}/git/trees/{tree_sha}")
    entries = {
        entry["path"]: entry["sha"]
        for entry in tree.get("tree", [])
        if isinstance(entry, dict) and entry.get("type") == "blob"
    }
    for name, data in files.items():
        sha = entries.get(name)
        if sha is None or github.get_blob(sha) != data:
            return False
    return True


def commit_channel_files(github, files: dict, message: str, *,
                         branch: str = CHANNEL_BRANCH, attempts: int = 6) -> dict:
    """Commit the channel document and its signature as one non-forced update."""
    if not files:
        raise PublicationError("no channel files to commit")
    for name, data in files.items():
        if "/" in name or name.startswith(".") or not name.endswith((".json", ".json.sig")):
            raise PublicationError(f"refusing to commit unexpected path: {name}")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise PublicationError(f"{name} must be nonempty bytes")
    if not isinstance(message, str) or not message.strip():
        raise PublicationError("a commit message is required")

    for attempt in range(1, attempts + 1):
        head_sha = github.branch_head(branch)
        if head_sha is not None and _files_unchanged(github, head_sha, files):
            return {"state": "unchanged", "commit": head_sha, "branch": branch}

        entries = []
        for name in sorted(files):
            blob = github.post_json(
                f"/repos/{github.repository}/git/blobs",
                {
                    "content": base64.b64encode(bytes(files[name])).decode("ascii"),
                    "encoding": "base64",
                },
            )
            sha = blob.get("sha") if isinstance(blob, dict) else None
            if not isinstance(sha, str):
                raise PublicationError("blob creation returned no digest")
            entries.append({"path": name, "mode": "100644", "type": "blob", "sha": sha})
        tree_payload = {"tree": entries}
        if head_sha is not None:
            parent = github.get_json(
                f"/repos/{github.repository}/git/commits/{head_sha}")
            base_tree = parent.get("tree", {}).get("sha")
            if not isinstance(base_tree, str):
                raise PublicationError("the channel branch commit has no tree")
            tree_payload["base_tree"] = base_tree
        tree = github.post_json(f"/repos/{github.repository}/git/trees", tree_payload)
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(tree_sha, str):
            raise PublicationError("tree creation returned no digest")
        commit = github.post_json(
            f"/repos/{github.repository}/git/commits",
            {
                "message": message,
                "tree": tree_sha,
                "parents": [head_sha] if head_sha else [],
            },
        )
        commit_sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(commit_sha, str):
            raise PublicationError("commit creation returned no digest")
        try:
            if head_sha is None:
                github.post_json(
                    f"/repos/{github.repository}/git/refs",
                    {"ref": f"refs/heads/{branch}", "sha": commit_sha},
                )
            else:
                github.patch_json(
                    f"/repos/{github.repository}/git/refs/heads/{branch}",
                    {"sha": commit_sha, "force": False},
                )
            return {"state": "committed", "commit": commit_sha, "branch": branch}
        except GitHubConflict:
            if attempt == attempts:
                raise PublicationError(
                    "the channel branch kept moving; rerun publication"
                ) from None
            time.sleep(min(2 ** attempt, 15))
    raise PublicationError("the channel branch kept moving; rerun publication")


def fetch_published_channel(github, *, channel, trusted_key, expected_digest,
                            expected_revision, workdir, branch=CHANNEL_BRANCH) -> dict:
    """Authenticate the published channel and its signed package descriptor."""
    channel = _require_channel(channel)
    # Resolve the branch once so the document and its signature are read from
    # the same immutable commit; two branch reads could pair a new document
    # with an old signature.
    head = github.branch_head(branch)
    if head is None:
        raise PublicationError(f"the {branch} branch has no published channel yet")
    name = f"{channel}.json"
    document = github.get_file(name, head)
    signature = github.get_file(name + ".sig", head)
    if len(signature) > MAX_SIGNATURE_BYTES:
        raise PublicationError("channel signature exceeds the allowed size")
    try:
        channel_doc = load_channel(
            document, signature, expected_channel=channel, trusted_key=trusted_key
        )
    except CatalogError as exc:
        raise PublicationError(f"channel verification failed: {exc}") from exc

    # Only the authenticated document decides withdrawal, and a withdrawn
    # release must not reach descriptor downloads, the workdir, or a tag.
    if channel_doc["withdrawn"]:
        raise PublicationError(
            f"the published {channel} channel has been withdrawn; "
            "nothing may be promoted from it"
        )

    descriptor_url = channel_doc["packageDescriptor"]["url"]
    signature_url = channel_doc["packageDescriptor"]["signatureUrl"]
    descriptor = github.get_url_bytes(
        descriptor_url, MAX_DOCUMENT_BYTES, "package descriptor")
    descriptor_signature = github.get_url_bytes(
        signature_url, MAX_SIGNATURE_BYTES, "package descriptor signature")
    try:
        package = bind_package(
            channel_doc, descriptor, descriptor_signature, trusted_key=trusted_key
        )
    except CatalogError as exc:
        raise PublicationError(f"package binding failed: {exc}") from exc

    if channel_doc["ociDigest"] != expected_digest:
        raise PublicationError(
            "the published channel does not describe the resolved source digest"
        )
    if package["sourceRevision"] != expected_revision:
        raise PublicationError(
            "the published channel revision does not match the source image"
        )
    if "offline-upgrade" not in package["operations"]:
        raise PublicationError("the published package is not upgrade-capable")

    archive_name = package["archive"]["fileName"]
    target = Path(workdir)
    target.mkdir(parents=True, exist_ok=True)
    descriptor_path = target / (archive_name + ".update.json")
    signature_path = target / (archive_name + ".update.json.sig")
    descriptor_path.write_bytes(descriptor)
    signature_path.write_bytes(descriptor_signature)

    return {
        "channel": channel,
        "releaseId": package["releaseId"],
        "releaseTag": release_tag(package["releaseId"]),
        "version": package["version"],
        "sourceRevision": package["sourceRevision"],
        "ociDigest": package["ociDigest"],
        "ostreeCommit": package["ostreeCommit"],
        "supportedModels": package["supportedModels"],
        "descriptorPath": str(descriptor_path),
        "signaturePath": str(signature_path),
        "descriptorUrl": descriptor_url,
        "signatureUrl": signature_url,
        "publishedAt": channel_doc["publishedAt"],
    }


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise PublicationError("GITHUB_TOKEN is required")
    return token


def _write_json(path, payload) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(text)
        return
    Path(path).write_text(text, encoding="utf-8")


def _load_json(path: Path, label: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PublicationError(f"{label} is not readable JSON: {exc}") from exc


def command_verify_package(args) -> int:
    plan = verify_package_directory(args.directory, trusted_key=args.trusted_key)
    if args.run:
        run_info = _load_json(args.run, "run metadata")
        require_run_identity(run_info, plan)
        plan["run"] = run_info
    _write_json(args.out, plan)
    return 0


def command_verify_run(args) -> int:
    github = GitHub(args.repository, _token())
    run = github.get_json(f"/repos/{args.repository}/actions/runs/{args.run_id}")
    artifacts = github.get_json(
        f"/repos/{args.repository}/actions/runs/{args.run_id}/artifacts?per_page=100"
    )
    payload = verify_package_run(
        run, artifacts, run_id=args.run_id, repository=args.repository
    )
    _write_json(args.out, payload)
    return 0


def command_plan_release(args) -> int:
    plan = _load_json(args.plan, "package plan")
    urls = descriptor_urls(
        args.repository, plan["releaseTag"], plan["archive"]["fileName"])
    _write_json(args.out, urls)
    return 0


def find_release(github, tag: str):
    """Return the one release in the listing tagged ``tag``, or None.

    ``GET /releases/tags/{tag}`` is documented to return published releases
    only, so a draft GitHub already holds is missing there and every rerun
    would create yet another draft.  The list endpoint also returns drafts to a
    token with write access, and it is paged, so every page is read.  A tag
    held by more than one release (a published release plus a draft, or several
    drafts) is ambiguous and is never resumed as if it were one release.
    """
    found = None
    page = 1
    while True:
        listing = github.get_json(
            f"/repos/{github.repository}/releases"
            f"?per_page={RELEASE_PAGE_SIZE}&page={page}"
        )
        if not isinstance(listing, list):
            raise PublicationError("the release listing must be an array")
        for release in listing:
            if not isinstance(release, dict):
                raise PublicationError("release listing entries must be objects")
            if release.get("tag_name") != tag:
                continue
            if found is not None:
                raise PublicationError(
                    f"more than one release is tagged {tag}; refusing to resume "
                    "an ambiguous release"
                )
            found = release
        if len(listing) < RELEASE_PAGE_SIZE:
            return found
        page += 1


def command_release_view(args) -> int:
    tag = release_tag(args.tag)
    github = GitHub(args.repository, _token())
    # Drafts never appear in releases/tags/{tag}, so the listing is read in
    # full; only a listing that completed without this tag is "absent".
    payload = find_release(github, tag)
    if payload is None:
        result = {"present": False, "tag": tag}
    else:
        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise PublicationError("release metadata must list its assets")
        listed = []
        for asset in assets:
            if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
                raise PublicationError("release assets must be named objects")
            listed.append({
                "name": asset["name"],
                "size": asset.get("size"),
                "digest": asset.get("digest"),
                "state": asset.get("state"),
                "url": asset.get("url"),
                "browser_download_url": asset.get("browser_download_url"),
            })
        result = {
            "present": True,
            "tag": tag,
            "releaseId": payload.get("id"),
            "isDraft": payload.get("draft") is True,
            "targetCommitish": payload.get("target_commitish"),
            "assets": listed,
        }
    _write_json(args.out, result)
    return 0


def command_release_state(args) -> int:
    plan = _load_json(args.plan, "package plan")
    release = _load_json(args.release, "release metadata")
    hashes = None
    if args.verify_remote:
        if not args.repository:
            raise PublicationError("--verify-remote requires --repository")
        hashes = verify_release_assets(
            GitHub(args.repository, _token()), plan, release)
    _write_json(args.out, release_state(plan["assets"], release, hashes=hashes))
    return 0


def command_fetch_channel(args) -> int:
    github = GitHub(args.repository, _token())
    payload = fetch_published_channel(
        github,
        channel=args.channel,
        trusted_key=args.trusted_key,
        expected_digest=args.expected_oci_digest,
        expected_revision=args.expected_source_revision,
        workdir=args.workdir,
        branch=args.branch,
    )
    _write_json(args.out, payload)
    return 0


def command_commit_channel(args) -> int:
    channel = _require_channel(args.channel)
    document = _read_bounded(args.channel_file, MAX_DOCUMENT_BYTES, "channel document")
    signature = _read_bounded(
        args.channel_signature, MAX_SIGNATURE_BYTES, "channel signature")
    if not signature.strip():
        raise PublicationError("channel signature must not be empty")
    # The CLI contract is the same as the device: the document is only a
    # channel once its detached signature verifies against the trusted key.
    try:
        verified = load_channel(
            document, signature,
            expected_channel=channel, trusted_key=args.trusted_key,
        )
    except CatalogError as exc:
        raise PublicationError(f"channel verification failed: {exc}") from exc

    github = GitHub(args.repository, _token())
    message = (
        f"Publish {channel} channel: {verified['releaseId']} "
        f"({verified['ociDigest']})"
    )
    result = commit_channel_files(
        github,
        {f"{channel}.json": document, f"{channel}.json.sig": signature},
        message,
        branch=args.branch,
    )
    result["channel"] = channel
    result["releaseId"] = verified["releaseId"]
    result["ociDigest"] = verified["ociDigest"]
    _write_json(args.out, result)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    verify = commands.add_parser(
        "verify-package", help="verify a downloaded package artifact")
    verify.add_argument("--directory", type=Path, required=True)
    verify.add_argument("--trusted-key", type=Path, default=DEFAULT_TRUSTED_KEY)
    verify.add_argument("--run", type=Path, default=None)
    verify.add_argument("--out", type=Path, default=None)
    verify.set_defaults(handler=command_verify_package)

    run = commands.add_parser(
        "verify-run", help="verify a completed package build run")
    run.add_argument("--repository", required=True)
    run.add_argument("--run-id", type=int, required=True)
    run.add_argument("--out", type=Path, default=None)
    run.set_defaults(handler=command_verify_run)

    plan = commands.add_parser(
        "plan-release", help="derive immutable release URLs from a package plan")
    plan.add_argument("--plan", type=Path, required=True)
    plan.add_argument("--repository", required=True)
    plan.add_argument("--out", type=Path, default=None)
    plan.set_defaults(handler=command_plan_release)

    view = commands.add_parser(
        "release-view", help="read a release, distinguishing a missing release from an empty draft")
    view.add_argument("--repository", required=True)
    view.add_argument("--tag", required=True)
    view.add_argument("--out", type=Path, default=None)
    view.set_defaults(handler=command_release_view)

    state = commands.add_parser(
        "release-state", help="compare a verified package with an existing release")
    state.add_argument("--plan", type=Path, required=True)
    state.add_argument("--release", type=Path, required=True)
    state.add_argument("--repository", default=None)
    state.add_argument("--verify-remote", action="store_true")
    state.add_argument("--out", type=Path, default=None)
    state.set_defaults(handler=command_release_state)

    fetch = commands.add_parser(
        "fetch-channel", help="verify the published source channel for promotion")
    fetch.add_argument("--repository", required=True)
    fetch.add_argument("--channel", required=True, choices=CHANNELS)
    fetch.add_argument("--branch", default=CHANNEL_BRANCH)
    fetch.add_argument("--trusted-key", type=Path, default=DEFAULT_TRUSTED_KEY)
    fetch.add_argument("--expected-oci-digest", required=True)
    fetch.add_argument("--expected-source-revision", required=True)
    fetch.add_argument("--workdir", type=Path, required=True)
    fetch.add_argument("--out", type=Path, default=None)
    fetch.set_defaults(handler=command_fetch_channel)

    commit = commands.add_parser(
        "commit-channel", help="commit a signed channel document to its branch")
    commit.add_argument("--repository", required=True)
    commit.add_argument("--branch", default=CHANNEL_BRANCH)
    commit.add_argument("--channel", required=True, choices=CHANNELS)
    commit.add_argument("--channel-file", type=Path, required=True)
    commit.add_argument("--channel-signature", type=Path, required=True)
    commit.add_argument("--trusted-key", type=Path, default=DEFAULT_TRUSTED_KEY)
    commit.add_argument("--out", type=Path, default=None)
    commit.set_defaults(handler=command_commit_channel)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except (PublicationError, PackageError, CatalogError, OSError, ValueError) as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

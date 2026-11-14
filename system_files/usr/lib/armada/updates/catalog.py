"""Validation and compatibility checks for signed Freedom update channels."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import re
from urllib.parse import urlsplit

from .signature import DEFAULT_TRUSTED_KEY, SignatureError, load_signed_json


_CHANNELS = frozenset(("stable", "beta", "testing"))
_MODELS = frozenset(("TB322FC", "TB323FU", "TB710FU"))
_TOP_KEYS = frozenset((
    "schemaVersion", "kind", "product", "channel", "releaseId", "version",
    "sourceRevision", "ociDigest", "ostreeCommit", "supportedModels",
    "packageDescriptor", "minimumUpdaterVersion", "publishedAt", "withdrawn",
))
_DESCRIPTOR_KEYS = frozenset(("url", "signatureUrl", "bytes", "sha256"))
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z", re.ASCII)
_UPDATER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z", re.ASCII
)
_HEX40 = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class CatalogError(ValueError):
    """A channel document is invalid, untrusted, or incompatible."""


def load_channel(document: bytes, signature_b64: bytes, *, expected_channel: str,
                 trusted_key: Path = DEFAULT_TRUSTED_KEY) -> dict:
    try:
        data = load_signed_json(document, signature_b64, trusted_key=trusted_key)
    except SignatureError as exc:
        raise CatalogError(f"channel signature verification failed: {exc}") from exc
    return validate_channel(data, expected_channel)


def _identity(value, field: str) -> None:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise CatalogError(f"{field} must be a nonempty ASCII-safe token of at most 128 characters")
    if value in ("latest", "stable", "beta", "testing"):
        raise CatalogError(f"{field} must not be a floating channel name")


def _url(value, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise CatalogError(f"{field} must be an absolute HTTPS URL")
    if "#" in value or any(
        ch == "\\" or ch.isspace() or ord(ch) < 32 or 127 <= ord(ch) < 160
        for ch in value
    ):
        raise CatalogError(f"{field} contains a forbidden character or fragment")
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise CatalogError(f"{field} must be an absolute HTTPS URL with a hostname")
        if parsed.username is not None or parsed.password is not None:
            raise CatalogError(f"{field} must not contain credentials")
        parsed.port
    except ValueError as exc:
        raise CatalogError(f"{field} is malformed") from exc


def _updater_tuple(value, field: str) -> tuple:
    if not isinstance(value, str):
        raise CatalogError(f"{field} must be a canonical three-part version")
    match = _UPDATER.fullmatch(value)
    if not match:
        raise CatalogError(f"{field} must be a canonical three-part version")
    try:
        return tuple(int(part) for part in match.groups())
    except ValueError as exc:
        raise CatalogError(f"{field} must be a canonical three-part version") from exc


def validate_channel(data: dict, expected_channel: str) -> dict:
    if not isinstance(expected_channel, str) or expected_channel not in _CHANNELS:
        raise CatalogError("unknown expected channel")
    if not isinstance(data, dict) or set(data) != _TOP_KEYS:
        raise CatalogError("channel must contain exactly the schema 1 top-level keys")
    if type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1:
        raise CatalogError("schemaVersion must be integer 1")
    if data["kind"] != "freedom-channel" or data["product"] != "armadaos-freedom":
        raise CatalogError("channel kind or product is invalid")
    channel = data["channel"]
    if not isinstance(channel, str) or channel not in _CHANNELS or channel != expected_channel:
        raise CatalogError("channel does not match the expected channel")
    _identity(data["releaseId"], "releaseId")
    _identity(data["version"], "version")
    if not isinstance(data["sourceRevision"], str) or not _HEX40.fullmatch(data["sourceRevision"]):
        raise CatalogError("sourceRevision must be 40 lowercase hexadecimal characters")
    digest = data["ociDigest"]
    if not isinstance(digest, str) or not digest.startswith("sha256:") or not _HEX64.fullmatch(digest[7:]):
        raise CatalogError("ociDigest must be sha256 followed by 64 lowercase hexadecimal characters")
    if not isinstance(data["ostreeCommit"], str) or not _HEX64.fullmatch(data["ostreeCommit"]):
        raise CatalogError("ostreeCommit must be 64 lowercase hexadecimal characters")
    models = data["supportedModels"]
    if not isinstance(models, list) or not models:
        raise CatalogError("supportedModels must be a nonempty list")
    if any(type(model) is not str or model not in _MODELS for model in models):
        raise CatalogError("supportedModels contains an unsupported model")
    if len(set(models)) != len(models):
        raise CatalogError("supportedModels must not contain duplicates")
    descriptor = data["packageDescriptor"]
    if not isinstance(descriptor, dict) or set(descriptor) != _DESCRIPTOR_KEYS:
        raise CatalogError("packageDescriptor must contain exactly url, signatureUrl, bytes, and sha256")
    _url(descriptor["url"], "packageDescriptor.url")
    _url(descriptor["signatureUrl"], "packageDescriptor.signatureUrl")
    if descriptor["url"] == descriptor["signatureUrl"]:
        raise CatalogError("package descriptor and signature URLs must differ")
    if type(descriptor["bytes"]) is not int or not 1 <= descriptor["bytes"] <= 2 * 1024 * 1024:
        raise CatalogError("packageDescriptor.bytes must be between 1 and 2 MiB")
    if not isinstance(descriptor["sha256"], str) or not _HEX64.fullmatch(descriptor["sha256"]):
        raise CatalogError("packageDescriptor.sha256 must be 64 lowercase hexadecimal characters")
    _updater_tuple(data["minimumUpdaterVersion"], "minimumUpdaterVersion")
    published = data["publishedAt"]
    try:
        if not isinstance(published, str):
            raise ValueError
        parsed = datetime.fromisoformat(published[:-1] + "+00:00" if published.endswith("Z") else published)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
    except ValueError as exc:
        raise CatalogError("publishedAt must be ISO 8601 with an explicit timezone") from exc
    if type(data["withdrawn"]) is not bool:
        raise CatalogError("withdrawn must be a boolean")
    return deepcopy(data)


def require_compatible(channel: dict, model: str, updater_version: str) -> None:
    if not isinstance(channel, dict):
        raise CatalogError("channel must be an object")
    validated = validate_channel(channel, channel.get("channel"))
    if validated["withdrawn"]:
        raise CatalogError("channel release has been withdrawn")
    if model not in validated["supportedModels"]:
        raise CatalogError("model is not supported by this channel")
    current = _updater_tuple(updater_version, "updater_version")
    minimum = _updater_tuple(validated["minimumUpdaterVersion"], "minimumUpdaterVersion")
    if current < minimum:
        raise CatalogError("updater version is older than minimumUpdaterVersion")

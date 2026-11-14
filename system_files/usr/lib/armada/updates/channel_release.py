"""Promote verified package identities to immutable update channels."""

from hashlib import sha256

from .catalog import CatalogError, validate_channel
from .package_contract import PackageError, load_package
from .signature import DEFAULT_TRUSTED_KEY


_IDENTITY_FIELDS = (
    "releaseId", "version", "sourceRevision", "ociDigest", "ostreeCommit",
    "minimumUpdaterVersion",
)


def _upgrade_package(document, signature, trusted_key):
    try:
        package = load_package(document, signature, trusted_key=trusted_key)
    except PackageError as exc:
        raise CatalogError(f"package verification failed: {exc}") from exc
    if "offline-upgrade" not in package["operations"]:
        raise CatalogError("package does not declare the offline-upgrade operation")
    return package


def make_channel(document: bytes, signature: bytes, *, channel: str,
                 descriptor_url: str, signature_url: str, published_at: str,
                 expected_oci_digest: str, withdrawn: bool = False,
                 trusted_key=DEFAULT_TRUSTED_KEY) -> dict:
    package = _upgrade_package(document, signature, trusted_key)
    if not isinstance(expected_oci_digest, str) or not expected_oci_digest:
        raise CatalogError("expected_oci_digest must be a nonempty string")
    if package["ociDigest"] != expected_oci_digest:
        raise CatalogError("package OCI digest does not match expected_oci_digest")

    channel_document = {
        "schemaVersion": 1,
        "kind": "freedom-channel",
        "product": "armadaos-freedom",
        "channel": channel,
        "releaseId": package["releaseId"],
        "version": package["version"],
        "sourceRevision": package["sourceRevision"],
        "ociDigest": package["ociDigest"],
        "ostreeCommit": package["ostreeCommit"],
        "supportedModels": package["supportedModels"],
        "packageDescriptor": {
            "url": descriptor_url,
            "signatureUrl": signature_url,
            "bytes": len(document),
            "sha256": sha256(document).hexdigest(),
        },
        "minimumUpdaterVersion": package["minimumUpdaterVersion"],
        "publishedAt": published_at,
        "withdrawn": withdrawn,
    }
    return validate_channel(channel_document, channel)


def bind_package(channel_document: dict, document: bytes, signature: bytes, *,
                 trusted_key=DEFAULT_TRUSTED_KEY) -> dict:
    if not isinstance(channel_document, dict):
        raise CatalogError("channel must be an object")
    channel = validate_channel(channel_document, channel_document.get("channel"))
    if not isinstance(document, (bytes, bytearray)):
        raise CatalogError("package document must be original bytes or bytearray")

    descriptor = channel["packageDescriptor"]
    if len(document) != descriptor["bytes"]:
        raise CatalogError("package descriptor byte length does not match the channel")
    if sha256(document).hexdigest() != descriptor["sha256"]:
        raise CatalogError("package descriptor digest does not match the channel")

    package = _upgrade_package(document, signature, trusted_key)
    if any(package[field] != channel[field] for field in _IDENTITY_FIELDS):
        raise CatalogError("package identity does not match the channel")
    if set(package["supportedModels"]) != set(channel["supportedModels"]):
        raise CatalogError("package supportedModels do not match the channel")
    return package

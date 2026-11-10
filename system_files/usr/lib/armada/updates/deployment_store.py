"""Internal low-level OSTree deployment preparation.

Appends a caller-authenticated, previously imported target commit to an existing
sysroot without making it default, removing prior deployments, seeding /var/home,
or formatting storage. Caller holds /run/lock/armada-update.lock for the full
transaction; this module also acquires and releases the OSTree Sysroot try_lock.
"""
import json
import re
from pathlib import Path

from .boot_context import _read_regular
from .boot_payload import verify_manifest

_MAX_MANIFEST_BYTES = 1024 * 1024
_UINT32_MAX = 0xFFFFFFFF
_BINDING_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]{0,63})/([0-9a-f]{64})\.(0|[1-9][0-9]{0,9})")
_ORIGIN_RE = re.compile(
    r"ostree-image-signed:docker://ghcr\.io/armadaos-freedom/armadaos-freedom:"
    r"(stable|beta|testing)@(sha256:[0-9a-f]{64})"
)


class DeploymentError(Exception):
    pass


def _gi():
    import gi
    gi.require_version("OSTree", "1.0")
    from gi.repository import Gio, GLib, OSTree
    return Gio, GLib, OSTree


def _require_str(value, name):
    if not isinstance(value, str):
        raise DeploymentError(f"{name} must be str")
    return value


def _parse_binding(binding):
    _require_str(binding, "current_binding")
    m = _BINDING_RE.fullmatch(binding)
    if not m:
        raise DeploymentError(f"malformed current_binding: {binding!r}")
    serial = int(m.group(3))
    if serial > _UINT32_MAX:
        raise DeploymentError(f"current_binding serial out of uint32 range: {serial}")
    return m.group(1), m.group(2), serial


def _binding_of(dep):
    return f"{dep.get_osname()}/{dep.get_csum()}.{dep.get_deployserial()}"


def _validate_origin(origin_ref, oci_digest):
    _require_str(origin_ref, "origin_ref")
    m = _ORIGIN_RE.fullmatch(origin_ref)
    if not m:
        raise DeploymentError(f"invalid origin_ref: {origin_ref!r}")
    if m.group(2) != oci_digest:
        raise DeploymentError("origin_ref digest does not match oci_digest")


def _require_nonsymlink_dir(root, *parts):
    cur = root
    for part in parts:
        cur = cur / part
        if cur.is_symlink() or not cur.is_dir():
            raise DeploymentError(f"expected non-symlink directory: {cur}")
    try:
        resolved = cur.resolve(strict=True)
    except OSError as exc:
        raise DeploymentError(f"cannot resolve directory {cur}: {exc}") from exc
    if resolved != cur:
        raise DeploymentError(f"non-canonical directory path: {cur}")


def _candidate_path(sysroot, root, dep, osname, commit):
    serial = dep.get_deployserial()
    if type(serial) is not int or serial < 0 or serial > _UINT32_MAX:
        raise DeploymentError(f"invalid deployment serial: {serial!r}")
    if dep.get_osname() != osname or dep.get_csum() != commit:
        raise DeploymentError("deployment identity mismatch")
    gfile = sysroot.get_deployment_directory(dep)
    raw = gfile.get_path() if gfile is not None else None
    if not isinstance(raw, str) or not raw:
        raise DeploymentError("sysroot returned empty deployment path")
    parts = ("ostree", "deploy", osname, "deploy", f"{commit}.{serial}")
    expected = root.joinpath(*parts)
    actual = Path(raw)
    if actual != expected:
        raise DeploymentError(f"unexpected deployment path: {raw!r} != {str(expected)!r}")
    _require_nonsymlink_dir(root, *parts)
    return str(expected)


def _load_manifest(dep_root, model):
    p = Path(dep_root) / "usr" / "share" / "armada" / "update-boot" / "manifest.json"
    try:
        raw = _read_regular(p, _MAX_MANIFEST_BYTES)
    except Exception as exc:
        raise DeploymentError(f"invalid manifest file {p}: {exc}") from exc
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentError(f"manifest not UTF-8 JSON: {exc}") from exc
    try:
        verified = verify_manifest(dep_root, model, doc)
    except Exception as exc:
        raise DeploymentError(f"manifest verification failed: {exc}") from exc
    if not isinstance(verified, dict) or not isinstance(verified.get("kernelRelease"), str) or not verified["kernelRelease"]:
        raise DeploymentError("verified manifest missing kernelRelease")
    return verified


def _validate_commit_metadata(commit_variant, oci_digest, source_revision, version):
    meta = commit_variant.unpack()[0]
    if not isinstance(meta, dict) or meta.get("ostree.manifest-digest") != oci_digest:
        raise DeploymentError("commit manifest-digest != oci_digest")
    img_cfg_raw = meta.get("ostree.container.image-config")
    if isinstance(img_cfg_raw, str):
        try:
            img = json.loads(img_cfg_raw)
        except json.JSONDecodeError as exc:
            raise DeploymentError(f"invalid image-config JSON: {exc}") from exc
    elif isinstance(img_cfg_raw, dict):
        img = img_cfg_raw
    else:
        raise DeploymentError("missing ostree.container.image-config")
    if not isinstance(img, dict) or img.get("architecture") != "arm64" or img.get("os") != "linux":
        raise DeploymentError("image-config architecture/os mismatch")
    cfg = img.get("config")
    labels = cfg.get("Labels") if isinstance(cfg, dict) else None
    if not isinstance(labels, dict):
        raise DeploymentError("image-config missing Labels")
    if labels.get("org.opencontainers.image.revision") != source_revision:
        raise DeploymentError("image revision label mismatch")
    if labels.get("org.opencontainers.image.version") != version:
        raise DeploymentError("image version label mismatch")


def _ensure_pinned(sysroot, dep, label):
    if not dep.is_pinned():
        if not sysroot.deployment_set_pinned(dep, True):
            raise DeploymentError(f"failed pinning {label} deployment")


def _candidate_options(dep):
    bootcfg = dep.get_bootconfig()
    if bootcfg is None:
        raise DeploymentError("candidate deployment missing bootconfig")
    options = bootcfg.get("options")
    if not isinstance(options, str) or not options:
        raise DeploymentError("candidate bootconfig options missing or empty")
    if len(options) > 64 * 1024:
        raise DeploymentError("candidate bootconfig options exceed 64KiB")
    for ch in options:
        code = ord(ch)
        if ch in ("'", '"', "\\") or code > 0x7E or (code < 0x20 and ch not in (" ", "\t")):
            raise DeploymentError(f"candidate options contain invalid character: {ch!r}")
    tokens = [tok for tok in re.split(r"[ \t]+", options) if tok]
    if not tokens:
        raise DeploymentError("candidate bootconfig options contain no tokens")
    return tokens


def _binding_tokens(tokens):
    values = []
    for tok in tokens:
        if tok == "armada.deployment":
            raise DeploymentError("candidate options contain bare armada.deployment")
        if tok.startswith("armada.deployment="):
            values.append(tok[len("armada.deployment="):])
    if len(values) > 1:
        raise DeploymentError("candidate options contain duplicate armada.deployment tokens")
    return values


def _ensure_candidate_binding(sysroot, dep, source_binding):
    target_binding = _binding_of(dep)
    tokens = _candidate_options(dep)
    values = _binding_tokens(tokens)
    if values and values[0] not in (source_binding, target_binding):
        raise DeploymentError(f"candidate has unexpected armada.deployment: {values[0]!r}")
    new_kargs = [tok for tok in tokens if not tok.startswith("armada.deployment=")]
    new_kargs.append(f"armada.deployment={target_binding}")
    if tokens == new_kargs:
        return
    if not sysroot.deployment_set_kargs(dep, new_kargs, None):
        raise DeploymentError("failed setting candidate armada.deployment binding")


def _check_candidate_binding(dep, expected_binding):
    values = _binding_tokens(_candidate_options(dep))
    if len(values) != 1 or values[0] != expected_binding:
        raise DeploymentError("candidate armada.deployment binding not persisted exactly once")


def append_verified_deployment(*, sysroot_path, current_binding, target_commit, oci_digest,
                               source_revision, version, model, origin_ref):
    Gio, GLib, OSTree = _gi()
    osname, cur_commit, cur_serial = _parse_binding(current_binding)
    _require_str(target_commit, "target_commit")
    _require_str(oci_digest, "oci_digest")
    _require_str(source_revision, "source_revision")
    _require_str(version, "version")
    _require_str(model, "model")
    if not re.fullmatch(r"[0-9a-f]{64}", target_commit):
        raise DeploymentError("target_commit must be 64 lower hex")
    if target_commit == cur_commit:
        raise DeploymentError("target commit equals current commit")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", oci_digest):
        raise DeploymentError("oci_digest must be sha256:64lowerhex")
    if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise DeploymentError("source_revision must be 40 lower hex")
    if not version or version.strip() != version or version == "latest":
        raise DeploymentError("version must be trimmed, non-empty, and not 'latest'")
    if not model or model.strip() != model:
        raise DeploymentError("model must be a non-empty trimmed string")
    _validate_origin(origin_ref, oci_digest)

    if sysroot_path is None or not isinstance(sysroot_path, (str, Path)):
        raise DeploymentError("sysroot_path must be str or Path")
    raw_root = Path(sysroot_path)
    if not str(raw_root) or not raw_root.exists() or not raw_root.is_dir():
        raise DeploymentError(f"sysroot directory does not exist: {raw_root}")
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise DeploymentError(f"cannot resolve sysroot_path: {exc}") from exc
    if not root.is_dir():
        raise DeploymentError(f"resolved sysroot is not a directory: {root}")
    _require_nonsymlink_dir(root, "ostree", "repo")
    _require_nonsymlink_dir(root, "boot")

    sysroot = OSTree.Sysroot.new(Gio.File.new_for_path(str(root)))
    if not sysroot.initialize_with_mount_namespace(None):
        raise DeploymentError("sysroot initialize_with_mount_namespace failed")
    ok, acquired = sysroot.try_lock()
    if not (ok and acquired):
        raise DeploymentError("sysroot busy: try_lock not acquired")
    try:
        if not sysroot.load(None):
            raise DeploymentError("sysroot load failed")
        ok, repo = sysroot.get_repo(None)
        if not ok or repo is None:
            raise DeploymentError("no repo in sysroot")
        ok, commit_variant, state = repo.load_commit(target_commit)
        if not ok or commit_variant is None:
            raise DeploymentError(f"target commit not in repo: {target_commit}")
        if int(state) != 0:
            raise DeploymentError("target commit is partial/metadata-only")
        _validate_commit_metadata(commit_variant, oci_digest, source_revision, version)

        deployments = list(sysroot.get_deployments() or [])
        cur_matches = [d for d in deployments if _binding_of(d) == current_binding]
        if len(cur_matches) != 1:
            raise DeploymentError("current binding not found exactly once")
        current = cur_matches[0]
        if current.get_deployserial() != cur_serial:
            raise DeploymentError("current deployment serial mismatch")
        if current.get_unlocked() != OSTree.DeploymentUnlockedState.NONE:
            raise DeploymentError("current deployment unlocked")
        _candidate_path(sysroot, root, current, osname, cur_commit)
        original_bindings = [_binding_of(d) for d in deployments]

        matches = [d for d in deployments if d.get_csum() == target_commit and d.get_osname() == osname]
        if matches:
            if len(matches) != 1:
                raise DeploymentError("ambiguous existing target deployments")
            td = matches[0]
            origin_kf = td.get_origin()
            if origin_kf is None:
                raise DeploymentError("existing target deployment missing origin")
            try:
                existing_ref = origin_kf.get_string("origin", "container-image-reference")
            except Exception as exc:
                raise DeploymentError(f"failed reading existing target origin: {exc}") from exc
            if existing_ref != origin_ref:
                raise DeploymentError("existing target origin mismatch")
            cpath = _candidate_path(sysroot, root, td, osname, target_commit)
            verified = _load_manifest(cpath, model)
            _ensure_pinned(sysroot, current, "current")
            _ensure_pinned(sysroot, td, "candidate")
            _ensure_candidate_binding(sysroot, td, current_binding)
            if not sysroot.load(None):
                raise DeploymentError("sysroot reload after reuse pin/binding failed")
            reread_deps = list(sysroot.get_deployments() or [])
            if [_binding_of(d) for d in reread_deps] != original_bindings:
                raise DeploymentError("deployment bindings changed unexpectedly on reuse")
            by_binding = {_binding_of(d): d for d in reread_deps}
            if not by_binding[current_binding].is_pinned() or not by_binding[_binding_of(td)].is_pinned():
                raise DeploymentError("pinned state not persisted on reuse")
            _check_candidate_binding(by_binding[_binding_of(td)], _binding_of(td))
            return {"deployment": _binding_of(td), "path": cpath,
                    "kernelRelease": verified["kernelRelease"], "model": model,
                    "reused": True, "retainedDeployments": original_bindings}

        _ensure_pinned(sysroot, current, "current")
        origin = GLib.KeyFile()
        origin.set_string("origin", "container-image-reference", origin_ref)
        ok, cand = sysroot.deploy_tree(osname, target_commit, origin, current, None, None)
        if not ok or cand is None:
            raise DeploymentError("deploy_tree failed")
        cpath = _candidate_path(sysroot, root, cand, osname, target_commit)
        verified = _load_manifest(cpath, model)
        cand_binding = _binding_of(cand)
        expected = original_bindings + [cand_binding]
        if not sysroot.write_deployments(deployments + [cand], None):
            raise DeploymentError("write_deployments failed")
        if not sysroot.load(None):
            raise DeploymentError("sysroot reload after write_deployments failed")
        reread_deps = list(sysroot.get_deployments() or [])
        if [_binding_of(d) for d in reread_deps] != expected:
            raise DeploymentError("post-write deployment bindings mismatch")
        reg_cand = [d for d in reread_deps if _binding_of(d) == cand_binding]
        if len(reg_cand) != 1:
            raise DeploymentError("registered candidate deployment not found uniquely")
        _ensure_pinned(sysroot, reg_cand[0], "candidate")
        _ensure_candidate_binding(sysroot, reg_cand[0], current_binding)
        if not sysroot.load(None):
            raise DeploymentError("sysroot reload after candidate pin/binding failed")
        final_deps = list(sysroot.get_deployments() or [])
        if [_binding_of(d) for d in final_deps] != expected:
            raise DeploymentError("final deployment bindings mismatch")
        by_binding = {_binding_of(d): d for d in final_deps}
        if not by_binding[current_binding].is_pinned() or not by_binding[cand_binding].is_pinned():
            raise DeploymentError("pinned state not persisted")
        _check_candidate_binding(by_binding[cand_binding], cand_binding)
        return {"deployment": cand_binding, "path": cpath,
                "kernelRelease": verified["kernelRelease"], "model": model,
                "reused": False, "retainedDeployments": original_bindings}
    finally:
        sysroot.unlock()


def promote_confirmed_deployment(*, sysroot_path, current_binding, previous_binding):
    Gio, GLib, OSTree = _gi()
    osname, cur_commit, cur_serial = _parse_binding(current_binding)
    prev_osname, prev_commit, prev_serial = _parse_binding(previous_binding)
    if prev_osname != osname:
        raise DeploymentError("previous_binding osname does not match current_binding")
    if current_binding == previous_binding or cur_commit == prev_commit:
        raise DeploymentError("current_binding and previous_binding must be distinct")

    if sysroot_path is None or not isinstance(sysroot_path, (str, Path)):
        raise DeploymentError("sysroot_path must be str or Path")
    raw_root = Path(sysroot_path)
    if not str(raw_root) or not raw_root.is_absolute() or raw_root.is_symlink() or not raw_root.exists() or not raw_root.is_dir():
        raise DeploymentError(f"sysroot directory invalid: {raw_root}")
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise DeploymentError(f"cannot resolve sysroot_path: {exc}") from exc
    if root != raw_root or not root.is_dir():
        raise DeploymentError(f"non-canonical sysroot_path: {raw_root}")
    _require_nonsymlink_dir(root, "ostree", "repo")
    _require_nonsymlink_dir(root, "boot")

    sysroot = OSTree.Sysroot.new(Gio.File.new_for_path(str(root)))
    if not sysroot.initialize_with_mount_namespace(None):
        raise DeploymentError("sysroot initialize_with_mount_namespace failed")
    ok, acquired = sysroot.try_lock()
    if not (ok and acquired):
        raise DeploymentError("sysroot busy: try_lock not acquired")
    try:
        if not sysroot.load(None):
            raise DeploymentError("sysroot load failed")
        if sysroot.get_staged_deployment() is not None:
            raise DeploymentError("staged deployment present in sysroot")

        deployments = list(sysroot.get_deployments() or [])
        cur_matches = [d for d in deployments if _binding_of(d) == current_binding]
        if len(cur_matches) != 1:
            raise DeploymentError("current deployment not found uniquely")
        prev_matches = [d for d in deployments if _binding_of(d) == previous_binding]
        if len(prev_matches) != 1:
            raise DeploymentError("previous deployment not found uniquely")

        current = cur_matches[0]
        previous = prev_matches[0]
        if current.get_deployserial() != cur_serial or previous.get_deployserial() != prev_serial:
            raise DeploymentError("deployment serial mismatch")
        if (current.get_unlocked() != OSTree.DeploymentUnlockedState.NONE or
                previous.get_unlocked() != OSTree.DeploymentUnlockedState.NONE):
            raise DeploymentError("deployment unlocked state is not NONE")

        _candidate_path(sysroot, root, current, osname, cur_commit)
        _candidate_path(sysroot, root, previous, osname, prev_commit)
        _check_candidate_binding(current, current_binding)
        _ensure_pinned(sysroot, current, "current")
        _ensure_pinned(sysroot, previous, "previous")

        expected_deps = [current] + [d for d in deployments if _binding_of(d) != current_binding]
        expected_bindings = [_binding_of(d) for d in expected_deps]
        changed = [_binding_of(d) for d in deployments] != expected_bindings
        if changed:
            if not sysroot.write_deployments(expected_deps, None):
                raise DeploymentError("write_deployments failed")

        if not sysroot.load(None):
            raise DeploymentError("sysroot reload failed")
        final_deps = list(sysroot.get_deployments() or [])
        if [_binding_of(d) for d in final_deps] != expected_bindings:
            raise DeploymentError("post-promotion deployment order mismatch")
        final_cur = [d for d in final_deps if _binding_of(d) == current_binding]
        final_prev = [d for d in final_deps if _binding_of(d) == previous_binding]
        if len(final_cur) != 1 or len(final_prev) != 1:
            raise DeploymentError("post-promotion deployments not unique")
        if not final_cur[0].is_pinned() or not final_prev[0].is_pinned():
            raise DeploymentError("pinned state not persisted")
        _check_candidate_binding(final_cur[0], current_binding)
        return {
            "deployment": current_binding,
            "previousDeployment": previous_binding,
            "changed": changed,
        }
    finally:
        sysroot.unlock()

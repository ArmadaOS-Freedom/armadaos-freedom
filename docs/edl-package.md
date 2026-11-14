# EDL package build

**English** · [简体中文](edl-package.zh-CN.md)

The EDL package is the 9008 deployment path: it carries the raw images,
kernels, device trees, and metadata that the Windows installer writes over
Qualcomm 9008 (EDL) mode. It is built as a separate artifact from the flashable
SD card image, and it does not replace the existing SD image path.

## Requirements

- A Linux host with Docker, where containers may use loop devices and
  `SYS_ADMIN` for filesystem mounts, with btrfs support on the host.
- Python 3 on the host. The wrapper runs a host helper before it invokes
  Docker.
- `just` and `git`, with submodules checked out recursively.
- Free disk space for the raw images, their ZIP volumes, the pulled container
  image, and the container images the build creates locally.

```console
$ git submodule update --init --recursive
```

## Build a package

```console
$ just build-edl-package \
    docker://ghcr.io/armadaos-freedom/armadaos-freedom:testing \
    sha256:DIGEST \
    FULL_SOURCE_COMMIT \
    EXACT_OCI_VERSION
```

| Argument | Value |
|---|---|
| `IMAGE` | Registry reference that includes a real accepted channel (`stable`, `beta`, or `testing`), e.g. `docker://ghcr.io/armadaos-freedom/armadaos-freedom:testing`. A `docker://` prefix is accepted but not required, because the wrapper also takes a bare registry reference. The build derives the signed image origin from the channel, and `staging` cannot derive an accepted signed origin. |
| `DIGEST` | Exact `sha256:` OCI manifest digest of that reference, supplied separately from the tag. |
| `REVISION` | Full 40-character lowercase commit SHA the image was built from. |
| `VERSION` | Exact `org.opencontainers.image.version` label value of that image. |

All four arguments must describe one build. The checked-out `HEAD` must equal
`REVISION`, the digest must resolve to the requested image, and the image's
`org.opencontainers.image.revision` and `org.opencontainers.image.version`
labels must match `REVISION` and `VERSION`. The build stops on a mismatch.
A channel name or tag is not a version, and an older commit may not be paired
with a newer image.

The recipe takes these four positional arguments and forwards no model or
configuration options, so it packages one artifact containing every profile
registered in `build_files/edl-devices.json`. That single package installs all
of them, including `TB710FU`, and its descriptor also declares
`offline-upgrade` for the selected models with the dual-slot backend (the
Lenovo Legion Y700 models). It writes the package to `output/edl/<version>/`
and uses a fresh `_build-edl.*` work directory under the checkout, which is
ignored by Git.

To package a subset, or to use another configuration file, call the wrapper
directly with the same four values:

```console
$ bash /PATH/TO/repo/build_files/build-edl-package.sh \
    --oci docker://ghcr.io/armadaos-freedom/armadaos-freedom:testing \
    --manifest-digest sha256:DIGEST \
    --source-commit FULL_SOURCE_COMMIT \
    --version EXACT_OCI_VERSION \
    --source /PATH/TO/repo \
    --devices-config /PATH/TO/repo/build_files/edl-devices.json \
    --work /PATH/TO/edl-work \
    --output /PATH/TO/edl-output \
    --model TB322FC
```

`--model` is repeatable, and only the named profiles are selected; without it
every profile in the configuration is packaged. `--devices-config` defaults to
`build_files/edl-devices.json` in the source tree.

`--work` and `--output` must be absolute and must be new or empty. Neither may
be the source root or contain the source tree, and the two must not be equal or
nested inside each other. Fresh directories inside the source tree are allowed,
which is how the `just` recipe uses `_build-edl.*` and `output/edl` there. The
full ZIP stays in the work directory; only the split volumes, their checksums,
and the package README are written to the output directory.

The OCI image contains the model runtimes built when the system image is built.
EDL packaging reuses the embedded payload and verifies its manifest, kernel,
initramfs, DTBs, and configuration. Each selected model needs one runtime. If
`--runtime MODEL=ABSOLUTE_FILE` is given, it must cover every selected model
exactly once, and each supplied runtime must have exactly the same hash and
length as the embedded runtime; it cannot override that runtime. Images lacking
the required manifest or components fail packaging and must be rebuilt from
matching source and image inputs.

## Package a locally built image

An image built with `just build` carries its version label and a revision
label, the commit checked out when it was built. Package it with

```console
$ just build-edl-package-local ORIGIN [IMAGE] [TAG]
```

`ORIGIN` is the signed origin the installed system updates from. A local
image has no channel to derive it from, and the wrapper only accepts
`ostree-image-signed:docker://ghcr.io/armadaos-freedom/armadaos-freedom:` followed by
`stable`, `beta` or `testing` (optionally `@sha256:...`). `IMAGE` and `TAG`
default to the image `just build` writes. The recipe reads the version and
revision labels with `skopeo`, copies the image from local container storage
into a temporary OCI layout (the producer reads it as `oci:<dir>:latest`, and
the copy, several GB, is removed afterwards), and calls `build-edl-package`
with the layout, its manifest digest, the labels and `ORIGIN`. As there, the
checked-out `HEAD` must be the commit the image was built from, and every
registered profile is packaged. An image without the labels, for example one
built by a plain `podman build`, is rejected before anything is copied.

Local hosts differ from the CI runner in a few ways:

- `skopeo` must be installed. The wrapper runs `docker` with
  `--cap-add SYS_ADMIN` and loop devices; on a podman host, a `docker` command
  that calls **rootful** podman (for example `podman-docker` in a rootful
  podman machine) works.
- The producer needs two unused `/dev/loopN` devices and stops with "two
  unused /dev/loopN devices are required ...; found N" otherwise. A minimal VM
  may only have `/dev/loop-control`; loop devices can be added through it, for
  example
  `sudo python3 -c 'import fcntl,os; fd=os.open("/dev/loop-control",os.O_RDWR); [fcntl.ioctl(fd,0x4C80,i) for i in range(4)]'`
  (`LOOP_CTL_ADD` for loop0-3).
- On an SELinux host (for example a Fedora CoreOS podman machine), the
  bind-mounted paths (source, work, and any `--devices-config` or `--runtime`
  files) either need the `container_file_t` label, or the containers need to
  run with `label = false`. Importing the image into ostree also writes the
  image's own `security.selinux` attributes: the producer container needs
  `CAP_MAC_ADMIN` for that (otherwise `EACCES`), and while the host enforces a
  policy that does not know those labels the write still fails with
  `Invalid argument`, so put the host in permissive mode (`setenforce 0`) for
  the build. A `containers.conf` override with `label = false` and
  `MAC_ADMIN` added to `default_capabilities` covers the container side; set
  `CONTAINERS_CONF_OVERRIDE` only for this command, since it applies to every
  container.

## Device configuration

`build_files/edl-devices.json` uses `schemaVersion` 1, with one global
`runtimeSource` and a `profiles` object keyed by model ID:

| Field | Meaning |
|---|---|
| `schemaVersion` | Must be `1`. |
| `runtimeSource` | Source-tree-relative path of the runtime snapshot shared by every profile. |
| `profiles` | Model ID to profile. Registered model IDs select profiles; the pipeline has no fixed model list. |
| `displayName` | Human-readable model name recorded in the package README. |
| `compatible` | `soc` and `device` compatibility strings written into the per-model manifest. |
| `dtb` | Device tree path, relative to the deployed image's DTB directory. |
| `required` | Required DTB compatibility strings. |
| `forbidden` | Excluded DTB compatibility strings. |
| `extraArgs` | Extra kernel arguments for that model's normal boot line. |
| `runtimeConfig` | Runtime build configuration, relative to the runtime snapshot. |
| `runtimeCmakeArgs` | CMake arguments used to build that model's runtime. |
| `bootPartitionBytes` | Declared boot partition size recorded in the model manifest. |
| `diagnosticMode` | `rd-break` or `verbose`. |

Packaging an adapted device still requires working Linux kernel support and a
matching device tree in the deployed image, a runtime profile in the runtime
snapshot, and a matching model registration in the Windows installer. A
configuration entry alone does not make arbitrary Android hardware supported.
A profile outside the shipped package contract can be packaged for first
installation only; the build never advertises offline-upgrade or recovery for
it, and selecting such a profile together with any other models makes the
whole package install-only.

## Bundled plugin sources

Plugin versions are decided when the OS image is built, not when an EDL package
is packaged. Every supported OS image build — the `just build` recipe, the
`build_push` image job, and the PR image build — first resolves all bundled
plugins from the catalogue in `decky/plugin-sources.json`:

- release entries resolve to the latest public release and its single matching
  asset, verified against the announced SHA-256 digest; no pinned older binary
  is used.
- git entries resolve the configured branch's current tip to one immutable
  40-character commit SHA and build that exact checkout.
- in-tree entries use the package in the source tree.

That resolution writes `decky/bundled-plugins/plugins.lock.json`,
`plugins.resolved.json`, and `assets/`, covering one selection. These are
generated artifacts, not committed old binaries. A source that cannot be
resolved, or an asset whose digest does not verify, fails the build; there is no
fallback to an older cache or an older lock.

The selection is fixed for that image build and for the resulting OCI image.
EDL packaging consumes that frozen image: it does not resolve plugins again and
does not silently refresh the bundled contents later.

## Continuous builds

The build also runs automatically. On `main`, the `build_edl_package` job in
[`Build images`](../.github/workflows/build.yml) follows the container image
job and builds one EDL package from the image it just pushed, using the
`testing` channel, that image's digest, its actual source revision, and its
`org.opencontainers.image.version` label. The artifact is
`edl-package-<run id>`. It contains every registered profile, including
`TB710FU`, and its signed descriptor declares both `install` and
`offline-upgrade`: installation is available for every listed model, while an
offline-upgrade request is accepted only for the selected models with the
dual-slot backend. This is the official artifact, and **Publish update
channel** accepts it as before. The descriptor is signed on the same runner
that built it, so there is only one package to sign.

The artifact stays in the workflow run until an operator publishes the
release; no release and no update channel changes automatically. The SD image
path remains available; its automatic R2 publication is enabled separately as
described below.

[`Build EDL package`](../.github/workflows/build-edl-package.yml) can also be
started by hand. It takes the same four values as the recipe, checks out the
requested revision with submodules, and runs the same all-profile build,
uploading the split package, `parts.json`, `SHA256SUMS`, and the package
README under `edl-package-<run id>`.

## Publish an update channel

The image ships `/etc/armada/updates/channels.json`, which points every channel
at the signed documents on the `update-channels` branch:
`https://raw.githubusercontent.com/ArmadaOS-Freedom/armadaos-freedom/update-channels/<channel>.json`
and its `.json.sig`. All three channels are listed so the service can select
them, but a channel stays unavailable until an operator publishes it: the
device fetches that document, verifies its signature, and has no fallback to
another channel or source.

The flow is build, publish testing, then promote.

1. Build the package: `Build images` on `main`, or `Build EDL package` by hand.
2. Run **Publish update channel** with the completed run ID and `testing`. It
   checks that the run belongs to this repository, that it completed
   successfully, and that it produced `edl-package-<run id>`. It then
   verifies the descriptor signature, the split parts, the parts manifest, the
   `offline-upgrade` capability, and that the OCI digest resolves to the signed
   image built from the descriptor's source revision. It creates the draft
   release `<releaseId>`, uploads the split parts, the parts manifest, the
   descriptor, and the descriptor signature, publishes the release only after
   every asset is present, and only then signs and commits `testing.json` with
   `testing.json.sig`.
3. Run **Promote release** with `source: testing` and `target: beta` or
   `stable`. Promotion first verifies the published source channel and its
   descriptor signature, then takes the immutable OCI digest recorded in that
   channel, inspects the image at that digest, and requires its revision label
   and Cosign signature to match before writing any tag. The moving `:testing`
   tag is never read, so a newer build that re-pointed the source tag does not
   block promoting the older release the channel still names. The target
   channel then points at the same release assets. Nothing is rebuilt, and a
   source channel without a valid signed publication fails before any tag is
   written.

Nothing is replaced in place. A published release must match the verified
package exactly, while an existing draft — even an empty one — is completed:
only the missing assets are uploaded, and an asset that is already present is
hashed against the signed plan rather than replaced. A release whose content
differs fails the run. The channel document and its signature reach the
`update-channels` branch as one commit per channel; updates are serialized and
never force-pushed.

Before a release is created, resumed, or published, its Git tag is bound to the
verified `SOURCE_REVISION`. An existing tag is resolved to the commit it peels
to, including an annotated tag; a missing tag counts as absent only when GitHub
returns a genuine 404, never on an authentication, permission, or server error.
A draft that has no tag yet is bound through its `target_commitish`, resolved
to an exact commit SHA. Any mismatch stops the run before an asset or the
release is written, and the tag target is never changed.

Publishing a build from the current commit keeps using the workflow's
`GITHUB_TOKEN`. For a historical build the release tag targets a commit that
can differ from the workflow run; GitHub's create-release documentation states
that `GITHUB_TOKEN` cannot create such a release when that commit changes
`.github/workflows`, because it never holds Workflows write. The official REST
update-a-release documentation imposes the same requirement when an existing
historical draft is published. The optional `ARMADA_RELEASE_TOKEN` secret is
used for that historical `gh release create` and for both
`gh release edit --draft=false` paths that publish a draft. When it is
configured, those steps use it; when it is not, they attempt `GITHUB_TOKEN`,
which may succeed where GitHub permits it. If GitHub denies that permission and
the workflow contents differ between the tag target and the run, a dedicated
token with **Contents** and **Workflows** write is required. Checkout, artifact
download, verification, release reads, asset upload, and channel publication
all keep `GITHUB_TOKEN` without retargeting. The tag target is always the
verified build revision — never the current branch head.

## Package layout

The package uses `schemaVersion` 2 with deployment kind `edl-raw-v1`:

- The boot and root raw images, kernel, and initramfs are stored once and shared
  by every selected model.
- Each model keeps its own runtime plus normal and diagnostic DTB boot images,
  and its own manifest.
- Sharing the payloads does not make boot images interchangeable between
  models; keep using the matching per-model images.

The completed ZIP is split into raw byte volumes of a fixed 1900 MiB, without
recompression, named `<name>.zip.001`, `<name>.zip.002`, and so on, with the
last volume shorter. `<name>.parts.json` and `SHA256SUMS` record the volumes and
their checksums as distribution integrity records. Each volume stays below
2 GiB so it can be a release asset; that is a property of the package, not a
limit of the Actions artifact upload.

## Diagnostic boot modes

Each profile declares one diagnostic mode:

- `rd-break` appends `rd.break=pre-mount` to the normal boot arguments.
- `verbose` removes `quiet`, every `loglevel=` switch, and every `rd.break`
  variant, preserves the other normal arguments including board extras, and
  ensures `loglevel=8 ignore_loglevel printk.time=1 initcall_debug rd.debug`
  are present, appending missing flags in this order. Its output carries no
  `rd.break`.

The Gen 4 profile (TB322FC) uses `rd-break`; the Gen 5 profile (TB323FU) uses
`verbose` and supplies its board arguments through `extraArgs`. The Xiaoxin Pad
Pro GT profile (TB710FU) uses `rd-break` and passes `clk_ignore_unused
pd_ignore_unused regulator_ignore_unused` through `extraArgs`: Linux then never
switches off the clocks, power domains and regulators the bootloader left on,
including those of the display it is still scanning out.

`runtimeCmakeArgs` is a per-profile source option. The Gen 5 profile passes
`-DKSHIM_TB323FU_ANDROID_TOUCH_4M8_TRIAL=ON`, which limits the original Android
touch controller to 4.8 MHz at handoff; the Gen 4 and TB710FU profiles pass
`OFF`, and the CMake default is `OFF`.

## Flashing with the Windows installer

Put every volume of a package in the same folder, without renaming them, and
select the `.zip.001` file in the matching installer; it reads the continuous
numbered volumes from that folder. The installer does not read `parts.json`.

A package records its source as provenance: one exact nonzero lowercase
40-character commit SHA, read without trimming or normalizing. The
[Windows installer](https://github.com/ArmadaOS-Freedom/armadaos-freedom-installer)
validates that provenance and selects the matching model before writing.

The installer creates the tablet-specific 2 GiB `ARMADA_BACKUP` image from
verified local recovery material. It is not part of the shared Linux payload.
Version 0.7.4 creates recovery images with the v3 settings area in the final
64 KiB; support for reading or saving settings remains model-specific. See the
[recovery image contract](https://github.com/ArmadaOS-Freedom/armadaos-freedom-installer/blob/main/docs/package-contract.md).

The legacy SD card image and its manual installation flow remain available.

## Publication configuration

Official images use `ghcr.io/armadaos-freedom/armadaos-freedom`. The
`Build images` workflow publishes a signed `testing` image from `main`;
`Promote release` verifies the same digest before assigning `beta` or
`stable`. Configure the encrypted Cosign key as `SIGNING_SECRET` and its
password as `SIGNING_PASSWORD`. Its matching public key is shipped at
`system_files/etc/pki/containers/armadaos-freedom.pub`. Keep private keys
outside the source tree.

GHCR packages must be public and grant this repository Actions access. The
workflow checks anonymous image access before publishing channel tags. A
built package stays in its `edl-package-<run-id>` artifact until **Publish
update channel** turns it into a release and a signed `testing` channel; beta
and stable follow only through **Promote release**. The shipped
`system_files/etc/armada/updates/channels.json` names all three channels, and
each becomes usable only once its document exists on the `update-channels`
branch. Legacy disk-image publication to R2 is opt-in: set the repository
variable `ARMADA_PUBLISH_DISK=true` only after configuring the R2 endpoint,
bucket and credentials.

Windows installer 0.7.32 and later accepts the organization namespace and
pre-transfer packages from the old `mydanyi` namespace. Upgrade the installer
before using a new organization package; older installers reject that unknown
origin. New packages and update targets use the organization namespace;
historical packages are not silently relabeled.

## Signed package descriptors

The build wrapper creates a .zip.update.json sidecar from the completed archive
and its split files. It binds the release identity, source revision, OCI digest,
OSTree commit, supported models, payload paths, compressed and expanded sizes,
and SHA-256 digests. Shared system files are listed once.

The local wrapper leaves this descriptor unsigned. The EDL Actions workflow
signs its original UTF-8 bytes with the release key, verifies the result with
the fixed public key, and uploads .zip.update.json.sig alongside the volumes.
It signs the one package the workflow builds, and before upload it checks that
the descriptor's `supportedModels` exactly match the configured profiles and
that the descriptor declares `install` and `offline-upgrade`. SHA256SUMS and
parts.json do not replace this signature. A downloaded public key supplied by
the package cannot establish trust in that package.

The producer checks that the deployed image carries the normal-Linux updater
and then declares both `install` and `offline-upgrade` for the all-profile
package; a package built from an image without that updater fails the build
instead of being relabeled. The operation list is package-level: every listed
model can be installed, while an offline-upgrade request is accepted only for
a selected model with the dual-slot backend. A package whose selected models
include a profile outside the shipped contract is built install-only: the
wrapper never requests offline-upgrade for it, and it passes that configured
model identifier explicitly so validation accepts the install package while
still rejecting offline-upgrade and recovery for it. The offline upgrade keeps
user data and writes both boot slots from the verified payload; the online OCI
update instead rotates only the non-current slot. A signature alone does not
make a package upgrade-capable: the descriptor must declare offline-upgrade,
the requested model must be eligible, and the installed updater must meet
`minimumUpdaterVersion`.

Offline upgrade is currently supported only on the Lenovo Legion Y700 Gen 4
(TB322FC) and Gen 5 (TB323FU). The Lenovo Xiaoxin Pad Pro GT (TB710FU) is part
of the same all-profile package for installation, but its update slot backend
is not implemented yet; being listed in the package is not offline-upgrade
support for that model, and an upgrade request naming it is rejected before
any slot work.

Recovery manifests describe the RAM rescue path and are not a prerequisite here:
this package ships no rescue artifacts, and the object may be absent or partial.
Every entry that is present is still validated against the same schema and file
records. The channel generator refuses install-only descriptors.

.github/scripts/make-update-channel.py prepares an unsigned channel document
from an already signed upgrade-capable descriptor. Promotion retains the same
release, source, OCI, OSTree and package identities; it does not rebuild the
system. Its verify command authenticates both documents and checks their
binding. Generating or verifying a document does not publish a channel or
retag an image: publishing signs that document with `SIGNING_SECRET` and
commits it with its signature, as described above.

## Tests

```console
$ just test-edl-package
```

This runs the EDL filesystem tests and needs Docker with privileged access to
loop devices and btrfs. It is kept separate from `just test`.

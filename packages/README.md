# packages

Upstream-derived packages for Armada. Each top-level directory is one
component: a pinned upstream (`BASE.env`) + `patches/` + a `build.sh`.
`PATCHES.md` records where each patch came from.

## How a package builds

`build.sh` runs **inside** the builder container, not on the host. It sees:

| path    | contents                                                    |
| ------- | ----------------------------------------------------------- |
| `/work` | the package directory, cwd, artifacts go to `./out`          |
| `/src`  | this directory: `toolchain.env`, `TERRA.env`, sibling packages |

`Containerfile` turns each one into a pair of stages: `pkg-<name>`, which runs
`build.sh` as a `RUN` step, and `out-<name>`, a scratch stage holding just the
artifacts in the layout the image build bind-mounts at `/packages/<name>`.
Running the build as a step is what lets it cache as a layer, so an unchanged
package is not rebuilt.

## Cache resolution

Every package is addressed by a `package-hash.sh` content hash. Local lookups and the
published registries use the same recorded build inputs; only matching hashes
are reused:

1. local image `localhost/armada/pkg/<name>:<hash>`
2. primary registry `ARMADA_PKG_REGISTRY` (default
   `ghcr.io/armadaos-freedom/armada/pkg`)
3. fallback registry `ARMADA_PKG_FALLBACK_REGISTRY` (default
   `ghcr.io/armada-os/armada/pkg`); set it to an empty string to disable the
   fallback entirely

`packages/resolve-package-ref.sh` performs the registry lookups with `skopeo`,
so `skopeo` must be installed locally. It distinguishes "absent" (exit 3, keep
looking / build) from "denied" or network errors (nonzero, abort) so a private
package or outage cannot be silently mistaken for a cache miss.

## Building one

From the repository root:

```bash
just package gamescope
```

That builds the stage and tags it `localhost/armada/pkg/gamescope:<hash>`,
where the hash comes from `package-hash.sh`, the same tag CI publishes under.
Run `just package NAME` explicitly for a missing or changed package. `just build` prefers a locally built package and reuses the
published image for every other one, so you only ever build what you are
changing. `just packages-status` shows which is which. `just packages` builds
all packages (opt-in, slow).

To iterate inside the builder instead, a shell-level edit/retry loop without
going through the layer cache:

```bash
./build-local.sh gamescope
```

It wraps `build.sh` in the same `podman run` the stages replaced, with the same
`/work` and `/src`, and leaves output in `gamescope/out/`.

## CI

`.github/workflows/packages.yml` resolves each package's content hash first and
builds only the ones with no published image, one per job so they stay parallel
across runners. Most commits change no package, so the matrix is usually empty.
Each build publishes `ghcr.io/<owner>/armada/pkg/<name>:<hash>`, which the image
build resolves by the same hash. Layer cache lives in a per-package GHCR repo
under `armada/buildcache/`, so one package's cache can be expired or purged
without disturbing the others.

The PR workflow reads registries anonymously. If neither registry resolves a
reference and a lookup failed (denied, timeout, network), the PR stops instead
of treating that error as a cache miss. Only a confirmed hash miss schedules a
build. The publishing workflow authenticates to upload packages, then separately
checks anonymous access to every resulting reference. There is no stale or `latest` reuse: only the exact
content hash is ever consulted.

Most packages build natively on `ubuntu-24.04-arm`. `mesa-android`,
`lepton` and `mesa-x86` build on `ubuntu-24.04`: they
cross-compile Android arm64 and x86 targets from an x86_64 host, and emulating
that would be far too slow. Only the runner differs, but the stages are built
the same way. On Apple Silicon, rebuilding these x86-hosted stages can require slow emulation;
using an existing package image avoids that compilation.

Rebuilding a package still needs network access even for a pinned Fedora base,
since fetching sources happens inside the build.

Changes to shared inputs such as `packages/Containerfile`,
`packages/toolchain.env`, or `packages/scrub-scratch.sh` invalidate every package
hash. A new cache set then needs to be published; older tags are not substitutes.

### Enabling package CI on a fork

1. Repository settings -> Actions -> General: enable Actions (forks often
   start with workflows disabled).
2. Run the `Packages` workflow once initially so packages start publishing;
   permissions are documented at
   https://docs.github.com/en/packages/learn-github-packages/about-permissions-for-github-packages#about-scopes-and-permissions-for-package-registries
3. GHCR packages default to **private**, independently of the repository's
   visibility. For each required package under `armada/pkg`: Package settings -> Change
   visibility -> Public, then rerun the workflow. The separate
   `armada/buildcache` layer caches are optional; make those public too if PR
   builders should reuse them.
4. Run `Build images` after the package verification passes. Its published
   `armadaos-freedom` container image must also be Public for anonymous pulls.
   If its first upload is private, change the package visibility and rerun.

Publishing a repository does not publish its container packages. See
[GitHub's package visibility guide](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility).
No package or image settings are changed automatically by these workflows.

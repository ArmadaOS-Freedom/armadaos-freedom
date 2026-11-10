#!/usr/bin/env bash
# Generate, sign, verify, and commit one signed Freedom update channel.
#
# Inputs (environment):
#   CHANNEL               stable | beta | testing
#   DESCRIPTOR            verified *.zip.update.json path
#   DESCRIPTOR_SIGNATURE  matching *.zip.update.json.sig path
#   DESCRIPTOR_URL        immutable release URL of the descriptor
#   SIGNATURE_URL         immutable release URL of the descriptor signature
#   EXPECTED_OCI_DIGEST   digest recorded in the descriptor
#   TRUSTED_KEY           public key that signs descriptors and channels
#   REPOSITORY            ArmadaOS-Freedom/armadaos-freedom
#   GITHUB_TOKEN          token used to commit to the channel branch
#   COSIGN_PRIVATE_KEY    signing key, supplied by the caller from secrets
#   COSIGN_PASSWORD       signing key password
#   CHANNEL_BRANCH        defaults to update-channels
#   WORK_DIR              scratch directory (defaults to a fresh temp dir)
#
# The commit contains the channel document and its signature together. The
# channel only changes after the release it points at is public and complete,
# which the caller must already have established.
set -euo pipefail

: "${CHANNEL:?CHANNEL is required}"
: "${DESCRIPTOR:?DESCRIPTOR is required}"
: "${DESCRIPTOR_SIGNATURE:?DESCRIPTOR_SIGNATURE is required}"
: "${DESCRIPTOR_URL:?DESCRIPTOR_URL is required}"
: "${SIGNATURE_URL:?SIGNATURE_URL is required}"
: "${EXPECTED_OCI_DIGEST:?EXPECTED_OCI_DIGEST is required}"
: "${TRUSTED_KEY:?TRUSTED_KEY is required}"
: "${REPOSITORY:?REPOSITORY is required}"
: "${GITHUB_TOKEN:?GITHUB_TOKEN is required}"
: "${COSIGN_PRIVATE_KEY:?COSIGN_PRIVATE_KEY is required}"
: "${COSIGN_PASSWORD:?COSIGN_PASSWORD is required}"
: "${CHANNEL_BRANCH:=update-channels}"

work_dir="${WORK_DIR:-$(mktemp -d)}"
mkdir -p "${work_dir}"
channel_file="${work_dir}/${CHANNEL}.json"
channel_signature="${channel_file}.sig"
published_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

python3 .github/scripts/make-update-channel.py make \
  --descriptor "${DESCRIPTOR}" \
  --descriptor-signature "${DESCRIPTOR_SIGNATURE}" \
  --trusted-key "${TRUSTED_KEY}" \
  --channel "${CHANNEL}" \
  --descriptor-url "${DESCRIPTOR_URL}" \
  --signature-url "${SIGNATURE_URL}" \
  --published-at "${published_at}" \
  --expected-oci-digest "${EXPECTED_OCI_DIGEST}" \
  --output "${channel_file}"

cosign sign-blob \
  --key env://COSIGN_PRIVATE_KEY \
  --tlog-upload=false \
  --output-signature "${channel_signature}" \
  "${channel_file}"

# Verify the document and its binding before it can reach the channel branch.
python3 .github/scripts/make-update-channel.py verify \
  --channel-file "${channel_file}" \
  --channel-signature "${channel_signature}" \
  --descriptor "${DESCRIPTOR}" \
  --descriptor-signature "${DESCRIPTOR_SIGNATURE}" \
  --trusted-key "${TRUSTED_KEY}" \
  --expected-channel "${CHANNEL}"

python3 .github/scripts/update-publication.py commit-channel \
  --repository "${REPOSITORY}" \
  --branch "${CHANNEL_BRANCH}" \
  --channel "${CHANNEL}" \
  --channel-file "${channel_file}" \
  --channel-signature "${channel_signature}" \
  --trusted-key "${TRUSTED_KEY}"

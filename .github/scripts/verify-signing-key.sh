#!/usr/bin/env bash
# Prove the private signing key is the counterpart of the public key devices
# trust, without a transparency log, registry, or network access.
#
# Inputs (environment):
#   COSIGN_PRIVATE_KEY         private key, read via env:// only
#   COSIGN_PASSWORD            password for that key
#   ARMADA_SIGNING_PUBLIC_KEY  trusted public key, PEM
#
# cosign derives the public half from the private key and writes only that
# public key to a private temporary file. openssl re-encodes both it and the
# trusted key as DER SubjectPublicKeyInfo, and cmp compares the bytes. No
# signature is created, so --tlog-upload / Rekor never enters the picture and
# no Rekor entry is required. The private key is never written to disk or
# printed.
set -euo pipefail

: "${COSIGN_PRIVATE_KEY:?COSIGN_PRIVATE_KEY is required}"
: "${COSIGN_PASSWORD:?COSIGN_PASSWORD is required}"
: "${ARMADA_SIGNING_PUBLIC_KEY:?ARMADA_SIGNING_PUBLIC_KEY is required}"

if [ ! -f "${ARMADA_SIGNING_PUBLIC_KEY}" ] || [ ! -s "${ARMADA_SIGNING_PUBLIC_KEY}" ]; then
  echo "::error::Freedom signing public key missing or empty: ${ARMADA_SIGNING_PUBLIC_KEY}"
  exit 1
fi

for tool in cosign openssl cmp mktemp; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "::error::${tool} is required to prove the signing key but was not found."
    exit 1
  fi
done

umask 077
workdir="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/armada-signing-key.XXXXXX")"
cleanup() { rm -rf -- "${workdir}"; }
trap cleanup EXIT HUP INT TERM
chmod 700 "${workdir}"

derived_public="${workdir}/derived.pub"
derived_der="${workdir}/derived.der"
trusted_der="${workdir}/trusted.der"

if ! cosign public-key --key env://COSIGN_PRIVATE_KEY \
    --outfile "${derived_public}" >/dev/null; then
  echo "::error::cosign could not derive a public key from COSIGN_PRIVATE_KEY. Check that SIGNING_SECRET is a cosign private key and SIGNING_PASSWORD decrypts it."
  exit 1
fi
chmod 600 "${derived_public}"

if ! openssl pkey -pubin -in "${derived_public}" -outform DER \
    -out "${derived_der}"; then
  echo "::error::The public key derived from COSIGN_PRIVATE_KEY is not a PEM public key openssl can read."
  exit 1
fi
if ! openssl pkey -pubin -in "${ARMADA_SIGNING_PUBLIC_KEY}" -outform DER \
    -out "${trusted_der}"; then
  echo "::error::The trusted public key is not a PEM public key openssl can read: ${ARMADA_SIGNING_PUBLIC_KEY}"
  exit 1
fi

for der in "${derived_der}" "${trusted_der}"; do
  if [ ! -s "${der}" ]; then
    echo "::error::Public key canonicalization produced an empty file: ${der}"
    exit 1
  fi
  chmod 600 "${der}"
done

if ! cmp -s "${derived_der}" "${trusted_der}"; then
  echo "::error::COSIGN_PRIVATE_KEY is not the counterpart of the trusted public key ${ARMADA_SIGNING_PUBLIC_KEY}. Nothing was signed or published."
  exit 1
fi

echo "Signing key matches the trusted Freedom public key."

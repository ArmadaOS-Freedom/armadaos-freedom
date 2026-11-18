# TB323FU CDSP firmware

Signed vendor CDSP firmware for the Lenovo TB323FU (SM8850, kaanapali, board
baldur), joined from this unit's own signed vendor `.mdt`/`.bXX` split images
with the upstream linux-msm `pil-squasher`; the original segment bytes and
vendor signatures are preserved, and nothing here is re-signed.

- `qcom/kaanapali/lenovo/baldur/cdsp.mbn` - CDSP image
- `qcom/kaanapali/lenovo/baldur/cdsp_dtb.mbn` - CDSP device tree image
- `SHA256SUMS` - checksums of the two files above

`build_files/40-vendor-system-files.sh` verifies `SHA256SUMS` and installs the
bundle at `/usr/share/tb323fu/firmware/compute/`. At boot
`tb323fu-cdsp-prepare.service` copies both files into the kernel firmware
search path and starts the CDSP, on the exact `lenovo,baldur` device tree only.

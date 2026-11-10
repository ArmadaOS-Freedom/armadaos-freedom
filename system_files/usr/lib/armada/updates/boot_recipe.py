DIAGNOSTIC_VERBOSE_TAIL = (
    "loglevel=8",
    "ignore_loglevel",
    "printk.time=1",
    "initcall_debug",
    "rd.debug",
)


def model_boot_args(normal, profile):
    tokens = normal.split()
    for extra in profile["extraArgs"]:
        if extra in ("ostree", "armada.deployment") or extra.startswith(
            ("ostree=", "armada.deployment=")
        ):
            raise ValueError("profile extraArgs must not introduce ostree or deployment bindings")
        key = extra.split("=", 1)[0]
        present = [token for token in tokens if token.split("=", 1)[0] == key]
        if present:
            if any(token != extra for token in present):
                raise ValueError(f"deployment already passes a conflicting {key}= option")
            continue
        tokens.append(extra)
    return " ".join(tokens)


def diagnostic_boot_args(normal, profile):
    """Return the diagnostic kernel command line for one profile mode.

    rd-break simply appends the pre-mount break. verbose keeps every normal
    token (including duplicates and original order) except the quiet switch and
    all loglevel=/rd.break switches, then appends the missing verbose tail in
    its fixed order.
    """
    mode = profile["diagnosticMode"]
    if mode == "rd-break":
        return normal + " rd.break=pre-mount"
    if mode != "verbose":
        raise ValueError(f"unsupported diagnosticMode {mode!r}")
    tokens = []
    for token in normal.split():
        if token == "quiet":
            continue
        if token.startswith("loglevel="):
            continue
        if token == "rd.break" or token.startswith("rd.break="):
            continue
        tokens.append(token)
    for token in DIAGNOSTIC_VERBOSE_TAIL:
        if token not in tokens:
            tokens.append(token)
    return " ".join(tokens)

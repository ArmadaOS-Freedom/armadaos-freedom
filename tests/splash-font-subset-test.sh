#!/usr/bin/env bash
# Subset-font regression for armada-splash's generated splash font.
#
# Fixture-based test: it exercises the generator against the real system TTC
# and asserts the deterministic subset is single-face, keeps printable ASCII
# plus every localized catalog codepoint, fails on a required glyph the source
# face cannot supply, and propagates a new catalog Chinese character into the
# subset. It needs fontTools, a C compiler and a real Noto Sans CJK TTC. When
# no TTC is found it prints SKIP (so CI without the fonts package is honest
# rather than inventing a pass).
#
# Usage:
#   tests/splash-font-subset-test.sh [<fixture-dir>]
#
# If <fixture-dir> is omitted the test searches %{_datadir} and
# /usr/share/fonts for the canonical Noto Sans CJK Regular TTC. If none is
# found it prints SKIP (so CI without the fonts package is honest rather
# than inventing a pass).
#
# Every object file, binary, subset and log lands under a task-controlled
# output directory: $SPLASH_FONT_SUBSET_OUT if set, otherwise
# $TMPDIR/build-subset when TMPDIR is defined, otherwise
# tests/build-subset inside this checkout.

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/packages/armada-splash/src"
GEN="$ROOT/packages/armada-splash/scripts/gen-splash-font.py"
HARNESS="$ROOT/packages/armada-splash/tests/font-test.c"
EN="$SRC/locales/en.h"
ZH="$SRC/locales/zh-CN.h"

fixture_dir="${1:-}"
cjk_font=""
if [[ -n "$fixture_dir" ]]; then
    cjk_font="$fixture_dir/NotoSansCJK-Regular.ttc"
    [[ -f "$cjk_font" ]] || { echo "FAIL: fixture not found: $cjk_font" >&2; exit 1; }
else
    for candidate in \
        /usr/share/fonts/google-noto-sans-cjk-fonts/NotoSansCJK-Regular.ttc \
        /usr/share/fonts/noto-sans-cjk/NotoSansCJK-Regular.ttc \
        /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc ; do
        if [[ -f "$candidate" ]]; then
            cjk_font="$candidate"
            break
        fi
    done
    if [[ -z "$cjk_font" ]]; then
        echo "SKIP: no Noto Sans CJK Regular TTC found; pass a fixture directory"
        exit 0
    fi
fi

[[ -f "$GEN" ]] || { echo "FAIL: generator not found: $GEN" >&2; exit 1; }
[[ -f "$HARNESS" ]] || { echo "FAIL: harness not found: $HARNESS" >&2; exit 1; }
[[ -f "$SRC/stb_impl.c" ]] || { echo "FAIL: missing $SRC/stb_impl.c" >&2; exit 1; }
[[ -f "$EN" ]] || { echo "FAIL: missing $EN" >&2; exit 1; }
[[ -f "$ZH" ]] || { echo "FAIL: missing $ZH" >&2; exit 1; }

if ! python3 -c 'import fontTools' 2>/dev/null; then
    echo "SKIP: python3-fonttools not installed; cannot generate or verify subset"
    exit 0
fi

OUT="${SPLASH_FONT_SUBSET_OUT:-${TMPDIR:-$ROOT/tests}/build-subset}"
mkdir -p "$OUT"

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

CC="${CC:-cc}"
command -v "$CC" >/dev/null 2>&1 || fail "no C compiler ($CC)"

drm_flags=(-ldrm)
if command -v pkg-config >/dev/null 2>&1 && pkg-config --exists libdrm 2>/dev/null; then
    read -r -a drm_flags <<<"$(pkg-config --cflags --libs libdrm)"
fi

"$CC" -O2 -w -c "$SRC/stb_impl.c" -o "$OUT/stb_impl.o" \
    || fail "stb_impl.c failed to compile"
"$CC" -O2 -Wall -Wextra -I"$SRC" "$HARNESS" "$SRC/splash-i18n.c" "$OUT/stb_impl.o" \
    "${drm_flags[@]}" -lm \
    -o "$OUT/font-test" || fail "font-test.c failed to compile or link"

run_gen() { # <out> <source> <extra-catalog-or-empty>
    local out="$1" src="$2" extra="$3"
    local -a args=(--source "$src" --output "$out" --catalog "$EN" --catalog "$ZH")
    if [[ -n "$extra" ]]; then args+=(--catalog "$extra"); fi
    python3 "$GEN" "${args[@]}"
}

# 1. Deterministic pair: two runs must be byte-identical.
run_gen "$OUT/font-a.ttf" "$cjk_font" "" || fail "generator run A failed"
run_gen "$OUT/font-b.ttf" "$cjk_font" "" || fail "generator run B failed"
if ! cmp -s "$OUT/font-a.ttf" "$OUT/font-b.ttf"; then
    fail "two generator runs differ (subset is not deterministic)"
fi
printf 'PASS: subset font is deterministic across two runs\n'

# 2. The generated file is a single-face TTF, not a TTC (magic 0x00010000 or
#    \"true\"), and the C harness can load it via load_font() in a new mode.
if ! "$OUT/font-test" ttf       "$OUT/font-a.ttf" >"$OUT/ttf.log" 2>&1; then
    sed 's/^/    /' "$OUT/ttf.log" >&2
    fail "generated subset is not accepted by load_font()"
fi
printf 'PASS: generated subset loads as single-face TTF through load_font()\n'

# 3. Coverage: every codepoint used by ASCII + both locales is present.
#    The generator fails if a required non-whitespace codepoint is missing, so a
#    successful run above already asserts coverage. Additional confirmation
#    via the harness's subset mode: subset mode rasterizes real catalog text
#    (both English and Chinese keys, no arbitrary Chinese).
if ! "$OUT/font-test" subset "$OUT/font-a.ttf" >"$OUT/subset.log" 2>&1; then
    sed 's/^/    /' "$OUT/subset.log" >&2
    fail "subset mode failed (ASCII or catalog coverage regression)"
fi
printf 'PASS: generated subset covers ASCII and localized catalog text\n'

# 4. Missing glyph fails: the source TTC has every Chinese glyph the shipping
#    catalogs use, so we force a hole by feeding a synthetic catalog whose
#    string encodes U+10FFFF (outside any font's repertoire). The C literal
#    decoder is expected to turn \xF4\x8F\xBF\xBF into that codepoint.
cat >"$OUT/missing-catalog.h" <<'HDR'
static const SplashMsg SPLASH_TEST_MISSING[] = {
    { "test.missing", "\xF4\x8F\xBF\xBF" },
};
HDR
if run_gen "$OUT/font-missing.ttf" "$cjk_font" "$OUT/missing-catalog.h" 2>"$OUT/missing.log"; then
    fail "generator accepted a catalog containing an unsupported codepoint"
fi
printf 'PASS: generator fails when a required codepoint is missing\n'

# 5. Addition propagation: append a real catalog Chinese character that is not
#    used by either shipping header, regenerate, and confirm it appears in the
#    subset's cmap. The added character is a Simplified Chinese character in
#    the same block the catalogs use (U+5B57 '字' = character/word).
if grep -q '字' "$EN" "$ZH"; then
    fail "probe character U+5B57 already appears in a shipping catalog; pick another"
fi
cat >"$OUT/add-catalog.h" <<'HDR'
static const SplashMsg SPLASH_TEST_ADD[] = {
    { "test.add", "\xE5\xAD\x97" },
};
HDR
run_gen "$OUT/font-add.ttf" "$cjk_font" "$OUT/add-catalog.h" \
    || fail "generator failed for added-character catalog"
python3 - "$OUT/font-add.ttf" <<'PY' || fail "added character U+5B57 not present in regenerated subset"
import sys
from fontTools.ttLib import TTFont
f = TTFont(sys.argv[1], lazy=True)
covered = set()
for t in f['cmap'].tables:
    covered.update(t.cmap.keys())
if 0x5B57 not in covered:
    sys.exit(1)
PY
# Also assert the shipping-only subset did NOT include it, proving the test
# would catch a regression if the generator started ignoring catalogs.
python3 - "$OUT/font-a.ttf" <<'PY' || { echo "FAIL: shipping subset unexpectedly contains probe character" >&2; exit 1; }
import sys
from fontTools.ttLib import TTFont
f = TTFont(sys.argv[1], lazy=True)
covered = set()
for t in f['cmap'].tables:
    covered.update(t.cmap.keys())
if 0x5B57 in covered:
    sys.exit(1)
PY
printf 'PASS: added catalog Chinese character propagates into the subset\n'

printf 'splash subset font test passed\n'

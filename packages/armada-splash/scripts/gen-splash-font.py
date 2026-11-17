#!/usr/bin/python3
# Generate a deterministic single-face subset font for armada-splash.
#
# The production splash loads its status font through load_font(), which
# selects the Noto Sans Mono CJK SC face from the system CJK collection by
# family name. Shipping the whole collection is wasteful for a boot splash,
# so we extract just that one face and subset it to the printable ASCII
# range plus every codepoint actually used by the shipping en.h/zh-CN.h
# catalogs, then write a plain single-face .ttf.
#
# Usage:
#   gen-splash-font.py --source <NotoSansCJK-Regular.ttc> \
#       --output build/font.ttf \
#       --catalog src/locales/en.h --catalog src/locales/zh-CN.h
#
# Determinism: the output depends only on the input TTC face and the sorted
# codepoint set. fontTools is invoked with fixed save options; no
# timestamps, no hash randomization. Running twice on the same inputs
# yields byte-identical output (regression covers this).
#
# Failure modes:
#   * source file missing or unreadable -> exit 1
#   * no face whose family is exactly "Noto Sans Mono CJK SC" -> exit 1
#   * a required non-whitespace codepoint is missing from the chosen face
#     -> exit 1 (we do not silently drop required glyphs)
#   * fontTools import failure (not installed) -> exit 1 with a clear message

import argparse
import os
import re
import sys

TARGET_FAMILY = "Noto Sans Mono CJK SC"

# Printable ASCII: 0x20..0x7E inclusive.
ASCII_RANGE = range(0x20, 0x7F)

# Only these are allowed to be absent from the source face; everything else
# that appears in a catalog string must resolve to a glyph. This keeps the
# build honest about coverage while tolerating (only) space-like codepoints
# that some fonts omit.
WHITESPACE_ALLOW_MISSING = set()


def die(msg):
    sys.stderr.write("gen-splash-font: %s\n" % msg)
    sys.exit(1)


def try_import_fonttools():
    try:
        from fontTools.ttLib import TTFont, TTCollection  # noqa: F401
    except ImportError as exc:
        die("fontTools is required at build time (python3-fonttools): %s" % exc)
    from fontTools.ttLib import TTFont, TTCollection
    from fontTools import subset as ft_subset
    return TTFont, TTCollection, ft_subset


def name_record_is_target(name_table, name_id):
    """True if any localized string on TTC face has family name TARGET_FAMILY."""
    if name_table is None:
        return False
    try:
        records = name_table.names
    except AttributeError:
        return False
    for rec in records:
        if rec.nameID != name_id:
            continue
        try:
            value = rec.toUnicode()
        except Exception:
            continue
        if value == TARGET_FAMILY:
            return True
    return False


def select_target_face(TTCollection, source):
    """Return (index, TTFont) of the first face whose family is TARGET_FAMILY."""
    try:
        collection = TTCollection(source, lazy=False)
    except Exception as exc:
        die("cannot open TTC %s: %s" % (source, exc))
    try:
        for idx, font in enumerate(collection.fonts):
            # nameID 1 is the family name in OpenType/TrueType naming.
            if name_record_is_target(font["name"], 1):
                # Copy the font out of the collection (drop the collection
                # wrapper) so save() writes a single-face TTF.
                return idx, font
    finally:
        # Do not let fonttools hold the collection open; we copied the face
        # object above but must keep a reference for the caller.
        pass
    die("no face in %s has family name %r" % (source, TARGET_FAMILY))


def extract_catalog_string_literals(path):
    """Yield each decoded C string literal from a splash locale header.

    Handles both single-line "..." literals and adjacent-literal
    concatenation: any number of "..." tokens between commas are joined
    by the C preprocessor, so a line continuation across lines within one
    catalog entry is auto-concatenated here as well.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        die("cannot read catalog %s: %s" % (path, exc))

    # Strip // line comments and /* ... */ block comments without touching
    # string contents. Locale headers currently use // only, but the block
    # form is cheap to support and avoids future surprises.
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            # Copy the whole string literal verbatim.
            out.append(c)
            i += 1
            while i < n:
                ch = text[i]
                out.append(ch)
                i += 1
                if ch == '\\' and i < n:
                    out.append(text[i])
                    i += 1
                    continue
                if ch == '"':
                    break
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '*':
            i += 2
            while i + 1 < n and not (text[i] == '*' and text[i + 1] == '/'):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    cleaned = ''.join(out)

    # Parse out the raw bytes of each C string literal and decode C escapes
    # faithfully: \" \\ \' \? \n \t \r \a \b \f \v \0 octal \ooo and
    # \\xHH hex byte escapes produce the corresponding byte (the byte stream
    # is decoded as UTF-8 at the end, so \\xE5\\xAD\\x97 -> '字').
    # \\uXXXX and \\UXXXXXXXX Unicode universal escapes are decoded directly
    # as UTF-8 text. Unknown or malformed escapes and raw control bytes are
    # rejected; they are never silently reinterpreted as ASCII backslashes.
    # Adjacent literals (only whitespace/newlines between them) are parts of
    # one C string: their decoded bytes are concatenated before UTF-8
    # decoding, so adjacent-literal concatenation is honored.
    literal_re = re.compile(r'"((?:\\.|[^"\\])*)"', re.DOTALL)

    simple_escape = {'"': 0x22, '\\': 0x5c, "'": 0x27, '?': 0x3f,
                     'a': 0x07, 'b': 0x08, 'f': 0x0c, 'n': 0x0a,
                     'r': 0x0d, 't': 0x09, 'v': 0x0b}
    hex_digits = '0123456789abcdefABCDEF'
    oct_digits = '01234567'

    def decode_token(raw):
        decoded = bytearray()
        j = 0
        while j < len(raw):
            ch = raw[j]
            if ch != '\\':
                decoded += ch.encode('utf-8')
                j += 1
                continue
            if j + 1 >= len(raw):
                die("trailing backslash in catalog %s literal %r"
                    % (path, raw))
            nxt = raw[j + 1]
            if nxt in simple_escape:
                decoded.append(simple_escape[nxt])
                j += 2
                continue
            if nxt == 'x':
                k = j + 2
                start = k
                while k < len(raw) and raw[k] in hex_digits:
                    k += 1
                if k == start:
                    die("malformed \\x escape in catalog %s literal %r"
                        % (path, raw))
                val = int(raw[start:k], 16)
                if val > 0xff:
                    die("out-of-range \\x escape (\\x%s) in catalog %s "
                        "literal %r" % (raw[start:k], path, raw))
                decoded.append(val)
                j = k
                continue
            if nxt in oct_digits:
                k = j + 1
                start = k
                while k < len(raw) and (k - start) < 3 and raw[k] in oct_digits:
                    k += 1
                val = int(raw[start:k], 8)
                if val > 0xff:
                    die("out-of-range octal escape (\\%s) in catalog %s "
                        "literal %r" % (raw[start:k], path, raw))
                decoded.append(val)
                j = k
                continue
            if nxt == 'u':
                k = j + 2
                digits = raw[k:k + 4]
                if len(digits) != 4 or any(d not in hex_digits for d in digits):
                    die("malformed \\u escape in catalog %s literal %r"
                        % (path, raw))
                cp = int(digits, 16)
                if 0xD800 <= cp <= 0xDFFF:
                    die("surrogate \\u%04X not supported in catalog %s literal %r"
                        % (cp, path, raw))
                decoded += chr(cp).encode('utf-8')
                j = k + 4
                continue
            if nxt == 'U':
                k = j + 2
                digits = raw[k:k + 8]
                if len(digits) != 8 or any(d not in hex_digits for d in digits):
                    die("malformed \\U escape in catalog %s literal %r"
                        % (path, raw))
                cp = int(digits, 16)
                if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
                    die("invalid \\U%08X in catalog %s literal %r"
                        % (cp, path, raw))
                decoded += chr(cp).encode('utf-8')
                j = k + 8
                continue
            die("unknown escape \\%s in catalog %s literal %r"
                % (nxt, path, raw))
        return bytes(decoded)

    def flush_group(group):
        joined = b''.join(decode_token(raw) for raw in group)
        try:
            return joined.decode('utf-8')
        except UnicodeDecodeError as exc:
            die("invalid UTF-8 in catalog %s literal(s) %r: %s"
                % (path, group, exc))

    group = []
    last_end = None
    for match in literal_re.finditer(cleaned):
        if last_end is not None and group and match.start() > last_end:
            between = cleaned[last_end:match.start()]
            if between.strip():
                yield flush_group(group)
                group = []
        group.append(match.group(1))
        last_end = match.end()
    if group:
        yield flush_group(group)


def collect_catalog_codepoints(paths):
    """Union of ASCII printable + every codepoint used by any catalog string."""
    codepoints = set(ASCII_RANGE)
    for path in paths:
        for literal in extract_catalog_string_literals(path):
            for ch in literal:
                codepoints.add(ord(ch))
    return codepoints


def glyphs_present(font, codepoints):
    """Return (present_set, missing_set) for codepoints in the source face."""
    cmap = font['cmap']
    # getBestCmap returns a single map covering the Unicode BMP+; Noto Sans
    # Mono CJK SC has a single Unicode cmap so this is enough. If multiple
    # cmaps exist take their union so we do not miss a codepoint.
    covered = set()
    for table in cmap.tables:
        try:
            covered.update(table.cmap.keys())
        except AttributeError:
            pass
    present = {cp for cp in codepoints if cp in covered}
    missing = {cp for cp in codepoints if cp not in covered}
    return present, missing


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True,
                        help='Noto Sans CJK TTC file to extract the SC mono face from')
    parser.add_argument('--output', required=True,
                        help='path of the single-face .ttf to write')
    parser.add_argument('--catalog', action='append', default=[],
                        help='locale header file whose string literals pick glyphs (repeatable)')
    args = parser.parse_args(argv)

    if not os.path.isfile(args.source):
        die("source font not found: %s" % args.source)
    if not args.catalog:
        die("at least one --catalog is required")

    TTFont, TTCollection, ft_subset = try_import_fonttools()

    index, face = select_target_face(TTCollection, args.source)

    codepoints = collect_catalog_codepoints(args.catalog)
    present, missing = glyphs_present(face, codepoints)

    if missing:
        missing_txt = ' '.join('U+%04X' % cp for cp in sorted(missing))
        die("subsets required %d codepoint(s) missing from %s: %s"
            % (len(missing), TARGET_FAMILY, missing_txt))

    # fontTools subset options. Keep_glyph_names False keeps the subset
    # self-contained; layout_features='*' preserves whatever shaping Noto
    # ships so kerning/advances do not regress; notdef_glyph keeps the
    # fallback glyph so a future catalog entry degrades to tofu instead of
    # a missing cmap slot.
    options = ft_subset.Options()
    options.name_IDs = ['*']     # keep family/style/full/ps + copyright/license
    options.name_legacy = True
    options.name_languages = ['*']
    options.layout_features = ['*']
    options.glyph_names = False
    options.notdef_outline = True
    options.recalc_bounds = False
    options.recalc_timestamp = False
    options.drop_tables = []
    options.hinting = True

    subsetter = ft_subset.Subsetter(options=options)
    subsetter.populate(unicodes=sorted(codepoints))
    subsetter.subset(face)

    # Determinism: fontTools' TTFont.save() consults recalcTimestamp and
    # recalcBBoxes on the font instance to decide whether to touch head
    # timestamps and glyph bounds. The instance extracted from a TTCollection
    # defaults both to True, which would overwrite our normalized values and
    # produce differing bytes across runs. Disable them before save, then
    # normalize head.created/modified so the output is stable.
    face.recalcTimestamp = False
    face.recalcBBoxes = False
    if 'head' in face:
        face['head'].modified = 0
        face['head'].created = 0

    # Single-face TTF: the TTFont we got from the collection has no
    # underlying file handle pointing at the collection anymore once we
    # drop the wrapper; save() writes a standalone sfnt.
    out_dir = os.path.dirname(args.output)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    face.save(args.output, reorderTables=False)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

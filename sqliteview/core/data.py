"""Value helpers. Port of upstream ``src/Data.{h,cpp}``."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

BOMS = (b"\xEF\xBB\xBF", b"\x00\x00\xFE\xFF", b"\xFF\xFE\x00\x00", b"\xFE\xFF", b"\xFF\xFE")

_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xFF\xD8\xFF", "JPEG"),
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
    (b"BM", "BMP"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
    (b"\x00\x00\x01\x00", "ICO"),
)


def starts_with_bom(data: bytes) -> bool:
    return data.startswith(BOMS)


def remove_bom(data: bytes) -> tuple[bytes, bytes]:
    for bom in BOMS:
        if data.startswith(bom):
            return data[len(bom):], bom
    return data, b""


def _is_printable(text: str) -> bool:
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cn" or (cat == "Cc" and not ch.isspace()):
            return False
    return True


def is_text_only(data: bytes, encoding: str = "", quick_test: bool = False) -> bool:
    """True when ``data`` decodes cleanly to printable text (upstream ``isTextOnly``)."""
    if starts_with_bom(data):
        return True
    enc = encoding or "utf-8"
    sample = data[:512] if quick_test else data
    if quick_test and len(data) > 512:
        if (not encoding or encoding.upper().startswith("ISO-8859")) and b"\0" in sample:
            return False
        try:
            sample.decode(enc, errors="strict")
        except UnicodeDecodeError as e:
            # A multibyte character truncated at the end of the sample is fine
            return e.start >= len(sample) - 3
        return True
    try:
        text = sample.decode(enc)
    except (UnicodeDecodeError, LookupError):
        return False
    return _is_printable(text)


def is_binary(value: Any) -> bool:
    """Binary means: bytes that aren't printable text (upstream ``SqliteTableModel::isBinary``)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return not is_text_only(bytes(value), quick_test=True)
    return False


def image_format(data: Any) -> str:
    """Return an image format name if the value looks like an image, else ''."""
    if not isinstance(data, (bytes, bytearray)):
        return ""
    b = bytes(data[:16])
    for sig, fmt in _IMAGE_SIGNATURES:
        if b.startswith(sig):
            return fmt
    if b.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "WEBP"
    head = bytes(data[:256]).lstrip()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in bytes(data[:1024])):
        return "SVG"
    return ""


def value_to_text(value: Any) -> str:
    """Text representation of a SQLite value as the original shows it (QByteArray -> QString)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        return _format_float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        b = bytes(value)
        b, _ = remove_bom(b)
        return b.decode("utf-8", errors="replace")
    return str(value)


def _format_float(v: float) -> str:
    # SQLite prints REAL values with up to 15 significant digits ("%!.15g")
    if v != v or v in (float("inf"), float("-inf")):
        return {"inf": "Inf", "-inf": "-Inf"}.get(repr(v), "NaN")
    s = "%.15g" % v
    if "e" not in s and "." not in s:
        s += ".0"
    return s


def value_to_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    return value_to_text(value).encode("utf-8")


def human_readable_size(byte_count: int) -> str:
    size = float(byte_count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB"):
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} YiB"


def remove_comments_from_query(query: str) -> str:
    """Strip -- and /* */ comments outside of quotes (upstream ``removeCommentsFromQuery``)."""
    out: list[str] = []
    i, n = 0, len(query)
    quote = ""
    changed = False
    while i < n:
        c = query[i]
        if not quote:
            if c in "'\"[" and (i == 0 or query[i - 1] != "\\"):
                quote = "]" if c == "[" else c
                out.append(c)
            elif c == "-" and query.startswith("--", i):
                j = query.find("\n", i)
                changed = True
                if j == -1:
                    break
                i = j
                continue
            elif c == "/" and query.startswith("/*", i):
                j = query.find("*/", i)
                changed = True
                if j == -1:
                    break
                i = j + 2
                continue
            else:
                out.append(c)
        else:
            if c == quote and (i == 0 or query[i - 1] != "\\"):
                quote = ""
            out.append(c)
        i += 1
    result = "".join(out).strip()
    if changed:
        result = re.sub(r"\n+", "\n", result)
        result = re.sub(r"[ \t]+\n", "\n", result)
    return result


_FIELD_SEP_RE = re.compile(r"\t|\r?\n")


def parse_clipboard(text: str) -> list[list[str]]:
    """Parse tab separated clipboard text with quoted fields (upstream ``parseClipboard``)."""
    if text.endswith("\n"):
        text = text[:-1]
    if text.endswith("\r"):
        text = text[:-1]
    if not text:
        return []
    rows: list[list[str]] = [[]]
    i, n = 0, len(text)
    while True:
        field = None
        if i < n and text[i] == '"':
            j, buf, closed = i + 1, [], False
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    closed = True
                    break
                buf.append(text[j])
                j += 1
            if closed and (j + 1 == n or text[j + 1] in "\t\r\n"):
                field, i = "".join(buf), j + 1
        if field is None:
            m = _FIELD_SEP_RE.search(text, i)
            k = m.start() if m else n
            field, i = text[i:k], k
        # LibreOffice converts tabs inside text to SOH (0x01); convert them back
        rows[-1].append(field.replace("\x01", "\t"))
        if i >= n:
            break
        if text[i] == "\t":
            i += 1
        else:
            i += 2 if text[i] == "\r" else 1
            rows.append([])
        if i >= n:
            rows[-1].append("")
            break
    return rows

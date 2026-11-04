"""Small Apple Live Photo identifier readers; used only in the bounded worker."""

import struct
import uuid

VERSION = 1


def identifier(value):
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeError:
            return ""
    if not isinstance(value, str) or len(value) > 128:
        return ""
    try:
        return str(uuid.UUID(value.strip("\0 \t\r\n")))
    except ValueError:
        return ""


def apple_identifier(exif):
    """Read Apple MakerNote tag 17 without following offsets outside the note."""
    try:
        note = exif.get_ifd(34665).get(37500, b"")
        if not isinstance(note, bytes) or not 16 <= len(note) <= 128 * 1024:
            return ""
        if note[:10] != b"Apple iOS\0" or note[12:14] not in (b"MM", b"II"):
            return ""
        endian = ">" if note[12:14] == b"MM" else "<"
        count = struct.unpack_from(endian + "H", note, 14)[0]
        if count > 512 or 16 + 12 * count > len(note):
            return ""
        values = set()
        for index in range(count):
            tag, kind, length, offset = struct.unpack_from(endian + "HHII", note, 16 + index * 12)
            if tag == 17 and kind == 2 and 1 <= length <= 128:
                start = offset if length > 4 else 24 + index * 12
                if start < 0 or start + length > len(note):
                    return ""
                value = identifier(note[start:start + length])
                if value:
                    values.add(value)
        return values.pop() if len(values) == 1 else ""
    except (KeyError, TypeError, ValueError, struct.error, SyntaxError):
        return ""

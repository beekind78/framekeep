"""Small, bounded header checks; image decoding remains in the restricted worker."""

FORMAT_EXTENSIONS = {
    "JPEG": ("JPG", "JPEG"),
    "PNG": ("PNG",),
    "WEBP": ("WEBP",),
    "GIF": ("GIF",),
    "TIFF": ("TIF", "TIFF"),
    "BMP": ("BMP",),
    "HEIF": ("HEIC", "HEIF"),
    "AVIF": ("AVIF",),
}
PHOTO_EXTENSIONS = {"." + suffix.lower() for suffixes in FORMAT_EXTENSIONS.values() for suffix in suffixes}
_PHOTO_BRANDS = {
    b"heic",
    b"heix",
    b"hevc",
    b"hevx",
    b"heim",
    b"heis",
    b"hevm",
    b"hevs",
    b"mif1",
    b"msf1",
    b"avif",
    b"avis",
}
_VIDEO_BRANDS = {b"qt  ", b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"M4A "}


def media_kind(header: bytes) -> str:
    """Classify at most 128 bytes, without parsing metadata or invoking a codec."""
    header = header[:128]
    if header.startswith(
        (
            b"\xff\xd8\xff",
            b"\x89PNG\r\n\x1a\n",
            b"GIF87a",
            b"GIF89a",
            b"BM",
            b"II*\x00",
            b"MM\x00*",
            b"II+\x00",
            b"MM\x00+",
        )
    ) or (header[:4] == b"RIFF" and header[8:12] == b"WEBP"):
        return "photo"
    if len(header) >= 16 and header[4:8] == b"ftyp":
        length = int.from_bytes(header[:4], "big")
        if length >= 16:
            brands = {header[8:12]} | {header[i : i + 4] for i in range(16, min(length, len(header)) - 3, 4)}
            if brands & _PHOTO_BRANDS:
                return "photo"
            if brands & _VIDEO_BRANDS:
                return "video"
    # Older QuickTime files may start with a movie atom or wide + media data.
    if header[4:8] == b"moov" or (header[:8] == b"\x00\x00\x00\x08wide" and header[12:16] == b"mdat"):
        return "video"
    if header.startswith(b"bplist00") or header.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<?xml"):
        return "metadata"
    return "unknown"


def photo_extension(format_name: str, suffix: str) -> str:
    """Keep a valid photo suffix, otherwise use the decoder's canonical extension."""
    extensions = FORMAT_EXTENSIONS[format_name]
    original = suffix.lstrip(".").upper()
    return original if original in extensions else extensions[0]

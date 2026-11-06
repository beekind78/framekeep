"""Bounded MOV/MP4 metadata reads. No video decoding or external references."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .live_metadata import identifier
from .source_files import open_source, require_file_identity

MAX_ATOMS = 4096
MAX_READ_BYTES = 1024 * 1024
MAX_VIDEO_BYTES = 64 * 1024 * 1024 * 1024


def read_video_metadata(path: Path, root: Path, *, expected=None) -> dict:
    from .library import fallback_date

    with open_source(path, root) as stream:
        require_file_identity(stream, expected)
        info = os.fstat(stream.fileno())
        if not 0 < info.st_size <= MAX_VIDEO_BYTES:
            raise ValueError("Video exceeds the 64 GiB file limit.")
        atoms = read_bytes = 0
        video = False
        created = None
        duration = 0.0
        extension = "MOV"
        live_ids = set()

        def read_at(position, size):
            nonlocal read_bytes
            read_bytes += size
            if read_bytes > MAX_READ_BYTES:
                raise ValueError("Video metadata exceeds the read limit.")
            stream.seek(position)
            data = stream.read(size)
            if len(data) != size:
                raise ValueError("Video metadata is incomplete.")
            return data

        def children(start, end):
            nonlocal atoms
            position = start
            while position < end:
                atoms += 1
                if atoms > MAX_ATOMS or end - position < 8:
                    raise ValueError("Video metadata has invalid or excessive atoms.")
                head = read_at(position, 8)
                length, name = int.from_bytes(head[:4], "big"), head[4:8]
                header_size = 8
                if length == 1:
                    length = int.from_bytes(read_at(position + 8, 8), "big")
                    header_size = 16
                elif length == 0:
                    length = end - position
                if length < header_size or position + length > end:
                    raise ValueError("Video atom exceeds its container boundary.")
                payload, finish = position + header_size, position + length
                yield name, payload, finish
                position = finish

        def metadata(start, end):
            keys, values = {}, {}
            if end - start < 8:
                return
            first = read_at(start, 8)
            if first[4:8] not in (b"hdlr", b"keys", b"ilst"):
                if first[:4] != b"\0" * 4:
                    return
                start += 4
            for name, payload, finish in children(start, end):
                if name == b"keys" and 8 <= finish - payload <= 65536:
                    data = read_at(payload, finish - payload)
                    count, position = int.from_bytes(data[4:8], "big"), 8
                    if count > 512:
                        continue
                    for index in range(1, count + 1):
                        if len(data) - position < 8:
                            raise ValueError("Invalid Live Photo metadata key.")
                        length = int.from_bytes(data[position:position + 4], "big")
                        if length < 8 or position + length > len(data):
                            raise ValueError("Invalid Live Photo metadata key boundary.")
                        if data[position + 4:position + 8] == b"mdta":
                            keys[index] = data[position + 8:position + length]
                        position += length
                elif name == b"ilst":
                    for key, value_start, value_end in children(payload, finish):
                        for field, data_start, data_end in children(value_start, value_end):
                            if field == b"data" and 8 < data_end - data_start <= 136:
                                raw = read_at(data_start, data_end - data_start)
                                if int.from_bytes(raw[:4], "big") == 1:
                                    value = identifier(raw[8:])
                                    if value:
                                        values.setdefault(int.from_bytes(key, "big"), set()).add(value)
            for key, value in values.items():
                if keys.get(key) == b"com.apple.quicktime.content.identifier":
                    live_ids.update(value)

        def walk(start, end, parents=()):
            nonlocal video, created, duration, extension
            for name, payload, finish in children(start, end):
                if name == b"ftyp" and not parents:
                    if finish - payload < 8:
                        raise ValueError("Invalid video file type atom.")
                    brand = read_at(payload, 4)
                    extension = "MOV" if brand == b"qt  " else "MP4"
                elif name in (b"moov", b"trak", b"mdia"):
                    if len(parents) >= 5:
                        raise ValueError("Video metadata nesting exceeds the limit.")
                    walk(payload, finish, (*parents, name))
                elif name == b"udta" and parents == (b"moov",):
                    walk(payload, finish, (*parents, name))
                elif name == b"meta" and parents in ((b"moov",), (b"moov", b"udta")):
                    metadata(payload, finish)
                elif name == b"hdlr" and parents[-2:] == (b"trak", b"mdia"):
                    if finish - payload < 12:
                        raise ValueError("Invalid video handler metadata.")
                    video |= read_at(payload + 8, 4) == b"vide"
                elif name == b"mvhd" and parents == (b"moov",):
                    if finish - payload < 20:
                        raise ValueError("Invalid movie header.")
                    version = read_at(payload, 1)[0]
                    required = 32 if version == 1 else 20
                    if version not in (0, 1) or finish - payload < required:
                        raise ValueError("Unsupported or incomplete movie header.")
                    data = read_at(payload, required)
                    if version == 1:
                        timestamp = int.from_bytes(data[4:12], "big")
                        scale = int.from_bytes(data[20:24], "big")
                        ticks = int.from_bytes(data[24:32], "big")
                    else:
                        timestamp = int.from_bytes(data[4:8], "big")
                        scale = int.from_bytes(data[12:16], "big")
                        ticks = int.from_bytes(data[16:20], "big")
                    if scale:
                        duration = ticks / scale
                    if timestamp:
                        try:
                            value = datetime(1904, 1, 1, tzinfo=UTC) + timedelta(seconds=timestamp)
                            created = value.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
                        except (OverflowError, ValueError, OSError):
                            pass

        walk(0, info.st_size)
        require_file_identity(stream, expected)
        if not video:
            raise ValueError("This container has no video track; it was not indexed as a video.")
        date, source = (created, "Video metadata") if created else fallback_date(path, info.st_mtime)
        original = path.suffix.lstrip(".").upper()
        if original == extension or (extension == "MP4" and original == "M4V"):
            extension = original
    return {
        "ok": True,
        "metadata": {
            "captured_at": date,
            "date_source": source,
            "camera": "",
            "width": 0,
            "height": 0,
            "kind": "Videos",
            "kind_source": "video",
            "kind_score": None,
            "kind_suggestion": "Videos",
            "category_version": 0,
            "media_type": "video",
            "duration": duration,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "extension": extension,
            "live_identifier": next(iter(live_ids)) if len(live_ids) == 1 else "",
        },
    }

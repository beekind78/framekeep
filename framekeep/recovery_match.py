"""Find larger visual matches using local fingerprints and bounded JPEG thumbnails."""

import heapq
import math
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .source_files import open_source, source_path
from .visual_similarity import VERSION, fingerprint

SAMPLE_SIDE = 36
SHORTLIST_SIZE = 24
CATALOG_LIMIT = 20000


def _thumbnail_sample(path, root):
    import numpy as np
    from PIL import Image

    with open_source(path, root) as stream:
        if os.fstat(stream.fileno()).st_size > 512 * 1024:
            return None
        with Image.open(stream, formats=("JPEG",)) as image:
            if image.format != "JPEG" or max(image.size) > 480:
                return None
            sample = image.convert("RGB").resize((SAMPLE_SIDE, SAMPLE_SIDE), Image.Resampling.LANCZOS)
            return np.asarray(sample, dtype=np.float32)


def find_clearer_copy(image, data, *, visible=None, exclude_sha="", preferred_stem=""):
    import numpy as np
    from PIL import Image

    try:
        database = source_path(data / "library.sqlite3", data)
        thumbnails = source_path(data / "thumbnails", data)
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute(
                "SELECT p.id,p.sha256,v.phash,v.dhash,v.aspect,p.filename FROM photos p "
                "JOIN visual_fingerprints v ON v.photo_id=p.id AND v.sha256=p.sha256 "
                "WHERE p.missing=0 AND p.media_type='image' AND v.version=? AND p.width*p.height>=? "
                "AND p.sha256<>? ORDER BY "
                "(? <> '' AND substr(lower(p.filename),1,length(?)+1)=? || '.') DESC, "
                "p.width*p.height DESC,p.id LIMIT ?",
                (VERSION, max(1, image.width * image.height // 9) if visible else 4 * image.width * image.height,
                 exclude_sha, preferred_stem, preferred_stem, preferred_stem, CATALOG_LIMIT),
            ).fetchall()
    except (OSError, ValueError, sqlite3.Error):
        return None
    if not rows:
        return None

    # Compare only the pixels that survived. Initial JPEG recovery defaults to the leading area.
    area_box = visible or (0, 0, image.width, image.height * 4 // 5)
    mask = Image.new("L", image.size, 0)
    mask.paste(255, area_box)
    if np.asarray(image.crop(area_box).convert("L")).std() < 10:
        return None
    rotations = ((0, None), (90, Image.Transpose.ROTATE_90), (180, Image.Transpose.ROTATE_180),
                 (270, Image.Transpose.ROTATE_270))
    best_score, match = 4.5, None
    samples = {}
    for angle, turn in rotations:
        query = image.transpose(turn) if turn is not None else image
        area = mask.transpose(turn) if turn is not None else mask
        mask_sample = np.asarray(area.resize((SAMPLE_SIDE, SAMPLE_SIDE)))
        valid = mask_sample > 240
        if not valid.any():
            continue
        damaged = mask_sample < 16
        rgb = np.asarray(query.resize((SAMPLE_SIDE, SAMPLE_SIDE), Image.Resampling.LANCZOS), dtype=np.float32)[valid]
        fp = fingerprint(query)
        phash, dhash = int(fp["phash"], 16), int(fp["dhash"], 16)
        shortlist = heapq.nsmallest(SHORTLIST_SIZE, (
            (bool(preferred_stem) and Path(filename).stem.lower() != preferred_stem,
             (int(p, 16) ^ phash).bit_count() + (int(d, 16) ^ dhash).bit_count(), photo_id, sha256)
            for photo_id, sha256, p, d, aspect, filename in rows if abs(aspect / fp["aspect"] - 1) <= .08
        ))
        for _, _, photo_id, sha256 in shortlist:
            if photo_id not in samples:
                try:
                    samples[photo_id] = _thumbnail_sample(thumbnails / f"{photo_id}.jpg", thumbnails)
                except (OSError, ValueError):
                    samples[photo_id] = None
            sample = samples[photo_id]
            if sample is None:
                continue
            if visible and damaged.any() and _gray_padding(sample[damaged]).mean() > .9:
                continue
            errors = np.abs(sample[valid] - rgb).mean(axis=1)
            score = float(errors.mean())
            if score <= best_score and np.percentile(errors, 90) <= 12:
                best_score, match = score, {"id": photo_id, "sha256": sha256, "rotation": angle}
    return match


def _gray_padding(rgb):
    return (rgb.min(axis=-1) >= 118) & (rgb.max(axis=-1) <= 138)


def gray_edge_crop(image):
    """Suggest a crop only when a mostly uniform gray band reaches an image edge."""
    import numpy as np
    from PIL import Image

    sample = image.copy()
    sample.thumbnail((480, 480), Image.Resampling.BOX)
    width, height = sample.size
    padding = _gray_padding(np.asarray(sample))
    rows, columns = padding.mean(axis=1), padding.mean(axis=0)
    best, crop = 0, None
    for line, edge in ((rows, "top"), (rows[::-1], "bottom"),
                       (columns, "left"), (columns[::-1], "right")):
        usable = np.flatnonzero(line < .985)
        if not usable.size:
            continue
        cut = int(usable[0])
        fraction = cut / len(line)
        if cut < 4 or not .035 <= fraction <= .88 or fraction <= best:
            continue
        # Include the boundary row in the cut when most of it is still padding.
        cut += int(line[cut] > .55)
        width_cut = math.ceil(cut * image.width / width)
        height_cut = math.ceil(cut * image.height / height)
        crop = {
            "top": (0, height_cut, image.width, image.height),
            "bottom": (0, 0, image.width, image.height - height_cut),
            "left": (width_cut, 0, image.width, image.height),
            "right": (0, 0, image.width - width_cut, image.height),
        }[edge]
        best = fraction
    return crop


def trim_gray_edge(image):
    import numpy as np

    width, height = image.size
    band = max(8, width // 4)
    # Only consider a small, solid neutral-gray tail at the end of the JPEG scan.
    rows = np.all(np.asarray(image)[:, -band:] == 128, axis=(1, 2))
    cut = height
    while cut and rows[cut - 1]:
        cut -= 1
    if 4 <= height - cut <= height // 5:
        return image.crop((0, 0, width, cut))
    return image

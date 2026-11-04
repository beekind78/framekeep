"""Conservative perceptual matches. A match is a review suggestion, never a deletion rule."""

from __future__ import annotations

VERSION = 1
PHASH_DISTANCE = 6
DHASH_DISTANCE = 8
MAX_COMPARISONS = 2_000_000
MAX_PAIRS = 20_000


def fingerprint(image):
    """Only called on an already bounded, orientation-normalized image in the worker."""
    import numpy as np
    from PIL import Image

    gray = np.asarray(image.convert("L").resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float32)
    basis = np.cos(np.pi / 32 * (np.arange(32) + 0.5)[None, :] * np.arange(8)[:, None])
    low = (basis @ gray @ basis.T).flatten()[1:]
    small = np.asarray(image.convert("L").resize((9, 8), Image.Resampling.LANCZOS))

    def bits(values):
        result = 0
        for value in values:
            result = (result << 1) | int(value)
        return f"{result:016x}"

    color = np.asarray(image.resize((8, 8)), dtype=np.float32).mean(axis=(0, 1))
    return {
        "phash": bits(low > np.median(low)),
        "dhash": bits((small[:, 1:] > small[:, :-1]).flatten()),
        "aspect": image.width / image.height,
        "color": [round(float(channel), 2) for channel in color],
        "texture": round(float(gray.std()), 2),
        "version": VERSION,
    }


def compare(left, right):
    """Return distances only when both fingerprints support a close visual match."""
    if left["sha256"] == right["sha256"] or min(left["texture"], right["texture"]) < 5:
        return None
    if abs(left["aspect"] / right["aspect"] - 1) > 0.025:
        return None
    phash = (int(left["phash"], 16) ^ int(right["phash"], 16)).bit_count()
    dhash = (int(left["dhash"], 16) ^ int(right["dhash"], 16)).bit_count()
    if phash > PHASH_DISTANCE or dhash > DHASH_DISTANCE:
        return None
    if max(abs(a - b) for a, b in zip(left["color"], right["color"], strict=True)) > 22:
        return None
    return {"phash_distance": phash, "dhash_distance": dhash}


def find_pairs(rows, cancelled=lambda: False):
    """BK-tree candidates avoid a full all-pairs scan. Each result is a direct pair.

    Exact byte copies share one representative here and remain in Exact duplicates.
    Search limits are explicit in the response rather than silently claiming completeness.
    """
    unique = {}
    for row in sorted(rows, key=lambda row: (-row["width"] * row["height"], row["id"])):
        if row["texture"] >= 5:
            unique.setdefault(row["sha256"], row)
    root, pairs, comparisons = None, [], 0
    for row in unique.values():
        if cancelled():
            return pairs, True
        code = int(row["phash"], 16)
        if root is None:
            root = [code, [row], {}]
            continue
        pending = [root]
        while pending:
            node = pending.pop()
            distance = (code ^ node[0]).bit_count()
            comparisons += 1
            if comparisons > MAX_COMPARISONS:
                return pairs, True
            if distance <= PHASH_DISTANCE:
                for other in node[1]:
                    comparisons += 1
                    if comparisons > MAX_COMPARISONS:
                        return pairs, True
                    match = compare(row, other)
                    if match:
                        pairs.append({"left": other["id"], "right": row["id"], **match})
                        if len(pairs) >= MAX_PAIRS:
                            return pairs, True
            pending.extend(
                child for edge, child in node[2].items()
                if distance - PHASH_DISTANCE <= edge <= distance + PHASH_DISTANCE
            )
        node = root
        while True:
            distance = (code ^ node[0]).bit_count()
            if not distance:
                node[1].append(row)
                break
            if distance not in node[2]:
                node[2][distance] = [code, [row], {}]
                break
            node = node[2][distance]
    pairs.sort(key=lambda pair: (pair["phash_distance"] + pair["dhash_distance"], pair["left"], pair["right"]))
    return pairs, False

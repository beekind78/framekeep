"""Original procedural landscape illustrations. No personal photos or external assets."""

import math
import random
import shutil
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw


def landscape(path: Path, seed: int, date: str):
    randomizer = random.Random(seed)
    palettes = [
        ((200, 219, 218), (226, 222, 189), (52, 96, 85), (26, 68, 63)),
        ((223, 175, 147), (250, 211, 156), (153, 110, 101), (85, 98, 88)),
        ((142, 178, 184), (217, 223, 198), (73, 107, 112), (36, 73, 80)),
        ((190, 196, 185), (240, 220, 181), (113, 129, 100), (54, 81, 71)),
    ]
    sky, horizon, back, front = palettes[seed % 4]
    w, h = (900, 1200) if seed % 5 == 0 else (1200, 900)
    image = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(image)
    for y in range(h):
        blend = min(y / (h * 0.68), 1)
        color = tuple(int(a * (1 - blend) + b * blend) for a, b in zip(sky, horizon))
        draw.line((0, y, w, y), fill=color)
    sunx, suny = int(w * (0.2 + randomizer.random() * 0.55)), int(h * 0.26)
    draw.ellipse((sunx - 48, suny - 48, sunx + 48, suny + 48), fill=(248, 232, 197))
    for layer, color in enumerate((back, front, tuple(max(c - 22, 0) for c in front))):
        base = h * (0.45 + layer * 0.16)
        phase = randomizer.random() * math.pi
        points = [(0, h)]
        for x in range(0, w + 20, 20):
            y = base + math.sin(x / w * 5 + phase) * h * 0.09 + math.sin(x / w * 11 + phase) * h * 0.035
            points.append((x, int(y)))
        points.append((w, h))
        draw.polygon(points, fill=color)
    if seed % 3 == 0:
        draw.polygon(
            [(w * 0.5, h * 0.61), (w * 0.58, h * 0.68), (w * 0.45, h), (w * 0.1, h)], fill=(149, 177, 166)
        )
    for _ in range(8000):
        x, y = randomizer.randrange(w), randomizer.randrange(h)
        rgb = image.getpixel((x, y))
        change = randomizer.randint(-8, 8)
        draw.point((x, y), fill=tuple(max(0, min(255, c + change)) for c in rgb))
    exif = Image.Exif()
    exif[306], exif[271], exif[272] = (
        datetime.fromisoformat(date).strftime("%Y:%m:%d %H:%M:%S"),
        "Framekeep",
        "Demo illustrations",
    )
    image.save(path, quality=91, exif=exif)


def create_demo(data: Path):
    folder = data / "demo-library"
    folder.mkdir(exist_ok=True)
    names = [
        "Quiet mornings",
        "The long way home",
        "A little escape",
        "Golden hour",
        "Somewhere slow",
        "Across the valley",
        "Coastal light",
        "Into the green",
        "A different perspective",
        "Last light",
        "Weekend wandering",
        "Still waters",
        "A familiar view",
        "The open road",
        "Small adventures",
        "Room to breathe",
        "Another sunrise",
        "Far from the city",
    ]
    for index, name in enumerate(names):
        month = [1, 3, 6, 8, 11, 12][index // 3]
        date = f"{2024 if index < 9 else 2025}-{month:02d}-{index % 3 + 12:02d}T09:30:00"
        path = folder / f"{name}.jpg"
        if not path.exists():
            landscape(path, index, date)
    for index in (2, 7):
        source = folder / f"{names[index]}.jpg"
        target = folder / f"{names[index]} - backup.jpg"
        if not target.exists():
            shutil.copy2(source, target)
    screenshot = folder / "Screenshot_20250308_102030.png"
    if not screenshot.exists():
        image = Image.new("RGB", (720, 1100), "#f4f0e8")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((45, 70, 675, 270), radius=20, fill="#244b43")
        for index in range(5):
            draw.rounded_rectangle((45, 310 + index * 140, 675, 425 + index * 140), radius=15, fill="#e5dfd2")
            draw.ellipse((70, 335 + index * 140, 130, 395 + index * 140), fill="#b1bfa3")
            draw.rectangle((160, 345 + index * 140, 570, 355 + index * 140), fill="#a6ad9c")
            draw.rectangle((160, 375 + index * 140, 400, 385 + index * 140), fill="#c3c6b8")
        image.save(screenshot)
    return folder

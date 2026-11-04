"""Rasterize the existing code-native Framekeep mark into a multi-resolution Windows icon."""

from pathlib import Path

from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1]
scale = 8
image = Image.new("RGBA", (40 * scale, 40 * scale))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((0, 0, 40 * scale - 1, 40 * scale - 1), radius=12 * scale, fill="#244b43")
paths = [((11, 17), (11, 11), (17, 11)), ((23, 11), (29, 11), (29, 17)),
         ((29, 23), (29, 29), (23, 29)), ((17, 29), (11, 29), (11, 23)),
         ((14, 24), (19, 18), (23, 22), (26, 19))]
width = round(2.4 * scale)
for path in paths:
    points = [(x * scale, y * scale) for x, y in path]
    draw.line(points, fill="#f8f4e9", width=width, joint="curve")
    for x, y in (points[0], points[-1]):
        radius = width / 2
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill="#f8f4e9")
draw.ellipse((23 * scale, 13 * scale, 27 * scale, 17 * scale), fill="#dccba4")
image.save(root / "framekeep" / "static" / "framekeep.ico", format="ICO",
           sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)])

"""Create transparent Windows/web icons from the MagMechSim app mark."""

from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw


ASSETS = Path(__file__).resolve().parents[1] / "src" / "ngsolve_gui" / "assets"
PNG = ASSETS / "magmechsim_icon_128x128.png"
ICO = ASSETS / "magmechsim_icon.ico"
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main():
    image = Image.open(PNG).convert("RGBA")
    width, height = image.size
    if width != height:
        raise ValueError("The application mark must be square.")

    # The source artwork has a white canvas beyond its rounded-square mark.
    # Replace that canvas with a supersampled alpha mask; the mark itself and
    # its internal white highlights remain untouched.
    scale = 8
    large = Image.new("L", (width * scale, height * scale), 0)
    draw = ImageDraw.Draw(large)
    right = width * scale - 1
    bottom = height * scale - 1
    draw.rounded_rectangle(
        (0, 0, right, bottom),
        radius=round(width * 0.25 * scale),
        fill=255,
    )
    # BOX averages the supersampled coverage exactly. LANCZOS rings beyond
    # curved transparent corners and can restore a pale fringe on dark themes.
    alpha = large.resize((width, height), Image.Resampling.BOX)
    # The original artwork was flattened onto white. Clear only the connected
    # near-white matte from the image perimeter to remove its light fringe;
    # internal white/silver logo strokes are not connected to that perimeter.
    rgb = image.load()
    alpha_pixels = alpha.load()
    pending = deque()
    visited = bytearray(width * height)

    def enqueue(x, y):
        index = y * width + x
        if visited[index]:
            return
        visited[index] = 1
        red, green, blue, _ = rgb[x, y]
        if min(red, green, blue) >= 220 and max(red, green, blue) - min(red, green, blue) <= 40:
            pending.append((x, y))

    for offset in range(width):
        enqueue(offset, 0)
        enqueue(offset, height - 1)
        enqueue(0, offset)
        enqueue(width - 1, offset)
    while pending:
        x, y = pending.popleft()
        alpha_pixels[x, y] = 0
        if x:
            enqueue(x - 1, y)
        if x + 1 < width:
            enqueue(x + 1, y)
        if y:
            enqueue(x, y - 1)
        if y + 1 < height:
            enqueue(x, y + 1)
    image.putalpha(alpha)
    image.save(PNG, optimize=True)
    icon_source = image.resize((256, 256), Image.Resampling.LANCZOS)
    icon_source.save(ICO, format="ICO", sizes=ICO_SIZES)


if __name__ == "__main__":
    main()

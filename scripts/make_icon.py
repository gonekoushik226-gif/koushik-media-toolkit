"""Generate assets/app.ico and assets/app.png (run once; the results are committed).

    python scripts/make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SIZE = 1024


def draw() -> Image.Image:
    # Diagonal blue -> violet gradient
    gradient = Image.new("RGBA", (SIZE, SIZE))
    top, bottom = (37, 99, 235), (124, 58, 237)
    pixels = gradient.load()
    for y in range(SIZE):
        for x in range(SIZE):
            t = (x + y) / (2 * (SIZE - 1))
            pixels[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)) + (255,)
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, SIZE - 40, SIZE - 40), radius=220, fill=255)
    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    icon.paste(gradient, (0, 0), mask)

    draw_ = ImageDraw.Draw(icon)
    # Play triangle (video / audio)
    draw_.polygon([(380, 250), (380, 650), (720, 450)], fill=(255, 255, 255, 255))
    # Four dots: video, audio, images, PDF
    colors = [(96, 165, 250), (52, 211, 153), (251, 191, 36), (248, 113, 113)]
    for index, color in enumerate(colors):
        cx = 290 + index * 148
        draw_.ellipse((cx - 50, 740, cx + 50, 840), fill=(255, 255, 255, 255))
        draw_.ellipse((cx - 34, 756, cx + 34, 824), fill=color + (255,))
    return icon


def main() -> None:
    image = draw()
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    image.resize((256, 256), Image.Resampling.LANCZOS).save(assets / "app.png")
    sizes = [(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
    image.resize((256, 256), Image.Resampling.LANCZOS).save(assets / "app.ico", sizes=sizes)
    print("Wrote", assets / "app.ico")


if __name__ == "__main__":
    main()

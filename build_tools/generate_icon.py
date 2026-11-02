from pathlib import Path

from PIL import Image, ImageDraw


def make_icon(size: int) -> Image.Image:
    scale = size / 48
    image = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)

    def points(values):
        return [round(value * scale) for value in values]

    blue = (66, 133, 244, 255)
    red = (234, 67, 53, 255)
    yellow = (251, 188, 4, 255)
    green = (52, 168, 83, 255)
    width = max(2, round(5 * scale))
    draw.line(points((8, 20, 8, 10, 20, 10)), fill=blue, width=width, joint="curve")
    draw.line(points((28, 10, 38, 10, 38, 20)), fill=red, width=width, joint="curve")
    draw.line(points((8, 28, 8, 38, 20, 38)), fill=yellow, width=width, joint="curve")
    draw.line(points((28, 38, 38, 38, 38, 28)), fill=green, width=width, joint="curve")
    draw.ellipse(tuple(points((15, 15, 31, 31))), fill=(255, 255, 255, 240), outline=blue,
                 width=max(1, round(2 * scale)))
    draw.ellipse(tuple(points((20, 20, 26, 26))), fill=blue)
    draw.line(points((30, 30, 43, 43)), fill=blue, width=width)
    return image


if __name__ == "__main__":
    output = Path(__file__).resolve().parents[1] / "assets" / "sinlens.ico"
    make_icon(256).save(output, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(output)

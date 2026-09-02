"""Generate profile avatar candidates for the Bluesky account.

    python3 tools/avatar.py [--out DIR]

Drawn with renderer.py's own bevel, smiley and mine functions, so the avatar
is made of literally the same code as the boards rather than merely resembling
them. Change the board palette and these follow.

Deliberately no flag: the red flag is the most recognisable Minesweeper icon
there is, but this bot has no flagging mechanic, and an avatar promising a
feature the game does not have is a small lie to everyone who sees it.

Bluesky crops avatars to a circle and shows them at ~40px in the feed, so each
candidate is also rendered small and round in the contact sheet — legibility
at that size is the whole test.
"""

import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import renderer  # noqa: E402

SIZE = 1000
FACE = renderer.FACE
NUMBERS = renderer.NUMBER_COLORS
EXPLODED_CELL = renderer.EXPLODED_CELL


def font(size: int):
    return renderer._font(size)


def tile(draw, box, raised=True, fill=FACE):
    """A classic tile: flat fill plus the bevel that does all the work."""
    draw.rectangle(box, fill=fill)
    renderer._bevel(draw, box, max(4, int((box[2] - box[0]) * 0.045)),
                    raised=raised)


def numeral(draw, box, value):
    x0, y0, x1, y1 = box
    size = int((x1 - x0) * 0.66)
    draw.text(((x0 + x1) // 2, (y0 + y1) // 2), str(value),
              fill=NUMBERS[value], font=font(size), anchor="mm")


def variant_smiley() -> Image.Image:
    """The smiley button. Yellow on grey is the loudest thing at 32 pixels,
    and anyone who has played the game knows it instantly."""
    img = Image.new("RGB", (SIZE, SIZE), FACE)
    draw = ImageDraw.Draw(img)
    inset = int(SIZE * 0.045)
    renderer._bevel(draw, (0, 0, SIZE, SIZE), int(SIZE * 0.045), raised=True)
    renderer._smiley(draw, (inset, inset, SIZE - inset, SIZE - inset), "happy")
    return img


def variant_mine() -> Image.Image:
    """The cell you never want: a mine on the red that means you just lost."""
    img = Image.new("RGB", (SIZE, SIZE), FACE)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, SIZE, SIZE), fill=EXPLODED_CELL)
    renderer._bevel(draw, (0, 0, SIZE, SIZE), int(SIZE * 0.03), raised=False)
    renderer._draw_mine(draw, (0, 0, SIZE, SIZE))
    return img


def variant_quad() -> Image.Image:
    """Two numerals, a mine on red, and one unopened tile — the whole visual
    vocabulary of the game in four squares."""
    img = Image.new("RGB", (SIZE, SIZE), FACE)
    draw = ImageDraw.Draw(img)
    half = SIZE // 2
    boxes = [(0, 0, half, half), (half, 0, SIZE, half),
             (0, half, half, SIZE), (half, half, SIZE, SIZE)]
    for box, value in zip(boxes[:2], (1, 2)):
        tile(draw, box, raised=False)
        numeral(draw, box, value)
    draw.rectangle(boxes[2], fill=EXPLODED_CELL)
    renderer._bevel(draw, boxes[2], 12, raised=False)
    renderer._draw_mine(draw, boxes[2])
    tile(draw, boxes[3], raised=True)
    return img


def variant_board() -> Image.Image:
    """A 3x3 fragment of a real position — richest at profile size."""
    img = Image.new("RGB", (SIZE, SIZE), FACE)
    draw = ImageDraw.Draw(img)
    cell = SIZE // 3
    layout = [["1", "2", "hidden"],
              ["1", "mine", "2"],
              ["hidden", "2", "1"]]
    for r, row in enumerate(layout):
        for c, token in enumerate(row):
            box = (c * cell, r * cell, (c + 1) * cell, (r + 1) * cell)
            if token == "hidden":
                tile(draw, box, raised=True)
            elif token == "mine":
                draw.rectangle(box, fill=EXPLODED_CELL)
                renderer._bevel(draw, box, 8, raised=False)
                renderer._draw_mine(draw, box)
            else:
                tile(draw, box, raised=False)
                numeral(draw, box, int(token))
    return img


def variant_flag() -> Image.Image:
    """The red flag on a raised tile.

    Honest only since flagging shipped — before that it advertised a feature
    the game did not have.
    """
    img = Image.new("RGB", (SIZE, SIZE), FACE)
    draw = ImageDraw.Draw(img)
    renderer._bevel(draw, (0, 0, SIZE, SIZE), int(SIZE * 0.055), raised=True)
    renderer._draw_flag(draw, (0, 0, SIZE, SIZE))
    return img


def variant_flag_quad() -> Image.Image:
    """A flag, a mine, and two numerals — the whole game in four squares."""
    img = Image.new("RGB", (SIZE, SIZE), FACE)
    draw = ImageDraw.Draw(img)
    half = SIZE // 2
    boxes = [(0, 0, half, half), (half, 0, SIZE, half),
             (0, half, half, SIZE), (half, half, SIZE, SIZE)]
    tile(draw, boxes[0], raised=False)
    numeral(draw, boxes[0], 1)
    tile(draw, boxes[1], raised=True)
    renderer._draw_flag(draw, boxes[1])
    draw.rectangle(boxes[2], fill=EXPLODED_CELL)
    renderer._bevel(draw, boxes[2], 12, raised=False)
    renderer._draw_mine(draw, boxes[2])
    tile(draw, boxes[3], raised=False)
    numeral(draw, boxes[3], 2)
    return img


VARIANTS = {
    "a-smiley": (variant_smiley, "the smiley button"),
    "b-mine": (variant_mine, "a mine on the red cell"),
    "c-quad": (variant_quad, "1, 2, a mine, and an unopened tile"),
    "d-board": (variant_board, "a 3x3 fragment of a position"),
    "e-flag": (variant_flag, "the red flag on a raised tile"),
    "f-flagquad": (variant_flag_quad, "a flag, a mine, and two numerals"),
}


def circular(img: Image.Image, px: int) -> Image.Image:
    """How Bluesky will actually show it: cropped to a circle."""
    small = img.resize((px, px), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (px * 4, px * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, px * 4 - 1, px * 4 - 1), fill=255)
    small.putalpha(mask.resize((px, px), Image.LANCZOS))
    return small


def contact_sheet(images: dict) -> Image.Image:
    pad, big, label_h = 40, 260, 34
    width = pad + len(images) * (big + pad)
    height = pad + label_h + big + 30 + 96 + pad
    sheet = Image.new("RGB", (width, height), "#f5f5f4")
    draw = ImageDraw.Draw(sheet)
    small_font = font(20)
    for i, (name, img) in enumerate(images.items()):
        x = pad + i * (big + pad)
        draw.text((x, pad), name, fill="#1c1917", font=small_font)
        sheet.paste(circular(img, big), (x, pad + label_h), circular(img, big))
        y = pad + label_h + big + 30
        for j, px in enumerate((88, 48, 32)):
            offset = x + j * 96
            sheet.paste(circular(img, px), (offset, y + (88 - px) // 2),
                        circular(img, px))
    draw.text((pad, height - pad + 6), "profile size, then 88 / 48 / 32 px",
              fill="#57534e", font=small_font, anchor="ls")
    return sheet


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp/minesweeper-avatar")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    images = {}
    for name, (builder, caption) in VARIANTS.items():
        img = builder()
        path = os.path.join(args.out, f"avatar-{name}.png")
        img.save(path)
        images[name] = img
        print(f"{path}  {SIZE}x{SIZE} — {caption}")

    sheet_path = os.path.join(args.out, "contact-sheet.png")
    contact_sheet(images).save(sheet_path)
    print(f"{sheet_path}  — all of them, circle-cropped at four sizes")


if __name__ == "__main__":
    main()

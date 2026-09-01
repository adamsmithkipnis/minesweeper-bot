"""Generate profile avatar candidates for the Bluesky account.

    python3 tools/avatar.py [--out DIR]

Drawn with the same palette and bevel as renderer.py, so the avatar looks
like the boards the bot actually posts rather than a stock bomb icon.

Deliberately no flag: the classic red flag is the most recognisable
Minesweeper icon there is, but this bot has no flagging mechanic, and an
avatar promising a feature the game does not have is a small lie to every
person who sees it.

Bluesky crops avatars to a circle and shows them at ~40px in the feed, so
each candidate is also rendered as a small circle in the contact sheet —
legibility at that size is the whole test.
"""

import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import renderer  # noqa: E402  — palette and font lookup

SIZE = 1000
BG = renderer.BG
HIDDEN_FILL = renderer.HIDDEN_FILL
HIDDEN_LIGHT = renderer.HIDDEN_LIGHT
HIDDEN_DARK = renderer.HIDDEN_DARK
OPEN_FILL = renderer.OPEN_FILL
GRID_LINE = renderer.GRID_LINE
MINE_FILL = "#b91c1c"
NUMBERS = renderer.NUMBER_COLORS

_BOLD_PATHS = [
    ("/System/Library/Fonts/Menlo.ttc", 1),      # Menlo Bold
    ("/System/Library/Fonts/Monaco.ttf", 0),
    ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
]


def font(size: int):
    for path, index in _BOLD_PATHS:
        try:
            return ImageFont.truetype(path, size, index=index)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def hidden_cell(draw, box, bevel=None):
    """An unopened tile: flat fill with a light top-left and dark bottom-right."""
    x0, y0, x1, y1 = box
    bevel = bevel or max(3, int((x1 - x0) * 0.038))
    draw.rectangle(box, fill=HIDDEN_FILL)
    draw.polygon([(x0, y0), (x1, y0), (x1 - bevel, y0 + bevel),
                  (x0 + bevel, y0 + bevel), (x0 + bevel, y1 - bevel),
                  (x0, y1)], fill=HIDDEN_LIGHT)
    draw.polygon([(x1, y1), (x0, y1), (x0 + bevel, y1 - bevel),
                  (x1 - bevel, y1 - bevel), (x1 - bevel, y0 + bevel),
                  (x1, y0)], fill=HIDDEN_DARK)


def opened_cell(draw, box, fill=OPEN_FILL, outline=GRID_LINE, width=3):
    draw.rectangle(box, fill=fill, outline=outline, width=width)


def numeral(draw, box, value):
    x0, y0, x1, y1 = box
    size = int((x1 - x0) * 0.66)
    draw.text(((x0 + x1) // 2, (y0 + y1) // 2 + size * 0.02), str(value),
              fill=NUMBERS[value], font=font(size), anchor="mm")


def mine(draw, box, scale=0.30):
    """The bomb: eight spikes, a black body, and a highlight."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    radius = (x1 - x0) * scale
    spike = radius * 1.62
    width = max(3, int(radius * 0.26))
    for dx, dy in ((1, 0), (0, 1), (0.707, 0.707), (0.707, -0.707)):
        draw.line((cx - dx * spike, cy - dy * spike,
                   cx + dx * spike, cy + dy * spike),
                  fill="#0b0f14", width=width)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                 fill="#0b0f14")
    glint = radius * 0.34
    draw.ellipse((cx - radius * 0.46, cy - radius * 0.46,
                  cx - radius * 0.46 + glint, cy - radius * 0.46 + glint),
                 fill="#c3ced9")


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def variant_mine() -> Image.Image:
    """One mine, filling the frame. Loudest at 40 pixels."""
    img = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(img)
    opened_cell(draw, (0, 0, SIZE, SIZE), fill=MINE_FILL, outline=None, width=0)
    mine(draw, (0, 0, SIZE, SIZE), scale=0.235)
    return img


def variant_quad() -> Image.Image:
    """Two numerals, a mine, and one unopened tile.

    The coloured digits are what separate Minesweeper from any other bomb
    icon, and at 2x2 they survive the shrink to feed size.
    """
    img = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(img)
    half = SIZE // 2
    quads = [(0, 0, half, half), (half, 0, SIZE, half),
             (0, half, half, SIZE), (half, half, SIZE, SIZE)]

    opened_cell(draw, quads[0])
    numeral(draw, quads[0], 1)
    opened_cell(draw, quads[1])
    numeral(draw, quads[1], 2)
    opened_cell(draw, quads[2], fill=MINE_FILL)
    mine(draw, quads[2], scale=0.30)
    hidden_cell(draw, quads[3])
    return img


def variant_board() -> Image.Image:
    """A 3x3 fragment of a real position — richest at profile size."""
    img = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(img)
    cell = SIZE // 3
    layout = [
        ["1", "2", "hidden"],
        ["1", "mine", "2"],
        ["hidden", "2", "1"],
    ]
    for r, row in enumerate(layout):
        for c, token in enumerate(row):
            box = (c * cell, r * cell, (c + 1) * cell, (r + 1) * cell)
            if token == "hidden":
                hidden_cell(draw, box)
            elif token == "mine":
                opened_cell(draw, box, fill=MINE_FILL)
                mine(draw, box, scale=0.29)
            else:
                opened_cell(draw, box)
                numeral(draw, box, int(token))
    return img


def variant_triad() -> Image.Image:
    """1, 2, 3 and a mine — the strongest colour signature at feed size.

    Blue, green and red digits together are the thing nobody mistakes for a
    generic bomb icon; the trade is losing the raised unopened tile.
    """
    img = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(img)
    half = SIZE // 2
    quads = [(0, 0, half, half), (half, 0, SIZE, half),
             (0, half, half, SIZE), (half, half, SIZE, SIZE)]

    for box, value in zip(quads[:2], (1, 2)):
        opened_cell(draw, box)
        numeral(draw, box, value)
    opened_cell(draw, quads[2], fill=MINE_FILL)
    mine(draw, quads[2], scale=0.30)
    opened_cell(draw, quads[3])
    numeral(draw, quads[3], 3)
    return img


def _quad_layout(order) -> Image.Image:
    """A 2x2 from a list of four tokens: an int draws that numeral, "mine"
    draws the bomb on red."""
    img = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(img)
    half = SIZE // 2
    boxes = [(0, 0, half, half), (half, 0, SIZE, half),
             (0, half, half, SIZE), (half, half, SIZE, SIZE)]
    for box, token in zip(boxes, order):
        if token == "mine":
            opened_cell(draw, box, fill=MINE_FILL)
            mine(draw, box, scale=0.30)
        else:
            opened_cell(draw, box)
            numeral(draw, box, token)
    return img


def variant_triad_diagonal() -> Image.Image:
    """1, 2, 3 and a mine, with the two reds on opposite corners."""
    return _quad_layout(["mine", 1, 2, 3])


def variant_four() -> Image.Image:
    """1, 2, 4 and a mine: blue, green, purple, red — four distinct hues."""
    return _quad_layout([1, 2, "mine", 4])


VARIANTS = {
    "a-mine": (variant_mine, "one mine, filling the frame"),
    "b-quad": (variant_quad, "two numerals, a mine, one unopened tile"),
    "c-board": (variant_board, "a 3x3 fragment of a position"),
    "d-triad": (variant_triad, "1, 2, 3 and a mine — max colour signature"),
    "e-diag": (variant_triad_diagonal, "same, with the two reds on opposite corners"),
    "f-four": (variant_four, "1, 2, 4 and a mine — four distinct hues"),
}


# ---------------------------------------------------------------------------
# Contact sheet
# ---------------------------------------------------------------------------

def circular(img: Image.Image, px: int) -> Image.Image:
    """How Bluesky will actually show it: cropped to a circle."""
    small = img.resize((px, px), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (px * 4, px * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, px * 4 - 1, px * 4 - 1), fill=255)
    small.putalpha(mask.resize((px, px), Image.LANCZOS))
    return small


def contact_sheet(images: dict) -> Image.Image:
    """Each candidate at profile size and at feed size, side by side."""
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
    print(f"{sheet_path}  — all three, circle-cropped at four sizes")


if __name__ == "__main__":
    main()

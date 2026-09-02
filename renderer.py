"""Board image generation and screen-reader alt text.

Both outputs are built from one pure function, `display_grid`, so the picture
and the words can never describe different positions. That function is also
the single place where the hidden layer can become visible, and it only does
so once the game is over — which makes "the bot never shows the mines during
play" a property one small function is responsible for, and one test can pin.

The look is the original Windows Minesweeper: the silver face, raised tiles
with a white top-left and grey bottom-right bevel, sunken panels, the classic
numeral colours, and a header carrying two seven-segment counters and the
smiley. The chrome is drawn rather than pulled from an image so it scales to
any board size.

One deliberate departure: the original has no row or column labels, because
you play it with a mouse. Here a coordinate is how a person votes, so the
labels stay — set in the grey margin outside the sunken grid panel, where the
original frame has the room for them.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

import game

# Display tokens. These are what the alt text prints and what the drawing
# code switches on.
HIDDEN = "?"
MINE = "*"
EXPLODED = "X"

# The classic palette. Everything is made of one silver, and the illusion of
# depth is entirely bevels: white on the top-left, mid-grey on the bottom
# right for a raised tile, and the two swapped for a sunken panel.
FACE = "#C0C0C0"
BEVEL_LIGHT = "#FFFFFF"
BEVEL_DARK = "#808080"
GRID_LINE = "#808080"
TEXT = "#000000"
LED_BG = "#000000"
LED_ON = "#FF0000"
LED_OFF = "#3B0000"      # unlit segments stay faintly visible, as on real LEDs
MINE_CELL = "#C0C0C0"
EXPLODED_CELL = "#FF0000"
HIGHLIGHT = "#0000FF"

# Kept under their old names because tools/avatar.py imports them.
BG = FACE
HIDDEN_FILL = FACE
HIDDEN_LIGHT = BEVEL_LIGHT
HIDDEN_DARK = BEVEL_DARK
OPEN_FILL = FACE

NUMBER_COLORS = {
    1: "#0000FF", 2: "#008000", 3: "#FF0000", 4: "#000080",
    5: "#800000", 6: "#008080", 7: "#000000", 8: "#808080",
}

CELL = 54
TILE_BEVEL = 4
PANEL_BEVEL = 3
OUTER_BEVEL = 6
PAD = 12
HEADER_H = 76
LABEL_LEFT = 36
LABEL_TOP = 32

_FONT_PATHS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]


def _font(size: int):
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def display_grid(state: game.GameState) -> list:
    """The board as tokens, from the point of view of somebody reading the post.

    Hidden cells are '?' while the game is live — including the mines. The
    mines only become visible once the run is over, which is the entire
    premise of moving from Battleship to a solitaire game: while play is
    happening there is nothing published that anyone could read the answer
    from.
    """
    over = state.status != game.ACTIVE
    grid = []
    for r in range(state.rows):
        row = []
        for c in range(state.cols):
            if (r, c) in state.revealed:
                row.append(str(state.revealed[(r, c)]))
            elif over and (r, c) in state.mine_cells:
                row.append(EXPLODED
                           if game.index_to_coord(r, c) == state.exploded_cell
                           else MINE)
            else:
                row.append(HIDDEN)
        grid.append(row)
    return grid


# ---------------------------------------------------------------------------
# Chrome: bevels, seven-segment counters, the smiley
# ---------------------------------------------------------------------------

def _bevel(draw, box, width, raised=True):
    """The one effect the whole look is built from.

    Two L-shaped bands: light along the top and left, dark along the bottom
    and right, which the eye reads as a tile lit from the top-left. Swap them
    and the same shape reads as a hole.
    """
    x0, y0, x1, y1 = box
    light = BEVEL_LIGHT if raised else BEVEL_DARK
    dark = BEVEL_DARK if raised else BEVEL_LIGHT
    draw.polygon([(x0, y0), (x1, y0), (x1 - width, y0 + width),
                  (x0 + width, y0 + width), (x0 + width, y1 - width),
                  (x0, y1)], fill=light)
    draw.polygon([(x1, y1), (x0, y1), (x0 + width, y1 - width),
                  (x1 - width, y1 - width), (x1 - width, y0 + width),
                  (x1, y0)], fill=dark)


# Which of the seven segments each digit lights, in the order a b c d e f g.
_SEGMENTS = {
    0: "abcdef", 1: "bc", 2: "abdeg", 3: "abcdg", 4: "bcfg",
    5: "acdfg", 6: "acdefg", 7: "abc", 8: "abcdefg", 9: "abcdfg",
    "-": "g",
}


def _digit(draw, box, value):
    """One seven-segment digit, unlit segments left faintly visible."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    t = max(2, int(w * 0.22))          # segment thickness
    mid = y0 + h / 2
    lit = _SEGMENTS.get(value, "")

    horizontal = {
        "a": (x0, y0),
        "g": (x0, mid - t / 2),
        "d": (x0, y1 - t),
    }
    vertical = {
        "f": (x0, y0, mid),
        "b": (x1 - t, y0, mid),
        "e": (x0, mid, y1),
        "c": (x1 - t, mid, y1),
    }
    for name, (sx, sy) in horizontal.items():
        colour = LED_ON if name in lit else LED_OFF
        draw.polygon([(sx + t / 2, sy), (sx + w - t / 2, sy),
                      (sx + w - t, sy + t / 2), (sx + w - t / 2, sy + t),
                      (sx + t / 2, sy + t), (sx + t, sy + t / 2)], fill=colour)
    for name, (sx, sy, sy2) in vertical.items():
        colour = LED_ON if name in lit else LED_OFF
        draw.polygon([(sx, sy + t / 2), (sx + t / 2, sy),
                      (sx + t, sy + t / 2), (sx + t, sy2 - t / 2),
                      (sx + t / 2, sy2), (sx, sy2 - t / 2)], fill=colour)


def _counter(draw, box, value):
    """The three-digit red readout, zero-padded like the original."""
    x0, y0, x1, y1 = box
    draw.rectangle(box, fill=LED_BG)
    text = f"{max(0, min(999, int(value))):03d}"
    gap = 4
    dw = (x1 - x0 - gap * 4) / 3
    for i, char in enumerate(text):
        dx = x0 + gap + i * (dw + gap)
        _digit(draw, (dx, y0 + gap, dx + dw, y1 - gap), int(char))


def _smiley(draw, box, mood):
    """Neutral while playing, sunglasses on a clear, dead on a mine."""
    x0, y0, x1, y1 = box
    draw.rectangle(box, fill=FACE)
    _bevel(draw, box, PANEL_BEVEL, raised=True)
    inset = PANEL_BEVEL + 4
    face = (x0 + inset, y0 + inset, x1 - inset, y1 - inset)
    draw.ellipse(face, fill="#FFFF00", outline=TEXT, width=2)

    fx0, fy0, fx1, fy1 = face
    w, h = fx1 - fx0, fy1 - fy0
    eye_y = fy0 + h * 0.34
    left_x, right_x = fx0 + w * 0.31, fx0 + w * 0.69
    r = max(2, w * 0.065)

    if mood == "dead":
        for cx in (left_x, right_x):
            s = r * 1.5
            draw.line((cx - s, eye_y - s, cx + s, eye_y + s), fill=TEXT, width=3)
            draw.line((cx + s, eye_y - s, cx - s, eye_y + s), fill=TEXT, width=3)
        draw.ellipse((fx0 + w * 0.38, fy0 + h * 0.58,
                      fx0 + w * 0.62, fy0 + h * 0.82), outline=TEXT, width=3)
    else:
        if mood == "cool":
            draw.rectangle((fx0 + w * 0.14, eye_y - r * 1.4,
                            fx1 - w * 0.14, eye_y + r * 1.1), fill=TEXT)
        else:
            for cx in (left_x, right_x):
                draw.ellipse((cx - r, eye_y - r * 1.3, cx + r, eye_y + r * 1.3),
                             fill=TEXT)
        draw.arc((fx0 + w * 0.24, fy0 + h * 0.38,
                  fx1 - w * 0.24, fy0 + h * 0.86), start=20, end=160,
                 fill=TEXT, width=3)


def _draw_mine(draw, box):
    """Black body, eight spikes, one white glint — as it always was."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    radius = (x1 - x0) * 0.22
    spike = radius * 1.75
    width = max(2, int(radius * 0.30))
    for dx, dy in ((1, 0), (0, 1), (0.707, 0.707), (0.707, -0.707)):
        draw.line((cx - dx * spike, cy - dy * spike,
                   cx + dx * spike, cy + dy * spike), fill=TEXT, width=width)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=TEXT)
    glint = radius * 0.36
    draw.ellipse((cx - radius * 0.5, cy - radius * 0.5,
                  cx - radius * 0.5 + glint, cy - radius * 0.5 + glint),
                 fill="#FFFFFF")


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------

def render_board(state: game.GameState, highlight: str = "") -> bytes:
    """Render the board to PNG bytes. `highlight` rings a coordinate."""
    grid = display_grid(state)
    grid_w, grid_h = state.cols * CELL, state.rows * CELL

    grid_x = OUTER_BEVEL + PAD + LABEL_LEFT + PANEL_BEVEL
    grid_y = (OUTER_BEVEL + PAD + HEADER_H + PAD + LABEL_TOP + PANEL_BEVEL)
    width = grid_x + grid_w + PANEL_BEVEL + PAD + OUTER_BEVEL
    height = grid_y + grid_h + PANEL_BEVEL + PAD + OUTER_BEVEL

    img = Image.new("RGB", (width, height), FACE)
    draw = ImageDraw.Draw(img)

    # The window itself.
    _bevel(draw, (0, 0, width, height), OUTER_BEVEL, raised=True)

    # Header: sunken panel spanning the grid, counters at the ends, smiley
    # centred. Left counter is the mine count; right is the turn number,
    # which is this game's equivalent of the original's clock.
    hx0 = OUTER_BEVEL + PAD
    hx1 = width - OUTER_BEVEL - PAD
    hy0 = OUTER_BEVEL + PAD
    hy1 = hy0 + HEADER_H
    draw.rectangle((hx0, hy0, hx1, hy1), fill=FACE)
    _bevel(draw, (hx0, hy0, hx1, hy1), PANEL_BEVEL, raised=False)

    inset = PANEL_BEVEL + 10
    counter_w, counter_h = 108, HEADER_H - inset * 2
    _counter(draw, (hx0 + inset, hy0 + inset,
                    hx0 + inset + counter_w, hy0 + inset + counter_h),
             state.mine_count)
    _counter(draw, (hx1 - inset - counter_w, hy0 + inset,
                    hx1 - inset, hy0 + inset + counter_h),
             state.turn_number)

    mood = {game.CLEARED: "cool", game.EXPLODED: "dead"}.get(state.status, "happy")
    face_size = HEADER_H - inset * 2
    fx = (hx0 + hx1) / 2 - face_size / 2
    _smiley(draw, (fx, hy0 + inset, fx + face_size, hy0 + inset + face_size), mood)

    # Labels, in the grey margin — the one thing the original does not have,
    # and the one thing a voter cannot play without.
    label_font = _font(20)
    letters = game.row_letters(state.rows)
    for c in range(state.cols):
        draw.text((grid_x + c * CELL + CELL // 2, grid_y - PANEL_BEVEL - LABEL_TOP // 2),
                  str(c + 1), fill=TEXT, font=label_font, anchor="mm")
    for r in range(state.rows):
        draw.text((grid_x - PANEL_BEVEL - LABEL_LEFT // 2, grid_y + r * CELL + CELL // 2),
                  letters[r], fill=TEXT, font=label_font, anchor="mm")

    # The playing field: one sunken panel, tiles inside it.
    _bevel(draw, (grid_x - PANEL_BEVEL, grid_y - PANEL_BEVEL,
                  grid_x + grid_w + PANEL_BEVEL, grid_y + grid_h + PANEL_BEVEL),
           PANEL_BEVEL, raised=False)

    cell_font = _font(int(CELL * 0.62))
    for r in range(state.rows):
        for c in range(state.cols):
            x0, y0 = grid_x + c * CELL, grid_y + r * CELL
            box = (x0, y0, x0 + CELL, y0 + CELL)
            token = grid[r][c]

            if token == HIDDEN:
                draw.rectangle(box, fill=FACE)
                _bevel(draw, box, TILE_BEVEL, raised=True)
                continue

            # Opened cells are flat, separated by a single grey rule — the
            # absence of a bevel is what makes them read as already cleared.
            fill = EXPLODED_CELL if token == EXPLODED else FACE
            draw.rectangle(box, fill=fill)
            draw.line((x0, y0, x0 + CELL, y0), fill=GRID_LINE)
            draw.line((x0, y0, x0, y0 + CELL), fill=GRID_LINE)

            if token in (MINE, EXPLODED):
                _draw_mine(draw, box)
            elif token != "0":
                draw.text((x0 + CELL // 2, y0 + CELL // 2 - 1), token,
                          fill=NUMBER_COLORS[int(token)], font=cell_font,
                          anchor="mm")

    if highlight and state.in_bounds(highlight):
        r, c = game.coord_to_index(highlight)
        x0, y0 = grid_x + c * CELL, grid_y + r * CELL
        draw.rectangle((x0 + 2, y0 + 2, x0 + CELL - 2, y0 + CELL - 2),
                       outline=HIGHLIGHT, width=2)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Alt text
# ---------------------------------------------------------------------------

ALT_LIMIT = 1800     # Bluesky allows more; this leaves headroom.


def build_alt_text(state: game.GameState) -> str:
    """Describe the board completely enough to play from.

    A 9x9 position fits in a few hundred characters, so unlike the Battleship
    boards this is not a summary — it is the whole position, cell by cell. A
    screen-reader user can reconstruct the grid exactly and vote on the same
    footing as everyone else.
    """
    grid = display_grid(state)
    letters = game.row_letters(state.rows)
    opened, total = len(state.revealed), state.total_safe

    when = (f"turn {state.turn_number}" if state.turn_number
            else "a new board")
    head = (f"Minesweeper, {when}. "
            f"{state.rows} by {state.cols} grid, rows A to {letters[-1]}, "
            f"columns 1 to {state.cols}. "
            f"{state.mine_count} mines. "
            f"{opened} of {total} safe cells opened.")

    legend = ("Reading each row left to right: a digit is how many mines touch "
              "that cell, 0 is an opened blank, ? is still hidden")
    if state.status != game.ACTIVE:
        legend += ", * is a mine, X is the mine that was hit"
    legend += "."

    rows = [f"{letters[r]}: " + " ".join(grid[r]) for r in range(state.rows)]

    tail = ""
    if state.last_coord and state.last_result:
        if state.last_result == game.MINE:
            tail = f"Last move {state.last_coord} hit a mine."
        else:
            try:
                count = state.revealed[game.coord_to_index(state.last_coord)]
                touching = ("touches no mines" if count == 0
                            else f"touches {count} mine{'s' if count != 1 else ''}")
                tail = f"Last move {state.last_coord} was safe and {touching}."
            except (KeyError, ValueError):
                tail = f"Last move {state.last_coord} was safe."

    parts = [head, legend] + rows + ([tail] if tail else [])
    text = "\n".join(parts)
    if len(text) > ALT_LIMIT:
        text = text[:ALT_LIMIT - 1] + "…"
    return text

"""Board image generation and screen-reader alt text.

Both outputs are built from one pure function, `display_grid`, so the picture
and the words can never describe different positions. That function is also
the single place where the hidden layer can become visible, and it only does
so once the game is over — which makes "the bot never shows the mines during
play" a property one small function is responsible for, and one test can pin.

Layout (9x9 at CELL=54):
    row labels  x 0..44        grid x 44..530      right margin 20
    header      y 0..52        column labels ~y 66
    grid        y 80..566      footer y 566..596
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

import game

CELL = 54
LABEL_GUTTER = 44
HEADER_H = 52
COL_LABEL_H = 28
FOOTER_H = 30
MARGIN = 20

# Display tokens. These are what the alt text prints and what the drawing
# code switches on.
HIDDEN = "?"
MINE = "*"
EXPLODED = "X"

BG = "#0d1117"
HEADER_BG = "#1c2532"
GRID_LINE = "#2d3748"
# Hidden vs. opened-blank is the distinction a voter reads the board by, so
# these two are pushed well apart in value rather than being neighbouring
# shades of slate.
HIDDEN_FILL = "#5a6b82"
HIDDEN_LIGHT = "#7d90a8"
HIDDEN_DARK = "#3c4757"
OPEN_FILL = "#121821"
MINE_FILL = "#7f1d1d"
EXPLODED_FILL = "#ef4444"
LABEL = "#8b949e"
TITLE = "#e6edf3"
HIGHLIGHT = "#fbbf24"

# Classic Minesweeper numerals, lifted toward the light end so they hold up
# on a dark board (plain black for 7 would vanish).
NUMBER_COLORS = {
    1: "#4d9fff", 2: "#4ade80", 3: "#ff6b6b", 4: "#c084fc",
    5: "#fbbf24", 6: "#22d3ee", 7: "#f472b6", 8: "#cbd5e1",
}

_FONT_PATHS = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
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
# Image
# ---------------------------------------------------------------------------

def _draw_mine(draw: ImageDraw.ImageDraw, cx: int, cy: int) -> None:
    radius = CELL // 5
    for dx, dy in ((1, 0), (0, 1), (0.7, 0.7), (0.7, -0.7)):
        length = radius * 1.7
        draw.line((cx - dx * length, cy - dy * length,
                   cx + dx * length, cy + dy * length), fill="#0d1117", width=3)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                 fill="#0d1117")
    draw.ellipse((cx - radius // 2, cy - radius // 2,
                  cx - radius // 6, cy - radius // 6), fill="#94a3b8")


def render_board(state: game.GameState, highlight: str = "") -> bytes:
    """Render the board to PNG bytes. `highlight` rings a coordinate."""
    grid = display_grid(state)
    grid_w = state.cols * CELL
    grid_h = state.rows * CELL
    width = LABEL_GUTTER + grid_w + MARGIN
    height = HEADER_H + COL_LABEL_H + grid_h + FOOTER_H
    grid_x, grid_y = LABEL_GUTTER, HEADER_H + COL_LABEL_H

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    title_font = _font(20)
    label_font = _font(15)
    cell_font = _font(28)

    # Header.
    draw.rectangle((0, 0, width, HEADER_H), fill=HEADER_BG)
    draw.text((LABEL_GUTTER, HEADER_H // 2), "MINESWEEPER",
              fill=TITLE, font=title_font, anchor="lm")
    draw.text((width - MARGIN, HEADER_H // 2), f"{state.mine_count} MINES",
              fill=LABEL, font=label_font, anchor="rm")

    # Column numbers along the top, row letters down the side.
    letters = game.row_letters(state.rows)
    for c in range(state.cols):
        draw.text((grid_x + c * CELL + CELL // 2, HEADER_H + COL_LABEL_H // 2),
                  str(c + 1), fill=LABEL, font=label_font, anchor="mm")
    for r in range(state.rows):
        draw.text((LABEL_GUTTER // 2 + 4, grid_y + r * CELL + CELL // 2),
                  letters[r], fill=LABEL, font=label_font, anchor="mm")

    for r in range(state.rows):
        for c in range(state.cols):
            x0, y0 = grid_x + c * CELL, grid_y + r * CELL
            x1, y1 = x0 + CELL, y0 + CELL
            token = grid[r][c]

            if token == HIDDEN:
                draw.rectangle((x0, y0, x1, y1), fill=HIDDEN_FILL)
                # Bevel: light on the top-left, dark on the bottom-right, so
                # unopened cells read as raised the way they do in the game.
                draw.line((x0, y1, x0, y0, x1, y0), fill=HIDDEN_LIGHT, width=3)
                draw.line((x1, y0, x1, y1, x0, y1), fill=HIDDEN_DARK, width=3)
            elif token in (MINE, EXPLODED):
                draw.rectangle((x0, y0, x1, y1),
                               fill=EXPLODED_FILL if token == EXPLODED else MINE_FILL,
                               outline=GRID_LINE)
                _draw_mine(draw, x0 + CELL // 2, y0 + CELL // 2)
            else:
                draw.rectangle((x0, y0, x1, y1), fill=OPEN_FILL, outline=GRID_LINE)
                number = int(token)
                if number:
                    draw.text((x0 + CELL // 2, y0 + CELL // 2), token,
                              fill=NUMBER_COLORS[number], font=cell_font,
                              anchor="mm")

    if highlight and state.in_bounds(highlight):
        r, c = game.coord_to_index(highlight)
        x0, y0 = grid_x + c * CELL, grid_y + r * CELL
        draw.rectangle((x0 + 1, y0 + 1, x0 + CELL - 1, y0 + CELL - 1),
                       outline=HIGHLIGHT, width=3)

    opened, total = len(state.revealed), state.total_safe
    draw.text((LABEL_GUTTER, height - FOOTER_H // 2),
              f"TURN {state.turn_number}" if state.turn_number else "NEW BOARD",
              fill=LABEL, font=label_font, anchor="lm")
    draw.text((width - MARGIN, height - FOOTER_H // 2),
              f"{opened}/{total} CLEARED", fill=LABEL, font=label_font,
              anchor="rm")

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

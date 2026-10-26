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
from datetime import date, datetime

from PIL import Image, ImageDraw, ImageFont

import game

# Display tokens. These are what the alt text prints and what the drawing
# code switches on.
HIDDEN = "?"
MINE = "*"
EXPLODED = "X"
FLAG = "F"
CHECK = "C"

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


def display_grid(state: game.GameState, flags=(), checks=()) -> list:
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
            elif (r, c) in state.spent_mines:
                # Already detonated: everybody watched it go off, so showing
                # it leaks nothing and helps the survivors. The guarantee is
                # unchanged for mines nobody has hit.
                row.append(EXPLODED)
            elif over and (r, c) in state.mine_cells:
                row.append(EXPLODED
                           if game.index_to_coord(r, c) == state.exploded_cell
                           else MINE)
            elif game.index_to_coord(r, c) in flags:
                # A flag is what the crowd claims, never what the board knows.
                # Right and wrong flags look identical until the cell opens.
                row.append(FLAG)
            elif game.index_to_coord(r, c) in checks:
                # A note between players, below a flag: a cell can be both,
                # and the bet is the stronger thing to show.
                row.append(CHECK)
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
    # The original goes negative when you over-flag, and so does this.
    value = max(-99, min(999, int(value)))
    text = f"-{abs(value):02d}" if value < 0 else f"{value:03d}"
    gap = 4
    dw = (x1 - x0 - gap * 4) / 3
    for i, char in enumerate(text):
        dx = x0 + gap + i * (dw + gap)
        _digit(draw, (dx, y0 + gap, dx + dw, y1 - gap),
               "-" if char == "-" else int(char))


# ---------------------------------------------------------------------------
# Holidays: the smiley dresses up for a few days a year
# ---------------------------------------------------------------------------

HALLOWEEN = "halloween"
CHRISTMAS = "christmas"
EASTER = "easter"
NEW_YEAR = "new_year"
VALENTINES = "valentines"
ST_PATRICKS = "st_patricks"
APRIL_FOOLS = "april_fools"
PI_DAY = "pi_day"
FRIDAY_13TH = "friday_13th"
THANKSGIVING = "thanksgiving"
LUNAR_NEW_YEAR = "lunar_new_year"
BIRTHDAY = "birthday"

# The first commit. Move it if the bot's public debut should count instead.
BOT_BIRTHDAY = (9, 1)

# The lunar new year has no closed form worth carrying; these are the
# published dates. Past the end of the table the costume simply stops.
LUNAR_NEW_YEARS = {
    2026: date(2026, 2, 17), 2027: date(2027, 2, 6), 2028: date(2028, 1, 26),
    2029: date(2029, 2, 13), 2030: date(2030, 2, 3), 2031: date(2031, 1, 23),
    2032: date(2032, 2, 11), 2033: date(2033, 1, 31), 2034: date(2034, 2, 19),
    2035: date(2035, 2, 8), 2036: date(2036, 1, 28), 2037: date(2037, 2, 15),
    2038: date(2038, 2, 4), 2039: date(2039, 1, 24), 2040: date(2040, 2, 12),
}


def easter_sunday(year: int) -> date:
    """Western (Gregorian) Easter, by the anonymous Meeus/Jones/Butcher rule."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month, day = divmod(h + el - 7 * m + 114, 31)
    return date(year, month, day + 1)


def thanksgiving(year: int) -> date:
    """US Thanksgiving: the fourth Thursday of November."""
    first = date(year, 11, 1)
    return date(year, 11, 1 + (3 - first.weekday()) % 7 + 21)


def holiday(day: date | None = None) -> str | None:
    """Which costume the smiley wears on `day`, in the bot's local time.

    Earlier rules win where two days can coincide: Easter can fall on
    April Fools' Day, and Friday the 13th is only a fallback.
    """
    day = day or datetime.now().astimezone().date()
    md = (day.month, day.day)
    if day == easter_sunday(day.year):
        return EASTER
    if day == thanksgiving(day.year):
        return THANKSGIVING
    if day == LUNAR_NEW_YEARS.get(day.year):
        return LUNAR_NEW_YEAR
    if (10, 25) <= md <= (10, 31):
        return HALLOWEEN
    if (12, 18) <= md <= (12, 25):
        return CHRISTMAS
    fixed = {(12, 31): NEW_YEAR, (1, 1): NEW_YEAR, (2, 14): VALENTINES,
             (3, 14): PI_DAY, (3, 17): ST_PATRICKS, (4, 1): APRIL_FOOLS,
             BOT_BIRTHDAY: BIRTHDAY}
    if md in fixed:
        return fixed[md]
    if day.day == 13 and day.weekday() == 4:
        return FRIDAY_13TH
    return None


YELLOW = "#FFFF00"
PUMPKIN = "#FF8C1A"
PUMPKIN_RIB = "#D96A00"
STEM = "#3C7A1E"
SANTA_RED = "#D01010"
FUR = "#FFFFFF"
BUNNY = "#EEEEEE"
BUNNY_PINK = "#FF9EC4"
GOLD = "#FFC400"
SHAMROCK = "#1E9A3C"
HEART = "#E0103A"
BLUSH = "#FF8FA8"
MASK = "#F2F2EA"
LANTERN = "#D81E1E"
BEAK = "#FF9A00"
FEATHERS = ["#8B4513", "#D2691E", "#E8A33D", "#B22222", "#8B4513",
            "#D2691E", "#E8A33D"]
CONFETTI = ["#FF3B3B", "#2E7DFF", "#1E9A3C", "#FFC400", "#C040FF"]

FACE_FILL = {HALLOWEEN: PUMPKIN, EASTER: BUNNY, FRIDAY_13TH: MASK,
             LUNAR_NEW_YEAR: LANTERN}

# Costumes worn on top of the head, which need the face to sit lower.
_HEADROOM = {CHRISTMAS: 0.22, EASTER: 0.16, NEW_YEAR: 0.26,
             ST_PATRICKS: 0.26, BIRTHDAY: 0.24, THANKSGIVING: 0.22}


def _heart(draw, cx, cy, s, fill):
    """A heart `2s` wide, centred on (cx, cy)."""
    draw.ellipse((cx - s, cy - s * 0.8, cx, cy + s * 0.2), fill=fill)
    draw.ellipse((cx, cy - s * 0.8, cx + s, cy + s * 0.2), fill=fill)
    draw.polygon([(cx - s * 0.97, cy - s * 0.15), (cx + s * 0.97, cy - s * 0.15),
                  (cx, cy + s)], fill=fill)


def _top_hat(draw, face, top, crown, band, buckle=None):
    """A stovepipe sitting on the head, brim just above the eyes."""
    fx0, fy0, fx1, fy1 = face
    w, h = fx1 - fx0, fy1 - fy0
    brim_y = fy0 + h * 0.10
    draw.rectangle((fx0 + w * 0.20, top, fx1 - w * 0.20, brim_y),
                   fill=crown, outline=TEXT)
    draw.rectangle((fx0 + w * 0.20, brim_y - h * 0.15, fx1 - w * 0.20, brim_y - h * 0.04),
                   fill=band)
    if buckle:
        draw.rectangle((fx0 + w * 0.42, brim_y - h * 0.17, fx0 + w * 0.58, brim_y - h * 0.02),
                       outline=buckle, width=2)
    draw.rectangle((fx0 - w * 0.02, brim_y - h * 0.03, fx1 + w * 0.02, brim_y + h * 0.05),
                   fill=crown, outline=TEXT)


def _smiley(draw, box, mood, theme=None):
    """Neutral while playing, sunglasses on a clear, dead on a mine.

    `theme` is a holiday from `holiday()`. It changes the costume, never the
    mood: a jack-o-lantern still puts sunglasses on for a clear. April Fools'
    is the exception that is not drawn here — `render_board` turns the
    finished face upside down.
    """
    x0, y0, x1, y1 = box
    draw.rectangle(box, fill=FACE)
    _bevel(draw, box, PANEL_BEVEL, raised=True)
    inset = PANEL_BEVEL + 4
    face = (x0 + inset, y0 + inset, x1 - inset, y1 - inset)
    top = y0 + PANEL_BEVEL + 1          # highest a hat or an ear may reach

    if theme in _HEADROOM:
        shrink = (face[2] - face[0]) * _HEADROOM[theme]
        face = (face[0] + shrink / 2, face[1] + shrink, face[2] - shrink / 2,
                face[3])
    fx0, fy0, fx1, fy1 = face
    w, h = fx1 - fx0, fy1 - fy0
    cx = fx0 + w / 2

    # Behind the head.
    if theme == EASTER:
        for ex, lean in ((fx0 + w * 0.30, -0.06), (fx0 + w * 0.70, 0.06)):
            draw.ellipse((ex - w * 0.15 + w * lean, top,
                          ex + w * 0.15 + w * lean, fy0 + h * 0.35),
                         fill=BUNNY, outline=TEXT, width=2)
            draw.ellipse((ex - w * 0.06 + w * lean, top + h * 0.12,
                          ex + w * 0.06 + w * lean, fy0 + h * 0.18),
                         fill=BUNNY_PINK)
    elif theme == THANKSGIVING:
        # A tail fan, one slice per feather, spread above the head.
        fan_cy = fy0 + h * 0.55
        rx = (x1 - x0) / 2 - PANEL_BEVEL - 1
        ry = fan_cy - top
        fan = (cx - rx, fan_cy - ry, cx + rx, fan_cy + ry)
        span = 200 / len(FEATHERS)
        for i, colour in enumerate(FEATHERS):
            draw.pieslice(fan, 170 + i * span, 170 + (i + 1) * span,
                          fill=colour, outline=TEXT)
    elif theme == HALLOWEEN:
        draw.rectangle((fx0 + w * 0.44, fy0 - h * 0.10,
                        fx0 + w * 0.58, fy0 + h * 0.10), fill=STEM)

    # The head.
    if theme == LUNAR_NEW_YEAR:
        draw.ellipse(face, fill=LANTERN, outline=GOLD, width=3)
        for cap_y in (fy0, fy1):
            draw.rectangle((cx - w * 0.22, cap_y - h * 0.06, cx + w * 0.22, cap_y + h * 0.06),
                           fill=GOLD, outline=TEXT)
    else:
        draw.ellipse(face, fill=FACE_FILL.get(theme, YELLOW), outline=TEXT, width=2)
    if theme == HALLOWEEN:
        for inner in (0.30, 0.70):
            draw.arc((fx0 + w * inner - w * 0.14, fy0 + 2,
                      fx0 + w * inner + w * 0.14, fy1 - 2),
                     start=90 if inner < 0.5 else 270,
                     end=270 if inner < 0.5 else 90, fill=PUMPKIN_RIB, width=1)
    elif theme == FRIDAY_13TH:
        # Breathing holes and the red chevrons of a goalie mask.
        for px, py in ((0.20, 0.55), (0.80, 0.55), (0.27, 0.66), (0.73, 0.66)):
            draw.ellipse((fx0 + w * px - 1.2, fy0 + h * py - 1.2,
                          fx0 + w * px + 1.2, fy0 + h * py + 1.2), fill=TEXT)
        for side in (-1, 1):
            draw.polygon([(cx + side * w * 0.06, fy0 + h * 0.07),
                          (cx + side * w * 0.15, fy0 + h * 0.13),
                          (cx + side * w * 0.06, fy0 + h * 0.19)], fill=SANTA_RED)
    elif theme == VALENTINES:
        for bx in (0.24, 0.76):
            draw.ellipse((fx0 + w * (bx - 0.09), fy0 + h * 0.52,
                          fx0 + w * (bx + 0.09), fy0 + h * 0.64), fill=BLUSH)

    eye_y = fy0 + h * 0.34
    left_x, right_x = fx0 + w * 0.31, fx0 + w * 0.69
    r = max(2, w * 0.065)

    # Eyes.
    if mood == "dead":
        for ex in (left_x, right_x):
            s = r * 1.5
            draw.line((ex - s, eye_y - s, ex + s, eye_y + s), fill=TEXT, width=3)
            draw.line((ex + s, eye_y - s, ex - s, eye_y + s), fill=TEXT, width=3)
    elif mood == "cool":
        draw.rectangle((fx0 + w * 0.14, eye_y - r * 1.4,
                        fx1 - w * 0.14, eye_y + r * 1.1), fill=TEXT)
    elif theme == HALLOWEEN:
        for ex in (left_x, right_x):
            s = r * 1.6
            draw.polygon([(ex, eye_y - s), (ex + s, eye_y + s * 0.8),
                          (ex - s, eye_y + s * 0.8)], fill=TEXT)
    elif theme == VALENTINES:
        for ex in (left_x, right_x):
            _heart(draw, ex, eye_y, r * 1.7, HEART)
    else:
        for ex in (left_x, right_x):
            draw.ellipse((ex - r, eye_y - r * 1.3, ex + r, eye_y + r * 1.3),
                         fill=TEXT)

    # Between the eyes and the mouth.
    if theme == EASTER:
        ny = fy0 + h * 0.55
        draw.polygon([(fx0 + w * 0.43, ny - h * 0.05), (fx0 + w * 0.57, ny - h * 0.05),
                      (cx, ny + h * 0.04)], fill=BUNNY_PINK, outline=TEXT)
        for side in (-1, 1):
            for dy in (-0.04, 0.04):
                draw.line((fx0 + w * (0.5 + side * 0.14), ny + h * dy,
                           fx0 + w * (0.5 + side * 0.48), ny + h * dy * 2.5),
                          fill=TEXT, width=1)
    elif theme == THANKSGIVING:
        by = fy0 + h * 0.50
        draw.polygon([(cx - w * 0.09, by - h * 0.05), (cx + w * 0.09, by - h * 0.05),
                      (cx, by + h * 0.10)], fill=BEAK, outline=TEXT)

    # Mouth.
    if mood == "dead":
        draw.ellipse((fx0 + w * 0.38, fy0 + h * 0.58,
                      fx0 + w * 0.62, fy0 + h * 0.82), outline=TEXT, width=3)
    elif theme == HALLOWEEN:
        # The carved zig-zag grin.
        my, bottom = fy0 + h * 0.58, fy0 + h * 0.80
        xs = [fx0 + w * (0.22 + 0.56 * i / 6) for i in range(7)]
        upper = [(x, my + (h * 0.07 if i % 2 else 0)) for i, x in enumerate(xs)]
        draw.polygon(upper + [(xs[-1] - w * 0.06, bottom - h * 0.04),
                              (cx, bottom),
                              (xs[0] + w * 0.06, bottom - h * 0.04)], fill=TEXT)
    elif theme == EASTER:
        ny = fy0 + h * 0.59
        draw.line((cx, ny, cx, ny + h * 0.08), fill=TEXT, width=2)
        draw.arc((fx0 + w * 0.32, ny - h * 0.06, cx, ny + h * 0.12),
                 start=20, end=160, fill=TEXT, width=2)
        draw.arc((cx, ny - h * 0.06, fx0 + w * 0.68, ny + h * 0.12),
                 start=20, end=160, fill=TEXT, width=2)
        draw.rectangle((fx0 + w * 0.43, ny + h * 0.12, fx0 + w * 0.57, ny + h * 0.24),
                       fill=FUR, outline=TEXT)
        draw.line((cx, ny + h * 0.12, cx, ny + h * 0.24), fill=TEXT, width=1)
    elif theme == PI_DAY:
        # The smile is a π: a bar with a straight left leg and a hooked right.
        bar_y = fy0 + h * 0.60
        draw.line((fx0 + w * 0.26, bar_y, fx1 - w * 0.26, bar_y), fill=TEXT, width=3)
        draw.line((fx0 + w * 0.40, bar_y, fx0 + w * 0.38, fy0 + h * 0.84),
                  fill=TEXT, width=3)
        draw.line((fx0 + w * 0.60, bar_y, fx0 + w * 0.60, fy0 + h * 0.80),
                  fill=TEXT, width=3)
        draw.line((fx0 + w * 0.60, fy0 + h * 0.80, fx0 + w * 0.67, fy0 + h * 0.84),
                  fill=TEXT, width=3)
    elif theme == FRIDAY_13TH and mood != "cool":
        # No mouth on the mask, only rows of holes where one would be.
        for row, cols in ((0.66, (0.40, 0.50, 0.60)), (0.76, (0.45, 0.55))):
            for px in cols:
                draw.ellipse((fx0 + w * px - 1.5, fy0 + h * row - 1.5,
                              fx0 + w * px + 1.5, fy0 + h * row + 1.5), fill=TEXT)
    else:
        draw.arc((fx0 + w * 0.24, fy0 + h * 0.38,
                  fx1 - w * 0.24, fy0 + h * 0.86), start=20, end=160,
                 fill=TEXT, width=3)

    # On top of the head, last.
    if theme == CHRISTMAS:
        # Santa's cap, flopping right.
        brim_y = fy0 + h * 0.07
        draw.polygon([(fx0 + w * 0.08, brim_y), (fx1 - w * 0.08, brim_y),
                      (fx1 - w * 0.02, top + 1 + h * 0.22), (fx0 + w * 0.55, top + 1),
                      (fx0 + w * 0.20, top + 1 + h * 0.08)],
                     fill=SANTA_RED, outline=TEXT)
        draw.rounded_rectangle((fx0 - w * 0.02, brim_y - h * 0.08,
                                fx1 + w * 0.02, brim_y + h * 0.08),
                               radius=h * 0.08, fill=FUR, outline=TEXT)
        pom = w * 0.11
        px, py = fx1 - w * 0.02, top + 1 + h * 0.22
        draw.ellipse((px - pom, py - pom, px + pom, py + pom), fill=FUR, outline=TEXT)
    elif theme == ST_PATRICKS:
        _top_hat(draw, face, top, SHAMROCK, TEXT, buckle=GOLD)
    elif theme == NEW_YEAR:
        _top_hat(draw, face, top, TEXT, GOLD)
        for sx, sy, colour in ((x0 + 8, y0 + 9, GOLD), (x1 - 9, y0 + 12, GOLD),
                               (x0 + 9, y1 - 14, CONFETTI[0]), (x1 - 9, y1 - 12, CONFETTI[1])):
            draw.line((sx - 3, sy, sx + 3, sy), fill=colour, width=2)
            draw.line((sx, sy - 3, sx, sy + 3), fill=colour, width=2)
    elif theme == BIRTHDAY:
        # A striped cone party hat, a little askew, and confetti.
        base_l, base_r = (fx0 + w * 0.22, fy0 + h * 0.10), (fx0 + w * 0.70, fy0 + h * 0.02)
        tip = (fx0 + w * 0.52, top + 2)
        draw.polygon([base_l, base_r, tip], fill=CONFETTI[1], outline=TEXT)
        for t in (0.33, 0.66):
            lx = base_l[0] + (tip[0] - base_l[0]) * t
            ly = base_l[1] + (tip[1] - base_l[1]) * t
            rx = base_r[0] + (tip[0] - base_r[0]) * t
            ry = base_r[1] + (tip[1] - base_r[1]) * t
            draw.line((lx, ly, rx, ry), fill=GOLD, width=2)
        draw.ellipse((tip[0] - 3, tip[1] - 2, tip[0] + 3, tip[1] + 4), fill=CONFETTI[0])
        for i, (sx, sy) in enumerate(((x0 + 8, y0 + 10), (x1 - 9, y0 + 9),
                                      (x0 + 9, y1 - 12), (x1 - 8, y1 - 14),
                                      (x1 - 12, y0 + 20))):
            draw.rectangle((sx - 1.5, sy - 1.5, sx + 1.5, sy + 1.5),
                           fill=CONFETTI[i % len(CONFETTI)])


def _draw_flag(draw, box):
    """The classic red pennant on a black pole."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    pole_x = x0 + w * 0.56
    draw.line((pole_x, y0 + h * 0.24, pole_x, y0 + h * 0.72),
              fill=TEXT, width=max(2, int(w * 0.055)))
    draw.rectangle((x0 + w * 0.28, y0 + h * 0.70, x0 + w * 0.72, y0 + h * 0.78),
                   fill=TEXT)
    draw.polygon([(pole_x, y0 + h * 0.22), (pole_x, y0 + h * 0.50),
                  (x0 + w * 0.26, y0 + h * 0.36)], fill="#FF0000")


def _draw_check(draw, box, font):
    """A cell marked to check: a bold black question mark on the tile.

    Classic Minesweeper's own symbol for "not sure", which is exactly what
    the mark means. A tick read as "safe" to some people, the opposite.
    """
    x0, y0, x1, y1 = box
    draw.text(((x0 + x1) / 2, (y0 + y1) / 2 - 1), "?", fill=TEXT, font=font,
              anchor="mm")


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

def render_board(state: game.GameState, highlight: str = "",
                 flags=(), day: date | None = None, checks=()) -> bytes:
    """Render the board to PNG bytes. `highlight` rings a coordinate.

    `day` picks the smiley's holiday costume and defaults to today.
    """
    grid = display_grid(state, flags, checks)
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
             state.mine_count - len(flags) - state.detonations)
    _counter(draw, (hx1 - inset - counter_w, hy0 + inset,
                    hx1 - inset, hy0 + inset + counter_h),
             state.turn_number)

    mood = {game.CLEARED: "cool", game.EXPLODED: "dead"}.get(state.status, "happy")
    face_size = HEADER_H - inset * 2
    fx = (hx0 + hx1) / 2 - face_size / 2
    button = (fx, hy0 + inset, fx + face_size, hy0 + inset + face_size)
    theme = holiday(day)
    _smiley(draw, button, mood, theme)
    if theme == APRIL_FOOLS:
        # Upside down. Only the face inside the bevel turns, so the button
        # still reads as raised.
        bx0, by0, bx1, by1 = (round(v) for v in button)
        inner = (bx0 + PANEL_BEVEL, by0 + PANEL_BEVEL,
                 bx1 - PANEL_BEVEL, by1 - PANEL_BEVEL)
        img.paste(img.crop(inner).rotate(180), inner[:2])

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

            if token in (HIDDEN, FLAG, CHECK):
                draw.rectangle(box, fill=FACE)
                _bevel(draw, box, TILE_BEVEL, raised=True)
                if token == FLAG:
                    _draw_flag(draw, box)
                elif token == CHECK:
                    _draw_check(draw, box, cell_font)
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


def build_alt_text(state: game.GameState, flags=(), checks=()) -> str:
    """Describe the board completely enough to play from.

    A 9x9 position fits in a few hundred characters, so unlike the Battleship
    boards this is not a summary — it is the whole position, cell by cell. A
    screen-reader user can reconstruct the grid exactly and vote on the same
    footing as everyone else.
    """
    grid = display_grid(state, flags, checks)
    letters = game.row_letters(state.rows)
    opened, total = len(state.revealed), state.total_safe
    # Counted from the grid, so a mark on an opened or flagged cell, which
    # does not show, is not counted either.
    marked = sum(row.count(CHECK) for row in grid)
    notes = ([f"{len(flags)} flagged"] if flags else []) + (
        [f"{marked} marked to check"] if marked else [])

    when = (f"turn {state.turn_number}" if state.turn_number
            else "a new board")
    head = (f"Minesweeper, {when}. "
            f"{state.rows} by {state.cols} grid, rows A to {letters[-1]}, "
            f"columns 1 to {state.cols}. "
            f"{state.mine_count} mines. "
            f"{opened} of {total} safe cells opened"
            + "".join(f", {note}" for note in notes) + ".")

    legend = ("Reading each row left to right: a digit is how many mines touch "
              "that cell, 0 is an opened blank, ? is still hidden")
    if flags:
        legend += ", F is a cell somebody has flagged as a mine"
    if marked:
        legend += ", C is a cell somebody has marked to check (maybe a mine)"
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

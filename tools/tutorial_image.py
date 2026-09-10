#!/usr/bin/env python3
"""Generate assets/tutorial.png for the pinned how-to post."""

from __future__ import annotations

import io
import os
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import game
import renderer


def main() -> None:
    # A small, intentionally legible position illustrating all three actions.
    state = game.GameState(
        game_id=0, rows=5, cols=5, mine_count=4,
        mine_cells={(0, 0), (0, 3), (2, 2), (4, 4)},
        revealed={
            (1, 0): 1, (1, 1): 2, (1, 2): 2, (1, 3): 2, (1, 4): 1,
            (2, 0): 0, (2, 1): 1, (2, 3): 1, (2, 4): 0,
            (3, 0): 0, (3, 1): 1, (3, 2): 1, (3, 3): 2, (3, 4): 1,
        },
    )
    board = Image.open(io.BytesIO(
        renderer.render_board(state, highlight="D4", flags={"C3"})))
    margin, panel_h = 48, 270
    width = 900
    image = Image.new("RGB", (width, board.height + panel_h + margin * 2),
                      "#F4F4F0")
    board_x = (width - board.width) // 2
    image.paste(board, (board_x, margin))
    draw = ImageDraw.Draw(image)
    title = renderer._font(38)
    body = renderer._font(25)
    small = renderer._font(21)
    y = board.height + margin + 22
    draw.text((margin, y), "PLAY IN ONE REPLY", font=title, fill="#111111")
    y += 56
    examples = [
        ("D4", "Open one cell"),
        ("Flag C3", "Mark a suspected mine — flags cost no turn"),
        ("C3 is a mine, so D4 is safe — D4", "Explain the deduction, then vote"),
    ]
    for command, explanation in examples:
        draw.text((margin, y), command, font=body, fill="#000080")
        y += 31
        draw.text((margin + 18, y), explanation, font=small, fill="#333333")
        y += 38
    output = os.path.join(ROOT, "assets", "tutorial.png")
    image.save(output, format="PNG", optimize=True)
    print(output)


if __name__ == "__main__":
    main()

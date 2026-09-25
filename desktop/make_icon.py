"""Generate ``desktop/blaxcy.png``, the icon the desktop entry names.

The committed PNG is a build artifact, and a build artifact whose generator has
been lost becomes an unreviewable blob, so the generator lives beside it:

    .venv/bin/python desktop/make_icon.py

It needs Pillow, which is already a runtime dependency (section 27) because the
perception layer encodes image crops -- so no dependency is introduced for an icon.
The output is deterministic: same code, same pixels, so a re-run is a no-op and a
diff means the design changed on purpose.

The design states the project's own architecture at a glance: an eye (the EYE that
determines what is actually there) on a dark plate, with the pupil offset like a
gaze. It is deliberately not a screenshot or a logo borrowed from anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

#: Output size in pixels; must match ``installer.installer.ICON_PIXELS``.
SIZE = 256
#: Where the icon is written, relative to the repository root.
OUTPUT = Path(__file__).resolve().parent / "blaxcy.png"

PLATE = (23, 32, 44, 255)
PLATE_EDGE = (58, 76, 98, 255)
SCLERA = (236, 244, 248, 255)
IRIS = (77, 208, 225, 255)
IRIS_DEEP = (26, 145, 166, 255)
PUPIL = (12, 18, 26, 255)
GLINT = (255, 255, 255, 210)


def build_icon(size: int = SIZE) -> Image.Image:
    """Render the BLAXCY icon at ``size`` pixels square."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    scale = size / 256.0

    def px(value: float) -> float:
        return value * scale

    # Rounded plate.
    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=px(54),
        fill=PLATE,
        outline=PLATE_EDGE,
        width=max(1, int(px(4))),
    )

    # Eye: an almond outline approximated by two arcs inside a bounding box.
    left, top, right, bottom = px(34), px(66), px(222), px(190)
    draw.ellipse((left, top, right, bottom), fill=SCLERA)

    # Iris and pupil, offset slightly right so it reads as a gaze rather than a
    # target symbol.
    iris_left, iris_top = px(94), px(80)
    iris_right, iris_bottom = px(166), px(176)
    draw.ellipse((iris_left, iris_top, iris_right, iris_bottom), fill=IRIS)
    inner = px(14)
    draw.ellipse(
        (iris_left + inner, iris_top + inner, iris_right - inner, iris_bottom - inner),
        fill=IRIS_DEEP,
    )
    pupil_inset = px(34)
    draw.ellipse(
        (iris_left + pupil_inset, iris_top + pupil_inset, iris_right - pupil_inset, iris_bottom - pupil_inset),
        fill=PUPIL,
    )

    # Catchlight.
    draw.ellipse((px(116), px(98), px(134), px(116)), fill=GLINT)

    # Upper lid line, so the shape reads as an eye and not as a coin.
    draw.arc(
        (px(30), px(58), px(226), px(206)),
        start=188,
        end=352,
        fill=PLATE_EDGE,
        width=max(1, int(px(7))),
    )
    return image


def main(argv: list[str] | None = None) -> int:
    """Write the icon and report the path and its size."""
    del argv
    icon = build_icon()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    icon.save(OUTPUT, format="PNG", optimize=True)
    print(f"wrote {OUTPUT} ({icon.width}x{icon.height}, {OUTPUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry
    raise SystemExit(main(sys.argv[1:]))

"""Verify the Lightning Castle upgrade controls and read their counters."""

from pathlib import Path

import numpy as np
from PIL import Image


GEMS = (289, 2, 399, 36)
LEVEL = (316, 421, 456, 455)
PRICE = (902, 480, 965, 517)
UI = {
    "toggle": ((341, 419, 418, 499), 23),
    "castle_slot": ((343, 325, 417, 408), 24),
    "chain_lightning": ((532, 178, 825, 216), 18),
    "detail_close": ((970, 138, 1020, 181), 24),
    "button": ((806, 434, 983, 474), 23),
    "bulk": ((315, 586, 480, 629), 37),
    "list_title": ((845, 27, 1039, 77), 25),
    "list_close": ((1220, 49, 1275, 99), 26),
}
HOME_TEXT = {
    "town": (20, 66, 148, 105),
    "upgrade": (923, 89, 1218, 129),
    "battle": (1126, 668, 1257, 706),
    "replay": (950, 668, 1084, 706),
}
TITLE = (320, 107, 719, 155)
TITLE_SHIFTS = ((0, 0), (0, 1), (-1, 2), (-1, -2), (-1, -3))
HOME_SHIFTS = ((0, 0), (0, 1), (0, 2), (-1, 1), (1, 1), (0, -1), (-2, 0), (-2, 2))


def crop(rgb, box):
    left, top, right, bottom = box
    return rgb[top:bottom, left:right]


def digit_mask(rgb, palette):
    red, green, blue = rgb.transpose(2, 0, 1)
    if palette == "level":
        return (green > 106) & (green - red > 49) & (green - blue > 49)
    if palette == "cost":
        return (blue > 115) & (blue - red > 46) & (green - red > 40)
    white = max(54, np.quantile(rgb.max(axis=2), 0.995) * 0.59)
    return (rgb.min(axis=2) > white) & (np.ptp(rgb, axis=2) < 35)


def glyphs(mask):
    columns = mask.any(axis=0)
    starts = np.flatnonzero(columns & ~np.r_[False, columns[:-1]])
    ends = np.flatnonzero(columns & ~np.r_[columns[1:], False]) + 1
    for start, end in zip(starts, ends):
        tile = mask[:, start:end]
        rows = np.flatnonzero(tile.any(axis=1))
        yield tile[rows[0]:rows[-1] + 1]


def normalize(glyph):
    return np.asarray(Image.fromarray(glyph).resize((18, 24)), dtype=bool)


def text_mask(rgb):
    return (rgb.min(axis=2) > 145) & (np.ptp(rgb, axis=2) < 48)


class TowerVision:
    def __init__(self):
        with np.load(Path(__file__).with_name("tower_templates.npz"), allow_pickle=False) as atlas:
            self.ui = {key: atlas[key].astype(np.int16) for key in UI}
            self.numbers = {key: atlas[key].astype(bool) for key in ("gems", "level", "cost")}
            self.text = {key: text_mask(atlas[key]) for key in (*HOME_TEXT, "title")}

    def _matches(self, rgb, name, shifts=((0, 0),)):
        box, tolerance = UI[name]
        left, top, right, bottom = box
        return any(np.mean(np.abs(rgb[top + dy:bottom + dy, left + dx:right + dx]
                                  - self.ui[name])) < tolerance for dx, dy in shifts)

    def _text_matches(self, rgb, name, box, shifts, minimum):
        left, top, right, bottom = box
        target = self.text[name]
        for dx, dy in shifts:
            observed = text_mask(rgb[top + dy:bottom + dy, left + dx:right + dx])
            if np.count_nonzero(observed & target) > minimum * np.count_nonzero(observed | target):
                return True
        return False

    def is_home(self, rgb):
        """Check the bright, unobscured controls of the recorded home layout."""
        return all(self._text_matches(rgb, name, box, HOME_SHIFTS, 0.63)
                   for name, box in HOME_TEXT.items())

    def _number(self, rgb, box, palette, limit):
        digits = list(glyphs(digit_mask(crop(rgb, box), palette)))
        if not 1 <= len(digits) <= limit:
            return None
        value = 0
        for glyph in digits:
            height, width = glyph.shape
            if not (18 <= height <= 28 and 11 <= width <= 21):
                return None
            scores = np.count_nonzero(self.numbers[palette] != normalize(glyph),
                                      axis=(2, 3)).min(axis=1)
            best, second = np.argsort(scores)[:2]
            if scores[best] > 72 or scores[second] - scores[best] < 17:
                return None
            value = value * 10 + int(best)
        return value

    def home(self, rgb):
        """Return (gems, edit layout) on a previously verified home screen."""
        rgb = np.asarray(rgb, dtype=np.int16)
        if not self._matches(rgb, "toggle"):
            return None, None
        if self._matches(rgb, "castle_slot"):
            layout = "castle"
        elif all(np.max((rgb[52:66, left:right].max(axis=2) < 95).mean(axis=1)) > 0.85
                 for left, right in ((275, 321), (357, 409), (437, 486))):
            layout = "hero"
        else:
            layout = None
        return self._number(rgb, GEMS, "gems", 5), layout

    def panel(self, rgb):
        """Read a verified Lightning Castle detail or Castles list."""
        rgb = np.asarray(rgb, dtype=np.int16)
        # The detail panel bounces a few pixels while the level changes.
        if (self._text_matches(rgb, "title", TITLE, TITLE_SHIFTS, 0.69)
                and self._matches(rgb, "chain_lightning", TITLE_SHIFTS)
                and self._matches(rgb, "detail_close")):
            gems = self._number(rgb, GEMS, "gems", 5)
            level = self._number(rgb, LEVEL, "level", 7)
            cost = None
            if self._matches(rgb, "button") and not self._matches(rgb, "bulk"):
                value = self._number(rgb, PRICE, "cost", 1)
                if value in (2, 3):
                    cost = value
            return "tower", gems, level, cost
        if self._matches(rgb, "list_title") and self._matches(rgb, "list_close"):
            return "castles", None, None, None
        return None

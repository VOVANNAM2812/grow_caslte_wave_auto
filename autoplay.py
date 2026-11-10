"""Play Grow Castle waves, cast ready skills, and collect gold ads on LDPlayer."""

import argparse
from dataclasses import dataclass, replace
from io import BytesIO
from hashlib import sha256
from pathlib import Path
import re
import subprocess
from time import monotonic, sleep

import numpy as np
from PIL import Image, ImageOps

from captcha import VIEWPORT
from ldplayer_captcha import run, watch

import ad_close
from tower_upgrade import Action, TowerUpgrade, lock_journal
from windows_video import read_video


TEMPLATES = {
    "hud": ((0, 6, 29, 36), 17),
    "hud_dim": ((0, 6, 29, 36), 10),
    "upgrade": ((924, 94, 1217, 129), 25),
    "battle": ((1126, 673, 1256, 702), 20),
    "replay": ((951, 673, 1083, 702), 20),
    "wave": ((615, 40, 707, 68), 13),
    "wave_late": ((615, 40, 707, 68), 0),
    "speed": ((25, 634, 96, 680), 17),
    "gold_coin": ((253, 617, 322, 681), 14),
    "gold_get": ((565, 620, 688, 655), 14),
    "gold_get_centered": ((562, 625, 690, 678), 17),
    "replay_100": ((804, 633, 919, 669), 19),
    "replay_80": ((957, 633, 1072, 669), 19),
    "skip_title": ((475, 242, 797, 294), 16),
    "skip_close": ((866, 194, 911, 238), 17),
    "victory": ((457, 232, 827, 310), 18),
    "processing": ((466, 260, 831, 311), 20),
    "captcha": ((407, 91, 867, 129), 16),
    "captcha_start": ((847, 536, 1003, 623), 14),
    "hp_zero": ((918, 7, 938, 29), 0),
    "reward": ((1068, 26, 1266, 56), 0),
    "reward_light": ((1098, 30, 1268, 54), 0),
    "reward_mixed": ((1060, 22, 1262, 53), 0),
    "ad_end_close": ((1237, 22, 1277, 62), 15),
    "ad_end_cta": ((247, 651, 393, 680), 15),
    "ad_sheet_close": ((1237, 19, 1277, 59), 14),
    "ad_sheet_chevron": ((1219, 136, 1245, 159), 15),
    "ad_sheet_cta": ((958, 535, 1162, 589), 16),
}
GOLD_HUD = (36, 4, 194, 39)
# A ready bar is a cyan pill above the unit that owns it. The first nine
# belong to the Lightning Castle heroes, then the tree and the two snowman
# rows. Value is (bar x, bar y, tap x, tap y).
TARGETS = (
    (293, 146, 293, 184), (373, 146, 373, 184), (453, 146, 453, 184),
    (293, 241, 293, 277), (373, 241, 373, 277), (453, 241, 453, 277),
    (293, 331, 293, 370), (373, 331, 373, 370), (453, 331, 453, 370),
    (119, 143, 119, 181),
    (133, 363, 133, 401), (133, 479, 133, 517),
)
# Cyan ready bars are only drawn on friendly units, so their position doubles
# as proof that this really is a battle screen.
READY_BAR = (18, 3)
# The wave/boss progress pill sits just under the HP/MP bar.
HEALTH = (560, 0, 1260, 64)
GAME_SCREENS = {"home", "gold", "replay_menu", "battle", "skip"}
RETURN_SCREENS = GAME_SCREENS | {"victory", "defeat", "tower", "castles", "captcha"}
AD_CLOSE_SCREENS = {"reward_granted", "ad_end_card", "ad_end_sheet", "ad_unknown_close"}
AD_ACTIVITY = "com.google.android.gms.ads.AdActivity"
CAPTCHA_ATTEMPTS = 3
CAPTCHA_WINDOW = 16
CAPTCHA_PAUSE = 4


def viewport(image):
    """Normalise the game viewport, or None when the picture is not the game.

    Ads can lock themselves to portrait, and 720x1280 is exactly 9:16, not 16:9.
    Refusing that size meant the close button was never even looked for on a
    rotated ad, so an upright frame is now accepted too: every template is
    matched against corner regions, and a portrait ad simply fails those
    matches, which is what the ad path expects.
    """
    width, height = image.size
    ratio = width / height
    if abs(ratio / (16 / 9) - 1) > 0.03 and abs(ratio / (9 / 16) - 1) > 0.03:
        return None
    return image.convert("RGB").resize(VIEWPORT, Image.Resampling.BILINEAR)


def region(array, box):
    left, top, right, bottom = box
    return array[top:bottom, left:right]


def pale_pixels(rgb, minimum):
    return (rgb.max(axis=2) > minimum) & (np.ptp(rgb, axis=2) < 38)


@dataclass(frozen=True)
class Screen:
    name: str
    point: tuple[int, int] | None = None
    ready: tuple[int, ...] = ()
    shifts: tuple[int, ...] = ()
    gold: np.ndarray | None = None
    gems: int | None = None
    layout: str | None = None
    level: int | None = None
    cost: int | None = None
    points: tuple = ()
    sub: str | None = None


def color_runs(mask, minimum, maximum=10_000):
    """Left/right edges of every horizontal run of set pixels."""
    mask = np.asarray(mask, dtype=bool)
    indexes = np.flatnonzero(mask)
    if not len(indexes):
        return []
    breaks = np.flatnonzero(np.diff(indexes) > 1)
    starts = np.r_[indexes[0], indexes[breaks + 1]]
    ends = np.r_[indexes[breaks], indexes[-1]]
    return [(int(start), int(end)) for start, end in zip(starts, ends)
            if minimum <= end - start + 1 <= maximum]


def health_row(rgb):
    """Rows covered by the castle HP/MP bar pair, or None off a battle screen.

    The HP bar empties to black while a boss is being finished, so the MP bar
    alone is enough to locate the pair.
    """
    left, top, right, bottom = HEALTH
    band = rgb[top:bottom, left:right]
    red, green, blue = band.transpose(2, 0, 1)
    health = (red > 130) & (red - green > 60) & (red - blue > 55)
    mana = (blue > 130) & (blue - red > 55) & (blue - green > 30)
    rows, health_rows = [], []
    for y in range(band.shape[0]):
        health_runs = color_runs(health[y], 40, 240)
        mana_runs = color_runs(mana[y], 90, 240)
        if not mana_runs:
            continue
        health_rows.extend(y for _ in health_runs)
        rows.append(y + top)
    if not rows or len(rows) < 5:
        return None
    return (min(rows), max(rows)) if health_rows or len(rows) >= 5 else None


def status_pill(rgb, health):
    """The progress pill under the HP bar: "wave" or "boss", or None.

    A boss wave replaces the white WAVE progress pill with a labelled BOSS
    health bar, which is why the plain templates stop matching mid-fight.
    Every candidate row is inspected before a verdict, because the BOSS label
    itself is a long pale run that looks like a WAVE progress bar: a red fill
    or a wide black frame anywhere in the band outranks a pale run.
    """
    if health is None:
        return None
    top = health[1] + 3
    wave_row = None
    for y in range(top, min(top + 46, rgb.shape[0] - 1)):
        segment = rgb[y, 560:1260]
        red, green, blue = segment.transpose(1, 0)
        fill = (red > 130) & (red - green > 60) & (red - blue > 55)
        frame = (red < 85) & (green < 85) & (blue < 95)
        pale = (red > 170) & (green > 170) & (blue > 175)
        dark = (red < 60) & (green < 60) & (blue < 70)
        # A BOSS bar is a wide black frame holding a red fill whose length is
        # the boss's remaining health: 43 px when nearly dead, over 220 px at
        # half. Its left edge drifts by a dozen pixels when the capture is
        # scaled, so the span is read by length with a wide window.
        long_frame = [run for run in color_runs(frame, 150, 620) if run[1] - run[0] >= 150]
        if long_frame:
            if any(95 <= left <= 240 and right - left >= 20
                   for left, right in color_runs(fill, 20, 480)):
                return "boss", y
            # No red at all: the boss is nearly down and only its black frame
            # is left. That frame starts level with the BOSS label, far left of
            # where the WAVE progress bar begins, and runs on to the right.
            if any(left <= 160 and right >= 500 for left, right in long_frame):
                return "boss", y
        if wave_row is None:
            # The WAVE label sits left of a long pale, late (dark) or full bar.
            if any(190 <= left <= 285 and right - left >= 190
                   for left, right in color_runs(pale, 190, 480)):
                wave_row = y
            elif any(190 <= left <= 285 and right - left >= 285
                     for left, right in color_runs(dark, 285, 480)):
                wave_row = y
    if wave_row is not None:
        return "wave", wave_row
    return None


def wave_pill(rgb, health):
    """A filled WAVE progress pill, whichever shade the bar is in.

    The progress fades from white to black across a wave and a scaled window
    capture blurs its edges, so the pill is recognised from the black frame
    straddling a long run of pale or dark fill instead of by template.
    """
    if health is None:
        return False
    top = health[1] + 3
    for y in range(top, min(top + 46, rgb.shape[0] - 1)):
        segment = rgb[y, 560:1260]
        red, green, blue = segment.transpose(1, 0)
        pale = (red > 185) & (green > 185) & (blue > 190)
        dark = (red < 80) & (green < 80) & (blue < 80)
        for runs, minimum in ((pale, 120), (dark, 150)):
            for left, right in color_runs(runs, minimum, 460):
                if 215 <= left <= 270 and right - left >= minimum:
                    return True
    return False


def ready_bars(rgb, drift=26):
    """Indexes of the cyan skill/hero cooldown bars that are full.

    The tolerant margins let a capture scaled away from 1280 × 720 still count.
    A window capture that kept the emulator's title bar shifts the whole HUD
    down by a few dozen rows, which would hide every bar from its fixed spot, so
    each bar is searched over a short vertical range.
    """
    ready = []
    for index, offset in ready_offsets(rgb, drift):
        ready.append(index)
    return ready


def ready_offsets(rgb, drift=26):
    """(bar index, vertical offset) for every full cooldown bar.

    The offset is how far the bar sits from its nominal row. A window capture
    that kept the emulator's title bar pushes the whole HUD down by that much,
    and the tap has to follow, or skill taps land on empty ground.
    """
    hits = []
    half_width, half_height = READY_BAR
    for index, (x, y, _, _) in enumerate(TARGETS):
        if x + half_width >= rgb.shape[1]:
            continue
        best = (0.0, 0)
        for offset in range(0, drift + 1):
            for dy in ((offset,) if offset == 0 else (offset, -offset)):
                cy = y + dy
                if cy - half_height < 0 or cy + half_height >= rgb.shape[0]:
                    continue
                box = rgb[cy - half_height:cy + half_height + 1,
                          x - half_width:x + half_width + 1]
                if box.size == 0:
                    continue
                red, green, blue = box.astype(np.int16).transpose(2, 0, 1)
                cyan = (blue > 140) & (green > 88) & (blue - red > 38) & (green - red > 30)
                coverage = float(cyan.mean())
                if coverage > best[0]:
                    best = (coverage, dy)
                if coverage >= 0.85:
                    break
            if best[0] >= 0.85:
                break
        if best[0] >= 0.70:
            hits.append((index, best[1]))
    return hits


def ready_groups(rgb):
    """Ready cooldown bars grouped into the three hero rows.

    Two ready bars in one hero row is signed evidence from the game's own
    HUD: a lone cyan slime in the lane never lines up with the grid columns.
    """
    xs = (293, 373, 453)
    ys = (146, 241, 331)

    counts = [0, 0, 0]
    for index, _ in ready_offsets(rgb):
        x, y, _, _ = TARGETS[index]
        column = min(range(3), key=lambda candidate: abs(x - xs[candidate]))
        # A bar drawn between two columns (a moved HUD, a stray slime) is not
        # evidence of the 3 × 3 grid.
        if abs(x - xs[column]) > 12:
            continue
        row = min(range(3), key=lambda candidate: abs(y - ys[candidate]))
        counts[row] += 1
    return counts


CROSS_SIZE = 9
CLOSE_CORNERS = {
    "top-right": (1060, 1280, 0, 110),
    "top-left": (0, 220, 0, 110),
    "bottom-right": (1060, 1280, 610, 720),
    "bottom-left": (0, 220, 610, 720),
}
STILL_FRAME = 1.5
# Second-long wait before a guess based only on a corner X is allowed.
AD_MINIMUM = {"reward_granted": 0.0, "ad_end_card": 0.0, "ad_end_sheet": 0.0,
              "ad_unknown_close": 8.0}


def _integral(mask):
    return np.pad(mask.cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))


def _box_sums(mask, size):
    integral = _integral(mask)
    return (integral[size:, size:] - integral[:-size, size:]
            - integral[size:, :-size] + integral[:-size, :-size])


def _diagonal_ink(gray, polarity, size=CROSS_SIZE):
    """Score every pixel as the centre of an X of the given polarity."""
    ink = (gray > 150) if polarity > 0 else (gray < 105)
    ink = ink.astype(np.float32)
    height, width = ink.shape
    span = 2 * size + 1
    if height <= span or width <= span:
        return None
    # Sum each diagonal of every span-sized window by shifting the mask.
    summed = np.zeros((height - 2 * size, width - 2 * size), dtype=np.float32)
    for step in range(-size, size + 1):
        row = size + step
        column = size + step
        summed += ink[row:row + height - 2 * size, column:column + width - 2 * size]
        summed += ink[row:row + height - 2 * size, 2 * size - column:2 * size - column + width - 2 * size]
    diagonals = 2 * span
    coverage = summed / diagonals
    # A flat block is not a cross: a real X in this box is mostly background.
    area = _box_sums(ink, span) / (span * span)
    return coverage - 0.9 * area


def close_candidates(rgb):
    """Candidate ad close buttons in the screen corners, best first."""
    found = []
    for corner, (x0, x1, y0, y1) in CLOSE_CORNERS.items():
        region = rgb[y0:y1, x0:x1]
        gray = region.mean(axis=2).astype(np.float32) if region.ndim == 3 else region.astype(np.float32)
        for polarity in (1, -1):
            scores = _diagonal_ink(gray, polarity)
            if scores is None:
                continue
            for _ in range(4):
                y, x = np.unravel_index(scores.argmax(), scores.shape)
                score = float(scores[y, x])
                if score < 0.42:
                    break
                point = (int(x + x0 + CROSS_SIZE), int(y + y0 + CROSS_SIZE))
                if all(abs(point[0] - other[0]) > 26 or abs(point[1] - other[1]) > 26
                       for _, other in found):
                    found.append((score, point))
                scores[max(0, y - 30):y + 31, max(0, x - 30):x + 31] = -1
    return found


def chevron_candidates(rgb):
    """The >> / >| skip control that some ad layouts show before the X.

    AdMob's end cards often hide the X until the last page, and offer a double
    chevron to page forward. It is drawn, not a widget, so it is found by shape:
    two parallel strokes leaning the same way, in the top corners.
    """
    found = []
    for corner, (x0, x1, y0, y1) in CLOSE_CORNERS.items():
        region = rgb[y0:y1, x0:x1]
        gray = region.mean(axis=2).astype(np.float32) if region.ndim == 3 else region.astype(np.float32)
        for polarity in (1, -1):
            ink = gray * polarity
            # A chevron is a diagonal band; count how much of each column is lit
            # and look for the two humps a ">>" produces.
            columns = (ink > ink.mean() + 0.6 * ink.std()).sum(axis=0).astype(np.float32)
            if columns.size < 12 or columns.max() < 4:
                continue
            peaks = 0
            for i in range(2, columns.size - 2):
                if (columns[i] > 0.75 * columns.max()
                        and columns[i] >= columns[i - 1] and columns[i] >= columns[i + 1]):
                    peaks += 1
            if peaks < 2:
                continue
            weight = float(columns.std() / max(columns.mean(), 1e-6))
            if weight < 0.9:
                continue
            centre = (x0 + x1) // 2, (y0 + y1) // 2
            found.append((weight, centre))
    return sorted(found, key=lambda item: -item[0])


def countdown_band(rgb):
    """The "Reward in 11 seconds" strip, as (left, right, top, bottom).

    Every rewarded ad puts its remaining time in the top-right corner. Reading
    it is the only way to tell an ad that is still counting from one that has
    finished, because both show the same X. Returns None when the strip is
    empty, which is what an end card looks like.
    """
    height, width = rgb.shape[:2]
    x0, y0 = int(width * 0.70), int(height * 0.015)
    x1, y1 = int(width * 0.985), int(height * 0.125)
    strip = rgb[y0:y1, x0:x1].mean(axis=2)
    if strip.size == 0:
        return None
    ink = np.abs(strip - np.median(strip)) > 42
    columns = ink.sum(axis=0)
    runs, start = [], None
    for index, value in enumerate(columns):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if index - start >= 6:
                runs.append([start, index])
            start = None
    if start is not None and len(columns) - start >= 6:
        runs.append([start, len(columns)])
    merged = []
    for run in runs:
        if merged and run[0] - merged[-1][1] < 14:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    # The countdown is words; the X beside it is one narrow glyph.
    words = [run for run in merged if run[1] - run[0] >= 16]
    if not words:
        return None
    rows = ink[:, words[0][0]:words[-1][1]]
    found = np.nonzero(rows.any(axis=1))[0]
    return (words[0][0] + x0, words[-1][1] + x0, int(found.min()) + y0,
            int(found.max()) + y0)


# Buttons of the "Close Video? / You will lose your reward" warning. Tapping
# CLOSE VIDEO throws the reward away, so the tool must answer RESUME VIDEO and
# wait for the ad to finish. The two are told apart by fill, not by wording,
# so the language of the ad does not matter.
AD_RESUME, AD_CONFIRM_CLOSE = "resume", "confirm-close"


def ad_warning_dialog(rgb):
    """(kind, point) when the ad asks whether to really close, else None.

    The panel is a large pale card over the middle of the screen, carrying one
    solid coloured button and one text-only button. The solid one continues the
    ad: it is the safe answer, and the only one that keeps the reward.
    """
    height, width = rgb.shape[:2]
    centre = rgb[int(height * 0.20):int(height * 0.78), int(width * 0.20):int(width * 0.72)]
    if centre.size == 0:
        return None
    pale = float((centre.min(axis=2) > 225).mean())
    if pale < 0.45:
        return None
    saturated = ((centre.max(axis=2) - centre.min(axis=2)) > 55) & (centre.min(axis=2) < 210)
    rows = saturated.sum(axis=1)
    if not rows.any():
        return None
    solid_rows = np.nonzero(rows > 0.35 * rows.max())[0]
    if solid_rows.size < 12:
        return None
    band = saturated[solid_rows.min():solid_rows.max() + 1]
    columns = band.sum(axis=0)
    solid_cols = np.nonzero(columns > 0.55 * band.shape[0])[0]
    if solid_cols.size < 24:
        return None
    point = (int(solid_cols.mean()) + int(width * 0.20),
             int(solid_rows.mean()) + int(height * 0.20))
    return AD_RESUME, point


def in_payout_zone(point):
    """True for the bottom strip an Install bar occupies.

    Ads put a wide dark "Install" bar there, and a stray tap on it leaves the
    ad for a store page. The whole strip is refused rather than only the bar
    itself, because the bar's edges move between ads.
    """
    return point[1] >= 600


def payout_pill(rgb):
    """The big dark "Install" bar some ads put over the bottom of the screen.

    It is never a close control: tapping it leaves the ad and opens a store
    page, which is what the tool must not do. Only a wide, near-black, mostly
    flat band counts.
    """
    height, width = rgb.shape[:2]
    band = rgb[int(height * 0.88):height, int(width * 0.30):]
    if band.size == 0:
        return False
    dark = band.max(axis=2) < 80
    return bool(dark.mean() > 0.55 and dark.sum() > 0.55 * band.shape[0] * band.shape[1])


class Vision:
    def __init__(self, upgrade_tower=False):
        with np.load(Path(__file__).with_name("play_templates.npz"), allow_pickle=False) as atlas:
            self.templates = {name: atlas[name].astype(np.int16) for name in TEMPLATES}
        self.zero = pale_pixels(self.templates["hp_zero"], 145)
        # The progress bar behind the WAVE label changes from white to black.
        self.wave_text = np.mean(np.abs(self.templates["wave"] - self.templates["wave_late"]),
                                 axis=2) < 14
        self.reward = Image.fromarray(self.templates["reward"].astype(np.uint8)).convert("L")
        light_reward = Image.fromarray(self.templates["reward_light"].astype(np.uint8)).convert("L")
        self.reward_light = ImageOps.invert(light_reward)
        self.reward_mixed = Image.fromarray(self.templates["reward_mixed"].astype(np.uint8)).convert("L")
        self.tower = None
        if upgrade_tower:
            from tower_vision import TowerVision

            self.tower = TowerVision()

    def matches(self, rgb, name, offset_y=0):
        box, tolerance = TEMPLATES[name]
        if offset_y:
            left, top, right, bottom = box
            box = (left, top + offset_y, right, bottom + offset_y)
        template = self.templates[name]
        difference = np.abs(region(rgb, box) - template)
        if name != "wave":
            return np.mean(difference) < tolerance
        if np.mean(difference[self.wave_text]) < tolerance:
            return True
        # The top HUD text in native ADB captures is one pixel below the
        # recorded MP4 samples and has sharper edges.
        left, top, right, bottom = box
        shifted = region(rgb, (left, top + 1, right, bottom + 1))
        return np.mean(np.abs(shifted - template)[self.wave_text]) < 20

    def hp_empty(self, rgb, dim):
        threshold = 53 if dim else 145
        left, top, right, bottom = TEMPLATES["hp_zero"][0]
        if not any(np.count_nonzero(pale_pixels(rgb[top + dy:bottom + dy, left:right],
                                                threshold) ^ self.zero) <= 42
                   for dy in (0, 1)):
            return False
        bar = rgb[8:28, 657:917]
        red, green, blue = bar.transpose(2, 0, 1)
        health = (red > (65 if dim else 130)) & (red - blue > (35 if dim else 80))
        return health.sum() < 6 and pale_pixels(bar, threshold).sum() < 8

    def reward_close(self, image):
        # Search only the player chrome for the full label and the adjoining X.
        left, top = 902, 8
        chrome = image.crop((left, top, 1280, 106)).convert("L")
        point = self.match_reward(chrome, self.reward, 168, (182, 14))
        if point is None:
            point = self.match_reward(ImageOps.invert(chrome), self.reward_light, 143, (156, 12))
        if point is None:
            point = self.match_reward(chrome, self.reward_mixed, 163, (183, 16), dark_x=True)
        if point is not None:
            return left + point[0], top + point[1]
        return None

    @staticmethod
    def match_reward(chrome, template, close_start, close_center, dark_x=False):
        field = np.asarray(chrome) > 165
        fft_size = (128, 640)
        field_fft = np.fft.rfft2(field.astype(np.float32), s=fft_size, axes=(0, 1))
        integral = np.pad(field.cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))
        for scale in (1.0, 0.85, 1.15):
            size = (round(template.width * scale), round(template.height * scale))
            mask = np.asarray(template.resize(size, Image.Resampling.BILINEAR)) > 165
            height, width = mask.shape
            kernel = np.fft.rfft2(mask.astype(np.float32), s=fft_size, axes=(0, 1))
            overlap = np.fft.irfft2(field_fft * kernel.conj(), s=fft_size, axes=(0, 1))
            count = (integral[height:, width:] - integral[:-height, width:]
                     - integral[height:, :-width] + integral[:-height, :-width])
            score = 2 * overlap[:count.shape[0], :count.shape[1]] / (count + mask.sum())
            y, x = np.unravel_index(score.argmax(), score.shape)
            if score[y, x] < 0.83:
                continue
            x_start = round(close_start * scale)
            close = field[y:y + height, x + x_start:x + width]
            expected = mask[:, x_start:]
            if 2 * (close & expected).sum() < 0.76 * (close.sum() + expected.sum()):
                continue
            cx, cy = (round(coordinate * scale) for coordinate in close_center)
            if dark_x:
                # The bright circle alone is a match. Check the dark X inside it.
                radius = round(8 * scale)
                glyph = ~field[y + cy-radius:y + cy+radius+1, x + cx-radius:x + cx+radius+1]
                expected_x = ~mask[cy-radius:cy+radius+1, cx-radius:cx+radius+1]
                if 2 * (glyph & expected_x).sum() < 0.76 * (glyph.sum() + expected_x.sum()):
                    continue
            return int(x + cx), int(y + cy)
        return None

    def ad_end_close(self, rgb):
        # The two-card end screen replaces the playback progress and ad count
        # with an empty header and a standalone close button.
        if not self.matches(rgb, "ad_end_close"):
            return None
        header = rgb[:69, :1228]
        if np.max(np.mean(np.abs(header - header[0, 0]), axis=(1, 2))) > 8:
            return None
        template = self.templates["ad_end_cta"]
        height, width, _ = template.shape
        tolerance = TEMPLATES["ad_end_cta"][1]
        for left in (247, 888):
            if not any(np.mean(np.abs(rgb[top:top + height, left:left + width] - template))
                       < tolerance for top in range(578, 680)):
                return None
        return 1257, 42

    def ad_chrome(self, rgb):
        """True when the frame looks like an ad that has finished playing."""
        header = rgb[:36]
        return bool(np.mean(np.ptp(header, axis=2)) < 40) or bool(self.matches(rgb, "skip_title"))

    def home_screen(self, rgb):
        gold = region(rgb, GOLD_HUD)
        gems, layout = self.tower.home(rgb) if self.tower else (None, None)
        # The offer slides a few pixels as it appears after the results screen.
        for offset in (0, *range(-6, 0), *range(1, 7)):
            if self.matches(rgb, "gold_coin", offset) and (
                    self.matches(rgb, "gold_get", offset)
                    or self.matches(rgb, "gold_get_centered", offset)):
                return Screen("gold", (626, 649 + offset), gold=gold, gems=gems, layout=layout)
        return Screen("home", gold=gold, gems=gems, layout=layout)

    def captcha_start_visible(self, image):
        frame = viewport(image)
        if frame is None:
            return False
        rgb = np.asarray(frame, dtype=np.int16)
        return self.matches(rgb, "captcha") and self.matches(rgb, "captcha_start")

    def classify(self, image, watching_ad=False, ad_foreground=False):
        if image is None:
            return Screen("ad_playing" if ad_foreground else "unknown")
        rgb = np.asarray(image, dtype=np.int16)
        if watching_ad or ad_foreground:
            # Order matters. A warning dialog and a running countdown both sit
            # above the ad, and neither may be treated as a finished end card:
            # tapping their X abandons the reward and opens the store page.
            if dialog := ad_warning_dialog(rgb):
                return Screen("ad_warning", dialog[1], sub=dialog[0])
            if countdown_band(rgb) is not None:
                return Screen("ad_counting")
            extra = tuple(point for _, point in close_candidates(rgb))
            if point := self.reward_close(image):
                return Screen("reward_granted", point, points=extra)
            if point := self.ad_end_close(rgb):
                return Screen("ad_end_card", point, points=extra)
            if all(self.matches(rgb, name)
                   for name in ("ad_sheet_close", "ad_sheet_chevron", "ad_sheet_cta")):
                return Screen("ad_end_sheet", (1257, 39), points=extra)
            chevrons = tuple(point for _, point in chevron_candidates(rgb))
            if chevrons and not payout_pill(rgb):
                # A >> pages an end card forward; the X appears on the last one.
                # It is only trusted when no Install bar is over the screen: a
                # busy ad picture produces chevron shapes all the time, and the
                # bottom-left one lands on artwork that opens the store.
                return Screen("ad_end_card", chevrons[0],
                              points=chevrons[1:] + extra)
            if self.ad_chrome(rgb) and extra:
                # A close button at an unexpected spot with an unfamiliar
                # background still leaves a finished ad that can be dismissed.
                return Screen("ad_unknown_close", extra[0], points=extra[1:])
        if ad_foreground:
            return Screen("ad_playing")
        if self.matches(rgb, "captcha"):
            return Screen("captcha")
        if self.matches(rgb, "processing"):
            return Screen("processing")
        if self.matches(rgb, "victory"):
            return Screen("victory")
        if self.tower is not None and (panel := self.tower.panel(rgb)):
            kind, gems, level, cost = panel
            return Screen(kind, gems=gems, level=level, cost=cost)
        hud = self.matches(rgb, "hud")
        dim = not hud and self.matches(rgb, "hud_dim")
        if (hud or dim) and self.hp_empty(rgb, dim):
            return Screen("defeat")
        if hud and self.matches(rgb, "skip_title") and self.matches(rgb, "skip_close"):
            return Screen("skip", (888, 216))
        if hud and self.matches(rgb, "upgrade"):
            if self.matches(rgb, "replay_100") and self.matches(rgb, "replay_80"):
                return Screen("replay_menu", (857, 623))
            if self.matches(rgb, "battle") and self.matches(rgb, "replay"):
                return self.home_screen(rgb)
        if self.tower is not None and self.tower.is_home(rgb):
            return self.home_screen(rgb)
        # The WAVE pill is replaced by a BOSS bar during boss waves, so the
        # battle screen can no longer be recognised from that template alone.
        # A scaled window can also miss the tiny HUD template, so the HP/MP
        # bars and cooldown pills are checked even without it.
        if self.matches(rgb, "upgrade"):
            return Screen("unknown")
        health = health_row(rgb)
        if health is not None:
            pill = status_pill(rgb, health)
            offsets = dict(ready_offsets(rgb))
            ready = tuple(index for index in offsets)
            # A boss wave shows the BOSS bar instead of the WAVE pill, and a
            # wave whose progress turned dark no longer matches the template.
            # On a battle screen every bar left is a cooldown, so a few ready
            # bars are enough evidence when both pills are hard to read.
            if ((pill is not None and pill[0] == "boss") or self.matches(rgb, "wave")
                    or self.matches(rgb, "wave_late") or wave_pill(rgb, health)
                    or max(ready_groups(rgb)) >= 3):
                return Screen("battle", ready=ready,
                              shifts=tuple(offsets.get(index, 0) for index in ready))
        return Screen("unknown")


class Player:
    def __init__(self, mode, report, tower=None, auto_captcha=False, *, watch_ads=True):
        self.mode = mode
        self.report = report
        self.tower = tower
        self.auto_captcha = auto_captcha
        self.watch_ads = watch_ads
        self.screen = "unknown"
        self.since = 0
        self.acted = False
        self.wait_notified = False
        self.paused = False
        self.ad = None
        self.ad_since = 0
        self.ad_warned = False
        self.reward_label_seen = False
        self.ad_close_attempts = {}
        self.last_device_close = float("-inf")
        self.ad_taps = 0
        self.ad_tried_points = []
        self.ad_seen = set()
        # An ad showing "Reward in N seconds" must be left alone: its X
        # abandons the reward, so nothing is tapped until the counter is gone.
        self.waiting_out_countdown = False
        self.poked_warning = False
        self.skip_offer = False
        self.gold_before = None
        self.after_victory = False
        self.skills_used = {}
        self.last_skill = float("-inf")
        self.last_ad_tap = float("-inf")

    def ad_minimum(self):
        """Seconds before corner-only guesses are allowed to tap."""
        minimum = 0.0
        for name in self.ad_seen:
            minimum = max(minimum, AD_MINIMUM.get(name, 0.0))
        return minimum

    def start_ad(self, time, gold=None):
        self.ad, self.ad_since, self.ad_warned = "watch", time, False
        self.gold_before = gold
        self.reward_label_seen = False
        self.ad_close_attempts.clear()
        self.ad_taps = 0
        self.ad_tried_points.clear()
        self.ad_seen.clear()
        self.poked_warning = False
        self.waiting_out_countdown = False

    def feed_ad(self, time, screen, age):
        name = screen.name
        if name in AD_CLOSE_SCREENS:
            self.ad_seen.add(name)
        if name == "ad_warning":
            # The ad asked whether to really leave. Answer the button that
            # keeps the reward, and treat the ad as still running afterwards.
            if screen.sub == AD_RESUME and time - self.last_ad_tap >= 1.4:
                self.last_ad_tap = time
                self.ad = "playing"
                self.poked_warning = False
                # Resuming puts the ad back on its counter, which its own X
                # would abandon. Nothing else may be tapped until it ends.
                self.waiting_out_countdown = True
                return Action("keep watching ad", screen.point)
            if time - self.last_ad_tap >= 1.4 and not self.poked_warning:
                # The panel was already up when the tool arrived, so the ad
                # itself was probably dismissed by a tap it made earlier.
                self.poked_warning = True
                self.last_ad_tap = time
                return Action("keep watching ad", screen.point)
            return None
        if name == "ad_counting":
            # Time left on the counter: there is a finished ad underneath the
            # X but tapping it now throws the reward away. Wait it out.
            if self.ad == "watch":
                self.ad = "playing"
            self.waiting_out_countdown = True
            return None
        if name == "ad_playing":
            if self.ad == "watch":
                self.ad = "playing"
            if age >= 1.5:
                self.ad_close_attempts.clear()
        elif name in AD_CLOSE_SCREENS:
            self.waiting_out_countdown = False
            self.ad = "return"
            self.reward_label_seen |= name == "reward_granted"
            options = [screen.point]
            if time - self.ad_since >= self.ad_minimum():
                # Guesswork only once the ad has had time to reach its end
                # card, otherwise a logo could be tapped mid-video.
                options += list(screen.points)
            options = [point for point in options if point]
            if options:
                # Retry on a different spot: the found button may be a logo.
                offset = self.ad_taps % len(options)
                point = options[offset]
                if point not in self.ad_tried_points:
                    self.ad_tried_points.append(point)
            else:
                point = None
            attempts, last_tap = self.ad_close_attempts.get(name, (0, float("-inf")))
            if point and age >= 0.20 and attempts < 6 and self.ad_taps < 15 \
                    and time - last_tap >= 1.4 and time - self.last_ad_tap >= 1.4:
                self.ad_close_attempts[name] = (attempts + 1, time)
                self.ad_taps += 1
                self.last_ad_tap = time
                label = "close granted ad" if name == "reward_granted" else "close ad end card"
                return Action(label, point)
        elif name in RETURN_SCREENS and age >= 0.55:
            if self.ad == "return":
                changed = (screen.gold is not None and self.gold_before is not None
                           and np.mean(np.abs(screen.gold - self.gold_before)) > 6)
                message = "gold HUD changed" if changed else "gold HUD change unconfirmed"
                result = "Reward granted" if self.reward_label_seen else "Ad end card closed"
                self.report(time, f"{result}, returned to game ({message})")
                self.ad = None
            elif self.ad == "playing" or time - self.ad_since >= 12:
                self.report(time, "Game returned without a recognized reward")
                self.ad = None
                self.acted = False
        if self.ad and not self.ad_warned and time - self.ad_since >= 90:
            self.report(time, "Ad still open: waiting for recognized reward or manual close")
            self.ad_warned = True
        return None

    def device_close(self, adb, serial, time):
        """Close an ad through the device, in three layers of certainty.

        A picture can only show a close button that is drawn the way the
        templates expect. These three reach the ones it cannot: a real widget at
        any position and in any language, a drawn chevron that pages the ad
        forward, and BACK for an end card that shows no control at all.
        """
        if self.waiting_out_countdown or time - self.last_device_close < 2.5:
            return None
        self.last_device_close = time
        point = ad_close.close_from_widgets(adb, serial)
        if point is not None:
            return Action("close ad by widget", point)
        # Nothing to tap: either the ad has not offered its X yet, or the SDK is
        # showing a Play Store sheet on top. BACK is safe here because an ad
        # that is still counting down swallows the key itself.
        if time - self.ad_since >= self.ad_minimum() + 2:
            ad_close.send_back(adb, serial)
            return Action("close ad with BACK", None)
        return None

    def close_stalled_ad(self, time, screen, still_for):
        """Keep trying visible X buttons when a finished ad refuses to close."""
        if self.ad == "device":
            return None
        if self.waiting_out_countdown or screen.name in {"ad_counting", "ad_warning"}:
            # The counter is still running. Its X is real but tapping it asks
            # whether to abandon the reward, and the ad underneath often has an
            # Install bar in a corner: guessing there opens a store page.
            return None
        if not self.ad or still_for < STILL_FRAME:
            return None
        if time - self.ad_since < self.ad_minimum():
            return None
        options = [screen.point, *screen.points]
        options = [point for point in options if point and not in_payout_zone(point)]
        if not options or time - self.last_ad_tap < 1.4:
            return None
        for point in options:
            if point not in self.ad_tried_points[-2:]:
                self.ad_tried_points.append(point)
                self.last_ad_tap = time
                return Action("close stalled ad", point)
        if len(options) > 1 and time - self.last_ad_tap >= 6:
            # Every visible spot was tried; cycle once more slowly in case the
            # ad only becomes dismissible after its timer ends.
            self.ad_tried_points.clear()
            self.last_ad_tap = time
            return Action("retry ad close", options[0])
        return None

    def feed(self, time, screen):
        if screen.name == "gold" and not self.watch_ads:
            # The inline offer leaves the home controls and tower slots available.
            screen = replace(screen, name="home")
        name = screen.name
        previous = self.screen
        if name != previous:
            self.screen, self.since = name, time
            self.acted = False
            self.wait_notified = False
        age = time - self.since
        if self.ad is None and (name == "ad_playing" or name in AD_CLOSE_SCREENS):
            self.start_ad(time)
            self.skip_offer = True
        if self.ad:
            if self.tower is not None:
                self.tower.suspend()
            action = self.feed_ad(time, screen, age)
            if self.ad:
                return action
        if name == "captcha":
            if self.tower is not None:
                self.tower.suspend()
            if not self.paused:
                message = "automatic attempt" if self.auto_captcha else "manual solution"
                self.report(time, f"CAPTCHA: paused for {message}")
                self.paused = True
            return None
        if self.paused:
            if name not in RETURN_SCREENS or age < 0.45:
                return None
            self.report(time, "Game screen restored: resumed")
            self.paused = False
        if name == "defeat" and self.mode == "wave":
            self.mode = "replay"
            self.report(time, "Castle HP reached zero: switched to Replay")
        if name == "victory":
            self.skills_used.clear()
            self.after_victory = self.watch_ads
        elif name == "battle":
            self.after_victory = False
        if name in {"home", "battle"} and age >= 0.45:
            self.skip_offer = False
        if name == "home" and self.after_victory:
            if age < 1.6:
                return None
            self.after_victory = False
        if name == "gold" and not self.skip_offer:
            if age < 0.20:
                return None
            self.after_victory = False
            self.start_ad(time, screen.gold)
            self.skip_offer = True
            self.acted = True
            return Action("open gold ad (GET)", screen.point)
        if self.tower is not None and name in {"home", "tower", "castles", "unknown"}:
            try:
                handled, action = self.tower.feed(time, screen)
            except RuntimeError as exc:
                self.report(time, f"{exc} Automatic upgrades disabled for this run.")
                self.tower = None
            else:
                if handled:
                    return action
        elif self.tower is not None:
            self.tower.suspend()
        if name == "unknown" and age >= 3 and not self.wait_notified:
            self.report(time, "Unknown screen: waiting for the main screen or a battle")
            self.wait_notified = True
        if name == "battle":
            return self.cast(time, screen.ready, screen.shifts) if age >= 0.45 else None
        if self.acted or age < 0.20:
            return None
        if name == "skip":
            action = Action("close Wave Skip", screen.point)
        elif name == "replay_menu" and self.mode == "replay":
            action = Action("start Replay 100%", screen.point)
        elif name in {"home", "gold"} and age >= 0.85:
            self.skills_used.clear()
            if self.mode == "wave":
                action = Action("start next Battle", (1191, 648))
            else:
                action = Action("open Replay menu", (1017, 648))
        else:
            return None
        self.acted = True
        return action

    def cast(self, time, ready, shifts=()):
        for index in tuple(self.skills_used):
            self.skills_used[index] = 0 if index in ready else self.skills_used[index] + 1
            if self.skills_used[index] >= 2:
                del self.skills_used[index]
        if time - self.last_skill < 0.30:
            return None
        shift_of = dict(zip(ready, shifts)) if shifts else {}
        for index in ready:
            if index not in self.skills_used:
                self.skills_used[index] = 0
                self.last_skill = time
                _, _, x, y = TARGETS[index]
                return Action(f"cast skill {index + 1}", (x, y + shift_of.get(index, 0)))
        return None


def adb_frames(adb, serial, seconds):
    start = monotonic()
    while not seconds or monotonic() - start < seconds:
        sample = monotonic()
        raw = run(adb, "-s", serial, "exec-out", "screencap", "-p")
        with Image.open(BytesIO(raw)) as screen:
            yield sample - start, screen.copy()
        sleep(max(0, 0.22 - (monotonic() - sample)))


def replay_frames(video):
    next_sample = 0
    for _, time, image in read_video(video):
        if time >= next_sample - 0.002:
            yield time, image
            next_sample += 0.20


def foreground_activity(adb, serial):
    focus = run(adb, "-s", serial, "shell", "dumpsys window | grep mCurrentFocus || true")
    match = re.search(r"mCurrentFocus=.*\bcom\.raongames\.growcastle/([^\s}]+)",
                      focus.decode(errors="replace"))
    return match[1] if match else None


def play(args, report, journal):
    tower = TowerUpgrade(report, journal) if args.upgrade_tower else None
    player = Player(args.mode, report, tower, args.auto_captcha, watch_ads=args.ads)
    vision = Vision(args.upgrade_tower)
    adb = args.ldconsole.with_name("adb.exe")
    frames = replay_frames(args.video) if args.video else adb_frames(adb, args.serial, args.seconds)
    prefix = "TAP" if args.tap else "PREVIEW"
    print(f"{prefix} mode={args.mode} source={args.video or args.serial}", flush=True)
    previous = None
    still_since = 0.0
    for time, original in frames:
        current = viewport(original)
        if current is not None:
            current = np.asarray(current, dtype=np.int16)
        if previous is None or current is None:
            previous = current
        else:
            if float(np.mean(np.abs(current - previous))) > 3:
                still_since = time
            previous = current
        still_for = time - still_since
        if args.video:
            screen = vision.classify(viewport(original), watching_ad=player.ad is not None)
        else:
            activity = foreground_activity(adb, args.serial)
            # Any ad SDK's window owns the frame, not only AdMob's AdActivity:
            # UnityAds, AppLovin, IronSource and Mintegral each use their own.
            ad_window = ad_close.ad_in_foreground(adb, args.serial)
            # An ad can show footage of a game. Its Android window owns the
            # frame until it closes, even if the image resembles a game screen.
            screen = (vision.classify(viewport(original), watching_ad=player.ad is not None,
                                      ad_foreground=ad_window)
                      if (activity or ad_window) else Screen("unfocused"))
        was_paused = player.paused
        action = player.feed(time, screen)
        if player.ad and not action:
            # A playing ad moves; a stalled one is a still frame still showing X.
            action = player.close_stalled_ad(time, screen, still_for)
            if not action and not args.video and still_for >= STILL_FRAME:
                # The picture could not find a close button. Ask the device
                # instead: a real widget, a drawn chevron, or BACK.
                action = player.device_close(adb, args.serial, time)
        if args.auto_captcha and player.paused and not was_paused:
            attempt_started = monotonic()
            limit = min(CAPTCHA_WINDOW, args.seconds - time) if args.seconds else CAPTCHA_WINDOW
            selected = False
            for attempt in range(1, CAPTCHA_ATTEMPTS + 1):
                try:
                    if limit <= 0:
                        raise RuntimeError("out of time")
                    selected = watch(args.ldconsole, args.index, args.serial, limit, True,
                                     start_screen=vision.captcha_start_visible)
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                    report(time + monotonic() - attempt_started, f"CAPTCHA attempt stopped: {exc}")
                if selected:
                    report(time + monotonic() - attempt_started,
                           "CAPTCHA: selection sent; waiting for the game to resume")
                    break
                if attempt < CAPTCHA_ATTEMPTS:
                    # The panel stays up after a miss; a fresh look may read the
                    # settled layout the previous attempt could not finish.
                    report(time + monotonic() - attempt_started,
                           f"CAPTCHA: attempt {attempt} did not settle; trying again")
                    sleep(CAPTCHA_PAUSE)
                    limit = (min(CAPTCHA_WINDOW, args.seconds - time)
                             if args.seconds else CAPTCHA_WINDOW)
            else:
                report(time + monotonic() - attempt_started, "CAPTCHA: waiting for manual solution")
        if action:
            if args.tap and action.point is None:
                # Delivered by the device already (BACK), nothing to tap.
                pass
            elif args.tap:
                if action.charge is not None:
                    raw = run(adb, "-s", args.serial, "exec-out", "screencap", "-p")
                    with Image.open(BytesIO(raw)) as check:
                        latest = vision.classify(viewport(check))
                        if (check.size != original.size or latest.name != "tower"
                                or (latest.gems, latest.level, latest.cost) != action.charge
                                or foreground_activity(adb, args.serial) != activity):
                            tower.suspend()
                            continue
                    tower.record_tap(action)
                x = round(action.point[0] * original.width / VIEWPORT[0])
                y = round(action.point[1] * original.height / VIEWPORT[1])
                run(adb, "-s", args.serial, "shell", "input", "tap", str(x), str(y))
            elif action.charge is not None:
                tower.record_tap(action)
            report(time, f"{prefix} {action.name} {action.point}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, help="preview decisions on a recorded MP4")
    parser.add_argument("--mode", choices=("wave", "replay"), default="wave")
    parser.add_argument("--ldconsole", type=Path, default=Path(r"D:\LDPlayer\LDPlayer9\ldconsole.exe"))
    parser.add_argument("--serial", help="ADB serial (defaults to emulator-5554 for index 0)")
    parser.add_argument("--index", type=int, default=0, help="LDPlayer instance index for CAPTCHA capture")
    parser.add_argument("--seconds", type=float, default=0, help="live capture limit, 0 waits indefinitely")
    parser.add_argument("--tap", action="store_true", help="send the recognized actions to LDPlayer")
    parser.add_argument("--ads", action=argparse.BooleanOptionalAction, default=True,
                        help="open gold reward ads automatically (default: enabled)")
    parser.add_argument("--auto-captcha", action="store_true",
                        help="automatically start and try one selection per CAPTCHA (requires --tap)")
    parser.add_argument("--upgrade-tower", action="store_true",
                        help="spend gems on a batch of 30 Lightning Castle upgrades at 90+ gems")
    args = parser.parse_args()
    if args.video and args.tap:
        parser.error("--video is preview only")
    if args.auto_captcha and not args.tap:
        parser.error("--auto-captcha requires --tap")
    if args.seconds < 0 or args.index < 0:
        parser.error("--seconds and --index must be nonnegative")
    args.serial = args.serial or f"emulator-{5554 + 2 * args.index}"

    def report(time, message):
        print(f"{time:6.2f}s  {message}", flush=True)

    journal = session_lock = None
    if args.tap:
        device_id = sha256(args.serial.encode()).hexdigest()[:16]
        session_lock = Path(__file__).with_name(f".autoplay-{device_id}.lock")
        if args.upgrade_tower:
            journal = Path(__file__).with_name(f".tower-upgrade-{device_id}.json")
    try:
        with lock_journal(session_lock, f"Autoplay is already running with --tap on {args.serial}."), lock_journal(journal):
            play(args, report, journal)
    except KeyboardInterrupt:
        pass
    except subprocess.CalledProcessError as exc:
        parser.exit(1, f"{exc.stderr.decode(errors='replace').strip() or exc}\n")
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()

"""Detect and follow the fast-spinning cup in Grow Castle's diamond challenge."""

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from windows_video import read_video


# Cup layout relative to the game image, normalized from a 1280 × 720 viewport.
VIEWPORT = (1280, 720)
BOARD = (420, 115, 850, 535)
BOARD_SIZE = (215, 210)
SLOTS = np.array([(110, 62), (155, 77), (170, 122), (155, 167),
                  (110, 182), (65, 167), (50, 122), (65, 77)])
SLOT_NAMES = ("top", "top-right", "right", "bottom-right",
              "bottom", "bottom-left", "left", "top-left")
CENTER = np.array([110, 122])
ANGLES = np.arange(0, 360, 15)
FFT_SIZE = (256, 256)
RADIUS = 27
BOARD_Y, BOARD_X = np.indices(BOARD_SIZE[::-1])
GEM_AREA = ((BOARD_X - CENTER[0]) ** 2 + (BOARD_Y - CENTER[1]) ** 2) < 73 ** 2
# Ring radius of every slot, used to validate a tracked cup position.
SLOT_RADIUS = np.linalg.norm(SLOTS - CENTER, axis=1)
SLOT_ANGLE = np.degrees(np.arctan2(SLOTS[:, 1] - CENTER[1], SLOTS[:, 0] - CENTER[0])) % 360


def board_image(image):
    width, height = image.size
    if abs(width / height / (16 / 9) - 1) > 0.03:
        raise ValueError("Expected the full 16:9 game viewport.")
    left, top, right, bottom = BOARD
    box = (left * width / VIEWPORT[0], top * height / VIEWPORT[1],
           right * width / VIEWPORT[0], bottom * height / VIEWPORT[1])
    return image.resize(BOARD_SIZE, Image.Resampling.BILINEAR, box=box)


def gray_of(image):
    """Single-channel view of a board crop, accepting RGB or L input."""
    if image.mode != "L":
        image = image.convert("L")
    return np.asarray(image, dtype=np.float32)


def gem_position(image):
    colors = np.asarray(image, dtype=np.int16)
    red, green, blue = colors[:, :, :3].transpose(2, 0, 1)
    y, x = np.nonzero(GEM_AREA & (blue > 129) & (blue > red + 35) & (green > red + 23))
    if len(x) < 12 or len(x) > 310 or np.ptp(x) > 28 or np.ptp(y) > 26:
        return None
    return np.array([np.mean(x), np.mean(y)])


def reveal_position(image):
    """A diamond shown under a cup anywhere on the ring.

    After a wrong tap the game lifts the chosen cup and shows where the diamond
    really was. That reveal is the only certain answer the panel ever gives, so
    it is read anywhere on the board instead of only over the middle.
    """
    colors = np.asarray(image, dtype=np.int16)
    red, green, blue = colors[:, :, :3].transpose(2, 0, 1)
    y, x = np.nonzero((blue > 129) & (blue > red + 35) & (green > red + 23))
    if len(x) < 12 or len(x) > 320 or np.ptp(x) > 30 or np.ptp(y) > 28:
        return None
    point = np.array([np.mean(x), np.mean(y)])
    gap, distance, slot = ring_distance(point)
    # Only trust a diamond sitting out on the cup ring, not one in the middle.
    if distance > 26 or gap > 24:
        return None
    return slot


def reveal_slot(boards):
    """Reward cup revealed after a miss, if the recording shows one."""
    seen = []
    for board in boards:
        slot = reveal_position(board)
        if slot is not None:
            seen.append(slot)
    if len(seen) < 2:
        return None
    counts = {}
    for slot in seen:
        counts[slot] = counts.get(slot, 0) + 1
    slot, count = max(counts.items(), key=lambda item: item[1])
    return slot if count >= max(2, len(seen) // 2) else None


def patch(image, center, radius):
    x, y = int(round(center[0])), int(round(center[1]))
    return image[y - radius:y + radius + 1, x - radius:x + radius + 1]


def upright_layout(gray):
    # A ring of nearly identical upright sprites around a flat background.
    if gray.ndim == 3:
        gray = gray.mean(axis=2)
    reference = patch(gray, SLOTS[0], 14)
    background = float(np.median(gray[100:145, 80:140]))
    if reference.size == 0 or reference.mean() < background + 30 or reference.std() < 9:
        return False
    return all(np.mean(np.abs(patch(gray, slot, 14) - reference)) < 12 for slot in SLOTS[1:])


def angle_distance(a, b):
    return abs((a - b + 180) % 360 - 180)


@dataclass(frozen=True)
class Pose:
    x: int
    y: int
    angle: int
    score: float

    def distance(self, point):
        return float(np.hypot(self.x - point[0], self.y - point[1]))

    @property
    def position(self):
        return self.x, self.y


@dataclass(frozen=True)
class Observation:
    time: float
    poses: list[Pose]
    common_angle: int | None

    def outliers(self, threshold):
        if self.common_angle is None:
            return []
        return [pose for pose in self.poses
                if pose.score >= threshold and angle_distance(pose.angle, self.common_angle) >= 30]


class PoseMatcher:
    def __init__(self, image):
        x, y = SLOTS[0]
        sprite = image.crop((x - RADIUS, y - RADIUS, x + RADIUS + 1, y + RADIUS + 1))
        array = np.asarray(sprite)
        self.background = int(np.median(array[:, :, :3] if array.ndim == 3 else array))
        yy, xx = np.mgrid[-RADIUS:RADIUS + 1, -RADIUS:RADIUS + 1]
        distance_sq = (xx * xx + yy * yy) / 26 ** 2
        weights = ((distance_sq <= 1) / (1 + 0.5 * distance_sq)).astype(np.float32)
        kernels, energies = [], []
        for angle in ANGLES:
            rotated = sprite.rotate(int(angle), resample=Image.Resampling.BICUBIC,
                                    fillcolor=self.background)
            rotated = rotated.convert("L") if rotated.mode != "L" else rotated
            template = np.asarray(rotated, dtype=np.float32) - self.background
            kernels.append(self._centered_fft(template * weights))
            energies.append(np.sum(template * template * weights))
        self.kernels = np.stack(kernels)
        self.energies = np.array(energies)[:, None, None]
        self.window = self._centered_fft(weights)

    @staticmethod
    def _centered_fft(template):
        padded = np.zeros(FFT_SIZE, dtype=np.float32)
        size = 2 * RADIUS + 1
        padded[:size, :size] = template
        centered = np.roll(padded, (-RADIUS, -RADIUS), axis=(0, 1))
        return np.fft.rfft2(centered).conj()

    def observe(self, time, gray):
        if gray.ndim == 3:
            gray = gray.mean(axis=2)
        height, width = gray.shape
        signal = gray - self.background
        correlation = np.fft.irfft2(np.fft.rfft2(signal, s=FFT_SIZE) * self.kernels,
                                    s=FFT_SIZE, axes=(-2, -1))
        energy = np.fft.irfft2(np.fft.rfft2(signal * signal, s=FFT_SIZE) * self.window,
                               s=FFT_SIZE, axes=(-2, -1))
        # Normalized template score, penalizing both missed and extra pixels.
        scores = 2 * correlation / (energy + self.energies + 1)
        scores = scores[:, :height, :width]
        best_angle = scores.argmax(axis=0)
        best_score = scores.max(axis=0)
        best_score[:20] = 0
        best_score[:, :20] = 0
        best_score[:, -20:] = 0
        poses = []
        for _ in SLOTS:
            y, x = np.unravel_index(best_score.argmax(), best_score.shape)
            score = float(best_score[y, x])
            if score < 0.67:
                break
            angle = int(ANGLES[best_angle[y, x]])
            poses.append(Pose(int(x), int(y), angle, score))
            # Each cup can produce several nearby peaks as the angle changes.
            best_score[(BOARD_X - x) ** 2 + (BOARD_Y - y) ** 2 <= 17 ** 2] = 0
        common = None
        if len(poses) >= 3:
            angles = [pose.angle for pose in poses]
            common = max(angles, key=lambda a: sum(p.score for p in poses
                                                    if angle_distance(a, p.angle) <= 15))
            if sum(angle_distance(common, angle) <= 15 for angle in angles) < 3:
                common = None
        return Observation(time, poses, common)


def ring_distance(point):
    """Signed distance of a point from the cup ring, and the nearest slot."""
    offset = np.asarray(point, dtype=float) - CENTER
    radius = float(np.hypot(*offset))
    angle = float(np.degrees(np.arctan2(offset[1], offset[0])) % 360)
    gaps = np.abs((SLOT_ANGLE - angle + 180) % 360 - 180)
    slot = int(np.argmin(gaps))
    return abs(radius - SLOT_RADIUS[slot]), slots_nearest(point)[1], slot


def slots_nearest(point):
    distances = np.linalg.norm(SLOTS - np.asarray(point, dtype=float), axis=1)
    slot = int(np.argmin(distances))
    return slot, float(distances[slot])


def entry_slot(gem_trail):
    """The cup that hides the diamond when the shuffle starts.

    The diamond sits in the middle of the ring until the cups start moving and
    then slides out under the cup that covers it. Fitting a direction over the
    whole exit instead of a single frame keeps this stable on slow captures.
    """
    if len(gem_trail) < 3:
        return None
    times = np.array([point[0] for point in gem_trail])
    spots = np.array([point[1] for point in gem_trail], dtype=float)
    # Frames where the diamond was still parked over the middle of the ring.
    still = np.linalg.norm(spots - CENTER, axis=1) < 12
    if not still.any():
        still = np.linalg.norm(spots - CENTER, axis=1) < 19
    if not still.any():
        return None
    origin = np.median(spots[still], axis=0)
    last_still = int(np.flatnonzero(still)[-1])
    moved = np.linalg.norm(spots[last_still:] - origin, axis=1)
    if moved.max() < 8:
        return None
    # Use the furthest visible point of the exit: the direction is least noisy there.
    exit_point = spots[last_still + int(np.argmax(moved))]
    direction = exit_point - origin
    norm = float(np.hypot(*direction))
    if norm < 8:
        return None
    direction /= norm
    vectors = SLOTS - CENTER
    vectors = vectors / np.linalg.norm(vectors, axis=1)[:, None]
    alignment = vectors @ direction
    slot = int(np.argmax(alignment))
    # Require a clear winner, otherwise the diamond barely moved.
    if alignment[slot] < 0.80:
        return None
    return slot


@dataclass
class CupTrack:
    slot: int
    patch: np.ndarray
    position: np.ndarray
    velocity: np.ndarray
    score: float = 1.0
    alive: bool = True

    def predict(self):
        return self.position + self.velocity


PATCH_SIZE = 21
PATCH_RADIUS = PATCH_SIZE // 2


def cup_patch(gray, center):
    x, y = int(round(center[0])), int(round(center[1]))
    crop = gray[y - PATCH_RADIUS:y + PATCH_RADIUS + 1, x - PATCH_RADIUS:x + PATCH_RADIUS + 1]
    return crop.astype(np.float32) if crop.shape == (PATCH_SIZE, PATCH_SIZE) else None


def correlate(frame, template, center, reach=11):
    """Best normalized cross-correlation of a patch near a predicted point."""
    template = template - template.mean()
    denominator = float(np.sqrt((template ** 2).sum())) + 1e-6
    height, width = frame.shape
    best_score, best_point = -1.0, None
    cx, cy = int(round(center[0])), int(round(center[1]))
    for dy in range(-reach, reach + 1, 2):
        for dx in range(-reach, reach + 1, 2):
            x, y = cx + dx, cy + dy
            if x < PATCH_RADIUS or y < PATCH_RADIUS or x + PATCH_RADIUS >= width \
                    or y + PATCH_RADIUS >= height:
                continue
            candidate = frame[y - PATCH_RADIUS:y + PATCH_RADIUS + 1,
                              x - PATCH_RADIUS:x + PATCH_RADIUS + 1]
            candidate = candidate - candidate.mean()
            score = float((template * candidate).sum() / (denominator * (np.sqrt((candidate ** 2).sum()) + 1e-6)))
            if score > best_score:
                best_score, best_point = score, np.array([x, y], dtype=float)
    return best_score, best_point


class CupTracker:
    """Follows all eight cups through one shuffle.

    Cups orbit the ring and tumble, so this uses plain patch correlation with a
    constant-velocity prediction. Tracks that lose their cup are dropped instead
    of drifting onto a neighbour, which keeps the surviving identities honest.
    """

    def __init__(self, gray):
        self.tracks = []
        for slot, center in enumerate(SLOTS):
            crop = cup_patch(gray, center)
            if crop is not None:
                self.tracks.append(CupTrack(slot, crop, np.array(center, dtype=float), np.zeros(2)))
        self.update(gray)

    def update(self, gray):
        proposals = []
        for track in self.tracks:
            if not track.alive:
                continue
            score, point = correlate(gray, track.patch, track.predict())
            if point is None or score < 0.55:
                track.alive = False
                track.score = score
                continue
            proposals.append((score, id(track), track, point))
        # Each position may only belong to one cup.
        taken = set()
        for score, _, track, point in sorted(proposals, key=lambda item: -item[0]):
            key = (int(round(point[0] / 6)), int(round(point[1] / 6)))
            if key in taken:
                track.alive = False
                continue
            taken.add(key)
            track.velocity = point - track.position
            track.position = point
            track.score = score
            refreshed = cup_patch(gray, point)
            if refreshed is not None:
                track.patch = 0.6 * track.patch + 0.4 * refreshed

    @property
    def alive(self):
        return [track for track in self.tracks if track.alive]

    def final_slot(self):
        """Nearest slot of every surviving cup, refusing crowded guesses."""
        mapping = {}
        used = set()
        for track in sorted(self.alive, key=lambda item: -item.score):
            gap, distance, slot = ring_distance(track.position)
            if distance > 20 or gap > 26 or slot in used:
                continue
            used.add(slot)
            mapping[track.slot] = slot
        return mapping


@dataclass(frozen=True)
class Result:
    start_time: float
    end_time: float
    initial_slot: int | None
    final_slot: int | None
    confirmations: int
    reason: str = ""
    ranked: tuple = ()

    def candidates(self):
        """Final slots in confidence order, including the reported one.

        Eight identical cups make a whole-ring roll invisible in the settled
        frames: the ring may have turned by any number of slots. When the vote
        is not confident the neighbours of the best guess are offered too, so a
        wrong tap can still be followed by a correct one instead of running out
        of attempts.
        """
        order = []
        if self.final_slot is not None:
            order.append(self.final_slot)
        for slot, _, _ in self.ranked:
            if slot not in order:
                order.append(slot)
        if self.final_slot is None and order:
            best = order[0]
            # Nearest neighbours first: the ordinary shuffle rolls the ring by
            # one or two cups, and trying a distant cup wastes the few attempts
            # the game allows.
            neighbours = sorted((slot for slot in range(len(SLOTS)) if slot != best),
                                key=lambda slot: min((slot - best) % len(SLOTS),
                                                     (best - slot) % len(SLOTS)))
            for slot in neighbours:
                if slot not in order:
                    order.append(slot)
        return order

    def point(self, slot):
        """Board crop slot to full-viewport tap coordinates."""
        x, y = SLOTS[slot]
        return int(BOARD[0] + 2 * x), int(BOARD[1] + 2 * y)

    @property
    def coordinate(self):
        if self.final_slot is not None:
            return self.point(self.final_slot)
        return None


class Detector:
    """Watches the panel and reports which cup hides the diamond.

    A round is: the diamond parked in the middle under an upright ring, the
    cups shuffling, then the ring settling again. The diamond shows itself
    before the shuffle, so the entry cup is read from the diamond's exit
    direction and the final cup from cup tracking and correlation voting.
    """

    def __init__(self, sample_interval=0.043):
        self.sample_interval = sample_interval
        self.reset()

    def reset(self):
        self.reference = None
        self.matcher = None
        self.gem_trail = []
        self.observations = []
        self.frames = []
        self.boards = []
        self.tracker = None
        self.start_time = 0.0
        self.motion = False
        self.settled_frames = 0
        self.initial_slot = None
        self.last_gem_slot = None
        self.last_gem_angle = None

    def feed(self, time, frame):
        return self.feed_board(time, board_image(frame))

    def feed_board(self, time, board):
        gray = gray_of(board)
        gem = gem_position(board)
        still = upright_layout(gray)
        if gem is not None:
            self.gem_trail.append((time, gem))
            del self.gem_trail[:-60]
        if gem is not None:
            # Remember the cup the diamond was last parked under, which is the
            # only hint left when the entry slide itself was missed.
            gap, distance, slot = ring_distance(gem)
            if distance <= 34 and gap <= 26:
                self.last_gem_slot = slot
            offset = np.asarray(gem, dtype=float) - CENTER
            reach = float(np.hypot(offset[0], offset[1]))
            if reach >= 1.0:
                self.last_gem_angle = float(np.degrees(np.arctan2(offset[1], offset[0])) % 360)
        if not self.motion:
            if still:
                # Remember the last quiet frame: the ring is about to move.
                self.reference = gray
                self.initial_slot = None
                return None
            if self.reference is None:
                return None
            difference = np.abs(gray - self.reference)
            movement = max(float(patch(difference, slot, 18).mean()) for slot in SLOTS)
            if movement < 8:
                return None
            self.motion = True
            self.start_time = time
            self.initial_slot = entry_slot(self.gem_trail)
            self.matcher = PoseMatcher(Image.fromarray(self.reference.astype(np.uint8)))
            self.tracker = CupTracker(self.reference)
        if time - self.start_time > 8:
            return self._finish(time, "cup layout did not settle")
        self.settled_frames = self.settled_frames + 1 if still else 0
        self.frames.append((time, gray))
        self.boards.append(board)
        self.tracker.update(gray)
        if (not self.observations or time - self.observations[0].time <= 0.18
                or time - self.observations[-1].time >= self.sample_interval
                or self.settled_frames >= 3):
            self.observations.append(self.matcher.observe(time, gray))
        if self.settled_frames >= 3 and time - self.start_time > 0.30:
            return self._finish(time)
        return None

    def _finish(self, time, reason=""):
        entry = self.initial_slot if self.initial_slot is not None else self.last_gem_slot
        if entry is None and self.last_gem_angle is not None:
            gaps = np.abs((SLOT_ANGLE - self.last_gem_angle + 180) % 360 - 180)
            entry = int(np.argmin(gaps))
        final, support, ranked = self._decide()
        if reason:
            final, support = None, 0
            if entry is not None:
                # Even an unfinished round leaves the best candidate behind so
                # the caller can offer it before the panel expires.
                ranked = ((entry, 0.0, "last diamond position"),)
        elif final is None and ranked:
            # Keep the best guess so the caller can retry a different cup.
            reason = reason or ranked[0][2] or "ambiguous tracking evidence"
            support = 0
        result = Result(self.observations[0].time if self.observations else self.start_time,
                        time, entry, final, support, reason,
                        tuple((slot, round(score, 2), note) for slot, score, note in ranked))
        self.reset()
        return result

    def _decide(self):
        """Combine independent evidence into a ranked list of final slots."""
        # A diamond shown under an opened cup after a miss is the answer itself;
        # no amount of patch voting can beat it.
        revealed = reveal_slot(self.boards)
        if revealed is not None:
            return revealed, 4, ((revealed, 4.0, "diamond revealed under cup"),)
        votes = {}
        notes = {}

        def add(slot, weight, note):
            votes[slot] = votes.get(slot, 0.0) + weight
            notes.setdefault(slot, []).append(note)

        # The match on the last frame is the strongest single cue: the diamond
        # cup either stopped where the tracker says or it did not.
        if self.frames and self.initial_slot is not None:
            template = cup_patch(self.frames[0][1], SLOTS[self.initial_slot])
            if template is not None:
                best_slot, best_score = None, 0.0
                for slot in range(len(SLOTS)):
                    score, point = correlate(self.frames[-1][1], template, SLOTS[slot], reach=14)
                    if point is None or score < 0.80:
                        continue
                    _, distance, target = ring_distance(point)
                    if distance <= 14 and score > best_score:
                        best_slot, best_score = target, score
                if best_slot is not None:
                    add(best_slot, 1.2 * best_score, "diamond cup matched")
        mapping = self.tracker.final_slot() if self.tracker else {}
        if self.initial_slot is not None and self.initial_slot in mapping:
            add(mapping[self.initial_slot], 2.0, "tracked cup")
        for origin, slot in mapping.items():
            if origin != self.initial_slot:
                add(slot, 0.3 * max(self.tracker.tracks[origin].score, 0.0), "other cup")
        # The old angle-outlier heuristic still helps on the slower swap shuffles.
        legacy, support, _ = select_final(self.observations, self.initial_slot,
                                          self.frames[-1][0] if self.frames else self.start_time)
        if legacy is not None:
            votes[legacy] = votes.get(legacy, 0.0) + 1.5
            notes.setdefault(legacy, []).append("spin path")
        ranked = sorted(((slot, score, ", ".join(notes.get(slot, [])) or "motion evidence")
                         for slot, score in votes.items()), key=lambda item: -item[1])
        if not ranked:
            # Tracking can die completely on a fast tumble. The entry cup is
            # still the best single guess, and the ring usually rolls only one
            # or two places, so hand the caller a usable starting point instead
            # of refusing to tap at all.
            if self.initial_slot is not None:
                return None, 0, ((self.initial_slot, 0.0, "entry cup, tracking lost"),)
            return None, 0, ()
        if self.initial_slot is None:
            # Without an entry cup the tracking evidence cannot prove anything.
            return None, 0, tuple((slot, score, "no visible diamond entry") for slot, score, _ in ranked)
        best, score, _ = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0.0
        if score < 1.2:
            return None, 0, ranked
        if runner and score < runner * 1.15:
            return None, 0, tuple((slot, value, "ambiguous tracking evidence") for slot, value, _ in ranked)
        return best, int(round(score * 2)), tuple(ranked)


def select_final(observations, initial_slot, end_time):
    """Angle-outlier tracking, kept for the slower left/right swap shuffle."""
    if initial_slot is None or len(observations) < 3:
        return None, 0, ""
    start_time = observations[0].time
    initial = SLOTS[initial_slot]
    early = []
    for obs in observations:
        if obs.time - start_time > 0.16:
            break
        near = [pose for pose in obs.outliers(0.73) if pose.distance(initial) <= 18
                and angle_distance(pose.angle, obs.common_angle) >= 45]
        if near:
            early.append(min(near, key=lambda pose: pose.distance(initial)))
    if not early:
        return None, 0, "no fast spin matched the diamond entry"

    # Trace strong angular outliers forward into an upright slot. When all cups
    # match the upright template again, spatial continuity carries their identity.
    votes = {slot: {} for slot in range(len(SLOTS))}
    late = start_time + 0.30 * (end_time - start_time)
    for i, obs in enumerate(observations):
        if obs.time < late:
            continue
        for seed in obs.outliers(0.71):
            current = seed
            last_seen = obs.time
            for later in observations[i + 1:]:
                if later.time - last_seen > 0.16:
                    break
                if later.poses:
                    nearest = min(later.poses, key=lambda p: p.distance(current.position))
                    if nearest.distance(current.position) <= 10:
                        current = nearest
                        last_seen = later.time
            if end_time - last_seen > 0.16:
                continue
            slot = int(np.argmin(np.linalg.norm(SLOTS - current.position, axis=1)))
            if current.distance(SLOTS[slot]) <= 8 and seed.score >= 0.75:
                votes[slot][i] = max(seed.score, votes[slot].get(i, 0))

    strong = {slot: [i for i, score in poses.items() if score >= 0.84]
              for slot, poses in votes.items()}
    if not any(strong.values()):
        return None, 0, ""
    order = sorted(strong, key=lambda slot: len(strong[slot]), reverse=True)
    best = order[0]
    second = order[1] if len(order) > 1 else None
    support = strong[best]
    if (len(support) >= 3 and (second is None or len(support) >= 2 * len(strong[second]))
            and observations[max(support)].time - observations[min(support)].time >= 0.08):
        return best, len(support), ""
    return None, 0, ""


def format_result(result):
    start = SLOT_NAMES[result.initial_slot] if result.initial_slot is not None else "unknown"
    line = f"{result.start_time:6.2f}s - {result.end_time:6.2f}s  entry={start}"
    if result.final_slot is None:
        detail = result.reason or "no selection"
        if result.ranked:
            best = ", ".join(f"{SLOT_NAMES[slot]}={score:.1f}" for slot, score, _ in result.ranked[:3])
            detail = f"{detail}; candidates: {best}"
        return f"{line}  no selection ({detail}; evidence={result.confirmations})"
    note = ", ".join(SLOT_NAMES[slot] for slot, _, _ in result.ranked[:3])
    return (f"{line}  final={SLOT_NAMES[result.final_slot]}  point={result.coordinate}"
            f"  evidence={result.confirmations} frames  ranked={note}")


def analyze_video(path):
    detector = Detector(sample_interval=0.017)
    if Path(path).suffix.lower() == ".npz":
        with np.load(path, allow_pickle=False) as recording:
            times, boards = recording["times"], recording["boards"]
            if times.ndim != 1 or boards.shape != (len(times), *BOARD_SIZE[::-1], 3):
                raise ValueError("Expected timestamped RGB CAPTCHA board crops.")
            for time, board in zip(times, boards):
                if result := detector.feed_board(float(time), Image.fromarray(board)):
                    yield result
    else:
        for _, time, frame in read_video(path):
            if result := detector.feed(time, frame):
                yield result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path, help="recorded 16:9 game viewport (MP4) or saved window round (NPZ)")
    args = parser.parse_args()
    for index, result in enumerate(analyze_video(args.video), 1):
        print(f"Round {index}: {format_result(result)}", flush=True)


if __name__ == "__main__":
    main()

"""Detect and follow the fast-spinning cup in Grow Castle's diamond challenge."""

import argparse
from dataclasses import dataclass
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


def board_image(image):
    width, height = image.size
    if abs(width / height / (16 / 9) - 1) > 0.03:
        raise ValueError("Expected the full 16:9 game viewport.")
    left, top, right, bottom = BOARD
    box = (left * width / VIEWPORT[0], top * height / VIEWPORT[1],
           right * width / VIEWPORT[0], bottom * height / VIEWPORT[1])
    return image.resize(BOARD_SIZE, Image.Resampling.BILINEAR, box=box)


def gem_position(image):
    colors = np.asarray(image, dtype=np.int16)
    red, green, blue = colors[:, :, :3].transpose(2, 0, 1)
    y, x = np.nonzero(GEM_AREA & (blue > 129) & (blue > red + 35) & (green > red + 23))
    if len(x) < 12 or len(x) > 310 or np.ptp(x) > 28 or np.ptp(y) > 26:
        return None
    return np.array([np.mean(x), np.mean(y)])


def patch(image, center, radius):
    x, y = center
    return image[y - radius:y + radius + 1, x - radius:x + radius + 1]


def upright_layout(gray):
    # A ring of nearly identical upright sprites around a flat background.
    reference = patch(gray, SLOTS[0], 14)
    background = float(np.median(gray[100:145, 80:140]))
    if reference.mean() < background + 30 or reference.std() < 9:
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
        self.background = int(np.median(np.asarray(sprite)[:, :3]))
        yy, xx = np.mgrid[-RADIUS:RADIUS + 1, -RADIUS:RADIUS + 1]
        distance_sq = (xx * xx + yy * yy) / 26 ** 2
        weights = ((distance_sq <= 1) / (1 + 0.5 * distance_sq)).astype(np.float32)
        kernels, energies = [], []
        for angle in ANGLES:
            rotated = sprite.rotate(int(angle), resample=Image.Resampling.BICUBIC,
                                    fillcolor=self.background)
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


@dataclass(frozen=True)
class Result:
    start_time: float
    end_time: float
    initial_slot: int | None
    final_slot: int | None
    confirmations: int
    reason: str = ""

    @property
    def coordinate(self):
        if self.final_slot is not None:
            x, y = SLOTS[self.final_slot]
            return int(BOARD[0] + 2 * x), int(BOARD[1] + 2 * y)
        return None


def select_final(observations, initial_slot, end_time):
    if initial_slot is None:
        return None, 0, "no visible diamond entry"
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
    individual = {slot: {} for slot in range(len(SLOTS))}
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
            if current.distance(SLOTS[slot]) <= 8:
                if seed.score >= 0.75:
                    votes[slot][i] = max(seed.score, votes[slot].get(i, 0))
                if sum(angle_distance(seed.angle, p.angle) <= 15 for p in obs.poses) == 1:
                    previous = individual[slot].get(i)
                    if previous is None or seed.score > previous.score:
                        individual[slot][i] = seed

    strong = {slot: [i for i, score in poses.items() if score >= 0.84]
              for slot, poses in votes.items()}
    best, second = sorted(strong, key=lambda slot: len(strong[slot]), reverse=True)[:2]
    support = strong[best]
    if (len(support) >= 3 and len(support) >= 2 * len(strong[second])
            and observations[max(support)].time - observations[min(support)].time >= 0.08):
        return best, len(support), ""

    # Overlapping cups can lower the template score. Require more samples
    # over a longer interval and no competing endpoint in that case.
    best, second = sorted(votes, key=lambda slot: len(votes[slot]), reverse=True)[:2]
    support = votes[best]
    if (len(support) >= 4 and not votes[second]
            and np.mean(list(support.values())) >= 0.78
            and observations[max(support)].time - observations[min(support)].time >= 0.15):
        return best, len(support), ""

    # Multiple ordinary cups can share a secondary rotation. In an ambiguous
    # round, require an independently rotating path along the direction the
    # verified entry cup began moving.
    candidates = []
    early_delta = np.subtract(early[-1].position, early[0].position)
    for slot, seeds in individual.items():
        displacement = SLOTS[slot] - initial
        axis = int(np.argmax(np.abs(displacement)))
        direction = np.sign(displacement[axis])
        if abs(displacement[axis]) < 30 or early_delta[axis] * direction < 4:
            continue
        track = []
        for i, pose in seeds.items():
            offset = np.subtract(pose.position, initial)
            progress = offset[axis] / displacement[axis]
            if not 0.3 <= progress <= 1.15 or abs(offset[1 - axis] - progress * displacement[1 - axis]) > 27:
                continue
            if not track or pose.position != track[-1][1].position or pose.angle != track[-1][1].angle:
                track.append((observations[i].time, pose))
        if (len(track) >= 5 and track[-1][0] - track[0][0] >= 0.13
                and len({pose.angle for _, pose in track}) >= 4
                and np.mean([pose.score for _, pose in track]) >= 0.74
                and sum(pose.score >= 0.78 for _, pose in track) >= 2
                and all(t2 - t1 <= 0.13 and p2.distance(p1.position) <= 12
                        and (p2.position[axis] - p1.position[axis]) * direction >= -3
                        for (t1, p1), (t2, p2) in zip(track, track[1:]))):
            candidates.append((slot, len(track)))
    if len(candidates) == 1:
        slot, count = candidates[0]
        return slot, count, ""
    counts = ", ".join(f"{SLOT_NAMES[slot]}={len(items)}" for slot, items in votes.items() if items)
    reason = "too little consistent tracking evidence"
    return None, len(support), f"{reason}; votes: {counts}" if counts else reason


class Detector:
    def __init__(self, sample_interval=0.043):
        self.sample_interval = sample_interval
        self.reference = None
        self.matcher = None
        self.initial_slot = None
        self.observations = []
        self.settled_frames = 0

    def feed(self, time, frame):
        return self.feed_board(time, board_image(frame))

    def feed_board(self, time, board):
        gray_image = board.convert("L")
        gray = np.asarray(gray_image, dtype=np.float32)
        gem = gem_position(board)
        centered_gem = gem is not None and np.linalg.norm(gem - CENTER) < 11
        if centered_gem and upright_layout(gray):
            if self.reference is None or self.observations:
                self.reference = gray
                self.matcher = PoseMatcher(gray_image)
            self.initial_slot = None
            self.observations.clear()
            self.settled_frames = 0
            return None
        if self.reference is None:
            return None

        difference = np.abs(gray - self.reference)
        movement = max(float(patch(difference, slot, 18).mean()) for slot in SLOTS)
        if not self.observations and gem is not None:
            direction = gem - CENTER
            if np.linalg.norm(direction) >= 19:
                vectors = SLOTS - CENTER
                alignment = vectors @ direction / np.linalg.norm(vectors, axis=1)
                self.initial_slot = int(np.argmax(alignment))
            return None
        if not self.observations and movement < 8:
            return None
        if self.observations and time - self.observations[0].time > 3:
            return self._finish(time, "cup layout did not settle")

        self.settled_frames = self.settled_frames + 1 if movement < 3.2 else 0
        # Preserve the first frames for the fast-spin check. Also retain
        # consecutive frames from a slower live capture.
        if (not self.observations or time - self.observations[0].time <= 0.18
                or time - self.observations[-1].time >= self.sample_interval or self.settled_frames >= 3):
            self.observations.append(self.matcher.observe(time, gray))
        if self.settled_frames >= 3:
            return self._finish(time)
        return None

    def _finish(self, time, reason=""):
        if reason:
            final, support = None, 0
        else:
            final, support, reason = select_final(self.observations, self.initial_slot, time)
        result = Result(self.observations[0].time, time, self.initial_slot, final, support, reason)
        self.__init__(self.sample_interval)
        return result


def format_result(result):
    start = SLOT_NAMES[result.initial_slot] if result.initial_slot is not None else "unknown"
    line = f"{result.start_time:6.2f}s - {result.end_time:6.2f}s  entry={start}"
    if result.final_slot is None:
        return f"{line}  no selection ({result.reason}; evidence={result.confirmations})"
    return (f"{line}  final={SLOT_NAMES[result.final_slot]}  point={result.coordinate}"
            f"  evidence={result.confirmations} frames")


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

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
SKILLS = tuple((x, bar_y, hero_y) for bar_y, hero_y in ((146, 184), (241, 277), (331, 370))
               for x in (293, 373, 453))
GAME_SCREENS = {"home", "gold", "replay_menu", "battle", "skip"}
RETURN_SCREENS = GAME_SCREENS | {"victory", "defeat", "tower", "castles", "captcha"}
AD_CLOSE_SCREENS = {"reward_granted", "ad_end_card", "ad_end_sheet"}
AD_ACTIVITY = "com.google.android.gms.ads.AdActivity"


def viewport(image):
    width, height = image.size
    if abs(width / height / (16 / 9) - 1) > 0.03:
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
    gold: np.ndarray | None = None
    gems: int | None = None
    layout: str | None = None
    level: int | None = None
    cost: int | None = None


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
            if point := self.reward_close(image):
                return Screen("reward_granted", point)
            if point := self.ad_end_close(rgb):
                return Screen("ad_end_card", point)
            if all(self.matches(rgb, name)
                   for name in ("ad_sheet_close", "ad_sheet_chevron", "ad_sheet_cta")):
                return Screen("ad_end_sheet", (1257, 39))
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
        if hud and self.matches(rgb, "wave") and self.matches(rgb, "speed"):
            ready = []
            for index, (x, y, _) in enumerate(SKILLS):
                red, green, blue = rgb[y-2:y+3, x-24:x+25].transpose(2, 0, 1)
                cyan = (blue > 164) & (green > 105) & (blue - red > 65) & (green - red > 50)
                if cyan.mean() >= 0.92:
                    ready.append(index)
            return Screen("battle", ready=tuple(ready))
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
        self.ad_taps = 0
        self.skip_offer = False
        self.gold_before = None
        self.after_victory = False
        self.skills_used = {}
        self.last_skill = float("-inf")

    def start_ad(self, time, gold=None):
        self.ad, self.ad_since, self.ad_warned = "watch", time, False
        self.gold_before = gold
        self.reward_label_seen = False
        self.ad_close_attempts.clear()
        self.ad_taps = 0

    def feed_ad(self, time, screen, age):
        name = screen.name
        if name == "ad_playing":
            if self.ad == "watch":
                self.ad = "playing"
            if age >= 1.5:
                self.ad_close_attempts.clear()
        elif name in AD_CLOSE_SCREENS:
            self.ad = "return"
            self.reward_label_seen |= name == "reward_granted"
            attempts, last_tap = self.ad_close_attempts.get(name, (0, float("-inf")))
            if age >= 0.20 and attempts < 3 and self.ad_taps < 9 and time - last_tap >= 1.5:
                self.ad_close_attempts[name] = (attempts + 1, time)
                self.ad_taps += 1
                label = "close granted ad" if name == "reward_granted" else "close ad end card"
                return Action(label, screen.point)
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
            return self.cast(time, screen.ready) if age >= 0.45 else None
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

    def cast(self, time, ready):
        for index in tuple(self.skills_used):
            self.skills_used[index] = 0 if index in ready else self.skills_used[index] + 1
            if self.skills_used[index] >= 2:
                del self.skills_used[index]
        if time - self.last_skill < 0.30:
            return None
        for index in ready:
            if index not in self.skills_used:
                self.skills_used[index] = 0
                self.last_skill = time
                x, _, y = SKILLS[index]
                return Action(f"cast skill {index + 1}", (x, y))
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
    for time, original in frames:
        if args.video:
            screen = vision.classify(viewport(original), watching_ad=player.ad is not None)
        else:
            activity = foreground_activity(adb, args.serial)
            # An ad can show footage of a game. Its Android window owns the
            # frame until it closes, even if the image resembles a game screen.
            screen = (vision.classify(viewport(original), watching_ad=player.ad is not None,
                                      ad_foreground=activity == AD_ACTIVITY)
                      if activity else Screen("unfocused"))
        was_paused = player.paused
        action = player.feed(time, screen)
        if args.auto_captcha and player.paused and not was_paused:
            attempt_started = monotonic()
            limit = min(16, args.seconds - time) if args.seconds else 16
            try:
                selected = limit > 0 and watch(
                    args.ldconsole, args.index, args.serial, limit, True,
                    start_screen=vision.captcha_start_visible,
                )
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                report(time + monotonic() - attempt_started, f"CAPTCHA attempt stopped: {exc}")
                selected = False
            if selected:
                report(time + monotonic() - attempt_started, "CAPTCHA: selection sent; waiting for the game to resume")
            else:
                report(time + monotonic() - attempt_started, "CAPTCHA: waiting for manual solution")
        if action:
            if args.tap:
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

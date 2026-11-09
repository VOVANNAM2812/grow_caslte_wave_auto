"""Watch one LDPlayer instance for Grow Castle's diamond challenge."""

import argparse
import ctypes as ct
from ctypes import wintypes
from io import BytesIO
from pathlib import Path
from queue import Empty, Full, Queue
import re
from shutil import copyfile
import subprocess
from tempfile import NamedTemporaryFile
from threading import Event, Thread
from time import monotonic, sleep
from uuid import uuid4

import numpy as np
from PIL import Image, ImageGrab

from captcha import CENTER, Detector, VIEWPORT, board_image, format_result, gem_position, upright_layout
from windows_video import read_video


START_POINT = (925, 580)
CAPTURE_FPS = 36


def run(executable, *arguments, timeout=8):
    return subprocess.run([str(executable), *arguments], capture_output=True, check=True,
                          timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW).stdout


def window_handle(console, index):
    output = run(console, "list2").decode(errors="replace")
    for line in output.splitlines():
        columns = line.split(",")
        if len(columns) >= 7 and columns[0] == str(index):
            hwnd = int(columns[3])
            if columns[4] == "1" and hwnd and ct.windll.user32.IsWindow(hwnd):
                return hwnd
            break
    raise RuntimeError(f"LDPlayer instance {index} has no running game window.")


def grab_game_window(hwnd):
    user32 = ct.windll.user32
    if user32.GetAncestor(hwnd, 2) == user32.GetForegroundWindow():
        # A foreground desktop grab includes LDPlayer's hardware-rendered game,
        # which a direct window grab can return as a black rectangle.
        origin, bounds = wintypes.POINT(), wintypes.RECT()
        if (user32.ClientToScreen(hwnd, ct.byref(origin))
                and user32.GetClientRect(hwnd, ct.byref(bounds))
                and bounds.right > 0 and bounds.bottom > 0):
            return ImageGrab.grab(bbox=(origin.x, origin.y, origin.x + bounds.right,
                                        origin.y + bounds.bottom), all_screens=True)
    return ImageGrab.grab(window=hwnd)


def capture_frames(hwnd, frames, stop, start):
    try:
        while not stop.is_set():
            timestamp = monotonic() - start
            if not ct.windll.user32.IsWindow(hwnd):
                raise RuntimeError("LDPlayer's game window closed.")
            with grab_game_window(hwnd) as image:
                frames.put_nowait((timestamp, board_image(image)))
            # Leave room for LDPlayer to render on fast capture hosts.
            stop.wait(max(0, start + timestamp + 1 / CAPTURE_FPS - monotonic()))
    except Full:
        stop.set()
    except (OSError, RuntimeError, ValueError) as exc:
        try:
            frames.put_nowait(exc)
        except Full:
            stop.set()


def game_frame(adb, serial):
    focus = run(adb, "-s", serial, "shell", "dumpsys", "window").decode(errors="replace")
    if not re.search(r"mCurrentFocus=.*\bcom\.raongames\.growcastle/", focus):
        raise RuntimeError("Grow Castle must be the foreground app on the selected ADB device.")

    screenshot = run(adb, "-s", serial, "exec-out", "screencap", "-p")
    with Image.open(BytesIO(screenshot)) as screen:
        return screen.convert("RGB")


def matching_game_frame(adb, serial, captured_board):
    frame = game_frame(adb, serial)
    board = board_image(frame)
    gray = np.asarray(board.convert("L"), dtype=np.float32)
    captured = np.asarray(captured_board.convert("L"), dtype=np.float32)
    if not upright_layout(gray) or np.mean(np.abs(gray - captured)) > 5:
        raise RuntimeError("The selected device no longer shows the same cup layout.")
    return frame, gem_position(board)


def tap_point(adb, serial, frame, point):
    x = round(point[0] * frame.width / VIEWPORT[0])
    y = round(point[1] * frame.height / VIEWPORT[1])
    run(adb, "-s", serial, "shell", "input", "tap", str(x), str(y))
    return x, y


def tap_cup(adb, serial, result, captured_board):
    frame, gem = matching_game_frame(adb, serial, captured_board)
    if gem is not None:
        raise RuntimeError("The cups are no longer ready for a selection.")
    x, y = tap_point(adb, serial, frame, result.coordinate)
    print(f"Sent one tap to {serial} at ({x}, {y}).", flush=True)


def verify_start(frame, gem, start_screen):
    if gem is None or np.linalg.norm(gem - CENTER) >= 11 or not start_screen(frame):
        raise RuntimeError("The CAPTCHA Start button or centered diamond could not be verified.")


def solve_from_adb_recording(adb, serial, start_screen):
    # Record inside Android, where occluding Windows apps cannot hide the game.
    initial = game_frame(adb, serial)
    initial_board = board_image(initial)
    verify_start(initial, gem_position(initial_board), start_screen)
    remote = f"/data/local/tmp/growcastle-captcha-{uuid4().hex}.mp4"
    recording = subprocess.Popen(
        [str(adb), "-s", serial, "shell", "screenrecord", "--size", "960x540",
         "--time-limit", "7", remote],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    local = None
    selected = False
    try:
        deadline = monotonic() + 2
        while recording.poll() is None and monotonic() < deadline:
            try:
                size = run(adb, "-s", serial, "shell", "stat", "-c", "%s", remote, timeout=2)
                if size.strip().isdigit() and int(size) > 0:
                    break
            except subprocess.CalledProcessError:
                pass
            sleep(0.1)
        else:
            raise RuntimeError("ADB screen recording did not start.")

        frame, gem = matching_game_frame(adb, serial, initial_board)
        verify_start(frame, gem, start_screen)
        if recording.poll() is not None:
            raise RuntimeError("ADB screen recording ended before CAPTCHA Start.")
        tap_point(adb, serial, frame, START_POINT)
        print("CAPTCHA: started during ADB screen recording.", flush=True)
        _, stderr = recording.communicate(timeout=9)
        if recording.returncode:
            raise RuntimeError(stderr.decode(errors="replace").strip() or "ADB screen recording failed.")

        data = run(adb, "-s", serial, "exec-out", "cat", remote)
        with NamedTemporaryFile(suffix=".mp4", delete=False) as target:
            local = Path(target.name)
            target.write(data)
        detector = Detector(sample_interval=0.017)
        video = read_video(local)
        try:
            for _, time, image in video:
                board = board_image(image)
                result = detector.feed_board(time, board)
                if result is not None:
                    print(f"Round 1: {format_result(result)}", flush=True)
                    if result.coordinate is None:
                        return False
                    tap_cup(adb, serial, result, board)
                    selected = True
                    return True
        finally:
            video.close()
        return False
    finally:
        if recording.poll() is None:
            recording.kill()
            recording.communicate()
        if local is not None:
            if not selected:
                try:
                    logs = Path(__file__).with_name("logs")
                    logs.mkdir(exist_ok=True)
                    saved = logs / Path(remote).name
                    copyfile(local, saved)
                    print(f"CAPTCHA: kept unselected round at {saved}", flush=True)
                except OSError as exc:
                    print(f"CAPTCHA: could not keep the round recording: {exc}", flush=True)
            local.unlink(missing_ok=True)
        try:
            run(adb, "-s", serial, "shell", "rm", "-f", remote, timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass


def keep_window_round(frames):
    try:
        logs = Path(__file__).with_name("logs")
        logs.mkdir(exist_ok=True)
        saved = logs / f"growcastle-captcha-window-{uuid4().hex}.npz"
        np.savez_compressed(saved, times=[time for time, _ in frames],
                            boards=np.stack([np.asarray(board.convert("RGB")) for _, board in frames]))
        print(f"CAPTCHA: kept unselected window round at {saved}", flush=True)
    except OSError as exc:
        print(f"CAPTCHA: could not keep the window recording: {exc}", flush=True)


def watch(console, index, serial, duration, tap, start_screen=None):
    if start_screen is not None and not tap:
        raise ValueError("Automatic CAPTCHA start requires tap mode.")
    ct.windll.user32.SetProcessDPIAware()
    hwnd = window_handle(console, index)
    adb = console.with_name("adb.exe")
    if tap:
        if run(adb, "-s", serial, "get-state").strip() != b"device":
            raise RuntimeError(f"ADB device {serial} is unavailable.")

    detector = Detector(sample_interval=0.025 if start_screen is not None else 0.043)
    # Keep a complete short shuffle available even while pose analysis lags.
    frames = Queue(maxsize=CAPTURE_FPS * 5)
    stop = Event()
    start = monotonic()
    capture = Thread(target=capture_frames, args=(hwnd, frames, stop, start), daemon=True)
    print(f"Watching LDPlayer instance {index} ({'one tap enabled' if tap else 'preview only'}).",
          flush=True)
    capture.start()
    rounds = 0
    started = False
    selected = False
    round_frames = []
    try:
        while not duration or monotonic() - start < duration:
            if start_screen is not None and not started and monotonic() - start >= 1.25:
                break
            if stop.is_set():
                raise RuntimeError("Capture is ahead of analysis. Restart with LDPlayer at 1280x720.")
            try:
                item = frames.get(timeout=0.1)
            except Empty:
                continue
            if isinstance(item, Exception):
                if start_screen is not None and not started:
                    break
                raise item
            timestamp, board = item
            if started and len(round_frames) < CAPTURE_FPS * 7:
                round_frames.append((timestamp, board))
            result = detector.feed_board(timestamp, board)
            if start_screen is not None and not started and detector.reference is not None:
                frame, gem = matching_game_frame(adb, serial, board)
                verify_start(frame, gem, start_screen)
                tap_point(adb, serial, frame, START_POINT)
                started = True
                round_frames.append((timestamp, board))
                print("CAPTCHA: started after observing the centered diamond.", flush=True)
            if result is not None:
                rounds += 1
                print(f"Round {rounds}: {format_result(result)}", flush=True)
                if tap and result.coordinate is not None:
                    if monotonic() - start - result.end_time > 4:
                        raise RuntimeError("Prediction is stale. Run the detector again.")
                    tap_cup(adb, serial, result, board)
                    selected = True
                    return True
                if start_screen is not None:
                    return False
    finally:
        stop.set()
        capture.join(timeout=2)
        if round_frames and not selected:
            keep_window_round(round_frames)
    if start_screen is not None and not started and (not duration or duration >= 12):
        print("CAPTCHA: window capture unavailable; using ADB screen recording.", flush=True)
        return solve_from_adb_recording(adb, serial, start_screen)
    print(f"Stopped after {rounds} completed challenge(s).", flush=True)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ldconsole", type=Path, default=Path(r"D:\LDPlayer\LDPlayer9\ldconsole.exe"),
                        help="path to ldconsole.exe (adb.exe must be beside it for --tap)")
    parser.add_argument("--index", type=int, default=0, help="LDPlayer instance index (default 0)")
    parser.add_argument("--serial", help="ADB serial (default emulator-5554 for instance 0)")
    parser.add_argument("--seconds", type=float, default=0, help="stop after N seconds (default 0, wait)")
    parser.add_argument("--tap", action="store_true", help="send one verified ADB tap and exit")
    args = parser.parse_args()
    if args.index < 0 or args.seconds < 0:
        parser.error("--index and --seconds must be nonnegative")
    serial = args.serial or f"emulator-{5554 + 2 * args.index}"
    try:
        watch(args.ldconsole, args.index, serial, args.seconds, args.tap)
    except KeyboardInterrupt:
        pass
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace").strip() or str(exc)
        parser.exit(1, f"{detail}\n")
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()

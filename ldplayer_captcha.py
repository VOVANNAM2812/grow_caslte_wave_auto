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
import sys
from tempfile import NamedTemporaryFile
from threading import Event, Thread
from time import monotonic, sleep
from uuid import uuid4

import numpy as np
from PIL import Image, ImageGrab

from captcha import (CENTER, Detector, SLOT_NAMES, VIEWPORT, board_image, dark_share,
                     format_result, gem_position, upright_layout)
from captcha_sheet import build as build_sheet
from windows_video import read_video



class BITMAPINFOHEADER(ct.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


START_POINT = (925, 580)
CAPTURE_FPS = 58
# A frame this dark holds no cups; it is a hidden window, not a view of the game.
BLACK_FRAME = 0.5
# How long a grab that cannot see the window waits before trying again.
CAPTURE_RETRY = 0.05


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


def window_box(hwnd):
    """Screen rectangle of the game client area, or None when it is not usable.

    LDPlayer draws the game with the GPU, so only a desktop grab of the area it
    occupies shows the cups. A window grab returns a black rectangle, and a
    minimised window occupies no area at all.
    """
    user32 = ct.windll.user32
    if user32.IsIconic(hwnd):
        return None
    origin, bounds = wintypes.POINT(), wintypes.RECT()
    if not (user32.ClientToScreen(hwnd, ct.byref(origin))
            and user32.GetClientRect(hwnd, ct.byref(bounds))):
        return None
    if bounds.right < 320 or bounds.bottom < 180:
        return None
    return origin.x, origin.y, origin.x + bounds.right, origin.y + bounds.bottom


def blt_box(box):
    """Copy just `box` of the desktop into an image with BitBlt.

    `ImageGrab.grab` always captures the whole desktop and then crops, which
    costs about 50 ms a frame on a 1080p screen. Copying only the game area
    with BitBlt is several times faster, and the shuffle lasts under a second,
    so the extra frames are what make the cups followable.
    """
    x, y, right, bottom = box
    width, height = right - x, bottom - y
    screen = ct.windll.user32.GetDC(0)
    if not screen:
        return None
    memory = ct.windll.gdi32.CreateCompatibleDC(screen)
    bitmap = ct.windll.gdi32.CreateCompatibleBitmap(screen, width, height)
    try:
        ct.windll.gdi32.SelectObject(memory, bitmap)
        if not ct.windll.gdi32.BitBlt(memory, 0, 0, width, height, screen, x, y, 0x00CC0020):
            return None
        header = BITMAPINFOHEADER()
        header.biSize = ct.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height            # negative: rows top down
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0             # BI_RGB
        buffer = ct.create_string_buffer(width * height * 4)
        if not ct.windll.gdi32.GetDIBits(memory, bitmap, 0, height, buffer,
                                         ct.byref(header), 0):
            return None
        # frombytes copies, so the bitmap buffer can be released straight away.
        return Image.frombytes("RGB", (width, height), buffer.raw, "raw", "BGRX", 0, 1)
    finally:
        ct.windll.gdi32.DeleteObject(bitmap)
        ct.windll.gdi32.DeleteDC(memory)
        ct.windll.user32.ReleaseDC(0, screen)


def grab_game_window(hwnd):
    """Desktop grab of the LDPlayer client area.

    Returns None instead of a black rectangle: grabbing with the window handle
    cannot see a hardware-rendered surface, and quietly returning the black
    result is how a whole round gets analysed on pictures of nothing.
    """
    box = window_box(hwnd)
    if box is None:
        return None
    image = blt_box(box)
    if image is None:
        image = ImageGrab.grab(bbox=box, all_screens=True)
    if dark_share(image) >= BLACK_FRAME:
        return None
    return image


def grab_device(adb, serial):
    """Screenshot straight from the emulated framebuffer.

    This keeps working while LDPlayer is minimised or behind another window,
    because nothing about it depends on the window being on screen. It is
    slower than a desktop grab, so it is the fallback rather than the norm.
    """
    screenshot = run(adb, "-s", serial, "exec-out", "screencap", "-p", timeout=6)
    with Image.open(BytesIO(screenshot)) as screen:
        return screen.convert("RGB")


def capture_frames(frames, stop, start, adb, serial, hwnd=None):
    """Feed timestamped board crops, switching source with the window.

    A desktop grab is used while LDPlayer is visible on screen because it runs
    at a much higher rate. As soon as the window is minimised, behind another
    window, or grabbed as black, frames come from ADB instead, so the round is
    never recorded as a rectangle of nothing.
    """
    source = "window" if hwnd else "adb"
    seen = 0
    began = monotonic()
    announced = None
    try:
        while not stop.is_set():
            timestamp = monotonic() - start
            if hwnd is not None and not ct.windll.user32.IsWindow(hwnd):
                raise RuntimeError("LDPlayer's game window closed.")
            image = grab_game_window(hwnd) if source == "window" else None
            if image is None:
                # The window cannot be seen: read the device itself instead.
                source = "adb"
                image = grab_device(adb, serial)
                if dark_share(image) >= BLACK_FRAME:
                    raise RuntimeError("ADB returned a black screen; is the game running?")
            else:
                source = "window"
            if source != announced:
                print(f"CAPTCHA: capturing from "
                      f"{'the LDPlayer window' if source == 'window' else 'ADB (window hidden)'}.",
                      flush=True)
                announced = source
            seen += 1
            with image:
                frames.put_nowait((timestamp, board_image(image)))
            if source == "window":
                # Leave room for LDPlayer to render on fast capture hosts.
                stop.wait(max(0, start + timestamp + 1 / CAPTURE_FPS - monotonic()))
            else:
                stop.wait(CAPTURE_RETRY)
    except Full:
        stop.set()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        try:
            frames.put_nowait(exc)
        except Full:
            stop.set()
    finally:
        elapsed = monotonic() - began
        if elapsed > 0 and seen:
            rate = seen / elapsed
            print(f"CAPTCHA: capture ran at {rate:.0f} frames/s from {source}.", flush=True)
            if rate < 30:
                print("CAPTCHA: that is too slow to follow the shuffle; keep the "
                      "LDPlayer window visible for a full-rate capture.", flush=True)


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


def panel_visible(frame):
    """True while the diamond CAPTCHA panel covers the game, in any of its
    states: waiting for START, shuffling, or showing "Incorrect"/"Try again"."""
    from autoplay import Vision

    gray = np.asarray(frame.convert("L"), dtype=np.int16)
    # The panel interior is a flat brown card: no HUD or battle pixels in it.
    card = gray[130:470, 330:950]
    if float(np.ptp(card)) < 60:
        return False
    flat = np.abs(card - int(np.median(card))) < 14
    return float(flat.mean()) > 0.55 and Vision(False).matches(np.asarray(frame, dtype=np.int16),
                                                               "captcha")


def feedback(frame):
    """Read the CAPTCHA panel: "wrong" or "waiting"."""
    if not panel_visible(frame):
        return None
    gray = np.asarray(frame.convert("L"), dtype=np.int16)
    if (gray[545:620, 300:1000] > 110).mean() > 0.05:
        return "wrong"
    return "waiting"


def replay_reveal(recording, result):
    """Read the answer the game showed after a miss.

    `recording` is either a saved video path or the list of (time, board) frames
    still held in memory. Either way only frames from after the tap are used, so
    the diamond the game lifts out is not mistaken for the entry diamond.
    """
    from captcha import reveal_slot
    if isinstance(recording, (list, tuple)):
        boards = [board for time, board in recording if time >= result.end_time]
    else:
        boards = []
        video = read_video(recording)
        try:
            for _, time, image in video:
                if time >= result.end_time:
                    boards.append(board_image(image))
        finally:
            video.close()
    if not boards:
        return None
    return reveal_slot(boards)


def select_candidates(adb, serial, result):
    """Offer the tracked cup, then the next candidates while a miss is shown.

    The game reveals the diamond for a few seconds after a wrong pick, so a
    second guess is much better evidence than a single tracked vote.
    """
    deadline = monotonic() + 22
    for slot in result.candidates()[:4]:
        if monotonic() > deadline:
            break
        frame = game_frame(adb, serial)
        if not panel_visible(frame):
            print("CAPTCHA: panel closed after a selection.", flush=True)
            return slot
        x, y = result.point(slot)
        tap_point(adb, serial, frame, (x, y))
        print(f"CAPTCHA: tapped {SLOT_NAMES[slot]} ({x}, {y}).", flush=True)
        wrong = False
        until = monotonic() + 6
        while monotonic() < until:
            sleep(0.5)
            state = feedback(game_frame(adb, serial))
            if state == "wrong":
                wrong = True
                break
            if state is None:
                print(f"CAPTCHA: {SLOT_NAMES[slot]} closed the panel.", flush=True)
                return slot
        if not wrong:
            return slot
        # Let the "Try again" countdown finish so the next tap counts.
        until = monotonic() + 12
        while monotonic() < until:
            if feedback(game_frame(adb, serial)) != "wrong":
                break
            sleep(0.8)
    return None


def verify_start(frame, gem, start_screen):
    if gem is None or np.linalg.norm(gem - CENTER) >= 11 or not start_screen(frame):
        raise RuntimeError("The CAPTCHA Start button or centered diamond could not be verified.")


def solve_from_adb_recording(adb, serial, start_screen):
    """Start the round and follow it from an on-device recording.

    Two recordings are kept: one that always runs the full 16 seconds so a
    failed round can be inspected, and one created after START for analysis.
    The recording outlives the first selection, so the reveal after a miss
    still ends up in the analysed frames.
    """
    initial = game_frame(adb, serial)
    initial_board = board_image(initial)
    verify_start(initial, gem_position(initial_board), start_screen)
    remote = f"/data/local/tmp/growcastle-captcha-{uuid4().hex}.mp4"
    # The device framebuffer is recorded, so the result does not depend on the
    # LDPlayer window being visible while the round runs.
    recording = subprocess.Popen(
        # 60 frames a second: the shuffle lasts under a second and the cup
        # holding the diamond is told apart by how fast it spins, which needs
        # far more than the 15-27 frames a 30 fps capture gives.
        [str(adb), "-s", serial, "shell", "screenrecord", "--video-size", "1280x720",
         "--bit-rate", "20000000", "--time-limit", "16", remote],
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
        started = monotonic()
        print("CAPTCHA: started during ADB screen recording.", flush=True)
        _, stderr = recording.communicate(timeout=18)
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
                if result is None:
                    continue
                print(f"Round 1: {format_result(result)}", flush=True)
                if select_candidates(adb, serial, result) is not None:
                    selected = True
                    return True
                # The recording keeps running past the last tap, so the frames
                # where the game lifts the chosen cup and shows the diamond are
                # already on disk. Read them before deciding the round failed.
                revealed = replay_reveal(local, result)
                if revealed is not None:
                    print(f"CAPTCHA: the recording shows the diamond under "
                          f"{SLOT_NAMES[revealed]}; tapping it.", flush=True)
                    frame = game_frame(adb, serial)
                    x, y = result.point(revealed)
                    tap_point(adb, serial, frame, (x, y))
                    sleep(1.5)
                    if feedback(game_frame(adb, serial)) != "wrong":
                        selected = True
                        return True
                return False
        finally:
            video.close()
        print(f"CAPTCHA: no round completed ({monotonic() - started:.0f}s of recording).", flush=True)
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
                    keep_contact_sheet(saved)
                except OSError as exc:
                    print(f"CAPTCHA: could not keep the round recording: {exc}", flush=True)
            local.unlink(missing_ok=True)
        try:
            run(adb, "-s", serial, "shell", "rm", "-f", remote, timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass


def keep_contact_sheet(recording):
    """Write the PDF contact sheet that a chat upload can accept."""
    try:
        target = recording.with_suffix(".pdf")
        frames, pages = build_sheet(recording, 0.0, None, 0.1, target)
        print(f"CAPTCHA: wrote {frames} frames on {pages} page(s) to {target} "
              "- send this PDF to report the round.", flush=True)
    except (OSError, ValueError, SystemExit) as exc:
        print(f"CAPTCHA: could not write the contact sheet: {exc}", flush=True)


def keep_window_round(frames):
    try:
        logs = Path(__file__).with_name("logs")
        logs.mkdir(exist_ok=True)
        saved = logs / f"growcastle-captcha-window-{uuid4().hex}.npz"
        np.savez_compressed(saved, times=[time for time, _ in frames],
                            boards=np.stack([np.asarray(board.convert("RGB")) for _, board in frames]))
        print(f"CAPTCHA: kept unselected window round at {saved}", flush=True)
        keep_contact_sheet(saved)
    except OSError as exc:
        print(f"CAPTCHA: could not keep the window recording: {exc}", flush=True)


def watch(console, index, serial, duration, tap, start_screen=None):
    if start_screen is not None and not tap:
        raise ValueError("Automatic CAPTCHA start requires tap mode.")
    # A desktop grab is the fast path and only exists on Windows. Without it
    # capture still runs, straight from the device over ADB.
    hwnd = None
    if sys.platform == "win32":
        ct.windll.user32.SetProcessDPIAware()
        try:
            hwnd = window_handle(console, index)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            print(f"CAPTCHA: no window handle ({exc}); capturing from ADB only.", flush=True)
    if not tap and hwnd is None:
        raise RuntimeError("Preview mode needs the LDPlayer window; add --tap for ADB capture.")
    adb = console.with_name("adb.exe")
    if tap:
        if run(adb, "-s", serial, "get-state").strip() != b"device":
            raise RuntimeError(f"ADB device {serial} is unavailable.")

    # Watching a round that has to be solved always goes through the 60 fps ADB
    # recording: the cup holding the diamond is told apart by how hard it
    # spins, and the window grab cannot hold that rate. The window is used to
    # follow along and to confirm the panel, not as the measuring instrument.
    if start_screen is not None:
        print("CAPTCHA: recording at 60 frames/s through ADB.", flush=True)
        return solve_from_adb_recording(adb, serial, start_screen)

    detector = Detector(sample_interval=0.043)
    # Keep a complete short shuffle available even while pose analysis lags.
    frames = Queue(maxsize=CAPTURE_FPS * 5)
    stop = Event()
    start = monotonic()
    # ADB is always available as a source, so capture never depends on the
    # LDPlayer window staying visible: it is only the faster of the two.
    capture = Thread(target=capture_frames, args=(frames, stop, start, adb, serial, hwnd),
                     daemon=True)
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
                if result.blind():
                    # Nothing was seen, so there is nothing to tap: guessing
                    # here would only spend one of the player's attempts.
                    if start_screen is not None:
                        return False
                    raise RuntimeError(
                        "The CAPTCHA round was recorded as black frames. Keep the "
                        "LDPlayer window visible on screen, or run with --tap so "
                        "frames can come from ADB.")
                if tap and (result.coordinate is not None or result.candidates()):
                    if monotonic() - start - result.end_time > 4:
                        raise RuntimeError("Prediction is stale. Run the detector again.")
                    if select_candidates(adb, serial, result) is not None:
                        selected = True
                        return True
                    # Keep capturing for a moment: the game opens the chosen
                    # cup and shows the diamond only after the tap.
                    reveal_deadline = monotonic() + 3.5
                    while monotonic() < reveal_deadline:
                        try:
                            more = frames.get(timeout=0.2)
                        except Empty:
                            continue
                        if isinstance(more, Exception):
                            break
                        round_frames.append(more)
                    revealed = replay_reveal(round_frames, result)
                    if revealed is not None:
                        print(f"CAPTCHA: captured replay shows the diamond under "
                              f"{SLOT_NAMES[revealed]}; tapping it.", flush=True)
                        frame = game_frame(adb, serial)
                        x, y = result.point(revealed)
                        tap_point(adb, serial, frame, (x, y))
                        sleep(1.5)
                        if feedback(game_frame(adb, serial)) != "wrong":
                            selected = True
                            return True
                    if start_screen is not None:
                        return False
                if start_screen is not None:
                    return False
    finally:
        stop.set()
        capture.join(timeout=2)
        if round_frames and not selected:
            keep_window_round(round_frames)
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

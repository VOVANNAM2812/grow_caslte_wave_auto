"""Pile a CAPTCHA recording into a PDF contact sheet that chat can accept.

Chat uploads take PDFs but not videos, so a round saved by auto play is turned
into pages of timestamped board crops. Each crop keeps the whole cup ring, so
the diamond entry, the shuffle and the final layout stay visible.

Usage:
    python captcha_sheet.py logs/growcastle-captcha-<id>.mp4
    python captcha_sheet.py logs/growcastle-captcha-window-<id>.npz
    python captcha_sheet.py video1.mp4 --from 2.0 --to 5.0 --out round.pdf
"""

import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from captcha import BOARD, VIEWPORT, board_image
from windows_video import read_video

PAGE = (1240, 1754)
MARGIN = 30
COLUMNS = 3
ROWS = 4
LABEL = 26


def crop_board(image):
    """Cup ring at twice the detector resolution, aspect kept."""
    width, height = image.size
    left, top, right, bottom = BOARD
    box = (left * width / VIEWPORT[0], top * height / VIEWPORT[1],
           right * width / VIEWPORT[0], bottom * height / VIEWPORT[1])
    return image.resize((430, 420), Image.Resampling.BILINEAR, box=box)


def page(frames, number, total):
    sheet = Image.new("RGB", PAGE, "white")
    draw = ImageDraw.Draw(sheet)
    draw.text((MARGIN, 14), f"Grow Castle CAPTCHA - page {number}/{total}",
              fill="black")
    cell = (PAGE[0] - 2 * MARGIN - (COLUMNS - 1) * 10) // COLUMNS
    top = 68
    for slot, (time, crop) in enumerate(frames):
        column, row = slot % COLUMNS, slot // COLUMNS
        x = MARGIN + column * (cell + 10)
        y = top + row * (cell * 420 // 430 + LABEL + 12)
        scaled = crop.resize((cell, cell * 420 // 430), Image.Resampling.LANCZOS)
        sheet.paste(scaled, (x, y))
        draw.text((x + 2, y + scaled.height + 4), f"t = {time:6.2f}s",
                  fill="black")
    return sheet


def _keep(candidates, start, stop, step):
    """Crops inside the time window, at most one per `step` seconds."""
    previous = None
    for time, crop in candidates:
        if time < start or (stop is not None and time > stop):
            continue
        if previous is not None and time - previous < step - 1e-9:
            continue
        previous = time
        yield time, crop


def frames_from_mp4(path, start, stop, step):
    return _keep(((float(time), crop_board(image)) for _, time, image in read_video(path)),
                 start, stop, step)


def frames_from_npz(path, start, stop, step):
    with np.load(path, allow_pickle=False) as recording:
        times, boards = recording["times"], recording["boards"]
    return _keep(((float(time), Image.fromarray(board).resize((430, 420), Image.Resampling.NEAREST))
                  for time, board in zip(times, boards)),
                 start, stop, step)


def build(path, start, stop, step, target):
    reader = frames_from_npz if path.suffix.lower() == ".npz" else frames_from_mp4
    frames = list(reader(path, start, stop, step))
    if not frames:
        raise SystemExit("No frames captured; widen --from/--to.")
    per_page = COLUMNS * ROWS
    pages = [page(frames[i:i + per_page], i // per_page + 1,
                  (len(frames) + per_page - 1) // per_page)
             for i in range(0, len(frames), per_page)]
    pages[0].save(target, save_all=True, append_images=pages[1:],
                  resolution=150.0, quality=82)
    return len(frames), len(pages)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("recording", type=Path,
                        help="MP4 saved by auto play, or a saved NPZ window round")
    parser.add_argument("--from", dest="start", type=float, default=0.0,
                        help="first timestamp to keep (default 0)")
    parser.add_argument("--to", dest="stop", type=float, default=None,
                        help="last timestamp to keep (default: end)")
    parser.add_argument("--step", type=float, default=0.125,
                        help="seconds between crops (default 0.125 = 8 per second)")
    parser.add_argument("--out", type=Path, default=None,
                        help="PDF path (default: recording name with .pdf)")
    args = parser.parse_args()
    target = args.out or args.recording.with_suffix(".pdf")
    frames, pages = build(args.recording, args.start, args.stop, args.step, target)
    size = target.stat().st_size / 1e6
    print(f"{frames} frames on {pages} pages -> {target} ({size:.1f} MB)")


if __name__ == "__main__":
    main()

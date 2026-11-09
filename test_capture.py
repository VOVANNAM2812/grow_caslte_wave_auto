"""Checks that a hidden LDPlayer window can never pass for a view of the game.

Run with:  python test_capture.py logs/*.npz

The recordings are the ones `ldplayer_captcha.py` keeps after an unselected
round. One of them was captured while the LDPlayer window was behind another
window: 107 of its 131 frames are a black rectangle. Those frames must be
refused instead of analysed, and a round that goes black while it is already
under way must end instead of waiting for a shuffle that can never be seen.
"""

import sys
from pathlib import Path

import numpy as np
from PIL import Image

from captcha import Detector, analyze_video, dark_share, format_result


def rounds(path):
    return list(analyze_video(path))


def check(recordings):
    blind_rounds = 0
    seen_rounds = 0
    for path in recordings:
        with np.load(path, allow_pickle=False) as saved:
            boards = saved["boards"]
            times = saved["times"]
        share = np.mean([dark_share(Image.fromarray(board)) for board in boards])
        print(f"{Path(path).name}: {share:.0%} of the frames are black")
        for result in rounds(path):
            print(f"  {format_result(result)}")
            if share >= 0.5:
                # A round recorded through a hidden window holds nothing to go
                # on, so it must offer no guess at all.
                assert result.blind(), "a mostly black round was not refused"
                assert result.candidates() == [], "a blind round offered guesses"
                blind_rounds += 1
            else:
                assert not result.blind(), "a visible round was marked blind"
                seen_rounds += 1

    assert dark_share(Image.new("RGB", (215, 210), (0, 0, 0))) == 1.0

    if blind_rounds:
        # A window that closes mid-round must end the round promptly instead of
        # leaving the caller waiting for a shuffle that will never be seen.
        det = Detector(sample_interval=0.017)
        with np.load(recordings[-1], allow_pickle=False) as saved:
            times, boards = saved["times"], saved["boards"]
        visible = [i for i, board in enumerate(boards)
                   if dark_share(Image.fromarray(board)) < 0.1][:30]
        for i in visible:
            det.feed_board(float(times[i]), Image.fromarray(boards[i]))
        black = Image.new("RGB", (215, 210), (0, 0, 0))
        closed = None
        for step in range(40):
            closed = det.feed_board(float(times[visible[-1]]) + (step + 1) * 0.05, black)
            if closed is not None:
                break
        assert closed is not None, "the detector stalled on a black window"
        assert closed.blind(), "a mid-round blackout was not reported"
        print(f"  blackout: {format_result(closed)}")

    print(f"PASS: {blind_rounds} blind round(s) refused, {seen_rounds} visible analysed")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    check(sys.argv[1:])

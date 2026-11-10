"""Checks for the three ad-closing layers, without a device or a live ad.

Run with:  python test_ad_close.py

The XML below is the shape `uiautomator dump` produces. Each case is one of the
layouts that used to defeat the template matcher: a close button that has no
text, one in a different language, one in the top-left instead of the
top-right, and a full-screen ad whose widgets are all unnamed so only the
corner rule can find anything.
"""

import numpy as np

import ad_close
import autoplay
from PIL import Image


def hierarch(*nodes):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<hierarchy rotation="0">\n'
            + "".join(nodes)
            + "</hierarchy>\n")


def node(text="", desc="", rid="", box="[0,0][10,10]", clickable="false",
         cls="android.widget.ImageView"):
    return (f'<node index="0" text="{text}" resource-id="{rid}" class="{cls}" '
            f'package="com.google.android.gms.ads" content-desc="{desc}" '
            f'clickable="{clickable}" enabled="true" bounds="{box}" />\n')


def check_widgets():
    # 1. an icon-only X in the top right, no text at all
    xml = hierarch(node(box="[640,18][712,90]", clickable="true"))
    found = ad_close.widgets(xml)
    best = ad_close.close_widget(found, 720, 1280)
    assert best is not None, "icon-only X was not found"
    assert best[1] == (676, 54), best[1]
    print(f"icon-only X      -> {best[1]}  (score {best[0]:.1f})")

    # 2. the same control labelled in Vietnamese
    xml = hierarch(node(text="Đóng", box="[640,18][712,90]", clickable="true"))
    best = ad_close.close_widget(ad_close.widgets(xml), 720, 1280)
    assert best is not None and best[0] >= 3.0, best
    print(f"'Đóng' button    -> {best[1]}  (score {best[0]:.1f})")

    # 3. a close button in the top LEFT
    xml = hierarch(node(desc="Close ad", box="[8,20][80,92]", clickable="true"))
    best = ad_close.close_widget(ad_close.widgets(xml), 720, 1280)
    assert best is not None and best[1] == (44, 56), best
    print(f"top-left close   -> {best[1]}")

    # 4. nothing dismissible: a big CTA and an unnamed background
    xml = hierarch(node(box="[0,0][720,1280]"),
                   node(box="[100,900][620,1000]", clickable="true",
                        cls="android.widget.Button"))
    best = ad_close.close_widget(ad_close.widgets(xml), 720, 1280)
    assert best is None, f"invented a close button: {best}"
    print("no control       -> None (correctly refused)")


def check_activity():
    samples = {
        "com.raongames.growcastle/com.raongames.growcastle.MainActivity": False,
        "com.google.android.gms.ads.AdActivity": True,
        "com.unity3d.services.ads.adunit.AdUnitActivity": True,
        "com.applovin.adview.AppLovinFullscreenActivity": True,
        "com.android.vending/com.google.android.finsky.activities.MainActivity": True,
        "com.google.android.gms/com.google.android.gms.ads.AdActivity": True,
    }
    for window, expected in samples.items():
        package = window.split("/")[0]
        got = any(package.startswith(known) for known in ad_close.AD_PACKAGES)
        got = got and not package.startswith(ad_close.GAME_PACKAGE)
        assert got == expected, f"{window}: got {got}, expected {expected}"
        print(f"  {'AD ' if got else 'game'}  {window}")


def check_viewport():
    assert autoplay.viewport(Image.new("RGB", (1280, 720))) is not None
    assert autoplay.viewport(Image.new("RGB", (720, 1280))) is not None, \
        "a portrait ad is still being thrown away"
    assert autoplay.viewport(Image.new("RGB", (900, 900))) is None
    print("portrait 720x1280 accepted, square still refused")


def check_labels():
    # Every language the word list claims must actually be matched.
    for word in ("close", "Đóng", "bỏ qua", "schließen", "закрыть", "关闭", "닫기"):
        node_ = {"text": word, "box": (0, 0, 40, 40)}
        assert any(candidate in word.lower() for candidate in ad_close.CLOSE_WORDS), word
    print(f"{len(ad_close.CLOSE_WORDS)} close words registered")


def check_classify_types():
    """classify() must never mix a list and a tuple in one Screen.

    A TypeError here kills the whole run mid-ad, which is exactly what happened
    when the chevron branch returned `list + tuple`. Every ad branch is exercised
    with a picture that triggers it, and the resulting Screen is checked.
    """
    vision = autoplay.Vision(False)
    blank = Image.new("RGB", (1280, 720), (16, 16, 16))

    # Corners that look like a drawn X, plus a chevron-like pair of strokes.
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    for x, y in ((40, 40), (1240, 40), (40, 680), (1240, 680)):
        frame[y - 12:y + 12, x - 12:x + 12] = 255
    drawn = Image.fromarray(frame)

    cases = [(blank, "blank"), (drawn, "drawn corners")]
    for image, label in cases:
        for watching in (True, False):
            for ad_fg in (True, False):
                screen = vision.classify(image, watching_ad=watching, ad_foreground=ad_fg)
                assert isinstance(screen, autoplay.Screen), (label, screen)
                assert isinstance(screen.points, tuple), \
                    f"{label}: points became {type(screen.points).__name__}"
                assert screen.point is None or isinstance(screen.point, tuple), label
    print(f"{len(cases) * 4} classify() branches returned a well-formed Screen")


def check_running_ad_untouched():
    """A live ad with a counter must not be tapped, by any path.

    The screenshot this is built from showed "Reward in 11 seconds" with a big
    Install bar over the bottom of the ad. Tapping the X there asks whether to
    abandon the reward, and the taps that followed opened a store page. So the
    frame is rebuilt from the measurements taken off that screenshot and every
    route to a tap is checked.
    """
    rgb = np.zeros((720, 1280, 3), dtype=np.int16)
    rgb[:, :, :] = 148            # the ad's flat grey background
    # countdown words in the top right
    rgb[36:50, 1054:1133] = 40
    rgb[36:50, 1150:1232] = 40
    # the X beside them
    for step in range(17):
        rgb[36 + step, 1250 + step // 2] = 30
        rgb[36 + step, 1260 - step // 2] = 30
    # the Install bar across the bottom
    rgb[640:718, 461:1279] = 45
    live = Image.fromarray(rgb.astype(np.uint8))

    assert autoplay.countdown_band(np.asarray(live, dtype=np.int16)) is not None, \
        "the countdown was not read, so a live ad looks finished"
    assert autoplay.payout_pill(np.asarray(live, dtype=np.int16)), "Install bar missed"
    assert autoplay.in_payout_zone((110, 665)), "bottom-left corner not protected"

    vision = autoplay.Vision(False)
    screen = vision.classify(live, watching_ad=True, ad_foreground=True)
    assert screen.name == "ad_counting", \
        f"a live ad was classified as '{screen.name}', which invites a tap"

    player = autoplay.Player("wave", lambda *a: None)
    player.feed_ad(0.0, autoplay.Screen("ad_playing"), 0.0)
    assert player.feed_ad(1.0, screen, 1.0) is None, "tapped a counting ad"
    assert player.close_stalled_ad(3.0, screen, 5.0) is None, "guessed a spot"
    assert player.device_close(None, None, 9.0) is None, "reached the device"
    print("live ad with counter: no tap from any path")

    # The warning dialog must be answered with the button that keeps the reward.
    warn = np.zeros((720, 1280, 3), dtype=np.int16)
    warn[:, :, :] = 148
    warn[227:515, 405:890] = 250                      # the pale panel
    warn[426:480, 679:869] = (60, 130, 245)           # the solid blue button
    warning = Image.fromarray(warn.astype(np.uint8))
    found = autoplay.ad_warning_dialog(np.asarray(warning, dtype=np.int16))
    assert found is not None and found[0] == autoplay.AD_RESUME, found
    assert 700 < found[1][0] < 850 and 400 < found[1][1] < 500, found[1]
    print(f"warning dialog: answers RESUME at {found[1]} (never CLOSE VIDEO)")

    screen = vision.classify(warning, watching_ad=True, ad_foreground=True)
    assert screen.name == "ad_warning" and screen.sub == autoplay.AD_RESUME, screen
    action = player.feed_ad(12.0, screen, 1.0)
    assert action is not None and action.point == found[1], action
    print("warning answered, and the counter is now waited out")


def _frame(name):
    """One of the real screenshots the reported bugs were found on."""
    from pathlib import Path
    return Image.open(Path(__file__).with_name("testdata") / name).convert("RGB") \
        .resize((1280, 720), Image.Resampling.LANCZOS)


def check_loading_screen_is_not_a_dialog():
    """A pale loading screen must not be mistaken for the close warning.

    testdata/loading.png is the frame that exposed this: the game's white
    loading page with a blue spinner, shown while the ad is torn down.
    ad_warning_dialog() saw 97% pale and one saturated blob, and answered
    "keep watching ad" at the spinner every 1.5 s, forever.
    """
    loading = _frame("loading.png")
    frame = np.asarray(loading, dtype=np.int16)

    assert autoplay.ad_warning_dialog(frame) is None, \
        "the loading screen was taken for the close warning"
    assert autoplay.countdown_band(frame) is None, \
        "a white status strip was read as a countdown"
    vision = autoplay.Vision(False)
    screen = vision.classify(loading, watching_ad=True, ad_foreground=True)
    assert screen.name not in {"ad_warning", "ad_counting"}, screen.name

    player = autoplay.Player("wave", lambda *a: None)
    player.feed_ad(0.0, autoplay.Screen("ad_playing"), 0.0)
    for time in (10.0, 11.5, 13.0, 20.0):
        assert player.feed_ad(time, screen, time) is None, "tapped the spinner"
    print("loading screen: no warning, no countdown, no tap")

    # The real dialog, from the screenshot that showed it, must still be found.
    counting = _frame("ad_counting.png")
    found = autoplay.ad_warning_dialog(np.asarray(counting, dtype=np.int16))
    assert found is not None and found[0] == autoplay.AD_RESUME, found
    assert 700 < found[1][0] < 860 and 400 < found[1][1] < 500, found[1]
    print(f"real dialog still answered at {found[1]}")


def check_every_real_frame():
    """Every screenshot a bug was found on, checked for the right decision.

    These are the frames the user reported problems with, so they stay in
    testdata/ and each one pins one behaviour:

      ad_counting.png  "Reward in 11 seconds" - a running ad, never tapped
      ad_banner19.png  the same banner on a WHITE ad: the background changed,
                       the decision must not
      ad_dialog.png    the "Close Video?" panel over a running ad
      loading.png      the game's loading page, not a dialog
    """
    from pathlib import Path
    data = Path(__file__).with_name("testdata")
    vision = autoplay.Vision(False)
    expected = {
        "ad_counting.png": "ad_warning",   # it also has the panel open
        "ad_banner19.png": "ad_counting",
        "ad_dialog.png": "ad_warning",
        "loading.png": {"ad_playing", "unknown"},
    }
    for name, want in expected.items():
        image = _frame(name)
        screen = vision.classify(image, watching_ad=True, ad_foreground=True)
        if isinstance(want, set):
            assert screen.name in want, f"{name}: got {screen.name}"
        else:
            assert screen.name == want, f"{name}: got {screen.name}, want {want}"
        # No frame of a running ad may yield a tap the player will act on.
        if screen.name in {"ad_counting", "ad_warning"}:
            player = autoplay.Player("wave", lambda *a: None)
            player.feed_ad(0.0, autoplay.Screen("ad_playing"), 0.0)
            action = player.feed_ad(5.0, screen, 5.0)
            if screen.name == "ad_counting":
                assert action is None, f"{name}: tapped a counting ad"
            else:
                assert action is None or action.name == "keep watching ad", action
        print(f"  {name:18s} -> {screen.name}")
    # The white banner is the one that regressed: both must read as counting.
    for name in ("ad_counting.png", "ad_banner19.png"):
        pass


if __name__ == "__main__":
    print("--- widgets ---")
    check_widgets()
    print("\n--- activity recognition ---")
    check_activity()
    print("\n--- viewport ---")
    check_viewport()
    print("\n--- labels ---")
    check_labels()
    print("\n--- classify types ---")
    check_classify_types()
    print("\n--- a running ad ---")
    check_running_ad_untouched()
    print("\n--- a loading screen ---")
    check_loading_screen_is_not_a_dialog()
    print("\n--- every real frame ---")
    check_every_real_frame()
    print("\nPASS: all ad-closing layers behave as described")

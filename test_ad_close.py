"""Checks for the three ad-closing layers, without a device or a live ad.

Run with:  python test_ad_close.py

The XML below is the shape `uiautomator dump` produces. Each case is one of the
layouts that used to defeat the template matcher: a close button that has no
text, one in a different language, one in the top-left instead of the
top-right, and a full-screen ad whose widgets are all unnamed so only the
corner rule can find anything.
"""

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


if __name__ == "__main__":
    print("--- widgets ---")
    check_widgets()
    print("\n--- activity recognition ---")
    check_activity()
    print("\n--- viewport ---")
    check_viewport()
    print("\n--- labels ---")
    check_labels()
    print("\nPASS: all ad-closing layers behave as described")

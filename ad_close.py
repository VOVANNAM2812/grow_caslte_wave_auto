"""Find and close a rewarded ad on an LDPlayer instance.

Three independent ways to dismiss a finished ad, tried in order of how much
they can be trusted:

1. `uiautomator dump` lists the real widgets, so a close button is located from
   its bounds no matter which language the ad uses, what shape it is drawn in,
   or whether the screen is upright or rotated to 720x1280.
2. Geometry finds the X or the >> skip arrow drawn by Canvas/WebGL, which never
   appears as a widget at all.
3. KEYCODE_BACK dismisses the end card that some SDKs show after the reward,
   and the small Google Play sheet that they sometimes open by accident.

Activity names are read from `dumpsys window`, so any ad SDK is recognised, not
just AdMob's AdActivity. All three steps are safe to call at any time: an ad
that is still counting down swallows BACK itself, and a dump that finds nothing
simply returns None.
"""

import re
import subprocess
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Packages that show full-screen ads or the sheets they open over the game.
AD_PACKAGES = (
    "com.google.android.gms.ads",
    "com.google.android.gms",
    "com.unity3d.services",
    "com.unity3d.ads",
    "com.applovin",
    "com.applovin.array",
    "com.ironsource",
    "com.mintegral",
    "com.mbridge",
    "com.vungle",
    "com.bytedance.sdk",
    "com.adcolony",
    "com.chartboost",
    "com.facebook.ads",
    "com.google.android.finsky",
    "com.android.vending",
)

# Words that mark a dismiss control in the ad's own language.
CLOSE_WORDS = ("close", "dismiss", "skip", "no thanks", "not now", "later",
               "đóng", "bỏ qua", "tắt", "kok", "zamknij", "schließen", "cerrar",
               "fermer", "chiudi", "fechar", "закрыть", "закрити", "关闭", "关闭广告",
               "닫기", "閉じる", "ปิด", "tutup", "kapat", "để sau")

GAME_PACKAGE = "com.raongames.growcastle"


def run(executable, *arguments, timeout=10):
    return subprocess.run([str(executable), *arguments], capture_output=True, check=True,
                          timeout=timeout, creationflags=CREATE_NO_WINDOW).stdout


def foreground_window(adb, serial):
    """Activity name in front of the user, or an empty string."""
    try:
        text = run(adb, "-s", serial, "shell",
                   "dumpsys", "window", timeout=12).decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"mCurrentFocus=.*?\s([\w.]+)/([\w.$]+)", text)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return ""


def ad_in_foreground(adb, serial):
    """True when the window in front belongs to an ad SDK, not to the game."""
    window = foreground_window(adb, serial)
    if not window:
        return False
    package = window.split("/")[0]
    if package.startswith(GAME_PACKAGE):
        return False
    return any(package.startswith(known) for known in AD_PACKAGES)


def screen_size(adb, serial):
    """Real display size, which is 720x1280 while a vertical ad plays."""
    try:
        text = run(adb, "-s", serial, "shell", "wm", "size",
                   timeout=8).decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)x(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def dump_widgets(adb, serial, remote="/data/local/tmp/growcastle-ui.xml"):
    """The uiautomator hierarchy as text, or an empty string."""
    try:
        run(adb, "-s", serial, "shell", "uiautomator", "dump", remote, timeout=25)
        return run(adb, "-s", serial, "exec-out", "cat", remote,
                   timeout=12).decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""


NODE = re.compile(r"<node\b[^>]*>", re.S)
ATTRIBUTE = re.compile(r'([\w-]+)="([^"]*)"')
BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def widgets(xml):
    """Every node with its attributes and pixel bounds."""
    found = []
    for match in NODE.finditer(xml):
        attributes = dict(ATTRIBUTE.findall(match.group(0)))
        box = BOUNDS.search(attributes.get("bounds", ""))
        if not box:
            continue
        left, top, right, bottom = (int(value) for value in box.groups())
        if right - left < 8 or bottom - top < 8:
            continue
        attributes["box"] = (left, top, right, bottom)
        attributes["centre"] = ((left + right) // 2, (top + bottom) // 2)
        found.append(attributes)
    return found


def _labels(node):
    return " ".join(node.get(name, "") for name in
                    ("text", "content-desc", "resource-id", "class")).lower()


def close_widget(found, width, height):
    """Closest match to a dismiss control, as a tap point.

    A node that says close, and one drawn as a small square in a top corner,
    are both accepted. The second rule covers the icon-only X that most ads
    use, which has no text at all.
    """
    best = None
    for node in found:
        left, top, right, bottom = node["box"]
        box_width, box_height = right - left, bottom - top
        label = _labels(node)
        clicks = node.get("clickable") == "true"
        named = any(word in label for word in CLOSE_WORDS)
        # A dismiss control sits in a corner: the top is by far the most common,
        # and a named one is trusted anywhere.
        in_top = top < height * 0.22
        if not named and not in_top:
            continue
        if not named and box_width > width * 0.25:
            # An icon only counts when it is small: a wide widget across the top
            # is a banner, not an X.
            continue
        score = 3.0 if named else 0.0
        if in_top:
            score += 1.0
        if box_width < width * 0.12 and box_height < height * 0.10:
            score += 0.5
        if clicks:
            score += 0.5
        if best is None or score > best[0]:
            best = (score, node["centre"], label.strip())
    return best


def close_from_widgets(adb, serial):
    """Tap a dump-located close button; returns its point or None."""
    size = screen_size(adb, serial)
    if size is None:
        return None
    xml = dump_widgets(adb, serial)
    if not xml or "<hierarchy" not in xml:
        return None
    found = widgets(xml)
    if not found:
        return None
    best = close_widget(found, *size)
    if best is None:
        return None
    score, point, label = best
    print(f"AD: widget '{label or 'icon'}' at {point} (score {score:.1f}).", flush=True)
    tap(adb, serial, point)
    return point


def tap(adb, serial, point):
    run(adb, "-s", serial, "shell", "input", "tap", str(int(point[0])), str(int(point[1])))


def send_back(adb, serial):
    """KEYCODE_BACK, which an ad still counting down ignores by itself."""
    run(adb, "-s", serial, "shell", "input", "keyevent", "4")

"""Report what this LDPlayer instance can do about recording a CAPTCHA round.

Run it when a round says the recording could not start:

    .\\.venv\\Scripts\\python.exe check_record.py

It prints the device's own screenrecord usage, which is what decides the size
flag, and then tries one short recording so the reason for a failure is in the
output instead of being guessed at.
"""

import argparse
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def shell(adb, serial, command, timeout=15):
    """Both streams and the exit status, because usage text goes to stderr."""
    finished = subprocess.run(
        [str(adb), "-s", serial, "shell", command],
        capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    return finished.returncode, (finished.stdout + finished.stderr).decode(errors="replace")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ldconsole", type=Path,
                        default=Path(r"D:\LDPlayer\LDPlayer9\ldconsole.exe"),
                        help="path to ldconsole.exe (adb.exe must be beside it)")
    parser.add_argument("--serial", default="emulator-5554")
    args = parser.parse_args()

    adb = args.ldconsole.with_name("adb.exe")
    if not adb.exists():
        raise SystemExit(f"adb.exe not found beside {args.ldconsole}")
    print(f"adb: {adb}")
    print(f"device: {args.serial}")

    code, text = shell(adb, args.serial, "getprop ro.build.version.release")
    print(f"Android version: {text.strip() or 'unknown'}")

    code, text = shell(adb, args.serial, "which screenrecord")
    print(f"which screenrecord: {text.strip() or '(nothing)'}  exit={code}")

    print("\n--- screenrecord --help ---")
    code, text = shell(adb, args.serial, "screenrecord --help")
    print(f"exit={code}")
    print(text.strip() or "(no output)")

    print("\n--- screenrecord (no arguments) ---")
    code, text = shell(adb, args.serial, "screenrecord")
    print(f"exit={code}")
    print(text.strip() or "(no output)")

    usage = shell(adb, args.serial, "screenrecord --help")[1]
    for flag in ("--video-size", "--size"):
        if flag in usage:
            chosen = flag
            break
    else:
        chosen = None
    print(f"\nsize flag this build understands: {chosen or 'none listed'}")

    remote = "/data/local/tmp/growcastle-check.mp4"
    shell(adb, args.serial, f"rm -f {remote}")
    command = "screenrecord"
    if chosen:
        command += f" {chosen} 1280x720"
    command += f" --bit-rate 20000000 --time-limit 2 {remote}"
    print(f"\n--- trying: {command} ---")
    code, text = shell(adb, args.serial, command, timeout=20)
    print(f"exit={code}")
    if text.strip():
        print(text.strip())

    print("\n--- result ---")
    code, text = shell(adb, args.serial, f"ls -l {remote}")
    print(text.strip() or f"{remote} was not created")
    if "No such file" in text or not text.strip():
        print("\nThe device did not record. The output above says why; send it along.")


if __name__ == "__main__":
    sys.exit(main())

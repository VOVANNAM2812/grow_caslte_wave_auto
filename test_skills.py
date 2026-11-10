"""The twelve skill bars must all get cast, not just the front of the list.

Run with:  python test_skills.py

This reproduces a real failure: the run log never contained "cast skill 11"
or "cast skill 12", yet both snowman rows had full bars. The cause was the
order skills were chosen in, not their coordinates - those were measured and
are correct.
"""

import autoplay as A

# How long each bar takes to fill again, in seconds. The nine castle heroes
# recharge about once a second; the tree and the snowmen are slower.
PERIOD = {index: 1.0 for index in range(9)}
PERIOD[9] = 2.0
PERIOD[10] = 4.0
PERIOD[11] = 4.0


def simulate(seconds=300.0, step=0.20):
    player = A.Player("wave", lambda *a: None)
    last_used = {index: -1e9 for index in range(12)}
    fired = {index: 0 for index in range(12)}
    time = 0.0
    while time < seconds:
        ready = tuple(index for index in range(12)
                      if time - last_used[index] >= PERIOD[index])
        for action in player.cast(time, ready, (0,) * len(ready)) or []:
            index = int(action.name.split()[-1]) - 1
            fired[index] += 1
            last_used[index] = time
        time += step
    return fired


def check_all_twelve_fire():
    fired = simulate()
    for index in range(12):
        assert fired[index] > 0, f"skill {index + 1} never fired in 5 minutes"
    # No skill may be starved: the slowest is 4 s against a 1 s hero, so every
    # bar should get a turn regularly rather than a handful of times.
    # At the default three taps a second, five minutes is 900 casts over a
    # field where the fastest hero refills once a second, so an even split is
    # about 25 each. What matters is that no bar is left behind, not the total.
    floor = min(fired.values())
    ceiling = max(fired.values())
    assert floor >= 20, f"a skill only fired {floor} times: still starved"
    assert ceiling / floor < 1.25, f"uneven casting: {floor}..{ceiling}"
    print(f"5 minutes: every skill fired, {floor}..{ceiling} times each")


def check_snowmen_are_not_last():
    """The two snowman rows are the last entries and used to be forgotten."""
    fired = simulate()
    print(f"  snowman rows: skill 11 x{fired[10]}, skill 12 x{fired[11]}")
    assert fired[10] > 0 and fired[11] > 0
    # They must not be starved relative to the fast heroes.
    fastest = max(fired[index] for index in range(9))
    assert fired[10] >= fastest * 0.8, \
        f"snowmen fall behind: {fired[10]} vs {fastest}"


def check_no_repeat_within_two_frames():
    """A skill that was just cast must not be cast again immediately."""
    player = A.Player("wave", lambda *a: None)
    used = []
    for step in range(6):
        for action in player.cast(step * 0.35, (6, 7, 8, 9, 10, 11), (0,) * 6) or []:
            used.append(action.name)
    assert len(set(used)) == len(used), f"repeated a skill back to back: {used}"
    print(f"  six frames: {used}")


def check_cast_rate():
    """The tap rate is a number the user picks, not the frame rate of the loop.

    Previously one tap was allowed per frame, so the pace was whatever the
    screen grab managed - about one a second - and a busy bar waited for the
    next frame no matter how many were ready.
    """
    def measured(rate, seconds=30.0, fps=10.0):
        player = A.Player("wave", lambda *a: None, rate=rate)
        time = 0.0
        total = 0
        while time < seconds:
            total += len(player.cast(time, (6, 7, 8, 9, 10, 11), (0,) * 6) or [])
            time += 1.0 / fps
        return total / seconds

    for wanted in (1, 2, 3, 5, 8):
        got = measured(wanted)
        assert abs(got - wanted) < 0.15, f"asked for {wanted}/s, got {got:.2f}/s"
        print(f"  --skill-rate {wanted}: {got:.2f} taps/s")

    # The default must be three, which is what "1 giây 3 skill" means.
    default = A.Player("wave", lambda *a: None).rate
    assert default == 3.0, f"default rate is {default}, expected 3"


def check_rate_keeps_rotation():
    """Raising the rate must not break the rotation between the twelve bars."""
    period = {index: 1.0 for index in range(9)}
    period[9] = 2.0
    period[10] = 4.0
    period[11] = 4.0
    player = A.Player("wave", lambda *a: None, rate=3.0)
    last_used = {index: -1e9 for index in range(12)}
    fired = {index: 0 for index in range(12)}
    time = 0.0
    while time < 120.0:
        ready = tuple(index for index in range(12)
                      if time - last_used[index] >= period[index])
        for action in player.cast(time, ready, (0,) * len(ready)) or []:
            index = int(action.name.split()[-1]) - 1
            fired[index] += 1
            last_used[index] = time
        time += 0.1
    weak = min(fired.values())
    assert weak > 0, "a skill was starved even at 3 taps a second"
    assert fired[10] > 0 and fired[11] > 0, "snowmen starved at the higher rate"
    print(f"  2 minutes at 3/s: every skill fired, fewest={weak}, "
          f"snowmen={fired[10]}/{fired[11]}")


if __name__ == "__main__":
    print("--- all twelve ---")
    check_all_twelve_fire()
    print("\n--- snowman rows ---")
    check_snowmen_are_not_last()
    print("\n--- no repeats ---")
    check_no_repeat_within_two_frames()
    print("\n--- cast rate ---")
    check_cast_rate()
    print("\n--- rate keeps rotation ---")
    check_rate_keeps_rotation()
    print("\nPASS: every skill bar gets a turn")

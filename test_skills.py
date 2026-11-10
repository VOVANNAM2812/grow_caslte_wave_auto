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
        action = player.cast(time, ready, (0,) * len(ready))
        if action is not None:
            fired[int(action.name.split()[-1]) - 1] += 1
            last_used[int(action.name.split()[-1]) - 1] = time
        time += step
    return fired


def check_all_twelve_fire():
    fired = simulate()
    for index in range(12):
        assert fired[index] > 0, f"skill {index + 1} never fired in 5 minutes"
    # No skill may be starved: the slowest is 4 s against a 1 s hero, so every
    # bar should get a turn regularly rather than a handful of times.
    floor = min(fired.values())
    ceiling = max(fired.values())
    assert floor >= 40, f"a skill only fired {floor} times: still starved"
    assert ceiling / floor < 2.0, f"uneven casting: {floor}..{ceiling}"
    print(f"5 minutes: every skill fired, {floor}..{ceiling} times each")


def check_snowmen_are_not_last():
    """The two snowman rows are the last entries and used to be forgotten."""
    fired = simulate()
    print(f"  snowman rows: skill 11 x{fired[10]}, skill 12 x{fired[11]}")
    assert fired[10] > 0 and fired[11] > 0
    # They must not be starved relative to the fast heroes.
    fastest = max(fired[index] for index in range(9))
    assert fired[10] >= fastest * 0.5, \
        f"snowmen fall behind: {fired[10]} vs {fastest}"


def check_no_repeat_within_two_frames():
    """A skill that was just cast must not be cast again immediately."""
    player = A.Player("wave", lambda *a: None)
    used = []
    for step in range(6):
        action = player.cast(step * 0.35, (6, 7, 8, 9, 10, 11), (0,) * 6)
        if action is not None:
            used.append(action.name)
    assert len(set(used)) == len(used), f"repeated a skill back to back: {used}"
    print(f"  six frames: {used}")


if __name__ == "__main__":
    print("--- all twelve ---")
    check_all_twelve_fire()
    print("\n--- snowman rows ---")
    check_snowmen_are_not_last()
    print("\n--- no repeats ---")
    check_no_repeat_within_two_frames()
    print("\nPASS: every skill bar gets a turn")

"""Manage a verified batch of single Lightning Castle upgrades."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json


GEM_THRESHOLD = 90
UPGRADE_COUNT = 30
TOWER_LAYER_TOGGLE = (380, 457)
LIGHTNING_LAYER = (380, 366)
EXIT_LAYER_VIEW = (550, 90)
LV_UP = (896, 470)


@dataclass(frozen=True)
class Action:
    name: str
    # None means the action is delivered by the device itself (a BACK key),
    # which has no screen coordinate to tap.
    point: tuple[int, int] | None
    charge: tuple[int, int, int] | None = None


@dataclass
class Batch:
    gems: int
    level: int
    restore_hero: bool
    confirmed: int = 0
    pending_cost: int | None = None
    closed: bool = False

    def validate(self):
        if (type(self.gems) is not int or self.gems < 0
                or type(self.level) is not int or self.level < 1
                or type(self.confirmed) is not int or not 0 <= self.confirmed <= UPGRADE_COUNT
                or type(self.restore_hero) is not bool or type(self.closed) is not bool
                or (self.pending_cost is not None
                    and (type(self.pending_cost) is not int or self.pending_cost not in (2, 3)))
                or (self.pending_cost is not None and self.confirmed == UPGRADE_COUNT)
                or (self.closed and self.confirmed != UPGRADE_COUNT)):
            raise ValueError("Invalid tower upgrade journal")


@contextmanager
def lock_journal(path, conflict="A tower upgrade process already owns this LDPlayer journal."):
    if path is None:
        yield
        return
    import msvcrt

    with path.with_suffix(".lock").open("a+b") as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError(conflict) from exc
        yield


class TowerUpgrade:
    def __init__(self, report, journal=None):
        self.report = report
        self.journal = journal
        self.batch = None
        if journal is not None and journal.exists():
            data = json.loads(journal.read_text(encoding="utf-8"))
            if data is not None:
                try:
                    self.batch = Batch(**data)
                except TypeError as exc:
                    raise ValueError("Invalid tower upgrade journal") from exc
                self.batch.validate()
        self.active = self.batch is not None and not self.batch.closed
        self.restore_hero = self.batch is not None and self.batch.restore_hero
        self.navigation = None
        self.observation = None
        self.since = 0
        self.unverified_since = None

    def save(self):
        if self.journal is not None:
            temporary = self.journal.with_suffix(".tmp")
            data = asdict(self.batch) if self.batch else None
            temporary.write_text(json.dumps(data) + "\n", encoding="utf-8")
            temporary.replace(self.journal)

    def navigate(self, time, name, point, expected, layout=None):
        self.navigation = (time, expected, layout)
        return True, Action(name, point)

    def suspend(self):
        self.navigation = None
        self.observation = None
        self.unverified_since = None

    def wait_for_reading(self, time, message):
        if self.unverified_since is None:
            self.unverified_since = time
        elif time - self.unverified_since > 5:
            raise RuntimeError(message)
        return True, None

    def feed(self, time, screen):
        reading = (screen.name, screen.gems, screen.layout, screen.level, screen.cost)
        if reading != self.observation:
            self.observation, self.since = reading, time
        stable = time - self.since >= 0.18
        name, gems, layout, level, cost = reading

        if not self.active:
            if self.batch is not None:
                rearmed = name in {"home", "gold"} and stable and gems is not None and gems < GEM_THRESHOLD
                if self.batch.gems >= GEM_THRESHOLD and not rearmed:
                    return False, None
                self.batch = None
                self.restore_hero = False
                self.save()
            if (name not in {"home", "tower"} or not stable
                    or gems is None or gems < GEM_THRESHOLD):
                return False, None
            self.active = True
            self.report(time, f"Tower upgrade triggered at {gems} gems")

        if self.navigation:
            started, expected, expected_layout = self.navigation
            matches = name == expected or (expected == "castles" and name == "home")
            if matches and (expected_layout is None or layout == expected_layout):
                if not stable:
                    return True, None
                self.navigation = None
            elif time - started > 5:
                raise RuntimeError(f"Tower upgrade stopped while waiting for {expected}.")
            else:
                return True, None
        if not stable:
            return self.wait_for_reading(time, "Tower upgrade stopped: screen did not stabilize.")

        if self.batch is not None and self.batch.confirmed == UPGRADE_COUNT:
            return self.finish(time, name, layout)
        if name == "home":
            if layout == "hero":
                self.restore_hero = True
                if self.batch is not None:
                    self.batch.restore_hero = True
                    self.save()
                return self.navigate(time, "show castle layers", TOWER_LAYER_TOGGLE, "home", "castle")
            if layout == "castle":
                return self.navigate(time, "open Lightning Castle", LIGHTNING_LAYER, "tower")
        if name != "tower" or gems is None or level is None:
            return self.wait_for_reading(time, "Tower upgrade stopped: open the verified Lightning Castle panel.")

        if self.batch is None:
            if gems < GEM_THRESHOLD:
                raise RuntimeError("Tower upgrade stopped: gem balance fell below 90 before the batch.")
            if cost not in (2, 3):
                return self.wait_for_reading(time, "Tower upgrade stopped: single-upgrade price is unverified.")
            self.batch = Batch(gems, level, self.restore_hero)
            self.save()

        batch = self.batch
        if batch.pending_cost is not None:
            if (gems, level) != (batch.gems - batch.pending_cost, batch.level + 1):
                return self.wait_for_reading(
                    time, f"Tower upgrade stopped at {batch.confirmed}/{UPGRADE_COUNT}: "
                    "last tap is unconfirmed; inspect the level and gem balance before restarting."
                )
            batch.gems, batch.level = gems, level
            batch.pending_cost = None
            batch.confirmed += 1
            self.save()
            self.report(time, f"Lightning Castle {batch.confirmed}/{UPGRADE_COUNT} confirmed "
                              f"(level {level}, {gems} gems)")
            self.unverified_since = None
            if batch.confirmed == UPGRADE_COUNT:
                return self.finish(time, name, layout)
        if (gems, level) != (batch.gems, batch.level) or cost not in (2, 3) or gems < cost:
            return self.wait_for_reading(time, "Tower upgrade stopped: level, balance or price is unverified.")

        self.unverified_since = None
        return True, Action(f"upgrade Lightning Castle {batch.confirmed + 1}/{UPGRADE_COUNT} ({cost} gems)",
                            LV_UP, (gems, level, cost))

    def record_tap(self, action):
        # Write ahead of the ADB call. A crash leaves a pending tap that can
        # only be resolved by observing the exact resulting level and balance.
        gems, level, cost = action.charge
        batch = self.batch
        if (batch is None or batch.pending_cost is not None or batch.confirmed >= UPGRADE_COUNT
                or (gems, level) != (batch.gems, batch.level) or cost not in (2, 3)):
            raise RuntimeError("Tower upgrade stopped: journal and planned tap disagree.")
        batch.pending_cost = cost
        self.save()

    def finish(self, time, name, layout):
        if name == "tower":
            return self.navigate(time, "close Lightning Castle", (994, 160), "castles")
        if name == "castles":
            return self.navigate(time, "close Castles menu", (1247, 76), "home")
        if name == "home":
            if self.restore_hero and layout == "castle":
                return self.navigate(time, "restore hero slots", EXIT_LAYER_VIEW, "home", "hero")
            if not self.restore_hero or layout == "hero":
                self.batch.closed = True
                self.save()
                self.active = False
                return True, None
        return self.wait_for_reading(time, "Tower upgrade completed 30 levels; close the menu manually.")

"""SimCode TESTBED controller.

This is NOT a starter and must not be copied into one — it deliberately plays the
whole loop so that features can be verified on a real running city:

    explore -> mine what the level asks for -> haul it to the Base -> level up

It is written to be LADDER-AGNOSTIC. The Base ladder is generated from the world
seed, so what a level wants differs per city and cannot be hardcoded: everything
below reads `base.quest.required` and reacts. That is also the behaviour we want to
exercise — a controller that assumed ore+metal would silently stall on a world that
wanted carbon.

Run by an automated testbed; the human-facing starter stays minimal on purpose.
"""

from simcode import on, robots, world, buildings

EXPLORE_HOP = 6
EXPLORE_RADIUS = 34  # how far from a charging pad we let a robot roam
CHARGE_MARGIN = 20
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]

# Raws are the only things a robot can mine directly; everything else is processed.
RAWS = ("ore", "metal", "crystal", "carbon")


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _base():
    for b in buildings.all():
        if b.type == "base":
            return b
    return None


def _quest_shortfall():
    """{item: still needed} for the CURRENT level, read from the Base — never assumed."""
    b = _base()
    if not b or not b.quest:
        return {}
    req = b.quest.required or {}
    got = b.quest.progress or {}
    return {it: n - got.get(it, 0) for it, n in req.items() if n - got.get(it, 0) > 0}


def _pads():
    out = [(0, 0)]
    for b in buildings.all():
        if b.type in ("flying_station", "charging_tower") and b.status == "active":
            out.append(tuple(b.position))
    return out


def _nearest_pad(here):
    return min(_pads(), key=lambda p: _dist(here, p))


def _stocked_sources(items):
    """Buildings holding any of `items` that a robot may pick up from.

    The Base and Flying Stations reserve their stores, so they are never sources.
    """
    out = []
    for b in buildings.all():
        if b.type in ("base", "flying_station") or b.status != "active":
            continue
        store = getattr(b, "storage", None) or getattr(b, "output", None)
        if not store:
            continue
        for it in items:
            if (store.items or {}).get(it, 0) > 0:
                out.append((tuple(b.position), it))
                break
    return out


@on.idle
def act(e):
    r = robots[e.robot_id]
    here = r.position
    pad = _nearest_pad(here)

    # 1. Stay alive. The threshold must be DISTANCE-AWARE: a flat number strands any
    #    robot further from a pad than that number, and it dies mid-flight with its
    #    cargo. Charge whenever we could not still get home with a margin to spare.
    if r.energy is not None and r.energy < _dist(here, pad) + CHARGE_MARGIN:
        if r.cell == pad:
            r.charge()
        else:
            r.move_to(*pad)
        return

    need = _quest_shortfall()

    # 2. Carrying something? Deliver it. Quest items go to the Base; anything else
    #    goes to a construction site that wants it, else to Storage.
    held = {it: n for it, n in (r.inventory.items or {}).items() if n > 0}
    if held:
        for it in held:
            if it in need:
                base = _base()
                if r.cell == tuple(base.position):
                    r.drop(it)
                else:
                    r.move_to(*base.position)
                return
        # Not quest material — feed a site that needs it, else bank it.
        for b in buildings.all():
            if b.status != "constructing" or not b.construction:
                continue
            req = b.construction.required or {}
            got = b.construction.delivered or {}
            for it in held:
                if req.get(it, 0) - got.get(it, 0) > 0:
                    if r.cell == tuple(b.position):
                        r.drop(it)
                    else:
                        r.move_to(*b.position)
                    return
        store = r.nearest(type="storage")
        if store:
            if r.cell == tuple(store):
                r.drop()
            else:
                r.move_to(*store)
            return
        r.drop()
        return

    # 3. Empty. If a mine already holds what the level wants, go collect it.
    if need:
        sources = _stocked_sources(list(need))
        if sources:
            pos, item = min(sources, key=lambda s: _dist(here, s[0]))
            if r.cell == pos:
                # ALWAYS name the item. `pick_up()` with no args takes ONE sorted-first
                # item type, so at a Storage holding {metal, ore} it grabs metal — which,
                # if the level wants ore, gets banked straight back on the next idle. That
                # is an infinite pick-up/drop loop, and it looks like a busy city.
                r.pick_up(item)
            else:
                r.move_to(*pos)
            return

    # 4. No stock yet. Fund the nearest unfinished site from Storage, so mines finish.
    for b in buildings.all():
        if b.status != "constructing" or not b.construction:
            continue
        req = b.construction.required or {}
        got = b.construction.delivered or {}
        short = {it: n - got.get(it, 0) for it, n in req.items() if n - got.get(it, 0) > 0}
        if not short:
            continue
        for pos, it in _stocked_sources(list(short)):
            if r.cell == pos:
                r.pick_up(it)
            else:
                r.move_to(*pos)
            return

    # 5. Place a mine on a spot for a raw the level wants (or ore/metal, which fund
    #    everything). Only raws can be mined; processed items come from processors.
    #
    # ONE unfinished site at a time. A site needs ore hauled into it before it starts
    # producing, and the starting capital funds about one — so placing a site on every
    # spot we can see just spreads that capital across sites that ALL stall
    # half-supplied. It looks like a busy city and produces nothing.
    unfinished = sum(1 for b in buildings.all() if b.status == "constructing")
    if unfinished >= 1:
        pass
    else:
        wanted_now = [it for it in need if it in RAWS] or ["ore", "metal"]
        for res in wanted_now:
            spot = r.nearest(kind=f"{res}_spot")
            if not spot:
                continue
            if any(tuple(b.position) == tuple(spot) for b in buildings.all()):
                continue
            world.build("mining", *spot)
            return

    wanted = [it for it in need if it in RAWS] or ["ore", "metal"]
    # 6. Nothing to do here — explore, which is how new spots are found at all.
    n = r.memory.get("hop", 0) + 1
    r.memory["hop"] = n
    dx, dy = DIRS[n % len(DIRS)]
    dest = (here[0] + dx * EXPLORE_HOP, here[1] + dy * EXPLORE_HOP)
    # Keep exploration within reach of a pad — wandering out costs the round trip and
    # the fleet is tiny.
    if _dist(dest, _nearest_pad(dest)) > EXPLORE_RADIUS:
        dest = (pad[0] + dx * EXPLORE_HOP, pad[1] + dy * EXPLORE_HOP)
    if r.energy is not None:
        trip = _dist(here, dest) + _dist(dest, _nearest_pad(dest)) + CHARGE_MARGIN
        if r.energy < trip:
            if r.cell == pad:
                r.charge()
            else:
                r.move_to(*pad)
            return
    r.move_to(*dest)


@on.base_level_up
def levelled(e):
    # log() is a ROBOT command; pick any live robot to carry the line.
    for r in robots.all():
        r.log(f"LEVEL UP -> {e.level}; unlocked={getattr(e, 'unlocks', None)}")
        return


@on.quest_updated
def quest(e):
    for r in robots.all():
        r.log(f"quest now L{e.level}: {e.requirements}")
        return

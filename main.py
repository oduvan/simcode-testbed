"""SimCode TESTBED controller.

NOT a starter — do not copy this into the templates. The starter is deliberately a
blank canvas; this deliberately plays the whole loop so features can be verified on a
real running city:

    sustain a fleet -> mine what the level asks for -> haul it to the Base -> level up

Two things drive every decision here, both learned the hard way:

1. ROBOTS EXPIRE BY DISTANCE FLOWN, and no amount of charging prevents it. A previous
   version of this file explored happily and reached tick 81,000 with ZERO robots and
   a city that could never act again. So: a Flying Station goes up early, it is kept
   stocked with ore and metal, and replacements are built BEFORE the fleet ages out.
   Flight is otherwise minimised — every unit flown is lifespan spent.

2. THE LADDER IS GENERATED FROM THE WORLD SEED, so what a level wants differs per
   city. Everything reads `base.quest.required` and reacts; nothing is hardcoded.
"""

from simcode import on, robots, world, buildings

RAWS = ("ore", "metal", "crystal", "carbon")

FLEET_TARGET = 4        # robots we try to keep alive
LIFE_LOW = 0.30         # below this fraction of lifespan, a robot is "old"
CHARGE_MARGIN = 25      # spare battery beyond the trip home
EXPLORE_HOP = 5
EXPLORE_RADIUS = 26     # never wander further than this from a pad
STATION_KEEP = {"ore": 40, "metal": 20}   # stock we try to hold for robot building

DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _of(kind):
    return [b for b in buildings.all() if b.type == kind]


def _base():
    bs = _of("base")
    return bs[0] if bs else None


def _stations():
    return [b for b in _of("flying_station") if b.status == "active"]


def _pads():
    out = [(0, 0)]
    for b in buildings.all():
        if b.type in ("flying_station", "charging_tower") and b.status == "active":
            out.append(tuple(b.position))
    return out


def _nearest_pad(p):
    return min(_pads(), key=lambda q: _dist(p, q))


def _quest_short():
    b = _base()
    if not b or not b.quest:
        return {}
    req, got = b.quest.required or {}, b.quest.progress or {}
    return {i: n - got.get(i, 0) for i, n in req.items() if n - got.get(i, 0) > 0}


def _sources(items):
    """(position, item) for buildings we may pick `items` up from.

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


def _has_income():
    """At least one ACTIVE mine. Until this is true the city has no earnings and the
    starting Storage is all the capital there is."""
    return any(b.type == "mining" and b.status == "active" for b in buildings.all())


def _sites():
    out = []
    for b in buildings.all():
        if b.status != "constructing" or not b.construction:
            continue
        req, got = b.construction.required or {}, b.construction.delivered or {}
        short = {i: n - got.get(i, 0) for i, n in req.items() if n - got.get(i, 0) > 0}
        if short:
            out.append((tuple(b.position), short))
    return out


def _free_cell_near(origin, taken):
    for rad in range(1, 8):
        for dx in range(-rad, rad + 1):
            for dy in range(-rad, rad + 1):
                if max(abs(dx), abs(dy)) != rad:
                    continue
                c = (origin[0] + dx, origin[1] + dy)
                if c not in taken:
                    return c
    return None


def _go(r, dest):
    """Fly to `dest`, but ONLY if the robot could still reach a pad afterwards.

    Every move must be budgeted for the WHOLE round trip. Checking only "can I get
    home from where I am" is not enough: it lets a robot accept a job 40 units away
    and die on the far side. When the trip is unaffordable the robot charges instead
    — the job will still be there next idle.

    Returns True if it issued a command (the caller should return).
    """
    here = r.position
    dest = (float(dest[0]), float(dest[1]))
    if r.energy is not None:
        need = _dist(here, dest) + _dist(dest, _nearest_pad(dest)) + CHARGE_MARGIN
        if r.energy < need:
            pad = _nearest_pad(here)
            if r.cell == pad:
                r.charge()
            else:
                r.move_to(*pad)
            return True
    if r.cell == (round(dest[0]), round(dest[1])):
        return False           # already there — caller should act, not move
    r.move_to(*dest)
    return True


def _keep_fleet_stocked():
    """Build replacements before the fleet ages out. Called from any idle."""
    sts = _stations()
    if not sts:
        return
    st = sts[0]
    if st.production and st.production.active:
        return
    alive = list(robots.all())
    old = sum(1 for r in alive if (r.life_remaining or 0) < LIFE_LOW * (r.life_max or 1))
    if len(alive) - old >= FLEET_TARGET:
        return
    store = (st.storage.items if st.storage else {}) or {}
    if store.get("ore", 0) >= 12 and store.get("metal", 0) >= 6:
        st.build_robot("builder", 1)


@on.idle
def act(e):
    r = robots[e.robot_id]
    here = r.position
    pad = _nearest_pad(here)
    base = _base()

    _keep_fleet_stocked()

    # 1. Stay alive. Distance-aware: a flat threshold strands anything further out.
    if r.energy is not None and r.energy < _dist(here, pad) + CHARGE_MARGIN:
        if r.cell == pad:
            r.charge()
        else:
            r.move_to(*pad)
        return

    need = _quest_short()
    held = {i: n for i, n in (r.inventory.items or {}).items() if n > 0}
    sts = _stations()

    # 2. Carrying? Deliver, in priority order.
    if held:
        # a. INFRASTRUCTURE FIRST while the city has no income. A mine costs ore, and
        #    ore is often exactly what the level asks for too — so a naive
        #    "quest items go to the Base" rule ships every scrap of ore to the Base and
        #    the mine is never funded. No income, no station, and the fleet ages out
        #    with the quest barely touched. Seen: income=False for 3000 ticks with a
        #    mine site stuck needing 15 ore and 45 ore sitting in Storage.
        if not _has_income():
            for pos, short in _sites():
                for it in held:
                    if it in short:
                        if _go(r, pos):
                            return
                        r.drop(it)
                        return

        # b. quest material -> the Base
        for it in held:
            if it in need:
                if _go(r, tuple(base.position)):
                    return
                r.drop(it)
                return
        # c. a construction site that wants it
        for pos, short in _sites():
            for it in held:
                if it in short:
                    if _go(r, pos):
                        return
                    r.drop(it)
                    return
        # d. keep the station funded — this is what keeps the fleet alive
        if sts:
            st = sts[0]
            store = (st.storage.items if st.storage else {}) or {}
            for it in ("ore", "metal"):
                if it in held and store.get(it, 0) < STATION_KEEP[it]:
                    if _go(r, tuple(st.position)):
                        return
                    r.drop(it)
                    return
        # e. bank the rest
        store_pos = r.nearest(type="storage")
        if store_pos:
            if _go(r, tuple(store_pos)):
                return
            r.drop()
            return
        r.drop()
        return

    # 3. Empty. Fund an unfinished site first — an unbuilt mine earns nothing.
    for pos, short in _sites():
        for src, it in _sources(list(short)):
            if _go(r, src):
                return
            r.pick_up(it)   # ALWAYS name the item: a bare pick_up() takes one
            return           # sorted-first type and can loop forever

    # 4. Build income BEFORE anything else spends the starting capital. Stocking the
    #    station first drains Storage into it and leaves the city with no mine at all —
    #    which is exactly what happened: the capital went into the station, no ore was
    #    ever earned, and the fleet aged out with the quest untouched.
    if not _has_income() and not _sites():
        taken = {tuple(b.position) for b in buildings.all()}
        for res in (["ore"] + [i for i in need if i in RAWS] + ["metal"]):
            spot = r.nearest(kind=f"{res}_spot")
            if spot and tuple(spot) not in taken:
                world.build("mining", *spot)
                return

    # 5. Keep the station stocked so replacements can be built — only once mines are
    #    earning, so this never eats the seed capital.
    if sts and _has_income():
        st = sts[0]
        store = (st.storage.items if st.storage else {}) or {}
        want = [i for i in ("ore", "metal") if store.get(i, 0) < STATION_KEEP[i]]
        if want:
            for src, it in _sources(want):
                if _go(r, src):
                    return
                r.pick_up(it)
                return

    # 6. Collect what the level asks for.
    if need:
        srcs = _sources(list(need))
        if srcs:
            pos, it = min(srcs, key=lambda s: _dist(here, s[0]))
            if _go(r, pos):
                return
            r.pick_up(it)
            return

    # 7. Expand. A Flying Station (cheap; both a charging pad and the only way to
    #    replace robots) once there is income to pay for it, then more mines — ONE
    #    site at a time, because spreading the capital stalls them all.
    if not _sites():
        taken = {tuple(b.position) for b in buildings.all()}
        if _has_income() and not _of("flying_station"):
            cell = _free_cell_near(tuple(base.position), taken)
            if cell:
                world.build("flying_station", *cell)
                return
        for res in ([i for i in need if i in RAWS] or []) + ["ore", "metal"]:
            spot = r.nearest(kind=f"{res}_spot")
            if spot and tuple(spot) not in taken:
                world.build("mining", *spot)
                return

    # 8. Nothing to do — explore a little, staying inside range of a pad. Every unit
    #    flown is lifespan spent, so this is the last resort, not the default.
    n = r.memory.get("hop", 0) + 1
    r.memory["hop"] = n
    dx, dy = DIRS[n % len(DIRS)]
    dest = (here[0] + dx * EXPLORE_HOP, here[1] + dy * EXPLORE_HOP)
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
    for r in robots.all():
        r.log(f"LEVEL UP -> {e.level}  unlocks={getattr(e, 'unlocks', None)}")
        return

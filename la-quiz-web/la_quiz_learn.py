"""Round-based learning engine for the LA quiz.

Pure logic with no Flask or database access, so it can be tested directly.

A round teaches a small cluster of places in three phases:

    study  -> the round's places are shown labeled on the map
    drill  -> each place is asked until answered correctly; a miss puts the
              place back in the queue two turns later and requires two
              correct answers in a row to clear it. The same place is never
              asked twice in a row: another place from the round is asked
              in between if needed
    final  -> every place in the round is asked once more; any miss sends the
              missed places back to the drill and the final check repeats

A round is complete only after a final check with no misses. Long-term
retention uses Leitner boxes: a place cleared without a miss moves up a box,
a missed place drops to box 0, and a place in box b is due for review
2**b rounds after it was last practiced. Box MASTERED_BOX or higher counts
as mastered.
"""

import math
import random

ROUND_SIZE = 8
MAX_REVIEW = 3
MASTERED_BOX = 3
MAX_BOX = 5
EXACT_RADIUS = 25  # always correct within this many native pixels
NEAR_RADIUS = 75  # correct up to here if the target is the nearest place
DRILL_RETRY_GAP = 2  # a missed place returns after this many other questions
DRILL_STREAK_GAP = 3

_COMPASS = ["east", "north-east", "north", "north-west", "west", "south-west", "south", "south-east"]


def distance(city, x, y):
    return math.hypot(city["x"] - x, city["y"] - y)


def compass(dx, dy):
    """8-way compass direction of the vector (dx, dy) in image coordinates."""
    angle = math.atan2(-dy, dx)
    return _COMPASS[round(angle / (math.pi / 4)) % 8]


def is_seen(p):
    return p is not None and p.get("last_round", -1) >= 0


def is_mastered(p):
    return is_seen(p) and p["box"] >= MASTERED_BOX


def grade(cities, target, x, y):
    """Return (correct, distance) for a tap at (x, y) when asked for target."""
    d = distance(target, x, y)
    if d <= EXACT_RADIUS:
        return True, d
    if d >= NEAR_RADIUS:
        return False, d
    nearest = min(cities, key=lambda c: distance(c, x, y))
    return nearest["id"] == target["id"], d


def landmark_hint(cities, target, progress):
    """Describe target relative to a nearby place, preferring one already learned."""
    others = sorted((c for c in cities if c["id"] != target["id"]), key=lambda c: distance(c, target["x"], target["y"]))
    if not others:
        return ""
    known = [c for c in others[:6] if is_seen(progress.get(c["id"]))]
    anchor = known[0] if known else others[0]
    d = distance(anchor, target["x"], target["y"])
    if d < 20:
        return f"right next to {anchor['name']}"
    return f"{compass(target['x'] - anchor['x'], target['y'] - anchor['y'])} of {anchor['name']}"


def miss_direction(target, x, y):
    """Where the target lies relative to the tap."""
    return compass(target["x"] - x, target["y"] - y)


def _weakness(p):
    attempts = max(p.get("attempts", 0), 1)
    return (p["box"], -p.get("misses", 0) / attempts, p.get("last_round", -1))


def choose_round_cities(cities, progress, round_no, prev_ids=(), rng=random):
    """Pick the ids of the places to teach in round round_no."""
    size = min(ROUND_SIZE, len(cities))
    by_id = {c["id"]: c for c in cities}
    seen = [c for c in cities if is_seen(progress.get(c["id"]))]
    unseen = [c for c in cities if not is_seen(progress.get(c["id"]))]

    def due(c):
        p = progress[c["id"]]
        return round_no - p["last_round"] >= 2 ** p["box"]

    reviews = sorted((c for c in seen if due(c)), key=lambda c: _weakness(progress[c["id"]]))
    picked = [c["id"] for c in reviews[: (MAX_REVIEW if unseen else size)]]

    if unseen and len(picked) < size:
        # Teach new places as a geographic cluster, continuing near the last round.
        prev = [by_id[i] for i in prev_ids if i in by_id]
        if prev:
            cx = sum(c["x"] for c in prev) / len(prev)
            cy = sum(c["y"] for c in prev) / len(prev)
            seed = min(unseen, key=lambda c: distance(c, cx, cy))
        else:
            seed = rng.choice(unseen)
        cluster = sorted(unseen, key=lambda c: distance(c, seed["x"], seed["y"]))
        picked += [c["id"] for c in cluster[: size - len(picked)]]

    if len(picked) < size:
        # Everything has been seen: fill with the weakest remaining places.
        rest = sorted((c for c in seen if c["id"] not in picked), key=lambda c: _weakness(progress[c["id"]]))
        picked += [c["id"] for c in rest[: size - len(picked)]]
    return picked


def new_round(cities, progress, round_no, prev_ids=(), rng=random):
    return {
        "round": round_no,
        "phase": "study",
        "cities": choose_round_cities(cities, progress, round_no, prev_ids, rng),
        "queue": [],
        "need": {},
        "streak": {},
        "missed": [],
        "final": [],
        "final_missed": [],
        "final_passes": 0,
        "answered": 0,
        "correct": 0,
        "last": None,
    }


def _avoid_repeat(queue, last, spacers=(), rng=random):
    """Never ask the same place twice in a row.

    When the queue holds only that place, ask one of spacers (another place
    from the round) first, so a retry tests recall rather than short-term
    memory.
    """
    if not queue or queue[0] != last:
        return
    if len(queue) > 1:
        queue[0], queue[1] = queue[1], queue[0]
        return
    others = [i for i in spacers if i != last]
    if others:
        queue.insert(0, rng.choice(others))


def _shuffled(ids, rng):
    ids = list(ids)
    rng.shuffle(ids)
    return ids


def start(state, rng=random):
    """Leave the study phase and begin the drill."""
    if state["phase"] != "study":
        return
    state["phase"] = "drill"
    state["queue"] = _shuffled(state["cities"], rng)
    state["need"] = {str(i): 1 for i in state["cities"]}
    state["streak"] = {str(i): 0 for i in state["cities"]}


def current(state):
    if state["phase"] == "drill" and state["queue"]:
        return state["queue"][0]
    if state["phase"] == "final" and state["final"]:
        return state["final"][0]
    return None


def cleared(state):
    """Places in the round not waiting in the drill queue."""
    return len(state["cities"]) - len(set(state["queue"]))


def _begin_final(state, rng):
    state["phase"] = "final"
    state["final"] = _shuffled(state["cities"], rng)
    state["final_missed"] = []
    _avoid_repeat(state["final"], state["last"])


def answer(state, correct, rng=random):
    """Record an answer to the current question and advance the round."""
    cid = current(state)
    if cid is None:
        return
    key = str(cid)
    state["answered"] += 1
    state["correct"] += int(correct)
    state["last"] = cid

    if state["phase"] == "drill":
        queue = state["queue"]
        queue.pop(0)
        if correct:
            state["streak"][key] += 1
            # A spacer (an already-cleared place) is removed by any correct answer.
            if state["streak"][key] < state["need"][key]:
                queue.insert(min(DRILL_STREAK_GAP, len(queue)), cid)
        else:
            state["streak"][key] = 0
            state["need"][key] = 2
            if cid not in state["missed"]:
                state["missed"].append(cid)
            queue.insert(min(DRILL_RETRY_GAP, len(queue)), cid)
        _avoid_repeat(queue, cid, state["cities"], rng)
        if not queue:
            _begin_final(state, rng)
    elif state["phase"] == "final":
        state["final"].pop(0)
        if not correct:
            state["final_missed"].append(cid)
            if cid not in state["missed"]:
                state["missed"].append(cid)
        if not state["final"]:
            if state["final_missed"]:
                state["final_passes"] += 1
                state["phase"] = "drill"
                state["queue"] = _shuffled(state["final_missed"], rng)
                for i in state["final_missed"]:
                    state["need"][str(i)] = 2
                    state["streak"][str(i)] = 0
                _avoid_repeat(state["queue"], cid, state["cities"], rng)
            else:
                state["phase"] = "complete"


def finish_round(state, progress):
    """Apply Leitner updates for a completed round; return a summary.

    progress maps city id -> dict with box/attempts/misses/last_round and is
    updated in place (missing entries are created).
    """
    newly_mastered = []
    for cid in state["cities"]:
        p = progress.setdefault(cid, {"box": 0, "attempts": 0, "misses": 0, "last_round": -1})
        was_mastered = is_mastered(p)
        if cid in state["missed"]:
            p["box"] = 0
        else:
            p["box"] = min(MAX_BOX, p["box"] + 1 if is_seen(p) else 1)
        p["last_round"] = state["round"]
        if is_mastered(p) and not was_mastered:
            newly_mastered.append(cid)
    return {
        "round": state["round"],
        "answered": state["answered"],
        "correct": state["correct"],
        "missed": list(state["missed"]),
        "final_passes": state["final_passes"],
        "newly_mastered": newly_mastered,
    }

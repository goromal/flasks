import random

import la_quiz_learn as learn


def grid_cities(n=30):
    return [{"id": i + 1, "name": f"P{i + 1}", "x": 60 * (i % 6), "y": 60 * (i // 6)} for i in range(n)]


def play_round(state, answers, rng):
    """Answer questions in order from answers (bools); answer True once exhausted."""
    learn.start(state, rng)
    it = iter(answers)
    asked = []
    while state["phase"] != "complete":
        asked.append(learn.current(state))
        learn.answer(state, next(it, True), rng)
        assert len(asked) < 500
    return asked


def test_first_round_is_a_geographic_cluster():
    cities = grid_cities()
    ids = learn.choose_round_cities(cities, {}, 0, rng=random.Random(0))
    assert len(ids) == learn.ROUND_SIZE
    by_id = {c["id"]: c for c in cities}
    xs = [by_id[i]["x"] for i in ids]
    ys = [by_id[i]["y"] for i in ids]
    assert max(xs) - min(xs) <= 180 and max(ys) - min(ys) <= 180


def test_perfect_round_asks_each_place_twice():
    cities = grid_cities()
    rng = random.Random(1)
    state = learn.new_round(cities, {}, 0, rng=rng)
    asked = play_round(state, [], rng)
    assert len(asked) == 2 * learn.ROUND_SIZE  # drill once + final check once
    assert sorted(asked[: learn.ROUND_SIZE]) == sorted(state["cities"])


def test_missed_place_returns_and_needs_two_in_a_row():
    cities = grid_cities()
    rng = random.Random(2)
    state = learn.new_round(cities, {}, 0, rng=rng)
    learn.start(state, rng)
    missed = learn.current(state)
    learn.answer(state, False, rng)
    assert state["queue"][learn.DRILL_RETRY_GAP] == missed
    assert learn.current(state) != missed
    asks = 0
    while state["phase"] == "drill":
        if learn.current(state) == missed:
            asks += 1
        learn.answer(state, True, rng)
    assert asks == 2


def test_final_check_miss_repeats_until_clean():
    cities = grid_cities()
    rng = random.Random(3)
    state = learn.new_round(cities, {}, 0, rng=rng)
    learn.start(state, rng)
    while state["phase"] == "drill":
        learn.answer(state, True, rng)
    assert state["phase"] == "final"
    learn.answer(state, False, rng)  # miss the first final question
    while state["phase"] == "final":
        learn.answer(state, True, rng)
    assert state["phase"] == "drill" and state["final_passes"] == 1
    while state["phase"] != "complete":
        learn.answer(state, True, rng)
    assert state["final_passes"] == 1


def test_never_asks_same_place_twice_in_a_row_when_avoidable():
    cities = grid_cities()
    rng = random.Random(4)
    state = learn.new_round(cities, {}, 0, rng=rng)
    asked = play_round(state, [i % 3 != 0 for i in range(200)], rng)
    assert all(a != b for a, b in zip(asked, asked[1:]))


def test_leitner_boxes_and_review_scheduling():
    cities = grid_cities()
    rng = random.Random(5)
    progress = {}
    state = learn.new_round(cities, progress, 0, rng=rng)
    learn.start(state, rng)
    missed = learn.current(state)
    learn.answer(state, False, rng)
    while state["phase"] != "complete":
        learn.answer(state, True, rng)
    learn.finish_round(state, progress)
    assert progress[missed]["box"] == 0
    assert all(progress[i]["box"] == 1 for i in state["cities"] if i != missed)
    # The missed place (box 0) is due again in the very next round.
    nxt = learn.choose_round_cities(cities, progress, 1, state["cities"], rng)
    assert missed in nxt
    assert sum(1 for i in nxt if i in progress) <= learn.MAX_REVIEW


def test_mastery_is_reached_and_everything_is_taught():
    cities = grid_cities(20)
    rng = random.Random(6)
    progress = {}
    state = learn.new_round(cities, progress, 0, rng=rng)
    for _ in range(40):
        play_round(state, [], rng)
        learn.finish_round(state, progress)
        state = learn.new_round(cities, progress, state["round"] + 1, state["cities"], rng)
    assert all(learn.is_seen(progress.get(c["id"])) for c in cities)
    assert all(learn.is_mastered(progress[c["id"]]) for c in cities)


def test_grade_uses_nearest_place_inside_near_radius():
    a = {"id": 1, "name": "A", "x": 100, "y": 100}
    b = {"id": 2, "name": "B", "x": 150, "y": 100}
    cities = [a, b]
    assert learn.grade(cities, a, 110, 100)[0]  # exact radius
    assert learn.grade(cities, a, 70, 100)[0]  # 30 px off, but A is nearest
    assert not learn.grade(cities, a, 140, 100)[0]  # nearer to B
    assert not learn.grade(cities, a, 100, 200)[0]  # beyond near radius


def test_hints_and_directions():
    a = {"id": 1, "name": "A", "x": 100, "y": 100}
    b = {"id": 2, "name": "B", "x": 200, "y": 100}
    assert learn.landmark_hint([a, b], a, {}) == "west of B"
    assert learn.miss_direction(a, 100, 200) == "north"
    assert learn.compass(10, 10) == "south-east"

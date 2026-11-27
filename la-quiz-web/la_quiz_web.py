import os
import argparse
import datetime
import json
import random
import sqlite3
import uuid
import flask
import la_quiz_learn as learn

parser = argparse.ArgumentParser()
parser.add_argument("--port", action="store", type=int, default=5000, help="Port to run the server on")
parser.add_argument("--subdomain", action="store", type=str, default="/la-quiz", help="Subdomain for a reverse proxy")
parser.add_argument("--db-path", action="store", type=str, default="", help="Path to SQLite database")
parser.add_argument("--maps-dir", action="store", type=str, default="", help="Directory containing map images")
args = parser.parse_args()

urlroot = args.subdomain
if urlroot != "/":
    urlroot += "/"
url_for_prefix = args.subdomain.replace("/", "").replace("-", "_")
if len(url_for_prefix) > 0:
    url_for_prefix += "."

bp = flask.Blueprint("la_quiz", __name__, url_prefix=args.subdomain)

PWD = os.getcwd()
if args.db_path and args.db_path[0] == '/':
    DB_PATH = args.db_path
else:
    DB_PATH = os.path.join(PWD, args.db_path if args.db_path else "la_quiz.db")

if args.maps_dir and args.maps_dir[0] == '/':
    MAPS_DIR = args.maps_dir
else:
    MAPS_DIR = os.path.join(PWD, args.maps_dir if args.maps_dir else "maps")

app = flask.Flask(__name__, static_url_path=args.subdomain + "/static", static_folder=MAPS_DIR)
app.secret_key = b"la_quiz_secret_key_change_in_production"
app.permanent_session_lifetime = datetime.timedelta(days=3650)

def get_db():
    """Get database connection."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    """Initialize database schema."""
    db = get_db()
    db.execute('''
        CREATE TABLE IF NOT EXISTS regions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            map_image TEXT NOT NULL,
            map_width INTEGER NOT NULL,
            map_height INTEGER NOT NULL
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS cities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            region_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            x INTEGER NOT NULL,
            y INTEGER NOT NULL,
            FOREIGN KEY (region_id) REFERENCES regions (id)
        )
    ''')
    # Per-player learning progress for each place (Leitner box and counts).
    db.execute('''
        CREATE TABLE IF NOT EXISTS progress (
            player TEXT NOT NULL,
            city_id INTEGER NOT NULL,
            box INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            misses INTEGER NOT NULL DEFAULT 0,
            last_round INTEGER NOT NULL DEFAULT -1,
            PRIMARY KEY (player, city_id)
        )
    ''')
    # Per-player state of the round in progress for each region (JSON).
    db.execute('''
        CREATE TABLE IF NOT EXISTS round_state (
            player TEXT NOT NULL,
            region_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            PRIMARY KEY (player, region_id)
        )
    ''')
    db.commit()
    db.close()

# Initialize DB on startup
init_db()

def get_regions():
    """Get all regions."""
    db = get_db()
    regions = db.execute('SELECT * FROM regions ORDER BY name').fetchall()
    db.close()
    return [dict(r) for r in regions]

def get_cities_by_region(region_id):
    """Get all cities for a region."""
    db = get_db()
    cities = db.execute('SELECT * FROM cities WHERE region_id = ? ORDER BY id', (region_id,)).fetchall()
    db.close()
    return [dict(c) for c in cities]

def get_region_by_id(region_id):
    """Get region by ID."""
    db = get_db()
    region = db.execute('SELECT * FROM regions WHERE id = ?', (region_id,)).fetchone()
    db.close()
    return dict(region) if region else None

def player_id():
    """Stable anonymous id for this browser, kept in a long-lived session cookie."""
    flask.session.permanent = True
    if 'player' not in flask.session:
        flask.session['player'] = uuid.uuid4().hex
    return flask.session['player']

def load_progress(player, cities):
    db = get_db()
    rows = db.execute(
        'SELECT city_id, box, attempts, misses, last_round FROM progress WHERE player = ?', (player,)
    ).fetchall()
    db.close()
    ids = {c['id'] for c in cities}
    return {r['city_id']: dict(r) for r in rows if r['city_id'] in ids}

def save_progress(player, progress, city_ids):
    db = get_db()
    for cid in city_ids:
        p = progress[cid]
        db.execute(
            'INSERT OR REPLACE INTO progress (player, city_id, box, attempts, misses, last_round) VALUES (?, ?, ?, ?, ?, ?)',
            (player, cid, p['box'], p['attempts'], p['misses'], p['last_round'])
        )
    db.commit()
    db.close()

def load_state(player, region_id, cities, progress):
    """Load the round in progress, starting a new one if needed."""
    db = get_db()
    row = db.execute(
        'SELECT state FROM round_state WHERE player = ? AND region_id = ?', (player, region_id)
    ).fetchone()
    db.close()
    ids = {c['id'] for c in cities}
    state = json.loads(row['state']) if row else None
    if state is not None and not set(state['cities']) <= ids:
        # A place in this round was deleted; restart the round.
        state = learn.new_round(cities, progress, state['round'])
        save_state(player, region_id, state)
    if state is None:
        state = learn.new_round(cities, progress, 0)
        save_state(player, region_id, state)
    return state

def save_state(player, region_id, state):
    db = get_db()
    db.execute(
        'INSERT OR REPLACE INTO round_state (player, region_id, state) VALUES (?, ?, ?)',
        (player, region_id, json.dumps(state))
    )
    db.commit()
    db.close()

def view(state, cities, progress):
    """The client-facing summary of the round and overall progress."""
    by_id = {c['id']: c for c in cities}
    cid = learn.current(state)
    return {
        'phase': state['phase'],
        'round': state['round'] + 1,
        'round_cities': state['cities'],
        'review_ids': [i for i in state['cities'] if learn.is_seen(progress.get(i))],
        'round_size': len(state['cities']),
        'cleared': learn.cleared(state),
        'final_left': len(state['final']),
        'final_passes': state['final_passes'],
        'prompt': by_id[cid] if cid is not None else None,
        'mastered': sum(1 for c in cities if learn.is_mastered(progress.get(c['id']))),
        'seen': sum(1 for c in cities if learn.is_seen(progress.get(c['id']))),
        'total': len(cities),
    }

def region_context(region_id):
    cities = get_cities_by_region(region_id)
    player = player_id()
    progress = load_progress(player, cities)
    state = load_state(player, region_id, cities, progress) if cities else None
    return player, cities, progress, state

@bp.route("/")
def index():
    """Main page - region selection."""
    # Initialize debug mode if not set
    if 'debug_mode' not in flask.session:
        flask.session['debug_mode'] = False

    player = player_id()
    regions = get_regions()
    for region in regions:
        cities = get_cities_by_region(region['id'])
        progress = load_progress(player, cities)
        region['total'] = len(cities)
        region['mastered'] = sum(1 for c in cities if learn.is_mastered(progress.get(c['id'])))
    return flask.render_template(
        "index.html",
        urlroot=urlroot,
        regions=regions,
        debug_mode=flask.session.get('debug_mode', False)
    )

@bp.route("/quiz/<int:region_id>")
def quiz(region_id):
    """Quiz page for a specific region."""
    region = get_region_by_id(region_id)
    if not region:
        flask.flash("Region not found")
        return flask.redirect(flask.url_for(url_for_prefix + 'index'))

    # Initialize debug mode if not set
    if 'debug_mode' not in flask.session:
        flask.session['debug_mode'] = False

    player, cities, progress, state = region_context(region_id)

    # If no cities and not in debug mode, redirect with message
    if not cities and not flask.session.get('debug_mode', False):
        flask.flash("No cities found for this region. Enable debug mode to add cities.")
        return flask.redirect(flask.url_for(url_for_prefix + 'index'))

    # If no cities but in debug mode, show setup page
    if not cities:
        return flask.render_template(
            "setup_region.html",
            urlroot=urlroot,
            region=region,
            debug_mode=True
        )

    return flask.render_template(
        "quiz.html",
        urlroot=urlroot,
        region=region,
        cities=cities,
        initial_view=view(state, cities, progress),
        debug_mode=flask.session.get('debug_mode', False)
    )

@bp.route("/start/<int:region_id>", methods=["POST"])
def start_round(region_id):
    """Finish studying and start answering questions for the round."""
    player, cities, progress, state = region_context(region_id)
    if state is None:
        return flask.jsonify({"error": "No cities in this region"}), 400
    learn.start(state)
    save_state(player, region_id, state)
    return flask.jsonify({"view": view(state, cities, progress)})

@bp.route("/check/<int:region_id>", methods=["POST"])
def check_guess(region_id):
    """Grade a tap on the map for the current question."""
    data = flask.request.json or {}
    click_x = data.get('x')
    click_y = data.get('y')
    if click_x is None or click_y is None:
        return flask.jsonify({"error": "Invalid coordinates"}), 400

    player, cities, progress, state = region_context(region_id)
    cid = learn.current(state) if state else None
    if cid is None:
        return flask.jsonify({"error": "No question is waiting for an answer"}), 409
    target = next(c for c in cities if c['id'] == cid)

    is_correct, distance = learn.grade(cities, target, click_x, click_y)
    hint = learn.landmark_hint(cities, target, progress)
    p = progress.setdefault(cid, {'box': 0, 'attempts': 0, 'misses': 0, 'last_round': -1})
    p['attempts'] += 1
    p['misses'] += 0 if is_correct else 1

    phase = state['phase']
    learn.answer(state, is_correct)
    summary = None
    if state['phase'] == 'complete':
        summary = learn.finish_round(state, progress)
        by_id = {c['id']: c['name'] for c in cities}
        summary['missed'] = [by_id[i] for i in summary['missed']]
        summary['newly_mastered'] = [by_id[i] for i in summary['newly_mastered']]
        save_progress(player, progress, set(state['cities']) | {cid})
        state = learn.new_round(cities, progress, state['round'] + 1, state['cities'])
    else:
        save_progress(player, progress, [cid])
    save_state(player, region_id, state)

    return flask.jsonify({
        "correct": is_correct,
        "phase": phase,
        "distance": round(distance, 1),
        "city": target,
        "direction": learn.miss_direction(target, click_x, click_y),
        "hint": hint,
        "summary": summary,
        "view": view(state, cities, progress),
    })

@bp.route("/reset/<int:region_id>", methods=["POST"])
def reset_quiz(region_id):
    """Forget all learning progress for a region."""
    player = player_id()
    ids = [c['id'] for c in get_cities_by_region(region_id)]
    db = get_db()
    db.executemany('DELETE FROM progress WHERE player = ? AND city_id = ?', [(player, i) for i in ids])
    db.execute('DELETE FROM round_state WHERE player = ? AND region_id = ?', (player, region_id))
    db.commit()
    db.close()
    return flask.jsonify({"status": "reset"})

@bp.route("/skip/<int:region_id>", methods=["POST"])
def skip_city(region_id):
    """Move the current question to the back of the queue (debug mode only)."""
    if not flask.session.get('debug_mode', False):
        return flask.jsonify({"error": "Debug mode not enabled"}), 403
    player, cities, progress, state = region_context(region_id)
    queue = state['queue'] if state['phase'] == 'drill' else state['final'] if state['phase'] == 'final' else []
    if len(queue) > 1:
        queue.append(queue.pop(0))
        save_state(player, region_id, state)
    return flask.jsonify({"view": view(state, cities, progress)})

@bp.route("/toggle-debug", methods=["POST"])
def toggle_debug():
    """Toggle debug mode."""
    flask.session['debug_mode'] = not flask.session.get('debug_mode', False)
    return flask.jsonify({"debug_mode": flask.session['debug_mode']})

@bp.route("/update-coordinates/<int:city_id>", methods=["POST"])
def update_coordinates(city_id):
    """Update coordinates for a city (debug mode only)."""
    if not flask.session.get('debug_mode', False):
        return flask.jsonify({"error": "Debug mode not enabled"}), 403

    data = flask.request.json
    x = data.get('x')
    y = data.get('y')

    if x is None or y is None:
        return flask.jsonify({"error": "Invalid coordinates"}), 400

    db = get_db()
    db.execute('UPDATE cities SET x = ?, y = ? WHERE id = ?', (x, y, city_id))
    db.commit()
    db.close()

    return flask.jsonify({"status": "updated", "city_id": city_id, "x": x, "y": y})

@bp.route("/add-city/<int:region_id>", methods=["POST"])
def add_city(region_id):
    """Add a new city to a region (debug mode only)."""
    if not flask.session.get('debug_mode', False):
        return flask.jsonify({"error": "Debug mode not enabled"}), 403

    data = flask.request.json
    name = data.get('name')
    x = data.get('x')
    y = data.get('y')

    if not name or x is None or y is None:
        return flask.jsonify({"error": "Missing required fields"}), 400

    db = get_db()
    cursor = db.execute(
        'INSERT INTO cities (region_id, name, x, y) VALUES (?, ?, ?, ?)',
        (region_id, name, x, y)
    )
    city_id = cursor.lastrowid
    db.commit()
    db.close()

    return flask.jsonify({
        "status": "added",
        "city_id": city_id,
        "name": name,
        "x": x,
        "y": y
    })

@bp.route("/cities/<int:region_id>", methods=["GET"])
def list_cities(region_id):
    """Get all cities for a region (for debug mode city management)."""
    if not flask.session.get('debug_mode', False):
        return flask.jsonify({"error": "Debug mode not enabled"}), 403

    cities = get_cities_by_region(region_id)
    return flask.jsonify({"cities": cities})

def run():
    global args
    app.register_blueprint(bp)
    app.run(host="0.0.0.0", port=args.port)

if __name__ == "__main__":
    run()

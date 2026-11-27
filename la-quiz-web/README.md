# LA Quiz Web

A mobile-friendly web-based version of the LA Geography Quiz game.

## Features

- **Mobile-Responsive**: Touch-friendly interface optimized for mobile devices
- **SQLite Database**: Stores city locations in a database instead of JSON files
- **Multiple Regions**: Support for North, Central, East, and South LA
- **Progress Tracking**: Per-place mastery that persists across visits
- **Learning rounds**: Study, practice and a no-miss final check, with spaced review of missed places

## Installation

The application is packaged with Nix. Add it to your system configuration:

```nix
{
  services.la-quiz-web = {
    enable = true;
    dataDir = "/data/andrew/la-quiz-web";  # Optional: customize data directory
    port = 5050;  # Optional: customize port
    subdomain = "/la-quiz";  # Optional: customize subdomain
  };
}
```

## Setup

### 1. Initialize the Database

First, run the migration script to populate the database from JSON files:

```bash
# Create data directories
mkdir -p /data/andrew/la-quiz-web/maps

# Run migration
python migrate_json_to_db.py \
  --db-path /data/andrew/la-quiz-web/la_quiz.db \
  --json-dir ~/games/la-quiz \
  --maps-dir /data/andrew/la-quiz-web/maps \
  --images-dir ~/dev/packages/sources/anixdata/data/apps/la-quiz
```

### 2. Start the Server

If using the NixOS module, the service will start automatically. Otherwise:

```bash
la-quiz-web --port 5050 --subdomain /la-quiz \
  --db-path /data/andrew/la-quiz-web/la_quiz.db \
  --maps-dir /data/andrew/la-quiz-web/maps
```

### 3. Access the Application

Navigate to `http://localhost:5050/la-quiz/` (or your configured subdomain).

## Usage

### How the game teaches

The quiz is built to teach, not just test. Each region is learned a few places at a time in **rounds**:

1. **Study**: the round's places (8, or fewer near the end) are shown labeled on a zoomed-in map. New places are picked as a geographic cluster that continues from the previous round, so each round builds on neighbors you already know. Places due for review are mixed in and shown in orange.
2. **Practice**: you are asked each place until you get it right. A miss shows the correct spot (with the direction and a nearby known landmark), puts the place back in the queue two questions later, and requires two correct answers in a row to clear it.
3. **Final check**: every place in the round is asked once more. Any miss sends those places back to practice, then the final check repeats. The round is complete only after a final check with no misses.

While you answer, place-name text on the map is blurred so you can't just read the answer; roads, coastline and terrain stay visible as cues. The **Labels** button turns this off.

Progress is kept per browser (an anonymous long-lived cookie) in the database, using Leitner boxes: a place cleared without a miss moves up a box, a missed place drops to box 0, and a place in box *b* comes back for review 2^*b* rounds after it was last practiced. A place in box 3 or higher counts as **mastered**; the region cards show mastered/total.

A tap counts as correct within 25 px of the place, or within 75 px if the target is also the nearest place to the tap.

Feedback is shown above the map, never on top of it, so the correct location is always visible.

### Debug Mode

Debug mode allows you to update city coordinates and add new cities directly from the web interface:

1. Click the **Debug Mode** button to enable debug mode
2. When enabled, you'll see a debug panel with:
   - Current city information (name, ID, coordinates)
   - Last click coordinates
   - Options to set coordinates or add new cities

**Setting Coordinates:**
1. Click "Set Coordinates" button
2. Click on the map where the city should be located
3. Confirm the new coordinates
4. The database will be updated immediately

**Adding New Cities:**
1. Click "Add New City" button
2. Enter the city name in the form
3. Click on the map to set the coordinates
4. Click "Save City" to add it to the database
5. The page will reload to include the new city in the quiz

Debug mode is session-based, so it persists only for your current browser session.

## Architecture

### Database Schema

**regions table:**
- `id`: Primary key
- `name`: Region name (e.g., "North LA")
- `map_image`: Filename of the map image
- `map_width`: Width of the map image
- `map_height`: Height of the map image

**cities table:**
- `id`: Primary key
- `region_id`: Foreign key to regions table
- `name`: City name
- `x`: X coordinate on the map
- `y`: Y coordinate on the map

**progress table** (per player and place): `box`, `attempts`, `misses`, `last_round`.

**round_state table** (per player and region): the round in progress, as JSON.

### Files

- `la_quiz_web.py`: Flask application, routes and persistence
- `la_quiz_learn.py`: round, drill and spaced-review logic (no Flask or database)
- `templates/index.html`: Region selection page
- `templates/quiz.html`: Quiz interface with interactive map
- `migrate_json_to_db.py`: Migration utility to import JSON data
- `module.nix`: NixOS service module
- `default.nix`: Nix package definition

## Development

### Running Locally

```bash
# Install dependencies
pip install flask pillow

# Create test database
python migrate_json_to_db.py \
  --db-path ./test.db \
  --json-dir ~/games/la-quiz \
  --maps-dir ./test-maps \
  --images-dir ~/dev/packages/sources/anixdata/data/apps/la-quiz

# Run server
python la_quiz_web.py --port 5000 --subdomain / --db-path ./test.db --maps-dir ./test-maps
```

### Mobile Testing

The application is designed to work on mobile devices. Test with:
- Chrome DevTools mobile emulation
- Real mobile devices on the same network
- Different screen sizes and orientations

## Differences from Original

The web version differs from the original Tkinter application in several ways:

- **Database vs JSON**: Uses SQLite instead of JSON files for better scalability
- **Web-based**: Accessible from any device with a browser
- **Mobile-first**: Touch-optimized interface
- **Learning rounds**: Spaced, round-based practice instead of a plain score
- **Enhanced Debug Mode**: Toggle-able debug mode with in-browser coordinate editing and city addition (vs. command-line only in original)

## Future Enhancements

Potential improvements:
- User accounts and persistent high scores
- Leaderboards
- Difficulty levels (varying proximity requirements)
- Additional regions
- Time-based challenges
- Multiplayer mode

"""Build a self-contained folio.db fixture for Tester's folio-source tests.

Embeds the subset of the folio schema (sources/folio/backend/.../schema.sql)
that subsystem E reads — no FTS/triggers/passage_links needed for reads.
"""
import sqlite3

_DDL = """
CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT,
    source_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE chapters (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL,
    title TEXT NOT NULL, order_idx INTEGER NOT NULL, parent_id INTEGER);
CREATE TABLE blocks (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL,
    chapter_id INTEGER, order_idx INTEGER NOT NULL, type TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE passages (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL,
    start_block INTEGER NOT NULL, start_off INTEGER NOT NULL,
    end_block INTEGER NOT NULL, end_off INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE highlights (id INTEGER PRIMARY KEY, passage_id INTEGER NOT NULL,
    color TEXT NOT NULL DEFAULT 'yellow');
CREATE TABLE notes (id INTEGER PRIMARY KEY, passage_id INTEGER, chapter_id INTEGER,
    book_id INTEGER, body TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE passage_tags (passage_id INTEGER NOT NULL, tag_id INTEGER NOT NULL,
    PRIMARY KEY (passage_id, tag_id));
CREATE TABLE summaries (id INTEGER PRIMARY KEY, scope TEXT NOT NULL,
    scope_id INTEGER NOT NULL, body TEXT NOT NULL,
    generated_by TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL);
"""

_T = "2026-07-31 00:00:00 UTC"


def build_fixture_db(path):
    """Create a folio.db at `path` with one book, two chapters, blocks,
    two passages (one single-block, one multi-block), highlights, notes at
    each scope, tags, and book+chapter summaries."""
    conn = sqlite3.connect(path)
    conn.executescript(_DDL)
    # Book 1, chapters 10 & 11
    conn.execute("INSERT INTO books VALUES (1,'The Test Book','A. Author','h1',?)", (_T,))
    conn.execute("INSERT INTO chapters VALUES (10,1,'Chapter One',0,NULL)")
    conn.execute("INSERT INTO chapters VALUES (11,1,'Chapter Two',1,NULL)")
    # Blocks: ch10 -> order 0,1 ; ch11 -> order 2,3
    conn.execute("INSERT INTO blocks VALUES (100,1,10,0,'p','Alpha beta gamma delta.')")
    conn.execute("INSERT INTO blocks VALUES (101,1,10,1,'p','Second block here.')")
    conn.execute("INSERT INTO blocks VALUES (102,1,11,2,'p','Third block words.')")
    conn.execute("INSERT INTO blocks VALUES (103,1,11,3,'p','Fourth and last.')")
    # Passage 1000: single-block, block 100 offsets 6..14 -> 'beta gam'
    conn.execute("INSERT INTO passages VALUES (1000,1,100,6,100,14,?)", (_T,))
    # Passage 1001: multi-block, block 101 off 7 .. block 102 off 5 (ch10->ch11 span)
    conn.execute("INSERT INTO passages VALUES (1001,1,101,7,102,5,?)", (_T,))
    conn.execute("INSERT INTO highlights VALUES (500,1000,'yellow')")
    conn.execute("INSERT INTO highlights VALUES (501,1001,'green')")
    # Notes: book-level, chapter-level (ch10), passage-level (on highlighted 1000)
    conn.execute("INSERT INTO notes VALUES (1,NULL,NULL,1,'Book note.',?,?)", (_T, _T))
    conn.execute("INSERT INTO notes VALUES (2,NULL,10,NULL,'Chapter one note.',?,?)", (_T, _T))
    conn.execute("INSERT INTO notes VALUES (3,1000,NULL,NULL,'Passage note.',?,?)", (_T, _T))
    # Tags on passage 1000
    conn.execute("INSERT INTO tags VALUES (7,'theme')")
    conn.execute("INSERT INTO passage_tags VALUES (1000,7)")
    # Summaries: book + chapter(10)
    conn.execute("INSERT INTO summaries VALUES (1,'book',1,'Book summary.','user',?)", (_T,))
    conn.execute("INSERT INTO summaries VALUES (2,'chapter',10,'Ch1 summary.','llm',?)", (_T,))
    conn.commit()
    conn.close()

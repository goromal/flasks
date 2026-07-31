"""Read study materials from a folio.db (read-only) for exam generation.

folio (sources/folio) is a separate app; its SQLite schema is the contract
(see sources/folio/backend/folio_backend/schema.sql). This module never
writes and depends only on the stdlib, so it is trivially testable against a
fixture DB. It knows nothing about Flask or wormhole.
"""
import sqlite3


def open_ro(db_path):
    """Open a folio.db read-only (URI mode). Caller closes the connection."""
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_books(conn):
    """[{id, title, author}] ordered by title."""
    rows = conn.execute(
        "SELECT id, title, author FROM books ORDER BY title").fetchall()
    return [{"id": r["id"], "title": r["title"], "author": r["author"]} for r in rows]


def list_chapters(conn, book_id):
    """[{id, title, order_idx, parent_id}] for a book, ordered by order_idx."""
    rows = conn.execute(
        "SELECT id, title, order_idx, parent_id FROM chapters "
        "WHERE book_id = ? ORDER BY order_idx", (book_id,)).fetchall()
    return [dict(r) for r in rows]


def resolve_passage_text(conn, passage):
    """Quoted text for a passage's (start_block,start_off)->(end_block,end_off).

    Blocks flow in `order_idx` order; the passage spans from its start block to
    its end block inclusive. The first block is sliced from start_off, the last
    block to end_off, middle blocks kept whole, joined by blank lines. Returns
    "" if the anchor blocks are missing.
    """
    start = conn.execute("SELECT book_id, order_idx FROM blocks WHERE id = ?",
                         (passage["start_block"],)).fetchone()
    end = conn.execute("SELECT order_idx FROM blocks WHERE id = ?",
                       (passage["end_block"],)).fetchone()
    if start is None or end is None:
        return ""
    blocks = conn.execute(
        "SELECT text FROM blocks WHERE book_id = ? AND order_idx BETWEEN ? AND ? "
        "ORDER BY order_idx",
        (start["book_id"], start["order_idx"], end["order_idx"])).fetchall()
    if not blocks:
        return ""
    if len(blocks) == 1:
        return blocks[0]["text"][passage["start_off"]:passage["end_off"]]
    parts = [blocks[0]["text"][passage["start_off"]:]]
    parts.extend(b["text"] for b in blocks[1:-1])
    parts.append(blocks[-1]["text"][:passage["end_off"]])
    return "\n\n".join(parts)


def _highlighted_passage_ids(conn, book_id, chapter_ids):
    """IDs of passages that have >=1 highlight, scoped to chapters by the
    passage's START block chapter when chapter_ids is given."""
    if chapter_ids:
        qs = ",".join("?" * len(chapter_ids))
        rows = conn.execute(
            "SELECT DISTINCT p.id FROM passages p "
            "JOIN highlights h ON h.passage_id = p.id "
            "JOIN blocks b ON b.id = p.start_block "
            "WHERE p.book_id = ? AND b.chapter_id IN (%s)" % qs,
            (book_id, *chapter_ids)).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT p.id FROM passages p "
            "JOIN highlights h ON h.passage_id = p.id WHERE p.book_id = ?",
            (book_id,)).fetchall()
    return [r["id"] for r in rows]


def _passage_tags(conn, passage_id):
    rows = conn.execute(
        "SELECT t.name FROM passage_tags pt JOIN tags t ON t.id = pt.tag_id "
        "WHERE pt.passage_id = ? ORDER BY t.name", (passage_id,)).fetchall()
    return [r["name"] for r in rows]


def gather_study_materials(conn, book_id, chapter_ids=None, include_block_text=False):
    """Study materials for a book, optionally scoped to chapters.

    Returns {"book", "summaries", "notes", "passages", "block_text"}. Whole
    book when chapter_ids is falsy; otherwise scoped to those chapters, with
    book-level summaries/notes excluded. "passages" are the highlighted ones
    (each with resolved text, tags, inline note); other notes go in "notes".
    """
    chapter_ids = list(chapter_ids or [])
    scoped = bool(chapter_ids)
    qs = ",".join("?" * len(chapter_ids)) if scoped else ""

    row = conn.execute("SELECT id, title, author FROM books WHERE id = ?",
                       (book_id,)).fetchone()
    book = dict(row) if row else {"id": book_id, "title": "", "author": None}

    # Summaries
    if scoped:
        summaries = conn.execute(
            "SELECT scope, scope_id, body, generated_by FROM summaries "
            "WHERE scope = 'chapter' AND scope_id IN (%s) ORDER BY id" % qs,
            tuple(chapter_ids)).fetchall()
    else:
        summaries = conn.execute(
            "SELECT scope, scope_id, body, generated_by FROM summaries "
            "WHERE (scope='book' AND scope_id=?) OR (scope='chapter' AND scope_id IN "
            "(SELECT id FROM chapters WHERE book_id=?)) ORDER BY id",
            (book_id, book_id)).fetchall()

    hl_ids = _highlighted_passage_ids(conn, book_id, chapter_ids)

    # Passages: the highlighted ones, with resolved text + tags + inline note.
    passages = []
    for pid in hl_ids:
        prow = conn.execute("SELECT * FROM passages WHERE id = ?", (pid,)).fetchone()
        hrow = conn.execute(
            "SELECT color FROM highlights WHERE passage_id = ? LIMIT 1", (pid,)).fetchone()
        nrow = conn.execute(
            "SELECT body FROM notes WHERE passage_id = ? ORDER BY id LIMIT 1", (pid,)).fetchone()
        passages.append({
            "id": pid,
            "text": resolve_passage_text(conn, prow),
            "tags": _passage_tags(conn, pid),
            "note": nrow["body"] if nrow else None,
            "color": hrow["color"] if hrow else "yellow",
        })
    passages.sort(key=lambda p: p["id"])

    # Notes: everything in scope EXCEPT notes shown inline on a highlighted passage.
    hl_set = set(hl_ids)
    if scoped:
        note_rows = conn.execute(
            "SELECT id, passage_id, chapter_id, book_id, body FROM notes "
            "WHERE chapter_id IN (%s) OR passage_id IN "
            "(SELECT p.id FROM passages p JOIN blocks b ON b.id=p.start_block "
            " WHERE p.book_id=? AND b.chapter_id IN (%s)) ORDER BY id" % (qs, qs),
            (*chapter_ids, book_id, *chapter_ids)).fetchall()
    else:
        note_rows = conn.execute(
            "SELECT id, passage_id, chapter_id, book_id, body FROM notes "
            "WHERE book_id=? OR chapter_id IN (SELECT id FROM chapters WHERE book_id=?) "
            "OR passage_id IN (SELECT id FROM passages WHERE book_id=?) ORDER BY id",
            (book_id, book_id, book_id)).fetchall()
    notes = [dict(n) for n in note_rows if n["passage_id"] not in hl_set]

    # Optional raw block text.
    block_text = None
    if include_block_text:
        if scoped:
            brows = conn.execute(
                "SELECT text FROM blocks WHERE book_id=? AND chapter_id IN (%s) "
                "ORDER BY order_idx" % qs, (book_id, *chapter_ids)).fetchall()
        else:
            brows = conn.execute(
                "SELECT text FROM blocks WHERE book_id=? ORDER BY order_idx",
                (book_id,)).fetchall()
        block_text = "\n\n".join(b["text"] for b in brows)

    return {"book": book, "summaries": [dict(s) for s in summaries],
            "notes": notes, "passages": passages, "block_text": block_text}


def compose_source_texts(materials):
    """Serialize gathered materials into the exam generator's list[str]. One
    labeled entry per non-empty category; empty categories are omitted."""
    book = materials["book"]
    out = []
    if materials["summaries"]:
        lines = ["[Book: %s — Summaries]" % book["title"]]
        for s in materials["summaries"]:
            lines.append("- (%s %s): %s" % (s["scope"], s["scope_id"], s["body"]))
        out.append("\n".join(lines))
    if materials["notes"]:
        lines = ["[Notes]"]
        for n in materials["notes"]:
            lines.append("- %s" % n["body"])
        out.append("\n".join(lines))
    if materials["passages"]:
        lines = ["[Highlighted passages]"]
        for p in materials["passages"]:
            tag_s = " _(tags: %s)_" % ", ".join(p["tags"]) if p["tags"] else ""
            note_s = " — note: %s" % p["note"] if p["note"] else ""
            lines.append("- \"%s\"%s%s" % (p["text"], tag_s, note_s))
        out.append("\n".join(lines))
    if materials["block_text"]:
        out.append("[Chapter text]\n%s" % materials["block_text"])
    return out

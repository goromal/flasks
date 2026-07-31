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

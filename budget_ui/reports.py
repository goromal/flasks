"""Index and serve spending reports dropped into a shared directory.

The budget agent writes each report as `<id>.html` plus an `<id>.json` sidecar
(title, request, headline, from/to, created, totals, tags). Only reports with
both files are listed; the sidecar is the index entry.
"""
import json
import os
import re

from flask import Blueprint, abort, jsonify, request, send_file

REPORT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")

# Reports are self-contained pages with inline scripts (the animated Sankey).
# Sandboxing gives them an opaque origin, so their scripts can't use this app's
# cookies or endpoints, and `default-src 'none'` keeps them from loading or
# sending anything off the page.
REPORT_CSP = ("sandbox allow-scripts; default-src 'none'; "
              "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:")

LISTED_FIELDS = ("id", "title", "request", "headline", "from", "to", "created", "tags", "totals")


def fuzzy_match(query, text):
    """True if query's characters appear in order within text (case-insensitive)."""
    query = query.lower()
    if not query:
        return True
    it = iter(text.lower())
    return all(ch in it for ch in query)


def report_paths(reports_dir, report_id):
    if not REPORT_ID_RE.match(report_id or ""):
        raise ValueError("invalid report id")
    base = os.path.join(reports_dir, report_id)
    return base + ".html", base + ".json"


def list_reports(reports_dir):
    """Sidecar metadata for every complete report, newest period first."""
    try:
        names = os.listdir(reports_dir)
    except FileNotFoundError:
        return []
    entries = []
    for name in names:
        report_id, ext = os.path.splitext(name)
        if ext != ".json" or not REPORT_ID_RE.match(report_id):
            continue
        html_path, meta_path = report_paths(reports_dir, report_id)
        if not os.path.isfile(html_path):
            continue
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        entry = {k: meta.get(k) for k in LISTED_FIELDS}
        entry["id"] = report_id
        entry["title"] = entry["title"] or report_id
        if not isinstance(entry["tags"], list):
            entry["tags"] = []
        entries.append(entry)
    entries.sort(key=lambda e: (str(e.get("to") or ""), str(e.get("created") or "")),
                 reverse=True)
    return entries


def create_reports_blueprint(reports_dir, url_prefix=""):
    reports_dir = os.path.expanduser(reports_dir)
    bp = Blueprint("reports", __name__, url_prefix=f"{url_prefix}/reports")

    @bp.route("")
    def index():
        query = request.args.get("q", "")
        entries = [e for e in list_reports(reports_dir)
                   if fuzzy_match(query, " ".join(
                       str(e.get(k) or "") for k in ("title", "request", "from", "to"))
                       + " " + " ".join(map(str, e["tags"])))]
        return jsonify({"reports": entries})

    @bp.route("/<report_id>")
    def view(report_id):
        try:
            html_path, _ = report_paths(reports_dir, report_id)
        except ValueError:
            abort(404)
        if not os.path.isfile(html_path):
            abort(404)
        resp = send_file(html_path, mimetype="text/html", max_age=0)
        resp.headers["Content-Security-Policy"] = REPORT_CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @bp.route("/<report_id>/delete", methods=["POST"])
    def delete(report_id):
        try:
            paths = report_paths(reports_dir, report_id)
        except ValueError:
            return jsonify({"error": "invalid report id"}), 400
        removed = 0
        for path in paths:
            try:
                os.remove(path)
                removed += 1
            except FileNotFoundError:
                pass
        if not removed:
            return jsonify({"error": "not found"}), 404
        return jsonify({"deleted": report_id})

    return bp

import json
import os
import sys
import tempfile
import unittest

from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from reports import REPORT_CSP, create_reports_blueprint, fuzzy_match, list_reports  # noqa: E402


def write_report(root, report_id, meta=None, html="<html>report</html>"):
    if html is not None:
        with open(os.path.join(root, report_id + ".html"), "w") as f:
            f.write(html)
    if meta is not None:
        with open(os.path.join(root, report_id + ".json"), "w") as f:
            f.write(meta if isinstance(meta, str) else json.dumps(meta))


class ListReportsTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_missing_dir_is_empty(self):
        self.assertEqual(list_reports(os.path.join(self.root, "nope")), [])

    def test_lists_complete_reports_newest_period_first(self):
        write_report(self.root, "sep", {"title": "September", "from": "2026-09-01", "to": "2026-09-30"})
        write_report(self.root, "oct", {"title": "October", "from": "2026-10-01", "to": "2026-10-31",
                                        "tags": ["monthly"], "totals": {"spent": 12.5}})
        write_report(self.root, "orphan-meta", {"title": "no page"}, html=None)
        write_report(self.root, "orphan-page", None)
        entries = list_reports(self.root)
        self.assertEqual([e["id"] for e in entries], ["oct", "sep"])
        self.assertEqual(entries[0]["tags"], ["monthly"])
        self.assertEqual(entries[0]["totals"], {"spent": 12.5})

    def test_bad_sidecar_still_lists_with_id_as_title(self):
        write_report(self.root, "broken", "{not json")
        write_report(self.root, "notdict", "[1, 2]")
        titles = sorted(e["title"] for e in list_reports(self.root))
        self.assertEqual(titles, ["broken", "notdict"])

    def test_ignores_unsafe_names(self):
        write_report(self.root, ".hidden", {"title": "x"})
        self.assertEqual(list_reports(self.root), [])

    def test_fuzzy_match(self):
        self.assertTrue(fuzzy_match("oct26", "October 2026"))
        self.assertTrue(fuzzy_match("", "anything"))
        self.assertFalse(fuzzy_match("zz", "October"))


class ReportRoutesTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        app = Flask(__name__)
        app.register_blueprint(create_reports_blueprint(self.root, url_prefix="/budget"))
        self.client = app.test_client()
        write_report(self.root, "oct", {"title": "October", "to": "2026-10-31", "tags": ["monthly"]})
        write_report(self.root, "q3", {"title": "Third quarter", "to": "2026-09-30"})

    def test_index_filters(self):
        r = self.client.get("/budget/reports")
        self.assertEqual([e["id"] for e in r.get_json()["reports"]], ["oct", "q3"])
        r = self.client.get("/budget/reports?q=quarter")
        self.assertEqual([e["id"] for e in r.get_json()["reports"]], ["q3"])
        r = self.client.get("/budget/reports?q=monthly")
        self.assertEqual([e["id"] for e in r.get_json()["reports"]], ["oct"])

    def test_view_is_sandboxed(self):
        r = self.client.get("/budget/reports/oct")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"<html>report</html>")
        self.assertEqual(r.headers["Content-Security-Policy"], REPORT_CSP)
        self.assertIn("sandbox", REPORT_CSP)
        self.assertEqual(r.mimetype, "text/html")

    def test_view_rejects_traversal_and_missing(self):
        self.assertEqual(self.client.get("/budget/reports/..%2Fsecret").status_code, 404)
        self.assertEqual(self.client.get("/budget/reports/missing").status_code, 404)

    def test_delete_removes_both_files(self):
        r = self.client.post("/budget/reports/oct/delete")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(self.root, "oct.html")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "oct.json")))
        self.assertEqual(self.client.post("/budget/reports/oct/delete").status_code, 404)
        self.assertEqual(self.client.post("/budget/reports/.x/delete").status_code, 400)
        self.assertEqual(self.client.get("/budget/reports/oct/delete").status_code, 405)


if __name__ == "__main__":
    unittest.main()

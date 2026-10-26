import json
import os
import shutil
import sys
import tempfile
import unittest

from werkzeug.security import generate_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import assistant_ui  # noqa: E402


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        secrets = os.path.join(self.tmp, "s.json")
        with open(secrets, "w") as handle:
            json.dump({"secret_key": "k", "password_hash": generate_password_hash("pw")}, handle)
        # Flask resolves templates/ and static/ next to the module; mirror the
        # packaged layout (the Nix build copies them there).
        root = os.path.join(HERE, "..")
        for sub, files in (("templates", ["main.html"]), ("static", ["assistant.js", "assistant.css"])):
            os.makedirs(os.path.join(root, sub), exist_ok=True)
            for name in files:
                shutil.copy(os.path.join(root, name), os.path.join(root, sub, name))
        self.addCleanup(lambda: [shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                                 for d in ("templates", "static")])
        self.app = assistant_ui.create_app(
            "/assistant", ["claude"], secrets, os.path.join(self.tmp, "repo"),
            secure_cookie=False)
        self.client = self.app.test_client()

    def test_page_and_assets(self):
        page = self.client.get("/assistant/")
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn('data-base="/assistant/agent"', body)
        self.assertIn("/assistant/static/assistant.js", body)
        self.assertEqual(self.client.get("/assistant/static/assistant.js").status_code, 200)
        self.assertEqual(self.client.get("/assistant/static/assistant.css").status_code, 200)

    def test_status_reports_missing_repo(self):
        self.client.post("/assistant/agent/login", json={"password": "pw"})
        self.assertEqual(self.client.get("/assistant/agent/status").get_json(), {"present": False})


if __name__ == "__main__":
    unittest.main()

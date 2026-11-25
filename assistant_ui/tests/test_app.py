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
        # packaged layout (the Nix build copies them there). static/ also holds
        # the committed agent_composer.js symlink, so only remove the files this
        # test copies in, not the whole directory.
        root = os.path.join(HERE, "..")
        templates_dir = os.path.join(root, "templates")
        os.makedirs(templates_dir, exist_ok=True)
        shutil.copy(os.path.join(root, "main.html"), os.path.join(templates_dir, "main.html"))
        self.addCleanup(lambda: shutil.rmtree(templates_dir, ignore_errors=True))
        static_dir = os.path.join(root, "static")
        os.makedirs(static_dir, exist_ok=True)
        for name in ("assistant.js", "assistant.css"):
            dest = os.path.join(static_dir, name)
            shutil.copy(os.path.join(root, name), dest)
            self.addCleanup(lambda dest=dest: os.path.exists(dest) and os.remove(dest))
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
        self.assertIn("/assistant/static/agent_composer.js", body)
        script = self.client.get("/assistant/static/assistant.js")
        self.assertEqual(script.status_code, 200)
        # Same terminal key bar as Agent UI, including Open URL.
        self.assertIn("'Open URL'", script.get_data(as_text=True))
        script.close()
        self.assertEqual(self.client.get("/assistant/static/assistant.css").status_code, 200)
        composer = self.client.get("/assistant/static/agent_composer.js")
        self.assertEqual(composer.status_code, 200)
        self.assertIn("AgentComposer", composer.get_data(as_text=True))

    def test_status_reports_missing_repo(self):
        self.client.post("/assistant/agent/login", json={"password": "pw"})
        self.assertEqual(self.client.get("/assistant/agent/status").get_json(), {"present": False})


if __name__ == "__main__":
    unittest.main()

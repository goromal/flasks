import json
import sys
import tempfile
import unittest
from unittest import mock

# tester parses argv and initializes its DB at import time.
_TMP = tempfile.TemporaryDirectory()
_argv, sys.argv = sys.argv, ["tester", "--data-dir", _TMP.name]
import tester  # noqa: E402
sys.argv = _argv

tester.app.register_blueprint(tester.bp)


class ApiBase(unittest.TestCase):
    def setUp(self):
        self.client = tester.app.test_client()

    def create(self, **body):
        return self.client.post("/tester/api/exams", json=body)

    def submit(self, exam_id, **body):
        return self.client.post(f"/tester/api/exams/{exam_id}/submit", json=body)


class Rote(ApiBase):
    def test_create_from_text_and_submit(self):
        r = self.create(title="Poem", exam_type="rote", text="Line one.\nLine two.\nLine three.")
        self.assertEqual(r.status_code, 201)
        exam = r.get_json()
        self.assertEqual(exam["content"]["atoms"], ["Line one.", "Line two.", "Line three."])
        self.assertEqual(exam["source_material"]["created_via"], "api")

        r = self.submit(exam["id"], answers=[
            {"chunk_index": 0, "answer": "line one."},
            {"chunk_index": 2, "answer": "something else"},
        ])
        self.assertEqual(r.status_code, 201)
        res = r.get_json()
        self.assertEqual(res["score"], 0.5)
        self.assertEqual([d["correct"] for d in res["details"]], [True, False])
        self.assertEqual(self.client.get(f"/tester/api/results/{res['id']}").get_json(), res)

    def test_chunk_size_groups_atoms(self):
        exam = self.create(title="P", exam_type="rote", atoms=["a b", "c d", "e f"]).get_json()
        res = self.submit(exam["id"], chunk_size=2,
                          answers=[{"chunk_index": 0, "answer": "a b c d"}]).get_json()
        self.assertEqual(res["details"][0]["chunk"], "a b c d")
        self.assertEqual(res["score"], 1.0)
        r = self.submit(exam["id"], chunk_size=2, answers=[{"chunk_index": 2, "answer": "x"}])
        self.assertEqual(r.status_code, 400)

    def test_html_form_submit_still_works(self):
        exam = self.create(title="P", exam_type="rote", atoms=["a", "b", "c"]).get_json()
        r = self.client.post(f"/tester/exam/{exam['id']}/submit", data={
            "chunk_size": "1", "blank_indices": "[1, 2]", "answer_0": "b", "answer_1": "nope"})
        self.assertEqual(r.status_code, 302)
        result_id = int(r.headers["Location"].rstrip("/").rsplit("/", 1)[1])
        res = self.client.get(f"/tester/api/results/{result_id}").get_json()
        self.assertEqual([d["chunk"] for d in res["details"]], ["b", "c"])
        self.assertEqual(res["score"], 0.5)

    def test_rejects_single_unit(self):
        self.assertEqual(self.create(title="P", exam_type="rote", text="one").status_code, 400)


class MultipleChoice(ApiBase):
    Q = [{"question": "2+2?", "options": ["3", "4"], "correct": 1},
         {"question": "Sky?", "options": ["blue", "green", "red"], "correct": 0}]

    def test_create_and_submit(self):
        exam = self.create(title="MC", exam_type="multiple_choice", questions=self.Q).get_json()
        res = self.submit(exam["id"], answers=[1, None]).get_json()
        self.assertEqual(res["score"], 0.5)
        self.assertEqual(res["details"][1]["user_answer"], -1)

    def test_validation(self):
        bad = [{"question": "x", "options": ["a", "b"], "correct": 2}]
        self.assertEqual(self.create(title="MC", exam_type="multiple_choice",
                                     questions=bad).status_code, 400)
        exam = self.create(title="MC", exam_type="multiple_choice", questions=self.Q).get_json()
        self.assertEqual(self.submit(exam["id"], answers=[1]).status_code, 400)

    def test_html_form_submit_still_works(self):
        exam = self.create(title="MC", exam_type="multiple_choice", questions=self.Q).get_json()
        r = self.client.post(f"/tester/exam/{exam['id']}/submit",
                             data={"answer_0": "1", "answer_1": "0"})
        self.assertEqual(r.status_code, 302)
        result_id = int(r.headers["Location"].rstrip("/").rsplit("/", 1)[1])
        self.assertEqual(self.client.get(f"/tester/api/results/{result_id}").get_json()["score"], 1.0)


class ShortAnswer(ApiBase):
    Q = ["Capital of France?", "Author of Hamlet?"]

    def exam(self):
        return self.create(title="SA", exam_type="short_answer", questions=self.Q).get_json()

    def test_caller_grades_skip_ai(self):
        with mock.patch.object(tester, "call_claude") as claude:
            res = self.submit(self.exam()["id"], answers=["Paris", "Marlowe"], grades=[
                {"score": 1, "justification": "right"},
                {"score": 0, "justification": "wrong"},
            ]).get_json()
        claude.assert_not_called()
        self.assertEqual(res["score"], 0.5)
        self.assertEqual(res["details"][1]["user_answer"], "Marlowe")

    def test_ai_grading(self):
        reply = json.dumps([{"question": q, "user_answer": "a", "score": 0.5,
                             "justification": "meh"} for q in self.Q])
        with mock.patch.object(tester, "call_claude", return_value=reply):
            res = self.submit(self.exam()["id"], answers=["a", "a"]).get_json()
        self.assertEqual(res["score"], 0.5)

    def test_ai_failure_is_502(self):
        with mock.patch.object(tester, "call_claude", side_effect=RuntimeError("no key")):
            r = self.submit(self.exam()["id"], answers=["a", "b"])
        self.assertEqual(r.status_code, 502)
        self.assertIn("no key", r.get_json()["error"])


class Misc(ApiBase):
    def test_unknown_exam_404(self):
        self.assertEqual(self.client.get("/tester/api/exams/99999").status_code, 404)
        self.assertEqual(self.submit(99999, answers=[]).status_code, 404)

    def test_bad_type_and_list(self):
        self.assertEqual(self.create(title="x", exam_type="essay").status_code, 400)
        exams = self.client.get("/tester/api/exams").get_json()["exams"]
        self.assertTrue(all({"id", "title", "exam_type"} <= set(e) for e in exams))


if __name__ == "__main__":
    unittest.main()

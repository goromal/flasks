# tester

Self-testing exam tool (rote, multiple choice, short answer).

## Agent API

JSON endpoints under the app's subdomain (default `/tester`), for agents that
create exams from their own content and submit answers directly. Like the rest
of the app, they're unauthenticated and rely on the LAN-only nginx vhost.
Errors return `{"error": "..."}` with status 400 (bad input), 404 (unknown id),
or 502 (AI grading failed).

| Method | Path | Returns |
| --- | --- | --- |
| `GET` | `/api/exams` | `{"exams": [{id, title, exam_type, created_at}]}` |
| `POST` | `/api/exams` | the created exam (201) |
| `GET` | `/api/exams/<id>` | exam with parsed `content` and `source_material` |
| `POST` | `/api/exams/<id>/submit` | the stored result (201) |
| `GET` | `/api/results/<id>` | result with parsed `details` |

### Create

Every body has `title` and `exam_type`, plus an optional `source_material`
object (stored with `created_via: "api"`). Exams created this way show up
in the UI and can be taken there as usual.

```jsonc
// rote: give "text" (split by line, falling back to sentence) or "atoms"; 2+ units
{"title": "Psalm 23", "exam_type": "rote", "text": "The Lord is my shepherd...\n..."}
// multiple_choice: "correct" is the 0-based option index
{"title": "Ch. 3", "exam_type": "multiple_choice",
 "questions": [{"question": "2+2?", "options": ["3", "4"], "correct": 1}]}
// short_answer
{"title": "Ch. 3", "exam_type": "short_answer", "questions": ["Why ...?"]}
```

### Submit

```jsonc
// rote: blank out chunks of chunk_size atoms (default 1); fuzzy-graded at 80%
{"chunk_size": 1, "answers": [{"chunk_index": 0, "answer": "The Lord is my shepherd"}]}
// multiple_choice: one option index per question, null = unanswered
{"answers": [1, null, 0]}
// short_answer: one string per question; graded by Claude unless the caller
// passes its own "grades" (one {"score": 0.0-1.0, "justification": str} each)
{"answers": ["Because ..."], "grades": [{"score": 0.8, "justification": "..."}]}
```

```bash
curl -s -X POST http://localhost:5757/tester/api/exams/3/submit \
  -H 'Content-Type: application/json' -d '{"answers": [1, 0]}'
```

## Tests

```bash
PYTHONPATH=../wormhole:. python -m unittest discover -s tests -t .
```

# cozy HTTP API

For batch work from a shell, `cozyctl` (below) is usually easier than driving
these endpoints by hand.

## cozyctl

Ships in the same package as the UI, and is installed on any host running cozy
with `COZY_URL` pre-pointed at the local service.

```bash
# One named generator job per <name>.txt in the directory, then start the queue.
cozyctl queue ~/cozy-state/prompts -w imggen-quantized --width 512 --height 768

cozyctl queue ./prompts -w imggen-quantized -n   # preview, queue nothing
cozyctl status                                    # what is running / pending
cozyctl start / cozyctl stop
```

Each file's *name* becomes the job's output image name and its *contents*
become the prompt, which is the same layout the prompt library uses — so the
prompt database directory can be pointed at directly.

The whole directory is validated before the first job is queued, so a filename
that is not a legal output name stops the run rather than queueing half of it.
Empty files are skipped with a warning. `queue` starts the scheduler when it is
done unless `--no-start` is given.

Connection settings: `--url` (env `COZY_URL`), and `--token-file` (env
`COZY_TOKEN_FILE`, default `~/secrets/flask/cozy-api-token`) or `--token` /
env `COZY_TOKEN`.


Every endpoint the browser UI uses is a plain JSON endpoint, so a script can
drive the queue the same way the page does. All state lives on the server
(`queue.json` under the state dir), so a job added by a script shows up in every
open browser within a second, on any machine.

## Authentication

Two ways in, both giving the same access:

- **Session cookie** — POST the login form, keep the cookie. What the browser does.
- **Bearer token** — `Authorization: Bearer <token>`. What a script should do.

The token is optional. With no `api_token_hash` in the secrets file, bearer auth
is off and every token is rejected.

To enable it, add a hash to the secrets file (`--secrets-file`, deployed at
`/data/andrew/secrets/flask/cozy.json`) alongside the existing keys:

```bash
python3 -c 'from werkzeug.security import generate_password_hash as g; \
            import sys; print(g(sys.argv[1]))' "$MY_TOKEN"
```

```json
{
  "secret_key": "...",
  "password_hash": "scrypt:...",
  "api_token_hash": "scrypt:..."
}
```

Then `systemctl restart cozy`. The token is stored hashed, so a leaked secrets
file does not hand over a working token — but that makes each check a
deliberately slow KDF. Prefer a session cookie over a bearer token if you intend
to poll at any rate.

Unauthenticated requests under `/cozy/api/` get `401 {"error": "unauthorized"}`.
Everything else still redirects to the login page.

> The service speaks plain HTTP on the LAN. A bearer token is sniffable there,
> and unlike a 20-minute session it does not expire — treat it accordingly.

## Queueing a job

```bash
curl -X POST http://jetson-orin-agx.local/cozy/api/queue/add \
  -H "Authorization: Bearer $COZY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"workflow": "imggen-quantized", "prompt": "a cat on a porch",
       "width": 400, "height": 800, "basename": "porch-cat"}'
```

→ `{"id": "<32 hex chars>", "eta": 91.4}`

**Adding does not start the queue.** Follow with `POST /api/queue/start`, which
returns 409 if the queue is already draining or a single-job generate holds the
run lock — both of which mean your job will run anyway, so a 409 here is
normally fine to ignore.

### Job fields

| Field | Required | Notes |
| --- | --- | --- |
| `workflow` | yes | must be one of the configured `--workflows` |
| `prompt` | | |
| `width`, `height` | | default 400x800; ignored by edit workflows |
| `basename` | | names the output image; `^[A-Za-z0-9][A-Za-z0-9._ -]*$` |
| `image` | edit only | input path relative to the input or output dir |
| `remote_image` | edit only | `{"host": ..., "path": ...}`, staged when the job runs |
| `rect` | edit only | `{"x","y","w","h"}` crop region |

## Endpoints

| Endpoint | Method | |
| --- | --- | --- |
| `/cozy/api/queue/add` | POST | returns `{id, eta}` |
| `/cozy/api/queue/status` | GET | current job, pending list, results, `total_eta` |
| `/cozy/api/queue/start` | POST | begin draining; 409 if busy |
| `/cozy/api/queue/stop` | POST | stop after the current job |
| `/cozy/api/queue/remove` | POST | `{"id": ...}` |
| `/cozy/api/queue/clear` | POST | drop finished results |
| `/cozy/api/queue/image?id=<id>` | GET | result PNG (`&kind=crop` for the crop) |

`/api/generate` runs a single job outside the queue and returns 409 while the
queue is active; prefer the queue endpoints for scripted use.

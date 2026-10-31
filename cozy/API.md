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

There is no baked-in default URL: cozy's port is chosen by the NixOS module
(anixpkgs' `service-ports.nix`), which this repo cannot see, so a hardcoded copy
here would rot silently. On a host running cozy the packaging sets `COZY_URL`
for you; from anywhere else, pass `--url http://myhost.local/cozy`.


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
| `/cozy/api/queue/cancel` | POST | `{"id": ...}`; removes a pending job or interrupts the running one (result status `cancelled`); 404 if neither |
| `/cozy/api/queue/clear` | POST | drop finished results |
| `/cozy/api/queue/image?id=<id>` | GET | result PNG (`&kind=crop` for the crop) |
| `/cozy/api/input-fit?name=<path>` | GET | what the input-size ceiling will do; `&x&y&w&h` measures a crop instead |

`/api/generate` runs a single job outside the queue and returns 409 while the
queue is active; prefer the queue endpoints for scripted use.

## Input-size ceiling

Edit workflows will not hand ComfyUI an input larger than `--max-input-bytes`
(1 MiB by default, 0 to disable). An oversized input is re-encoded as JPEG and,
if that is still too big, resized down; a crop is measured on its own, so a
small selection out of a huge photo is never shrunk. `/api/input-fit` reports
what will happen before the job runs, and `fit` — `{scale, from, to}`, or
`null` when nothing was resized — appears on `/api/status` and on each queue
result afterwards.

## Video tab

Present only when cozy runs with `--ollama-url` (anixpkgs enables it on hosts
with `machines.localLlm`). One video job at a time; it holds the same GPU lock
as image jobs, so analyze/export return 409 while an image job runs and vice
versa.

| Endpoint | Method | |
| --- | --- | --- |
| `/cozy/api/video/list` | GET | `{videos: [{value, label, source}]}` from the input/output dirs |
| `/cozy/api/video/upload` | POST | multipart `file` → `{value}`; up to `--max-video-bytes` (2 GiB) |
| `/cozy/api/video/models` | GET | `{models: [{name, vision, default, secs_per_frame}]}`: every installed Ollama model (what `anix-llm models` lists); 502 if Ollama is down |
| `/cozy/api/video/estimate?video=&stride=&precision=&model=` | GET | before analyzing a local video: `{duration, stride, coarse_frames, frames, secs_per_frame, eta_s}`; `frames` assumes one matching segment, timings are `null` until a run has measured that model |
| `/cozy/api/video/analyze` | POST | `{prompt, video \| remote_video: {host, path}, stride?, precision?, merge_gap?, fresh?}` → 202; `fresh: true` re-judges every frame instead of reusing earlier verdicts; `model` picks an installed vision model (default `--video-model`; 400 if not installed or text-only), and verdicts are only reused for the same model |
| `/cozy/api/video/status` | GET | `state` (`idle`, `staging`, `probing`, `analyzing`, `review`, `exporting`, `done`, `error`, `cancelled`), `samples`, `segments`, `calls_done`/`calls_est`, `eta_s`, `error`, `output` |
| `/cozy/api/video/cancel` | POST | stop analysis at the next frame, or kill a running export (back to review); 404 if nothing is running |
| `/cozy/api/video/clear` | POST | forget the job (segments, thumbnails); files are untouched; 409 while running |
| `/cozy/api/video/export` | POST | `{keep: [bool per segment], basename?}` → 202; writes `<output-dir>/video/<basename>.mp4` |
| `/cozy/api/video/result` | GET | the exported MP4, inline (it stays in `<output-dir>/video/`) |
| `/cozy/api/video/file?name=` / `?job=1` | GET | range-capable source video for previews |
| `/cozy/api/video/frame?ms=` | GET | a sampled frame's JPEG thumbnail |

Times are seconds. `stride` defaults to `clamp(duration/120, 2, 10)`,
`precision` to 1, `merge_gap` to `precision`. Re-analyzing the same video with
the same prompt reuses every verdict already made.

`/api/flush` also deletes input videos (`video-uploads/` and video files at
the top of the input dir), clears the video job, and returns 409 while a
video job is running. Exported videos stay in `<output-dir>/video/` for the
output dir's `.flush.sh`.

"""cozyctl -- command-line client for the cozy queue API.

Queues one generator job per prompt file in a directory, naming each job's
output after the file it came from. The directory format is the same one the
cozy prompt library uses (``<name>.txt``), so the prompt database can be pointed
at directly.
"""
import argparse
import os
import re
import sys

import requests

DEFAULT_URL = "http://127.0.0.1:6262/cozy"
DEFAULT_TOKEN_FILE = "~/secrets/flask/cozy-api-token"

# Mirrors cozy._NAME_RE. Checked here so a badly named prompt file is caught
# before anything is queued, rather than 400ing partway through a directory.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")


class Error(Exception):
    """A message for the user; main() prints it and exits nonzero."""


def human_eta(seconds):
    if not seconds:
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)


def read_token(token, token_file):
    """The bearer token, or None to talk to a cozy with API auth switched off.

    An explicit --token-file that does not exist is an error; the default one
    missing just means no token, since that is a fresh install's state.
    """
    if token:
        return token
    env = os.environ.get("COZY_TOKEN")
    if env:
        return env.strip()
    explicit = token_file is not None
    path = os.path.expanduser(token_file or DEFAULT_TOKEN_FILE)
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError as e:
        if explicit:
            raise Error("cannot read token file %s: %s" % (path, e))
        return None


def read_prompt_dir(path):
    """[(name, text)] for every non-empty .txt in `path`, sorted by name.

    Raises if any file would produce an output name the server rejects, so the
    whole directory is validated before the first job is queued.
    """
    if not os.path.isdir(path):
        raise Error("not a directory: %s" % path)
    prompts, bad, empty = [], [], []
    for fn in sorted(os.listdir(path)):
        if not fn.endswith(".txt"):
            continue
        name = fn[:-len(".txt")]
        if not NAME_RE.match(name):
            bad.append(fn)
            continue
        with open(os.path.join(path, fn)) as f:
            text = f.read().strip()
        if not text:
            empty.append(fn)
            continue
        prompts.append((name, text))
    if bad:
        raise Error(
            "these filenames cannot be used as output names: %s\n"
            "names must match %s" % (", ".join(bad), NAME_RE.pattern))
    for fn in empty:
        print("skipping %s: empty" % fn, file=sys.stderr)
    if not prompts:
        raise Error("no usable .txt prompt files in %s" % path)
    return prompts


class Cozy:
    def __init__(self, url, token, timeout=60):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = "Bearer " + token

    def call(self, method, path, json_body=None):
        try:
            r = self.session.request(method, self.url + path, json=json_body,
                                     timeout=self.timeout)
        except requests.RequestException as e:
            raise Error("cannot reach cozy at %s: %s" % (self.url, e))
        if r.status_code == 401:
            raise Error(
                "cozy rejected the API token.\n"
                "Set one with ~/secrets/flask/cozy-api-token, $COZY_TOKEN, or "
                "--token-file, and make sure api_token_hash is in cozy's "
                "secrets file.")
        # Errors carry a JSON {"error": ...}; fall back to the status line for
        # anything that failed before reaching the app (a proxy, say).
        if not r.ok:
            try:
                msg = r.json().get("error") or r.reason
            except ValueError:
                msg = r.reason
            raise Error("cozy: %s (HTTP %d)" % (msg, r.status_code))
        return r.json()

    def add(self, spec):
        return self.call("POST", "/api/queue/add", spec)

    def status(self):
        return self.call("GET", "/api/queue/status")

    def start(self):
        try:
            self.call("POST", "/api/queue/start")
            return True
        except Error as e:
            # 409 means it is already draining, which is the desired end state.
            if "HTTP 409" in str(e):
                return False
            raise

    def stop(self):
        return self.call("POST", "/api/queue/stop")


def cmd_queue(args, cozy):
    prompts = read_prompt_dir(args.directory)
    print("%d prompt%s from %s -> %s at %dx%d" % (
        len(prompts), "" if len(prompts) == 1 else "s",
        args.directory, args.workflow, args.width, args.height))
    if args.dry_run:
        for name, text in prompts:
            head = text.replace("\n", " ")
            print("  %-28s %s" % (name, head[:60] + ("…" if len(head) > 60 else "")))
        print("(dry run; nothing queued)")
        return 0

    queued = 0
    try:
        for name, text in prompts:
            res = cozy.add({"workflow": args.workflow, "prompt": text,
                            "width": args.width, "height": args.height,
                            "basename": name})
            queued += 1
            print("  %-28s %s" % (name, human_eta(res.get("eta"))))
    except Error:
        # Report what did land: the queue keeps them, so the user needs to know
        # whether to clear it before retrying.
        if queued:
            print("queued %d of %d before failing" % (queued, len(prompts)),
                  file=sys.stderr)
        raise

    # Ask the server for the total rather than summing the per-job ETAs above:
    # it also counts the rest gap between jobs, so a local sum would disagree
    # with what 'cozyctl status' reports a moment later. It covers the whole
    # queue, hence the wording -- anything already pending is included.
    try:
        total = cozy.status().get("total_eta")
    except Error:
        total = None
    print("queued %d job%s%s" % (
        queued, "" if queued == 1 else "s",
        "; queue ETA " + human_eta(total) if total else ""))
    if args.no_start:
        print("not started (--no-start); run 'cozyctl start' when ready")
    elif cozy.start():
        print("queue started")
    else:
        print("queue already running")
    return 0


def cmd_status(args, cozy):
    snap = cozy.status()
    cur = snap.get("current")
    if cur:
        print("running: %s (%s) %s%%" % (
            cur.get("basename") or cur.get("workflow"),
            cur.get("workflow"), cur.get("progress", "?")))
    else:
        print("running: %s" % ("idle" if not snap.get("active") else "starting"))
    jobs = snap.get("jobs") or []
    for j in jobs:
        print("  pending %-24s %s" % (
            j.get("basename") or j.get("workflow"), human_eta(j.get("eta"))))
    results = snap.get("results") or []
    if results:
        ok = sum(1 for r in results if r.get("status") == "success")
        print("results: %d (%d ok, %d failed)" % (len(results), ok, len(results) - ok))
    print("%d pending, total ETA %s" % (len(jobs), human_eta(snap.get("total_eta"))))
    return 0


def cmd_start(args, cozy):
    print("queue started" if cozy.start() else "queue already running")
    return 0


def cmd_stop(args, cozy):
    cozy.stop()
    print("queue will stop after the current job")
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="cozyctl",
        description="Queue cozy image-generation jobs from the command line.")
    p.add_argument("--url", default=os.environ.get("COZY_URL", DEFAULT_URL),
                   help="base URL of the cozy app (env COZY_URL, default %s)" % DEFAULT_URL)
    p.add_argument("--token", default=None,
                   help="API token (env COZY_TOKEN; prefer --token-file)")
    p.add_argument("--token-file", default=os.environ.get("COZY_TOKEN_FILE"),
                   help="file holding the API token (default %s)" % DEFAULT_TOKEN_FILE)
    sub = p.add_subparsers(dest="command", required=True)

    q = sub.add_parser("queue", help="queue one generator job per prompt file")
    q.add_argument("directory",
                   help="directory of <name>.txt prompt files; each file's name "
                        "becomes the output image name")
    q.add_argument("-w", "--workflow", required=True,
                   help="workflow to run, e.g. imggen-quantized (generator "
                        "workflows only; edit workflows need an input image)")
    q.add_argument("--width", type=int, default=400)
    q.add_argument("--height", type=int, default=800)
    q.add_argument("--no-start", action="store_true",
                   help="leave the queue stopped instead of starting it")
    q.add_argument("-n", "--dry-run", action="store_true",
                   help="list what would be queued and exit")
    q.set_defaults(func=cmd_queue)

    s = sub.add_parser("status", help="show the queue")
    s.set_defaults(func=cmd_status)

    st = sub.add_parser("start", help="start draining the queue")
    st.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="stop after the current job")
    sp.set_defaults(func=cmd_stop)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        cozy = Cozy(args.url, read_token(args.token, args.token_file))
        return args.func(args, cozy)
    except Error as e:
        print("cozyctl: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def run():
    sys.exit(main())


if __name__ == "__main__":
    run()

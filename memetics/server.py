"""Local dashboard, authenticated registration API, and signed GitHub wakeups."""

import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Memetics listeners</title><style>body{font:16px system-ui;max-width:1150px;margin:3rem auto;padding:0 1rem;background:#10151d;color:#dde7f1}h1{color:#9ff0cc}input,button{font:inherit;padding:.5rem;background:#202b39;color:inherit;border:1px solid #425268;border-radius:5px}section{overflow-x:auto}table{border-collapse:collapse;width:100%;margin-bottom:2rem}th,td{text-align:left;padding:.65rem;border-bottom:1px solid #314051;overflow-wrap:anywhere}a{color:#9ff0cc}small{color:#aebdcd}#error{color:#ffb8b8}pre{white-space:pre-wrap}details{margin:1rem 0}</style>
<h1>Memetics</h1><p>Standing upstream interests. Shared updates. Implementation pull requests.</p>
<label>API token <input id="token" type="password" autocomplete="off" placeholder="Only needed if configured"></label> <button id="refresh">Refresh</button>
<p id="error" role="alert"></p><small id="updated"></small><main id="status"></main>
<script>
const token=document.querySelector('#token'); token.value=sessionStorage.getItem('memetics-token')||'';
function table(title,rows,keys){const section=document.createElement('section');const h=document.createElement('h2');h.textContent=title;section.append(h);if(!rows.length){const p=document.createElement('p');p.textContent='None yet';section.append(p);return section;}const t=document.createElement('table');const header=t.insertRow();keys.forEach(k=>{const th=document.createElement('th');th.textContent=k;header.append(th)});rows.forEach(row=>{const tr=t.insertRow();keys.forEach(k=>{const td=tr.insertCell();td.textContent=row[k]??'—';if(k==='url'&&String(row[k]).startsWith('https://github.com/')){const a=document.createElement('a');a.href=row[k];a.textContent='Open PR';td.replaceChildren(a)}})});section.append(t);return section;}
async function refresh(){try{sessionStorage.setItem('memetics-token',token.value);const r=await fetch('/api/status',{headers:token.value?{Authorization:'Bearer '+token.value}:{}});if(!r.ok)throw Error('Status request failed: '+r.status);const s=await r.json();document.querySelector('#status').replaceChildren(table('Listeners',s.listeners,['id','enabled','observed','adopted']),table('Shared upstream watches',s.sources,['repo','ref','polls','error']),table('Jobs',s.jobs,['id','listener_id','status','attempts','reason']),table('Pull requests',s.proposals,['listener_id','state','url']));document.querySelector('#updated').textContent='Updated '+new Date().toLocaleTimeString()+' · '+s.cache.changes+' cached changes · '+s.cache.blobs+' indexed blobs';document.querySelector('#error').textContent='';}catch(e){document.querySelector('#error').textContent=e.message}}
document.querySelector('#refresh').onclick=refresh;refresh();setInterval(refresh,10000);
</script></html>"""


def make_server(store, host="127.0.0.1", port=8776, api_token="", webhook_secret=""):
    if host not in {"127.0.0.1", "localhost", "::1"} and not api_token:
        raise ValueError(
            "MEMETICS_API_TOKEN is required when listening beyond localhost"
        )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # Do not log request credentials or payloads.

        def send(self, status, data, content_type="application/json"):
            body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self, write=False):
            if not api_token:
                return not write
            expected = "Bearer " + api_token
            return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                self.send(200, PAGE, "text/html; charset=utf-8")
                return
            if path == "/health":
                self.send(200, {"status": "ok"})
                return
            if not self.authorized():
                self.send(401, {"error": "API token required"})
                return
            if path == "/api/status":
                self.send(200, store.status())
            elif path.startswith("/api/jobs/") and path.rsplit("/", 1)[-1].isdigit():
                job = store.one(
                    "SELECT * FROM jobs WHERE id=?", (int(path.rsplit("/", 1)[-1]),)
                )
                self.send(200 if job else 404, job or {"error": "unknown job"})
            else:
                self.send(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1_000_000:
                    self.send(413, {"error": "body must contain 1-1000000 bytes"})
                    return
                if path != "/webhooks/github" and not self.authorized(write=True):
                    self.send(401, {"error": "write API requires MEMETICS_API_TOKEN"})
                    return
                raw = self.rfile.read(length)
                if path == "/webhooks/github":
                    signature = (
                        "sha256="
                        + hmac.new(
                            webhook_secret.encode(), raw, hashlib.sha256
                        ).hexdigest()
                    )
                    if not webhook_secret or not hmac.compare_digest(
                        signature, self.headers.get("X-Hub-Signature-256", "")
                    ):
                        self.send(401, {"error": "invalid webhook signature"})
                        return
                data = json.loads(raw)
                if path == "/webhooks/github":
                    event_id = self.headers.get("X-GitHub-Delivery", "")
                    if not event_id or len(event_id) > 200:
                        raise ValueError("missing or invalid delivery id")
                    with store.connect() as db:
                        inserted = db.execute(
                            "INSERT OR IGNORE INTO events VALUES(?,?)",
                            (event_id, time.time()),
                        ).rowcount
                        if inserted and self.headers.get("X-GitHub-Event") == "push":
                            ref = data.get("ref", "").removeprefix("refs/heads/")
                            repo = data.get("repository", {}).get("full_name", "")
                            db.execute(
                                "UPDATE sources SET next_poll=0 WHERE lower(repo)=lower(?) AND ref=?",
                                (repo, ref),
                            )
                    self.send(202, {"accepted": True, "duplicate": not bool(inserted)})
                elif path == "/api/listeners":
                    if (
                        data.get("version") != 1
                        or not isinstance(data.get("listeners"), list)
                        or not data["listeners"]
                    ):
                        raise ValueError(
                            "version 1 manifest with nonempty listeners required"
                        )
                    self.send(201, {"listeners": store.register(data["listeners"])})
                elif path.startswith("/api/listeners/") and path.rsplit("/", 1)[-1] in {
                    "pause",
                    "resume",
                }:
                    _, _, _, listener, action = path.split("/")
                    store.enable(listener, action == "resume")
                    self.send(
                        200, {"listener": listener, "enabled": action == "resume"}
                    )
                else:
                    self.send(404, {"error": "not found"})
            except (ValueError, KeyError, TypeError) as exc:
                self.send(400, {"error": str(exc)})

    return ThreadingHTTPServer((host, port), Handler)


def serve(store, engine, host, port, interval):
    httpd = make_server(
        store,
        host,
        port,
        os.environ.get("MEMETICS_API_TOKEN", ""),
        os.environ.get("MEMETICS_WEBHOOK_SECRET", ""),
    )
    stop = threading.Event()

    def work():
        while not stop.is_set():
            try:
                engine.tick()
            except Exception as exc:
                print(json.dumps({"worker_error": str(exc)}), flush=True)
            stop.wait(interval)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    print(f"Memetics dashboard: http://{host}:{httpd.server_port}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        httpd.server_close()

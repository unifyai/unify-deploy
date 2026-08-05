#!/usr/bin/env bash
# Install patchright's Chromium without depending on cdn.playwright.dev.
#
# patchright resolves Chrome-for-Testing builds through a mirror list holding
# exactly one host (`cftUrl` in patchright-core), unlike ordinary playwright
# browsers which carry three. On a builder whose resolver answers
# cdn.playwright.dev with an AAAA record it cannot route, that single mirror is
# a dead end: node fails with EADDRNOTAVAIL, patchright retries the same host
# five times at over two minutes each, and the build hits its timeout. The
# playwright install earlier in the image survives the identical fault only
# because its mirror list includes an ESRP host to fall back to.
#
# cdn.playwright.dev is itself only a redirector to the Chrome-for-Testing
# bucket, so this points the download at that bucket directly through a
# loopback shim that rewrites the request path. Storage is Google-hosted and
# reachable from any builder that can pull its own base images. patchright
# still performs the install, so the browser layout and its completion marker
# stay patchright's business rather than something reimplemented here.
set -euo pipefail

PORT="${CFT_MIRROR_PORT:-8899}"
BUCKET="https://storage.googleapis.com/chrome-for-testing-public"

python3 - "$PORT" "$BUCKET" <<'PY' &
import http.server
import socketserver
import sys

port, bucket = int(sys.argv[1]), sys.argv[2]
# The only path shape patchright asks a CFT mirror for. Anything else is a
# change in patchright's URL scheme and must fail loudly rather than 404 into
# a confusing download error.
PREFIX = "/builds/cft/"


class Redirector(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _redirect(self):
        if not self.path.startswith(PREFIX):
            self.send_error(501, f"not a Chrome-for-Testing path: {self.path}")
            return
        self.send_response(302)
        self.send_header("Location", f"{bucket}/{self.path[len(PREFIX):]}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _redirect
    do_HEAD = _redirect

    def log_message(self, *args):
        pass


socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", port), Redirector) as httpd:
    httpd.serve_forever()
PY
mirror_pid=$!
trap 'kill "$mirror_pid" 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
    if python3 -c "import socket,sys; s=socket.create_connection(('127.0.0.1', $PORT), 0.2); s.close()" \
        2>/dev/null; then
        break
    fi
    sleep 0.1
done

cd /opt/magnitude/packages/magnitude-core
PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST="http://127.0.0.1:${PORT}" \
    npx patchright install --with-deps chromium

# Settled inside the layer that writes the bytes: a later chown -R would
# copy-up the whole browser tree into its own layer and ship it twice.
chown -R 1000:1000 /home/unity/.cache

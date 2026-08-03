/**
 * Serves the Canvas runtime host on its own local origin.
 *
 * Canvas renders assistant-authored code, and the isolation depends on two things
 * the browser enforces: a sandboxed frame with no `allow-same-origin`, and a
 * *different origin* from Console so the host's CSP is one we own and Console's is
 * never weakened. A port is part of an origin, so `localhost:3100` against
 * Console's `localhost:3000` gives self-host the same guarantee production gets
 * from `canvas.unify.ai` against `console.unify.ai` — with no hosts file or TLS.
 *
 * Without this, self-host authors canvases correctly and then has nothing to frame.
 *
 * The response headers come from the host bundle's own `scripts/headers.mjs`, the
 * same module the hosted bucket deploy and unify's author-time render gate read.
 * Restating the policy here is the one thing that must not happen: a local origin
 * running a laxer CSP than production would render canvases that then fail for real
 * viewers, which is exactly the class of bug the shared module exists to prevent.
 *
 * Static files only. There is no application here, and no request from a canvas can
 * reach it — `connect-src 'none'` means the frame cannot make one.
 */

import { createReadStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import { createServer } from 'node:http';
import { join, normalize, resolve, sep } from 'node:path';
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

const HOST_DIR = resolve(process.env.CANVAS_HOST_DIR || `${process.env.HOME}/.unity/canvas-host`);
const PORT = Number(process.env.CANVAS_PORT || 3100);
const HOST_ORIGIN = (process.env.CANVAS_ORIGIN || `http://localhost:${PORT}`).replace(/\/+$/, '');
const PARENT_ORIGINS = (process.env.CANVAS_ALLOWED_CONSOLE_ORIGINS || 'http://localhost:3000')
  .split(',')
  .map((origin) => origin.trim().replace(/\/+$/, ''))
  .filter(Boolean);

const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
  '.ttf': 'font/ttf',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.webp': 'image/webp',
  '.ico': 'image/x-icon',
};

function contentType(path) {
  const dot = path.lastIndexOf('.');
  return (dot === -1 ? null : CONTENT_TYPES[path.slice(dot).toLowerCase()]) ?? 'application/octet-stream';
}

/**
 * Map a request path to a file inside the host directory, or null.
 *
 * Normalising and then re-checking the prefix is what stops `..` segments from
 * reaching outside the served tree — the ordinary requirement for any file server,
 * and this one runs on a developer machine alongside their whole home directory.
 */
function resolveFile(urlPath) {
  const decoded = decodeURIComponent(urlPath.split('?')[0]);
  const candidate = resolve(join(HOST_DIR, normalize(decoded)));
  if (candidate !== HOST_DIR && !candidate.startsWith(HOST_DIR + sep)) return null;
  return candidate;
}

const { hostHeaders, inlineScriptHashes } = await import(
  pathToFileURL(join(HOST_DIR, 'scripts/headers.mjs')).href
);

const indexHtml = await readFile(join(HOST_DIR, 'host/v1/index.html'), 'utf8');
const HEADERS = hostHeaders({
  hostOrigin: HOST_ORIGIN,
  parentOrigins: PARENT_ORIGINS,
  // Derived from the document being served rather than passed in, so editing
  // index.html cannot silently invalidate the inline import-map hash.
  scriptHashes: inlineScriptHashes(indexHtml),
});

const server = createServer(async (request, response) => {
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    response.writeHead(405, { Allow: 'GET, HEAD' }).end();
    return;
  }

  const file = resolveFile(request.url || '/');
  if (!file) {
    response.writeHead(403).end();
    return;
  }

  let info;
  try {
    info = await stat(file);
  } catch {
    response.writeHead(404, { 'Content-Type': 'text/plain' }).end('Not found\n');
    return;
  }
  if (info.isDirectory()) {
    response.writeHead(404, { 'Content-Type': 'text/plain' }).end('Not found\n');
    return;
  }

  response.writeHead(200, {
    ...HEADERS,
    'Content-Type': contentType(file),
    'Content-Length': String(info.size),
    // The host is rebuilt in place by the installer, so a cached copy would
    // outlive a kit upgrade. Versioning lives in the path (`host/v1`), which is
    // what makes an old canvas keep rendering on the runtime it was reviewed on.
    'Cache-Control': 'no-cache',
  });

  if (request.method === 'HEAD') {
    response.end();
    return;
  }
  createReadStream(file).pipe(response);
});

server.listen(PORT, '127.0.0.1', () => {
  process.stdout.write(`canvas origin serving ${HOST_DIR} on ${HOST_ORIGIN}\n`);
  process.stdout.write(`  frame-ancestors ${PARENT_ORIGINS.join(' ')}\n`);
});

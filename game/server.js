'use strict';
/*
 * Threes-clone HTTP API + static browser client. Zero dependencies.
 *
 * Endpoints (JSON):
 *   GET  /api/health          -> {ok:true, name, version}
 *   GET  /api/state           -> full game state {grid, score, validMoves, queue, ...}
 *   POST /api/move   {dir}    -> apply move (up|down|left|right); returns new state or {ok:false,reason}
 *   POST /api/new    {seed?}  -> start a new game (returns new state)
 *   POST /api/undo            -> undo last move
 *   GET  /                    -> playable browser client
 *
 * Every move is appended to games/games.jsonl for AI-training/replay.
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const { Threes, MOVES } = require('./lib/engine');

const PORT = Number(process.env.PORT || 8321);
const HOST = process.env.HOST || '0.0.0.0';
const ROOT = __dirname;
const PUB = path.join(ROOT, 'public');
const DATA = path.join(ROOT, 'games');
fs.mkdirSync(DATA, { recursive: true });
const LOG = path.join(DATA, 'games.jsonl');

let game = new Threes();
let gameId = Date.now().toString(36);
fs.appendFileSync(LOG, JSON.stringify({ type: 'new', gameId, seed: game.seed, at: new Date().toISOString() }) + '\n');

function logEvent(ev) {
  fs.appendFileSync(LOG, JSON.stringify({ gameId, at: new Date().toISOString(), ...ev }) + '\n');
}

function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve) => {
    let buf = '';
    req.on('data', (c) => { buf += c; if (buf.length > 1e5) req.destroy(); });
    req.on('end', () => {
      try { resolve(buf ? JSON.parse(buf) : {}); } catch { resolve({}); }
    });
  });
}

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml', '.webmanifest': 'application/manifest+json' };

function serveStatic(res, urlPath) {
  let p = urlPath === '/' ? '/index.html' : urlPath;
  p = path.normalize(p).replace(/^(\.\.[\/\\])+/, '');
  const file = path.join(PUB, p);
  if (!file.startsWith(PUB)) { res.writeHead(403); return res.end('nope'); }
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404); return res.end('not found'); }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' });
    res.end(data);
  });
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    });
    return res.end();
  }

  const url = new URL(req.url, 'http://x');
  const p = url.pathname;

  try {
    if (p === '/api/health') return json(res, 200, { ok: true, name: 'threes-clone', moves: MOVES });

    if (p === '/api/state' && req.method === 'GET') {
      return json(res, 200, game.state());
    }

    if (p === '/api/move' && req.method === 'POST') {
      const body = await readBody(req);
      const dir = String(body.dir || body.move || '').toLowerCase();
      const r = game.applyMove(dir);
      if (!r.ok) return json(res, 400, r);
      logEvent({ type: 'move', dir, score: game.score, merges: r.merges.length, spawn: r.spawn });
      if (game.gameOver) logEvent({ type: 'gameover', score: game.score, maxTile: game.maxTile(), moves: game.moves });
      return json(res, 200, game.state());
    }

    if (p === '/api/new' && req.method === 'POST') {
      const body = await readBody(req);
      const seed = body.seed !== undefined && body.seed !== null ? Number(body.seed) : undefined;
      game = new Threes(seed !== undefined && Number.isFinite(seed) ? { seed } : {});
      gameId = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
      logEvent({ type: 'new', seed: game.seed });
      return json(res, 200, game.state());
    }

    if (p === '/api/undo' && req.method === 'POST') {
      const r = game.undo();
      if (!r.ok) return json(res, 400, r);
      logEvent({ type: 'undo' });
      return json(res, 200, game.state());
    }

    if (req.method === 'GET' && !p.startsWith('/api/')) return serveStatic(res, p);

    return json(res, 404, { ok: false, reason: 'not_found' });
  } catch (e) {
    return json(res, 500, { ok: false, reason: 'internal', detail: String(e && e.message || e) });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`threes-clone serving on http://${HOST === '0.0.0.0' ? 'localhost' : HOST}:${PORT}`);
  console.log(`API: GET /api/state | POST /api/move {"dir":"left"} | POST /api/new {seed} | POST /api/undo`);
});

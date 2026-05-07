import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { spawn } from 'node:child_process';
import {
  readFileSync,
  writeFileSync,
  existsSync,
  statSync,
  createReadStream,
  createWriteStream,
  mkdirSync,
  unlinkSync,
} from 'node:fs';
import { resolve, extname, join } from 'node:path';
import { createHash } from 'node:crypto';
import Anthropic from '@anthropic-ai/sdk';

const PORT = 5173;
const ROOT = resolve('.');
const DATA_DIR = resolve('./data');
const FOLLOWS = resolve(DATA_DIR, 'follows.json');
const CUTS = resolve(DATA_DIR, 'cuts.json');
const PROGRESS = resolve(DATA_DIR, 'unfollow-progress.json');
const PID_FILE = resolve(DATA_DIR, 'unfollow.pid');
const LOG_FILE = resolve(DATA_DIR, 'unfollow.log');
const SUGGESTIONS = resolve(DATA_DIR, 'llm-suggestions.json');
const ENV_FILE = resolve('./.env');
const TSX = resolve('./node_modules/.bin/tsx');

mkdirSync(DATA_DIR, { recursive: true });

// minimal .env loader (no dep)
(function loadEnv() {
  if (!existsSync(ENV_FILE)) return;
  for (const raw of readFileSync(ENV_FILE, 'utf8').split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const m = line.match(/^([A-Z0-9_]+)\s*=\s*(.*)$/i);
    if (!m) continue;
    process.env[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
})();

const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.ico': 'image/x-icon',
};

function isPidAlive(pid: number): boolean {
  try { process.kill(pid, 0); return true; } catch { return false; }
}

function currentUnfollowPid(): number | null {
  if (!existsSync(PID_FILE)) return null;
  const pid = Number(readFileSync(PID_FILE, 'utf8').trim());
  if (!pid || !isPidAlive(pid)) return null;
  return pid;
}

async function readBody(req: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  return Buffer.concat(chunks).toString('utf8');
}

function sendJson(res: ServerResponse, status: number, body: unknown) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(body));
}

type Suggestion = { cut: boolean; reason: string };
type PromptEntry = {
  prompt: string;
  goal: string;
  model: string;
  createdAt: string;
  suggestions: Record<string, Suggestion>;
};
type SuggestionsFile = Record<string, PromptEntry>;

function loadSuggestions(): SuggestionsFile {
  if (!existsSync(SUGGESTIONS)) return {};
  try { return JSON.parse(readFileSync(SUGGESTIONS, 'utf8')); } catch { return {}; }
}

function saveSuggestions(s: SuggestionsFile) {
  writeFileSync(SUGGESTIONS, JSON.stringify(s, null, 2));
}

function hashPrompt(goal: string, prompt: string): string {
  return createHash('sha256').update(goal + '\n---\n' + prompt).digest('hex').slice(0, 16);
}

type AnalysisState = {
  hash: string;
  goal: string;
  prompt: string;
  analyzed: number;
  total: number;
  running: boolean;
  error?: string;
  startedAt: string;
};

let analysis: AnalysisState | null = null;

const MODEL = 'claude-haiku-4-5';
const BATCH_SIZE = 30;

async function runAnalysis(goal: string, prompt: string) {
  const key = process.env.ANTHROPIC_API_KEY;
  if (!key) throw new Error('ANTHROPIC_API_KEY missing. Add it to .env in the project root.');
  if (!existsSync(FOLLOWS)) throw new Error('data/follows.json not found. Run `npm run sweep` first.');

  const hash = hashPrompt(goal, prompt);
  const all = JSON.parse(readFileSync(FOLLOWS, 'utf8')) as { id: string; handle: string; name: string; bio: string }[];
  const store = loadSuggestions();
  if (!store[hash]) {
    store[hash] = { prompt, goal, model: MODEL, createdAt: new Date().toISOString(), suggestions: {} };
    saveSuggestions(store);
  }
  const existing = store[hash].suggestions;
  const toAnalyze = all.filter((f) => !existing[f.id]);

  analysis = {
    hash, goal, prompt,
    analyzed: 0,
    total: toAnalyze.length,
    running: true,
    startedAt: new Date().toISOString(),
  };

  const client = new Anthropic({ apiKey: key });

  try {
    for (let i = 0; i < toAnalyze.length; i += BATCH_SIZE) {
      if (!analysis?.running) break;
      const batch = toAnalyze.slice(i, i + BATCH_SIZE);
      const results = await analyzeBatch(client, goal, prompt, batch);
      for (const r of results) {
        store[hash].suggestions[r.id] = { cut: Boolean(r.cut), reason: String(r.reason ?? '').slice(0, 80) };
      }
      saveSuggestions(store);
      analysis.analyzed = Math.min(i + BATCH_SIZE, toAnalyze.length);
    }
  } catch (err) {
    analysis.error = String(err);
    throw err;
  } finally {
    if (analysis) analysis.running = false;
  }
}

async function analyzeBatch(
  client: Anthropic,
  goal: string,
  prompt: string,
  batch: { id: string; handle: string; name: string; bio: string }[],
): Promise<{ id: string; cut: boolean; reason: string }[]> {
  const accounts = batch.map((f) => ({
    id: f.id,
    handle: f.handle,
    name: f.name,
    bio: f.bio?.slice(0, 400) ?? '',
  }));

  const systemPrompt = [
    'You help triage Twitter/X follows. For each account, decide whether the user should unfollow it based on their stated goal and cut criteria.',
    'Consider handle, name, and bio together. Do not assume facts not in the bio.',
    'Be CONSERVATIVE: when unsure, return cut: false. False positives are worse than false negatives — the user reviews every flagged account.',
    'Reason must be ≤12 words and explain specifically why this account matches the cut criteria.',
  ].join(' ');

  const userText = `User's goal: ${goal || '(not specified)'}\n\nCut criteria: ${prompt}\n\nAccounts:\n${JSON.stringify(accounts, null, 2)}`;

  const response = await client.messages.create({
    model: MODEL,
    max_tokens: 4096,
    system: systemPrompt,
    messages: [{ role: 'user', content: userText }],
    output_config: {
      format: {
        type: 'json_schema',
        schema: {
          type: 'object',
          properties: {
            results: {
              type: 'array',
              items: {
                type: 'object',
                properties: {
                  id: { type: 'string' },
                  cut: { type: 'boolean' },
                  reason: { type: 'string' },
                },
                required: ['id', 'cut', 'reason'],
                additionalProperties: false,
              },
            },
          },
          required: ['results'],
          additionalProperties: false,
        },
      },
    },
  } as unknown as Anthropic.MessageCreateParamsNonStreaming);

  const text = response.content
    .filter((b): b is Anthropic.TextBlock => b.type === 'text')
    .map((b) => b.text)
    .join('');
  try {
    const parsed = JSON.parse(text);
    if (!Array.isArray(parsed.results)) throw new Error('results is not an array');
    return parsed.results;
  } catch (err) {
    throw new Error(`Failed to parse model output: ${err}\n\nRaw: ${text.slice(0, 500)}`);
  }
}

function getAnalysisStatus() {
  return analysis ?? { running: false, analyzed: 0, total: 0, hash: null };
}

function getSuggestionsFor(hash: string) {
  const store = loadSuggestions();
  return store[hash]?.suggestions ?? {};
}

function getStatus() {
  const pid = currentUnfollowPid();
  const progress = existsSync(PROGRESS)
    ? JSON.parse(readFileSync(PROGRESS, 'utf8'))
    : { done: [], failed: [], startedAt: null, updatedAt: null };
  const cuts = existsSync(CUTS) ? JSON.parse(readFileSync(CUTS, 'utf8')) : [];
  const cutIds = new Set<string>(cuts.map((c: { id: string }) => c.id));
  const doneInThisBatch = (progress.done ?? []).filter((id: string) => cutIds.has(id));
  const failedInThisBatch = (progress.failed ?? []).filter((f: { id: string }) => cutIds.has(f.id));
  return {
    running: pid !== null,
    pid,
    processed: doneInThisBatch.length,
    failed: failedInThisBatch.length,
    total: cuts.length,
    startedAt: progress.startedAt,
    updatedAt: progress.updatedAt,
    lastFailed: failedInThisBatch.slice(-5),
    done: progress.done ?? [],
    failedIds: (progress.failed ?? []).map((f: { id: string }) => f.id),
  };
}

function startUnfollow(dryRun: boolean, perHour?: number): { ok: true; pid: number } | { ok: false; error: string } {
  if (currentUnfollowPid()) return { ok: false, error: 'Unfollow job already running.' };
  if (!existsSync(CUTS)) return { ok: false, error: 'No cuts.json — save your selections first.' };

  const args = ['unfollow.ts'];
  if (dryRun) args.push('--dry-run');

  const logStream = createWriteStream(LOG_FILE, { flags: 'a' });
  logStream.write(`\n----- ${new Date().toISOString()} — start (dryRun=${dryRun}, perHour=${perHour ?? 'default'}) -----\n`);

  const env: NodeJS.ProcessEnv = { ...process.env };
  if (perHour && perHour > 0) env.UNFOLLOW_PER_HOUR = String(perHour);

  const child = spawn(TSX, args, {
    cwd: ROOT,
    detached: true,
    stdio: ['ignore', 'pipe', 'pipe'],
    env,
  });
  child.stdout?.pipe(logStream);
  child.stderr?.pipe(logStream);
  child.on('exit', (code) => {
    logStream.write(`----- exit ${code} -----\n`);
    logStream.end();
  });
  child.unref();

  if (!child.pid) return { ok: false, error: 'Could not spawn unfollow process.' };
  writeFileSync(PID_FILE, String(child.pid));
  return { ok: true, pid: child.pid };
}

async function stopUnfollow(): Promise<{ ok: true; pid: number; forced: boolean } | { ok: false; error: string }> {
  const pid = currentUnfollowPid();
  if (!pid) return { ok: false, error: 'No unfollow job running.' };

  const killPidAndGroup = (sig: NodeJS.Signals) => {
    try { process.kill(-pid, sig); } catch { /* no group */ }
    try { process.kill(pid, sig); } catch { /* already gone */ }
  };

  killPidAndGroup('SIGTERM');

  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline && isPidAlive(pid)) {
    await new Promise((r) => setTimeout(r, 200));
  }

  let forced = false;
  if (isPidAlive(pid)) {
    killPidAndGroup('SIGKILL');
    forced = true;
    const deadline2 = Date.now() + 3000;
    while (Date.now() < deadline2 && isPidAlive(pid)) {
      await new Promise((r) => setTimeout(r, 200));
    }
  }

  try { if (existsSync(PID_FILE)) readFileSync(PID_FILE, 'utf8'); } catch {}
  try { unlinkSync(PID_FILE); } catch {}

  if (isPidAlive(pid)) return { ok: false, error: `Process ${pid} resisted SIGKILL` };
  return { ok: true, pid, forced };
}

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url ?? '/', `http://localhost:${PORT}`);
    const pathname = url.pathname;

    if (pathname === '/api/status' && req.method === 'GET') {
      return sendJson(res, 200, getStatus());
    }

    if (pathname === '/api/analyze/start' && req.method === 'POST') {
      if (analysis?.running) return sendJson(res, 409, { error: 'Analysis already running' });
      const body = await readBody(req);
      let goal = '', prompt = '';
      try {
        const parsed = JSON.parse(body || '{}');
        goal = String(parsed.goal ?? '');
        prompt = String(parsed.prompt ?? '').trim();
      } catch { return sendJson(res, 400, { error: 'invalid json' }); }
      if (!prompt) return sendJson(res, 400, { error: 'prompt is required' });
      if (!process.env.ANTHROPIC_API_KEY) {
        return sendJson(res, 400, { error: 'ANTHROPIC_API_KEY missing. Add it to a .env file in the project root (see .env.example).' });
      }
      if (!existsSync(FOLLOWS)) {
        return sendJson(res, 400, { error: 'data/follows.json not found. Run `npm run sweep` first.' });
      }
      const hash = hashPrompt(goal, prompt);
      runAnalysis(goal, prompt).catch((err) => {
        if (analysis) analysis.error = String(err?.message ?? err);
        console.error('analysis failed:', err);
      });
      return sendJson(res, 200, { hash });
    }

    if (pathname === '/api/analyze/status' && req.method === 'GET') {
      return sendJson(res, 200, getAnalysisStatus());
    }

    if (pathname === '/api/analyze/stop' && req.method === 'POST') {
      if (analysis?.running) analysis.running = false;
      return sendJson(res, 200, { stopped: true });
    }

    if (pathname === '/api/suggestions' && req.method === 'GET') {
      const hash = url.searchParams.get('hash');
      if (!hash) return sendJson(res, 400, { error: 'hash query param required' });
      return sendJson(res, 200, getSuggestionsFor(hash));
    }

    if (pathname === '/api/analyze/hash' && req.method === 'POST') {
      const body = await readBody(req);
      try {
        const { goal, prompt } = JSON.parse(body || '{}');
        return sendJson(res, 200, { hash: hashPrompt(String(goal ?? ''), String(prompt ?? '')) });
      } catch { return sendJson(res, 400, { error: 'invalid json' }); }
    }

    if (pathname === '/api/cuts' && req.method === 'POST') {
      const body = await readBody(req);
      let parsed: unknown;
      try { parsed = JSON.parse(body); }
      catch { return sendJson(res, 400, { error: 'invalid json' }); }
      if (!Array.isArray(parsed)) return sendJson(res, 400, { error: 'must be an array' });
      writeFileSync(CUTS, JSON.stringify(parsed, null, 2));
      return sendJson(res, 200, { saved: parsed.length });
    }

    if (pathname === '/api/cuts' && req.method === 'GET') {
      if (!existsSync(CUTS)) return sendJson(res, 200, []);
      return sendJson(res, 200, JSON.parse(readFileSync(CUTS, 'utf8')));
    }

    if (pathname === '/api/unfollow/start' && req.method === 'POST') {
      const body = await readBody(req);
      let dryRun = false;
      let perHour: number | undefined;
      try {
        const parsed = JSON.parse(body || '{}');
        dryRun = parsed?.dryRun === true;
        if (Number.isFinite(parsed?.perHour)) perHour = Number(parsed.perHour);
      } catch {}
      const result = startUnfollow(dryRun, perHour);
      return sendJson(res, result.ok ? 200 : 409, result);
    }

    if (pathname === '/api/unfollow/stop' && req.method === 'POST') {
      const result = await stopUnfollow();
      return sendJson(res, result.ok ? 200 : 409, result);
    }

    // static
    const rel = pathname === '/' ? '/index.html' : pathname;
    const full = join(ROOT, rel);
    if (!full.startsWith(ROOT)) { res.writeHead(403); return res.end('forbidden'); }
    if (!existsSync(full) || !statSync(full).isFile()) { res.writeHead(404); return res.end('not found'); }
    const ext = extname(full).toLowerCase();
    res.writeHead(200, { 'Content-Type': MIME[ext] ?? 'application/octet-stream', 'Cache-Control': 'no-store' });
    createReadStream(full).pipe(res);
  } catch (err) {
    sendJson(res, 500, { error: String(err) });
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`→ http://localhost:${PORT}`);
});

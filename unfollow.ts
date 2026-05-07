import { chromium, type BrowserContext, type Page } from 'playwright';
import { spawn as spawnProc } from 'node:child_process';
import { readFileSync, writeFileSync, existsSync, mkdirSync, unlinkSync } from 'node:fs';
import { resolve, dirname } from 'node:path';

const PROFILE_DIR = resolve('./data/playwright-profile');
const CUTS = resolve('./data/cuts.json');
const PROGRESS = resolve('./data/unfollow-progress.json');
const PID_FILE = resolve('./data/unfollow.pid');

let activeCtx: BrowserContext | null = null;
let shuttingDown = false;

async function gracefulExit(code: number) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`\nShutting down (code=${code})...`);
  if (activeCtx) {
    try { await activeCtx.close(); } catch (err) { console.error('ctx close err:', err); }
    activeCtx = null;
  }
  try { if (existsSync(PID_FILE)) unlinkSync(PID_FILE); } catch {}
  process.exit(code);
}

for (const sig of ['SIGTERM', 'SIGINT', 'SIGHUP'] as const) {
  process.on(sig, () => {
    console.log(`received ${sig}`);
    void gracefulExit(143);
  });
}

function keepAwake() {
  if (process.platform !== 'darwin') return;
  try {
    const c = spawnProc('caffeinate', ['-d', '-i', '-w', String(process.pid)], {
      detached: true,
      stdio: 'ignore',
    });
    c.unref();
    console.log(`→ caffeinate started (pid ${c.pid}) — display + system stay awake until unfollow exits`);
  } catch (err) {
    console.warn('caffeinate unavailable:', err);
  }
}

type Cut = { id: string; handle: string; name?: string };
type Progress = {
  done: string[];
  doneAt: Record<string, string>;
  failed: { id: string; handle: string; reason: string; at: string }[];
  startedAt: string;
  updatedAt: string;
};

const OPTS = {
  perHour: Number(process.env.UNFOLLOW_PER_HOUR ?? 200),
  dailyMax: Number(process.env.UNFOLLOW_DAILY_MAX ?? 1000),
  sessionMinutes: Number(process.env.UNFOLLOW_SESSION_MIN ?? 90),
  pauseMinutesBetweenSessions: [30, 60] as [number, number],
  jitterSecondsBetweenActions: [8, 25] as [number, number],
  allowedHours: { start: 9, end: 24 },
  dryRun: process.argv.includes('--dry-run'),
};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const randBetween = (a: number, b: number) => a + Math.random() * (b - a);

function loadProgress(): Progress {
  if (existsSync(PROGRESS)) {
    const p = JSON.parse(readFileSync(PROGRESS, 'utf8')) as Progress;
    p.doneAt ??= {};
    return p;
  }
  return {
    done: [],
    doneAt: {},
    failed: [],
    startedAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
}

function saveProgress(p: Progress) {
  p.updatedAt = new Date().toISOString();
  mkdirSync(dirname(PROGRESS), { recursive: true });
  writeFileSync(PROGRESS, JSON.stringify(p, null, 2));
}

function isAllowedHourNow() {
  const h = new Date().getHours();
  return h >= OPTS.allowedHours.start && h < OPTS.allowedHours.end;
}

async function waitUntilAllowedHour() {
  while (!isAllowedHourNow()) {
    console.log(`  outside allowed hours (${OPTS.allowedHours.start}-${OPTS.allowedHours.end}); sleeping 15 min`);
    await sleep(15 * 60_000);
  }
}

function doneInLast24h(progress: Progress): number {
  const cutoff = Date.now() - 24 * 3600_000;
  return Object.values(progress.doneAt).filter((iso) => new Date(iso).getTime() >= cutoff).length;
}

async function waitUntilUnderDailyCap(progress: Progress) {
  while (doneInLast24h(progress) >= OPTS.dailyMax) {
    const oldestRecent = Object.values(progress.doneAt)
      .map((iso) => new Date(iso).getTime())
      .filter((t) => t >= Date.now() - 24 * 3600_000)
      .sort((a, b) => a - b)[0];
    const waitMs = Math.max(60_000, (oldestRecent + 24 * 3600_000) - Date.now() + 30_000);
    const waitMin = Math.round(waitMs / 60_000);
    console.log(`  daily cap hit (${OPTS.dailyMax}/24h); sleeping ${waitMin} min until oldest rolls off`);
    await sleep(Math.min(waitMs, 30 * 60_000));
  }
}

async function unfollowOne(page: Page, cut: Cut): Promise<'ok' | 'already' | 'not-found' | 'error'> {
  const url = `https://x.com/${cut.handle}`;
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  try { await page.waitForLoadState('networkidle', { timeout: 6000 }); } catch {}
  await page.waitForTimeout(700 + Math.random() * 700);

  if (await page.locator('text=This account doesn\u2019t exist').count()) return 'not-found';
  if (await page.locator("text=This account doesn't exist").count()) return 'not-found';
  if (await page.locator('text=Account suspended').count()) return 'not-found';

  const primary = page.locator('[data-testid="primaryColumn"]').first();
  try { await primary.waitFor({ state: 'visible', timeout: 8000 }); } catch { return 'error'; }

  const followingBtn = primary.locator('[data-testid$="-unfollow"]').first();
  const followBtn = primary.locator('[data-testid$="-follow"]').first();

  const hasFollowing = await followingBtn.count();
  const hasFollow = await followBtn.count();

  if (hasFollow && !hasFollowing) return 'already';
  if (!hasFollowing) return 'error';

  if (OPTS.dryRun) {
    console.log(`  [dry-run] would unfollow @${cut.handle}`);
    return 'ok';
  }

  try {
    await followingBtn.scrollIntoViewIfNeeded({ timeout: 3000 });
    await followingBtn.click({ timeout: 5000 });
  } catch {
    await page.waitForTimeout(500);
    try { await followingBtn.click({ timeout: 5000, force: true }); } catch { return 'error'; }
  }
  await page.waitForTimeout(300 + Math.random() * 400);

  const confirm = page.locator('[data-testid="confirmationSheetConfirm"]').first();
  try {
    await confirm.waitFor({ state: 'visible', timeout: 10_000 });
    await confirm.click();
  } catch {
    const nowFollowOnly = await primary.locator('[data-testid$="-follow"]').first().count();
    const stillUnfollow = await primary.locator('[data-testid$="-unfollow"]').first().count();
    if (nowFollowOnly && !stillUnfollow) return 'ok';
    return 'error';
  }
  await page.waitForTimeout(500 + Math.random() * 500);

  const nowFollowBtn = await primary.locator('[data-testid$="-follow"]').first().count();
  return nowFollowBtn > 0 ? 'ok' : 'error';
}

async function detectLoggedIn(page: Page): Promise<boolean> {
  return (await page.locator('[data-testid="SideNav_NewTweet_Button"]').count()) > 0;
}

async function main() {
  if (!existsSync(CUTS)) {
    console.error(`Missing ${CUTS}. Export your cut list from the UI first.`);
    process.exit(1);
  }

  keepAwake();

  const cuts: Cut[] = JSON.parse(readFileSync(CUTS, 'utf8'));
  const progress = loadProgress();
  const doneSet = new Set(progress.done);
  const failCount: Record<string, number> = {};
  for (const f of progress.failed ?? []) failCount[f.id] = (failCount[f.id] ?? 0) + 1;
  const MAX_RETRIES = 3;
  const remaining = cuts.filter((c) => !doneSet.has(c.id) && (failCount[c.id] ?? 0) < MAX_RETRIES);
  const skipped = cuts.filter((c) => !doneSet.has(c.id) && (failCount[c.id] ?? 0) >= MAX_RETRIES);
  if (skipped.length) {
    console.log(`Skipping ${skipped.length} accounts that have failed ≥${MAX_RETRIES} times: ${skipped.map((c) => '@' + c.handle).join(', ')}`);
  }

  console.log(`\nTo unfollow: ${remaining.length} (of ${cuts.length} total)`);
  console.log(`Pacing: ~${OPTS.perHour}/hour, max ${OPTS.dailyMax}/day, session ≤${OPTS.sessionMinutes} min, hours ${OPTS.allowedHours.start}-${OPTS.allowedHours.end}`);
  console.log(`Overrides: UNFOLLOW_PER_HOUR, UNFOLLOW_DAILY_MAX, UNFOLLOW_SESSION_MIN`);
  if (OPTS.dryRun) console.log('DRY RUN — no unfollows will be performed.\n');
  else console.log('');

  mkdirSync(PROFILE_DIR, { recursive: true });

  const ctx: BrowserContext = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless: false,
    viewport: { width: 1280, height: 900 },
    args: ['--disable-blink-features=AutomationControlled'],
  });
  activeCtx = ctx;
  const page = ctx.pages()[0] ?? await ctx.newPage();

  await page.goto('https://x.com/home', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000);
  if (!(await detectLoggedIn(page))) {
    console.log('Not logged in. Log in manually; waiting up to 5 min...');
    const deadline = Date.now() + 5 * 60_000;
    while (Date.now() < deadline) {
      await page.waitForTimeout(2000);
      if (await detectLoggedIn(page)) break;
    }
    if (!(await detectLoggedIn(page))) { await ctx.close(); throw new Error('Login timeout'); }
  }

  let processed = 0;
  let sessionStart = Date.now();

  for (const cut of remaining) {
    await waitUntilAllowedHour();
    await waitUntilUnderDailyCap(progress);

    if ((Date.now() - sessionStart) / 60_000 >= OPTS.sessionMinutes) {
      const pauseMin = randBetween(OPTS.pauseMinutesBetweenSessions[0], OPTS.pauseMinutesBetweenSessions[1]);
      console.log(`\n— session break: pausing ${pauseMin.toFixed(0)} min —\n`);
      await sleep(pauseMin * 60_000);
      sessionStart = Date.now();
    }

    const start = Date.now();
    let result: Awaited<ReturnType<typeof unfollowOne>>;
    let failureReason = 'unfollow failed';
    try {
      result = await unfollowOne(page, cut);
    } catch (err) {
      result = 'error';
      failureReason = String(err);
    }

    if (result === 'ok' || result === 'already' || result === 'not-found') {
      progress.done.push(cut.id);
      progress.doneAt[cut.id] = new Date().toISOString();
    } else {
      progress.failed.push({ id: cut.id, handle: cut.handle, reason: failureReason, at: new Date().toISOString() });
    }
    saveProgress(progress);
    processed++;

    const elapsedSec = (Date.now() - start) / 1000;
    console.log(`  [${processed}/${remaining.length}] @${cut.handle} → ${result} (${elapsedSec.toFixed(1)}s)`);

    const hourlyTarget = 3600 / OPTS.perHour;
    const jitter = randBetween(OPTS.jitterSecondsBetweenActions[0], OPTS.jitterSecondsBetweenActions[1]);
    const wait = Math.max(jitter, hourlyTarget - elapsedSec);
    await sleep(wait * 1000);
  }

  console.log(`\n✓ Done. ${progress.done.length} unfollowed, ${progress.failed.length} failed.`);
  await ctx.close();
  activeCtx = null;
  try { if (existsSync(PID_FILE)) unlinkSync(PID_FILE); } catch {}
  try { if (existsSync(CUTS)) unlinkSync(CUTS); } catch {}
}

main().catch(async (err) => {
  console.error('Unfollow failed:', err);
  await gracefulExit(1);
});

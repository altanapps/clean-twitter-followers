import { chromium, type BrowserContext, type Page } from 'playwright';
import { writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';

const PROFILE_DIR = resolve('./data/playwright-profile');
const OUTPUT = resolve('./data/follows.json');

type FollowRecord = {
  id: string;
  handle: string;
  name: string;
  bio: string;
  followers: number;
  following: number;
  statuses: number;
  verified: boolean;
  mutual: boolean;
  avatar: string;
  position: number;
  harvestedAt: string;
};

function extractUsersFromGraphQL(json: unknown): any[] {
  const users: any[] = [];
  const walk = (node: any) => {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) { node.forEach(walk); return; }
    if (node.__typename === 'User' && node.rest_id && node.legacy) {
      users.push(node);
      return;
    }
    for (const v of Object.values(node)) walk(v);
  };
  walk(json);
  return users;
}

function toRecord(user: any, position: number): FollowRecord {
  const l = user.legacy ?? {};
  const core = user.core ?? {};
  const av = user.avatar ?? {};
  const handle = core.screen_name ?? l.screen_name ?? '';
  const name = core.name ?? l.name ?? '';
  const avatarUrl = av.image_url ?? l.profile_image_url_https ?? '';
  return {
    id: String(user.rest_id),
    handle,
    name,
    bio: (l.description ?? '').replace(/\s+/g, ' ').trim(),
    followers: Number(l.followers_count ?? 0),
    following: Number(l.friends_count ?? 0),
    statuses: Number(l.statuses_count ?? 0),
    verified: Boolean(l.verified || user.is_blue_verified || user.verification?.verified),
    mutual: Boolean(l.followed_by),
    avatar: avatarUrl.replace('_normal', '_bigger'),
    position,
    harvestedAt: new Date().toISOString(),
  };
}

async function waitForLogin(page: Page) {
  console.log('\n→ If not already logged in, log into X in the opened window.');
  console.log('  Waiting for a logged-in session (up to 5 minutes)...\n');
  const deadline = Date.now() + 5 * 60_000;
  while (Date.now() < deadline) {
    await page.waitForTimeout(2000);
    const url = page.url();
    if (url.includes('/home') || url.match(/x\.com\/[^/]+\/?$/)) {
      const hasComposer = await page.locator('[data-testid="SideNav_NewTweet_Button"]').count();
      if (hasComposer > 0) return;
    }
  }
  throw new Error('Timed out waiting for login');
}

async function detectHandle(page: Page): Promise<string> {
  const href = await page.locator('[data-testid="AppTabBar_Profile_Link"]').first().getAttribute('href');
  if (!href) throw new Error('Could not detect logged-in handle from profile link');
  return href.replace(/^\//, '').split('/')[0];
}

async function scrollUntilExhausted(page: Page, getCount: () => number) {
  let stale = 0;
  let last = getCount();
  const maxStale = 6;
  const maxScrolls = 600;
  let scrolls = 0;
  while (stale < maxStale && scrolls < maxScrolls) {
    await page.mouse.wheel(0, 2400);
    await page.waitForTimeout(900 + Math.random() * 600);
    scrolls++;
    const now = getCount();
    if (now === last) stale++;
    else { stale = 0; last = now; }
    if (scrolls % 10 === 0) console.log(`  scrolls=${scrolls} harvested=${now} stale=${stale}`);
  }
}

async function main() {
  mkdirSync(dirname(OUTPUT), { recursive: true });
  mkdirSync(PROFILE_DIR, { recursive: true });

  const ctx: BrowserContext = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless: false,
    viewport: { width: 1280, height: 900 },
    args: ['--disable-blink-features=AutomationControlled'],
  });

  const page = ctx.pages()[0] ?? await ctx.newPage();

  const byId = new Map<string, FollowRecord>();
  let nextPos = 0;

  ctx.on('response', async (response) => {
    const url = response.url();
    if (!url.includes('/graphql/') || !url.includes('Following')) return;
    if (url.includes('/Followers')) return;
    try {
      const json = await response.json();
      const users = extractUsersFromGraphQL(json);
      for (const u of users) {
        const id = String(u.rest_id);
        if (byId.has(id)) continue;
        byId.set(id, toRecord(u, nextPos++));
      }
    } catch { /* ignore non-JSON/parse errors */ }
  });

  await page.goto('https://x.com/home', { waitUntil: 'domcontentloaded' });
  await waitForLogin(page);

  const handle = await detectHandle(page);
  console.log(`→ Logged in as @${handle}`);
  console.log('→ Navigating to following list...');

  await page.goto(`https://x.com/${handle}/following`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);

  console.log('→ Scrolling to harvest (this takes a few minutes)...');
  await scrollUntilExhausted(page, () => byId.size);

  const follows = [...byId.values()].sort((a, b) => a.position - b.position);
  writeFileSync(OUTPUT, JSON.stringify(follows, null, 2));
  console.log(`\n✓ Wrote ${follows.length} follows to ${OUTPUT}`);

  await ctx.close();
}

main().catch((err) => {
  console.error('Sweep failed:', err);
  process.exit(1);
});

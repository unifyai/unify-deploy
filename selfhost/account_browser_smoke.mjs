#!/usr/bin/env node
import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const consoleRepoPath = process.env.CONSOLE_REPO_PATH;
if (!consoleRepoPath) {
  throw new Error('CONSOLE_REPO_PATH is required');
}

const requireFromConsole = createRequire(path.join(consoleRepoPath, 'package.json'));
const { chromium } = requireFromConsole('playwright');
const { encode } = requireFromConsole('next-auth/jwt');

const maxAgeSeconds = 30 * 24 * 60 * 60;

function log(kind, message) {
  console.log(`[${kind}] ${message}`);
}

function parseEnvFile(filePath) {
  const env = {};
  if (!fs.existsSync(filePath)) return env;

  for (const rawLine of fs.readFileSync(filePath, 'utf8').split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;

    const equalsIndex = line.indexOf('=');
    if (equalsIndex <= 0) continue;

    const key = line.slice(0, equalsIndex).trim();
    let value = line.slice(equalsIndex + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    env[key] = value;
  }

  return env;
}

function readJson(filePath, label) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch (error) {
    throw new Error(`${label} is not readable JSON at ${filePath}: ${error.message}`);
  }
}

function selfHostOwnerFilePath() {
  if (process.env.SELF_HOST_OWNER_FILE) return process.env.SELF_HOST_OWNER_FILE;

  const stateDir =
    process.env.SELF_HOST_STATE_DIR ||
    process.env.UNIFY_HOME ||
    path.join(os.homedir(), '.unity');
  return path.join(stateDir, 'self-host-owner.json');
}

function significantConsoleError(text) {
  return /Rendered more hooks|Application error|Hydration failed|Minified React error|Unhandled Runtime Error|Runtime Error|client-side exception|TypeError|ReferenceError/i.test(
    text
  );
}

async function launchBrowser() {
  try {
    return await chromium.launch({ headless: true });
  } catch (error) {
    if (!String(error?.message || error).includes('Executable doesn')) {
      throw error;
    }

    try {
      return await chromium.launch({ channel: 'chrome', headless: true });
    } catch {
      throw new Error(
        'Chromium is not installed for Playwright. Run `cd /Users/djl11/console && npx playwright install chromium`.'
      );
    }
  }
}

async function main() {
  const consolePort = process.env.CONSOLE_PORT || '3000';
  const consoleUrl = process.env.CONSOLE_URL || `http://localhost:${consolePort}`;
  const consoleOrigin = new URL(consoleUrl).origin;
  const consoleEnv = parseEnvFile(path.join(consoleRepoPath, '.env.local'));
  const jwtSecret = process.env.JWT_SECRET || consoleEnv.JWT_SECRET || consoleEnv.NEXTAUTH_SECRET;

  if (!jwtSecret) {
    throw new Error('JWT_SECRET is required to mint the local self-host browser session');
  }

  const ownerFile = selfHostOwnerFilePath();
  if (!fs.existsSync(ownerFile)) {
    const browser = await launchBrowser();
    try {
      const page = await browser.newPage();
      await page.goto(`${consoleOrigin}/login`, {
        waitUntil: 'domcontentloaded',
        timeout: 30_000,
      });
      await page.waitForLoadState('load', { timeout: 30_000 }).catch(() => {});
      const bodyText = await page.locator('body').innerText({ timeout: 5_000 }).catch(() => '');
      if (!/create|account|sign up|register/i.test(bodyText)) {
        throw new Error('Pre-signup login page did not show account creation copy');
      }
      log('OK', `Pre-signup browser smoke passed: ${consoleOrigin}/login`);
    } finally {
      await browser.close();
    }
    return;
  }

  const owner = readJson(ownerFile, 'Self-host owner file');
  if (!owner.userId || !owner.email) {
    throw new Error(`Self-host owner file is missing userId/email: ${ownerFile}`);
  }

  const token = await encode({
    token: {
      sub: String(owner.userId),
      email: String(owner.email),
      name: owner.name ?? null,
      picture: null,
      provider: 'credentials',
      iat: Math.floor(Date.now() / 1000),
    },
    secret: jwtSecret,
    maxAge: maxAgeSeconds,
  });

  const browser = await launchBrowser();

  try {
    const context = await browser.newContext();
    await context.addCookies([
      {
        name: `${consoleOrigin.startsWith('https:') ? '__Secure-' : ''}next-auth.session-token`,
        value: token,
        url: `${consoleOrigin}/`,
        httpOnly: true,
        sameSite: 'Lax',
        secure: consoleOrigin.startsWith('https:'),
        expires: Math.floor(Date.now() / 1000) + maxAgeSeconds,
      },
    ]);

    await context.addInitScript(() => {
      window.localStorage.setItem('console:self-host:deploy-epoch', 'stale');
      window.localStorage.setItem(
        'console:assistants:rightPaneState',
        JSON.stringify({
          primary: { tab: 'memory' },
          secondary: { tab: 'integrations' },
          splitRatio: 0.37,
        })
      );
      window.localStorage.setItem('console:assistants:info-panel-open', 'true');
      window.localStorage.setItem(
        'activePopOutCall',
        JSON.stringify({ assistantId: 'stale-assistant' })
      );
      window.sessionStorage.setItem('desktop-ready-stale-assistant', '{}');
    });

    const errors = [];
    const page = await context.newPage();
    page.on('pageerror', (error) => {
      errors.push(`pageerror: ${error.stack || error.message}`);
    });
    page.on('console', (message) => {
      if (message.type() !== 'error') return;
      const text = message.text();
      if (significantConsoleError(text)) {
        errors.push(`console.error: ${text}`);
      }
    });

    await page.goto(`${consoleOrigin}/assistants`, {
      waitUntil: 'domcontentloaded',
      timeout: 30_000,
    });
    await page.waitForLoadState('load', { timeout: 30_000 }).catch(() => {});
    await page.waitForTimeout(3_000);

    const currentUrl = new URL(page.url());
    const bodyText = await page.locator('body').innerText({ timeout: 5_000 }).catch(() => '');
    const htmlText = await page.evaluate(() => document.documentElement.innerText || '');
    const visibleText = `${bodyText}\n${htmlText}`;

    if (currentUrl.pathname.startsWith('/login')) {
      errors.push(`authenticated assistants landing redirected to ${currentUrl.pathname}`);
    }

    if (
      /Application error: a client-side exception|Rendered more hooks|Unhandled Runtime Error|Runtime Error/i.test(
        visibleText
      )
    ) {
      errors.push('Next.js runtime overlay is visible on /assistants');
    }

    if (!/\b(T-W1N|Loading workspace|Onboard)\b/.test(visibleText)) {
      errors.push('authenticated assistants landing did not render the expected UI');
    }

    if (errors.length > 0) {
      const preview = errors.slice(0, 5).join('\n');
      throw new Error(`Authenticated browser smoke failed:\n${preview}`);
    }

    await context.close();
    log('OK', `Authenticated browser smoke passed: ${consoleOrigin}/assistants`);
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  log('ERROR', error.message || String(error));
  process.exit(1);
});

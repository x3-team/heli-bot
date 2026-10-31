#!/usr/bin/env node
/**
 * Playwright over CDP against the user's Chrome or Edge.
 * stdin: one JSON command per line. stdout: one JSON result per line.
 */
import { spawn } from "node:child_process";
import { chromium } from "playwright-core";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const BROWSER_PATHS = {
  win32: {
    chrome: [
      "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
      "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    ],
    edge: [
      "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
      "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    ],
  },
  darwin: {
    chrome: ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    edge: ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
  },
  linux: {
    chrome: ["google-chrome", "chromium-browser", "chromium"],
    edge: ["microsoft-edge"],
  },
};

let browser = null;
let context = null;
let page = null;
let debugPort = null;
let child = null;

function findBinary(kind) {
  const list = BROWSER_PATHS[process.platform]?.[kind] ?? [];
  for (const candidate of list) {
    if (candidate.includes(path.sep) || candidate.includes("/")) {
      if (fs.existsSync(candidate)) return candidate;
    } else {
      return candidate;
    }
  }
  return null;
}

async function launch({ browser: kind, port }) {
  await stop({});
  const binary = findBinary(kind);
  if (!binary) {
    throw new Error("browser_binary_missing");
  }
  const userData = path.join(os.tmpdir(), `heli-${kind}-${process.pid}`);
  fs.mkdirSync(userData, { recursive: true });
  const args = [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${userData}`,
    "--no-first-run",
    "--no-default-browser-check",
    "about:blank",
  ];
  child = spawn(binary, args, { detached: true, stdio: "ignore" });
  child.unref();
  debugPort = port;
  await waitForCdp(port);
  browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
  context = browser.contexts()[0] ?? (await browser.newContext());
  page = context.pages()[0] ?? (await context.newPage());
  return { port, browser: kind };
}

async function waitForCdp(port) {
  for (let i = 0; i < 40; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (res.ok) return;
    } catch {
      // retry
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error("cdp_timeout");
}

async function attach({ port }) {
  await stop({});
  debugPort = port;
  browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
  context = browser.contexts()[0] ?? (await browser.newContext());
  page = context.pages()[0] ?? (await context.newPage());
  return { port };
}

async function navigate({ url }) {
  ensurePage();
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60_000 });
  return { url: page.url() };
}

async function click({ selector, x, y }) {
  ensurePage();
  if (selector) {
    await page.click(selector, { timeout: 15_000 });
  } else if (typeof x === "number" && typeof y === "number") {
    await page.mouse.click(x, y);
  } else {
    throw new Error("bad_click");
  }
  return { ok: true };
}

async function typeText({ text, selector }) {
  ensurePage();
  if (selector) {
    await page.fill(selector, text ?? "");
  } else {
    await page.keyboard.type(text ?? "");
  }
  return { ok: true };
}

async function screenshot() {
  ensurePage();
  const buf = await page.screenshot({ type: "png" });
  return { png_base64: buf.toString("base64"), url: page.url() };
}

async function stop() {
  try {
    if (browser) await browser.close();
  } catch {
    // ignore
  }
  browser = null;
  context = null;
  page = null;
  if (child && !child.killed) {
    try {
      child.kill();
    } catch {
      // ignore
    }
  }
  child = null;
  debugPort = null;
  return { stopped: true };
}

function ensurePage() {
  if (!page) throw new Error("not_connected");
}

async function handle(line) {
  const cmd = JSON.parse(line);
  const fn = { launch, attach, navigate, click, type: typeText, screenshot, stop }[cmd.op];
  if (!fn) throw new Error("unknown_op");
  return fn(cmd);
}

async function main() {
  const arg = process.argv[2];
  if (arg) {
    const cmd = JSON.parse(arg);
    const fn = { launch, attach, navigate, click, type: typeText, screenshot, stop }[cmd.op];
    if (!fn) throw new Error("unknown_op");
    const result = await fn(cmd);
    process.stdout.write(JSON.stringify(result));
    return;
  }
  process.stdin.setEncoding("utf8");
  let buffer = "";
  for await (const chunk of process.stdin) {
    buffer += chunk;
    let index = buffer.indexOf("\n");
    while (index >= 0) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      index = buffer.indexOf("\n");
      if (!line) continue;
      try {
        const result = await handle(line);
        process.stdout.write(`${JSON.stringify({ ok: true, result })}\n`);
      } catch (err) {
        process.stdout.write(
          `${JSON.stringify({ ok: false, error: String(err.message || err) })}\n`,
        );
      }
    }
  }
}

main().catch((err) => {
  process.stderr.write(String(err.message || err));
  process.exit(1);
});

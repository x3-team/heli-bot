"""Heli's own computer on the server: a headless Chromium per account, shown live in the app.

- The agent's browser_* tools and the ya.ru search run here (the app's local browser is not used).
- The user sees the latest frame in the «Компьютер агента» panel (GET /v1/cloud-browser/screen)
  and can click and type into it to sign in to a service or pass a captcha.
- Sign-ins persist: cookies and local storage are saved per account, encrypted with
  VAULT_MASTER_KEY, and loaded the next time that account's browser opens.

Playwright's sync API is bound to the thread that started it, so one worker thread owns the
browser and every call is queued to it. Requests to private and loopback addresses are blocked,
so a page cannot reach the API or other services on this host.

The request guard is a Playwright route handler, and the sync API runs handlers only while some
Playwright call is in progress. Between jobs the worker therefore keeps "pumping" (a short
wait_for_timeout): otherwise every request a page makes on its own — a captcha image, a form the
user submits on the live screen — would hang until the agent's next step, and the page would look
frozen. While someone watches the panel, the worker also refreshes the frame about once a second.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import files as heli_files
from app.config import get_settings
from app.web import WebError, check_url

log = logging.getLogger(__name__)

IDLE_S = 10 * 60
# All browsers taken: an account's browser unused this long gives its place to the next one.
EVICT_IDLE_S = 60
# The agent waits in line this long for a free browser.
BUSY_WAIT_S = 5 * 60
BUSY_NOTICE = "Все облачные браузеры сейчас заняты. Задача в очереди, начну, как только освободится место."
CALL_TIMEOUT_S = 60
# A worker stuck in one page this long is replaced by a new one.
RESTART_AFTER_S = 150
TIMEOUT_TEXT = (
    "Ошибка облачного браузера: tool_timeout — страница не ответила за минуту. Не повторяй то же действие: "
    "сделай browser_screenshot, открой раздел иначе или закончи и скажи, что не получилось."
)
# The agent waits this long for the user to pass a captcha on the live screen.
CAPTCHA_WAIT_S = 10 * 60
CAPTCHA_POLL_S = 2.0
# Worker between jobs: let route handlers run, refresh the frame while the panel is watched.
PUMP_S = 0.2
LIVE_FRAME_S = 1.0
WATCH_S = 12
REAP_EVERY_S = 30
# After ya.ru answers with a captcha, search through Mail.ru (Yandex results too) for a while.
YA_CAPTCHA_SKIP_S = 30 * 60
YA_SEARCH = "https://ya.ru/search/?text={q}&lr=213"
MAIL_SEARCH = "https://go.mail.ru/search?q={q}"
MAIL_WAIT_S = 12
GUARD_CACHE_S = 60
VIEWPORT = {"width": 1280, "height": 800}
JPEG_QUALITY = 60

TOOL_NAMES = (
    "browser_open",
    "browser_read",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_screenshot",
)
KEYS = {
    "Enter", "Escape", "Tab", "Backspace", "Delete", "PageDown", "PageUp", "ArrowDown", "ArrowUp",
    "ArrowLeft", "ArrowRight", "Home", "End", "Space",
    # 1С and lists: search in a list, pick a value (F4), clear it, add a row, edit a cell.
    "Control+F", "Alt+F", "F4", "Shift+F4", "Insert", "F2",
}


class CloudError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Result:
    text: str
    error: str | None = None
    frame: bytes | None = None
    files: list = field(default_factory=list)


# Same snapshot as packages/ui/src/cdp.ts, so the model reads pages the same way everywhere.
SNAPSHOT = r"""() => {
  const MAX_TEXT = 6000, MAX_ELEMENTS = 150, MAX_ROWS = 40, MAX_REPORT_ROWS = 80, BUDGET_MS = 2500;
  const t0 = performance.now();
  const late = () => performance.now() - t0 > BUDGET_MS;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    if (r.bottom < 0 || r.right < 0 || r.top > innerHeight * 3) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0";
  };
  const clean = (t) => (t || "").trim().replace(/\s+/g, " ");
  const label = (el) => {
    const secret = (el.getAttribute("type") || "").toLowerCase() === "password";
    const t = (el.getAttribute("aria-label") || el.innerText || (secret ? "" : el.value) || el.getAttribute("placeholder")
      || el.getAttribute("title") || el.getAttribute("name") || el.getAttribute("alt") || "");
    return clean(t).slice(0, 80);
  };
  const kind = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === "a") return "ссылка";
    if (tag === "button" || el.getAttribute("role") === "button") return "кнопка";
    if (tag === "select") return "список";
    if (tag === "textarea" || el.isContentEditable) return "поле";
    if (tag === "input") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (["submit", "button", "reset"].includes(type)) return "кнопка";
      if (type === "checkbox" || type === "radio") return "флажок";
      if (type === "password") return "поле пароля";
      return "поле";
    }
    return el.getAttribute("role") || "элемент";
  };
  const zOf = (el) => {
    let z = 0;
    for (let p = el; p && p !== document.body; p = p.parentElement) {
      const zi = parseInt(getComputedStyle(p).zIndex, 10);
      if (!isNaN(zi)) z = Math.max(z, zi);
    }
    return z;
  };
  document.querySelectorAll("[data-heli-ref]").forEach((el) => el.removeAttribute("data-heli-ref"));
  // 1С:Предприятие web client draws its UI with divs: sections, commands, grid cells.
  const onec = /#e1cib|\/ru_RU\//i.test(location.href) || /1С:Предприятие|1C:Enterprise/i.test(document.title)
    || !!document.querySelector("#themesCell, .themesCell, [id^=themesCell], #funcPanel, .ecsCommandBar");
  // 1С windows (period, question «Сохранить изменения?», choice forms, notices) are div.cloud[id$=win] with a
  // z-index; a modal one has a dark .surface right under it, and nothing below that surface takes a click.
  // win: the window on top; modal: the window right above the highest surface, the one that wants an answer
  // (a guide can lie over it without blocking anything).
  let win = null, winZ = -1, modal = null;
  const walls = [], wins = [];
  if (onec) {
    for (const w of document.querySelectorAll("[id$=win].cloud")) {
      if (!visible(w)) continue;
      const z = zOf(w);
      wins.push({ w, z });
      if (z > winZ) { winZ = z; win = w; }
    }
    for (const s of document.querySelectorAll(".surface.surfaceBlack")) {
      if (!visible(s)) continue;
      const z = zOf(s);
      if (!win || z < winZ) walls.push({ z, r: s.getBoundingClientRect() });
    }
    if (walls.length) {
      const top = Math.max(...walls.map((w) => w.z));
      const above = wins.filter((x) => x.z > top).sort((a, b) => a.z - b.z);
      modal = above.length ? above[0].w : null;
    }
  }
  // Under a dark surface (a tab's surface covers only that tab, the sections panel stays open).
  const blocked = (el) => {
    if (!walls.length) return false;
    const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2, z = zOf(el);
    const off = x < 0 || y < 0 || x > innerWidth || y > innerHeight;
    return walls.some((w) => z < w.z && (off || (x >= w.r.left && x <= w.r.right && y >= w.r.top && y <= w.r.bottom)));
  };
  const winTitle = (w) => {
    const box = w.querySelector(".toplineBox[data-title], [id$=headerTopLine_title]");
    return clean(box ? (box.getAttribute("data-title") || box.getAttribute("title") || box.innerText) : "").slice(0, 120);
  };
  // A 1С field is an input whose caption is a separate element (<id>#title_text), or only its id (form3_КонецПериода_i0).
  const onecName = (el) => {
    if (!el.id) return "";
    const cap = document.getElementById(el.id.replace(/_i0$/, "#title_text"));
    let n = cap ? clean(cap.innerText) : "";
    if (!n) {
      const m = /form\d+_(.+?)_i0$/.exec(el.id);
      if (m) {
        n = m[1].replace(/[0-9a-f]{6,}x.*$/i, "").replace(/\d{3,}.*$/, "").replace(/([а-яёa-z0-9])([А-ЯЁA-Z])/g, "$1 $2");
        n = n.replace(/^Поле\s+(?=\S)/, "");
        n = n.charAt(0).toUpperCase() + n.slice(1).toLowerCase();
      }
    }
    return n.replace(/\s*:$/, "").slice(0, 60);
  };
  const onecValue = (el) => {
    if ((el.getAttribute("type") || "").toLowerCase() === "password") return "";
    const v = clean(el.value);
    return /^[\s.:]*$/.test(v) ? "" : v.slice(0, 120);
  };
  const elements = [];
  const taken = new Set();
  const add = (el, k, name) => {
    if (elements.length >= MAX_ELEMENTS || taken.has(el) || !visible(el) || blocked(el)) return;
    const kk = k || kind(el);
    const field = kk === "поле" || kk === "поле пароля";
    let value = null;
    if (onec && field && el.matches("input, textarea")) {
      const own = onecName(el);
      if (own) { name = name || own; value = onecValue(el); }
    }
    if (name == null) name = label(el);
    if (!field && !name) return;
    for (let p = el.parentElement; p; p = p.parentElement) if (taken.has(p) && label(p) === name) return;
    const ref = elements.length + 1;
    taken.add(el);
    el.setAttribute("data-heli-ref", String(ref));
    const item = { ref, kind: kk, label: name };
    if (value !== null) item.value = value;
    elements.push(item);
  };
  if (onec) {
    // First what is on top: variants of a 1С field being typed into, then the open window's own close button.
    for (const li of document.querySelectorAll("#editDropDown li, .eddList li")) {
      add(li, /^\s*создать:/i.test(li.innerText || "") ? "вариант: создаст новый элемент" : "вариант");
    }
    for (const { w } of wins.sort((a, b) => b.z - a.z)) {
      const close = w.querySelector("[id$=headerTopLine_cmd_CloseButton]");
      if (close) add(close, "кнопка", winTitle(w) ? "Закрыть окно «" + winTitle(w) + "»" : "Закрыть окно");
    }
  }
  const nodes = [...document.querySelectorAll(
    "a[href], button, input:not([type=hidden]), textarea, select, [role=button], [role=link], [role=tab], [role=menuitem], [role=treeitem], [role=gridcell], [contenteditable=true]"
  )];
  if (win) nodes.sort((a, b) => (win.contains(b) ? 1 : 0) - (win.contains(a) ? 1 : 0));
  for (const el of nodes) { if (late()) break; add(el); }
  const rows = [];
  if (onec) {
    const ONEC = [
      "[id^=themesCell_theme_]", ".themeCell", ".themeTd", ".funcItem", ".functionItem", ".functionTitle",
      ".press", ".pressButton", ".frameButton", ".hpLink", ".hyperlink", ".submenuText", ".cmdText",
      ".openedItem", ".tabTitle", ".navigationItem", "[id$=_title_text]", "[id^=form][id*=Button]",
      "[id^=cmd_]", ".gridHead .gridBoxTitle"
    ].join(",");
    const own = [...document.querySelectorAll(ONEC)];
    if (win) own.sort((a, b) => (win.contains(b) ? 1 : 0) - (win.contains(a) ? 1 : 0));
    for (const el of own) {
      if (late() || elements.length >= MAX_ELEMENTS) break;
      add(el, el.matches(".openedItem, .tabTitle") ? "вкладка" : el.matches(".gridHead .gridBoxTitle") ? "колонка" : "кнопка");
    }
    // Grid rows: text of each line, cells joined with |; the row itself can be clicked by ref.
    const lines = [...document.querySelectorAll(".gridLine, .gridBody tr, [role=row]")];
    if (win) lines.sort((a, b) => (win.contains(b) ? 1 : 0) - (win.contains(a) ? 1 : 0));
    // A table part of a document (Товары, Услуги; not a list «Список»): each cell gets a ref of its own, so the
    // model can pick a cell and start editing it (F2 or typing) — a row's ref lands on whatever column is in the middle.
    const part = (line) => {
      const grid = line.closest("[id^=grid_]");
      if (!grid || /Список$/.test(grid.id) || line.closest(".gridHead")) return null;
      if (grid.querySelectorAll(".gridBody .gridLine").length > 12) return null;
      // Column by position: a header box may cover two body columns (Количество over quantity and unit).
      const names = [];
      for (const box of grid.querySelectorAll(".gridHead .gridBox")) {
        const n = clean(box.innerText);
        const r = box.getBoundingClientRect();
        if (n && r.width > 2) names.push({ n: n.slice(0, 40), l: r.left, r: r.right });
      }
      if (!names.length) return null;
      return (cell) => {
        const r = cell.getBoundingClientRect(), x = r.left + r.width / 2;
        const hit = names.find((h) => x >= h.l && x <= h.r);
        return hit ? hit.n : null;
      };
    };
    for (const line of lines) {
      if (late() || rows.length >= MAX_ROWS) break;
      if (!visible(line) || blocked(line)) continue;
      const names = part(line);
      if (names) {
        const before = elements.length;
        add(line, "строка");
        const ref = elements.length > before ? elements[elements.length - 1].ref : null;
        const cells = [], seen = new Set();
        for (const box of line.querySelectorAll(".gridBox")) {
          const col = names(box);
          if (!col || seen.has(col) || cells.length >= 12) continue;
          seen.add(col);
          const t = clean(box.innerText).slice(0, 60);
          const at = elements.length;
          add(box, "ячейка", col);
          if (elements.length > at) elements[at].inline = true;
          cells.push((elements.length > at ? "[" + elements[at].ref + "] " : "") + col + ": " + t);
        }
        const index = parseInt(line.getAttribute("rowindex"), 10);
        const number = isNaN(index) ? "" : " " + (index + 1);
        rows.push((ref ? "[" + ref + "] " : "") + "строка" + number + " — " + cells.join(" | "));
        continue;
      }
      const cells = [...line.querySelectorAll(".gridBoxText, .gridBox, td, [role=gridcell]")]
        .map((c) => clean(c.innerText || c.value)).filter(Boolean);
      const text = (cells.length ? [...new Set(cells)].join(" | ") : clean(line.innerText)).slice(0, 300);
      if (!text) continue;
      const before = elements.length;
      add(line, line.closest(".gridHead") ? "заголовок таблицы" : "строка");
      const ref = elements.length > before ? elements[elements.length - 1].ref : null;
      rows.push((ref ? "[" + ref + "] " : "") + text);
    }
  }
  // Anything else drawn as clickable (cursor: pointer) with a short caption: div-based UIs.
  if (elements.length < 40 || onec) {
    const all = document.body ? document.body.getElementsByTagName("*") : [];
    for (let i = 0; i < all.length && i < 6000; i++) {
      if (late() || elements.length >= MAX_ELEMENTS) break;
      const el = all[i];
      if (taken.has(el) || el.children.length > 3) continue;
      const own = clean(el.innerText);
      if (!own || own.length > 60) continue;
      if (getComputedStyle(el).cursor !== "pointer") continue;
      if (el.parentElement && taken.has(el.parentElement)) continue;
      add(el, "кнопка");
    }
  }
  // Values in inputs are not part of innerText: 1С forms are mostly inputs. Those with a ref show it there.
  const fields = [];
  if (onec) {
    for (const el of document.querySelectorAll("input.editInput, input[type=text], textarea")) {
      if (late() || fields.length >= 40) break;
      if (taken.has(el) || !visible(el) || blocked(el) || !onecValue(el)) continue;
      const name = onecName(el) || clean(el.getAttribute("title") || el.getAttribute("aria-label") || "");
      fields.push((name ? name + ": " : "") + onecValue(el));
    }
  }
  // A 1С report (табличный документ) is drawn in frames: corner, header, left column and body, each a set of
  // [y] rows of [x] cells. Put the cells back together by their row and column numbers.
  const report = [];
  let reportCut = false;
  if (onec && !late()) {
    for (const box of document.querySelectorAll(".moxelContainer")) {
      if (late() || !visible(box) || blocked(box)) continue;
      const grid = new Map();
      for (const frame of box.querySelectorAll("iframe")) {
        let doc = null;
        try { doc = frame.contentDocument; } catch (e) { doc = null; }
        if (!doc || !doc.body) continue;
        for (const row of doc.querySelectorAll("[y]")) {
          const y = parseInt(row.getAttribute("y"), 10);
          if (isNaN(y)) continue;
          for (const cell of row.children) {
            const x = parseInt(cell.getAttribute("x"), 10);
            if (isNaN(x)) continue;
            const t = clean(cell.innerText || cell.textContent);
            if (!t) continue;
            if (!grid.has(y)) grid.set(y, new Map());
            if (!grid.get(y).has(x)) grid.get(y).set(x, t.slice(0, 80));
          }
        }
      }
      const cols = [...new Set([...grid.values()].flatMap((line) => [...line.keys()]))].sort((a, b) => a - b);
      for (const y of [...grid.keys()].sort((a, b) => a - b)) {
        if (report.length >= MAX_REPORT_ROWS) { reportCut = true; break; }
        const line = grid.get(y);
        const cells = cols.map((x) => line.get(x) || "");
        while (cells.length && !cells[cells.length - 1]) cells.pop();
        report.push(cells.join(" | ").slice(0, 400));
      }
    }
  }
  // A window drawn over the page (1С dialogs: period, question, choice form; role=dialog elsewhere).
  // While it is open, clicks on the form behind it do nothing: the model must deal with it first.
  let dialog = null;
  if (modal) {
    const buttons = [...modal.querySelectorAll(".press, .pressButton, button, [role=button]")]
      .filter(visible).map((b) => clean(b.innerText || b.getAttribute("title") || "")).filter(Boolean);
    dialog = { title: winTitle(modal) || clean((modal.innerText || "").split("\n").find((t) => t.trim()) || "").slice(0, 120),
               buttons: [...new Set(buttons)].slice(0, 12), onec: true };
  }
  if (!dialog && !late()) {
    const BTN = /^(ок|ok|отмена|закрыть|да|нет|выбрать|сформировать|готово|применить|продолжить|сохранить|записать)$/i;
    const cands = [...document.querySelectorAll(
      "[role=dialog], [role=alertdialog], [aria-modal=true], [class*=indow], [class*=ialog], [class*=opup], [class*=odal], [class*=loud]"
    )].slice(0, 400);
    let best = null, bestZ = -1;
    for (const el of cands) {
      if (!visible(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 150 || r.height < 80 || r.width > innerWidth * 0.97 || r.height > innerHeight * 0.97) continue;
      const s = getComputedStyle(el);
      const dialogish = el.matches("[role=dialog], [role=alertdialog], [aria-modal=true]");
      if (!dialogish && s.position !== "absolute" && s.position !== "fixed") continue;
      const z = zOf(el);
      if (!dialogish && z < 10) continue;
      // 1С: a window with no dark surface under it (a guide, a notice) does not block the form.
      if (onec && el.matches("[id$=win].cloud") && !walls.length) continue;
      const buttons = [...el.querySelectorAll("button, [role=button], .press, .pressButton, .frameButton, input[type=button], input[type=submit], a")]
        .filter(visible).map((b) => clean(b.innerText || b.value || b.getAttribute("title") || "")).filter(Boolean);
      if (!dialogish && !buttons.some((b) => BTN.test(b))) continue;
      if (z > bestZ || (z === bestZ && best && best.contains(el))) {
        bestZ = z;
        best = el;
        const first = clean((el.innerText || "").split("\n").find((t) => t.trim()) || "");
        dialog = { title: first.slice(0, 120), buttons: [...new Set(buttons)].slice(0, 12) };
      }
    }
  }
  // Behind a 1С modal window the page is only noise: read the window itself.
  const source = dialog && dialog.onec ? modal : document.body;
  const text = (source ? source.innerText : "").replace(/\n{3,}/g, "\n\n").slice(0, MAX_TEXT);
  return JSON.stringify({ url: location.href, title: document.title, text, elements, rows, fields, report, reportCut, onec, dialog, partial: late() });
}"""

FIND = r"""([ref, text]) => {
  let el = ref != null ? document.querySelector('[data-heli-ref="' + ref + '"]') : null;
  const name = (e) => (e.innerText || e.value || e.getAttribute("aria-label") || e.getAttribute("title") || "").trim().replace(/\s+/g, " ").toLowerCase();
  const shown = (e) => { const r = e.getBoundingClientRect(); return r.width > 1 && r.height > 1 && getComputedStyle(e).visibility !== "hidden"; };
  const zOf = (e) => {
    let z = 0;
    for (let p = e; p && p !== document.body; p = p.parentElement) {
      const zi = parseInt(getComputedStyle(p).zIndex, 10);
      if (!isNaN(zi)) z = Math.max(z, zi);
    }
    return z;
  };
  // 1С: dark surfaces of modal windows; what is under one does not take a click.
  const walls = [...document.querySelectorAll(".surface.surfaceBlack")].filter(shown)
    .map((s) => ({ z: zOf(s), r: s.getBoundingClientRect() }));
  const blocked = (e) => {
    if (!walls.length) return false;
    const r = e.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2, z = zOf(e);
    return walls.some((w) => z < w.z && x >= w.r.left && x <= w.r.right && y >= w.r.top && y <= w.r.bottom);
  };
  // Several captions alike («Закрыть» of a window and of a tab, «Да» behind a question): the one that takes
  // the click, then the one on top.
  const best = (list) => {
    if (list.length < 2) return list[0] || null;
    const scored = list.slice(0, 30).map((e, i) => ({ e, i, b: blocked(e) ? 1 : 0, z: zOf(e) }));
    scored.sort((a, b) => a.b - b.b || b.z - a.z || a.i - b.i);
    return scored[0].e;
  };
  if (!el && text) {
    const want = text.trim().replace(/\s+/g, " ").toLowerCase();
    // «Закрыть окно «…»» is the name browser_read gives a 1С window's close button: find that window.
    const own = /^закрыть окно(?: «(.*)»)?$/.exec(want);
    if (own) {
      const wins = [...document.querySelectorAll("[id$=win].cloud")].filter(shown).sort((a, b) => zOf(b) - zOf(a));
      const title = (w) => ((w.querySelector(".toplineBox[data-title]") || { getAttribute: () => "" }).getAttribute("data-title") || "").trim().toLowerCase();
      const w = wins.find((x) => own[1] && title(x) === own[1]) || wins[0];
      el = w ? w.querySelector("[id$=headerTopLine_cmd_CloseButton]") : null;
    }
  }
  if (!el && text) {
    const want = text.trim().replace(/\s+/g, " ").toLowerCase();
    const all = [...document.querySelectorAll("[data-heli-ref], a, button, [role=button], input[type=submit], label")].filter(shown);
    const exact = all.filter((e) => name(e) === want);
    el = best(exact.length ? exact : all.filter((e) => name(e).includes(want)));
    if (!el) {
      // Div-based UIs (1С): the smallest visible elements whose text is exactly that, then ones that contain it.
      const t0 = performance.now();
      const same = [], part = [];
      const nodes = document.body ? document.body.getElementsByTagName("*") : [];
      for (let i = 0; i < nodes.length && i < 15000 && performance.now() - t0 < 2000; i++) {
        const e = nodes[i];
        if (["SCRIPT", "STYLE", "HTML", "BODY"].includes(e.tagName)) continue;
        const n = name(e);
        if (!n || n.length > want.length + 40) continue;
        if (n === want) { if (shown(e)) same.push(e); }
        else if (n.includes(want) && shown(e)) part.push(e);
      }
      const smallest = (list) => list.filter((e) => !list.some((o) => o !== e && e.contains(o)));
      el = best(smallest(same)) || best(smallest(part));
    }
  }
  if (!el) return null;
  el.scrollIntoView({ block: "center", inline: "center" });
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  // Something else drawn over that point (a 1С window over another one): the browser presses the element itself.
  const hit = document.elementFromPoint(x, y);
  const covered = !(hit && (hit === el || el.contains(hit) || hit.contains(el)));
  document.querySelectorAll("[data-heli-target]").forEach((e) => e.removeAttribute("data-heli-target"));
  el.setAttribute("data-heli-target", "1");
  return [x, y, covered, blocked(el)];
}"""

# Press the element FIND chose with events of its own: for a button another window covers.
PRESS_TARGET = r"""() => {
  const el = document.querySelector("[data-heli-target]");
  if (!el) return false;
  ["mousedown", "mouseup", "click"].forEach((t) =>
    el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window })));
  return true;
}"""

# After «Сохранить»: did the page take it? Validation errors shown, the record's id in the address.
SAVE_CHECK = r"""() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 1 && r.height > 1 && s.visibility !== "hidden" && s.display !== "none"; };
  const errors = [];
  const nodes = document.querySelectorAll(
    "[role=alert], [aria-invalid=true], [class*=error], [class*=Error], [class*=invalid], [class*=Invalid]");
  for (const e of nodes) {
    if (errors.length >= 5) break;
    if (!vis(e)) continue;
    let t = (e.innerText || "").trim().replace(/\s+/g, " ");
    if (!t && e.matches("[aria-invalid=true]")) t = "поле «" + (e.getAttribute("aria-label") || e.getAttribute("placeholder") || e.name || "?") + "» заполнено неверно";
    if (t && t.length < 200 && !errors.some((x) => x.includes(t) || t.includes(x))) errors.push(t);
  }
  const m = /[?&#/]id=([0-9a-f-]{8,})/i.exec(location.href);
  const saving = /Сохраняется|Saving\.\.\./i.test(document.body ? document.body.innerText.slice(0, 5000) : "");
  return JSON.stringify({ id: m ? m[1] : null, errors, saving });
}"""
_SAVE_BUTTON = re.compile(r"^\s*(сохранить|записать|save)\b", re.I)


ONEC_PAGE = r"""() => /1С:Предприятие|1C:Enterprise/i.test(document.title)
  || !!document.querySelector("#themesCell, .themesCell, [id^=themesCell], #funcPanel, .ecsCommandBar")"""

# Start-up windows of 1С (Путеводитель, Информация, Монитор ИТС) cover the work area: close them.
ONEC_CLOSE_START = r"""() => {
  const TITLES = [/^путеводитель/i, /^информация$/i, /^информация для/i, /монитор.*итс/i, /^начало работы/i, /^что нового/i, /^новости$/i];
  const CLOSE = ".openedClose, [id$=_close], [id$=Close], .frameClose, .closeButton, .close, [title='Закрыть'], [title^='Закрыть']";
  const fire = (el) => ["mousedown", "mouseup", "click"].forEach((t) =>
    el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window })));
  const closed = [];
  // Windows (div.cloud[id$=win]) carry their caption in data-title, not in the text: «Путеводитель по
  // демонстрационной базе», «Информация». They stack over other windows and cover their buttons.
  for (const w of document.querySelectorAll("[id$=win].cloud")) {
    const r = w.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const box = w.querySelector(".toplineBox[data-title]");
    const title = box ? (box.getAttribute("data-title") || "").trim() : "";
    if (!title || !TITLES.some((re) => re.test(title)) || closed.includes(title)) continue;
    const button = w.querySelector("[id$=headerTopLine_cmd_CloseButton]");
    if (button) { fire(button); closed.push(title); }
  }
  const t0 = performance.now();
  const nodes = document.body ? document.body.getElementsByTagName("*") : [];
  for (let i = 0; i < nodes.length && i < 20000 && performance.now() - t0 < 2000; i++) {
    const el = nodes[i];
    if (el.children.length > 2) continue;
    const text = (el.innerText || "").trim();
    if (!text || text.length > 60 || text.includes("\n") || !TITLES.some((re) => re.test(text))) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    let box = el, button = null;
    for (let up = 0; box && up < 6 && !button; up++, box = box.parentElement) button = box.querySelector(CLOSE);
    if (button && !closed.includes(text)) { fire(button); closed.push(text); }
  }
  return closed;
}"""

# Error windows of 1С that the start-window closer does not touch and that block the form: a navigation
# link that 1С did not take, the «версия конфигурации» notice, and «Перейти по ссылке» left open after a
# failed link. Closed by their own button; the result says which, so a failed link is not opened again.
ONEC_ERRORS = r"""() => {
  const RULES = [
    { kind: "nav", re: /неверный формат навигационной ссылки/i },
    { kind: "nav", re: /не удалось перейти по навигационной ссылке/i },
    { kind: "nav", re: /навигационн\S* ссылк\S* (?:не найден|не существует|недоступн)/i },
    { kind: "version", re: /версия конфигурации\s.{0,80}выпущена/i },
    { kind: "navwin", re: /^(?:переход по (?:навигационной )?ссылке|перейти по (?:навигационной )?ссылке)$/i },
  ];
  const ORDER = { nav: ["ок", "ok", "закрыть", "нет", "отмена"], version: ["ок", "ok", "закрыть", "нет"], navwin: ["отмена", "закрыть"] };
  const BUTTONS = "button, [role=button], .press, .pressButton, .frameButton, input[type=button], input[type=submit], a, [id^=form][id*=Button], [id*=_cmd_]";
  const CLOSE = ".openedClose, [id$=_close], [id$=Close], .frameClose, .closeButton, .close, [title='Закрыть'], [title^='Закрыть']";
  const clean = (t) => (t || "").replace(/\s+/g, " ").trim();
  const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 1 && r.height > 1 && s.visibility !== "hidden" && s.display !== "none"; };
  const fire = (el) => ["mousedown", "mouseup", "click"].forEach((t) =>
    el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window })));
  const out = [];
  const done = new Set();
  const t0 = performance.now();
  const nodes = document.body ? [...document.body.getElementsByTagName("*")] : [];
  for (const rule of RULES) {
    if (rule.kind === "navwin" && !out.some((o) => o.kind === "nav")) continue; // a working link closes it itself
    for (let i = 0; i < nodes.length && i < 20000 && performance.now() - t0 < 2500; i++) {
      const el = nodes[i];
      if (el.children.length > 3 || done.has(el)) continue;
      const text = clean(el.innerText);
      if (!text || text.length > 400 || !rule.re.test(text) || !visible(el)) continue;
      let box = el, button = null;
      for (let up = 0; box && box !== document.body && up < 12 && !button; up++, box = box.parentElement) {
        const r = box.getBoundingClientRect();
        if (r.width > innerWidth * 0.97 && r.height > innerHeight * 0.97) break;
        const cands = [...box.querySelectorAll(BUTTONS)].filter(visible);
        for (const want of ORDER[rule.kind]) {
          button = cands.find((b) => clean(b.innerText || b.value || b.getAttribute("title")).toLowerCase() === want);
          if (button) break;
        }
        if (!button) button = [...box.querySelectorAll(CLOSE)].find(visible) || null;
      }
      done.add(el);
      if (out.some((o) => o.text === text)) continue;
      if (button) fire(button);
      out.push({ kind: rule.kind === "navwin" ? "nav" : rule.kind, text: text.slice(0, 200), closed: !!button });
    }
  }
  return out;
}"""

# 1С has drawn either its sign-in form or its desktop (sections panel), not the loading screen in between.
ONEC_READY = r"""() => {
  const shown = (e) => { const r = e.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  if ([...document.querySelectorAll("input[type=password]")].some(shown)) return true;
  if ([...document.querySelectorAll("#themesCell, [id^=themesCell_theme_], #funcPanel")].some(shown)) return true;
  return (document.body ? document.body.innerText.trim().length : 0) > 300;
}"""

# Cheap check after a click in 1С: is there a start window or a notice the closers above would take?
ONEC_NEEDS_CLEANUP = r"""() => {
  const w = [...document.querySelectorAll("[id$=win].cloud")].some((e) => { const r = e.getBoundingClientRect(); return r.width > 1 && r.height > 1; });
  const t = document.body ? document.body.innerText : "";
  return w || /версия конфигурации|монитор портала|путеводитель|навигационн\S* ссылк/i.test(t);
}"""

# What is under a point: the label the guard checks before a click by coordinates.
AT_POINT = r"""([x, y]) => {
  let el = document.elementFromPoint(x, y);
  for (let i = 0; el && i < 4; i++, el = el.parentElement) {
    const t = (el.innerText || el.value || el.getAttribute("aria-label") || el.getAttribute("title") || "").trim().replace(/\s+/g, " ");
    if (t) return t.slice(0, 80);
  }
  return "";
}"""

CAPTCHA = r"""() => /showcaptcha|checkcaptcha|captcha/i.test(location.pathname)
  || /Подтвердите, что запросы отправляли вы|Я не робот|SmartCaptcha/i.test(document.body ? document.body.innerText.slice(0, 3000) : "")
  || [...document.querySelectorAll("iframe")].some((f) => {
       // A visible captcha widget in a frame (Yandex SmartCaptcha on the Битрикс24 sign-in, hCaptcha,
       // a reCAPTCHA checkbox). Invisible reCAPTCHA badges do not count.
       const src = f.src || "";
       if (!/smartcaptcha|captcha/i.test(src) || /size=invisible/i.test(src)) return false;
       const r = f.getBoundingClientRect(), s = getComputedStyle(f);
       return r.width >= 60 && r.height >= 40 && s.visibility !== "hidden" && s.display !== "none";
     })"""

# Organic results from a Yandex results page; skips ads and Yandex's own services. Same as cdp.ts.
SERP = r"""() => {
  const out = [], seen = new Set();
  const items = document.querySelectorAll("li.serp-item, .serp-item, [data-cid]");
  const pick = (root) => {
    if (root.querySelector(".label_theme_direct, [data-fast-name='direct'], .AdvLabel")) return;
    const a = root.querySelector("a.OrganicTitle-Link, a.organic__url, h2 a, a[href^='http']");
    if (!a) return;
    let href = a.href;
    try { const u = new URL(href); if (/(^|\.)yandex\.|(^|\.)ya\.ru$/.test(u.hostname)) return; } catch { return; }
    if (seen.has(href)) return;
    seen.add(href);
    const title = (root.querySelector("h2, .OrganicTitle-LinkText") || a).innerText.trim().replace(/\s+/g, " ");
    const snip = root.querySelector(".OrganicTextContentSpan, .organic__content-wrapper, .text-container, .Organic-ContentWrapper");
    out.push({ title: title.slice(0, 200), url: href, snippet: (snip ? snip.innerText : "").trim().replace(/\s+/g, " ").slice(0, 300) });
  };
  items.forEach((el) => out.length < 8 && pick(el));
  return JSON.stringify(out);
}"""


ONEC_HINT = (
    "(Это 1С. Быстрее всего — browser_open с навигационной ссылкой <база>/ru_RU/#e1cib/list/<Объект> или "
    "#e1cib/app/Отчет.<Имя>. Поле формы: browser_type с ref поля; в поле-ссылке (контрагент, счёт, номенклатура) "
    "после ввода выбери «вариант» из списка или нажми Tab. Табличная часть (Товары): «Добавить» или клик по "
    "ячейке, затем browser_press F2 — появится поле ячейки, в него browser_type, Tab — следующая ячейка. Цифры отчёта — в разделе «Отчёт» после «Сформировать». "
    "Поиск в списке: browser_press Control+F, затем browser_type без ref. Если нужного "
    "нет в списке элементов, сделай browser_screenshot и нажми browser_click по координатам x, y с картинки.)"
)


ONEC_LINK_FAILED = (
    "nav_link_failed=true: навигационная ссылка {link} не открылась ({why}). Окно ошибки закрыто. "
    "Не открывай эту ссылку снова. Один раз можно попробовать соседнее имя объекта из подсказки 1С, "
    "иначе иди через меню: раздел слева → нужный список или отчёт, или ☰ → «Все функции»."
)


ONEC_GO_FAILED = (
    "Навигационную ссылку не открыл: окно «Перейти по ссылке» (Shift+F11) не появилось, скорее всего поверх "
    "открыто другое окно. Базу не перезагружал: это сбросило бы вход в 1С. Ответь в открытом окне или закрой его, "
    "потом открой ссылку снова, или иди через меню раздела."
)


DIALOG_NOTE = (
    "Поверх страницы открыто окно «{title}»{buttons}. Пока оно открыто, клики по форме за ним не работают: "
    "сначала ответь в нём (нужная кнопка) или закрой его («Закрыть», «Отмена» или browser_press Escape)."
)


def format_snapshot(snap: dict) -> str:
    lines = [f"Адрес: {snap.get('url', '')}", f"Заголовок: {snap.get('title', '')}", ""]
    dialog = snap.get("dialog")
    if isinstance(dialog, dict) and (dialog.get("title") or dialog.get("buttons")):
        buttons = dialog.get("buttons") or []
        shown = (", кнопки: " + ", ".join(f"«{b}»" for b in buttons)) if buttons else ""
        lines[2:2] = [DIALOG_NOTE.format(title=dialog.get("title") or "окно", buttons=shown), ""]
    if isinstance(dialog, dict) and dialog.get("onec"):
        lines.append("Текст окна (форма за ним скрыта, пока окно открыто):")
    lines.append((snap.get("text") or "").strip() or "(текста нет)")
    if snap.get("fields"):
        lines += ["", "Поля формы:", *snap["fields"]]
    if snap.get("rows"):
        lines += ["", "Таблица (строки, ячейки через |):", *snap["rows"]]
    if snap.get("report"):
        lines += ["", "Отчёт (табличный документ 1С, строки сверху вниз, ячейки через |):", *snap["report"]]
        if snap.get("reportCut"):
            lines.append("(Отчёт длиннее: показано начало. Сузь отбор или период, если нужной строки нет.)")
    elements = snap.get("elements") or []
    if elements:
        lines += ["", "Элементы:"]
        for el in elements:
            if el.get("inline"):
                continue  # a table cell: its ref is in the table above
            label = f" «{el['label']}»" if el.get("label") else ""
            if "value" in el:
                label += f" = «{el['value']}»" if el["value"] else " (пусто)"
            lines.append(f"[{el['ref']}] {el['kind']}{label}")
    if snap.get("partial"):
        lines += ["", "(Страница большая: прочитана не целиком.)"]
    if snap.get("onec"):
        lines += ["", ONEC_HINT]
    return "\n".join(lines)


def _hits_or_empty(hits: list[dict] | None) -> str:
    return format_hits(hits) if hits else ""


def format_hits(hits: list[dict]) -> str:
    if not hits:
        return "Ничего не найдено."
    return "\n\n".join(f"{i}. {h['title']}\n{h['url']}\n{h.get('snippet', '')}" for i, h in enumerate(hits, 1))


_guard_cache: dict[tuple, tuple[float, bool]] = {}


def request_allowed_cached(url: str) -> bool:
    """request_allowed, remembered per scheme+host+port for a minute: a page loads dozens of
    resources from the same hosts, and each check is a DNS lookup on the browser's only thread."""
    parts = urlsplit(url)
    if parts.scheme.lower() in {"data", "blob", "about"}:
        return True
    try:
        key = (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port, bool(parts.username or parts.password))
    except ValueError:
        return False
    now = time.monotonic()
    hit = _guard_cache.get(key)
    if hit and now - hit[0] < GUARD_CACHE_S:
        return hit[1]
    allowed = request_allowed(url)
    if len(_guard_cache) > 2000:
        _guard_cache.clear()
    _guard_cache[key] = (now, allowed)
    return allowed


def request_allowed(url: str, resolver=None) -> bool:
    """Pages may load anything public; nothing on this host or its private network."""
    scheme = urlsplit(url).scheme.lower()
    if scheme in {"data", "blob", "about"}:
        return True
    if scheme not in {"http", "https", "ws", "wss"}:
        return False
    if scheme in {"ws", "wss"}:
        url = "http" + url[2:]
    try:
        check_url(url, **({"resolver": resolver} if resolver else {}))
    except WebError:
        return False
    return True


# --- saved sign-ins -------------------------------------------------------------------


def _key() -> bytes | None:
    raw = os.environ.get("VAULT_MASTER_KEY", "").strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except binascii.Error:
        return None
    return key if len(key) == 32 else None


class ProfileStore:
    """Cookies and local storage per account, AES-GCM sealed on disk. No key: nothing is kept."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or get_settings().cloud_browser_profiles)

    def _path(self, user_id: uuid.UUID) -> Path:
        return self.root / f"{uuid.UUID(str(user_id))}.bin"

    def load(self, user_id: uuid.UUID) -> dict | None:
        key, path = _key(), self._path(user_id)
        if key is None or not path.exists():
            return None
        try:
            blob = path.read_bytes()
            return json.loads(AESGCM(key).decrypt(blob[:12], blob[12:], str(user_id).encode()))
        except Exception:  # noqa: BLE001
            log.info("cloud browser profile unreadable, starting clean")
            return None

    def save(self, user_id: uuid.UUID, state: dict) -> bool:
        key = _key()
        if key is None:
            return False
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        nonce = os.urandom(12)
        blob = nonce + AESGCM(key).encrypt(nonce, json.dumps(state).encode(), str(user_id).encode())
        path = self._path(user_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        return True

    def forget(self, user_id: uuid.UUID) -> None:
        self._path(user_id).unlink(missing_ok=True)

    def exists(self, user_id: uuid.UUID) -> bool:
        return self._path(user_id).exists()


# --- one account's browser (worker thread only) -------------------------------------


class Page:
    def __init__(self, context, page) -> None:
        self.context = context
        self.page = page
        self.last_used = time.monotonic()
        # Downloads the page started; the browser moves them to the file store after each call.
        self.downloads: list = []
        if page is not None:
            page.on("download", lambda download: self.downloads.append(download))

    def frame(self) -> bytes | None:
        try:
            return self.page.screenshot(type="jpeg", quality=JPEG_QUALITY, timeout=10_000)
        except Exception:  # noqa: BLE001
            return None

    def state(self) -> dict | None:
        try:
            return self.context.storage_state()
        except Exception:  # noqa: BLE001
            return None

    def location(self) -> tuple[str, str]:
        try:
            return self.page.url, self.page.title()
        except Exception:  # noqa: BLE001
            return "", ""

    def read(self) -> str:
        return format_snapshot(json.loads(self.page.evaluate(SNAPSHOT)))

    def goto(self, url: str) -> None:
        try:
            target = check_url(url)
        except WebError as exc:
            raise CloudError(exc.code) from None
        try:
            self.page.goto(target, wait_until="domcontentloaded", timeout=25_000)
        except Exception as exc:  # noqa: BLE001
            # A direct link to a file: Chromium downloads it instead of opening a page
            # ("Download is starting" or net::ERR_ABORTED, then a download event).
            message = str(exc)
            if "Download is starting" not in message and "ERR_ABORTED" not in message:
                raise CloudError("navigation_failed") from exc
            self.page.wait_for_timeout(700)
            if not getattr(self, "downloads", None):
                raise CloudError("navigation_failed") from exc
            self.opened_file = True
            return
        self.settle(1500)

    def open(self, url: str) -> str:
        self.opened_file = False
        notes: list[str] = []
        link = onec_link(url)
        if link:
            url = onec_clean_link(url)
            failed = getattr(self, "failed_onec_links", None)
            if failed is None:
                failed = self.failed_onec_links = set()
            key = onec_link_key(url)
            if key in failed and self.is_onec():
                # The model keeps opening a link 1С already refused (1c-08: three times): send it to the menu.
                return ONEC_LINK_FAILED.format(link=key, why="уже не открылась в этом сеансе") + "\n\n" + self.read()
        if link and same_base(self.page.url, url) and self.is_onec():
            # 1С is open: changing only the #e1cib hash does not open the form. «Перейти по ссылке» does.
            self.onec_wait_ready()
            opened = False
            for attempt in range(2):
                # Start windows and the version notice right after sign-in swallow Shift+F11: close them first.
                if attempt:
                    self.page.wait_for_timeout(1500)
                pre = self.onec_cleanup()
                if pre:
                    notes.append(pre.strip())
                if self.onec_go(url):
                    opened = True
                    break
            if opened:
                notes.append("Открыл навигационную ссылку 1С через «Перейти по ссылке» (Shift+F11).")
            elif self.onec_signed_in():
                # A reload ends the 1С session: the model is back at the sign-in form (1c-06).
                return ONEC_GO_FAILED + "\n\n" + self.read()
            else:
                # Fallback: load the base again with the link, so 1С reads it on start.
                try:
                    self.page.goto("about:blank", timeout=5_000)
                except Exception:  # noqa: BLE001
                    pass
                self.goto(url)
                notes.append("Перезагрузил базу 1С с навигационной ссылкой.")
        else:
            self.goto(url)
            if self.opened_file:
                # The tab still shows the previous page; reading it would make the model think the link failed.
                return "По этому адресу файл, а не страница: браузер его скачал. Вкладка осталась на прежней странице."
        if self.is_onec():
            self.settle(1500)
            closed = self.close_start_windows()
            if closed:
                notes.append("Закрыл стартовые окна 1С: " + ", ".join(f"«{t}»" for t in closed) + ".")
            errors = self.close_error_windows()
            versions = [e for e in errors if e["kind"] == "version"]
            if versions:
                notes.append("Закрыл окно 1С о версии конфигурации.")
            nav = [e for e in errors if e["kind"] == "nav"]
            if nav and link:
                key = onec_link_key(url)
                self.failed_onec_links.add(key)
                notes.insert(0, ONEC_LINK_FAILED.format(link=key, why="1С ответила «" + nav[0]["text"][:120] + "»"))
        return ("\n".join(notes) + "\n\n" if notes else "") + self.read()

    def close_error_windows(self) -> list[dict]:
        """Close 1С error windows (bad navigation link, version notice). Escape if an error had no button."""
        found: list[dict] = []
        for _ in range(3):
            try:
                batch = self.page.evaluate(ONEC_ERRORS) or []
            except Exception:  # noqa: BLE001
                break
            batch = [e for e in batch if isinstance(e, dict) and e.get("text") not in {f["text"] for f in found}]
            if not batch:
                break
            found += batch
            if not all(e.get("closed") for e in batch):
                try:
                    self.page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
            self.settle(700)
        return found

    def is_onec(self) -> bool:
        try:
            return onec_link(self.page.url) or bool(self.page.evaluate(ONEC_PAGE))
        except Exception:  # noqa: BLE001
            return False

    def onec_go(self, url: str) -> bool:
        """Shift+F11 «Перейти по ссылке», type the e1cib link, Enter. False: the dialog did not show up."""
        link = onec_clean_link(url)
        link = link[link.lower().index("e1cib/"):]
        try:
            self.page.keyboard.press("Shift+F11")
            self.page.wait_for_timeout(900)
            focused = self.page.evaluate(
                "() => { const e = document.activeElement; return e && (e.tagName === 'INPUT' || e.tagName === 'TEXTAREA') ? (e.value || '') + '|ok' : ''; }"
            )
            if not focused:
                return False
            # 1С keeps the last link in this field: clear it, or the new one is glued on («Неверный формат…»).
            self.page.evaluate(
                "() => { const e = document.activeElement; if (e && 'value' in e) { e.value = '';"
                " e.dispatchEvent(new Event('input', { bubbles: true })); } }"
            )
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Delete")
            self.page.keyboard.insert_text(link)
            typed = self.page.evaluate("() => { const e = document.activeElement; return e && 'value' in e ? e.value : null; }")
            if isinstance(typed, str) and typed.strip() != link:
                self.page.evaluate(
                    "(v) => { const e = document.activeElement; e.value = v; e.dispatchEvent(new Event('input', { bubbles: true })); }",
                    link,
                )
            self.page.keyboard.press("Enter")
            self.settle(2500)
            return True
        except Exception:  # noqa: BLE001
            return False

    def close_start_windows(self) -> list[str]:
        closed: list[str] = []
        for _ in range(2):
            try:
                found = self.page.evaluate(ONEC_CLOSE_START) or []
            except Exception:  # noqa: BLE001
                break
            if not found:
                break
            closed += [t for t in found if t not in closed]
            self.settle(700)
        return closed

    def click(self, ref: int | None, text: str | None) -> str:
        point = self.page.evaluate(FIND, [ref, text])
        if not point:
            raise CloudError("element_not_found")
        covered = len(point) > 2 and bool(point[2])
        if len(point) > 3 and point[3] and self.is_onec():
            # Behind a modal 1С window: the click would do nothing, and the model would think it did.
            return (
                f"Не нажал «{text or ref}»: эта кнопка за открытым окном 1С. Сначала ответь в окне "
                "или закрой его.\n\n" + self.read()
            )
        under = str(self.page.evaluate(AT_POINT, [point[0], point[1]]) or "") or (text or "")
        if covered and self.is_onec():
            # Another 1С window lies over the button (a guide over the version notice): press the button itself.
            under = text or under
            self.page.evaluate(PRESS_TARGET)
        else:
            self.page.mouse.click(point[0], point[1])
        self.settle(1200)
        return self.onec_cleanup() + self.save_report(under) + self.read()

    def onec_signed_in(self) -> bool:
        try:
            return not self.page.evaluate(
                "() => [...document.querySelectorAll('input[type=password]')].some((e) => {"
                " const r = e.getBoundingClientRect(); return r.width > 1 && r.height > 1; })"
            )
        except Exception:  # noqa: BLE001
            return False

    def onec_wait_ready(self, seconds: int = 15) -> bool:
        """Right after «Войти» 1С draws its desktop for several seconds; a link opened or a key pressed then
        is lost, and a reload in that time drops the session (1c-06: back to the sign-in form)."""
        for _ in range(seconds):
            try:
                if self.page.evaluate(ONEC_READY):
                    return True
            except Exception:  # noqa: BLE001
                return False
            self.page.wait_for_timeout(1000)
        return False

    def onec_cleanup(self) -> str:
        """After an action in 1С: close start windows and the «версия конфигурации» notice that came up
        (they come right after «Войти»), so the model does not spend its steps on them."""
        try:
            if not self.is_onec():
                return ""
            self.onec_wait_ready()
            if not self.page.evaluate(ONEC_NEEDS_CLEANUP):
                return ""
        except Exception:  # noqa: BLE001
            return ""
        notes = []
        closed = self.close_start_windows()
        if closed:
            notes.append("Закрыл стартовые окна 1С: " + ", ".join(f"«{t}»" for t in closed) + ".")
        if any(e["kind"] == "version" for e in self.close_error_windows()):
            notes.append("Закрыл окно 1С о версии конфигурации.")
        return "\n".join(notes) + "\n\n" if notes else ""

    def save_report(self, label: str) -> str:
        """After a «Сохранить» click: say whether the page took it, so the model does not report a lost form."""
        if not _SAVE_BUTTON.search(label or ""):
            return ""
        try:
            for _ in range(8):
                check = json.loads(self.page.evaluate(SAVE_CHECK))
                if not check.get("saving"):
                    break
                self.page.wait_for_timeout(750)
        except Exception:  # noqa: BLE001
            return ""
        if check.get("errors"):
            return (
                "После «Сохранить» на странице ошибки: " + "; ".join(check["errors"])
                + ". Запись НЕ сохранена: исправь эти поля и нажми «Сохранить» снова.\n\n"
            )
        if check.get("saving"):
            return "После «Сохранить» страница всё ещё сохраняет: перечитай её и проверь, что запись сохранилась.\n\n"
        if check.get("id"):
            return f"После «Сохранить» ошибок нет, id записи в адресе: {check['id']}.\n\n"
        return "После «Сохранить» ошибок не видно, но id в адресе нет: проверь, что запись сохранилась.\n\n"

    def click_at(self, x: float, y: float, confirmed: bool) -> str:
        """A click by coordinates from the screenshot. The loop's guard cannot see what is there, so the
        browser checks: a button that sends, pays or deletes needs the confirmed click by its text."""
        if not (0 <= x <= VIEWPORT["width"] and 0 <= y <= VIEWPORT["height"]):
            raise CloudError("bad_point")
        under = str(self.page.evaluate(AT_POINT, [x, y]) or "")
        if not confirmed and risky_label(under):
            raise CloudError("needs_confirmation:" + under[:60])
        self.page.mouse.click(x, y)
        self.settle(1200)
        head = f"Нажал в точке ({int(x)}, {int(y)})" + (f" на «{under}»" if under else "") + ".\n\n"
        return head + self.onec_cleanup() + self.save_report(under) + self.read()

    def type(self, ref: int | None, text: str, submit: bool) -> str:
        secret = False
        if ref is not None:
            handle = self.page.query_selector(f'[data-heli-ref="{int(ref)}"]')
            if handle is None:
                raise CloudError("element_not_found")
            secret = (handle.get_attribute("type") or "").lower() == "password"
            handle.scroll_into_view_if_needed(timeout=5_000)
            try:
                handle.click(timeout=5_000)
            except Exception:  # noqa: BLE001
                # Covered by another 1С window or a hint over the field: focus it directly.
                handle.focus()
            self.page.keyboard.press("Control+A")
        onec = not secret and self.is_onec()
        if onec and len(text) <= 200:
            # 1С fills its choice list (контрагент, счёт, номенклатура) from key presses; pasted text gets
            # a stale list or none.
            # Key by key races with 1С rewriting the field while it looks up variants («шРома» for «Ромаш»):
            # paste all but the last character and type that one.
            self.page.wait_for_timeout(300)
            self.page.keyboard.press("Delete")
            if len(text) > 1:
                self.page.keyboard.insert_text(text[:-1])
            if text:
                self.page.keyboard.type(text[-1], delay=30)
        else:
            self.page.keyboard.insert_text(text)
        if submit:
            self.page.keyboard.press("Enter")
            self.settle(1500)
            return self.onec_cleanup() + self.read()
        if onec:
            # Show the field and the variants 1С offers for it. Not for passwords: never echo those.
            self.settle(900)
            return "Текст введён.\n\n" + self.read()
        # The typed value may be a password: never echo it back to the model.
        return "Текст введён."

    def press(self, key: str) -> str:
        if key not in KEYS:
            raise CloudError("bad_key")
        self.page.keyboard.press(" " if key == "Space" else key)
        self.settle(400)
        return f"Нажата клавиша {key}."

    def upload(self, payload: dict, ref: int | None, text: str | None) -> str:
        """Give a file to the page's file input: the field itself, a button that opens the picker,
        or, failing both, the first file input on the page (they are often hidden behind a button)."""
        handle = self.page.query_selector(f'[data-heli-ref="{int(ref)}"]') if ref is not None else None
        is_input = bool(handle and handle.evaluate("(e) => e.tagName === 'INPUT' && e.type === 'file'"))
        done = False
        if is_input:
            handle.set_input_files(payload)
            done = True
        elif ref is not None or text:
            point = self.page.evaluate(FIND, [ref, text])
            if point:
                try:
                    with self.page.expect_file_chooser(timeout=5_000) as chooser:
                        self.page.mouse.click(point[0], point[1])
                    chooser.value.set_files(payload)
                    done = True
                except Exception:  # noqa: BLE001
                    done = False
        if not done:
            fallback = self.page.query_selector("input[type=file]")
            if fallback is None:
                raise CloudError("no_file_input")
            fallback.set_input_files(payload)
        self.settle(1000)
        return f"Файл «{payload['name']}» выбран в форме.\n\n" + self.read()

    def take_downloads(self) -> list:
        pending = getattr(self, "downloads", None) or []
        taken = list(pending)
        pending.clear()
        return taken

    def is_captcha(self) -> bool:
        try:
            return bool(self.page.evaluate(CAPTCHA))
        except Exception:  # noqa: BLE001
            return False

    def serp(self) -> list[dict]:
        return json.loads(self.page.evaluate(SERP))

    def mail_search(self, query: str) -> list[dict] | None:
        """Search on go.mail.ru. Its results are Yandex's, shown in a partner frame that does not ask
        the server's address for a captcha the way ya.ru does. None: no results frame showed up."""
        self.goto(MAIL_SEARCH.format(q=quote(query)))
        deadline = time.monotonic() + MAIL_WAIT_S
        consent = False
        while time.monotonic() < deadline:
            if not consent:
                # Cookie banner on mail.ru: accept it once, the choice is kept with the sign-ins.
                try:
                    button = self.page.get_by_role("button", name="Принять")
                    if button.count():
                        button.first.click(timeout=3_000)
                        consent = True
                except Exception:  # noqa: BLE001
                    pass
            for frame in self.page.frames:
                if "yandex.ru/search" not in frame.url:
                    continue
                try:
                    if frame.evaluate(CAPTCHA):
                        return None
                    if frame.evaluate("() => document.querySelectorAll('li.serp-item, .serp-item').length"):
                        self.page.wait_for_timeout(300)
                        return json.loads(frame.evaluate(SERP))
                except Exception:  # noqa: BLE001
                    continue
            self.page.wait_for_timeout(500)
        return None

    def pump(self) -> None:
        """Give Playwright a moment to run route handlers and page events."""
        try:
            self.page.wait_for_timeout(20)
        except Exception:  # noqa: BLE001
            pass

    # The user's own hands in the panel.
    def user_input(self, action: dict) -> None:
        kind = action.get("kind")
        if kind == "click":
            x = min(max(float(action.get("x", 0)), 0.0), 1.0) * VIEWPORT["width"]
            y = min(max(float(action.get("y", 0)), 0.0), 1.0) * VIEWPORT["height"]
            self.page.mouse.click(x, y)
            self.settle(600)
        elif kind == "text":
            self.page.keyboard.insert_text(str(action.get("text") or "")[:2000])
            self.page.wait_for_timeout(150)
        elif kind == "key":
            key = str(action.get("key") or "")
            if key not in KEYS:
                raise CloudError("bad_key")
            self.page.keyboard.press(" " if key == "Space" else key)
            self.settle(400)
        elif kind == "scroll":
            self.page.mouse.wheel(0, max(min(float(action.get("dy", 0)), 3000), -3000))
            self.page.wait_for_timeout(300)
        elif kind == "open":
            self.goto(str(action.get("url") or ""))
        elif kind == "back":
            try:
                self.page.go_back(wait_until="domcontentloaded", timeout=15_000)
            except Exception:  # noqa: BLE001
                pass
            self.settle(500)
        elif kind != "refresh":
            raise CloudError("bad_input")

    def settle(self, ms: int) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=ms + 3000)
        except Exception:  # noqa: BLE001
            pass
        self.page.wait_for_timeout(ms)

    def close(self) -> None:
        try:
            self.context.close()
        except Exception:  # noqa: BLE001
            pass
        browser = getattr(self, "browser", None)
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


def run_tool(page: Page, tool: str, args: dict) -> str:
    ref = args.get("ref") if isinstance(args.get("ref"), int) and not isinstance(args.get("ref"), bool) else None
    if tool == "browser_open":
        return page.open(str(args.get("url") or ""))
    if tool == "browser_read":
        return page.onec_cleanup() + page.read()
    if tool == "browser_click":
        text = args.get("text") if isinstance(args.get("text"), str) else None
        x, y = args.get("x"), args.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool):
            return page.click_at(float(x), float(y), args.get("_confirmed") is True)
        if ref is None and not text:
            raise CloudError("element_not_found")
        return page.click(ref, text)
    if tool == "browser_type":
        return page.type(ref, str(args.get("text") or ""), args.get("submit") is True)
    if tool == "browser_press":
        return page.press(str(args.get("key") or "Enter"))
    if tool == "browser_screenshot":
        return f"Снимок экрана {VIEWPORT['width']}×{VIEWPORT['height']} сделан и приложен ниже. Адрес: {page.location()[0]}"
    if tool == UPLOAD_TOOL:
        payload = args.get("_payload")
        if not isinstance(payload, dict):
            raise CloudError("not_found")
        text = args.get("text") if isinstance(args.get("text"), str) else None
        return page.upload(payload, ref, text)
    raise CloudError("unknown_tool")


_SAME_SITE = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None", "unspecified": "Lax"}


def normalize_origin(raw: dict) -> dict | None:
    """One origin of a Playwright storage state: {"origin": "https://…", "localStorage": [{name, value}]}."""
    if not isinstance(raw, dict):
        return None
    origin = str(raw.get("origin") or "")
    if not origin.startswith("https://"):
        return None
    items = []
    for item in raw.get("localStorage") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("value"), str):
            items.append({"name": item["name"][:500], "value": item["value"][:100_000]})
    return {"origin": origin[:300], "localStorage": items[:500]} if items else None


def normalize_cookie(raw: dict) -> dict | None:
    """A cookie in Playwright's shape from the usual export formats (Playwright, Cookie-Editor, EditThisCookie)."""
    if not isinstance(raw, dict):
        return None
    name, value, domain = raw.get("name"), raw.get("value"), raw.get("domain")
    if not isinstance(name, str) or not name or not isinstance(value, str) or not isinstance(domain, str) or not domain:
        return None
    expires = raw.get("expires", raw.get("expirationDate", -1))
    try:
        expires = float(expires) if expires not in (None, "") and not raw.get("session") else -1
    except (TypeError, ValueError):
        expires = -1
    same = _SAME_SITE.get(str(raw.get("sameSite") or "lax").lower(), "Lax")
    secure = bool(raw.get("secure"))
    if same == "None" and not secure:
        same = "Lax"
    return {
        "name": name[:256],
        "value": value[:8192],
        "domain": domain.strip()[:256],
        "path": str(raw.get("path") or "/")[:512],
        "expires": expires,
        "httpOnly": bool(raw.get("httpOnly")),
        "secure": secure,
        "sameSite": same,
    }


def onec_link(url: str) -> bool:
    return "e1cib/" in (url or "").lower()


def onec_clean_link(url: str) -> str:
    """The link as 1С expects it in «Перейти по ссылке»: Cyrillic, not %D0%A1… (the address bar form of it),
    no spaces or trailing punctuation the model copied along. A %-encoded name is «Неверный формат…» in 1С."""
    from urllib.parse import unquote

    text = (url or "").strip().rstrip(".,;)").strip("«»\"'").rstrip(".,;)")
    if "%" in text:
        text = unquote(text)
    return text.replace(" ", "")


def onec_link_key(url: str) -> str:
    """e1cib/list/Справочник.Контрагенты: the same link whatever base and encoding it came with."""
    text = onec_clean_link(url)
    at = text.lower().find("e1cib/")
    return text[at:] if at >= 0 else text


def same_base(current: str, target: str) -> bool:
    """Same 1С base: scheme, host and path up to /ru_RU/ (or the whole path without the #hash)."""
    a, b = urlsplit(current or ""), urlsplit(target or "")
    if (a.scheme, a.netloc.lower()) != (b.scheme, b.netloc.lower()) or not a.netloc:
        return False

    def base(path: str) -> str:
        low = path.lower()
        cut = low.find("/ru_ru/")
        return low[: cut + 7] if cut >= 0 else low.rstrip("/")

    return base(a.path) == base(b.path)


def risky_label(label: str) -> bool:
    from app.agent.guard import _RISKY_LABEL

    return bool(label) and bool(_RISKY_LABEL.search(label))


def chromium_env(lib_root: str) -> dict:
    """Environment for Chromium: add the libraries unpacked without root, if there are any."""
    env = dict(os.environ)
    if lib_root:
        dirs = [str(Path(lib_root) / sub) for sub in ("usr/lib/x86_64-linux-gnu", "lib/x86_64-linux-gnu", "usr/lib")]
        dirs = [d for d in dirs if Path(d).is_dir()]
        if dirs:
            env["LD_LIBRARY_PATH"] = ":".join(dirs + [env.get("LD_LIBRARY_PATH", "")]).rstrip(":")
    return env


def playwright_launcher():
    """Start Playwright and Chromium. Returns (new_page(storage_state) -> Page, shutdown())."""
    from playwright.sync_api import sync_playwright

    settings = get_settings()
    env = chromium_env(settings.cloud_browser_libs)
    if env.get("LD_LIBRARY_PATH"):
        # Playwright's driver checks host libraries with its own environment before launching:
        # it has to see the unpacked ones too.
        os.environ["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH"]
        os.environ.setdefault("PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS", "1")
    pw = sync_playwright().start()
    options: dict = {
        "headless": True,
        "args": ["--disable-dev-shm-usage", "--no-first-run", "--disable-gpu", "--mute-audio"],
    }
    if settings.cloud_browser_executable:
        options["executable_path"] = settings.cloud_browser_executable
    options["env"] = env

    def guard(route) -> None:
        if request_allowed_cached(route.request.url):
            route.continue_()
        else:
            route.abort("blockedbyclient")

    def new_page(storage_state: dict | None) -> Page:
        # Each account gets its own Chromium process: a page in one account's browser
        # cannot reach another account's memory, cookies or open mail.
        browser = pw.chromium.launch(**options)
        try:
            return _open_page(browser, storage_state)
        except Exception:
            browser.close()
            raise

    def _open_page(browser, storage_state: dict | None) -> Page:
        context = browser.new_context(
            viewport=VIEWPORT,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            accept_downloads=True,
            service_workers="block",
            storage_state=storage_state,
        )
        context.route("**/*", guard)
        page = context.new_page()
        page.set_default_timeout(20_000)
        result = Page(context, page)
        result.browser = browser
        return result

    def shutdown() -> None:
        pw.stop()

    return new_page, shutdown


@dataclass
class _Frame:
    jpeg: bytes
    version: int
    url: str = ""
    title: str = ""
    taken: float = field(default_factory=time.monotonic)


@dataclass
class CloudBrowser:
    """One browser per account, all on one worker thread."""

    launcher: Callable[[], tuple] = playwright_launcher
    max_sessions: int = 2
    store: ProfileStore | None = None
    sessions: dict[uuid.UUID, Page] = field(default_factory=dict)
    frames: dict[uuid.UUID, _Frame] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._new_page = None
        self._shutdown = None
        self._watched: dict[uuid.UUID, float] = {}
        self._ya_captcha: dict[uuid.UUID, float] = {}
        self._last_reap = 0.0
        self._busy_since = 0.0
        self._orphans: dict = {}
        if self.store is None:
            self.store = ProfileStore()

    # --- public, any thread ---------------------------------------------------

    def call(self, user_id: uuid.UUID, tool: str, args: dict, task_id=None) -> Result:
        """Run one browser_* tool for the agent."""
        return self._run(user_id, lambda page: run_tool(page, tool, args), task_id=task_id)

    def search(
        self,
        user_id: uuid.UUID,
        query: str,
        ask_user: Callable[[], bool] | None = None,
    ) -> Result:
        """Search like a person: ya.ru first. When Yandex asks this server for a captcha, search
        through Mail.ru (the same Yandex results) and skip ya.ru for a while. Only if that fails
        too, `ask_user` asks the user to pass the captcha on the live screen (True: passed)."""
        ya_url = YA_SEARCH.format(q=quote(query))
        recent = time.monotonic() - self._ya_captcha.get(user_id, -1e9) < YA_CAPTCHA_SKIP_S
        if not recent:
            first = self._run(user_id, lambda page: (page.goto(ya_url), "captcha" if page.is_captcha() else "")[1])
            if first.error:
                return first
            if first.text != "captcha":
                return self._run(user_id, lambda page: format_hits(page.serp()))
            self._ya_captcha[user_id] = time.monotonic()
        fallback = self._run(user_id, lambda page: _hits_or_empty(page.mail_search(query)))
        if not fallback.error and fallback.text:
            return fallback
        # Mail.ru did not help: back to ya.ru, and the user passes its captcha.
        again = self._run(user_id, lambda page: (page.goto(ya_url), "captcha" if page.is_captcha() else "")[1])
        if again.error:
            return again
        if again.text == "captcha":
            if not (ask_user and ask_user()):
                return Result("Ошибка: captcha", "captcha", self.frame(user_id))
            self._ya_captcha.pop(user_id, None)
        return self._run(user_id, lambda page: format_hits(page.serp()))

    def url(self, user_id: uuid.UUID) -> str:
        """Address of the user's page now ("" without one)."""
        def job() -> str:
            page = self.sessions.get(user_id)
            return page.location()[0] if page else ""

        try:
            return str(self._submit(job).result(timeout=CALL_TIMEOUT_S) or "")
        except Exception:  # noqa: BLE001
            return ""

    def captcha(self, user_id: uuid.UUID) -> bool:
        """The current page asks to prove a human is there."""
        def job() -> bool:
            page = self.sessions.get(user_id)
            return bool(page and page.is_captcha())

        try:
            return bool(self._submit(job).result(timeout=CALL_TIMEOUT_S))
        except Exception:  # noqa: BLE001
            return False

    def watch(self, user_id: uuid.UUID) -> None:
        """The panel is open: keep its frame fresh for a few seconds."""
        self._watched[user_id] = time.monotonic()

    def user_input(self, user_id: uuid.UUID, action: dict) -> Result:
        """A click or keystroke from the user in the panel."""
        return self._run(user_id, lambda page: (page.user_input(action), "ok")[1], save=True)

    def stop(self, user_id: uuid.UUID) -> None:
        if self._thread is not None:
            self._submit(lambda: self._close(user_id)).result(timeout=CALL_TIMEOUT_S)

    def forget(self, user_id: uuid.UUID) -> None:
        """Sign out everywhere: close the browser and delete the saved sign-ins."""
        self.stop(user_id)
        self.store.forget(user_id)

    def status(self, user_id: uuid.UUID) -> dict:
        item = self.frames.get(user_id)
        return {
            "running": user_id in self.sessions,
            "version": item.version if item else 0,
            "url": item.url if item else "",
            "title": item.title if item else "",
            "saved_logins": self.store.exists(user_id),
            "saved_sites": self.saved_sites(user_id),
            "logins_kept": _key() is not None,
            "idle_minutes": IDLE_S // 60,
        }

    def saved_sites(self, user_id: uuid.UUID) -> list[str]:
        """Sites with saved cookies (names only, no values): shows that a sign-in survives between tasks."""
        state = self.store.load(user_id) or {}
        sites = {str(c.get("domain") or "").lstrip(".").lower() for c in state.get("cookies") or []}
        return sorted(s for s in sites if s)[:100]

    def import_cookies(self, user_id: uuid.UUID, cookies: list[dict], origins: list[dict] | None = None) -> int:
        """Add a sign-in made in the user's own browser to the saved profile: cookies exported from it, or a
        whole Playwright storage state (cookies + localStorage per origin). The next task starts already
        signed in. Returns how many cookies were taken."""
        if _key() is None:
            raise CloudError("no_vault_key")
        clean = [c for c in (normalize_cookie(raw) for raw in cookies) if c]
        stores = [o for o in (normalize_origin(raw) for raw in origins or []) if o]
        if not clean and not stores:
            raise CloudError("bad_cookies")

        def job() -> int:
            page = self.sessions.get(user_id)
            if page is not None:
                if clean:
                    page.context.add_cookies(clean)
                self._save(user_id, page)
                if not stores:
                    return len(clean)
                # localStorage only loads with a new context: save, merge, and start fresh next call.
                self._close(user_id)
            state = self.store.load(user_id) or {"cookies": [], "origins": []}
            keep = {(c.get("name"), c.get("domain"), c.get("path")): c for c in state.get("cookies") or []}
            for cookie in clean:
                keep[(cookie["name"], cookie["domain"], cookie["path"])] = cookie
            state["cookies"] = list(keep.values())
            by_origin = {o.get("origin"): o for o in state.get("origins") or []}
            for item in stores:
                by_origin[item["origin"]] = item
            state["origins"] = list(by_origin.values())
            self.store.save(user_id, state)
            return len(clean)

        try:
            return int(self._submit(job).result(timeout=CALL_TIMEOUT_S))
        except FutureTimeout:
            raise CloudError("tool_timeout") from None

    def frame(self, user_id: uuid.UUID) -> bytes | None:
        item = self.frames.get(user_id)
        return item.jpeg if item else None

    def frame_version(self, user_id: uuid.UUID) -> int:
        item = self.frames.get(user_id)
        return item.version if item else 0

    def active(self) -> int:
        return len(self.sessions)

    # --- worker thread ----------------------------------------------------------

    def _run(self, user_id: uuid.UUID, action: Callable[[Page], str], save: bool = True, task_id=None) -> Result:
        try:
            future = self._submit(lambda: self._do(user_id, action, save, task_id))
            return future.result(timeout=CALL_TIMEOUT_S)
        except CloudError as exc:
            if exc.code.startswith("needs_confirmation:"):
                under = exc.code.split(":", 1)[1]
                return Result(
                    f"Ошибка: в этой точке «{under}». Это действие нужно подтвердить: нажми через "
                    f"browser_click с text «{under}», тогда пользователя спросят.",
                    "needs_confirmation",
                    self.frame(user_id),
                )
            return Result(f"Ошибка облачного браузера: {exc.code}", exc.code, self.frame(user_id))
        except FutureTimeout:
            self._maybe_restart()
            return Result(TIMEOUT_TEXT, "tool_timeout", self.frame(user_id))

    def _submit(self, job: Callable) -> Future:
        future: Future = Future()
        with self._lock:
            if self._busy_since and time.monotonic() - self._busy_since > CALL_TIMEOUT_S:
                # The worker is stuck in a page (a script that never returns): don't queue behind it.
                self._maybe_restart_locked()
                if self._busy_since and time.monotonic() - self._busy_since > CALL_TIMEOUT_S:
                    future.set_exception(CloudError("tool_timeout"))
                    return future
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._work, args=(self._jobs,), name="cloud-browser", daemon=True)
                self._thread.start()
            self._jobs.put((job, future))
        return future

    def _maybe_restart(self) -> None:
        with self._lock:
            self._maybe_restart_locked()

    def _maybe_restart_locked(self) -> None:
        """Stuck longer than RESTART_AFTER_S: leave that worker and its browsers behind (it closes them
        when the page lets go) and start a clean one. Sign-ins were saved after the last good call."""
        if not self._busy_since or time.monotonic() - self._busy_since < RESTART_AFTER_S:
            return
        log.warning("cloud browser worker stuck for %.0f s, starting a new one", time.monotonic() - self._busy_since)
        old = self._thread
        self._orphans[old] = (dict(self.sessions), self._shutdown)
        self.sessions = {}
        self._new_page = None
        self._shutdown = None
        self._busy_since = 0.0
        stale_jobs = self._jobs
        self._jobs = queue.Queue()
        while True:
            try:
                _, pending = stale_jobs.get_nowait()
            except queue.Empty:
                break
            if pending.set_running_or_notify_cancel():
                pending.set_exception(CloudError("tool_timeout"))
        self._thread = None

    def _retire(self) -> None:
        """This worker was replaced while stuck: close what it owned and end."""
        pages, shutdown = self._orphans.pop(threading.current_thread(), ({}, None))
        for page in pages.values():
            page.close()
        if shutdown is not None:
            try:
                shutdown()
            except Exception:  # noqa: BLE001
                pass

    def _work(self, jobs: queue.Queue | None = None) -> None:
        jobs = jobs or self._jobs
        while True:
            if threading.current_thread() in self._orphans:
                self._retire()
                return
            try:
                job, future = jobs.get(timeout=PUMP_S if self.sessions else REAP_EVERY_S)
            except queue.Empty:
                if threading.current_thread() is self._thread:
                    self._idle()
                continue
            if not future.set_running_or_notify_cancel():
                continue
            self._busy_since = time.monotonic()
            try:
                future.set_result(job())
            except CloudError as exc:
                future.set_exception(exc)
            except BaseException as exc:  # noqa: BLE001
                log.info("cloud browser call failed: %s", type(exc).__name__)
                failed = CloudError("browser_failed")
                failed.__cause__ = exc
                future.set_exception(failed)
            finally:
                if threading.current_thread() is self._thread:
                    self._busy_since = 0.0
            if threading.current_thread() is self._thread:
                self._reap_now()

    def _idle(self) -> None:
        """No job: let pages load what they asked for, refresh watched screens, close idle ones."""
        now = time.monotonic()
        pumped = False
        for user_id, page in list(self.sessions.items()):
            watched = now - self._watched.get(user_id, -1e9) < WATCH_S
            item = self.frames.get(user_id)
            stale = item is None or now - item.taken > LIVE_FRAME_S
            if watched and stale:
                try:
                    self._capture(user_id, page, only_changed=True)
                except Exception:  # noqa: BLE001
                    pass
                pumped = True
        if not pumped and self.sessions:
            next(iter(self.sessions.values())).pump()
        self._reap_now()

    def _reap_now(self) -> None:
        if time.monotonic() - self._last_reap >= REAP_EVERY_S:
            self._last_reap = time.monotonic()
            self._reap()

    def _page(self, user_id: uuid.UUID) -> Page:
        page = self.sessions.get(user_id)
        if page is not None:
            return page
        if len(self.sessions) >= self.max_sessions and not self._evict_idle():
            raise CloudError("browser_busy")
        if self._new_page is None:
            try:
                self._new_page, self._shutdown = self.launcher()
            except Exception as exc:  # noqa: BLE001
                log.info("cloud browser failed to start: %s", type(exc).__name__)
                raise CloudError("browser_unavailable") from None
        page = self._new_page(self.store.load(user_id))
        page.last_used = time.monotonic()
        self.sessions[user_id] = page
        return page

    def _do(self, user_id: uuid.UUID, action: Callable[[Page], str], save: bool, task_id=None) -> Result:
        page = self._page(user_id)
        page.last_used = time.monotonic()
        try:
            text = action(page)
        except CloudError:
            self._capture(user_id, page)
            self._collect(user_id, page, task_id)
            raise
        frame = self._capture(user_id, page)
        if save:
            self._save(user_id, page)
        saved = self._collect(user_id, page, task_id)
        notes = [heli_files.saved_note(info) if isinstance(info, heli_files.FileInfo) else info for info in saved]
        if notes:
            text = "\n".join(notes) + ("\n\n" + text if text else "")
        return Result(text, None, frame, [info for info in saved if isinstance(info, heli_files.FileInfo)])

    def _collect(self, user_id: uuid.UUID, page: Page, task_id=None) -> list:
        """Move finished downloads into the file store. Returns FileInfo items and notes on failures."""
        out: list = []
        for download in page.take_downloads():
            name = heli_files.safe_name(getattr(download, "suggested_filename", "") or "", fallback="загрузка")
            try:
                if download.failure():
                    out.append(f"Скачать «{name}» не удалось.")
                    continue
                store = heli_files.store()
                tmp_dir = store.root / ".incoming"
                tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                tmp = tmp_dir / uuid.uuid4().hex
                download.save_as(str(tmp))
                out.append(store.put_path(user_id, name, tmp, heli_files.TO_PC, task_id))
            except heli_files.FileError as exc:
                limit = heli_files.human_size(heli_files.store().max_bytes)
                reason = f"больше {limit}" if exc.code == "file_too_large" else "хранилище файлов заполнено"
                out.append(f"Файл «{name}» не передан пользователю: {reason}.")
            except Exception as exc:  # noqa: BLE001
                log.info("download failed: %s", type(exc).__name__)
                out.append(f"Скачать «{name}» не удалось.")
            finally:
                try:
                    download.delete()
                except Exception:  # noqa: BLE001
                    pass
        return out

    def _capture(self, user_id: uuid.UUID, page: Page, only_changed: bool = False) -> bytes | None:
        jpeg = page.frame()
        if jpeg:
            old = self.frames.get(user_id)
            url, title = page.location()
            if only_changed and old is not None and (old.jpeg, old.url, old.title) == (jpeg, url, title):
                old.taken = time.monotonic()
                return jpeg
            self.frames[user_id] = _Frame(jpeg, self.frame_version(user_id) + 1, url, title)
        return jpeg

    def _save(self, user_id: uuid.UUID, page: Page) -> None:
        state = page.state()
        if state is not None:
            try:
                self.store.save(user_id, state)
            except OSError:
                log.info("cloud browser profile save failed")

    def _close(self, user_id: uuid.UUID) -> None:
        self._watched.pop(user_id, None)
        page = self.sessions.pop(user_id, None)
        if page is not None:
            self._save(user_id, page)
            page.close()
        self._maybe_shutdown()

    def _evict_idle(self) -> bool:
        """All browsers taken: close the one unused longest, if nobody touched it for EVICT_IDLE_S
        and its owner is not watching the screen. Its sign-ins are saved first."""
        now = time.monotonic()
        idle = [
            (page.last_used, user_id)
            for user_id, page in self.sessions.items()
            if now - page.last_used >= EVICT_IDLE_S and now - self._watched.get(user_id, -1e9) >= WATCH_S
        ]
        if not idle:
            return False
        self._close(min(idle)[1])
        return True

    def reserve(
        self,
        user_id: uuid.UUID,
        wait_s: float = BUSY_WAIT_S,
        on_wait: Callable[[bool], None] | None = None,
        halted: Callable[[], bool] | None = None,
        poll_s: float = 3.0,
    ) -> str | None:
        """Make sure this account has a browser. When all are taken, wait in line up to wait_s.
        Returns None when the browser is ready, or the error code."""
        deadline = time.monotonic() + wait_s
        waiting = False
        try:
            while True:
                try:
                    self._submit(lambda: (self._page(user_id), None)[1]).result(timeout=CALL_TIMEOUT_S)
                    return None
                except CloudError as exc:
                    if exc.code != "browser_busy" or time.monotonic() >= deadline or (halted and halted()):
                        return exc.code
                except FutureTimeout:
                    return "timeout"
                if not waiting:
                    waiting = True
                    if on_wait:
                        on_wait(True)
                time.sleep(poll_s)
        finally:
            if waiting and on_wait:
                on_wait(False)

    def _reap(self) -> None:
        now = time.monotonic()
        for user_id, page in list(self.sessions.items()):
            if now - page.last_used > IDLE_S:
                self._close(user_id)
        self._maybe_shutdown()

    def _maybe_shutdown(self) -> None:
        # Nobody is using it: give the memory back to the host.
        if not self.sessions and self._shutdown is not None:
            try:
                self._shutdown()
            except Exception:  # noqa: BLE001
                pass
            self._new_page = None
            self._shutdown = None


def ready() -> bool:
    """On, installed, and Chromium started at least once on this host."""
    if _ready:
        return available()
    if available() and time.monotonic() - _last_probe > 60:
        return _try_register()
    return False


def available() -> bool:
    if not get_settings().cloud_browser_enabled:
        return False
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


_pool: CloudBrowser | None = None
_ready = False
_probe_error = ""
_last_probe = 0.0
_pool_lock = threading.Lock()


def pool() -> CloudBrowser:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = CloudBrowser(max_sessions=max(1, get_settings().cloud_browser_max))
        return _pool


def set_pool(value: CloudBrowser | None) -> None:
    """Tests swap in a fake launcher."""
    global _pool
    with _pool_lock:
        _pool = value


# --- the agent loop's executor ------------------------------------------------------

SEARCH_TOOL = "browser_search"
UPLOAD_TOOL = "browser_upload"
CAPTCHA_TEXT = "Сайт просит пройти капчу. Пройдите её на экране «Компьютер агента», и я продолжу сам."
BITRIX_CAPTCHA_TEXT = (
    "Битрикс24 просит пройти капчу при входе. Пройдите её на экране «Компьютер агента», и я продолжу сам. "
    "Чтобы дальше работать без входа и капчи, добавьте в «Доступы» входящий вебхук портала: сайт — адрес "
    "портала, логин webhook, пароль — адрес вебхука."
)
SEARCH_CAPTCHA_TEXT = "Яндекс просит пройти капчу. Пройдите её на экране «Компьютер агента», и я продолжу сам."
_SHOT_TOOLS = {"browser_screenshot"}
# After these the page may have turned into a captcha.
_CAPTCHA_TOOLS = {"browser_open", "browser_click", "browser_type", "browser_press"}


def _run_for(task_id, user_id):
    try:
        from app.agent import runs

        return runs.registry.get(task_id, user_id)
    except Exception:  # noqa: BLE001
        return None


def ask_for_captcha(
    browser: CloudBrowser,
    user_id: uuid.UUID,
    agent_run,
    text: str = CAPTCHA_TEXT,
    wait_s: float = CAPTCHA_WAIT_S,
    poll_s: float = CAPTCHA_POLL_S,
) -> bool:
    """Pause the task and ask the user to pass the captcha on the live screen.

    The task goes on by itself once the page is no longer a captcha; «Продолжить» goes on at once,
    «Стоп» ends the task. True: the captcha is gone."""
    control = getattr(agent_run, "control", None)
    if agent_run is None or control is None or control.stopped:
        return False
    if hasattr(control, "pause"):
        control.pause()
    agent_run.publish({"type": "paused", "reason": "captcha", "text": text})
    browser.watch(user_id)
    deadline = time.monotonic() + wait_s
    solved = False
    try:
        while time.monotonic() < deadline and not control.stopped:
            if not getattr(control, "paused", True):
                solved = not browser.captcha(user_id)
                break
            time.sleep(poll_s)
            browser.watch(user_id)
            if not browser.captcha(user_id):
                solved = True
                break
    finally:
        if not control.stopped:
            if hasattr(control, "resume"):
                control.resume()
            agent_run.publish({"type": "resumed"})
    return solved


def execute(session, user, task_id, name: str, args: dict):
    """app.agent.executors contract: run(session, user, task_id, name, args) -> ClientResult."""
    from app.agent.control import ClientResult

    browser = pool()
    agent_run = _run_for(task_id, user.id)
    publish = agent_run.publish if agent_run else (lambda event: None)
    busy = browser.reserve(
        user.id,
        on_wait=lambda on: publish({"type": "notice", "text": BUSY_NOTICE if on else ""}),
        halted=(lambda: agent_run.control.stopped) if agent_run else None,
    )
    if busy:
        text = "Ошибка: все облачные браузеры заняты, попробуй позже" if busy == "browser_busy" else f"Ошибка облачного браузера: {busy}"
        return ClientResult(text, busy)
    if name == SEARCH_TOOL:
        result = browser.search(
            user.id,
            str(args.get("query") or ""),
            ask_user=lambda: ask_for_captcha(browser, user.id, agent_run, SEARCH_CAPTCHA_TEXT),
        )
    elif name == UPLOAD_TOOL:
        try:
            info, blob = heli_files.store().get(user.id, str(args.get("file_id") or ""))
            if info.direction != heli_files.FROM_PC:
                raise heli_files.FileError("not_found")
        except heli_files.FileError:
            return ClientResult("Ошибка: нет такого приложенного файла", "file_not_found")
        payload = {"name": info.name, "mimeType": _mime(info.name), "buffer": blob.read_bytes()}
        result = browser.call(user.id, name, {**args, "_payload": payload}, task_id=task_id)
    else:
        result = browser.call(user.id, name, args, task_id=task_id)
        if name in _CAPTCHA_TOOLS and not result.error and browser.captcha(user.id):
            publish({"type": "screen", "version": browser.frame_version(user.id)})
            portal = _bitrix_portal(browser.url(user.id), str(args.get("url") or "") if name == "browser_open" else "")
            webhook = _bitrix_webhook(session, user, portal) if portal is not None else None
            if webhook is not None:
                # REST through the webhook does the job without a sign-in: no pause, no steps spent on the captcha.
                for info in result.files:
                    publish(heli_files.published(info))
                return ClientResult(
                    f"Вход в Битрикс24 просит капчу. Для {webhook['site']} в «Доступах» есть вебхук: не входи на экране, "
                    "делай задачу через bitrix24_read" + (" и bitrix24_write" if _bitrix_sandbox(webhook["site"]) else "")
                    + ". Капчу не решай.",
                    "captcha_use_rest",
                )
            amo = _amo_token(session, user, browser.url(user.id), str(args.get("url") or "") if name == "browser_open" else "")
            if amo is not None:
                for info in result.files:
                    publish(heli_files.published(info))
                return ClientResult(
                    f"Вход в amoCRM просит капчу. Для {amo['site']} в «Доступах» есть токен: не входи на экране, "
                    "делай задачу через amocrm_read и amocrm_write. Капчу не решай.",
                    "captcha_use_rest",
                )
            text = BITRIX_CAPTCHA_TEXT if portal is not None else CAPTCHA_TEXT
            if ask_for_captcha(browser, user.id, agent_run, text):
                after = browser.call(user.id, "browser_read", {}, task_id=task_id)
                result = Result("Пользователь прошёл капчу.\n\n" + after.text, after.error, after.frame, result.files + after.files)
            elif agent_run is not None and not agent_run.control.stopped:
                # Nobody passed it in time: the agent never solves captchas. The run ends with what it did,
                # and «продолжи» picks it up once a person has signed in on the live screen.
                from app.agent.control import Stopped

                for info in result.files:
                    publish(heli_files.published(info))
                raise Stopped("captcha")
            else:
                result = Result(
                    "Ошибка: сайт показал капчу, и её не прошли. Попробуй другой сайт или скажи пользователю, "
                    "что нужно пройти капчу на экране «Компьютер агента».",
                    "captcha",
                    browser.frame(user.id),
                    result.files,
                )
    for info in result.files:
        publish(heli_files.published(info))
    publish({"type": "screen", "version": browser.frame_version(user.id)})
    shot = result.frame if (name in _SHOT_TOOLS or result.error) else None
    return ClientResult(result.text, result.error, shot)


def _bitrix_portal(current: str, opened: str) -> str | None:
    """The Битрикс24 portal behind a captcha: "" for its shared sign-in (bitrix24.net), None when it is not Битрикс24."""
    from app.skills.hosts import host_matches, host_of

    for url in (opened, current):
        host = host_of(url)
        if not host:
            continue
        if any(host_matches(host, s) for s in ("bitrix24.ru", "bitrix24.com", "bitrix24.by", "bitrix24.kz")):
            return host
        if host_matches(host, "bitrix24.net"):
            return ""
    return None


def _amo_token(session, user, current: str, opened: str) -> dict | None:
    """The «Доступы» token of the amoCRM account behind a captcha, or None (not amoCRM, no token)."""
    from app.agent import amocrm

    for url in (opened, current):
        if amocrm.account_host(url):
            try:
                return amocrm.token_entry_for(session, user, url)
            except Exception:  # noqa: BLE001
                return None
    return None


def _bitrix_webhook(session, user, portal: str) -> dict | None:
    try:
        from app.agent import bitrix

        return bitrix.webhook_for(session, user, portal or None) if session is not None else None
    except Exception:  # noqa: BLE001
        return None


def _bitrix_sandbox(site: str) -> bool:
    from app.agent import bitrix

    return bitrix.is_sandbox(site)


def _mime(name: str) -> str:
    import mimetypes

    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def probe(browser: CloudBrowser) -> bool:
    """Start Chromium once and close it: missing system libraries show up here, not mid-task."""
    global _probe_error

    def job() -> bool:
        new_page, shutdown = browser.launcher()
        try:
            new_page(None).close()
        finally:
            shutdown()
        return True

    try:
        browser._submit(job).result(timeout=CALL_TIMEOUT_S)
        _probe_error = ""
        return True
    except Exception as exc:  # noqa: BLE001
        cause = exc.__cause__ or exc
        _probe_error = f"{type(cause).__name__}: {str(cause)[:400]}"
        log.warning("cloud browser off: Chromium did not start (%s)", _probe_error)
        return False


def register(background: bool = False) -> bool:
    """Called at startup. Without Playwright, with the flag off or if Chromium cannot start,
    the agent has no browser tools (or the app's local browser when that is switched on).
    A failed start is retried at most once a minute when the panel asks for the status."""
    if not available():
        return False
    if background:
        threading.Thread(target=_try_register, name="cloud-browser-probe", daemon=True).start()
        return True
    return _try_register()


def _try_register() -> bool:
    global _ready, _last_probe
    from app.agent.executors import register_executor

    _last_probe = time.monotonic()
    if not probe(pool()):
        return False
    register_executor((*TOOL_NAMES, SEARCH_TOOL, UPLOAD_TOOL), execute)
    _ready = True
    log.info("cloud browser on: max %s sessions", pool().max_sessions)
    return True


def last_error() -> str:
    return _probe_error

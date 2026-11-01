/** A small, safe Markdown subset for chat replies: no HTML, only http(s)/mailto links. */

export type Inline =
  | { type: "text"; text: string }
  | { type: "strong"; children: Inline[] }
  | { type: "em"; children: Inline[] }
  | { type: "code"; text: string }
  | { type: "link"; href: string; children: Inline[] }
  | { type: "br" };

export type Block =
  | { type: "p"; inline: Inline[] }
  | { type: "h"; level: number; inline: Inline[] }
  | { type: "ul"; items: Inline[][] }
  | { type: "ol"; start: number; items: Inline[][] }
  | { type: "quote"; inline: Inline[] }
  | { type: "code"; text: string }
  | { type: "hr" };

/** The link target if it is safe to open, else null. */
export function safeHref(raw: string): string | null {
  const href = raw.trim().replace(/^<|>$/g, "");
  if (!/^(https?:\/\/|mailto:)/i.test(href)) {
    // «getheli.ru/path» and «www.site.ru» are links too.
    if (/^(www\.)?[\p{L}\d-]+(\.[\p{L}\d-]+)*\.[\p{L}]{2,}(\/\S*)?$/u.test(href)) return `https://${href}`;
    return null;
  }
  if (/[\s"<>]/.test(href)) return null;
  return href;
}

const URL_RE = /https?:\/\/[^\s<>()«»"']+(?:\([^\s<>()]*\)[^\s<>()«»"']*)*/gi;
const TRAILING = /[.,;:!?…»)\]'"]+$/;

function linkify(text: string, out: Inline[]): void {
  let last = 0;
  for (const match of text.matchAll(URL_RE)) {
    let url = match[0];
    const tail = url.match(TRAILING)?.[0] ?? "";
    // Keep a closing bracket that belongs to the URL, e.g. …/wiki/Foo_(bar).
    const keep = tail.startsWith(")") && url.slice(0, -tail.length).includes("(") ? 1 : 0;
    url = url.slice(0, url.length - tail.length + keep);
    const start = match.index ?? 0;
    if (start > last) out.push({ type: "text", text: text.slice(last, start) });
    const href = safeHref(url);
    out.push(href ? { type: "link", href, children: [{ type: "text", text: url }] } : { type: "text", text: url });
    last = start + url.length;
  }
  if (last < text.length) out.push({ type: "text", text: text.slice(last) });
}

function pushText(text: string, out: Inline[], autolink: boolean): void {
  if (!text) return;
  if (autolink) linkify(text, out);
  else out.push({ type: "text", text });
}

/** Finds the `]` that closes a `[` at `open`, allowing nested brackets. */
function closingBracket(text: string, open: number): number {
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    if (text[i] === "\\") i++;
    else if (text[i] === "[") depth++;
    else if (text[i] === "]" && --depth === 0) return i;
  }
  return -1;
}

export function parseInline(text: string, autolink = true): Inline[] {
  const out: Inline[] = [];
  let buffer = "";
  let i = 0;
  const flush = () => {
    pushText(buffer, out, autolink);
    buffer = "";
  };
  while (i < text.length) {
    const ch = text[i];
    const rest = text.slice(i);
    if (ch === "\\" && i + 1 < text.length && /[\\`*_[\]()#>~-]/.test(text[i + 1])) {
      buffer += text[i + 1];
      i += 2;
      continue;
    }
    if (ch === "\n") {
      flush();
      out.push({ type: "br" });
      i++;
      continue;
    }
    if (ch === "`") {
      const end = text.indexOf("`", i + 1);
      if (end > i + 1) {
        flush();
        out.push({ type: "code", text: text.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }
    if (ch === "[") {
      const close = closingBracket(text, i);
      if (close > i && text[close + 1] === "(") {
        const end = text.indexOf(")", close + 2);
        if (end > close + 1) {
          let target = text.slice(close + 2, end).trim();
          target = target.replace(/\s+"[^"]*"$/, ""); // [a](url "title")
          const href = safeHref(target);
          const label = text.slice(i + 1, close);
          flush();
          if (href) out.push({ type: "link", href, children: parseInline(label, false) });
          else out.push(...parseInline(label, false));
          i = end + 1;
          continue;
        }
      }
    }
    const strong = rest.match(/^(\*\*|__)(?=\S)([\s\S]*?\S)\1/);
    if (strong) {
      flush();
      out.push({ type: "strong", children: parseInline(strong[2], autolink) });
      i += strong[0].length;
      continue;
    }
    const prev = i > 0 ? text[i - 1] : " ";
    const em = /[\s([«"'—-]/.test(prev) || i === 0 ? rest.match(/^([*_])(?=[^\s*_])([^*_\n]*?[^\s*_])\1(?![\p{L}\d])/u) : null;
    if (em) {
      flush();
      out.push({ type: "em", children: parseInline(em[2], autolink) });
      i += em[0].length;
      continue;
    }
    buffer += ch;
    i++;
  }
  flush();
  return out;
}

const LIST_ITEM = /^\s{0,3}([-*•+]|\d{1,3}[.)])\s+(.*)$/;

export function parseMarkdown(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let para: string[] = [];
  const endPara = () => {
    if (para.length) blocks.push({ type: "p", inline: parseInline(para.join("\n")) });
    para = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();
    if (!trimmed) {
      endPara();
      continue;
    }
    if (/^```/.test(trimmed)) {
      endPara();
      const body: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) body.push(lines[i++]);
      blocks.push({ type: "code", text: body.join("\n") });
      continue;
    }
    if (/^([-*_])(\s*\1){2,}$/.test(trimmed)) {
      endPara();
      blocks.push({ type: "hr" });
      continue;
    }
    const heading = trimmed.match(/^(#{1,6})\s+(.*?)\s*#*$/);
    if (heading) {
      endPara();
      blocks.push({ type: "h", level: heading[1].length, inline: parseInline(heading[2]) });
      continue;
    }
    if (trimmed.startsWith(">")) {
      endPara();
      const body: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) body.push(lines[i++].trim().replace(/^>\s?/, ""));
      i--;
      blocks.push({ type: "quote", inline: parseInline(body.join("\n")) });
      continue;
    }
    const item = line.match(LIST_ITEM);
    if (item) {
      endPara();
      const ordered = /\d/.test(item[1]);
      const items: string[] = [];
      const start = ordered ? parseInt(item[1], 10) : 1;
      while (i < lines.length) {
        const current = lines[i].match(LIST_ITEM);
        if (current && /\d/.test(current[1]) === ordered) {
          items.push(current[2]);
        } else if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length) {
          items[items.length - 1] += "\n" + lines[i].trim(); // continuation line
        } else {
          break;
        }
        i++;
      }
      i--;
      const parsed = items.map((text) => parseInline(text));
      blocks.push(ordered ? { type: "ol", start, items: parsed } : { type: "ul", items: parsed });
      continue;
    }
    para.push(trimmed);
  }
  endPara();
  return blocks;
}

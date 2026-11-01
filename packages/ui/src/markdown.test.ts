import { test } from "node:test";
import assert from "node:assert/strict";
import { parseInline, parseMarkdown, safeHref, type Inline } from "./markdown.ts";

function links(inline: Inline[]): string[] {
  const out: string[] = [];
  const walk = (list: Inline[]) => {
    for (const node of list) {
      if (node.type === "link") out.push(node.href);
      if ("children" in node) walk(node.children);
    }
  };
  walk(inline);
  return out;
}

test("bold and markdown links from the GPU answer", () => {
  const inline = parseInline("**Если нужно самое дешёвое** — [Тераферма](https://teraferma.example/gpu) (RTX 3060 от 12 ₽/ч).");
  assert.equal(inline[0].type, "strong");
  assert.deepEqual(links(inline), ["https://teraferma.example/gpu"]);
  assert.ok(!JSON.stringify(inline).includes("**"));
});

test("bare URLs become links without trailing punctuation", () => {
  const inline = parseInline("Смотрите https://yougpu.example/price, там дешевле. И (https://a.example/x).");
  assert.deepEqual(links(inline), ["https://yougpu.example/price", "https://a.example/x"]);
  assert.deepEqual(links(parseInline("https://ru.wikipedia.org/wiki/Foo_(bar)")), ["https://ru.wikipedia.org/wiki/Foo_(bar)"]);
});

test("unsafe links are shown as text", () => {
  assert.deepEqual(links(parseInline("[жми](javascript:alert(1))")), []);
  assert.deepEqual(links(parseInline("[файл](file:///C:/x.exe)")), []);
  assert.equal(safeHref("getheli.ru/download"), "https://getheli.ru/download");
  assert.equal(safeHref("mailto:hi@getheli.ru"), "mailto:hi@getheli.ru");
});

test("snake_case and arithmetic stay text", () => {
  assert.deepEqual(parseInline("файл report_2026_final.xlsx и 2 * 3 = 6"), [
    { type: "text", text: "файл report_2026_final.xlsx и 2 * 3 = 6" },
  ]);
  assert.equal(parseInline("это *важно*")[1].type, "em");
});

test("blocks: headings, lists, rule, code", () => {
  const blocks = parseMarkdown(
    "### Варианты\n\n- **ВычислиРу** — от 89 ₽/ч\n- ГПУ-Арена\n\n---\n\n1. Первый\n2. Второй\n\n```\ncode **x**\n```\nИтого: два.",
  );
  assert.deepEqual(blocks.map((b) => b.type), ["h", "ul", "hr", "ol", "code", "p"]);
  const list = blocks[1];
  assert.ok(list.type === "ul" && list.items.length === 2 && list.items[0][0].type === "strong");
  const code = blocks[4];
  assert.ok(code.type === "code" && code.text === "code **x**");
});

test("line breaks inside a paragraph are kept", () => {
  const [p] = parseMarkdown("строка один\nстрока два");
  assert.ok(p.type === "p" && p.inline.some((n) => n.type === "br"));
});

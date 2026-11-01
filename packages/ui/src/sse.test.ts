import assert from "node:assert/strict";
import { test } from "node:test";
import { parseSse, stoppedNote } from "./sse.ts";

test("parseSse keeps a partial event for the next chunk", () => {
  const first = parseSse('id: 1\nevent: step\ndata: {"seq":1,"type":"step"}\n\n: ping\n\nid: 2\ndata: {"seq":2,');
  assert.deepEqual(first.events.map((e) => e.seq), [1]);
  const second = parseSse(first.rest + '"type":"done"}\n\n');
  assert.equal(second.events[0].type, "done");
  assert.equal(second.rest, "");
});

test("parseSse handles CRLF", () => {
  const { events } = parseSse('data: {"seq":3,"type":"paused"}\r\n\r\n');
  assert.equal(events[0].type, "paused");
});

test("stoppedNote explains why the run ended", () => {
  assert.equal(stoppedNote(null), null);
  assert.match(stoppedNote("confirm") ?? "", /не подтверждено/);
});

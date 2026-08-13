import { describe, expect, it } from "vitest";

import { createSseParser, readSseFrames } from "@/lib/sse";

/**
 * The SSE parser (#19).
 *
 * The answer arrives as `event:`/`data:` frames over a `fetch` body stream (ADR-0009). Nothing about
 * that stream respects frame boundaries — a chunk can end mid-frame, mid-line, or even mid-UTF-8 —
 * so the parser is the one piece where "it worked when I tried it" is worthless: locally the whole
 * answer often arrives in one chunk and every split-related bug stays invisible.
 */

function framesOf(...chunks: string[]) {
  const parser = createSseParser();
  return chunks.flatMap((chunk) => parser.push(chunk));
}

/** A stream that delivers exactly the chunks given, so splits are deliberate rather than lucky. */
function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

describe("createSseParser", () => {
  it("reads one complete frame", () => {
    expect(framesOf('event: token\ndata: {"text":"Invoices"}\n\n')).toEqual([
      { event: "token", data: '{"text":"Invoices"}' },
    ]);
  });

  it("reads several frames from one chunk", () => {
    const frames = framesOf('event: token\ndata: "a"\n\nevent: token\ndata: "b"\n\n');

    expect(frames.map((f) => f.data)).toEqual(['"a"', '"b"']);
  });

  it("joins a frame split across chunk boundaries", () => {
    // The bug this whole module exists to prevent. The split is placed mid-JSON on purpose.
    expect(framesOf('event: token\ndata: {"te', 'xt":"net 30"}\n\n')).toEqual([
      { event: "token", data: '{"text":"net 30"}' },
    ]);
  });

  it("joins a frame split between the event line and its data line", () => {
    expect(framesOf("event: citations\n", 'data: {"citations":[]}\n\n')).toEqual([
      { event: "citations", data: '{"citations":[]}' },
    ]);
  });

  it("joins a frame split inside the terminating blank line", () => {
    // "\n\n" arriving as "\n" + "\n" is the split most likely to emit a frame twice, or never.
    expect(framesOf('event: token\ndata: "x"\n', "\n")).toEqual([{ event: "token", data: '"x"' }]);
  });

  it("emits nothing until a frame is terminated", () => {
    const parser = createSseParser();

    expect(parser.push('event: token\ndata: {"text":"partial"}')).toEqual([]);
    expect(parser.push("\n\n")).toEqual([{ event: "token", data: '{"text":"partial"}' }]);
  });

  it("accepts CRLF line endings", () => {
    // The spec allows CRLF, and an intermediary can rewrite line endings.
    expect(framesOf('event: token\r\ndata: "x"\r\n\r\n')).toEqual([
      { event: "token", data: '"x"' },
    ]);
  });

  it("strips exactly one leading space after the colon", () => {
    // Per the spec: one optional space. A second space is data, and in JSON it would be significant
    // only as whitespace — but a parser that strips greedily would also eat it from a text token.
    expect(framesOf("event: token\ndata:  two spaces\n\n")).toEqual([
      { event: "token", data: " two spaces" },
    ]);
  });

  it("joins multi-line data with newlines", () => {
    expect(framesOf("event: token\ndata: line one\ndata: line two\n\n")).toEqual([
      { event: "token", data: "line one\nline two" },
    ]);
  });

  it("ignores comment lines", () => {
    // A proxy or the server may send `: keepalive` to hold the connection open.
    expect(framesOf(': keepalive\n\nevent: token\ndata: "x"\n\n')).toEqual([
      { event: "token", data: '"x"' },
    ]);
  });

  it("defaults a frame with no event field to 'message'", () => {
    expect(framesOf('data: "x"\n\n')).toEqual([{ event: "message", data: '"x"' }]);
  });

  it("does not emit an event for a blank line on its own", () => {
    // Otherwise a keepalive newline would look like an empty token and blank the answer.
    expect(framesOf("\n\n\n")).toEqual([]);
  });

  it("keeps a field name it does not recognise from becoming data", () => {
    expect(framesOf('id: 7\nretry: 100\nevent: token\ndata: "x"\n\n')).toEqual([
      { event: "token", data: '"x"' },
    ]);
  });
});

describe("readSseFrames", () => {
  it("yields frames in order as the stream delivers them", async () => {
    const stream = streamOf([
      'event: token\ndata: {"text":"Invoices are "}\n\n',
      'event: token\ndata: {"text":"payable net 30."}\n\nevent: citations\ndata: {"citations":[]}\n\n',
    ]);

    const seen = [];
    for await (const frame of readSseFrames(stream)) seen.push(frame.event);

    expect(seen).toEqual(["token", "token", "citations"]);
  });

  it("reassembles a multi-byte character split across chunks", async () => {
    // A stream can split *inside* a UTF-8 sequence. Decoding each chunk independently turns the
    // pound sign into U+FFFD, so an answer quoting a contract's amounts would silently corrupt.
    const encoded = new TextEncoder().encode('event: token\ndata: {"text":"£30"}\n\n');
    const split = encoded.indexOf(0xc2); // the first byte of "£"
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(encoded.slice(0, split + 1));
        controller.enqueue(encoded.slice(split + 1));
        controller.close();
      },
    });

    const frames = [];
    for await (const frame of readSseFrames(stream)) frames.push(frame);

    expect(frames).toEqual([{ event: "token", data: '{"text":"£30"}' }]);
  });

  it("drops an unterminated trailing frame rather than emitting a partial answer", async () => {
    // A connection cut mid-frame must not surface as a truncated-but-valid-looking token.
    const stream = streamOf([
      'event: token\ndata: {"text":"complete"}\n\nevent: token\ndata: {"tex',
    ]);

    const frames = [];
    for await (const frame of readSseFrames(stream)) frames.push(frame);

    expect(frames).toEqual([{ event: "token", data: '{"text":"complete"}' }]);
  });
});

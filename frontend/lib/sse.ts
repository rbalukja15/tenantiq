/**
 * Server-Sent Events parsing for the streaming answer (#19, ADR-0009).
 *
 * The query endpoint is a `POST`, and it needs the BFF's session cookie plus a CSRF header — so the
 * browser's native `EventSource` is unusable (it is GET-only and cannot set headers). The stream is
 * consumed with `fetch` + `ReadableStream` instead, which means parsing the wire format here.
 *
 * The format is small; the hazard is that **nothing about a byte stream respects frame boundaries**.
 * A chunk can end mid-frame, mid-line, or mid-UTF-8 sequence, and which of those happens depends on
 * network timing — so locally, where the whole answer usually arrives in one chunk, every
 * split-related bug is invisible. Hence an incremental parser that buffers, and a reader that
 * decodes with `{ stream: true }` so a multi-byte character split across two chunks survives.
 */

export type SseFrame = { event: string; data: string };

/**
 * A frame ends at a blank line, and the spec permits `\n`, `\r\n` or `\r` line endings — so there
 * are three possible terminators. Only ever acting on a *complete* one is what makes a chunk that
 * ends on a lone `\r` safe: it simply waits, rather than being normalised into a false frame end.
 */
const TERMINATORS = ["\r\n\r\n", "\n\n", "\r\r"] as const;

function findTerminator(buffer: string): { at: number; length: number } | null {
  let earliest: { at: number; length: number } | null = null;
  for (const terminator of TERMINATORS) {
    const at = buffer.indexOf(terminator);
    if (at === -1) continue;
    if (earliest === null || at < earliest.at) earliest = { at, length: terminator.length };
  }
  return earliest;
}

/**
 * One frame's lines into an event. Returns `null` for a frame carrying no `data:` field — a
 * keepalive comment or a stray blank line — because emitting it would look like an empty token and
 * blank the answer mid-stream.
 */
function parseFrame(raw: string): SseFrame | null {
  let event = "message"; // the spec's default when no `event:` field is present
  const data: string[] = [];
  for (const line of raw.split(/\r\n|\n|\r/)) {
    if (line === "" || line.startsWith(":")) continue; // blank, or a comment/keepalive
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const rawValue = colon === -1 ? "" : line.slice(colon + 1);
    // Exactly one optional leading space is part of the framing; a second one is data.
    const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue;
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
    // `id`, `retry` and anything unrecognised are deliberately dropped rather than treated as data.
  }
  return data.length === 0 ? null : { event, data: data.join("\n") };
}

/** Feed it decoded text in any chunking; get back only the frames that are complete. */
export function createSseParser(): { push(text: string): SseFrame[] } {
  let buffer = "";
  return {
    push(text: string): SseFrame[] {
      buffer += text;
      const frames: SseFrame[] = [];
      for (;;) {
        const terminator = findTerminator(buffer);
        if (terminator === null) break;
        const frame = parseFrame(buffer.slice(0, terminator.at));
        buffer = buffer.slice(terminator.at + terminator.length);
        if (frame !== null) frames.push(frame);
      }
      return frames;
    },
  };
}

/**
 * Read a response body as SSE frames.
 *
 * A trailing *unterminated* frame is deliberately dropped rather than flushed: a connection cut
 * mid-frame would otherwise surface as a truncated token that looks like a complete one, which in a
 * product whose claim is faithfulness is the worst possible way to fail.
 */
export async function* readSseFrames(body: ReadableStream<Uint8Array>): AsyncGenerator<SseFrame> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = createSseParser();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      // `stream: true` holds back a trailing partial UTF-8 sequence instead of emitting U+FFFD.
      for (const frame of parser.push(decoder.decode(value, { stream: true }))) yield frame;
    }
  } finally {
    reader.releaseLock();
  }
}

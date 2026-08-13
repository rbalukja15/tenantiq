/**
 * Splitting answer prose into readable text and citation chips (#19).
 *
 * The model writes `[n]` markers inline (ADR-0007); the backend resolves them against the sources it
 * actually retrieved and **drops any number the model invented** (ADR-0008). This function is where
 * that guarantee becomes visible: a chip is rendered only for a number that resolved. An invented
 * `[9]` stays literal text, because a citation you can click that leads nowhere is exactly what this
 * product claims never to produce.
 *
 * It follows that during streaming — when the citations frame, being terminal, has not arrived —
 * nothing is a chip yet. That is the honest rendering rather than a compromise: a marker earns its
 * chip when it is proven to resolve, not when it is written.
 */

export type AnswerSegment = { kind: "text"; text: string } | { kind: "citation"; number: number };

/** `[12]` and nothing else: digits only, so "[sic]" and "[emphasis added]" are left as prose. */
const MARKER = /\[(\d+)\]/g;

export function segmentAnswer(text: string, resolved: ReadonlySet<number>): AnswerSegment[] {
  const segments: AnswerSegment[] = [];
  let cursor = 0;

  // A fresh lastIndex per call: the regex is module-level and `g` regexes carry state.
  MARKER.lastIndex = 0;
  for (let match = MARKER.exec(text); match !== null; match = MARKER.exec(text)) {
    const number = Number(match[1]);
    if (!resolved.has(number)) continue; // unresolved — leave it in the prose, verbatim
    if (match.index > cursor)
      segments.push({ kind: "text", text: text.slice(cursor, match.index) });
    segments.push({ kind: "citation", number });
    cursor = match.index + match[0].length;
  }

  if (cursor < text.length) segments.push({ kind: "text", text: text.slice(cursor) });
  return segments;
}

import { QueryError } from "@/lib/query";

/**
 * Resolving a citation to the passage it points at (#19, over #51's `GET /api/chunks/<id>`).
 *
 * The answer stream names a chunk and gives its offsets but carries no text (ADR-0015), so this is
 * the step that turns a `[1]` into something a reader can check. It goes through the BFF's own
 * origin: the browser holds no API token, and it must not (ADR-0013).
 *
 * The API is snake_case and the UI is camelCase, so the mapping is explicit and in one place — the
 * boundary is the right place for that translation, not thirty call sites.
 */

export type Evidence = {
  chunkId: number;
  index: number;
  /** The stored chunk verbatim: exactly `source[startOffset:endOffset]` (#45). Never reformatted. */
  quote: string;
  startOffset: number;
  endOffset: number;
  document: { id: number; title: string };
};

type ChunkPayload = {
  id: number;
  index: number;
  text: string;
  start_offset: number;
  end_offset: number;
  document: { id: number; title: string };
};

/**
 * @returns the passage, or `null` when the chunk no longer exists.
 *
 * A 404 is an expected outcome, not a failure: deleting a document is allowed while an answer is
 * still on screen (ADR-0015), so a citation can outlive its chunk. The evidence really is gone, and
 * the UI says so — which is more honest than an error banner, and far more honest than leaving a
 * chip that silently does nothing.
 */
export async function resolveEvidence(
  chunkId: number,
  signal?: AbortSignal,
): Promise<Evidence | null> {
  let response: Response;
  try {
    response = await fetch(`/api/chunks/${chunkId}`, { signal });
  } catch {
    throw new QueryError("unavailable", "The source could not be loaded. Please try again.");
  }

  if (response.status === 404) return null;
  if (response.status === 401) {
    throw new QueryError("session_expired", "Your session has ended. Please sign in again.");
  }
  if (!response.ok) {
    // Deliberately not `null`. A 500 does not mean the passage is gone, and reporting it as absent
    // evidence would misstate what the system actually knows.
    throw new QueryError("unavailable", "The source could not be loaded. Please try again.");
  }

  const payload = (await response.json()) as ChunkPayload;
  return {
    chunkId: payload.id,
    index: payload.index,
    quote: payload.text,
    startOffset: payload.start_offset,
    endOffset: payload.end_offset,
    document: payload.document,
  };
}

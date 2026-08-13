import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { QueryError } from "@/lib/query";
import { resolveEvidence } from "@/lib/chunks";
import { server } from "./msw";

/**
 * Resolving a citation to its evidence (#19, over #51's `GET /api/chunks/<id>`).
 *
 * The answer stream names a chunk; this fetches the passage. The interesting case is the 404: a
 * tenant can delete a document while an answer is on screen (ADR-0015 deliberately allows it), so a
 * citation can outlive its chunk. That is not an error to shout about — the evidence really is gone,
 * and saying so is more honest than a failure banner.
 */

const CHUNK_URL = "http://localhost:3000/api/chunks/:id";

const PAYLOAD = {
  id: 12,
  index: 4,
  text: "Customer shall pay each undisputed invoice within thirty (30) days.",
  char_count: 66,
  start_offset: 4821,
  end_offset: 5190,
  document: { id: 3, title: "MSA_Acme_Northwind_2026.pdf" },
};

describe("resolveEvidence", () => {
  it("maps the API's snake_case payload onto the shape the UI renders", async () => {
    server.use(http.get(CHUNK_URL, () => HttpResponse.json(PAYLOAD)));

    expect(await resolveEvidence(12)).toEqual({
      chunkId: 12,
      index: 4,
      quote: "Customer shall pay each undisputed invoice within thirty (30) days.",
      startOffset: 4821,
      endOffset: 5190,
      document: { id: 3, title: "MSA_Acme_Northwind_2026.pdf" },
    });
  });

  it("requests the chunk through the BFF, never the API directly", async () => {
    let url = "";
    server.use(
      http.get(CHUNK_URL, ({ request }) => {
        url = request.url;
        return HttpResponse.json(PAYLOAD);
      }),
    );

    await resolveEvidence(12);

    // The browser holds no API token; only the proxy does (ADR-0013).
    expect(url).toBe("http://localhost:3000/api/chunks/12");
  });

  it("returns null when the chunk is gone rather than raising", async () => {
    // The document was deleted while its answer was still on screen.
    server.use(
      http.get(CHUNK_URL, () => HttpResponse.json({ detail: "Not found." }, { status: 404 })),
    );

    expect(await resolveEvidence(12)).toBeNull();
  });

  it("raises session_expired on a 401 so the page can react as it does elsewhere", async () => {
    server.use(
      http.get(CHUNK_URL, () => HttpResponse.json({ error: "session_expired" }, { status: 401 })),
    );

    await expect(resolveEvidence(12)).rejects.toMatchObject({ kind: "session_expired" });
  });

  it("raises on a server failure instead of pretending the evidence is missing", async () => {
    // A 500 is not "the passage is gone" — reporting it as absent evidence would quietly misstate
    // what the system knows.
    server.use(http.get(CHUNK_URL, () => new HttpResponse(null, { status: 500 })));

    await expect(resolveEvidence(12)).rejects.toBeInstanceOf(QueryError);
  });
});

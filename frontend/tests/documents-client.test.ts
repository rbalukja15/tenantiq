import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  buildUploadForm,
  deleteDocument,
  DocumentError,
  failureFor,
  isInFlight,
  listDocuments,
  messageIn,
  progressOf,
  uploadDocument,
  type DocumentSummary,
} from "@/lib/documents";

import { server } from "./msw";

/**
 * The document client (#20).
 *
 * Two things are asserted *directly* rather than through the fake server, and both for the same
 * reason `lib/upstream.ts` tests its header policy directly: the harness does not carry them
 * faithfully. jsdom serialises a `File` inside an `XMLHttpRequest` `FormData` down to a nine-byte
 * placeholder named `blob`, and the fake server emits exactly one (terminal) upload progress event.
 * A test written through the network would therefore "prove" the filename was sent when it was not,
 * and could never reach a partial percentage at all.
 */

const DOCUMENTS_URL = "http://localhost:3000/api/documents";
const DOCUMENT_URL = "http://localhost:3000/api/documents/:id";

const READY = {
  id: 3,
  title: "MSA_Acme_Northwind_2026.pdf",
  status: "ready",
  error: "",
  attempts: 1,
  content_type: "application/pdf",
  size_bytes: 184_320,
  original_filename: "MSA_Acme_Northwind_2026.pdf",
  created_at: "2026-08-14T09:12:00Z",
  updated_at: "2026-08-14T09:12:40Z",
};

describe("listDocuments", () => {
  it("returns the tenant's documents in the order the API gave them", async () => {
    const second = { ...READY, id: 4, title: "Amendment_2.txt", status: "pending" };
    server.use(http.get(DOCUMENTS_URL, () => HttpResponse.json([READY, second])));

    const documents = await listDocuments();

    expect(documents.map((document) => document.id)).toEqual([3, 4]);
    expect(documents[0]).toEqual({
      id: 3,
      title: "MSA_Acme_Northwind_2026.pdf",
      status: "ready",
      error: "",
      sizeBytes: 184_320,
      createdAt: "2026-08-14T09:12:00Z",
    });
  });

  it("carries the reason a document failed", async () => {
    // #13/#47 put a sanitized reason on the row. Dropping it here would leave the UI able to say
    // "Failed" and nothing else, which is the state a user can do least with.
    server.use(
      http.get(DOCUMENTS_URL, () =>
        HttpResponse.json([{ ...READY, status: "failed", error: "No extractable text." }]),
      ),
    );

    const [document] = await listDocuments();

    expect(document.error).toBe("No extractable text.");
  });

  it("reports an ended session as an ended session", async () => {
    server.use(
      http.get(DOCUMENTS_URL, () =>
        HttpResponse.json({ error: "session_expired" }, { status: 401 }),
      ),
    );

    await expect(listDocuments()).rejects.toMatchObject({ kind: "session_expired" });
  });

  it("does not present a proxy failure as an empty corpus", async () => {
    // The failure mode this pins: a `catch` that returns `[]` renders "No documents yet" to a tenant
    // whose documents are all still there. An empty list is a fact about the corpus, not about the
    // network, and the two must never be confused.
    server.use(
      http.get(DOCUMENTS_URL, () =>
        HttpResponse.json({ error: "upstream_unavailable" }, { status: 502 }),
      ),
    );

    await expect(listDocuments()).rejects.toMatchObject({ kind: "unavailable" });
  });
});

describe("deleteDocument", () => {
  it("sends the double-submit token, without which the proxy answers 403", async () => {
    let sent: string | null = null;
    server.use(
      http.delete(DOCUMENT_URL, ({ request }) => {
        sent = request.headers.get("x-csrf-token");
        return new HttpResponse(null, { status: 204 });
      }),
    );

    await deleteDocument(3, { csrfToken: "token-abc" });

    expect(sent).toBe("token-abc");
  });

  it("treats an already-deleted document as deleted", async () => {
    // Deleted in another tab, or by a colleague. The caller asked for it to be gone and it is gone;
    // raising here would leave a row on screen for a document that no longer exists.
    server.use(
      http.delete(DOCUMENT_URL, () => HttpResponse.json({ detail: "Not found." }, { status: 404 })),
    );

    await expect(deleteDocument(3, { csrfToken: "t" })).resolves.toBeUndefined();
  });

  it("reports a refused delete rather than pretending it worked", async () => {
    server.use(
      http.delete(DOCUMENT_URL, () => HttpResponse.json({ error: "forbidden" }, { status: 403 })),
    );

    await expect(deleteDocument(3, { csrfToken: "t" })).rejects.toBeInstanceOf(DocumentError);
  });
});

describe("uploadDocument", () => {
  const file = () => new File(["contract"], "msa.pdf", { type: "application/pdf" });

  it("posts to the app's own origin with the CSRF token", async () => {
    let method: string | null = null;
    let csrf: string | null = null;
    server.use(
      http.post(DOCUMENTS_URL, ({ request }) => {
        method = request.method;
        csrf = request.headers.get("x-csrf-token");
        return HttpResponse.json({ ...READY, status: "pending" }, { status: 201 });
      }),
    );

    const created = await uploadDocument(file(), { csrfToken: "token-abc" });

    expect(method).toBe("POST");
    expect(csrf).toBe("token-abc");
    expect(created.status).toBe("pending");
  });

  it("does not set content-type itself, so the browser can generate the boundary", async () => {
    // Setting `content-type: multipart/form-data` by hand omits the boundary, and Django then parses
    // an empty form and rejects a perfectly good file. The header must come from the browser.
    let contentType: string | null = null;
    server.use(
      http.post(DOCUMENTS_URL, ({ request }) => {
        contentType = request.headers.get("content-type");
        return HttpResponse.json(READY, { status: 201 });
      }),
    );

    await uploadDocument(file(), { csrfToken: "t" });

    expect(contentType).toMatch(/^multipart\/form-data; boundary=.+/);
  });

  it("surfaces the server's own reason for refusing a file", async () => {
    // Not a message invented on this side: the server states the allowed types, and restating them
    // here is how the two drift apart the day a format is added.
    server.use(
      http.post(DOCUMENTS_URL, () =>
        HttpResponse.json(
          { file: ["Unsupported file type. Allowed: PDF, plain text, Markdown."] },
          { status: 400 },
        ),
      ),
    );

    await expect(uploadDocument(file(), { csrfToken: "t" })).rejects.toMatchObject({
      kind: "rejected",
      message: "Unsupported file type. Allowed: PDF, plain text, Markdown.",
    });
  });

  it("reports progress, and finishes at complete", async () => {
    server.use(http.post(DOCUMENTS_URL, () => HttpResponse.json(READY, { status: 201 })));
    const seen: Array<number | null> = [];

    await uploadDocument(file(), {
      csrfToken: "t",
      onProgress: (progress) => seen.push(progress.fraction),
    });

    expect(seen.length).toBeGreaterThan(0);
    expect(seen.at(-1)).toBe(1);
  });

  it("stops when the caller aborts", async () => {
    server.use(http.post(DOCUMENTS_URL, () => HttpResponse.json(READY, { status: 201 })));
    const controller = new AbortController();
    controller.abort();

    await expect(
      uploadDocument(file(), { csrfToken: "t", signal: controller.signal }),
    ).rejects.toMatchObject({ kind: "aborted" });
  });
});

describe("buildUploadForm", () => {
  it("sends the file under the field name the serializer reads, with its real filename", async () => {
    // Asserted here rather than through the fake server: jsdom's XHR reduces this File to a nine-byte
    // placeholder named "blob", so a network-level assertion would pass while sending nothing.
    const file = new File(["contract"], "MSA_Acme_Northwind_2026.pdf", { type: "application/pdf" });

    const form = buildUploadForm(file);

    const sent = form.get("file");
    expect(sent).toBeInstanceOf(File);
    expect((sent as File).name).toBe("MSA_Acme_Northwind_2026.pdf");
    expect(await (sent as File).text()).toBe("contract");
    // The server defaults the title to the filename; sending our own would be a second source for
    // the same string.
    expect(form.get("title")).toBeNull();
  });
});

describe("progressOf", () => {
  it("reports a fraction when the browser knows the total", () => {
    expect(progressOf({ loaded: 512, total: 2048, lengthComputable: true }).fraction).toBe(0.25);
  });

  it("is indeterminate rather than stuck at zero when the length is unknown", () => {
    // A bar frozen at 0% for a minute reads as a hang. "We cannot say" has its own rendering.
    expect(progressOf({ loaded: 512, total: 0, lengthComputable: false }).fraction).toBeNull();
  });

  it("is indeterminate when the total is zero even if the browser claims otherwise", () => {
    // Guards a division by zero that would otherwise reach the bar as NaN and render nothing at all.
    expect(progressOf({ loaded: 0, total: 0, lengthComputable: true }).fraction).toBeNull();
  });

  it("never exceeds complete", () => {
    expect(progressOf({ loaded: 2100, total: 2048, lengthComputable: true }).fraction).toBe(1);
  });
});

describe("failureFor", () => {
  it.each([
    [401, "session_expired"],
    [403, "stale"],
    [429, "rate_limited"],
    [400, "rejected"],
    [500, "unavailable"],
    [502, "unavailable"],
  ] as const)("maps %i to %s", (status, kind) => {
    expect(failureFor(status, null).kind).toBe(kind);
  });

  it("keeps the server's retry window when it gave one", () => {
    const error = failureFor(429, { detail: "Request was throttled." }, "45");

    expect(error.retryAfterSeconds).toBe(45);
    expect(error.message).toBe("Request was throttled.");
  });

  it("still says something useful when the body is not JSON at all", () => {
    // A gateway's HTML error page must not turn into a blank message.
    expect(failureFor(502, null).message).not.toBe("");
  });
});

describe("messageIn", () => {
  it("prefers DRF's detail", () => {
    expect(messageIn({ detail: "Request was throttled." })).toBe("Request was throttled.");
  });

  it("finds a field error, which is where the useful sentence actually lives", () => {
    expect(messageIn({ file: ["File exceeds the maximum size of 26214400 bytes."] })).toBe(
      "File exceeds the maximum size of 26214400 bytes.",
    );
  });

  it("does not offer the BFF's own error code as a sentence", () => {
    // `{"error": "session_expired"}` is a machine token, not English. Showing it verbatim is how a
    // user ends up reading "session_expired" in a banner.
    expect(messageIn({ error: "session_expired" })).toBeNull();
  });

  it("has nothing to say about a body with no message in it", () => {
    expect(messageIn(null)).toBeNull();
    expect(messageIn({})).toBeNull();
  });
});

describe("isInFlight", () => {
  const at = (status: DocumentSummary["status"]): DocumentSummary => ({
    id: 1,
    title: "x",
    status,
    error: "",
    sizeBytes: 0,
    createdAt: "2026-08-14T09:12:00Z",
  });

  it.each([
    ["pending", true],
    ["processing", true],
    ["ready", false],
    ["failed", false],
  ] as const)("says %s is in flight: %s", (status, expected) => {
    // This predicate is what stops the polling loop. Getting `failed` wrong would poll a terminal
    // document forever; getting `processing` wrong would leave the row stuck on screen until reload.
    expect(isInFlight(at(status))).toBe(expected);
  });
});

/** The CSRF cookie the client must echo; without it every mutation is a 403. */
beforeEach(() => {
  document.cookie = "tiq_csrf=test-token";
});

afterEach(() => {
  document.cookie = "tiq_csrf=; max-age=0";
  vi.restoreAllMocks();
});

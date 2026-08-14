/**
 * The client half of document management (#20), over #10's `/api/documents` and #51's
 * `DELETE /api/documents/<id>`.
 *
 * Every call goes to the BFF's **own origin**, never to Django: the browser holds no API token and
 * must not (ADR-0013). The proxy attaches it.
 *
 * The one structural oddity here is that **uploading uses `XMLHttpRequest` while everything else
 * uses `fetch`**. That is not legacy — `fetch` cannot report upload progress. Its request body is
 * consumed opaquely, and the streaming-request form that would expose it (`duplex: "half"`) is not
 * available for this in any shipping browser. `XMLHttpRequest.upload` has emitted byte-accurate
 * progress events for fifteen years, and a 25 MB PDF on a hotel connection is exactly the case where
 * a determinate bar is the difference between "working" and "broken" (ADR-0017).
 *
 * What the progress means is stated carefully everywhere it surfaces: it counts **bytes accepted by
 * this app's own server**, not ingestion. Ingestion progress is the status pill, and the two are
 * deliberately never merged into one bar — a single bar that jumps to 100% and then sits there for a
 * minute is a worse lie than two honest indicators.
 */

/** Mirrors `Document.Status` in the backend (`app/models.py`). The canonical copy lives here rather
 *  than in the status pill: the set of ingestion states is a fact about the domain, and the pill is
 *  one of its consumers. */
export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

/** A document as the list screen renders it. The endpoint returns more (`attempts`, `content_type`,
 *  `original_filename`, `updated_at`); mapping only what is displayed keeps this from becoming a
 *  second, drifting copy of the serializer. */
export type DocumentSummary = {
  id: number;
  title: string;
  status: DocumentStatus;
  /** The surfaced, already-sanitized reason a document FAILED (#47). Empty otherwise. */
  error: string;
  sizeBytes: number;
  /** ISO-8601, kept as the server sent it — formatting is the view's business. */
  createdAt: string;
};

type DocumentPayload = {
  id: number;
  title: string;
  status: DocumentStatus;
  error?: string;
  size_bytes?: number;
  created_at: string;
};

export type DocumentErrorKind =
  | "session_expired"
  /** The CSRF cookie is missing or no longer matches — the page has been open across a re-login. */
  | "stale"
  /** The server refused this particular file: wrong type, too large. Carries its reason. */
  | "rejected"
  | "rate_limited"
  | "unavailable"
  /** The caller aborted. Not a failure; a caller that cares can tell it apart from one. */
  | "aborted";

export class DocumentError extends Error {
  readonly kind: DocumentErrorKind;
  /** Present only when the server said how long to wait (429 + `Retry-After`). */
  readonly retryAfterSeconds?: number;

  constructor(kind: DocumentErrorKind, message: string, retryAfterSeconds?: number) {
    super(message);
    this.name = "DocumentError";
    this.kind = kind;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

/**
 * The first human-readable string in a DRF error body, whatever shape it took.
 *
 * DRF answers a serializer rejection as `{"file": ["Unsupported file type. …"]}` and everything else
 * as `{"detail": "…"}`, so a client that reads only `detail` shows "That file was rejected" while the
 * server is holding the sentence that says *why*. The field message is the useful one, and it is the
 * server's own copy — never a guess reconstructed on this side.
 */
export function messageIn(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;
  const record = body as Record<string, unknown>;
  if (typeof record.detail === "string" && record.detail !== "") return record.detail;
  for (const value of Object.values(record)) {
    if (typeof value === "string" && value !== "" && value !== "session_expired") return value;
    if (Array.isArray(value) && typeof value[0] === "string" && value[0] !== "") return value[0];
  }
  return null;
}

/**
 * Map a failed response onto something worth showing a person.
 *
 * Pure, and separate from both transports, because the upload path (`XMLHttpRequest`) and the list /
 * delete paths (`fetch`) reach it with completely different objects in hand. Testing the mapping
 * directly is also the only way to pin the 403 and 429 cases: neither is reachable through the fake
 * server without asserting on a message the fake itself invented.
 */
export function failureFor(
  status: number,
  body: unknown,
  retryAfter?: string | null,
): DocumentError {
  if (status === 401) {
    return new DocumentError("session_expired", "Your session has ended. Please sign in again.");
  }
  if (status === 403) {
    return new DocumentError("stale", "This page has gone stale. Please reload and try again.");
  }
  if (status === 429) {
    const seconds = Number.parseInt(retryAfter ?? "", 10);
    return new DocumentError(
      "rate_limited",
      messageIn(body) ?? "Too many uploads. Please wait a moment.",
      Number.isFinite(seconds) ? seconds : undefined,
    );
  }
  if (status === 400 || status === 415) {
    return new DocumentError("rejected", messageIn(body) ?? "That file was rejected.");
  }
  return new DocumentError("unavailable", "TenantIQ is temporarily unavailable. Please try again.");
}

async function failureForResponse(response: Response): Promise<DocumentError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // A non-JSON error body (an nginx page, an empty 502) is not a reason to throw a different,
    // less useful error — the status alone still maps to something sayable.
  }
  return failureFor(response.status, body, response.headers.get("retry-after"));
}

function toSummary(payload: DocumentPayload): DocumentSummary {
  return {
    id: payload.id,
    title: payload.title,
    status: payload.status,
    error: payload.error ?? "",
    sizeBytes: payload.size_bytes ?? 0,
    createdAt: payload.created_at,
  };
}

/** The tenant's documents, oldest first — the order the API returns them in (`order_by("created_at")`),
 *  preserved rather than re-sorted so an upload lands where the next reload will show it. */
export async function listDocuments(signal?: AbortSignal): Promise<DocumentSummary[]> {
  let response: Response;
  try {
    response = await fetch("/api/documents", { signal });
  } catch {
    if (signal?.aborted) throw new DocumentError("aborted", "Cancelled.");
    throw new DocumentError("unavailable", "TenantIQ could not be reached. Please try again.");
  }
  if (!response.ok) throw await failureForResponse(response);
  const payload = (await response.json()) as DocumentPayload[];
  return payload.map(toSummary);
}

/**
 * Delete a document, its chunks and its stored file (#51).
 *
 * A 404 resolves rather than throwing. The caller's intent is "make this go away", and by the time a
 * 404 comes back it *has* gone away — deleted in another tab, or by a colleague. Reporting that as a
 * failure would leave a row on screen for a document that no longer exists, which is the one outcome
 * the user definitely did not ask for.
 */
export async function deleteDocument(
  id: number,
  options: { csrfToken: string; signal?: AbortSignal },
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`/api/documents/${id}`, {
      method: "DELETE",
      headers: { "x-csrf-token": options.csrfToken },
      signal: options.signal,
    });
  } catch {
    if (options.signal?.aborted) throw new DocumentError("aborted", "Cancelled.");
    throw new DocumentError("unavailable", "TenantIQ could not be reached. Please try again.");
  }
  if (response.status === 404) return;
  if (!response.ok) throw await failureForResponse(response);
}

/** How far along an upload is. `fraction` is `null` when the browser cannot say — an indeterminate
 *  bar is honest, a bar frozen at 0% is not. */
export type UploadProgress = { loaded: number; total: number; fraction: number | null };

/** Pure, so the mapping is provable: the fake server emits exactly one (terminal) progress event, so
 *  the interesting cases — a total of zero, a non-computable length — are unreachable through it. */
export function progressOf(event: {
  loaded: number;
  total: number;
  lengthComputable: boolean;
}): UploadProgress {
  const usable = event.lengthComputable && event.total > 0;
  return {
    loaded: event.loaded,
    total: event.total,
    // Clamped: a `loaded` past `total` (some proxies count headers) must not render a 103% bar.
    fraction: usable ? Math.min(1, Math.max(0, event.loaded / event.total)) : null,
  };
}

/**
 * The multipart body an upload sends.
 *
 * Extracted as a pure function for the same reason `lib/upstream.ts` extracts its header policy: the
 * request body does **not** survive this test harness intact. jsdom serialises a `File` inside an
 * `XMLHttpRequest` `FormData` down to a 9-byte placeholder named `blob`, so a test asserting through
 * the fake server would prove the filename was sent when it was not sent at all. Asserted here, where
 * the decision is actually made, and the transport below is then a thin caller of something proven.
 */
export function buildUploadForm(file: File): FormData {
  const form = new FormData();
  // The field name the serializer's write-only `file` field expects. `title` is deliberately not
  // sent: the server defaults it to the uploaded filename, and a second source for the same string
  // is a way for them to disagree.
  form.set("file", file);
  return form;
}

/**
 * Upload one file, reporting progress as its bytes leave the browser.
 *
 * @param onProgress called with bytes-to-our-server, never with ingestion progress — see the module
 *   note. Called at least once, on completion.
 */
export function uploadDocument(
  file: File,
  options: {
    csrfToken: string;
    onProgress?: (progress: UploadProgress) => void;
    signal?: AbortSignal;
  },
): Promise<DocumentSummary> {
  return new Promise((resolve, reject) => {
    if (options.signal?.aborted) {
      reject(new DocumentError("aborted", "Cancelled."));
      return;
    }

    const request = new XMLHttpRequest();
    const abort = () => request.abort();

    const settleFromResponse = () => {
      options.signal?.removeEventListener("abort", abort);
      // `responseType` is left as text and parsed here: a non-JSON error body (a gateway's HTML) must
      // reach `failureFor` as `null` rather than making the whole upload fail as "unavailable" with
      // the status thrown away.
      let body: unknown = null;
      try {
        body = JSON.parse(request.responseText);
      } catch {
        /* not JSON — the status still maps to something sayable */
      }
      if (request.status === 201 || request.status === 200) {
        resolve(toSummary(body as DocumentPayload));
        return;
      }
      reject(failureFor(request.status, body, request.getResponseHeader("retry-after")));
    };

    request.upload.addEventListener("progress", (event) => {
      options.onProgress?.(progressOf(event));
    });
    request.addEventListener("load", settleFromResponse);
    request.addEventListener("error", () => {
      options.signal?.removeEventListener("abort", abort);
      reject(new DocumentError("unavailable", "TenantIQ could not be reached. Please try again."));
    });
    request.addEventListener("abort", () => {
      options.signal?.removeEventListener("abort", abort);
      reject(new DocumentError("aborted", "Cancelled."));
    });

    request.open("POST", "/api/documents");
    // The double-submit half the browser cannot forge cross-origin (lib/csrf.ts). No `content-type`
    // is set: the browser has to generate the multipart boundary itself, and setting the header by
    // hand omits it — which makes Django parse an empty form and reject a perfectly good file.
    request.setRequestHeader("x-csrf-token", options.csrfToken);
    options.signal?.addEventListener("abort", abort);
    request.send(buildUploadForm(file));
  });
}

/** Ingestion is still moving; the list keeps polling while any document is in one of these. */
export function isInFlight(document: DocumentSummary): boolean {
  return document.status === "pending" || document.status === "processing";
}

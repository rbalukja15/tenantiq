"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { DocumentUpload } from "@/app/components/DocumentUpload";
import { Button } from "@/app/components/ui/Button";
import { Callout } from "@/app/components/ui/Callout";
import { DataTable } from "@/app/components/ui/DataTable";
import { EmptyState } from "@/app/components/ui/EmptyState";
import { Loading } from "@/app/components/ui/Loading";
import { StatusPill } from "@/app/components/ui/StatusPill";
import { readCsrfToken } from "@/lib/csrf-client";
import {
  DocumentError,
  deleteDocument,
  isInFlight,
  listDocuments,
  type DocumentSummary,
} from "@/lib/documents";
import { formatBytes, formatTimestamp } from "@/lib/format";

import styles from "./DocumentsScreen.module.css";

/**
 * The tenant's corpus: what is in it, what state each document is in, and how to add or remove one
 * (#20).
 *
 * **Why this polls.** Ingestion is asynchronous — the upload returns a PENDING row and a Celery
 * worker moves it to PROCESSING and then READY or FAILED (#11/#12). The issue's acceptance criterion
 * is that a user can *watch* a document reach ready, so the list has to learn about a transition it
 * did not cause. Polling is the honest tool for that at this size: the alternative worth having is a
 * server-sent stream per tenant, which means a long-lived connection, its own auth story and its own
 * reconnection semantics — a lot of machinery to shave a couple of seconds off a step that already
 * takes as long as an embedding run (ADR-0017).
 *
 * The loop is deliberately **self-limiting**, in both directions:
 *
 *   - it runs only while some document is PENDING or PROCESSING, and stops the moment they are all
 *     terminal — so an idle tab makes no requests at all, rather than a request every few seconds
 *     forever;
 *   - it gives up after a few consecutive failures, so a backend that is down is asked a handful of
 *     times and then left alone, with the last error still on screen.
 *
 * `setTimeout` chained from the *completed* request, never `setInterval`: an interval fires whether
 * or not the previous poll came back, so a slow API turns a status list into a pile-up.
 */

/** How many consecutive polling failures before the loop stops asking. */
const MAX_CONSECUTIVE_FAILURES = 3;

type Props = {
  /** Test seam: the suite drives real transitions rather than mocking the clock. */
  pollIntervalMs?: number;
};

type RowAction = { id: number; state: "confirming" | "deleting" };

export function DocumentsScreen({ pollIntervalMs = 2500 }: Props) {
  const [documents, setDocuments] = useState<DocumentSummary[] | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [action, setAction] = useState<RowAction | null>(null);
  // A single, always-mounted live region — a region inserted together with its message announces
  // nothing, because a live region only reports mutations made after it is registered (#19).
  const [announcement, setAnnouncement] = useState("");
  // Bumped to force the effect to re-run: an immediate refetch after an upload or a delete, and the
  // retry button. Deliberately not a "refresh()" callback — the effect owns the request lifecycle,
  // and a second entry point into it is a second place to forget to cancel.
  const [generation, setGeneration] = useState(0);
  // What the last poll saw, so a transition can be described. A ref rather than reading `documents`
  // inside the updater: React invokes a state updater more than once in development, so anything
  // with an effect in it (announcing) must not live there.
  const previous = useRef<Map<number, DocumentSummary["status"]>>(new Map());

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    let failures = 0;
    // Whether the last *successful* poll saw work in progress. A local, not the `documents` state:
    // this effect deliberately does not depend on `documents` (that would start a fresh chain on
    // every poll), so reading the state here would read the value captured when the effect ran —
    // `null` on the first pass, and `null` forever after.
    let moving = false;

    async function poll() {
      try {
        const next = await listDocuments(controller.signal);
        if (stopped) return;
        failures = 0;
        moving = next.some(isInFlight);
        setFailure(null);
        announceTransitions(previous.current, next, setAnnouncement);
        previous.current = new Map(next.map((document) => [document.id, document.status]));
        setDocuments(next);
        if (moving) timer = setTimeout(poll, pollIntervalMs);
      } catch (error) {
        if (stopped || (error instanceof DocumentError && error.kind === "aborted")) return;
        failures += 1;
        setFailure(
          error instanceof DocumentError
            ? error.message
            : "Your documents could not be loaded. Please try again.",
        );
        // Keep retrying only while there is known work in flight; a failed *first* load stops at
        // once and offers the button instead of hammering a backend that is plainly down.
        if (moving && failures < MAX_CONSECUTIVE_FAILURES) timer = setTimeout(poll, pollIntervalMs);
      }
    }

    void poll();
    return () => {
      stopped = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [pollIntervalMs, generation]);

  const onUploaded = useCallback((created: DocumentSummary) => {
    setAnnouncement(`${created.title} uploaded. Ingestion has started.`);
    // The row comes from the refetch rather than from this payload: one source for what is on
    // screen, and the poll restarts as a side effect of the same bump.
    setGeneration((value) => value + 1);
  }, []);

  const confirmDelete = useCallback(async (target: DocumentSummary) => {
    const csrfToken = readCsrfToken();
    if (csrfToken === null) {
      setAction(null);
      setFailure("This page has gone stale. Please reload and try again.");
      return;
    }
    setAction({ id: target.id, state: "deleting" });
    try {
      await deleteDocument(target.id, { csrfToken });
      setAction(null);
      setFailure(null);
      // Removed here as well as refetched: the server has confirmed, and leaving the row up until
      // the next response makes a completed delete look like it did nothing.
      setDocuments((current) => (current ?? []).filter((document) => document.id !== target.id));
      previous.current.delete(target.id);
      setAnnouncement(`${target.title} deleted.`);
      setGeneration((value) => value + 1);
    } catch (error) {
      setAction(null);
      setFailure(
        error instanceof DocumentError
          ? error.message
          : "That document could not be deleted. Please try again.",
      );
    }
  }, []);

  return (
    <section className={styles.screen}>
      <header className={styles.head}>
        <h1>Documents</h1>
        <p className={styles.lede}>
          Everything here belongs to this workspace, and an answer can only ever cite these
          documents.
        </p>
      </header>

      <p role="status" className={styles.announcement}>
        {announcement}
      </p>

      <DocumentUpload onUploaded={onUploaded} />

      {failure !== null && (
        <div className={styles.failure}>
          <Callout tone="error">{failure}</Callout>
          <Button variant="quiet" onClick={() => setGeneration((value) => value + 1)}>
            Try again
          </Button>
        </div>
      )}

      {documents === null && failure === null && <Loading label="Loading documents…" />}

      {documents !== null &&
        (documents.length === 0 ? (
          <EmptyState title="No documents yet">
            Upload a PDF, plain-text or Markdown file. Once it reaches <strong>Ready</strong> you
            can ask questions about it.
          </EmptyState>
        ) : (
          <DataTable
            caption="Your documents"
            head={
              <tr>
                <th scope="col">Document</th>
                <th scope="col">Status</th>
                <th scope="col" data-numeric>
                  Size
                </th>
                <th scope="col">Uploaded</th>
                <th scope="col">
                  <span className={styles.srOnly}>Actions</span>
                </th>
              </tr>
            }
          >
            {documents.map((document) => (
              <tr key={document.id}>
                <th scope="row">{document.title}</th>
                <td>
                  <StatusPill status={document.status} />
                  {document.status === "failed" && document.error !== "" && (
                    // The reason is already sanitized server-side (#47), and a "Failed" with no
                    // reason is the state a user can do least with.
                    <span className={styles.reason}>{document.error}</span>
                  )}
                </td>
                <td data-numeric>{formatBytes(document.sizeBytes)}</td>
                <td>
                  <time dateTime={document.createdAt}>{formatTimestamp(document.createdAt)}</time>
                </td>
                <td>
                  {action?.id === document.id ? (
                    <div className={styles.confirm}>
                      <p className={styles.warning}>
                        Delete permanently? This also removes every passage retrieved from it, so
                        existing answers will lose their sources.
                      </p>
                      <div className={styles.actions}>
                        <Button
                          variant="quiet"
                          disabled={action.state === "deleting"}
                          aria-label={`Confirm deleting ${document.title}`}
                          onClick={() => confirmDelete(document)}
                        >
                          {action.state === "deleting" ? "Deleting…" : "Delete"}
                        </Button>
                        <Button
                          variant="quiet"
                          disabled={action.state === "deleting"}
                          aria-label={`Keep ${document.title}`}
                          onClick={() => setAction(null)}
                        >
                          Cancel
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <Button
                      variant="quiet"
                      aria-label={`Delete ${document.title}`}
                      onClick={() => setAction({ id: document.id, state: "confirming" })}
                    >
                      Delete
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </DataTable>
        ))}
    </section>
  );
}

/**
 * Say what changed since the last poll — and only that.
 *
 * A document arriving at READY is the thing the user is waiting for and may not be looking at, so it
 * is worth announcing; the polling itself is not. A document seen for the first time is never
 * announced as a transition: on first load every row would otherwise be read out, which buries the
 * one line that matters under the whole corpus.
 */
function announceTransitions(
  before: Map<number, DocumentSummary["status"]>,
  after: DocumentSummary[],
  announce: (message: string) => void,
): void {
  const ready: string[] = [];
  const failed: string[] = [];
  for (const document of after) {
    const was = before.get(document.id);
    if (was === undefined || was === document.status) continue;
    if (document.status === "ready") ready.push(document.title);
    if (document.status === "failed") failed.push(document.title);
  }
  const parts: string[] = [];
  if (ready.length === 1) parts.push(`${ready[0]} is ready to query.`);
  else if (ready.length > 1) parts.push(`${ready.length} documents are ready to query.`);
  if (failed.length === 1) parts.push(`${failed[0]} failed to ingest.`);
  else if (failed.length > 1) parts.push(`${failed.length} documents failed to ingest.`);
  if (parts.length > 0) announce(parts.join(" "));
}

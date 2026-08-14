"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";

import { Button } from "@/app/components/ui/Button";
import { Callout } from "@/app/components/ui/Callout";
import { Loading } from "@/app/components/ui/Loading";
import { ProgressBar } from "@/app/components/ui/ProgressBar";
import { readCsrfToken } from "@/lib/csrf-client";
import { DocumentError, uploadDocument, type DocumentSummary } from "@/lib/documents";

import styles from "./DocumentUpload.module.css";

/**
 * Choose a file and watch its bytes leave the browser (#20).
 *
 * The screen shows **two** kinds of progress and never merges them, which is the whole design of
 * this component (ADR-0017):
 *
 *   1. *Uploading* — a determinate bar, driven by `XMLHttpRequest.upload` progress. It counts bytes
 *      accepted by this app's own server, and nothing else.
 *   2. *Saving* — indeterminate, from the moment the last byte is sent until the 201 comes back. The
 *      server is writing the file and committing a row; a bar parked at 100% for that whole time is
 *      the most common way an upload UI reads as hung.
 *
 * Ingestion is a third thing again, and belongs to the status pill on the row — not here. One bar
 * covering upload *and* ingestion would have to invent the second half, and a fabricated percentage
 * in a product whose claim is faithfulness is not a small lie.
 */

/**
 * A hint for the file picker, not a gate.
 *
 * It mirrors the server's allowed extensions, and that duplication is deliberately *only* tolerable
 * because `accept` cannot refuse anything: a user can still switch the dialog to "All Files", and
 * the upload still goes through. So the day the backend accepts a new format, this list is merely
 * stale rather than a client-side veto on a file the server would have taken. A real client-side
 * type or size check would be a second, drifting copy of a rule the server already owns — which is
 * why the rejection message shown below is the server's own sentence, never one reconstructed here.
 */
const ACCEPT = ".pdf,.txt,.md,application/pdf,text/plain,text/markdown";

type Phase = "idle" | "uploading" | "saving";

export function DocumentUpload({
  onUploaded,
}: {
  onUploaded: (document: DocumentSummary) => void;
}) {
  const inputId = useId();
  const errorId = `${inputId}-error`;
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [fraction, setFraction] = useState<number | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const inFlight = useRef<AbortController | null>(null);

  // An upload outlives a navigation away from this screen unless it is cancelled, and its `then`
  // would then call `setState` on an unmounted tree.
  useEffect(() => () => inFlight.current?.abort(), []);

  const submit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      if (file === null || phase !== "idle") return;

      // Read at submit time, not at render: a login in another tab rotates the cookie.
      const csrfToken = readCsrfToken();
      if (csrfToken === null) {
        setFailure("This page has gone stale. Please reload and try again.");
        return;
      }

      const controller = new AbortController();
      inFlight.current = controller;
      setFailure(null);
      setFraction(0);
      setPhase("uploading");

      try {
        const created = await uploadDocument(file, {
          csrfToken,
          signal: controller.signal,
          onProgress: (progress) => {
            setFraction(progress.fraction);
            // The last byte is gone but the response is not back yet: stop claiming to be uploading.
            if (progress.fraction === 1) setPhase("saving");
          },
        });
        if (controller.signal.aborted) return;
        setPhase("idle");
        setFile(null);
        // The picker keeps the old selection otherwise, so re-choosing the *same* file fires no
        // `change` event and the Upload button stays dead with a filename still showing.
        if (input.current) input.current.value = "";
        onUploaded(created);
      } catch (error) {
        if (error instanceof DocumentError && error.kind === "aborted") return;
        setPhase("idle");
        setFraction(null);
        setFailure(
          error instanceof DocumentError ? error.message : "The upload failed. Please try again.",
        );
      }
    },
    [file, phase, onUploaded],
  );

  const busy = phase !== "idle";

  return (
    <form className={styles.form} onSubmit={submit}>
      <div className={styles.field}>
        <label className={styles.label} htmlFor={inputId}>
          Add a document
        </label>
        <input
          id={inputId}
          ref={input}
          className={styles.input}
          type="file"
          name="file"
          accept={ACCEPT}
          // Deliberately not `required`. The submit button is disabled until a file is chosen and
          // the handler returns early without one, so the native constraint is unreachable — and
          // keeping it costs real coverage: jsdom reports `valueMissing` on a file input even when
          // `files.length` is 1, which blocks submission and makes the whole happy path untestable.
          disabled={busy}
          aria-describedby={failure === null ? `${inputId}-hint` : errorId}
          aria-invalid={failure === null ? undefined : true}
          onChange={(event) => {
            setFile(event.target.files?.[0] ?? null);
            setFailure(null);
          }}
        />
        <p id={`${inputId}-hint`} className={styles.hint}>
          PDF, plain text or Markdown. Answers can only cite documents in this workspace.
        </p>
      </div>

      <Button type="submit" disabled={busy || file === null}>
        {busy ? "Uploading…" : "Upload"}
      </Button>

      <div className={styles.status}>
        {phase === "uploading" && (
          <ProgressBar label={`Uploading ${file?.name ?? "document"}`} fraction={fraction} />
        )}
        {/* Not a second bar: there is no percentage to report, and inventing one here is exactly the
            fabricated measurement this component exists to avoid. */}
        {phase === "saving" && <Loading label="Saving…" />}

        {failure !== null && (
          <Callout id={errorId} tone="error">
            {failure}
          </Callout>
        )}
      </div>
    </form>
  );
}

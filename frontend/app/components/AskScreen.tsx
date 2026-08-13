"use client";

import { useCallback, useRef, useState } from "react";

import { Button } from "@/app/components/ui/Button";
import { Callout } from "@/app/components/ui/Callout";
import { CitationChip } from "@/app/components/ui/CitationChip";
import { EmptyState } from "@/app/components/ui/EmptyState";
import { Loading } from "@/app/components/ui/Loading";
import { NoEvidence } from "@/app/components/ui/NoEvidence";
import { SourceCard, type Source } from "@/app/components/ui/SourceCard";
import { TextField } from "@/app/components/ui/TextField";
import { segmentAnswer } from "@/lib/answer";
import { resolveEvidence, type Evidence } from "@/lib/chunks";
import { readCsrfToken } from "@/lib/csrf-client";
import { QueryError, streamAnswer, type Citation } from "@/lib/query";

import styles from "./AskScreen.module.css";

/**
 * Ask a question, watch the grounded answer arrive, check it against its sources (#19).
 *
 * Laid out as a critical edition: the answer on one side, the retrieved passages on the other,
 * joined by citations that can be selected from either end. That is the product's argument made
 * visible — an answer is only worth as much as the evidence sitting next to it.
 *
 * Three behaviours here are deliberate and easy to get wrong:
 *
 *   - **A marker becomes a chip only once it resolves.** Citations are the stream's *terminal*
 *     frame, so during streaming the prose shows `[1]` as written; when the frame lands, the numbers
 *     that resolved become controls and the ones the model invented stay as text (see `lib/answer`).
 *   - **A refusal is its own state, not a short answer.** The backend says which it is (#19's
 *     `refused` flag); nothing here infers it from an empty citation list, because an answer whose
 *     markers all failed to resolve is still an answer.
 *   - **A partial answer is kept.** If generation fails mid-sentence the tokens that arrived are
 *     real model output; they stay on screen with the failure stated beside them.
 */

type Phase = "idle" | "streaming" | "done";

/**
 * A citation joined to the passage it points at.
 *
 * Three states, not two, and the distinction is not cosmetic: `evidence` present is the normal case;
 * `"deleted"` means the chunk is genuinely gone (a 404 — ADR-0015 allows deleting a document while
 * its answer is still on screen); `"unavailable"` means the fetch failed for some other reason.
 * Collapsing the last two — the natural shape of a `.catch(() => null)` — makes the UI tell a user
 * their document was deleted because of a transient 500, which is the interface stating something
 * false about their data.
 */
type ResolvedSource = {
  citation: Citation;
  evidence: Evidence | null;
  missing: "deleted" | "unavailable" | null;
};

function toCardSource(resolved: ResolvedSource): Source {
  const { citation, evidence } = resolved;
  return {
    n: citation.number,
    documentTitle: citation.document_title,
    // The quote is the stored chunk verbatim (#45). Never re-derived, never trimmed.
    quote: evidence?.quote ?? "",
    chunkId: citation.chunk_id,
    // The offsets come from the same read as the quote when we have one. The citation frame and the
    // chunk endpoint are two reads of the same row, and a re-ingestion between them can move a span
    // (#45) — labelling this text with the other read's offsets would describe a span it no longer
    // occupies. Falling back to the citation's only when there is no quote to contradict.
    startOffset: evidence?.startOffset ?? citation.start_offset,
    endOffset: evidence?.endOffset ?? citation.end_offset,
    // No similarity: the API does not carry one, and inventing a number here would put a fabricated
    // measurement in the panel whose whole purpose is being checkable.
  };
}

export function AskScreen() {
  const [question, setQuestion] = useState("");
  const [asked, setAsked] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [answer, setAnswer] = useState("");
  const [sources, setSources] = useState<ResolvedSource[]>([]);
  const [refused, setRefused] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [active, setActive] = useState<number | null>(null);
  // A single, always-mounted live region. It must exist before the text it announces does: a live
  // region only reports mutations made *after* it is registered, so a region inserted together with
  // its message — which is what a `role="status"` inside a conditionally rendered block is —
  // announces nothing at all.
  const [announcement, setAnnouncement] = useState("");
  // One in flight at a time: asking again abandons the previous answer rather than interleaving two
  // token streams into the same paragraph.
  const inFlight = useRef<AbortController | null>(null);

  const submit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      const trimmed = question.trim();
      if (trimmed === "") return;

      inFlight.current?.abort();
      const controller = new AbortController();
      inFlight.current = controller;

      setAsked(trimmed);
      setPhase("streaming");
      setAnswer("");
      setSources([]);
      setRefused(false);
      setFailure(null);
      setActive(null);
      setAnnouncement("");

      // Read at submit time, not at render: a login in another tab rotates the cookie.
      const csrfToken = readCsrfToken();
      if (csrfToken === null) {
        setPhase("done");
        setFailure("This page has gone stale. Please reload and try again.");
        return;
      }

      let citations: Citation[] = [];
      // Every stream ends with a terminal frame — citations, or error (ADR-0009). If the body stops
      // without one, the connection died mid-answer, and the text on screen is a fragment. Saying so
      // matters more here than anywhere else: a truncated answer *looks* complete.
      let terminated = false;
      let refusedNow = false;
      try {
        for await (const event of streamAnswer(trimmed, { csrfToken, signal: controller.signal })) {
          if (event.type === "token") setAnswer((text) => text + event.text);
          else if (event.type === "citations") {
            terminated = true;
            citations = event.citations;
            refusedNow = event.refused;
            setRefused(event.refused);
          } else {
            terminated = true;
            setFailure(event.message);
          }
        }
      } catch (error) {
        setFailure(
          error instanceof QueryError ? error.message : "Something went wrong. Please try again.",
        );
        setPhase("done");
        return;
      }

      if (controller.signal.aborted) return;
      setPhase("done");
      if (!terminated) {
        setFailure("The connection ended before the answer finished — this answer is incomplete.");
      }

      // Resolve each citation to its passage. One failure must not blank the others, so each is
      // settled independently — a deleted document is a per-source state, not a page-level error.
      const resolved = await Promise.all(
        citations.map(async (citation): Promise<ResolvedSource> => {
          try {
            const evidence = await resolveEvidence(citation.chunk_id, controller.signal);
            return { citation, evidence, missing: evidence === null ? "deleted" : null };
          } catch {
            return { citation, evidence: null, missing: "unavailable" };
          }
        }),
      );
      if (controller.signal.aborted) return;
      setSources(resolved);
      // Announced only on the terminal state, and kept short. A failure is not announced here: the
      // error Callout is already `role="alert"`, and saying it twice is worse than saying it once.
      if (refusedNow) setAnnouncement("No supporting passage found.");
      else if (terminated) {
        setAnnouncement(
          resolved.length === 1
            ? "Answer complete. 1 source."
            : `Answer complete. ${resolved.length} sources.`,
        );
      }
    },
    [question],
  );

  const resolvedNumbers = new Set(sources.map((source) => source.citation.number));
  const segments = segmentAnswer(answer, resolvedNumbers);

  return (
    <div className={styles.screen}>
      <p role="status" className={styles.announcement}>
        {announcement}
      </p>
      <form className={styles.ask} onSubmit={submit}>
        <TextField
          label="Question"
          name="question"
          value={question}
          required
          placeholder="What are the payment terms?"
          onChange={(event) => setQuestion(event.target.value)}
        />
        <Button type="submit" disabled={phase === "streaming"}>
          {phase === "streaming" ? "Asking…" : "Ask"}
        </Button>
      </form>

      {failure !== null && <Callout tone="error">{failure}</Callout>}

      {phase === "idle" && failure === null && (
        <EmptyState title="Ask a question about your documents">
          Answers are drawn only from this workspace&rsquo;s documents, and every claim carries the
          passage it came from.
        </EmptyState>
      )}

      {phase !== "idle" && refused ? (
        <NoEvidence question={asked} />
      ) : (
        phase !== "idle" && (
          <div className={styles.edition}>
            <div className={styles.answer}>
              {/* Deliberately NOT a live region. `aria-live` on text that grows token by token
                  re-announces the whole answer from the beginning on every update, so a listener
                  hears it restart dozens of times and never reaches the end. The answer is read on
                  demand; the status region above announces only that it finished. */}
              <p aria-busy={phase === "streaming"}>
                {segments.map((segment, index) =>
                  segment.kind === "text" ? (
                    <span key={index}>{segment.text}</span>
                  ) : (
                    <CitationChip
                      key={index}
                      n={segment.number}
                      active={active === segment.number}
                      onSelect={setActive}
                    />
                  ),
                )}
                {phase === "streaming" && <span className={styles.caret} aria-hidden="true" />}
              </p>
              {phase === "streaming" && answer === "" && <Loading label="Thinking" />}
            </div>

            {sources.length > 0 && (
              <aside className={styles.sources} aria-label="Sources">
                {sources.map((resolved) => (
                  <div key={resolved.citation.chunk_id}>
                    <SourceCard
                      source={toCardSource(resolved)}
                      active={active === resolved.citation.number}
                      onSelect={setActive}
                    />
                    {resolved.missing !== null && (
                      <p className={styles.missing}>
                        {resolved.missing === "deleted"
                          ? "This passage is no longer available — the document has been deleted."
                          : "This passage could not be loaded. The answer above still cites it."}
                      </p>
                    )}
                  </div>
                ))}
              </aside>
            )}
          </div>
        )
      )}
    </div>
  );
}

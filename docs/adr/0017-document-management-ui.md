# ADR-0017 — Managing a corpus from the browser

- **Status:** Accepted
- **Date:** 2026-08-14

## Context

#20 is the other half of the product's loop: the ask screen (#19) can only cite documents that
someone put there. It needs three things — upload with progress, a list showing ingestion state, and
delete — over endpoints that already exist: `/api/documents` from #10 and
`DELETE /api/documents/<id>` from #51.

Three properties of the system, rather than any UI preference, decided the shape of it.

**`fetch` cannot report upload progress.** Its request body is consumed opaquely; the streaming
request form that would expose it (`duplex: "half"`) is not available for this in any shipping
browser. Every other call in the frontend is a `fetch`, so this is the one place the transport has to
differ, and the reason has to be written down or it reads as legacy.

**Ingestion is asynchronous, and the browser did not start it.** An upload returns a `PENDING` row
and a Celery worker moves it to `PROCESSING` and then `READY` or `FAILED` (#11/#12). The issue's
acceptance criterion is that a user can *watch* a document become queryable, so the list has to learn
about a state change nothing on this page caused.

**Delete is irreversible and takes the citations with it.** ADR-0015 made delete mean *gone*: the
row, the chunks and the raw file. An answer still on screen can therefore lose its evidence
mid-session, and there is no undo.

## Decision

**1. Upload over `XMLHttpRequest`; everything else over `fetch`.** `XMLHttpRequest.upload` has
emitted byte-accurate progress events for fifteen years, and a 25 MB PDF on a hotel connection is
exactly the case where a determinate bar is the difference between "working" and "broken". The
transport is confined to `uploadDocument` in `lib/documents.ts`; nothing else in the app knows.

**2. Three indicators, never merged into one.** What the upload bar measures is stated wherever it
appears: **bytes accepted by this app's own server**. Then:

| Phase       | Indicator                | What it means                                            |
| ----------- | ------------------------ | -------------------------------------------------------- |
| Uploading   | determinate bar          | bytes leaving the browser for the BFF                     |
| Saving      | indeterminate, "Saving…" | last byte sent; the server is writing the file and a row |
| Ingesting   | the row's status pill    | parsing, chunking, embedding — a worker's business       |

A single bar spanning all three would have to invent the second and third stretches. In a product
whose claim is that nothing is fabricated, a made-up percentage is not a small lie — and the
degenerate version of it, a bar parked at 100% while the server works, is the most common way an
upload UI reads as hung.

**3. The list polls, and the loop is self-limiting in both directions.** It runs only while some
document is `PENDING` or `PROCESSING`, and it gives up after three consecutive failures. So an idle
tab makes no requests at all, and a backend that is down is asked a handful of times and then left
alone with the last error on screen. Each poll is scheduled from the *completed* request with
`setTimeout`, never `setInterval` — an interval fires whether or not the previous response arrived,
so a slow API turns a status list into a pile-up.

The alternative worth having is a per-tenant server-sent stream. That means a long-lived connection,
its own authentication story and its own reconnection semantics, to shave a couple of seconds off a
step that already takes as long as an embedding run. Not at this size.

**4. Delete asks first, in the row, and says what it destroys.** Two steps, both keyboard-reachable,
with per-document accessible names (`Delete MSA.pdf`, `Confirm deleting MSA.pdf`) rather than a
column of identical "Delete" buttons that a screen-reader user cannot tell apart. The confirmation
states that the passages go too, because that is the consequence a user cannot see from the row. It
is not a `window.confirm`: that cannot be styled, cannot say this much, and is suppressed outright in
some browsers.

A 404 from the delete resolves rather than raising. The caller asked for the document to be gone, and
by the time a 404 comes back it *is* gone — deleted in another tab, or by a colleague. The row goes.

**5. Validation belongs to the server; the client duplicates only the hint.** The file picker carries
an `accept` list, which is a filter and not a gate — a user can switch the dialog to "All Files" and
the upload still happens. So that duplication can only ever go stale, never veto a file the server
would have taken. There is no client-side type or size check, and the rejection shown is the server's
own sentence, extracted from DRF's field errors rather than reconstructed here. The cost is honest
and accepted: an oversized file is discovered after it has been uploaded.

**6. `/documents` is its own route, and checks the session server-side.** Route gating (`proxy.ts`)
only checks that the session *cookie* exists, so the page repeats the ask screen's real check.
Without it, a cookie that outlived its session record would render the whole screen and then replace
the list with "your session has ended"; a redirect to the login form is both faster and the truth.
The screen is a Client Component rendered as a sibling and never handed the access token — props
crossing a `"use client"` boundary are serialised into the RSC payload and shipped to the browser
(ADR-0013 §1).

## Consequences

**Good.** The loop closes: upload, watch it become ready, ask a question about it, and delete it when
it is stale — with the three kinds of waiting distinguishable at a glance. The upload transport is
isolated in one function, so a future browser that gives `fetch` real upload progress is a change to
one file. Every failure mode has its own copy: a refused file says why, a dead backend says so and
stops asking, a refused delete leaves the row where it was.

**Bad, and accepted.** Polling costs one read per interval per open tab *while work is in flight*;
that is bounded by the per-tenant read throttle (ADR-0011) and by the loop stopping, but it is real,
and it is the first thing to replace if the corpus screen ever gets busy. An oversized upload is
rejected only after it has been sent. The status list is eventually consistent by up to one interval,
so a document can be `READY` for a moment before its row says so.

**A gap in what the harness can prove, stated rather than glossed.** The fake server emits every
upload event *after* the handler has responded — `loadstart`, `progress` and `load` all land one
millisecond before the response — so a partial upload cannot exist there, and jsdom reduces a `File`
inside an `XMLHttpRequest` `FormData` to a nine-byte placeholder named `blob`. Two consequences,
both handled the same way `lib/upstream.ts` handles the equivalent problem for header policy: the
multipart body is asserted against `buildUploadForm` directly, where the decision is made, and the
progress states are driven by one deliberately module-mocked test file
(`document-upload-progress.test.tsx`) that supplies the events the transport would have produced.
A test written through the network would have "proved" the filename was sent when it was not sent
at all.

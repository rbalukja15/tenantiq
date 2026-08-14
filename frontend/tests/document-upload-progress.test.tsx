import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DocumentUpload } from "@/app/components/DocumentUpload";
import type { UploadProgress } from "@/lib/documents";

/**
 * What the upload control does with the progress it is given (#20).
 *
 * This is the one file in the suite that mocks a module rather than the network, and the reason is
 * measured rather than assumed: the fake server emits **every** upload event *after* the handler has
 * already responded — `loadstart`, `progress` and `load` all land one millisecond before the
 * response does. So a partial upload simply cannot exist there, and neither can the state that
 * matters most here: the window after the last byte is sent and before the row is committed.
 *
 * In a browser that window is seconds long — the server is writing a 25 MB file and running a
 * transaction — and a bar parked at 100% through it is the classic way an upload reads as hung.
 * Testing it means supplying the progress the transport would have produced, which is what this
 * does; `document-upload.test.tsx` covers everything reachable through the real fetch path.
 */

let emit: ((progress: UploadProgress) => void) | undefined;
let settle: (() => void) | undefined;

vi.mock("@/lib/documents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/documents")>();
  return {
    ...actual,
    uploadDocument: vi.fn(
      (file: File, options: { onProgress?: (progress: UploadProgress) => void }) =>
        new Promise((resolve) => {
          emit = (progress) => options.onProgress?.(progress);
          settle = () =>
            resolve({
              id: 9,
              title: file.name,
              status: "pending",
              error: "",
              sizeBytes: file.size,
              createdAt: "2026-08-14T09:12:00Z",
            });
        }),
    ),
  };
});

beforeEach(() => {
  document.cookie = "tiq_csrf=test-token";
});

afterEach(() => {
  document.cookie = "tiq_csrf=; max-age=0";
  emit = undefined;
  settle = undefined;
});

/** Start an upload and stop, with the transport held open and under this test's control. */
async function startUpload() {
  const user = userEvent.setup();
  const onUploaded = vi.fn();
  render(<DocumentUpload onUploaded={onUploaded} />);
  await user.upload(
    screen.getByLabelText(/add a document/i),
    new File(["contract"], "msa.pdf", { type: "application/pdf" }),
  );
  await user.click(screen.getByRole("button", { name: /upload/i }));
  await waitFor(() => expect(emit).toBeDefined());
  return { user, onUploaded };
}

describe("DocumentUpload — progress", () => {
  it("reports how far along the upload is", async () => {
    await startUpload();

    emit?.({ loaded: 42, total: 100, fraction: 0.42 });

    const bar = await screen.findByRole("progressbar", { name: /uploading msa\.pdf/i });
    expect(bar).toHaveValue(42);
    // Also as text: a bar carries its meaning in colour and length alone, which WCAG 1.4.1 does not
    // accept and a screen magnifier user cannot use.
    expect(screen.getByText("42%")).toBeInTheDocument();
  });

  it("is indeterminate, not zero, when the browser cannot say how much there is", async () => {
    // `value={0}` would claim nothing has happened yet. A `<progress>` with no value is the
    // platform's own "we cannot say", which assistive technology already knows how to report.
    await startUpload();

    emit?.({ loaded: 0, total: 0, fraction: null });

    const bar = await screen.findByRole("progressbar");
    expect(bar).not.toHaveAttribute("value");
  });

  it("stops claiming to be uploading once the last byte has gone", async () => {
    await startUpload();

    emit?.({ loaded: 100, total: 100, fraction: 1 });

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/saving/i));
    // The bar is gone rather than pinned at 100%, which is what makes the wait read as work in
    // progress rather than as a stall.
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("returns to a usable form once the row is committed", async () => {
    const { onUploaded } = await startUpload();

    emit?.({ loaded: 100, total: 100, fraction: 1 });
    await waitFor(() => expect(screen.getByRole("status")).toBeInTheDocument());
    settle?.();

    await waitFor(() =>
      expect(onUploaded).toHaveBeenCalledWith(expect.objectContaining({ id: 9 })),
    );
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  });
});

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DocumentUpload } from "@/app/components/DocumentUpload";

import { server } from "./msw";

/**
 * The upload control (#20).
 *
 * The interesting behaviour is all in the *states*: an upload that has finished sending but has not
 * been saved yet, a file the server refused, and a picker that has to be emptied afterwards or the
 * same file can never be chosen twice. What the request body contains is proven in
 * `documents-client.test.ts`, directly — jsdom does not carry a `File` through an `XMLHttpRequest`
 * `FormData` intact, so asserting it here would prove nothing.
 */

const DOCUMENTS_URL = "http://localhost:3000/api/documents";

const CREATED = {
  id: 9,
  title: "msa.pdf",
  status: "pending",
  error: "",
  size_bytes: 2048,
  created_at: "2026-08-14T09:12:00Z",
};

const pdf = () => new File(["contract"], "msa.pdf", { type: "application/pdf" });

beforeEach(() => {
  document.cookie = "tiq_csrf=test-token";
});

afterEach(() => {
  document.cookie = "tiq_csrf=; max-age=0";
});

/**
 * @param applyAccept `false` models a user switching the file dialog to "All Files" — which is the
 *   whole reason `accept` is treated as a hint and the server stays the only real gate.
 */
async function choose(file = pdf(), applyAccept = true) {
  // `applyAccept` is a setup option, not an argument to `upload`: by default userEvent silently
  // discards a file the `accept` attribute does not list, exactly as the file dialog's filter would.
  const user = userEvent.setup({ applyAccept });
  const onUploaded = vi.fn();
  render(<DocumentUpload onUploaded={onUploaded} />);
  await user.upload(screen.getByLabelText(/add a document/i), file);
  return { user, onUploaded };
}

describe("DocumentUpload", () => {
  it("will not upload until a file has been chosen", async () => {
    // No handler is registered: a request here would trip the suite's unhandled-request guard.
    render(<DocumentUpload onUploaded={vi.fn()} />);

    expect(screen.getByRole("button", { name: /upload/i })).toBeDisabled();
  });

  it("uploads the chosen file and hands back the created document", async () => {
    server.use(http.post(DOCUMENTS_URL, () => HttpResponse.json(CREATED, { status: 201 })));
    const { user, onUploaded } = await choose();

    await user.click(screen.getByRole("button", { name: /upload/i }));

    await waitFor(() =>
      expect(onUploaded).toHaveBeenCalledWith(
        expect.objectContaining({ id: 9, title: "msa.pdf", status: "pending" }),
      ),
    );
  });

  it("shows how far the upload has got", async () => {
    server.use(
      http.post(DOCUMENTS_URL, async () => {
        await delay(40);
        return HttpResponse.json(CREATED, { status: 201 });
      }),
    );
    const { user } = await choose();

    await user.click(screen.getByRole("button", { name: /upload/i }));

    // Named after the file, not "Progress": a bar with a generic name is unusable the moment there
    // is more than one thing on the page that can be busy.
    await waitFor(() =>
      expect(screen.getByRole("progressbar", { name: /uploading msa\.pdf/i })).toBeInTheDocument(),
    );
  });

  it("shows the server's own reason for refusing a file", async () => {
    // A type the picker's `accept` hint does not list still reaches the server, because a user can
    // switch the dialog to "All Files" — which is exactly why there is no client-side type check
    // restating the allowed formats. That list would drift the day a format is added, and the
    // sentence shown here is the server's own.
    server.use(
      http.post(DOCUMENTS_URL, () =>
        HttpResponse.json(
          { file: ["Unsupported file type. Allowed: PDF, plain text, Markdown."] },
          { status: 400 },
        ),
      ),
    );
    const { user } = await choose(
      new File(["zip"], "archive.zip", { type: "application/zip" }),
      false,
    );

    await user.click(screen.getByRole("button", { name: /upload/i }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/unsupported file type/i),
    );
    // And the field itself is marked, so the message is not just floating text near a valid-looking
    // control.
    expect(screen.getByLabelText(/add a document/i)).toBeInvalid();
  });

  it("lets the user try again after a rejection", async () => {
    let attempts = 0;
    server.use(
      http.post(DOCUMENTS_URL, () => {
        attempts += 1;
        return attempts === 1
          ? HttpResponse.json({ file: ["File exceeds the maximum size."] }, { status: 400 })
          : HttpResponse.json(CREATED, { status: 201 });
      }),
    );
    const { user, onUploaded } = await choose();

    await user.click(screen.getByRole("button", { name: /upload/i }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    // The control must come back, and the chosen file must still be chosen — making the user pick
    // the file again to retry is the fix nobody wants.
    await waitFor(() => expect(screen.getByRole("button", { name: /upload/i })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: /upload/i }));

    await waitFor(() => expect(onUploaded).toHaveBeenCalled());
  });

  it("empties the picker after a success, so the same file can be uploaded again", async () => {
    // Without this the input keeps its selection, choosing that same file fires no `change` event,
    // and the Upload button stays dead with a filename still sitting next to it.
    server.use(http.post(DOCUMENTS_URL, () => HttpResponse.json(CREATED, { status: 201 })));
    const { user, onUploaded } = await choose();

    await user.click(screen.getByRole("button", { name: /upload/i }));
    await waitFor(() => expect(onUploaded).toHaveBeenCalled());

    const input = screen.getByLabelText(/add a document/i) as HTMLInputElement;
    expect(input.value).toBe("");
    expect(screen.getByRole("button", { name: /upload/i })).toBeDisabled();
  });

  it("does not send anything when the CSRF cookie is gone", async () => {
    // No handler: a request here fails the suite. The proxy would answer 403 anyway, but saying so
    // before spending a 25 MB upload on it is the difference between an explanation and a hang.
    document.cookie = "tiq_csrf=; max-age=0";
    const { user } = await choose();

    await user.click(screen.getByRole("button", { name: /upload/i }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/stale|reload/i));
  });

  it("does not let a second upload start on top of the first", async () => {
    let started = 0;
    server.use(
      http.post(DOCUMENTS_URL, async () => {
        started += 1;
        await delay(40);
        return HttpResponse.json(CREATED, { status: 201 });
      }),
    );
    const { user, onUploaded } = await choose();

    const button = screen.getByRole("button", { name: /upload/i });
    await user.click(button);
    await waitFor(() => expect(button).toBeDisabled());
    await waitFor(() => expect(onUploaded).toHaveBeenCalled());

    expect(started).toBe(1);
  });
});

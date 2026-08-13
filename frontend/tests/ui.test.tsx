import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Button } from "@/app/components/ui/Button";
import { Callout } from "@/app/components/ui/Callout";
import { CitationChip } from "@/app/components/ui/CitationChip";
import { DataTable } from "@/app/components/ui/DataTable";
import { EmptyState } from "@/app/components/ui/EmptyState";
import { Loading } from "@/app/components/ui/Loading";
import { NoEvidence } from "@/app/components/ui/NoEvidence";
import { SourceCard, type Source } from "@/app/components/ui/SourceCard";
import { StatusPill } from "@/app/components/ui/StatusPill";
import { TextField } from "@/app/components/ui/TextField";

/**
 * These assert **semantics**, never appearance.
 *
 * jsdom does not apply a CSS Module, so a test that checked for a class name would prove only that a
 * string was passed around — the visual result is verified by `next build` plus screenshots on the
 * PR. What is testable, and what actually breaks in use, is the accessible structure: names, roles,
 * associations, and whether meaning survives without colour.
 */

describe("Button", () => {
  it("does not submit a form unless it says it does", () => {
    // An unspecified <button> inside a form defaults to type="submit" in HTML — the classic
    // accidental-submit bug. The default here is the safe one.
    render(<Button>Save</Button>);

    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("type", "button");
  });

  it("can still be an explicit submit", () => {
    render(<Button type="submit">Continue</Button>);

    expect(screen.getByRole("button", { name: "Continue" })).toHaveAttribute("type", "submit");
  });

  it("does not fire while disabled", async () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Send
      </Button>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(onClick).not.toHaveBeenCalled();
  });
});

describe("TextField", () => {
  it("gives the input a real, associated label", () => {
    // Not a placeholder: a placeholder disappears the moment someone types, and leaves screen-reader
    // users with an unnamed field.
    render(<TextField label="Workspace" name="tenant" />);

    expect(screen.getByLabelText("Workspace")).toHaveAttribute("name", "tenant");
  });

  it("associates the hint with the input rather than leaving it floating", () => {
    render(<TextField label="Workspace" hint="The short name for your organisation." />);

    expect(screen.getByLabelText("Workspace")).toHaveAccessibleDescription(
      "The short name for your organisation.",
    );
  });

  it("keeps labels distinct when two fields share a page", () => {
    render(
      <>
        <TextField label="Workspace" />
        <TextField label="Email" />
      </>,
    );

    // Would fail on a hardcoded id: both labels would point at the same input.
    expect(screen.getByLabelText("Workspace")).not.toBe(screen.getByLabelText("Email"));
  });
});

describe("Callout", () => {
  it("announces an error assertively", () => {
    render(<Callout tone="error">Could not load your session.</Callout>);

    expect(screen.getByRole("alert")).toHaveTextContent("Could not load your session.");
  });

  it("uses a polite status for anything that is not an error", () => {
    // Marking every message an alert trains people to ignore alerts.
    render(<Callout tone="info">Processing has started.</Callout>);

    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Processing has started.");
  });
});

describe("StatusPill", () => {
  it.each([
    ["ready", "Ready"],
    ["processing", "Processing"],
    ["failed", "Failed"],
    ["pending", "Pending"],
  ] as const)("states %s in words, not only in colour", (status, label) => {
    // WCAG 1.4.1: colour must never be the sole carrier of meaning. A colour-only pill is
    // meaningless to a colour-blind user and to anyone reading the page as text.
    render(<StatusPill status={status} />);

    expect(screen.getByText(label)).toBeInTheDocument();
  });
});

describe("CitationChip", () => {
  it("is an operable control with a name a screen reader can use", () => {
    // A styled <span> would be invisible to the keyboard, and "2" alone tells a listener nothing.
    render(<CitationChip n={2} />);

    expect(screen.getByRole("button", { name: "Source 2" })).toBeInTheDocument();
  });

  it("reports whether it is the selected citation", () => {
    const { rerender } = render(<CitationChip n={1} />);
    expect(screen.getByRole("button")).toHaveAttribute("aria-pressed", "false");

    rerender(<CitationChip n={1} active />);
    expect(screen.getByRole("button")).toHaveAttribute("aria-pressed", "true");
  });

  it("reports which citation was chosen", async () => {
    const onSelect = vi.fn();
    render(<CitationChip n={3} onSelect={onSelect} />);

    await userEvent.click(screen.getByRole("button", { name: "Source 3" }));

    expect(onSelect).toHaveBeenCalledWith(3);
  });
});

describe("SourceCard", () => {
  const source: Source = {
    n: 1,
    documentTitle: "MSA_Acme_Northwind_2026.pdf",
    quote:
      "Customer shall pay each undisputed invoice within thirty (30) days of the invoice date.",
    chunkId: 12,
    startOffset: 4821,
    endOffset: 5190,
    similarity: 0.8312,
  };

  it("renders without a similarity score, because no API supplies one", () => {
    // #51's citations frame and chunk endpoint both omit similarity — it exists only on the
    // backend's internal Source dataclass. Requiring it here made the type unbuildable from real
    // data, and rendering `undefined.toFixed(2)` threw. Absent means absent: no "sim" row at all,
    // rather than a fabricated 0.00 sitting in the evidence panel looking like a measurement.
    const { similarity, ...withoutScore } = source;
    void similarity;

    render(<SourceCard source={withoutScore} />);

    expect(screen.getByText(withoutScore.quote)).toBeInTheDocument();
    expect(screen.queryByText(/sim/i)).toBeNull();
  });

  it("shows the stored chunk text verbatim", () => {
    // The product's central claim is that a citation resolves to real text. Truncating or tidying
    // the quote here would quietly defeat that, so the whole string must be present.
    render(<SourceCard source={source} />);

    expect(screen.getByText(source.quote)).toBeInTheDocument();
  });

  it("shows the span and similarity so the reader can go and check", () => {
    render(<SourceCard source={source} />);

    expect(screen.getByText("chunk 12")).toBeInTheDocument();
    expect(screen.getByText("4821–5190")).toBeInTheDocument();
    expect(screen.getByText("sim 0.83")).toBeInTheDocument();
  });

  it("keeps the evidence out of the control's accessible name", () => {
    // The failure this pins: making the whole card a <button> hides every descendant from the
    // accessibility tree (role=button is children-presentational), so the quote, chunk id and
    // offsets survive only as one unpunctuated accessible name — present, and unreadable.
    render(<SourceCard source={source} onSelect={() => {}} />);

    const control = screen.getByRole("button");
    expect(control).toHaveAccessibleName("Show source 1 in the answer");
    expect(control.textContent).not.toContain(source.quote);
    // The quote is real content in its own right, not a fragment of a label.
    expect(screen.getByText(source.quote)).toBeInTheDocument();
  });

  it("has no control at all when it is display-only", () => {
    // No focusable no-op in the tab order for a card nobody can act on.
    render(<SourceCard source={source} />);

    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText(source.quote)).toBeInTheDocument();
  });

  it("is selectable and reports its selection state", async () => {
    const onSelect = vi.fn();
    render(<SourceCard source={source} onSelect={onSelect} active />);

    const card = screen.getByRole("button");
    expect(card).toHaveAttribute("aria-pressed", "true");

    await userEvent.click(card);
    expect(onSelect).toHaveBeenCalledWith(1);
  });
});

describe("NoEvidence — the refusal state", () => {
  const refusal = () => screen.getByRole("region", { name: "No supporting passage found" });

  it("says plainly that nothing was found, and does not answer", () => {
    render(<NoEvidence question="What are the payment terms?" />);

    // A named region, not role="status": `status` implies aria-atomic, which would flatten the
    // heading, the explanation and both suggestions into a single structureless utterance.
    expect(refusal()).toBeInTheDocument();
    expect(screen.getByRole("heading")).toHaveTextContent("No supporting passage found");
  });

  it("offers no citations, because there is nothing to cite", () => {
    // The failure this guards against: a refusal that grows a source list or a citation chip and
    // starts to look like a confident answer with the evidence merely collapsed.
    render(<NoEvidence question="What are the payment terms?" />);

    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByText(/^Source \d+$/)).toBeNull();
  });

  it("quotes the question back without claiming to have answered it", () => {
    render(<NoEvidence question="What are the payment terms?" />);

    expect(refusal()).toHaveTextContent("What are the payment terms?");
    expect(refusal()).toHaveTextContent(/answered nothing/i);
  });

  it("leaves a space before the quoted question", () => {
    // JSX strips whitespace around a newline before an expression, so the obvious formatting
    // renders "close enough toWhat are the payment terms?" — invisible in a screenshot, because
    // the <q> element's own margin fakes the gap, and wrong for anyone listening to it.
    render(<NoEvidence question="What are the payment terms?" />);

    expect(refusal().textContent).toContain("close enough to ");
    expect(refusal().textContent).not.toContain("toWhat");
  });

  it("works with no question supplied", () => {
    render(<NoEvidence />);

    expect(screen.getByRole("heading")).toBeInTheDocument();
  });
});

describe("Loading", () => {
  it("announces progress in words, not with a bare spinner", () => {
    render(<Loading label="Retrieving passages…" />);

    expect(screen.getByRole("status")).toHaveTextContent("Retrieving passages…");
  });
});

describe("EmptyState", () => {
  it("names what is empty and can carry a next action", () => {
    render(
      <EmptyState title="No documents yet" action={<Button>Upload</Button>}>
        Upload a PDF to start asking questions.
      </EmptyState>,
    );

    expect(screen.getByRole("heading")).toHaveTextContent("No documents yet");
    expect(screen.getByRole("button", { name: "Upload" })).toBeInTheDocument();
  });
});

describe("DataTable", () => {
  it("is a labelled, keyboard-reachable scroll region", () => {
    // A wide table must scroll itself rather than the page — and a scrollable region that cannot be
    // focused is unreachable for anyone not using a mouse.
    render(
      <DataTable
        caption="Documents"
        head={
          <tr>
            <th>Title</th>
          </tr>
        }
      >
        <tr>
          <td>MSA.pdf</td>
        </tr>
      </DataTable>,
    );

    const region = screen.getByRole("region", { name: "Documents" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("table")).toBeInTheDocument();
  });
});

import { describe, expect, it } from "vitest";

import { segmentAnswer } from "@/lib/answer";

/**
 * Turning answer prose into text and citation chips (#19).
 *
 * The model writes `[n]` markers inline, and the backend resolves them to real chunks — dropping any
 * number it invented. This function is where the project's central invariant lands in the UI: **a
 * chip is only ever rendered for a number that actually resolved.** An invented `[9]` stays literal
 * text, because a citation you can click that leads nowhere is precisely the thing the whole product
 * claims not to do.
 */

const KNOWN = new Set([1, 2]);

describe("segmentAnswer", () => {
  it("returns a single run for prose with no markers", () => {
    expect(segmentAnswer("Invoices are payable net 30.", KNOWN)).toEqual([
      { kind: "text", text: "Invoices are payable net 30." },
    ]);
  });

  it("splits a marker out of the surrounding prose", () => {
    expect(segmentAnswer("Net 30[1] applies.", KNOWN)).toEqual([
      { kind: "text", text: "Net 30" },
      { kind: "citation", number: 1 },
      { kind: "text", text: " applies." },
    ]);
  });

  it("keeps a marker that resolved to nothing as literal text", () => {
    // The backend drops an invented number from the citations list but leaves it in the prose. A
    // chip here would be a dead link in the one place the product cannot afford one.
    expect(segmentAnswer("Net 30[9] applies.", KNOWN)).toEqual([
      { kind: "text", text: "Net 30[9] applies." },
    ]);
  });

  it("handles several markers, including adjacent ones", () => {
    expect(segmentAnswer("Both[1][2] agree.", KNOWN)).toEqual([
      { kind: "text", text: "Both" },
      { kind: "citation", number: 1 },
      { kind: "citation", number: 2 },
      { kind: "text", text: " agree." },
    ]);
  });

  it("handles a marker at the very start", () => {
    expect(segmentAnswer("[1] says so.", KNOWN)).toEqual([
      { kind: "citation", number: 1 },
      { kind: "text", text: " says so." },
    ]);
  });

  it("reads multi-digit numbers", () => {
    expect(segmentAnswer("See[12].", new Set([12]))).toEqual([
      { kind: "text", text: "See" },
      { kind: "citation", number: 12 },
      { kind: "text", text: "." },
    ]);
  });

  it("leaves bracketed text that is not a number alone", () => {
    // Contracts are full of "[sic]", "[emphasis added]" and section markers.
    expect(segmentAnswer("The clause [sic] applies.", KNOWN)).toEqual([
      { kind: "text", text: "The clause [sic] applies." },
    ]);
  });

  it("leaves a half-arrived marker as text while the answer is still streaming", () => {
    // Mid-stream the buffer can end anywhere. A chip must not flicker into existence on "[1".
    expect(segmentAnswer("Net 30[1", KNOWN)).toEqual([{ kind: "text", text: "Net 30[1" }]);
  });

  it("returns nothing for empty prose", () => {
    expect(segmentAnswer("", KNOWN)).toEqual([]);
  });

  it("renders no chips at all while the citations frame has not arrived", () => {
    // Citations are the *terminal* frame, so during streaming nothing is known to resolve yet.
    // Markers becoming chips only once they are proven is the honest behaviour, not a compromise.
    expect(segmentAnswer("Net 30[1] applies.", new Set())).toEqual([
      { kind: "text", text: "Net 30[1] applies." },
    ]);
  });
});

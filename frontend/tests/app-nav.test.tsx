import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppNav } from "@/app/components/AppNav";

/**
 * The signed-in area's section navigation (#20).
 *
 * Small, and worth testing for one reason: the "current page" marker is the part that is easy to get
 * wrong in a way nobody sees in a screenshot, because the underline still looks right.
 */

const pathname = vi.fn(() => "/");
vi.mock("next/navigation", () => ({ usePathname: () => pathname() }));

describe("AppNav", () => {
  it("reaches both surfaces of the app", () => {
    render(<AppNav />);

    expect(screen.getByRole("link", { name: "Ask" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: "Documents" })).toHaveAttribute("href", "/documents");
  });

  it("says which section you are in, rather than only colouring it", () => {
    // WCAG 1.4.1: the underline is not available to anyone listening to the page, and "current" is
    // exactly the kind of state a screen-reader user has no other way to discover.
    pathname.mockReturnValue("/documents");

    render(<AppNav />);

    expect(screen.getByRole("link", { name: "Documents" })).toHaveAttribute("aria-current", "page");
    // The trap this pins: `pathname.startsWith(href)` marks Ask current everywhere, because "/" is a
    // prefix of every path in the app.
    expect(screen.getByRole("link", { name: "Ask" })).not.toHaveAttribute("aria-current");
  });

  it("is a named landmark, so it can be skipped to and skipped over", () => {
    pathname.mockReturnValue("/");

    render(<AppNav />);

    expect(screen.getByRole("navigation", { name: "Sections" })).toBeInTheDocument();
  });
});

import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { UsageChart } from "@/app/components/UsageChart";

/**
 * One usage chart (#27).
 *
 * jsdom draws nothing, so these assert what a reader can get from the chart without seeing it: a
 * name for the drawing, a legend when colour alone would have to tell two lines apart, and the
 * day-by-day readout, which has to answer the keyboard as well as the pointer.
 */

const DATES = ["2026-10-20", "2026-10-21", "2026-10-22"];

function answers() {
  return render(
    <UsageChart
      title="Answers per day"
      summary="Answers per day, Oct 20 to Oct 22: 5 in total."
      dates={DATES}
      series={[{ label: "Answers", values: [2, 0, 3], display: ["2", "0", "3"] }]}
      unit="count"
    />,
  );
}

function tokens() {
  return render(
    <UsageChart
      title="Tokens per day"
      summary="Tokens per day."
      dates={DATES}
      series={[
        { label: "Input", values: [3000, 0, 333], display: ["3,000", "0", "333"] },
        { label: "Output", values: [1500, 0, 777], display: ["1,500", "0", "777"] },
      ]}
      unit="count"
    />,
  );
}

const readout = () => screen.queryByText(/^(Tue|Wed|Thu), Oct 2\d, 2026$/);

describe("UsageChart", () => {
  it("names the drawing with its summary, for a reader who cannot see it", () => {
    answers();

    expect(
      screen.getByRole("img", { name: "Answers per day, Oct 20 to Oct 22: 5 in total." }),
    ).toBeInTheDocument();
    expect(screen.getByRole("figure")).toHaveTextContent(/^Answers per day/);
  });

  it("keys two series in a legend, and a single one by its title alone", () => {
    const { unmount } = tokens();
    const legend = screen.getByRole("list");
    expect(legend).toHaveTextContent("Input");
    expect(legend).toHaveTextContent("Output");
    unmount();

    answers();
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("reads each day out from the keyboard, starting with the most recent", async () => {
    tokens();
    const plot = screen.getByRole("group", { name: "Tokens per day, day by day" });
    expect(readout()).toBeNull();

    await userEvent.tab();
    expect(plot).toHaveFocus();
    expect(screen.getByText("Thu, Oct 22, 2026")).toBeInTheDocument();
    expect(screen.getByText("333")).toBeInTheDocument();
    expect(screen.getByText("777")).toBeInTheDocument();

    await userEvent.keyboard("{ArrowLeft}{ArrowLeft}");
    expect(screen.getByText("Tue, Oct 20, 2026")).toBeInTheDocument();
    expect(screen.getByText("3,000")).toBeInTheDocument();

    await userEvent.keyboard("{ArrowLeft}"); // already on the first day: it stays there
    expect(screen.getByText("Tue, Oct 20, 2026")).toBeInTheDocument();

    await userEvent.keyboard("{End}");
    expect(screen.getByText("Thu, Oct 22, 2026")).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    expect(readout()).toBeNull();
  });

  it("says the day it moved to, for a keyboard user who cannot see the tooltip", async () => {
    tokens();

    await userEvent.tab();
    await userEvent.keyboard("{Home}");

    expect(screen.getByText("Tue, Oct 20, 2026: Input 3,000, Output 1,500")).toHaveAttribute(
      "aria-live",
      "polite",
    );
  });

  it("snaps the pointer to the nearest day, and lets go when it leaves", () => {
    answers();
    const drawing = screen.getByRole("img");
    // jsdom does no layout, so the box is given: the drawing at the width it was drawn at.
    drawing.getBoundingClientRect = () => ({ left: 0, width: 640 }) as DOMRect;

    // The plot runs from x=56 to x=624: the middle day sits at 340, the last at 624.
    fireEvent.pointerMove(drawing, { clientX: 330 });
    expect(screen.getByText("Wed, Oct 21, 2026")).toBeInTheDocument();

    fireEvent.pointerMove(drawing, { clientX: 639 });
    expect(screen.getByText("Thu, Oct 22, 2026")).toBeInTheDocument();

    fireEvent.pointerLeave(drawing);
    expect(readout()).toBeNull();
  });

  it("prints the amount the server formatted, not one recomputed from the number", async () => {
    // The geometry needs a number; the tooltip must not round it back into a different string.
    render(
      <UsageChart
        title="Estimated cost per day"
        summary="Estimated cost per day."
        dates={["2026-10-22"]}
        series={[{ label: "Estimated cost", values: [0.0105], display: ["$0.0105"] }]}
        unit="usd"
      />,
    );

    await userEvent.tab();

    expect(screen.getByText("$0.0105")).toBeInTheDocument();
  });
});

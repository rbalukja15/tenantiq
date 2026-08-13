# ADR-0014 — Frontend design system: CSS Modules over tokens

- **Status:** Accepted
- **Date:** 2026-08-12

## Context

#18 shipped a working app shell — login, route gating, tenant view, logout — with **no styling at
all**, which was right for an auth issue and wrong to leave standing. #19 (streaming chat with
citations) and #20 (document management) each add substantial UI. Without a shared system they would
each invent colours, spacing and states, and the project would end up with three visual languages
plus a retrofit.

The frontend had no CSS solution of any kind: no Tailwind, no CSS Modules in use, no stylesheet.
That is a real architectural decision — it determines the build pipeline, how every component in
#19/#20 is authored, and how theming works — so it gets recorded rather than settled implicitly by
whoever wrote the first `className`.

The visual direction is fixed separately and is not re-argued here: the product's differentiator is
that every answer is traceable to a real chunk at real character offsets, so the interface is laid
out like a critical edition — answer on one side, retrieved source on the other, joined by
citations. What follows is how that gets built.

## Decision

### 1. CSS Modules, over design tokens defined in one global stylesheet

`app/global.css` defines tokens and styles bare elements. It is imported **once**, in the root
layout, and defines no reusable classes. Everything else is a `.module.css` scoped to its component.

Why not the alternatives:

- **Tailwind** is what most people expect to see, and it is genuinely faster for conventional
  layouts. It fits this design badly: three font roles, a critical-edition split, and citation
  markers that sit at a specific optical offset all become arbitrary values, so the utility strings
  get long while a `tailwind.config` still has to carry the palette. The deciding factor is that this
  repository is meant to be **read** — a reviewer following a diff learns more from `.source[data-active]`
  than from a forty-class string. It also adds a build dependency the project does not currently have.
- **A single global stylesheet** with semantic class names is the simplest possible setup and is what
  the mockup itself used. Rejected because nothing scopes it: as #19 and #20 add components, every
  class name is a global name, and the cascade collisions that follow are exactly the failure mode
  CSS Modules removes for free. Next also warns that global styles are not unloaded on navigation.
- **CSS-in-JS** is a poor fit for Server Components and would add a runtime for no benefit here.

CSS Modules are native to Next, need no new dependency, and scope by default. Theming is a token
swap, which works because tokens are plain custom properties rather than a framework concept.

**One ordering constraint follows from this.** Next orders CSS by *import order*, and only
`next build` shows the final order. `global.css` is therefore imported in exactly one place — the
root layout — so token definitions always precede the modules that read them. A second import
elsewhere would be a silent hazard.

### 2. Tokens are the interface; components never hardcode a colour

Every colour, size, space and radius is a custom property. A component reads tokens; it does not
contain a hex value. Two token decisions carry more weight than the rest:

- **The accent means "grounded and cited", and appears nowhere else.** Status colours
  (ready / processing / failed) are a separate family, so a red "Failed" pill never competes with the
  citation accent for attention.
- **Two line weights, split on a WCAG boundary rather than an aesthetic one.** `--rule` and
  `--rule-soft` are decorative — card edges, row dividers — which the spec does not require to be
  perceivable, because the component is identifiable without them. `--rule-strong` is for a boundary
  that *is* the control, such as a text input's edge, where 1.4.11 requires 3:1. This split exists
  because the contrast test below rejected a single hairline used for both: holding every divider to
  3:1 turns the interface into a spreadsheet, and using the hairline on a field makes the field
  invisible to a low-vision user.

### 3. Type carries meaning, in three roles

- **serif** — prose: answers and source quotations. Text to be *read*.
- **sans** — interface: chrome, labels, controls. Text to be *operated*.
- **mono** — evidence: chunk ids, character offsets, similarity, money. Text to be *verified*.

Seeing mono is a signal that the value is a fact you could go and check. System font stacks: a
webfont would mean either a network request or a large inlined payload, and neither is worth it.

### 4. The refusal state is a primitive, not a branch inside the chat UI

`NoEvidence` — what a reader sees when nothing clears the similarity floor — ships here rather than
in #19. In a product whose entire claim is grounded answers, "we found nothing" is the most important
screen, and building it inside the chat component is how it becomes a grey box saying "No results".
It is set in the interface sans rather than the answer serif, carries no citations, and states what
the reader can do next.

### 5. What is tested, and what is not

Most "CSS tests" are theatre: jsdom does not apply a CSS Module, so asserting that a component
received a class name proves only that a string was passed around. So:

- **Component tests assert semantics** — accessible names, roles, label associations, and whether
  meaning survives without colour (a status pill states its status in words, per WCAG 1.4.1).
- **`tests/tokens.test.ts` reads the stylesheet as data** and checks the two properties that are
  objective: that both themes define the same token set, and that every text pair meets WCAG AA
  (4.5:1) and every control boundary meets 3:1 — in *both* themes. This catches the regression this
  system is most prone to: a colour nudged until it looks right in one theme and fails in the other.
- **Appearance is verified by `next build` plus screenshots on the PR**, and that limit is stated
  rather than papered over with assertions that cannot fail.

## Consequences

- **Easier.** #19 and #20 can build UI without choosing a colour, a size, or a spacing value —
  everything they need exists as a token or a primitive (button, field, callout, status pill, data
  table, citation chip, source card, empty / loading / refusal states). Dark mode comes free for any
  component that reads tokens. A contrast regression fails CI instead of shipping.
- **Harder / accepted.** CSS Modules mean a second file per component, and shared styles have to be
  extracted deliberately rather than composed inline. There is no theme *toggle* — the app follows
  the operating system's `prefers-color-scheme` — because a toggle needs somewhere to persist the
  choice, and that is not worth a cookie or a hydration boundary yet.
- **The system is unfinished by design.** It covers the screens that exist plus the primitives #19
  and #20 were specified to need. The ask screen itself is #19's, and the first component that needs
  a token this file does not have should add it here rather than reach for a hex value.

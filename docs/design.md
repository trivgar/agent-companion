# The look

The rules behind the dashboard's look. A change that breaks one of these is a
change to the design, not a detail.

## Colour

- Two themes, both first-class: warm paper by day, dusk navy by night. The
  tokens at the top of `static/base.css` carry the whole difference.
- No feature hardcodes a colour. If a rule needs one that is not a token, the
  token is what is missing.
- Neutrals are tinted, not grey: cool in the dark, warm in the light.
- Hue is identity. A session's hue marks that session and nothing else.
- Status reads through weight, fill and position, not through a palette.
- `--alert` (orange) means "this needs you" and nothing else. It is never
  chrome, never a link colour, never a cost.
- Text meets WCAG AA on every ground it can sit on, including the warmest
  point of the dusk sky.

## Ground

- The sky is on `<html>` and `<body>` is transparent. A feature never paints a
  page background.
- Anything fixed or sticky over scrolling content paints `--sky-top`.

## Type and touch

- One family, the system's. Hierarchy comes from size, weight and `--dim`.
- Labels that name a group are small, uppercase and letter-spaced.
- Figures use tabular numbers.
- Every tap target is at least 44px tall on a phone. A word that is the
  target gets a hit area wider than itself.

## Motion

- One easing for everything that arrives: `--ease-out`.
- Motion stops for the system's reduced-motion setting and for the app's own
  switch (`still.js`, under "more").

## Files

| File | Holds |
|---|---|
| `base.css` | Tokens, the sky, reset, motion stop, shared primitives |
| `shell.css` | Header, usage strip, navigation, the "more" menu |
| `still.js` | The animations switch |
| `card.css` | Session groups, the card, its figure slot, badges, details, actions |
| `card.js` | A card as an ordered list of parts |
| `sprite.js` | The session figure and its state overlays |

## Cards and sprites

**The card** is built by `card.js` from parts in a fixed order: `frame`, `top`,
`said`, `more`, `close`. A feature that adds to the card adds a part.

- Closed, a card shows the figure (40px), the name with its state dot, flags
  (`needs you`, `new`), the project at the right, and the last word in two
  lines at most. A last word older than 15 minutes carries its age.
- Open (a tap, Enter or Space on the card), it adds the meta line (branch,
  model, cost, state and when it last wrote, context once it reaches 70%, the
  cache chip), the title, background jobs, the context bar and the actions.
  Which cards are open is remembered by key, so a refresh does not close one.
- Three groups in this order, each with a count: Needs you, Working, Idle. An
  empty group is not drawn. The server's order is kept inside a group.
- A waiting card takes the `--alert` border and edge. Its tint, edge and accent
  are computed from the session's hue on the card itself.
- An idle card steps back by saturation, and its figure by opacity. Text
  opacity is never lowered.

**The figure** is one plain 32x32 person drawn as SVG paths. The body and the
cap take the session's hue, so a pinned colour still applies. Hair (5 colours),
skin (3 tones) and shape (plain, long hair, cap) each come from their own hash
of the name, so two sessions of a near hue still differ.

**Overlays** show state: a keyboard when working, a "!" when waiting, a "z"
after an hour asleep, and a small meter in `--alert` when the context is 85%
full or more. Waiting wins over working, working over full, full over asleep.
The working figure bobs, and stops with the animations switch.

## A reading it does not have

A meter, strip or badge says why it is blank in words. It never shows a zero
it did not measure.

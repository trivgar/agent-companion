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

## A reading it does not have

A meter, strip or badge says why it is blank in words. It never shows a zero
it did not measure.

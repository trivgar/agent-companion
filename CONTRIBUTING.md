# Contributing

Bug reports, ideas and pull requests are welcome. It is a small project, so an
answer can take a few days, and a change that does not fit may be turned down.

## Before you start

- For anything bigger than a small fix, open an issue first and say what you
  want to change and why.
- Read [CLAUDE.md](CLAUDE.md). It is the guide Claude Code reads in this repo,
  and its walls (mail never sends, writes need a paired device, the server
  stays on loopback, nothing personal) hold for every change.

## Sending a pull request

- Branch from `main` and keep the pull request to one change.
- Run the tests (README, "Tests") and add one for what you fixed or added.
- Nothing personal: no real names, addresses, keys, tailnet names or mail.
  `tests/test_leaks.py` checks for the common shapes.

## What happens to it

GitHub runs the test suite on your pull request; for a first-time contributor,
that run waits until the maintainer approves it. The maintainer then reads the
whole diff before any of it runs on their own machine, and lands it through
the merge queue, which tests and merges that exact commit. A later push to the
branch is read again before it runs.

## Security problems

Do not open a public issue for one. See [SECURITY.md](SECURITY.md).

# Security

agent-companion runs on your own PC, next to your Claude Code sessions, the
keys of the devices you pair, and your mail drafts, so a hole in it matters.

## Reporting a problem

Do not open a public issue. Report it privately on GitHub instead: on this
repository's **Security** tab, choose **Report a vulnerability**. Only the
maintainer sees it.

Say what you found, how to reproduce it, and what someone could do with it.
You will get an answer as soon as the maintainer can give one.

## What counts

- Someone other than the owner reading sessions, approving an ask, typing into
  a session, or pairing a device.
- A way through the walls in [CLAUDE.md](CLAUDE.md): mail that sends, a write
  without a paired device's key, the hook token approving an ask.
- Personal data, keys or tokens in the repository.

One limit is known and is not a hole in agent-companion: a session running in
bypass-permissions mode on your account can do anything you can, as the README
says.

## Supported versions

Fixes go to `main` and the next release. Older releases are not patched.

# Contributing

Thank you for helping! Bug reports, fixes and new features are welcome.

## Reporting bugs

Include the distribution, NetworkManager and Xray-core versions (`nm-vless check`) and
the relevant part of `journalctl -b -u NetworkManager | grep nm-vless`.
**Remove user ids, `vless://` links, subscription URLs and server addresses** before
posting logs. Security problems go to [SECURITY.md](SECURITY.md), not to public issues.

## Changes

1. Read [docs/development.md](docs/development.md) and run `make check`.
2. Keep changes focused; add tests for new behaviour.
3. Update `CHANGELOG.md` (*Unreleased*) and the documentation for user-visible changes.
4. Use clear commit messages: a short summary line, then what and why.

By contributing you agree that your work is licensed under GPL-2.0-or-later.

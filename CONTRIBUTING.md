# Contributing to Sentinel BigEye

Thank you for helping. BigEye is small on purpose: one page, a few add-ons, bridges that speak each camera's
own LAN protocol, and nothing that phones home.

## Good first contributions

- **Your camera.** Run the call-home monitor and the bridge against a camera that is not in the
  [tested list](README.md#cameras) and open an issue with what worked, what did not, and the model name.
  Never paste device UIDs, LAN tokens, account ids or pictures of your home.
- **Protocol notes.** Facts you measured yourself (packet layouts, command numbers) for `docs/protocols.md`.
- **Add-ons.** A module in `viewer/addons/` with `class Addon(ctx)` and `start()`.
- **Tests** for parts that need no camera (`tests/`).

## Ground rules

- Everything stays local. No cloud services, telemetry, analytics or auto-updates in the default setup.
- No vendor code, firmware, keys or recorded cloud answers in the repo. Write from your own observations.
- English for code, comments and docs. Small pull requests with a short "why".
- Run `pytest -q tests` (and `go test ./...` for a Go bridge) before you open a pull request.

## Licence of your contribution (please read)

BigEye is licensed under the **GNU AGPL-3.0**. By submitting a contribution (code, docs, graphics or data) you
agree that:

1. you wrote it yourself, or have the right to submit it, and it is not copied from code you may not share;
2. it is licensed to everyone under the AGPL-3.0, like the rest of BigEye; **and**
3. you also grant the BigEye maintainers (Sentinel Armory) a perpetual, worldwide, royalty-free, irrevocable
   licence to use, change and distribute your contribution under other licence terms as well, including
   permissive or commercial ones.

Point 3 lets the project offer BigEye under other terms later (for example to a company that cannot use the
AGPL) without having to track down every contributor. You keep the copyright in your contribution and can use
it however you like.

Please add a sign-off line to each commit (`git commit -s`), which confirms the above:

```
Signed-off-by: Your Name <you@example.com>
```

## Reporting a security problem

Do not open a public issue for a vulnerability in BigEye itself. Use GitHub's private
"Report a vulnerability" button on this repository instead.

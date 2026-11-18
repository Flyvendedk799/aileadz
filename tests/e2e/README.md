# Browser journey tests

`journey.test.cjs` walks the commercial journey in a real browser (Playwright, Chromium)
against the sandbox demo tenant on real MySQL: an employee requests a course, HR approves
and books it, the employee reports attendance, HR verifies it, and the completed course
shows up on the order, the CV and the development workspace. It also checks that a goal is
private until the employee shares it, that HR reaches a shared goal from its notification,
that a colleague cannot open someone else's order, and that no request answered with a
server error.

It is **not** part of `pytest`: the offline suite must never need MySQL or a browser.

## Run it

```bash
cd sandbox
./sandbox.sh reset && ./sandbox.sh up   # a fresh database (the journey orders a course once)
./sandbox.sh demo                        # schema + demo tenant (sandbox/demo_seed.py)
./sandbox.sh run &                       # http://127.0.0.1:5001
cd .. && BASE_URL=http://127.0.0.1:5001 node --test tests/e2e/journey.test.cjs
```

Needs Node 22 and the `playwright` package with its Chromium (`npx playwright install chromium`
on a machine that does not have it). No API key: the journey does not call the AI.

## Rules for new steps

- Steps run in order and share one order; each step logs in as the role that acts.
- Requests to other hosts are blocked on purpose. A page that only works with a CDN or a
  font host reachable is a finding, not something to allow here.
- Find things the way a person does (role, label, visible text). A `data-section` hook is fine
  for the order console, which already carries them.
- Start from a fresh seed. The journey is not written to be re-run on the same database.
- What it does not cover yet is listed under T0-5 in `docs/ROADMAP.md`.

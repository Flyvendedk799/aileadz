# Mind-Map v2 — navigation & immersion

> **Purpose.** `/mind-map` renders everything the AI has stored about a user as
> a 3D root → category → fact graph. v1 was a sphere you could click. v2 is a
> map you can *move through*. This file is the durable record of what changed,
> why, and where the invariants live.
>
> Code: `templates/fm/mind_map.html` (one file — DC template + DCLogic class).
> Runtime: `static/futurematch/assets/mind-map-support.js`.
> Data: `GET /api/profile/mindmap` (`api.py`). Tests: `tests/test_mind_map_v2.py`.

---

## 1. Why there is a v2

v1 shipped the *look* of a knowledge graph without the *use* of one. Concretely,
five things were missing or wrong:

| v1 | Consequence |
|---|---|
| No keyboard input at all | The page was mouse-only; the tree structure was invisible to anyone not orbiting it by hand |
| Search dimmed non-matches, and stopped there | You could see that something matched — you still had to find it in 3D yourself |
| Selecting a node changed nothing in the scene | Click → panel opens, camera moves, and the node looks exactly like every other node |
| Every leaf label drawn always, at world scale, `depthTest:false` | With a real profile loaded it was a wall of overlapping text |
| Fixed 195/74 layout radii | Clusters interpenetrated as soon as one category grew; the map read as noise |

Plus: the focused node landed *behind* the 340px inspector, an API 500 was
presented to the user as an empty profile, and every data reload disposed the
starfield and re-uploaded it.

v2 addresses all of the above. Everything below is implemented and covered by
either the structural test suite or the browser harness in §6.

---

## 2. Navigation

**The tree is the interface.** `_index()` builds `_byId` / `_kids` / `_parent` /
`_depth` from the edge list once per load; every navigation affordance reads
from that, so they can't disagree with each other or with the graph.

- **Keyboard traversal** (`_onKey`, bound to a `tabIndex="0"`,
  `role="application"` stage):

  | Key | Action |
  |---|---|
  | `↑` `↓` | Previous / next sibling (at the root: cycle its branches) |
  | `←` `→` | Up to the parent / down into the first child |
  | `Enter` | Isolate the selected branch (focus mode) |
  | `Esc` | Unwinds in order: focus mode → selection → search |
  | `/` | Jump to the search field |
  | `N` `P` | Next / previous search hit |
  | `F` · `Home` · `+` `−` · `Space` · `?` | Fit · select root · zoom · auto-rotate · shortcut overlay |

  Sibling and child moves walk *visible* nodes only, so navigation matches what
  the filters are actually showing. Space/Enter on a focused button belongs to
  that button, not the stage.

- **Search navigates.** `_applyFilter()` builds the match list (label, category,
  kind, detail, company, institution, issuer, source), keeps each match's
  ancestors visible so the path to a hit stays readable, and exposes a live
  `3 / 12` stepper. `Enter` / `Shift+Enter` / `N` / `P` fly to each hit in turn.

- **The inspector is a navigator**, not just a read-out: a clickable breadcrumb
  trail, a sibling stepper (`4 af 18`), and a child list for the root and every
  branch — the whole tree is reachable without hunting for a sphere in 3D.

- **Focus mode** (`_toggleFocus`) isolates one branch: everything outside it
  fades out and stops being a click target, and the camera frames the cluster.
  Entered by `Enter`, the panel button, or double-clicking a branch.

- **Deep links.** Selection writes `#n=<node id>` with `replaceState` (no history
  spam from arrow keys) and is restored on load and on `hashchange`.

- **Radar** (bottom-left): a top-down XZ projection with the camera position and
  view cone. Click it to jump to the nearest node. Hidden under 820px.

- **Panel-aware framing.** `_panelShift()` converts half the inspector width into
  world units at the target distance and offsets the orbit target along the
  camera's *right* vector, so a focused node lands in the middle of the visible
  area instead of behind the panel. (Getting the handedness wrong pushes it the
  other way — `right = up × dir`, not `dir × up`.)

---

## 3. Immersion

- **The scene reacts.** Hover and selection ease a scale/glow boost onto the
  node; the selection carries a rotating twin-torus halo, hover a camera-facing
  ring. Both are single re-targeted instances, not per-node objects.

- **The ancestor path is lit end-to-end.** Edges are one `LineSegments` with a
  **vertex-colour** attribute, so `_recolorEdges()` can brighten the root → …  →
  node chain (plus the selection's own children), dim everything else, and blank
  filtered-out edges — per edge, without extra draw calls. Flow particles read
  the same per-edge colour and a per-edge speed multiplier, so the lit path
  visibly runs faster.

- **Labels are placed, not just drawn.** `_layoutLabels()` (every 6th frame, plus
  immediately on hover/selection):
  1. sizes each label in **screen pixels** — sprite scale is recomputed from
     camera distance each frame, so a label is the same size up close and far
     away;
  2. ranks candidates by what earns the space — root, then selection/hover, then
     search hits, then the selection's relatives, then branches by child count,
     then leaves whose projected radius is ≥ 9px;
  3. places them greedily with **screen-space overlap rejection**, capped at 30.

  Net effect: the zoomed-out view is a clean map of branch names; leaning into a
  cluster reveals the few leaf labels that fit, instead of forty stacked cards.

- **Layout that scales with the data** (`_layout()`): each branch's leaf-sphere
  radius grows as `26 + 8.5·√n`, and the branch shell radius is then derived so
  the two largest clusters still clear each other
  (`bR = max(175, 0.62·maxLeafR·√k)` — min separation on a Fibonacci shell is
  ≈ `3.54R/√k`). Clusters are rotated to bloom *outward* from the core.

- **Entrance.** On first load the graph unfolds from the core, staggered by
  depth; nodes pop in as they arrive, labels only once they land.

- **Restraint where it matters.** `prefers-reduced-motion` disables auto-rotate,
  idle drift, pulsing and camera easing. `visibilitychange` cancels the render
  loop on a hidden tab (and restarts it cleanly, without a `getDelta` spike).

---

## 4. States the page now has

| State | Trigger | What the user sees |
|---|---|---|
| Loading | initial fetch | spinner overlay |
| Error | non-2xx from `/api/profile/mindmap` | retry card + explicit **Vis demo-data** |
| Empty | 2xx with zero leaves | "Din vidensbase er tom" + profiler / CV / add-memory CTAs |
| Demo | user opted in from the error card | graph + a **Demo-data** badge, so it's never mistaken for real data |

The API's 500 branch deliberately returns a root node so the client "degrades to
an empty-but-valid graph" — v2 checks `r.ok` **first**, so a DB failure reads as
a failure and not as "you have no profile". A *background* refresh that fails
(after a memory write) leaves the current graph alone.

Memory CRUD moved off `window.prompt`: an inline composer with the real category
vocabulary, and delete asks first.

---

## 5. Gotchas (read before editing)

1. **Never write a bracketed `x-dc` open tag above the real element** — not in a
   CSS comment, not in prose. `parseDcText()` regex-matches the *first* one in
   the raw page source and will hijack the template. Pinned by a test.
2. **`{{ … }}` bindings are resolved against `renderVals()` and nothing checks
   the pairing.** A renamed key renders as *nothing*, silently. Pinned by
   `test_every_template_binding_is_produced_by_render_vals`, which scans both
   halves of the file.
3. The DC expression resolver is deliberately small. Keep templates to the
   proven forms: `{{ ident }}`, `{{ ident.prop }}`, `sc-if value="{{ flag }}"`,
   `sc-for list="{{ arr }}" as="x"`. Compute everything else in `renderVals()`.
4. **Two GPU resource lifetimes.** `_chrome` (starfield, halo, hover ring) lives
   for the component; `_disposables` is per-graph and is emptied by
   `_disposeGraph()` on every reload. Putting chrome in the wrong list disposes
   the starfield on the first data refresh.
5. The whole page is Jinja-`{% raw %}`-fenced. Any new `{{ }}` outside that fence
   is a Jinja expression, not a DC binding.

---

## 6. How this was verified

`tests/test_mind_map_v2.py` (jinja2 only — no Flask, no DB) covers the
structural invariants: Jinja renders, the `x-dc` ordering gotcha, binding
parity, the keyboard surface, the v2 feature hooks, and that the composer's
category chips are a subset of `_MEMORY_CATEGORIES` in `app1/user_profile_db.py`.

Behaviour was verified in a real browser with a throwaway harness rather than by
eye, and the same technique is the fastest way to re-check a change:

1. Extract the `x-dc` block + `data-dc-script` block from the template into a
   standalone HTML page, next to local UMD copies of React 17, three r128,
   `OrbitControls` and `mind-map-support.js` (`npm i react@17 react-dom@17
   three@0.128.0` — the CDNs the page uses in production are blocked in CI-ish
   sandboxes, npm is not).
2. Stub `window.fetch` for `/api/profile/mindmap` and `/api/profile/memories`
   with a payload shaped exactly like `get_mindmap_api` returns, plus
   `?mode=empty` / `?mode=error` variants.
3. Serve it (`python3 -m http.server`) and drive it with Playwright
   (`executablePath: /opt/pw-browsers/chromium-*/chrome-linux/chrome`), asserting
   on the aria-live text, the panel, `location.hash` and the console — and
   screenshotting each state.

That harness is what caught the inverted panel-shift, the AABB-diagonal framing
(the map sat in the middle third of the stage), labels sized by sprite height
instead of glyph height, and the disposal-lifetime bug. It is intentionally not
committed — it is 60 lines of glue plus `node_modules`, and it is cheaper to
regenerate than to maintain.

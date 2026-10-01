# Mind map: navigation, immersion and gotchas

`/mind-map` renders everything the AI has stored about a user as a 3D root, category, fact graph. This file records how it behaves, the invariants tests pin, and the traps to read before editing.

- Code: `templates/fm/mind_map.html` (one file: DC template plus `DCLogic` class).
- Runtime: `static/futurematch/assets/mind-map-support.js`.
- Data: `GET /api/profile/mindmap` (`get_mindmap_api` in `api.py`).
- Tests: `tests/test_mind_map_v2.py` (structural, jinja2 only, no Flask or DB).
- Open items: ROADMAP R-3 (page loads three.js r128 while the CV portal loads 0.160).

## 1. Navigation

**The tree is the interface.** `_index()` builds `_byId` / `_kids` / `_parent` / `_depth` from the edge list once per load; every navigation affordance reads from that, so they cannot disagree with each other or with the graph.

- **Keyboard** (`_onKey`, on a `tabIndex="0"`, `role="application"` stage):

  | Key | Action |
  |---|---|
  | `↑` `↓` | Previous / next sibling (at the root: cycle its branches) |
  | `←` `→` | Up to the parent / down into the first child |
  | `Enter` | Isolate the selected branch (focus mode) |
  | `Esc` | Unwinds in order: focus mode, selection, search |
  | `/` | Jump to the search field |
  | `N` `P` | Next / previous search hit |
  | `F` · `Home` · `+` `−` · `Space` · `?` | Fit · select root · zoom · auto-rotate · shortcut overlay |

  Sibling and child moves walk visible nodes only. Space/Enter on a focused button belongs to that button, not the stage.
- **Search navigates.** `_applyFilter()` matches label, category, kind, detail, company, institution, issuer and source, keeps each match's ancestors visible, and exposes a live `3 / 12` stepper (`Enter` / `Shift+Enter` / `N` / `P` fly to each hit).
- **Inspector as navigator:** breadcrumb trail, sibling stepper, child list for the root and every branch.
- **Focus mode** (`_toggleFocus`): everything outside the branch fades and stops being a click target; the camera frames the cluster. Entered by `Enter`, the panel button, or double-clicking a branch.
- **Deep links:** selection writes `#n=<node id>` with `replaceState` and is restored on load and on `hashchange`.
- **Radar** (bottom-left, hidden under 820 px): top-down projection with camera and view cone; click to jump to the nearest node.
- **Panel-aware framing:** `_panelShift()` offsets the orbit target along the camera's right vector so a focused node lands in the visible area, not behind the 340 px inspector. Handedness matters: `right = up × dir`, not `dir × up`.

## 2. Immersion

- Hover and selection ease a scale/glow boost onto the node; a rotating twin-torus halo (selection) and a camera-facing ring (hover) are single re-targeted instances.
- Edges are one `LineSegments` with a vertex-colour attribute, so `_recolorEdges()` lights the root to node path per edge without extra draw calls; flow particles read the same per-edge colour and speed.
- **Labels are placed, not just drawn.** `_layoutLabels()` runs every 6th frame (and on hover/selection): sizes each label in screen pixels, ranks candidates (root, selection/hover, search hits, selection relatives, branches by child count, leaves with projected radius of at least 9 px) and places them greedily with screen-space overlap rejection, capped at 30.
- **Layout scales with the data** (`_layout()`): leaf-sphere radius is `26 + 8.5·√n` per branch; branch shell radius is `max(175, 0.62·maxLeafR·√k)` so the two largest clusters still clear each other.
- On first load the graph unfolds from the core, staggered by depth. `prefers-reduced-motion` disables auto-rotate, drift, pulsing and camera easing; `visibilitychange` cancels the render loop on a hidden tab and restarts it cleanly.

## 3. States

| State | Trigger | What the user sees |
|---|---|---|
| Loading | initial fetch | spinner overlay |
| Error | non-2xx from `/api/profile/mindmap` | retry card plus explicit **Vis demo-data** |
| Empty | 2xx with zero leaves | "Din vidensbase er tom" with profiler / CV / add-memory CTAs |
| Demo | user opted in from the error card | graph plus a **Demo-data** badge, never mistaken for real data |

The API's 500 branch deliberately still includes a usable root node alongside the 500 status (an empty but valid graph), so the client checks `r.ok` first and a DB failure reads as a failure, not "no profile". A background refresh that fails (after a memory write) leaves the current graph alone.

Adding and editing a memory share one inline composer with the real category vocabulary (a test pins it to `_MEMORY_CATEGORIES` in `app1/user_profile_db.py`): `addEditId` switches it to "Ret hukommelse", prefilled, and the save PUTs `{id,label,detail,category}` (the graph's memory meta carries `category` for this). Delete asks first.

**Ask the AI.** Every node but the root and the conversation digest has "Spørg AI om dette" (branches: "Uddyb med AI"). `_focusFor` maps it to a handoff focus: the node id for a stored row or memory, `section:<key>` for a branch or a summary leaf (`_BRANCH_SECTION`), and `_handoffUrl` opens `/ai-profiler?from=mind_map&focus=...`; the gap CTA opens the advisor the same way. The server resolves the focus against the user's own data (`app1/surface_context.py`, ai-framework.md 1b). The bottom ring and the root stats show the depth-aware `weighted_pct` as "profilstyrke". Structured profile facts carry stable entity ids and correction metadata; the inspector links to the canonical profile editor and removes supported facts through the user-scoped REST endpoints after confirmation. The graph also includes portfolio links, completed courses and saved learning paths.

## 4. Gotchas (read before editing)

1. **Never write a bracketed `x-dc` open tag above the real element**, not in a CSS comment, not in prose. `parseDcText()` regex-matches the first one in the raw page source and hijacks the template. Pinned by a test.
2. **`{{ … }}` bindings resolve against `renderVals()` and nothing checks the pairing.** A renamed key renders as nothing, silently. Pinned by `test_every_template_binding_is_produced_by_render_vals`.
3. The DC expression resolver is deliberately small. Stay with `{{ ident }}`, `{{ ident.prop }}`, `sc-if value="{{ flag }}"`, `sc-for list="{{ arr }}" as="x"`; compute everything else in `renderVals()`.
4. **Two GPU resource lifetimes.** `_chrome` (starfield, halo, hover ring) lives for the component; `_disposables` is per graph and is emptied by `_disposeGraph()` on every reload. Chrome in the wrong list gets disposed on the first data refresh.
5. The whole page is Jinja-`{% raw %}`-fenced. Any `{{ }}` outside the fence is a Jinja expression, not a DC binding.

## 5. Verifying a change

Structural invariants (Jinja renders, the `x-dc` ordering rule, binding parity, keyboard surface, feature hooks, category subset, endpoints exist) run in `tests/test_mind_map_v2.py`. For behaviour, a throwaway browser harness is the fastest check (it is not committed; regenerate it):

1. Extract the `x-dc` block and the `data-dc-script` block into a standalone page next to local copies of React 17, three r128, `OrbitControls` and `mind-map-support.js` (npm works where the production CDNs are blocked: `react@17 react-dom@17 three@0.128.0`).
2. Stub `window.fetch` for `/api/profile/mindmap` and `/api/profile/memories` with a payload shaped like `get_mindmap_api`'s, plus `?mode=empty` and `?mode=error` variants.
3. Serve it and drive it with Playwright, asserting on the aria-live text, the panel, `location.hash` and the console, and screenshot each state.

This technique caught the inverted panel shift, the framing bug where the map sat in the middle third of the stage, labels sized by sprite height instead of glyph height, and the disposal-lifetime bug.

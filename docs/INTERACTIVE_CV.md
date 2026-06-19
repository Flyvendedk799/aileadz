# Interactive CV — Full Implementation Plan

## What exists today
- `templates/fm/cv_upload.html` — 2-step server-rendered flow: upload → review checkboxes → apply
- `futurematch_ui.py` routes: `cv_upload` (GET), `cv_upload_parse` (POST → HTML), `cv_upload_apply` (POST → redirect)
- `cv_ingest.py` — `extract_text()` (PDF/TXT), `parse_profile_from_text()` (OpenAI extraction)
- `templates/fm/my_profile.html` — rich profile page; CV data displayed as skills/timeline/certs/languages cards

## Goal
Replace the flat upload form with a groundbreaking 3D interactive experience, add live AI parse feedback via SSE, and embed a CV preview widget on the profile page. Keep all existing routes as fallbacks; the new experience is a progressive layer on top.

---

## Phase 0 — Backend API additions

### 0.1 JSON parse endpoint
Add `POST /api/cv/parse` alongside the existing HTML-form route:
- Accepts `multipart/form-data` with `cv_file` or JSON `{ "cv_text": "..." }`
- Runs `extract_text()` + `parse_profile_from_text()`
- Returns JSON: `{ proposal: { summary, skills, experience, education, certifications, languages }, hint, char_count }`
- Auth: `@login_required`

### 0.2 SSE stream endpoint
Add `GET /api/cv/parse-stream` that accepts `?session_id=`:
- Client starts SSE connection first, then POSTs file
- Server emits progress events: `stage` (extracting → reading → analysing → done), `chunk` (partial AI output as it streams), `result` (final JSON proposal), `error`
- Requires `stream=True` on the OpenAI call — update `cv_ingest.py` to accept a `stream_callback`
- Session keyed by a UUID the frontend generates

### 0.3 Structured CV data endpoint
Add `GET /api/cv/summary` — returns the user's *applied* CV data (already saved profile data, formatted for display). Used by the profile page widget.

---

## Phase 1 — 3D Interactive Upload Page (new cv_upload.html)

Replace step 1 of `cv_upload.html`. The new frontend (single-file, see Claude Design prompt) takes over:

### Upload zone
- Full-screen 3D drag-and-drop landing zone (Three.js or WebGL canvas)
- File hover: particles converge, zone glows, depth increases
- Drop: file "shatters" into the scene, fragments reform as a document that floats in 3D space
- Paste text: keyboard graphic animates text flowing in

### Live AI parse stream
- On drop/submit: open SSE to `/api/cv/parse-stream`
- Scene transitions to "analysis mode" — 3D neural-net graph assembles in real time
- Each extracted category (skills, experience, etc.) materialises as a glowing node as the AI streams chunks
- Progress ring animates through stages

### Integration points
- `POST /api/cv/parse` — JSON endpoint (Phase 0.1)
- SSE `/api/cv/parse-stream` for live feedback (Phase 0.2)
- On success: transition directly to Phase 2 review in the same SPA — no page reload
- Fallback: existing `POST /cv-upload/parse` HTML form still works without JS

---

## Phase 2 — 3D Animated Review Step

Replace step 2 of `cv_upload.html` (the checkbox list):

### Spatial card explosion
- Proposal items fly in from the document (skills orbit as chips, experience as timeline cards in 3D space, education as stacked tiles)
- Each category group is a "layer" — user can rotate the scene to see different sections
- Tap/click a card: it expands in place with inline edit (name, level/period fields)
- Checkmark to accept, X to discard — dismissed cards fly out

### Accept & apply
- "Gem til min profil" button: accepted cards fly toward a profile icon, scene fades, normal page resumes
- Still POSTs to existing `POST /cv-upload/apply` (or a new JSON version)

---

## Phase 3 — Profile Page CV Widget (`my_profile.html`)

Add a compact but impressive CV section to the existing profile page:

### Placement
- After the hero section, before or alongside the completeness ring
- A new "CV" sub-section / tab within the existing Profil & CV layout

### Widget behaviour
- Fetches `GET /api/cv/summary` for current applied data
- Shows a mini interactive timeline (horizontal scroll, depth/parallax on hover)
- Skills rendered as a 3D bar chart or radial spider (canvas, not a heavy lib)
- "Opdater CV" → links to `/cv-upload` (the new experience)
- If no CV data yet: an animated placeholder that pulses "Tilføj dit CV →"

### Design system
- Stays within `--fm-*` tokens (light/dark auto)
- No extra CDN load on profile page — use CSS only for mini-widget; full Three.js only on cv_upload page

---

## Phase 4 — AI CV Coach (deferred)

- Chat-style assistant overlaid on the 3D review: "Du har ingen engelsk beskrivelse på dine job — vil du have mig til at foreslå en?"
- `POST /api/cv/improve` endpoint — takes a section + user's existing text, returns AI suggestions
- Inline accept/reject within the 3D scene

---

## File map

| File | Action |
|------|--------|
| `cv_ingest.py` | Add `stream_callback` param to `parse_profile_from_text` |
| `futurematch_ui.py` | Add `/api/cv/parse`, `/api/cv/parse-stream`, `/api/cv/summary` routes |
| `templates/fm/cv_upload.html` | Replace with new 3D SPA frontend (from Claude Design) |
| `templates/fm/my_profile.html` | Add Phase 3 CV widget section |
| `static/futurematch/assets/cv-upload.js` | New: 3D upload scene |
| `static/futurematch/assets/cv-review.js` | New: 3D review scene |

---

## Open questions
- SSE session keying: use a UUID cookie vs query param vs session storage?
- Should the review step be a full-page takeover or a modal over the upload scene?
- Profile widget: canvas-based mini-chart vs pure CSS animated bars?
- Phase 2 final POST: add a JSON `/api/cv/apply` or reuse the HTML form POST?
- Avatar/photo upload on the profile page — separate task or include in this scope?

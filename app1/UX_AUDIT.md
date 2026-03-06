# App1 chatbot UX audit (high-impact opportunities)

This audit identifies five high-impact improvements to the chatbot experience in `app1`, based on current frontend and backend behavior.

## 1) Add explicit conversation controls (Stop generating + Regenerate)

**Current state**
- During a request, `isSending` blocks any new input and the send button is disabled.
- There is no way to cancel a long or irrelevant response once streaming has started.

**Impact**
- Users can feel stuck while waiting.
- Faster recovery from misfires improves trust and perceived speed.

**Recommendation**
- Add a visible **Stop** action while streaming that cancels `fetch` via `AbortController`.
- Add **Regenerate answer** on assistant messages to quickly retry without retyping.

---

## 2) Preserve chat state across refresh/navigation

**Current state**
- The welcome screen is always rendered on page load.
- Chat messages are only in-memory DOM nodes and disappear on refresh.

**Impact**
- Users lose context if they refresh, switch tabs, or navigate away.
- Reduces confidence for longer decision journeys (course comparison, follow-up questions).

**Recommendation**
- Persist recent messages in `sessionStorage` or server-side history endpoint.
- Restore the UI from saved state and skip the welcome screen when history exists.

---

## 3) Improve error recovery UX (keep user message + contextual retry)

**Current state**
- On retry, the implementation removes the latest user message and bot row, then re-sends.
- Error copy is generic (“Noget gik galt. Prøv igen.”).

**Impact**
- Users may not understand what is being retried.
- Removing messages can feel like lost progress.

**Recommendation**
- Keep the original user message visible; only replace/append the failed assistant response.
- Add clearer error reasons when available (timeout/network/server) and contextual actions.

---

## 4) Upgrade accessibility and keyboard UX for starter prompts

**Current state**
- Starter prompt cards are clickable `<div>` elements with `onclick` handlers.
- They are not semantic buttons and lack keyboard interaction behavior.

**Impact**
- Keyboard and assistive technology users get a degraded first-run experience.
- Reduced inclusivity and discoverability of onboarding prompts.

**Recommendation**
- Convert welcome cards to `<button>` elements (or add button role/tabindex/keydown handling).
- Add visible focus styles and ARIA labels where needed.

---

## 5) Expand feedback from binary voting to actionable quality signals

**Current state**
- Feedback UI only supports thumbs up/down.
- Backend feedback payload stores rating + truncated answer snippet, but no reason category/comment.

**Impact**
- Hard to diagnose why answers are bad (irrelevant, too long, wrong language, missing details, etc.).
- Slower model/tool prompt iteration.

**Recommendation**
- Add optional lightweight reason chips after thumbs-down (e.g., “Irrelevant”, “Incorrect”, “Too vague”).
- Include optional free-text comment and latency metadata.

---

## Suggested implementation order

1. Stop/Regenerate controls (fastest perceived UX gain)
2. Error recovery improvements
3. Session persistence
4. Feedback reason taxonomy
5. Accessibility pass for starter cards and controls


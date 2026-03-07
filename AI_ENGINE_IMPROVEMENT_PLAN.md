# AI Engine Improvement Plan — AiLead

## Current State Summary

The AI engine is a 6-phase system (intent classification, state machine, memory, hybrid search, response guardrails, feedback) using GPT-4o for responses and GPT-4o-mini for classification/summarization. It has hybrid RAG (vector + BM25 + RRF), Danish synonym expansion, user profile tracking, and quality guardrails.

**This plan targets dramatically better conversations and product suggestions across all aspects.**

---

## PRIORITY 1: Conversation Quality (Biggest Impact)

### 1.1 — Replace Non-Streaming with True Streaming
**File:** `agent.py` lines 739-746
**Problem:** `stream=False` means the entire response is generated before anything is sent. The "streaming" is just chunking a finished string in 12-char pieces — fake streaming. This makes the chatbot feel slow and unresponsive.
**Fix:** Switch to `stream=True` with OpenAI's streaming API. Handle `tool_calls` accumulation during streaming. This gives word-by-word real-time output, dramatically improving perceived speed and engagement.

### 1.2 — Smarter System Prompt with Conversation Techniques
**File:** `agent.py` lines 68-123
**Problem:** The system prompt is instruction-heavy but lacks *conversation technique*. It tells the AI what NOT to do (visual rule) but doesn't teach it HOW to have a great advisory conversation.
**Fix:** Add:
- **Needs-anchoring technique:** "Always connect your recommendation to the user's stated goal. E.g., 'Since you want to lead a team, this PRINCE2 course gives you the framework to...'"
- **Decision-support framing:** "When showing results, highlight the ONE key differentiator per course that matters to THIS user's stated needs."
- **Progressive profiling:** "Naturally weave in questions about timeline, team size, current skill level — but only one per response and only when it would meaningfully change your recommendation."
- **Objection handling:** "If the user hesitates, ask what's holding them back rather than pushing more products."
- **Social proof:** When available, reference vendor reputation, popularity, or certification value.

### 1.3 — Context Window Optimization
**File:** `agent.py` lines 600-613
**Problem:** Memory pruning at 15 messages is aggressive. The summarization loses nuance (budget preferences, rejected courses, emotional cues). The system prompt + ephemeral messages + few-shot examples already consume ~2000+ tokens before the conversation even starts.
**Fix:**
- Increase threshold to 25 messages (GPT-4o handles 128K context)
- Use structured memory extraction instead of free-text summary: extract a JSON object with `{needs: [], rejected: [], preferences: {}, emotional_state: str, decision_stage: str}`
- Inject this structured memory as a compact system message instead of a prose summary
- Remove the shown-products message duplication (it repeats data already in tool results)

### 1.4 — Eliminate Redundant API Calls
**File:** `agent.py` lines 551-556, `tools.py` lines 138-168
**Problem:** Every single user message triggers 3+ OpenAI API calls BEFORE the main response:
1. `classify_intent()` — GPT-4o-mini call
2. `_extract_user_profile()` — GPT-4o-mini call (every 3rd message)
3. `_summarize_pruned_messages()` — GPT-4o-mini call (when >15 msgs)
4. Main response — GPT-4o call
5. `_check_response_quality()` correction — GPT-4o call (on violations)
6. `_generate_followup_suggestions()` — GPT-4o-mini call
7. `_generate_alternative_searches()` — GPT-4o-mini call (on 0 results)

That's up to **7 API calls per user message**. Each adds latency.
**Fix:**
- **Merge intent classification into the main GPT-4o call.** Use a structured output schema where the model returns `{intent, search_query, response_or_tool_call}` in one shot. GPT-4o is smart enough to classify intent AND act on it simultaneously.
- **Move follow-up suggestions to the main response.** Add to system prompt: "End every response with 2-3 suggestion chips as JSON in a `<suggestions>` tag." Parse them out server-side.
- **Move profile extraction to the main response.** The model already knows the user's profile from conversation — have it emit profile updates as part of its tool calls (it already does this with `update_user_profile`).

This reduces the typical flow from 4-7 API calls to **1-2 API calls**.

---

## PRIORITY 2: Search & Product Suggestions (Core Matching)

### 2.1 — Upgrade Embeddings Model
**File:** `rag.py` line 347
**Problem:** Using `text-embedding-3-small` (1536 dims). This is the cheapest/fastest model but has significantly lower quality than `text-embedding-3-large` (3072 dims), especially for Danish/mixed-language content.
**Fix:** Switch to `text-embedding-3-large` with `dimensions=1024` (compressed — same storage as current, much better quality). Requires re-running `build_index.py` to regenerate all product embeddings.

### 2.2 — Product Embedding Quality
**File:** `build_index.py`
**Problem:** Product embeddings are generated from `ai_summary` alone. This misses critical structured data: vendor reputation, price tier, location availability, tags, course dates, certification type.
**Fix:** Create a rich embedding text for each product:
```
"{title} | {vendor} | {product_type} | Pris: {price_tier} |
Lokationer: {locations} | Tags: {tags} | {ai_summary}"
```
This gives the vector search actual structured context to match against user queries like "billigt IT kursus i København" which currently relies entirely on BM25 for structured matching.

### 2.3 — Weighted Hybrid Search Tuning
**File:** `rag.py` lines 418-426
**Problem:** RRF treats vector and BM25 results equally (`k=60` for both). But for Danish queries, BM25 with synonym expansion is often more precise than vector similarity (embeddings are English-optimized). For vague queries, vectors are better.
**Fix:**
- Implement **weighted RRF** where the weight depends on query type:
  - Short specific queries ("PRINCE2 certificering") → BM25 weight 1.5x
  - Long vague queries ("noget med at blive bedre til at lede") → Vector weight 1.5x
  - Detection: if >60% of query tokens have direct BM25 hits, favor BM25
- Add a **diversity penalty**: if top 3 results are from the same vendor, demote the 3rd

### 2.4 — Better Relevance Filtering
**File:** `rag.py` lines 428-449
**Problem:** The dynamic `min_score` filtering is based on the top vector score, but this doesn't account for query difficulty. A niche query like "TOGAF certificering" might have a legitimately low top score (0.42) but still return the right result. Currently it would be filtered out.
**Fix:**
- Replace absolute score thresholds with **relative gap filtering**: if the #1 result's score is 2x+ the #4 result's score, only show top 1-2. If scores are close (within 15%), show all.
- Add a **"confidence signal"** to the response: when the top score is very high (>0.7), tell the AI "strong match found" so it can speak confidently. When low (<0.45), tell it "weak matches — ask user to clarify."

### 2.5 — Cross-Encoder Reranking (Major Quality Boost)
**File:** `rag.py` (new addition)
**Problem:** The current pipeline does retrieval (vector + BM25) and ranking in one step. But retrieval models optimize for recall, not precision. The top results often include tangentially related courses.
**Fix:** Add a **cross-encoder reranking step** after RRF fusion:
- Use a lightweight cross-encoder (e.g., GPT-4o-mini with a reranking prompt, or a local `ms-marco-MiniLM` model)
- Take top 10 RRF results → rerank by actual query-document relevance → return top 3
- Prompt approach: "Rate 1-10 how relevant this course is for someone searching '{query}': {title} - {summary}"
- This catches cases where keyword/vector matching succeeds but the course isn't actually what the user needs

### 2.6 — Contextual Search Enhancement
**File:** `tools.py` lines 280-302
**Problem:** `search_courses` uses only the rewritten query. It ignores rich context: what the user has already seen (to avoid duplicates), their budget, location, and format preferences.
**Fix:**
- Pass shown product handles to the search function and **exclude them from results** (prevent showing the same courses again)
- Auto-inject user preferences from the session profile as soft filters (location bonus, format bonus) even when the user doesn't explicitly state them in the current query
- When the user says "vis noget andet" / "show me more", automatically offset by the number of already-shown results

---

## PRIORITY 3: Conversation Intelligence

### 3.1 — Buying Signal Detection
**File:** `agent.py` (new addition to state machine)
**Problem:** The state machine has only 5 stages and no concept of buying readiness. It can't tell if a user is window-shopping vs. ready to purchase.
**Fix:** Add buying signal detection:
- **High intent signals:** "Hvor tilmelder jeg mig?", "Hvornår starter det?", "Er der pladser?", "Kan I sende en faktura?", price comparison of specific courses
- **Low intent signals:** "Bare undersøger", "Til næste år", comparing >5 courses without deciding
- When high-intent detected → prioritize showing enrollment links, upcoming dates, availability
- When low-intent detected → focus on education and building the case

### 3.2 — Proactive Needs Discovery
**File:** `agent.py` system prompt + state machine
**Problem:** The chatbot either asks questions OR searches. It doesn't do intelligent progressive discovery. A user says "jeg vil lære projektledelse" and the bot immediately searches, but doesn't ask "Er det til en specifik certificering som PRINCE2 eller PMP, eller mere generel projektledelse?" which would dramatically improve results.
**Fix:**
- Add a **first-search-then-refine** pattern: after the first search, if results are diverse (courses span multiple sub-topics), the AI should say "I found courses ranging from X to Y — which direction interests you more?"
- Implement **smart question selection**: maintain a priority list of questions that most affect search results: `[certification_type, experience_level, timeline, budget, format]`. Only ask if the answer would meaningfully change results.
- Add to system prompt: "After your first search, if results span very different sub-categories, briefly highlight the key dimensions the user should consider, with a concrete recommendation."

### 3.3 — Conversation Recovery & Repair
**File:** `agent.py` (new patterns)
**Problem:** When the AI misunderstands the user, there's no graceful recovery. The user has to start over or explicitly correct.
**Fix:**
- Detect confusion signals: "nej", "ikke det", "jeg mente", "det var ikke det jeg ledte efter"
- When detected, acknowledge the mistake, ask a clarifying question, and don't repeat the same search
- Track "negative feedback" from conversation (rejected results, corrections) and use as negative examples for the next search

### 3.4 — Multi-Person / Team Buying Support
**Problem:** No support for users buying courses for a team ("Vi er 5 projektledere der skal certificeres").
**Fix:**
- Detect team/group queries
- Show bulk pricing if available (variant data)
- Suggest date ranges that accommodate groups
- Mention if vendor offers in-house training options

---

## PRIORITY 4: Product Data & Knowledge

### 4.1 — Live Shopify Sync
**Problem:** Product data is a static JSON file (95MB). Prices, dates, and availability go stale immediately.
**Fix:**
- Add a daily sync job that pulls from Shopify's REST/GraphQL API
- Rebuild embeddings only for changed products (diff-based)
- Add an `updated_at` field so the AI can mention "price as of today"
- Remove past dates from variants (currently shows expired course dates)

### 4.2 — Richer Product Metadata
**Problem:** Products only have: title, vendor, price, tags, locations, dates, ai_summary. Missing: ratings, popularity, certification type, difficulty level, duration, prerequisites, target audience.
**Fix:**
- Extract structured metadata from `body_html` using GPT-4o-mini at index time:
  ```json
  {
    "duration_days": 3,
    "difficulty": "intermediate",
    "prerequisites": ["basic project management"],
    "target_audience": ["project managers", "team leads"],
    "certification": "PRINCE2 Foundation",
    "language": "dansk",
    "includes": ["exam voucher", "course material", "lunch"]
  }
  ```
- Store in augmented JSON and use in search/filtering
- Enable new filter dimensions: "3-day courses", "includes exam", "beginner-friendly"

### 4.3 — Vendor Intelligence
**Problem:** The AI has no knowledge about vendors (Readynez, Teknologisk Institut, etc.). It can't advise on vendor reputation, specialization, or quality.
**Fix:**
- Create a `vendor_profiles.json` with: specialization areas, typical price range, reputation notes, format strengths
- Inject relevant vendor context when comparing courses from different vendors
- Enable queries like "Hvem er bedst til ITIL kurser?" with data-backed answers

---

## PRIORITY 5: Technical Architecture

### 5.1 — Switch to Claude for Main Responses
**File:** `agent.py`
**Problem:** GPT-4o is good but has known weaknesses: verbose responses, tendency to ignore "keep it short" instructions, inconsistent Danish quality, and weaker tool-use compared to Claude.
**Fix:**
- Evaluate Claude Sonnet 4.6 or Opus 4.6 as the main response model:
  - Better instruction following (important for the visual rule)
  - More natural conversational tone
  - Stronger tool use with fewer hallucinated tool calls
  - Better at staying concise
- Keep GPT-4o-mini for embeddings and lightweight tasks
- A/B test both and compare: response quality, visual rule compliance, conversation naturalness, user ratings

### 5.2 — Parallel Tool Execution
**File:** `agent.py` lines 752-851
**Problem:** When the model calls multiple tools (e.g., `search_courses` + `get_user_profile`), they execute sequentially. Each tool call that involves an API call (embedding, DB query) adds latency.
**Fix:**
- Use `asyncio.gather()` or `concurrent.futures.ThreadPoolExecutor` to execute independent tool calls in parallel
- Typical savings: 200-500ms per multi-tool turn

### 5.3 — Response Caching Layer
**Problem:** Common queries ("projektledelse kurser", "ITIL certificering") generate identical search results every time, but still do full embedding + BM25 + RRF pipeline.
**Fix:**
- Add a search result cache keyed on `(normalized_query, active_filters)` with 15-minute TTL
- Cache at the `semantic_search_courses_detailed` level
- Invalidate on product data refresh

### 5.4 — Observability & A/B Testing
**Problem:** The admin log captures debug data but there's no way to:
- Compare two prompt versions
- Measure conversion rates
- See which searches produce the worst results
- Track latency per API call
**Fix:**
- Add `prompt_version` tag to every session
- Log latency for each API call (intent, main response, tools, suggestions)
- Add a dashboard showing: avg response time, search hit rate, quality violation rate, feedback ratio
- Enable A/B testing by randomly assigning `prompt_version` per session

---

## PRIORITY 6: Personalization

### 6.1 — Cross-Session Learning (Logged-in Users)
**Problem:** Each session starts fresh. A returning user who told the chatbot their role, preferences, and budget last week has to repeat everything.
**Fix:**
- On session start, load the user's MySQL profile and inject it as a first-class context message
- Include: past searches, past viewed courses, stated preferences, professional background
- Add a "Velkommen tilbage! Sidst kiggede du på PRINCE2 kurser — vil du fortsætte der, eller søger du noget nyt?" greeting

### 6.2 — Learning Path Recommendations
**Problem:** `recommend_for_profile` does a single semantic search based on goals + low skills. It doesn't build a coherent learning path.
**Fix:**
- Add a `suggest_learning_path` tool that:
  1. Analyzes the user's current skills and goals
  2. Identifies skill gaps in a logical order (foundation → intermediate → advanced)
  3. Maps each gap to 1-2 courses
  4. Returns a sequenced plan: "Start with X (foundation), then Y (builds on X), then Z (certification)"
- Use GPT-4o to reason about skill dependencies, not just keyword matching

### 6.3 — Anonymous User Persistence
**Problem:** Non-logged-in users lose everything when the session expires (1 hour). No continuity at all.
**Fix:**
- Use a browser fingerprint or localStorage token to maintain a lightweight anonymous profile
- Store: top 3 interest areas, budget range, location preference, last 5 viewed products
- On return visit, pre-populate the chat with: "Based on your earlier browsing, you were interested in [X]. Want to continue?"

---

## Implementation Order

| Phase | Items | Estimated Impact | Effort |
|-------|-------|-----------------|--------|
| **Week 1** | 1.1 (true streaming), 1.4 (reduce API calls), 2.6 (contextual search) | High | Medium |
| **Week 2** | 1.2 (better prompt), 2.1 (upgrade embeddings), 2.2 (richer embedding text) | High | Medium |
| **Week 3** | 2.3 (weighted RRF), 2.4 (better filtering), 3.2 (proactive discovery) | High | Medium |
| **Week 4** | 1.3 (context optimization), 3.1 (buying signals), 3.3 (conversation repair) | Medium | Medium |
| **Week 5** | 4.2 (richer metadata), 4.3 (vendor intelligence), 2.5 (cross-encoder rerank) | High | High |
| **Week 6** | 5.1 (evaluate Claude), 5.2 (parallel tools), 5.4 (observability) | Medium | Medium |
| **Week 7** | 4.1 (live Shopify sync), 6.1 (cross-session learning), 6.2 (learning paths) | High | High |
| **Week 8** | 6.3 (anon persistence), 3.4 (team buying), 5.3 (caching) | Medium | Medium |

---

## Key Metrics to Track

1. **Response latency** (target: <2s for first token, <5s total)
2. **Search hit rate** (% of searches returning relevant results — target: >85%)
3. **Visual rule compliance** (% of responses needing correction — target: <5%)
4. **Conversation depth** (avg messages per session — target: >6)
5. **Thumbs up ratio** (% positive feedback — target: >70%)
6. **API calls per message** (target: 1-2 avg, down from 4-7)
7. **Conversion rate** (% of sessions that click through to a course — target: >25%)

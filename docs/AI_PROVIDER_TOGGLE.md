# AI-udbyder: OpenAI ↔ Claude

Samtale-agenten kan køre på enten OpenAI eller Anthropic (Claude). Skiftet sker
fra `/admin/ai-settings` og kræver **ingen genstart** — næste forespørgsel efter
cachen udløber (60 s) bruger den nye udbyder.

## Hurtig start

1. Sæt `ANTHROPIC_API_KEY` i servermiljøet (aldrig i databasen).
2. Installér afhængigheden: `pip install -r requirements.txt` (`anthropic>=1,<2`).
3. Gå til **Admin → AI-udbyder**, vælg `Anthropic (Claude)`, gem.
4. Verificér på `/readyz` → `ai.ready: true`, eller i tabellen "Kørsler seneste
   24 timer" på samme side.

Tilbagerulning: vælg `OpenAI (GPT)` igen. Ét felt, ingen deploy.

## Hvad skiftet omfatter — og hvad det ikke gør

| Omfattet (følger toggle) | Ikke omfattet (altid OpenAI) |
|---|---|
| Værktøjsløkken for medarbejder-, HR- og leverandør-agenten | Embeddings + RAG-søgning (`app1/rag.py`) |
| Streaming af det endelige svar | Cross-encoder rerank |
| Værktøjsfrie completions (`run_direct_completion`) | CV-udtræk (`cv_ingest.py`) |
| Intent-routeren | Katalog-kategorisering, HR-indsigter, eval-dommeren |

**`OPENAI_API_KEY` er påkrævet i enhver konfiguration.** Anthropic har ingen
embeddings-API, og det leverede katalogindeks er bygget med
`text-embedding-3-small` (1024 dimensioner). De pinnede undersystemer bruger
`ai_provider.openai_fast_model()`, som ignorerer toggle'en.

## Tilstande

| Værdi | Betydning |
|---|---|
| `openai` | Standard. Uændret adfærd. |
| `anthropic` | Claude serverer samtale-agenten. Ved fejl falder forespørgslen automatisk tilbage til OpenAI (`runtime_path = anthropic-openai-fallback`). |
| `anthropic_shadow` | OpenAI serverer brugeren; en stikprøve (`AI_SHADOW_SAMPLE_RATE`) køres også gennem Claude i baggrunden og logges som `runtime = anthropic-shadow`. |

Skyggekørsler er **værktøjsfrie** med vilje: at køre værktøjsløkken igen ville
udføre skrivende værktøjer (ordrer, profilændringer, HR-writes) to gange. De
sammenligner derfor svarkvalitet, latens og pris — ikke værktøjsvalg.

## Modeller og pris

| Niveau | OpenAI | Claude | USD / 1M ind → ud |
|---|---|---|---|
| Hoved | `gpt-4o` | `claude-opus-5` | 2,50 → 10,00 vs. 5,00 → 25,00 |
| Hurtig | `gpt-4o-mini` | `claude-haiku-4-5` | 0,15 → 0,60 vs. 1,00 → 5,00 |

Bemærk det hurtige niveau: `AI_MODEL_ROUTING=balanced` sender de fleste ture
dertil, og Haiku er ~7× dyrere end `gpt-4o-mini`. Prompt-caching (se nedenfor)
trækker inputsiden ned igen. `claude-sonnet-5` er et billigere hovedvalg.
Priserne står i `ai_cost_model.PRICE_TABLE_USD_PER_1M` og skal opdateres der.

## Tekniske forskelle der er håndteret i adapteren

`ai_provider_anthropic.py` bærer detaljerne; de fire vigtigste:

1. **Ingen `temperature` / `top_p` / `top_k`** — fjernet på nuværende
   Claude-modeller (returnerer 400). Intentionen udtrykkes med
   `output_config.effort` (`low` på værktøjsture, `high` på hovedsvar).
2. **`max_tokens` har et gulv** (`ANTHROPIC_MIN_MAX_TOKENS`, standard 4096).
   Thinking-tokens tælles med i `max_tokens`, og adaptiv thinking er slået til
   som standard, så OpenAI-loftet på 320 ville blive brugt op før modellen nåede
   at udsende en `tool_use`-blok.
3. **Systemprompten flyttes til `system`-parameteren** med et
   `cache_control`-brudpunkt på den statiske blok. `consolidate_system_layers()`
   garanterer allerede at `messages[0]` er byte-stabil, så cache-præfikset
   (tools + system) holder på tværs af ture.
4. **Værktøjsresultater samles i én bruger-besked** som `tool_result`-blokke.
   Deles de op, holder modellen op med at kalde værktøjer parallelt.

Samtalehistorikken gemmes fortsat i OpenAI-format; konvertering sker først ved
API-grænsen. Derfor virker komprimering, token-budget, telemetri, genoptagelse
af samtaler og OpenAI-fallback uændret på tværs af udbydere.

## Kvalitetssammenligning

```bash
SANDBOX=1 AI_PROVIDER=openai    python3 ai_eval/run_eval.py --set-baseline
SANDBOX=1 AI_PROVIDER=anthropic python3 ai_eval/run_eval.py --gate
```

Dommeren bliver på OpenAI, så begge udbydere scores af den samme neutrale model.

## Observabilitet

* `/readyz` → `ai`-blokken: aktiv udbyder, modeller, om nøglerne er sat.
  Blokken er bevidst **ikke** cachet, fordi udbyderen kan skifte i drift.
* `/readyz` → `features.anthropic`: om SDK + nøgle overhovedet er til stede.
* `ai_agent_runs.runtime`: `chat` / `responses` / `anthropic` /
  `anthropic-shadow`; `runtime_path` skelner fallback- og guardrail-udfald.
* `/admin/ai-cost`: pris pr. model, nu også for `claude-*`.

## Indstillinger

Værdier gemmes i tabellen `ai_settings` (nøgle/værdi, oprettes automatisk) og
falder tilbage til miljøvariabler og derefter indbyggede standarder. Kun nøgler i
`ai_provider.MANAGED_KEYS` kan skrives fra admin-siden — API-nøgler er ikke
blandt dem og bliver i miljøet. Hver ændring skrives til `audit_log` med
`action_type = ai.settings.update`.

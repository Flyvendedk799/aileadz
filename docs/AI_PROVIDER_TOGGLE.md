# AI-udbyder: OpenAI ↔ Claude

Samtale-agenten kan køre på enten OpenAI eller Anthropic (Claude). Skiftet sker
fra `/admin/ai-settings` og kræver **ingen genstart** — næste forespørgsel efter
cachen udløber (60 s) bruger den nye udbyder.

## Hurtig start

1. Installér afhængigheden: `pip install -r requirements.txt` (`anthropic>=1,<2`).
2. Gå til **Admin → AI-udbyder**, indsæt `ANTHROPIC_API_KEY` under *API-nøgler*
   og gem. (Alternativt: sæt den som miljøvariabel — se nedenfor.)
3. Klik **Test anthropic** for at bekræfte at nøglen virker.
4. Vælg `Anthropic (Claude)` som udbyder og gem.
5. Verificér på `/readyz` → `ai.ready: true`, eller i tabellen "Kørsler seneste
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
`ai_provider.MANAGED_KEYS` kan skrives fra admin-siden. Hver ændring skrives til
`audit_log` med `action_type = ai.settings.update`.

## API-nøgler

`OPENAI_API_KEY` og `ANTHROPIC_API_KEY` kan sættes fra **Admin → AI-udbyder**.
De gemmes i en separat tabel, `ai_secrets`, adskilt fra almindelige indstillinger
netop så en nøgle aldrig kan havne i det snapshot admin-siden renderer.

**Opløsningsrækkefølge: database → miljøvariabel.** En nøgle sat i UI'en har
forrang; en nøgle sat i miljøet virker uændret, hvis der ingen række er i
databasen.

### Kryptering

Nøgler krypteres med Fernet, samme mønster som SSO-klienthemmeligheder i
`enterprise_sso`. Krypteringsnøglen findes sådan her:

1. `AI_SECRET_KEY` (miljø eller app-config) — **anbefalet**. Generér med
   `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. Ellers afledt af `SECRET_KEY` (svagere adskillelse — og roterer du
   `SECRET_KEY`, kan gemte nøgler ikke længere dekrypteres).

Er ingen af delene til stede, **afviser UI'en at gemme** i stedet for at skrive
nøglen ukrypteret. Det er den eneste forskel fra SSO-mønsteret, som stadig
tillader plaintext-fallback.

Kan en gemt række ikke dekrypteres (typisk fordi krypteringsnøglen er skiftet),
bruges den ikke — der falder opløsningen tilbage til miljøvariablen, og
admin-siden markerer rækken som ulæselig.

### Hvad siden aldrig viser

En gemt nøgle kan ikke læses tilbage. Siden viser kun om nøglen er sat, hvorfra
den kommer, de sidste fire tegn, og hvem der satte den hvornår. `audit_log`
registrerer nøglens navn og handlingen (`set` / `cleared`) — aldrig værdien. Et
tomt felt ved gem betyder "behold nuværende"; fjernelse kræver et eksplicit klik.

### Sikkerhedsmodel — vær opmærksom på

* **Kryptering beskytter database-dumps, replikaer og backups.** Den beskytter
  ikke mod en angriber der har både databasen og krypteringsnøglen (dvs.
  applikationsserveren).
* **Det er en reel rettighedsændring.** Før krævede det serveradgang at sætte en
  provider-nøgle; nu kan enhver konto med admin-rollen gøre det. Gennemgå hvem
  der har den rolle.
* Formularen beskyttes af `SESSION_COOKIE_SAMESITE='Lax'` (sat i `run.py`), som
  forhindrer cross-site POST i at medbringe sessionscookien. Appen har ikke
  CSRF-tokens nogen steder, så der er ikke tilføjet et her.
* Nøgler eksporteres til `os.environ` ved cache-opdatering, så ældre kaldesteder
  der læser `OPENAI_API_KEY` direkte (`app1`, `catalog_service`,
  `insights_engine`, `ai_eval`) også ser en UI-sat nøgle. Fjerner du en nøgle,
  slår det først helt igennem efter en genstart af processen.

### Test af forbindelsen

**Test openai** / **Test anthropic** kalder udbyderens model-liste-endpoint. Det
autentificerer uden at bruge tokens, gemmer intet og viser aldrig nøglen.

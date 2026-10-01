# AI-udbyder: OpenAI ↔ Claude

Samtale-agenten kan køre på enten OpenAI eller Anthropic (Claude). Skiftet sker
fra `/admin/ai-settings` og kræver **ingen genstart** — næste forespørgsel efter
cachen udløber (60 s) bruger den nye udbyder.

Kode: `ai_provider.py` (valg, modeller, `MANAGED_KEYS`), `ai_provider_anthropic.py`
(Claude-runtime + adapter), `ai_runtime.run_agent_with_fallback` (dispatch +
fallback), `ai_secrets.py` (API-nøgler), `admin_dashboard.ai_settings` (siden).
Resten af AI-arkitekturen: [ai-framework.md](ai-framework.md).

## Hurtig start

1. Installér afhængigheden: `pip install -r requirements.txt` (`anthropic>=1,<2`).
2. Gå til **Admin → AI-udbyder**, indsæt `ANTHROPIC_API_KEY` under *API-nøgler*
   og gem. (Alternativt: sæt den som miljøvariabel — se nedenfor.)
3. Klik **Test anthropic** for at bekræfte at nøglen virker.
4. Vælg `Anthropic (Claude)` som udbyder og gem.
5. Verificér på `/readyz` → `ai.ready: true` (kun for admin-session eller
   `X-Health-Token`; offentligt returneres kun `status`), eller i tabellen
   "Kørsler seneste 24 timer" på AI-udbyder-siden.

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
| `anthropic` | Claude serverer samtale-agenten. Ved fejl falder forespørgslen automatisk tilbage til OpenAI via Chat Completions (`runtime_path = anthropic-openai-fallback`; `anthropic-invalid-request-fallback` ved permanent 4xx, som også logges som ERROR). Har et skrivende værktøj allerede kørt, genafspilles løkken **ikke** — fejlen kastes videre (ville ellers udføre skrivningen to gange). |
| `anthropic_shadow` | OpenAI serverer brugeren; en stikprøve (`AI_SHADOW_SAMPLE_RATE`, standard 0,1; max 2 samtidige) køres også gennem Claude i baggrunden og logges som `runtime = anthropic-shadow`. |

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
Cache-læsninger afregnes til 10 % af inputprisen for `claude-*` (50 % for OpenAI;
`AI_CACHED_INPUT_DISCOUNT` overstyrer begge).

Modelstrenge og routing (`AI_MAIN_MODEL`, `AI_FAST_MODEL`, `ANTHROPIC_MAIN_MODEL`,
`ANTHROPIC_FAST_MODEL`, `AI_MODEL_ROUTING` = `quality|balanced|cost`) kan sættes
fra admin-siden. `AI_RUNTIME` (`responses` standard | `chat`) vælger kun
OpenAI-løkken.

## Tekniske forskelle der er håndteret i adapteren

`ai_provider_anthropic.py` bærer detaljerne; de fire vigtigste:

1. **Ingen `temperature` / `top_p` / `top_k`** — fjernet på nuværende
   Claude-modeller (returnerer 400). Intentionen udtrykkes med
   `output_config.effort` (`low` på værktøjsture og hurtig-niveauet, `high` på
   hovedsvar; `ANTHROPIC_EFFORT` overstyrer; Haiku-niveauet får slet ikke
   `effort`).
2. **`max_tokens` har et gulv — ét per turtype.** Thinking-tokens tælles med i
   `max_tokens`, og adaptiv thinking er slået til som standard, så alle
   OpenAI-lofter er for lave her.
   - *Værktøjsture* → `ANTHROPIC_MIN_MAX_TOKENS` (standard 4096). OpenAI-loftet
     på 320 ville blive brugt op før modellen nåede at udsende en
     `tool_use`-blok.
   - *Svarture* → `ANTHROPIC_ANSWER_MAX_TOKENS` (standard 16000). Ræsonnement
     over langt værktøjsoutput (kursussøgning, profilanalyse) bruger rutinemæssigt
     tusindvis af thinking-tokens før det første synlige token, så
     `AI_MAX_OUTPUT_TOKENS` afkorter svaret midt i en sætning.

   Et loft er ikke en udgift: kun genererede tokens faktureres, og svarlængden
   styres af prompten. Ture der alligevel rammer `max_tokens`, logges som
   `WARNING` og bliver genereret om — de serveres aldrig halve.
3. **Systemprompten flyttes til `system`-parameteren** med et
   `cache_control`-brudpunkt på den statiske blok (+ et andet efter
   knowledge-laget). `consolidate_system_layers()` garanterer at `messages[0]`
   er byte-stabil, så cache-præfikset (tools + system) holder på tværs af ture.
   Steering-laget sendes som afsluttende `role:"system"`-besked på modeller der
   understøtter det (Opus 5/4.8, Fable/Mythos) — se ai-framework.md §2b.
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
* `/readyz` → `features.anthropic`: om SDK'et kan importeres (nøglen vises i `ai`-blokken).
* `ai_agent_runs.runtime`: `chat` / `responses` / `anthropic` /
  `anthropic-shadow`; `runtime_path` skelner fallback- og guardrail-udfald
  (`*-fallback`, `*-over-budget`, `anthropic-refusal`, `anthropic-forced-final`).
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
* Formularen beskyttes af Flask-WTF CSRF (`csrf_protect.py`; token injiceres i
  alle POST-formularer) oven i `SESSION_COOKIE_SAMESITE='Lax'` (`run.py`).
* Nøgler eksporteres til `os.environ` ved cache-opdatering, så ældre kaldesteder
  der læser `OPENAI_API_KEY` direkte (`app1`, `catalog_service`,
  `insights_engine`, `ai_eval`) også ser en UI-sat nøgle. Fjerner du en nøgle,
  slår det først helt igennem efter en genstart af processen.

### Test af forbindelsen

**Test openai** / **Test anthropic** kalder udbyderens model-liste-endpoint. Det
autentificerer uden at bruge tokens, gemmer intet og viser aldrig nøglen.

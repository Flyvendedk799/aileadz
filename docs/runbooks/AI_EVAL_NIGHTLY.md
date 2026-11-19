# Nightly AI eval

The golden set (`ai_eval/golden_set.json`, see [../../ai_eval/README.md](../../ai_eval/README.md))
runs every night against the live model on the production server and is gated against the
committed baseline. The result shows up on Admin, Systemstatus under "AI-kvalitet".

## Why on the server and not in GitHub Actions

The eval needs two things the public repository does not have: an API key and the course
catalogue export (supplier data, gitignored). The server has both. ServerHoster CI has no
scheduled trigger, so the job is a systemd timer next to the backup.

## What runs

- `aileadz-ai-eval.timer` (03:00 UTC daily) starts `aileadz-ai-eval.service`, which runs
  `/usr/local/bin/aileadz-ai-eval.sh` as root.
- That wrapper reads `OPENAI_API_KEY` (and `ANTHROPIC_API_KEY` if set) from the running web
  service, points `CATALOG_SOURCE_FILE` at the deployed catalogue, and calls
  [`scripts/nightly_ai_eval.sh`](../../scripts/nightly_ai_eval.sh) from the deployed checkout
  with work dir `/root/aileadz-eval-nightly` and result dir `/root/.survhub/ai-eval`.
- The script clones `main`, starts a throwaway MySQL 8 container (`fm_ai_eval_db`, port
  3317 on localhost), seeds the sandbox, runs each provider that has a key with
  `--gate --threshold 10` against `ai_eval/baselines/<provider>.json`, and writes
  `latest-<provider>.json` plus a dated copy and the run log. Files older than 30 days go.
- The web service has `AI_EVAL_DIR=/root/.survhub/ai-eval`; `eval_status.py` reads it.
  The card is red when the gate failed or no run is newer than `AI_EVAL_MAX_AGE_HOURS`
  (default 36).

Cost: about 90 cases at roughly 4,000 tokens each, about 0.35 USD per night on the main model.

## Re-baselining

After an intentional quality change (prompt, model, tools), run the eval once and commit the
new baseline in a pull request:

```bash
python -m ai_eval.run_eval --provider openai --set-baseline --baseline ai_eval/baselines/openai.json
```

The gate threshold is 10 percentage points per metric: the set is small, and a single run
moves a few cases either way.

## Known limits

- Only providers with a key run. The server has no `ANTHROPIC_API_KEY`, so Claude is not
  evaluated until one is added to the web service.
- Vendor cases are skipped without `EVAL_VENDOR_EMAIL` / `EVAL_VENDOR_PASSWORD`.
- The sandbox tenant has no employees, so cases that need people data (capability
  scenarios, team reports) score the assistant's handling of an empty company.

## Run by hand

`sudo systemctl start aileadz-ai-eval.service`, then `journalctl -u aileadz-ai-eval -e`
and `ls /root/.survhub/ai-eval`.

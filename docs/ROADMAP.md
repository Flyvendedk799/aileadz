# Roadmap: open work only

Everything from the 2026-09 completion plan (security tiers S-1 to S-5, functionality tiers N-0 to N-8), the HR value plan (19 initiatives), the unified profile/CV plan and the interactive CV plan has shipped on `main`; `git log` is the history. This file lists only what a code check on `main` still shows as undone. Standing constraints live in [DECISIONS.md](DECISIONS.md). Owner-only steps (accounts, servers, browser checks) live in [USER_ACTIONS.md](USER_ACTIONS.md).

Sizes: **XS** under 1 h, **S** up to 1/2 day, **M** 1 to 3 days, **L** 1 to 2 weeks. Line numbers drift; search for the symbol.

## Identity

| ID | Item | Where | Size |
|---|---|---|---|
| R-1 | **SAML and LDAP/AD SSO** (deferred by decision, pick up only when an enterprise customer requires it). Needs: SAML signature verified against the stored certificate plus audience/destination/time-window checks (new dependency such as `python3-saml`; none in `requirements.txt` today), LDAP filter escaping, ACS/metadata URL fields in the config form (`generate_saml_request` raises `KeyError` on `acs_url`), and a settings-hub UI option (it offers OIDC only). | `enterprise_sso/__init__.py`: `SSO_DISABLED_MESSAGE`, `_has_signature` (presence-only check, forgeable), LDAP filter, `generate_saml_request`; `settings_hub.py` | L |

## Not on the roadmap, on purpose

- Payment providers, card or bank flows, invoice PDFs: billing is tracked, not processed (see DECISIONS.md).
- Self-serve upgrade or trial paywall copy (sales-led).
- Confirm cards in the embeddable widget: visitors are anonymous and write tools require a logged-in user (`auth_required` in `ai_tool_registry.py`).
- Loading skeletons: pages are server-rendered.

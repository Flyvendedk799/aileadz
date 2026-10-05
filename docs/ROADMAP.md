# Roadmap: open work only

The older completion plans are in git history. The implemented launch journeys and their operational limits are documented in [runbooks/LAUNCH_WORKFLOWS.md](runbooks/LAUNCH_WORKFLOWS.md). A feature existing in code does not prove a deployed customer can complete it: run that acceptance checklist with a sandbox customer, actual SMTP recipients and a supplier before commercial launch. Standing product constraints live in [DECISIONS.md](DECISIONS.md); owner-only operational work lives in [USER_ACTIONS.md](USER_ACTIONS.md).

## Commercial launch gate

- Validate the deployed employee → HR → supplier → attendance → outcome journey.
- Agree the actual customer offer, pilot success criteria, support ownership and supplier handling. Record them in the customer handover screen.
- Confirm actual mail receipt and worker health; do not use an SMTP-success counter as inbox-delivery evidence.
- Future supplier-specific live inventory/reservation integrations and binary certificate storage remain optional extensions, not claims made by the current UI.

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

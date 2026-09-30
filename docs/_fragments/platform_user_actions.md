# Platform fork – user actions

1. **Database**: run the normal schema sync on deploy (`schema_registry` / `enterprise_tables` create `account_tokens`, `webhook_deliveries`, `account_requests` and add `users.status`). No manual SQL.
2. **Email**: make sure the branded email sender is configured in production (same SMTP settings already used for order mails). The new `vendor_submission_result` template is sent through it.
3. **SSO (optional, per customer)**: for each customer using SSO, register this redirect URI with their identity provider (shown on the SSO tab): `https://<your-domain>/sso/callback/<company-slug>/oauth2`, and set the company's email domain under Virksomhed so `/sso/discover` can find it.
4. **Existing API keys**: keys created before Part A remain valid; new keys are shown once and cannot be recovered. Customers who lost a key must create a new one.
5. **Webhook consumers**: announce the new event names `employee.added`, `employee.updated`, `budget.overrun` to customers who want them; existing subscriptions are unchanged.

Otherwise: none.

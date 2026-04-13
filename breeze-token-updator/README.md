# breeze-token-updator

Token operations repository for ICICI Breeze session refresh.

This repo is intentionally separate from Titan and handles only:
- Daily Breeze token validation
- Expiry alert notification
- Secure token update endpoint integration (Supabase Edge Function or your own API)

## Daily user interaction

1. Scheduled workflow checks Breeze token health.
2. If expired, you receive a mobile alert with Breeze login URL.
3. You complete login + 2FA on mobile.
4. Redirect/callback endpoint stores `API_Session` in Supabase `session_config`.
5. Titan picks up the new token in its next run (or can be triggered immediately).

## Required GitHub secrets

- `BREEZE_API_KEY`
- `BREEZE_SECRET`
- `SUPABASE_URL`
- `SUPABASE_KEY`
- `TOKEN_UPDATE_URL` (endpoint that accepts the new token)
- `ALERT_WEBHOOK_URL` (optional; Slack/WhatsApp bridge/custom notifier)
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO` (optional email alerts)

## Local run

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -r requirements.txt
python src/validate_token.py
```

# 🌑 VoidKage — Production Telegram + Web Vault

VoidKage is a Telegram-linked private document vault. The same account can authenticate on the web and inside the Telegram bot using one password.

**Web:** https://voidkage.onrender.com/
**Telegram:** https://t.me/VoidKageBot
**GitHub:** https://github.com/PythosX

## Production authentication

1. User opens **@VoidKageBot** and sends `/start`.
2. New users create a password and confirm it.
3. Existing accounts without a password are prompted to create one.
4. Existing accounts with the legacy `vault_pin_hash` can continue using that credential and should change it from the dashboard/Telegram password flow.
5. Website login accepts the Telegram username or permanent Telegram ID plus the same password.
6. Passwords are stored only as bcrypt hashes.
7. Failed login attempts are temporarily locked.
8. Telegram sessions expire automatically.
9. Changing the password increments `security_version`, invalidating existing web sessions.
10. **🚨 KILL ALL ACTIVITY** increments `security_version` and ends the current Telegram authentication state.

## Storage

For production, configure Supabase Storage. When `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `SUPABASE_BUCKET` are present, VoidKage stores files in the configured Supabase bucket instead of Render's local filesystem.

This is important because Render web-service local storage should not be treated as permanent document storage.

## Required Render environment variables

```text
SECRET_KEY
DATABASE_URL
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
SUPABASE_BUCKET=voidkage-documents
TELEGRAM_BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET
WEB_URL=https://voidkage.onrender.com
MAX_UPLOAD_MB=20
TELEGRAM_SESSION_MINUTES=60
PASSWORD_SETUP_MINUTES=10
MAX_LOGIN_ATTEMPTS=5
LOCKOUT_MINUTES=15
SESSION_COOKIE_SECURE=true
```

## Telegram flow

```text
/start
  ↓
Password setup OR password login
  ↓
🌑 VOIDKAGE command center
  ├── 📂 My Documents
  ├── ➕ Add Document
  ├── 🌐 Web Vault
  ├── ⚙️ Account
  ├── 🔑 Change Password
  ├── 🚪 Logout Telegram
  └── 🚨 Kill All Activity
```

A document sent to Telegram is stored under the authenticated user's account. A Telegram caption becomes its dedicated display name.

## Security notes

- Never commit `.env` or service-role keys.
- Never place Telegram or Supabase secrets in frontend JavaScript.
- The Supabase service-role key is server-side only.
- Passwords are never logged or returned by the API.
- Telegram password entry is convenient but Telegram itself transports bot messages through Telegram infrastructure; for extremely sensitive credentials, a dedicated Telegram Mini App/WebApp password form is stronger.
- The webhook is protected with `X-Telegram-Bot-Api-Secret-Token`.

## Deploy

Push the repository to GitHub, connect it to Render, set the environment variables, and deploy. The application automatically configures the Telegram webhook during startup when the bot token and webhook secret are present.

Health check:

`/health`

Telegram diagnostic:

`/admin/telegram-status`

## Local development

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
copy .env.example .env
python app.py
```

For local HTTP, set:

```text
SESSION_COOKIE_SECURE=false
```

## Credits

Made with ♥ by **PythosX**.

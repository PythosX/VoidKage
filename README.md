# 🌌 VoidKage

VoidKage is a futuristic Telegram-linked private document vault.

## Current included functionality

- Flask web dashboard
- User records linked by Telegram ID
- Local/demo login for development
- Secure bcrypt Vault PIN helper
- Upload, download, rename and delete documents
- User-scoped authorization
- "Kill All Activity" security-version mechanism
- Render configuration
- Futuristic responsive UI

## Important production note

The included login page has a **demo login** so the project can run immediately. Before production, replace `/demo-login` with Telegram Login Widget or Telegram Mini App authentication and add the Telegram bot webhook handlers.

Do not commit `.env` or secrets.

## Run locally

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python app.py
```

Open http://127.0.0.1:5000

## Render

Set the required environment variables in Render. For permanent documents, use persistent external/object storage; Render free web service disks are not permanent.

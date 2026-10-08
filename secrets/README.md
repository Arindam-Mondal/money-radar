# secrets/

File-based secrets live here. Everything in this folder except this README is git-ignored.

| File | What it is | Created by |
|---|---|---|
| `client_secret.json` | Google OAuth desktop client, downloaded from Google Cloud Console | you (P1.1) |
| `gmail_token.json` | Gmail OAuth refresh/access token, `gmail.readonly` scope; rewritten on refresh | consent script (P1.1) |

Never commit, paste or log these files. If one leaks, revoke it at https://myaccount.google.com/permissions and create a new one.

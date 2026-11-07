# Flipkart Price Tracker

Zero-cost, single-user price tracker.

- **Dashboard**: `index.html` on GitHub Pages
- **Checker**: `tracker.py`, run daily by GitHub Actions
- **Database**: Supabase (PostgreSQL)
- **Scraping**: BeautifulSoup first, Groq (LLM) as fallback
- **Alerts**: Gmail SMTP

```
.
├── index.html
├── tracker.py
├── requirements.txt
├── schema.sql
└── .github/workflows/check_prices.yml
```

## 1. Supabase

1. Create a free project at https://supabase.com.
2. Open **SQL Editor**, paste `schema.sql`, and run it.
3. Open **Project Settings > API** and note:
   - **Project URL**
   - **anon public key**: goes into `index.html`
   - **service_role key**: goes into the GitHub secret `SUPABASE_KEY` (never into `index.html`)

## 2. Dashboard config

In `index.html`, replace the two placeholders near the bottom:

```js
const SUPABASE_URL = "https://YOUR-PROJECT.supabase.co";
const SUPABASE_ANON_KEY = "YOUR-ANON-PUBLIC-KEY";
```

## 3. Gmail app password

1. Turn on 2-Step Verification on your Google account.
2. Go to https://myaccount.google.com/apppasswords and create an app password.
3. Use the 16-character code as `EMAIL_PASSWORD` (not your normal password).

## 4. Groq API key

1. Sign in at https://console.groq.com/keys and click **Create API Key**.
2. Copy the key (starts with `gsk_`; it is shown only once) and save it as the GitHub secret `GROQ_API_KEY`.

It is only called when BeautifulSoup cannot find a price. The default model is `llama-3.3-70b-versatile`; to change it, set a `GROQ_MODEL` env var (for example `llama-3.1-8b-instant` for a faster option). Groq's free tier has rate limits, which are plenty for a daily run.

To test locally:

```bash
pip install -r requirements.txt
export GROQ_API_KEY=gsk_...   # plus the other variables from the secrets table below
python tracker.py
```

## 5. GitHub repository secrets

Push this folder to a new GitHub repo, then go to **Settings > Secrets and variables > Actions > New repository secret** and add:

| Secret | Value |
|---|---|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase **service_role** key |
| `GROQ_API_KEY` | Groq API key from console.groq.com/keys |
| `EMAIL_USER` | Your Gmail address |
| `EMAIL_PASSWORD` | Gmail app password |

Alerts go to `EMAIL_USER`. To send them elsewhere, also pass an `EMAIL_TO` secret and add it to the workflow's `env:` block.

## 6. GitHub Pages

1. **Settings > Pages**.
2. Source: **Deploy from a branch**, branch `main`, folder `/ (root)`.
3. After a minute your dashboard is live at `https://<user>.github.io/<repo>/`.

## 7. Test it

1. Add a product on the dashboard with a target price **above** its current price.
2. Go to **Actions > Check Flipkart Prices > Run workflow**.
3. Check the logs. The row should get a title and latest price, you should receive an email, and the card should move to **Alerts and history** with a green tick.
4. Tap the tick to mark it **Resolved**.

## How it behaves

- Only `ACTIVE` rows trigger an email, once. After that the row becomes `ALERT_SENT` and keeps updating `latest_price` daily with no repeat emails.
- If an email fails, the row stays `ACTIVE` and retries on the next run.
- If no price can be read, the row is left unchanged and the next run tries again.

## Known limits

- **Flipkart may block GitHub's IPs.** If logs show 403 or 529 errors, scraping is being blocked. The Groq fallback cannot help because it only sees pages that loaded. Fixes: retry later, or route requests through a residential proxy or a scraping API.
- **Dashboard security.** The anon key is public by design. Anyone who finds your dashboard URL can read, add and resolve trackers. For private use, keep the repo and URL to yourself. For real protection, add Supabase Auth and tighten the RLS policies in `schema.sql` to `authenticated`.
- **GitHub pauses scheduled workflows** after 60 days without repo activity. Re-enable it in the Actions tab if that happens.
- Cron runs can start a few minutes late; this is normal.

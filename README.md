# India Startup Funding Digest

Automatically checks Indian startup news RSS feeds every 4-6 hours, uses
Claude to pull out every genuine funding-round announcement (none skipped,
even if the amount or investors are undisclosed), and emails you a clean
digest. Non-funding news (M&A, layoffs, launches, policy, etc.) is
intentionally excluded — this is a funding-only digest.

## How it works
1. `digest.py` fetches the 10 RSS feeds listed at the top of the file
2. Compares against `seen.json` to find only new articles since last run
3. Sends new articles to the Claude API, which extracts every funding deal
   (company, stage, amount, investors, description) and ignores everything
   that isn't a funding announcement
4. Emails you the result via Gmail
5. Saves what it's seen back to `seen.json` so you never get duplicates
6. GitHub Actions runs this whole thing automatically 3x/day

## A note on volume
On the very first run, `seen.json` starts empty, so everything currently in
each feed counts as "new" — expect a larger first digest, then normal-sized
ones after. If a single Claude API call ever returns a truncated/invalid
response because there's too much to process in one go, the fix is to split
`new_entries` into smaller batches inside `summarize_with_claude` — flag it
to me and I'll add batching if this happens in practice.

## One-time setup (about 10 minutes)

### 1. Create a Gmail "app password"
Regular Gmail passwords don't work for scripts — you need an app-specific one.
1. Go to https://myaccount.google.com/apppasswords
   (you may need 2-Step Verification turned on first: https://myaccount.google.com/signinoptions/two-step-verification)
2. Create a new app password, name it e.g. "funding-digest"
3. Copy the 16-character password it gives you — you'll paste it into GitHub in step 3

### 2. Create a GitHub repo
1. Go to https://github.com/new
2. Name it anything, e.g. `india-funding-digest`
3. Make it **Private** (recommended, since it'll reference your email)
4. Upload all files from this folder (`digest.py`, `requirements.txt`,
   `seen.json`, `.github/workflows/digest.yml`) — either via the GitHub web
   UI's "Add file → Upload files", or via git:
   ```
   cd india-funding-digest
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
   git push -u origin main
   ```

### 3. Add your secrets to GitHub
In your repo: Settings → Secrets and variables → Actions → New repository secret.
Add these four:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Claude API key from console.anthropic.com |
| `GMAIL_ADDRESS` | The Gmail address you'll send *from* |
| `GMAIL_APP_PASSWORD` | The 16-character app password from step 1 |
| `DIGEST_TO_EMAIL` | The email address you want to *receive* the digest at (can be the same as GMAIL_ADDRESS) |

### 4. Test it
Go to the "Actions" tab in your repo → click "India Funding Digest" workflow
→ "Run workflow" (this uses the `workflow_dispatch` trigger, so you don't have
to wait for the schedule). Check the logs to see what it found, and check your
inbox.

### 5. Let it run
Once the manual test works, it'll run automatically at 6:00, 12:00, and 18:00
UTC every day (~11:30am, 5:30pm, 11:30pm IST). No further action needed.

## Adjusting things later
- **Change frequency**: edit the `cron` line in `.github/workflows/digest.yml`
  (cron times are in UTC)
- **Add/remove sources**: edit the `FEEDS` list at the top of `digest.py`
- **First run behavior**: since `seen.json` starts empty, the very first run
  will treat everything currently in each feed as "new" — expect a bigger
  first digest, then normal-sized ones after

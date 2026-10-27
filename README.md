# Cross-poster

Read and post across Bluesky, Mastodon and (experimentally) X from one small, self-hosted app.
It is written in Python with [FastHTML](https://fastht.ml), runs on your laptop, and installs on
your phone as a web app with real push notifications.

- **Sign in with OAuth** to Bluesky (full atproto OAuth: PAR, PKCE, DPoP) and to Mastodon or any
  server that speaks the Mastodon API (GoToSocial, Pleroma, Akkoma, Pixelfed, ...). Several accounts
  per network are fine.
- **One timeline for everything.** Posts from every network share one look: images with alt text,
  videos, link cards, quote posts, mentions and hashtags that open inside the app.
- **Native feeds**: Bluesky Following, Discover and your pinned feeds; Mastodon Home, Local,
  Federated, Trending, lists and followed hashtags.
- **Custom feeds you write in Python** (see below), with a live preview and a "why am I seeing this"
  score breakdown on every post.
- **Cross-post** to any mix of accounts at once, with per-network character counts, optional
  per-network text, images with alt text, content warnings and automatic threading of long posts.
- **Notifications** page plus Web Push to your phone and desktop.
- **X (experimental)**: posting, and optionally reading, with a hard monthly spend cap.

## Run it on your computer

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m crossposter.app          # http://127.0.0.1:5001
```

Try the interface without signing in to anything: `DEMO=true python -m crossposter.app`.

On a laptop, Bluesky uses its "localhost" OAuth mode automatically (no public URL needed, shorter
sessions). Open the app at `http://127.0.0.1:5001`, not `localhost`, so the sign-in redirect matches.

## Run it on Railway (for your phone)

Push notifications need the server to be always on, so the phone setup runs on a small host.

1. Create a Railway service from this repo. It builds from the `Dockerfile`.
2. Add a volume mounted at `/data` (database, keys and your feed files live there).
3. Set variables: `BASE_URL` (the public https URL), `SECRET_KEY` (long random string),
   `APP_PASSWORD`, `CONTACT_EMAIL`.
4. Open the URL, sign in with the app password, connect your accounts under Settings.

**On your phone:** open the URL in Safari (iPhone) or Chrome (Android), add it to your home
screen, open it from the home screen, then Settings → Turn on notifications. iPhone needs iOS 16.4
or later for web app notifications.

## Desktop app (Windows and macOS)

Download the zip for your computer from the [desktop-latest release](https://github.com/AndreasThinks/cross-poster-/releases/tag/desktop-latest), unzip it and open Cross-poster. It runs the same app in its own window, entirely on your computer: your accounts, feeds and database live in your user data folder (`%APPDATA%\Cross-poster` on Windows, `~/Library/Application Support/Cross-poster` on macOS), not on any server.

The builds aren't signed yet, so the first time you open it macOS needs right-click → Open, and Windows needs "More info → Run anyway".

**Updates follow `main`.** Every push to `main` (the same thing that redeploys the web version) publishes a small bundle of the app code. The desktop app checks for it after opening, downloads it quietly, and uses it the next time you open the app. If an update fails to start, the app goes back to the previous version and never tries that one again. When an update needs new libraries, the app asks you to download the new version instead. How it works is in `crossposter_desktop/updater.py`.

Things that differ from the web version: Bluesky uses its local sign-in mode, so you'll be asked to sign in again more often; and notifications show in the app while it's open, but there are no system notifications yet.

On Linux, or to run from source: `pip install -r requirements-desktop.txt` then `python -m crossposter_desktop` (pywebview needs GTK or Qt on Linux; `--headless` serves it to your browser instead).

## Custom feeds

A custom feed is a Python file in `feeds/` (on Railway, `/data/feeds`; the starter feeds are copied
there on first boot). Edit them in the app under **Feeds**, or in your editor; changes apply on save.

```python
from crossposter.feeds import Feed, Post, Ctx

feed = Feed(
    name="Popular in my network",
    sources=["bluesky:following", "mastodon:home"],
    window_hours=24,
)

def keep(post: Post, ctx: Ctx) -> bool:          # optional filter
    return not ctx.is_mine(post)

def score(post: Post, ctx: Ctx) -> dict:         # a number, or named terms that are summed
    decay = 1 / (ctx.age_hours(post) + 2) ** 1.2
    return {
        "engagement": 10 * ctx.normalised_engagement(post) * decay,
        "friends reposted": 2 * len(ctx.follows_who_engaged(post)) * decay,
    }
```

- **Sources**: `bluesky:following`, `bluesky:discover`, `bluesky:feed:<at:// uri>`,
  `bluesky:list:<at:// uri>`, `mastodon:home`, `mastodon:local`, `mastodon:public`,
  `mastodon:trending`, `mastodon:list:<id>`, `mastodon:tag:<name>`, `x:home`. The Feeds page lists
  the exact names for your accounts.
- **Post** has `text` (rich segments) and `plain_text`, `author`, `created_at`, `media`, `card`,
  `quote`, `likes`, `reposts`, `replies`, `quotes`, `is_reply`, `reposted_by`, `network`, `lang`.
- **Ctx** helpers: `age_hours`, `normalised_engagement` (0 to 1, compared within the post's own
  network, because Mastodon only reports counts your server has seen), `follows_who_engaged`
  (people you follow who reposted it), `is_mine`, `is_muted`, `log1p`, and `ctx.posts`.
- Feeds rank posts from a local cache that a background job keeps fresh (every 5 minutes, last 48
  hours), so they are fast and don't hammer the APIs. Ranking stops after `FEED_TIMEOUT_SECONDS`.

Feed code runs on your server with full Python, which is the point. Only you can edit it, behind
your app password; don't load feed files from people you don't trust.

## X (experimental)

X's API is pay-per-use: $0.005 per post read, $0.015 per post created, $0.20 for a post with a link
([pricing](https://docs.x.com/x-api/getting-started/pricing), October 2026). To try it:

1. Create a project and app at developer.x.com, enable OAuth 2.0 (confidential client), set the
   callback to `<BASE_URL>/auth/x/callback`, and buy some credit.
2. Set `X_ENABLED=true`, `X_CLIENT_ID`, `X_CLIENT_SECRET` and optionally `X_MONTHLY_CAP_USD`
   (default 10).
3. Connect your account in Settings. Posting works straight away. Reading your timeline and mentions
   is a separate switch, off by default; Settings shows what you've spent this month.

## Settings reference

See [`.env.example`](.env.example). Everything is optional on a laptop.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The code is small: `crossposter/adapters/` has one class per network implementing
`NetworkAdapter`, `models.py` is the shared post model, `feeds.py` the custom feed engine,
`push.py` notifications, `app.py` the routes and `ui.py` the components.

Not done yet: Bluesky DMs, video upload, editing posts, polls.

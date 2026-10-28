# Broadsheet

Read and post across Bluesky, Mastodon and X from one small, self-hosted app.
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
- **Mentions that follow people across networks.** Type `@` in the composer to pick someone from the
  people you've linked, accounts you've seen, or each network's search. Link a person's Bluesky,
  Mastodon and X accounts (on their profile page, or under Settings → People) and a cross-post tags
  them by the right handle on each network. Mentions of people you haven't linked are posted as plain
  text elsewhere, so they never tag a stranger on X, or through [Bridgy Fed](https://fed.brid.gy)
  when the person is bridged. A preview under the composer shows what each network will get.
- **Notifications** page plus Web Push to your phone and desktop.
- **X**: posting, and optionally reading, with a hard monthly spend cap.

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

## Desktop and Android apps

Download from **https://andreasthinks.github.io/cross-poster-/**. The apps run the same app entirely on your device: your accounts, feeds and database live in your user data folder (`%APPDATA%\Cross-poster` on Windows, `~/Library/Application Support/Cross-poster` on macOS, the app's private storage on Android). They don't use the Railway server or any other server of ours.

The builds aren't signed with a paid certificate yet, so the first time you open it macOS needs right-click → Open, Windows needs "More info → Run anyway", and Android needs you to allow installs from your browser.

**Desktop updates follow `main`.** Every push to `main` publishes a small bundle of the app code to GitHub Pages (`.github/workflows/apps.yml`). The desktop app checks for it after opening, downloads it quietly, and uses it the next time you open the app. If an update fails to start, the app goes back to the previous version and never tries that one again. When an update needs new libraries, the app asks you to download the new version instead. How it works is in `crossposter_desktop/updater.py`.

**Android** (`android/`) is a small Kotlin shell around the same Python app, using [Chaquopy](https://chaquo.com/chaquopy/). A background job checks for notifications about every 15 minutes while the app is closed. Install new versions over the old one, or point [Obtainium](https://obtainium.imranr.dev/) at the download page. APKs are signed with a shared debug key committed in `android/` so updates install cleanly; set the `ANDROID_KEYSTORE_*` variables in CI to sign with a private key instead (users would need to reinstall once).

Things that differ from the web version: Bluesky uses its local sign-in mode, so you'll be asked to sign in again more often; Web Push is replaced by in-app notifications on desktop and Android notifications on phones.

On Linux, or to run from source: `pip install -r requirements-desktop.txt` then `python -m crossposter_desktop` (pywebview needs GTK or Qt on Linux; `--headless` serves it to your browser instead). To build Android: `cd android && ./gradlew assembleRelease` with the Android SDK and Python 3.13 installed.

## Custom feeds

A custom feed is a Python file in `feeds/` (on Railway, `/data/feeds`; the starter feeds are copied
there on first boot). Edit them in the app under **Feeds**, or in your editor; changes apply on save.
Starter feeds added in later versions show up under **Add more feeds** on the Feeds page, one tap to
install. The Feeds page also sets the order of the tabs on Home, hides feeds from Home, copies a
feed as the starting point for a new one, and removes feeds (the file is kept as `_removed_<name>.py`).

Starter feeds: **Popular in my network**, **Links worth reading**, **Quiet posters**, and
**Across networks** (links and topics turning up on Bluesky and Mastodon at the same time).

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
  (people you follow who reposted it), `is_mine`, `is_muted`, `log1p`, and `ctx.posts`. For
  cross-network feeds: `links` and `tags` (normalised), `networks_discussing` (every network where
  the post or its link shows up), `people_sharing_link`, `tag_networks`, and `seen_in` (which
  sources the post came from). The editor has a reference panel listing them all.
- Feeds rank posts from a local cache that a background job keeps fresh (every 5 minutes, last 48
  hours), so they are fast and don't hammer the APIs. Ranking stops after `FEED_TIMEOUT_SECONDS`.

Feed code runs on your server with full Python, which is the point. Only you can edit it, behind
your app password; don't load feed files from people you don't trust.

## Community feeds

[Broadsheet Feeds](https://broadsheet-feeds-production.up.railway.app) is a directory of feeds people have
shared. In the app, **Feeds › Community** lists them with their code, download counts and how they're
doing; **Get** shows what a feed reads and its code before adding it to Home. To share one of yours,
choose **Share** on its card: the app checks it for anything personal (one of your lists, a handle),
sends only the file, name, description and an optional credit line, and you follow the review under
**Feeds › Your submissions**. No account is needed; the app keeps a private receipt for each feed.

Shared feeds may only import `crossposter.feeds` and standard tools like `math`, `collections` and
`re`, and can't touch the network or files (`crossposter/feedcheck.py` checks this without running
the code). A maintainer still reads every feed before it's listed.

**Anonymous stats.** The first time you add a community feed, the app asks whether to share
anonymous counts, with neither answer picked. If you say yes, once a week it sends, for community
feeds only: whether each is on Home, a rough "installed for" band, visits, posts read, posts liked or
replied to, and its tab position. Never handles, accounts, posts or your own feeds, and no device ID.
Settings › Privacy shows the exact report and turns it off. The directory folds each report into
weekly totals and discards it, keeps no IP addresses, and shows a figure only once 10 apps report it.

The directory itself is `directory/` (FastHTML + SQLite), deployed as its own Railway service from
`directory/Dockerfile` with a volume at `/data`. `ADMIN_PASSWORD` opens the review queue at `/review`.
Run it locally with `HTTPS=0 ADMIN_PASSWORD=pw python -m directory.app` (port 5002) and point the app
at it with `DIRECTORY_URL=http://127.0.0.1:5002`.

## X

X's API is pay-per-use: $0.005 per post read, $0.015 per post created, $0.20 for a post with a link
([pricing](https://docs.x.com/x-api/getting-started/pricing), October 2026). One X app covers the
web, desktop and Android versions:

1. At [developer.x.com](https://developer.x.com), create a project and app and buy some credit.
2. In the app's *User authentication settings*, switch on OAuth 2.0 with **Read and write**
   permissions. Choose **Native App** (a public client: only the client ID is needed, so it is safe
   to type into the desktop and phone apps) or **Web App** (also gives a client secret).
3. Add a callback URL for every place you'll sign in from. Settings shows the exact one for each:
   - web: `<BASE_URL>/auth/x/callback`
   - desktop: `http://127.0.0.1:47615/auth/x/callback`
   - Android: `http://127.0.0.1:47616/auth/x/callback`
4. Give the app the client ID (and secret for a Web App): either as `X_CLIENT_ID` and
   `X_CLIENT_SECRET` on the server, or under Settings, X app credentials, where they're stored
   encrypted. `X_MONTHLY_CAP_USD` sets the spend cap (default 10).
5. Connect your account in Settings. Posting works straight away, and X is ticked in the composer
   unless you switch that off. Reading your timeline and mentions is a separate switch, off by
   default; Settings shows what you've spent this month. `X_ENABLED=false` hides X entirely.

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

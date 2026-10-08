# knewdle-data: the Knewdle NOW menu check

Finds restaurants whose **own website menu** lists ramen, even when "ramen" isn't in their name
(e.g. "Kairu Sushi and Noodle Bar"), and publishes them as a small file the app loads like Overture.

Runs free on GitHub: GitHub Actions does the work every Monday, GitHub Pages hosts the result.
No servers, no keys, no card.

## How it works

1. **Candidates** (`menucheck/candidates.py`): US places from Overture Maps' free open data filed as
   Japanese / sushi / Asian / noodle / izakaya-style restaurants that have a website and *don't*
   already say "ramen" (the app finds those by itself).
2. **Menu check** (`menucheck/crawl.py`): reads the restaurant's own homepage and up to 4 linked
   menu pages or PDF menus.
   - Always honours `robots.txt` and identifies itself as `KnewdleNOW-MenuCheck` with a link to `docs/index.html`.
   - Never reads delivery apps, review sites, ordering platforms or social media.
   - Each restaurant is read at most monthly (ramen places) or every 3–6 months (others).
3. **Verdict** (`menucheck/classify.py`):
   - **shop**: 5+ ramen dishes making up a real share of the menu.
   - **serves**: 2+ ramen dishes.
   - **none**: 0–1 mentions. "Ramen salad", "ramen burger", one-off specials and "Sacramento" don't count.
4. **Publish**: `docs/menu_ramen.json` (served by GitHub Pages). Only facts are kept: name, location, website,
   menu link, how many ramen dishes. Never menu text, prices or photos.

## One-time setup (about 10 minutes)

1. Create a free GitHub account if you don't have one, then a **new public repository** named `knewdle-data`.
   (Public keeps Actions and Pages free.)
2. Upload everything in this folder to it (drag and drop on github.com works; keep the folder structure,
   including `.github/workflows/menu-check.yml`).
3. Edit `config.json`: replace `SWATSrocks` with your GitHub username.
   Opt-out contact: info@swats.rocks (in `docs/index.html`).
4. **Settings → Pages**: Source "Deploy from a branch", branch `main`, folder `/docs`. Save.
   Your results will live at `https://SWATSrocks.github.io/knewdle-data/menu_ramen.json`.
5. **Settings → Actions → General → Workflow permissions**: choose "Read and write permissions". Save.
6. **Actions → Menu check → Run workflow** to start the first run now (later runs happen every Monday).
7. In the app project's `local.properties`, add:
   `MENU_DATA_URL=https://SWATSrocks.github.io/knewdle-data/menu_ramen.json`

The first few weekly runs work through the backlog (up to 25,000 websites each); after that each run is short.

## Opt-outs

Restaurants can block the check in their `robots.txt` (`User-agent: KnewdleNOW-MenuCheck` / `Disallow: /`),
or ask by email; add their domain to `optout.txt` and they're skipped from the next run.

## Testing locally

    pip install -r requirements.txt pytest
    python -m pytest tests
    python -m menucheck.run --candidates-limit 200 --max-sites 50 --dry-run

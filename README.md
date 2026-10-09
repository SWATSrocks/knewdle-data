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
   - **serves**: 2+ ramen dishes, or one ramen bowl offered with 2+ broth choices, or one genuine
     ramen bowl (a named broth like tonkotsu/shoyu/miso/shio plus 2+ classic toppings).
   - **none**: 0–1 mentions. "Ramen salad", "ramen burger", one-off specials and "Sacramento" don't count.
4. **Publish**: `docs/menu_ramen.json` (served by GitHub Pages). Only facts are kept: name, location, website,
   menu link, how many ramen dishes. Never menu text, prices or photos.

## More ways it finds ramen (`menucheck/discover.py`)

Runs at the start of each weekly job and publishes its finds in the same file, under `"more"`:

- **Names** (`names.py`): Overture places the app's own name rule misses: other ramen words
  (tsukemen, mazesoba, chuka soba, 中華そば, 麺屋…), ramen chains whose names don't say ramen, known ramen brands
  (from OpenStreetMap's open name-suggestion-index), websites with "ramen" in the address, and Japanese "noodle bars".
- **Foursquare** (`foursquare.py`): US places Foursquare's open data (FSQ OS Places, Apache 2.0) files as
  "Ramen Restaurant" or names like ramen, not closed, confirmed in the last 3 years. Needs the `HF_TOKEN`
  repository secret (free Hugging Face account approved for `foursquare/fsq-os-places`); skipped without it.
  Credit is required: see `docs/NOTICE-foursquare.txt` and the `credits` field in `menu_ramen.json`.
- **Chains** (`chains.py`, list in `chains.json`): each ramen chain's own Locations pages, read politely
  (robots.txt and Crawl-delay honoured). Edit `chains.json` to add or remove a chain.
- **New websites** (`ctlog.py`): ramen-named domains from public Certificate Transparency logs (crt.sh). Each new one's
  homepage/contact page is read once for a US address. Ones first seen after the first run are marked new (`"nw"`),
  and "coming soon" ones are marked (`"so"`).
- Addresses become map points with the free U.S. Census Bureau geocoder. Nothing already shown by the app is repeated.

Run only this part: **Actions → Menu check → Run workflow → steps: `more`**.

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

# Fantasy Football Power Rankings

A phone-friendly power rankings website for a Sleeper fantasy football league. It updates itself every Tuesday and is hosted free on GitHub Pages.

**What it shows:** weekly power rankings with a short, fun write-up for each team; team pages with charts; playoff odds; weekly awards; best waiver pickups and trade results; a sortable stats table; all-time records and head-to-head.

The repo ships with **sample data** from a made-up league, so you can see the site before you set it up.

---

## Setup (about 15 minutes, no coding)

### 1. Find your Sleeper league ID

Open your league at [sleeper.com](https://sleeper.com) in a web browser. The URL looks like this:

```
https://sleeper.com/leagues/1180123456789012345/matchup
```

The long number is your **league ID**.

### 2. Put this project on GitHub

1. Create a free account at [github.com](https://github.com) if you don't have one.
2. Click **+** (top right) → **New repository**. Give it a name like `power-rankings`, set it to **Public** (GitHub Pages is free for public repos), and click **Create repository**.
3. Upload this project's files. The easiest way is GitHub Desktop, or from a terminal in this folder:

   ```bash
   git remote add origin https://github.com/YOUR-USERNAME/power-rankings.git
   ```

   ```bash
   git add -A
   ```

   ```bash
   git commit -m "Initial commit"
   ```

   ```bash
   git push -u origin main
   ```

### 3. Set your league ID

On GitHub, open `config.yaml`, click the ✏️ pencil icon, replace `<YOUR_LEAGUE_ID>` with your league ID, and click **Commit changes**.

*(Alternative: Settings → Secrets and variables → Actions → **Variables** tab → New variable named `LEAGUE_ID`.)*

### 4. Turn on GitHub Pages

Repository **Settings** → **Pages** → under *Build and deployment*, set **Source** to **GitHub Actions**.

### 5. (Optional) AI-written blurbs

Without this step the write-ups come from built-in templates, which work fine. For funnier, Claude-written blurbs:

1. Create an API key at [console.anthropic.com](https://console.anthropic.com) (a full season costs a few cents to a dollar or so).
2. Repository **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
3. Name: `ANTHROPIC_API_KEY`, value: your key.

### 6. Run it for the first time

**Actions** tab → **Update power rankings** → **Run workflow**. After a couple of minutes your site is live at:

```
https://YOUR-USERNAME.github.io/power-rankings/
```

(It's also linked at Settings → Pages.) From then on it updates automatically **every Tuesday at 15:00 UTC**, and whenever you change the code or config.

### 7. Share it

Send the link to your league. On a phone, open it and choose **Share → Add to Home Screen** (iPhone) or **⋮ → Add to Home screen** (Android) to get an app icon.

---

## Customizing

Everything adjustable lives in `config.yaml`:

| Setting | What it does |
|---|---|
| `weights` | How much each stat counts in the power score (defaults: PF 30%, Max PF 25%, Win % 25%, Efficiency 15%, Recent form 5%) |
| `decay` | How fast older weeks fade from "recent form" (0.85 = last week counts 85% as much as this week) |
| `simulations` | Number of playoff simulations (default 10,000) |
| `history_seasons` | How many past seasons to include in all-time records |
| `blurbs.model` / `blurbs.effort` | Which Claude model writes the blurbs, and how much effort it spends |

Not sure about your weights? The **Full Stats** tab has a "Do the rankings work?" section showing how often each weighting correctly predicted the next week's winners.

## Running it on your own computer (optional)

Needs [Python 3.11+](https://www.python.org/downloads/).

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate
```

(On Windows: `.venv\Scripts\activate`)

```bash
pip install -r requirements.txt
```

```bash
python -m pipeline
```

```bash
python -m http.server 8000 --directory site
```

Then open http://localhost:8000. Use `python -m pipeline --demo` to regenerate the sample league instead.

## Troubleshooting

- **The workflow failed with "Set league_id in config.yaml"**: step 3 wasn't saved, or the ID has a typo.
- **The site shows "Sample data"**: the workflow hasn't run successfully yet. Check the Actions tab.
- **The site is a 404**: make sure Pages → Source is set to **GitHub Actions** (step 4) and the workflow finished.
- **Write-ups say "template write-ups" in the footer**: no `ANTHROPIC_API_KEY` secret, or the API call failed (the log says why). The site still builds.
- **This week is missing on Tuesday**: Sleeper sometimes hasn't rolled over to the new week yet. Run the workflow again from the Actions tab later that day.

# binus-ae

Automates the monthly supervisor work on [BINUS Activity Enrichment](https://activity-enrichment.apps.binus.ac.id)
and keeps a progress tracker page you can open on your phone.

| Role | What it does |
|---|---|
| Site Supervisor | Approves every pending (red-dotted) logbook month. |
| Faculty Supervisor | Approves pending logbook months (consultation date = now, feedback = `ok`), clicks **Approve** / **Extend** on every assignment, and approves a final report **only** when every logbook month and every assignment of that student is approved. |
| Tracker | A static HTML page: per period, one row per student, logbook + assignment status per month, final report status, and a "Needs you" list. |

It drives a real browser (WebKit via Playwright) and clicks the same buttons you would. It is unofficial: if the
site changes, it may stop working. **Always preview with `--dry-run` first.**

## Install

No Python or other tools needed — the installer gets everything (uv, Python, the browser), then asks a few setup
questions.

**macOS / Linux** — paste into Terminal:

```sh
curl -LsSf https://raw.githubusercontent.com/Luche/binus-ae/main/install.sh | sh
```

**Windows** — paste into PowerShell (not tested yet):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Luche/binus-ae/main/install.ps1 | iex"
```

Already have [uv](https://docs.astral.sh/uv/)? `uv tool install https://github.com/Luche/binus-ae/archive/refs/heads/main.zip && binus-ae setup`

Setup asks for your BINUS email and/or Site Supervisor username, stores passwords in the system keychain (never in
a file), picks where to save the tracker page (your BINUS OneDrive if it syncs on this computer, so it opens on your
phone), and on macOS offers a reminder on days 5–9 of each month at 09:00. Run `binus-ae setup` again any time to
change settings.

## Use

Open a new terminal window after installing, then:

```sh
binus-ae --dry-run            # first run: sign in (approve MFA if asked) and preview; submits nothing
binus-ae                      # do everything for the configured number of recent periods
binus-ae --report             # only refresh the tracker page
```

| Option | Meaning |
|---|---|
| `--dry-run` | Log what would be done; submit nothing. |
| `--report` | Refresh the tracker page only (read-only). |
| `--role fs` / `--role ss` | Only Faculty Supervisor / only Site Supervisor. |
| `--only logbook\|assignment\|final-report` | Only one section. |
| `--periods 2 3` | Pick periods: 1 = newest. Default: the newest *N* from setup. |
| `--limit N` | Handle at most N students per section (good for a first live run). |
| `--headless` | No browser window (only once you are signed in). |

The Microsoft sign-in is remembered between runs. If Microsoft asks for MFA, run without `--headless` and approve it.

## Your data

Everything stays on your computer, in `~/.binus-ae/` (move it with `BINUS_AE_HOME`):
`config.env` (settings, no passwords), `profile-fs/` and `profile-ss/` (browser sessions), `logs/` (daily logs —
they contain student names), `last_success`. Passwords are in the keychain under the service `binus-ae`.
The tracker page contains student names and IDs; keep it in a private folder.

## Update / uninstall

Update: run the install command again.

Uninstall:

```sh
binus-ae setup                # answer "n" to the reminder question to remove it (macOS)
uv tool uninstall binus-ae
rm -rf ~/.binus-ae            # settings, browser sessions, logs
```

Then delete the `binus-ae` entries in Keychain Access (macOS) or Credential Manager (Windows).

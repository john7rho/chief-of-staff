# Chief of Staff

An always-on personal dashboard for a Mac, **run by Claude**. Claude syncs your calendars and inbox every hour,
plans your sleep and alarm around tomorrow's first commitment (and flights), flags risks before you notice them,
and keeps a glanceable glass-style dashboard up to date. A tiny local Python server does the rest: rings the alarm,
sends nudges, and talks to your gadgets.

> Everything runs locally on `127.0.0.1`. Your data stays in gitignored files on your Mac.

## What it does
| | |
|---|---|
| 📅 **Calendar** | Merges your calendars into a day timeline, countdown to the next event, Today / Tomorrow view |
| ⏰ **Sleep & alarm** | Picks wake time from your first commitment (with travel + airport buffers), bedtime from your sleep target; escalating alarm with snooze |
| 🌊 **Proactive risks** | Cross-checks official weather alerts (NWS), travel, and double-bookings against in-person plans and suggests fixes |
| ☁️ **Weather** | Live temp, rain chances, what to wear; the background animates rain, sun, clouds, fog, or snow |
| ✉️ **Inbox** | Read-only skim of Gmail for things that need a reply |
| 🎵 **Spotify** | Now playing, search & play, pick the Spotify device and the Mac's sound output |
| 🛏️ **BedJet 3** | Bluetooth control; warms the bed before bedtime with strength based on the night's forecast |
| 📷 **Room check** | Webcam snapshot compared with a baseline photo, on request |
| 💬 **Texts** | iMessage/SMS from the Mac, **only after you approve each message** |
| 🖥️ **Second Mac** | Sleeps another Mac's display at bedtime over SSH |
| 🫧 **Room air** | SwitchBot Meter Pro CO2 read passively over Bluetooth: CO₂, temperature, humidity, stuffy-air alerts |
| 💪 **WHOOP** | Recovery, strain, HRV, sleep (direct OAuth) feed bedtime suggestions |
| 🥗 **Food** | Log meals by chat, photo, or dashboard; Claude estimates macros |
| 🗄️ **History** | Local SQLite DB with optional encrypted, private GitHub backups |

## Requirements
- macOS 14+ (tested on macOS 26), Python 3.10+, Xcode command-line tools (`swiftc`), Google Chrome
- [Claude](https://claude.ai/download) desktop app with the Google Calendar and Gmail connectors
- Optional: Spotify Premium, `switchaudio-osx` (`brew install switchaudio-osx`), BedJet 3, a USB webcam

## Setup
```bash
git clone <this repo> ~/ChiefOfStaff
cd ~/ChiefOfStaff && ./install.sh
```
1. Edit `config.json` (calendars, home city lat/lon, sleep preferences) and `profile.md` (private facts for Claude).
2. In the Claude desktop app, open a session in `~/ChiefOfStaff` and ask:
   *"Create an hourly scheduled task (6am–midnight) that runs the sync procedure in CLAUDE.md."*
   Click **Run now** once to approve the calendar, Gmail, and file permissions.
3. Optional integrations:
   - **Spotify:** create an app at developer.spotify.com (Web API, redirect URI `http://127.0.0.1:8787/spotify/callback`),
     put the Client ID in `config.json`, then click *Connect Spotify* on the dashboard.
   - **BedJet:** `bin/bedjet scan`, then set `bedjet_schedule.enabled: true`.
   - **Room check:** set `webcam_name`, then ask Claude to take a baseline photo.
   - **Second Mac:** add a `Host studio` entry with an SSH key to `~/.ssh/config`.

## Talking to it
In any Claude session in this folder: *"set my alarm for 6:30"*, *"add 'call the dentist' to my list"*,
*"play some jazz on the kitchen speaker"*, *"warm the bed to 90°"*, *"check my room"*, *"text Sam I'm running late"*.
`CLAUDE.md` is the operating manual Claude follows; edit it to change the rules.

## Database & encrypted backups
- History lives in a local SQLite DB (`data/cos.db`, schema in `data/schema.sql`): food log, sleep plans + alarm
  outcomes, daily briefs, weather, BedJet runs. No ORM or server needed; Claude queries it with `sqlite3`.
- Optional nightly encrypted backup to a **private** GitHub repo:
  ```bash
  security add-generic-password -a "$USER" -s chief-of-staff-backup -w "$(openssl rand -base64 32)"   # save this key in your password manager too
  gh repo create <you>/chief-of-staff-backup --private && git clone https://github.com/<you>/chief-of-staff-backup .backup-repo
  bin/backup     # install.sh schedules it nightly at 3:30 AM
  bin/restore    # decrypts to a staging folder; --apply to restore
  ```
  Backups are AES-256 encrypted before they leave the Mac; OAuth tokens and app secrets are never included.

## Privacy & security
- The server binds to `127.0.0.1` only, rejects other `Host`/`Origin` headers (DNS-rebinding and cross-site protection),
  requires a custom header for writes, serves only the dashboard files, and caps request sizes.
- Personal files (`config.json`, `profile.md`, `state.json`, `todos.json`, tokens, photos, logs) are gitignored and `chmod 600/700`.
- Weather requests send only city-level coordinates. Photos never leave the Mac unless you ask.
- Claude never sends messages, cancels, or reschedules anything without your explicit approval.

## Layout
```
server.py        local runtime: dashboard, alarm, nudges, Spotify proxy, weather, BedJet schedule
index.html       the dashboard (single file, no build step)
CLAUDE.md        Claude's operating manual (sync procedure, sleep rules, integrations)
bin/             helpers: bedjet, roomcheck, text, contact, studio (+ Swift sources for the helper apps)
install.sh       builds helpers, installs launchd agents
```

## License
MIT

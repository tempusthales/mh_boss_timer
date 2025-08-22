# Mostly Harmless AOC Boss Timer Bot

# Acknowledgements
* [MH] Pride - Promptgramming
* [MH] TT - GitHub, Server Hosting, other stuff.

<hr>

A **discord.py 2.3+ bot** that tracks per-channel boss respawn timers with full interactive dashboards, dropdowns, and modals.  
Each channel has its own independent timers, bosses, and dashboards.

---

## ✨ Features

- **Per-channel timers** — independent tracking of bosses in each channel.
- **One dropdown per boss** — options: `Killed`, `Edit Time`.
- **Edit Time modal** — manually set time in `HH:MM:SS` format.
- **Slash commands** — `/settime`, `/addboss`, `/removeboss`, `/kill`.
- **Add Boss button** — modal to add new bosses (also updates `bosses.json`).
- **Remove Boss button** — dropdown for channel-only removal.
- **Async JSON locks** — race-safe I/O handling.
- **Dashboard auto-refresh** — updates once per second with timers.
- **Custom logo support** — if `mh.png` exists, it’s used as the dashboard thumbnail.

---

## 📂 Files Used

- **`bot.py`** — the main bot code.
- **`bosses.json`** — master boss list (default respawn times).
- **`channel_data.json`** — per-channel boss lists and timers (dynamic).
- **`dashboards.json`** — stores dashboard message IDs by channel.
- **`tracking.json`** — optional per-user filters.
- **`mh.png`** — optional thumbnail image.

---

## ⚙️ Requirements

- Python 3.9+
- `discord.py >= 2.3`
- `python-dotenv`

Install dependencies:

```bash
pip install -U discord.py python-dotenv
```

## 🔑 Setup
1. Clone/download this repo and place all files in a folder.
2. Create a .env file in the same directory with your Discord bot token:
```env
DISCORD_TOKEN=your_token_here
```

3. Run the bot:
```bash
python mh_boss_timer.py
```

<hr>

🚀 Usage

* Use /timers_cmd in a channel to create a dashboard.
* The bot will post (and pin, if possible) a message with:
    * Boss timers
    * Dropdowns per boss
    * Buttons to add/remove bosses
* Interact via dropdowns or slash commands to manage timers.

## 📜 Slash Commands

```markdown
| Command                           | Example                                      | Description                                           |
|-----------------------------------|----------------------------------------------|-------------------------------------------------------|
| /timers_cmd                       | /timers_cmd                                  | Creates a Boss Respawn Dashboard in the current channel. |
| /settime [boss] [HH:MM:SS]        | /settime "Adolescent Dragon" 07:15:00        | Sets a boss’s timer manually for this channel.        |
| /addboss                          | /addboss                                     | Adds a new boss via modal (name + respawn time). Updates bosses.json. |
| /removeboss                       | /removeboss                                  | Removes a boss from the current channel only.         |
| /kill [boss]                      | /kill "Adolescent Dragon"                    | Marks a boss as killed and resets its timer to default. |
```
## 🖱️ Dashboard Buttons

```markdown
| Button       | Function                                                   |
|--------------|------------------------------------------------------------|
| Killed       | Resets the boss’s timer to its default respawn.            |
| Edit Time    | Opens a modal to set a new countdown (HH:MM:SS).           |
| Add Boss     | Adds a new boss (same as /addboss).                        |
| Remove Boss  | Removes a boss (same as /removeboss).                      |
```
<hr>

📚 Managing Bosses
1. Add Bosses in Discord (Recommended)

*Use /addboss or the Add Boss button.
* Fill in:

    * Boss Name
* **Default Respawn Time** (in seconds)

*On submit:

    * The boss is added to bosses.json.
    * It’s also added to the current channel immediately.

2. Edit bosses.json Manually (Advanced)

Open the file in a text editor. Example:
```json
[
  { "name": "Adolescent Dragon", "respawn": 28800 },
  { "name": "Ancient Golem", "respawn": 43200 },
  { "name": "Fire Serpent", "respawn": 34200 }
]
```

📸 Example Dashboard
Boss Timers
─────────────
**Adolescent Dragon** — Respawns in 7h 59m (`07:59:00`)
**Fire Serpent** — READY (`00:00:00`)

✅ Notes

* Each channel is independent — bosses and timers don’t overlap.
* Dashboards auto-refresh every second.
* Works best if dashboard messages remain pinned.



#Boss Timer Bot

# Acknowledgements
* Pride - Promptgramming
* Tempus Thales - Code Optimization and cleanup, GitHub, Server Setup.

<hr>

A discordbot that tracks boss respawn timers in separate threads, featuring interactive dashboards with dropdown menus and modals. Each thread has its own independent timers, boss lists, and customized dashboards, allowing multiple users to monitor and interact with the bot simultaneously.

---

## ✨ Features

- **Per-channel timers** — independent tracking of bosses in each channel.
- **One dropdown per boss** — options: `Killed`, `Edit Time`.
- **Edit Time modal** — manually set time in `HH:MM:SS` format.
- **Slash commands** — `/settime`, `/addboss`, `/removeboss`, `/kill`, `/startbot`, `/stopbot`.
- **Add Boss button** — modal to add new bosses (also updates `bosses.json`).
- **Remove Boss button** — dropdown for channel-only removal.
- **Async JSON locks** — race-safe I/O handling.
- **Dashboard auto-refresh** — updates once per second with timers.
- **Custom logo support** — if `mh.png` exists, it’s used as the dashboard thumbnail.

---

## 📂 Files Used

- **`boss_timer.py`** — the main bot code.
- **`bosses.json`** — master boss list (default respawn times). 
- **`egg-boss_timer.json`** — Pelican Egg for hosting the bot.
- **`requirements.txt`** — text file that lists all the packages and their versions needed for a project.
- **`.env`** — environment variable for storing DISCORD_TOKEN.
- **`mh.png`** — Logo thumbnail image.
- **`boss_timer_banner.png`** - banner for Discord Application.
- **`boss_timer_icon`** - icon for Discord Application.

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



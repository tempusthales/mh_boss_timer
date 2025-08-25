import discord
from discord import app_commands
from discord.ext import commands, tasks
import os, json, asyncio, tempfile
from datetime import datetime, timezone
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

# ----------------------------
# Logging Setup
# ----------------------------
def setup_logging():
    log_file = f"bot_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=5),  # 5MB per file, keep 5 backups
            logging.StreamHandler()  # Also log to console
        ]
    )
    logger = logging.getLogger("BossTimerBot")
    logger.info(f"Logging initialized to {log_file}")
    return logger

logger = setup_logging()

# ----------------------------
# Setup
# ----------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

BOSSES_FILE = "bosses.json"            # master defaults (global)
CHANNEL_DATA_FILE = "channel_data.json"  # per-channel bosses + timers
DASHBOARDS_FILE = "dashboards.json"    # {channel_id: message_id}

# ----------------------------
# File Permissions Setup
# ----------------------------
def set_file_permissions():
    json_files = [BOSSES_FILE, CHANNEL_DATA_FILE, DASHBOARDS_FILE]
    for file in json_files:
        try:
            # Set permissions to 644 (rw-r--r--)
            if os.path.exists(file):
                os.chmod(file, 0o644)
                logger.info(f"Set permissions to 644 for {file}")
            else:
                logger.info(f"File {file} does not exist yet, will be created with default permissions")
        except Exception as e:
            logger.error(f"Failed to set permissions for {file}: {e}")

# ----------------------------
# Async JSON I/O with locks
# ----------------------------
_locks = {}
def _get_lock(path: str) -> asyncio.Lock:
    if path not in _locks:
        _locks[path] = asyncio.Lock()
    return _locks[path]

async def load_json(path, default):
    async with _get_lock(path):
        if os.path.exists(path):
            logger.info(f"Loading JSON file: {path}")
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load JSON file {path}: {e}")
                return default
        logger.info(f"JSON file {path} not found, using default: {default}")
        return default

async def save_json(path, data):
    async with _get_lock(path):
        logger.info(f"Saving JSON file: {path}")
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, dir=os.path.dirname(path) or ".") as tmp:
                json.dump(data, tmp, indent=4)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp.name, path)
            # Ensure new file has 644 permissions
            os.chmod(path, 0o644)
            logger.info(f"Successfully saved JSON file: {path}")
        except Exception as e:
            logger.error(f"Failed to save JSON file {path}: {e}")

# Initial async load at startup
async def load_initial_data():
    global bosses_master, channel_data, dashboards
    logger.info("Loading initial data")
    bosses_master = await load_json(BOSSES_FILE, [])
    channel_data = await load_json(CHANNEL_DATA_FILE, {})
    dashboards = await load_json(DASHBOARDS_FILE, {})
    logger.info("Initial data loaded successfully")

# ----------------------------
# Bot
# ----------------------------
intents = discord.Intents.default()
intents.message_content = True  # Enable message content intent
bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------------------
# Helpers
# ----------------------------
def find_master_boss(name: str):
    return next((b for b in bosses_master if b["name"].lower() == name.lower()), None)

def fmt_hms(seconds: float) -> str:
    neg = seconds < 0
    seconds = abs(int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{'-' if neg else ''}{h:02}:{m:02}:{s:02}"

def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def ensure_channel_record(cid: str):
    if cid not in channel_data:
        channel_data[cid] = {"bosses": [], "timers": {}}
        logger.info(f"Created new channel record for channel ID: {cid}")
    if "bosses" not in channel_data[cid]:
        channel_data[cid]["bosses"] = []
    if "timers" not in channel_data[cid]:
        channel_data[cid]["timers"] = {}

def get_channel_bosses(cid: str):
    ensure_channel_record(cid)
    return channel_data[cid]["bosses"]

def get_channel_timers(cid: str):
    ensure_channel_record(cid)
    return channel_data[cid]["timers"]

def parse_time(text: str) -> int:
    """Parse time input like '1h', '30m', '45s', or combinations like '1h30m'. Returns seconds."""
    text = text.strip().lower()
    total_seconds = 0
    current_number = ""
    valid_units = {'h': 3600, 'm': 60, 's': 1}
    
    for char in text:
        if char.isdigit():
            current_number += char
        elif char in valid_units:
            if not current_number:
                raise ValueError("Time format must include a number before the unit (h, m, s)")
            total_seconds += int(current_number) * valid_units[char]
            current_number = ""
        else:
            raise ValueError("Invalid time format. Use formats like '1h', '30m', '45s', or '1h30m'")
    
    if current_number:
        raise ValueError("Incomplete time format. Specify units (h, m, s)")
    
    if total_seconds <= 0:
        raise ValueError("Time must be positive")
    
    return total_seconds

async def reset_boss_timer(cid: str, boss_name: str):
    ensure_channel_record(cid)
    local = next((b for b in channel_data[cid]["bosses"]
                  if b["name"].lower() == boss_name.lower()), None)
    base = local or find_master_boss(boss_name)
    if not base:
        logger.warning(f"Boss {boss_name} not found for channel {cid}")
        return False
    channel_data[cid]["timers"][base["name"]] = now_ts() + int(base["respawn"])
    await save_json(CHANNEL_DATA_FILE, channel_data)
    logger.info(f"Reset timer for boss {boss_name} in channel {cid}")
    return True

async def set_boss_remaining(cid: str, boss_name: str, remaining_seconds: int):
    ensure_channel_record(cid)
    channel_data[cid]["timers"][boss_name] = now_ts() + int(remaining_seconds)
    await save_json(CHANNEL_DATA_FILE, channel_data)
    logger.info(f"Set remaining time for boss {boss_name} to {remaining_seconds}s in channel {cid}")

async def refresh_all_dashboards():
    logger.info("Refreshing all dashboards")
    for channel_id in list(dashboards.keys()):
        try:
            await update_dashboard_message(channel_id)
        except Exception as e:
            logger.error(f"Failed to update dashboard for channel {channel_id}: {e}")
    logger.info("Finished refreshing all dashboards")


# ----------------------------
# Event Listeners
# ----------------------------

# Register a listener for message deletion events
@bot.event
async def on_message_delete(message):
    # Check if the deleted message is a dashboard message
    for channel_id, dash_msg_id in list(dashboards.items()):
        if str(dash_msg_id) == str(message.id):
            dashboards.pop(channel_id, None)
            await save_json(DASHBOARDS_FILE, dashboards)
            logger.info(f"Dashboard message {message.id} deleted in channel {channel_id}. Dashboard reference removed.")

# ----------------------------
# UI Components
# ----------------------------
class EditTimeModal(discord.ui.Modal):
    def __init__(self, cid: str, boss_name: str):
        super().__init__(title=f"Edit Time for {boss_name} (e.g., 1h30m)")
        self.cid = cid
        self.boss_name = boss_name
        self.time_input = discord.ui.TextInput(
            label="New Remaining Time",
            placeholder="e.g., 1h, 30m, 45s, or 1h30m",
            required=True
        )
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        logger.info(f"EditTimeModal submitted for boss {self.boss_name} in channel {self.cid}")
        try:
            secs = parse_time(self.time_input.value)
        except Exception as e:
            logger.error(f"Invalid time input '{self.time_input.value}' for boss {self.boss_name}: {e}")
            await interaction.response.send_message(f"❌ {e}", ephemeral=True, delete_after=10)
            return
        await set_boss_remaining(self.cid, self.boss_name, secs)
        await update_dashboard_message(self.cid)
        await interaction.response.send_message(f"⏱ Set **{self.boss_name}** to `{self.time_input.value}` remaining.", ephemeral=True, delete_after=10)
        logger.info(f"Successfully updated time for boss {self.boss_name} to {self.time_input.value}")

class BossDropdown(discord.ui.Select):
    def __init__(self, cid: str, boss_name: str):
        self.cid = cid
        self.boss_name = boss_name
        super().__init__(
            placeholder=boss_name,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Killed", description=f"Reset {boss_name} by its default respawn"),
                discord.SelectOption(label="Edit Time", description=f"Manually set remaining time for {boss_name}")
            ]
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        logger.info(f"BossDropdown action: {choice} for boss {self.boss_name} in channel {self.cid}")
        if choice == "Killed":
            ok = await reset_boss_timer(self.cid, self.boss_name)
            await update_dashboard_message(self.cid)
            msg = "timer reset." if ok else "boss not found."
            await interaction.response.send_message(f"✅ **{self.boss_name}** {msg}", ephemeral=True, delete_after=10)
            logger.info(f"Killed action result: {msg} for boss {self.boss_name}")
        elif choice == "Edit Time":
            await interaction.response.send_modal(EditTimeModal(self.cid, self.boss_name))

class AddBossModal(discord.ui.Modal, title="Add New Boss"):
    def __init__(self, cid: str):
        super().__init__()
        self.cid = cid
        self.boss_name = discord.ui.TextInput(label="Boss Name", placeholder="Enter the boss name", required=True)
        self.respawn = discord.ui.TextInput(
            label="Respawn Time",
            placeholder="e.g., 8h, 30m, 45s, or 1h30m",
            required=True
        )
        self.add_item(self.boss_name)
        self.add_item(self.respawn)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.boss_name.value.strip()
        logger.info(f"AddBossModal submitted: {name} with respawn {self.respawn.value} in channel {self.cid}")
        try:
            respawn_seconds = parse_time(self.respawn.value.strip())
        except ValueError as e:
            logger.error(f"Invalid respawn time '{self.respawn.value}' for boss {name}: {e}")
            await interaction.response.send_message(f"❌ {e}", ephemeral=True, delete_after=10)
            return

        if not find_master_boss(name):
            bosses_master.append({"name": name, "respawn": respawn_seconds})
            await save_json(BOSSES_FILE, bosses_master)
            logger.info(f"Added {name} to master boss list with respawn {respawn_seconds}s")

        ensure_channel_record(self.cid)
        if not any(b["name"].lower() == name.lower() for b in channel_data[self.cid]["bosses"]):
            channel_data[self.cid]["bosses"].append({"name": name, "respawn": respawn_seconds})
            await save_json(CHANNEL_DATA_FILE, channel_data)
            logger.info(f"Added boss {name} to channel {self.cid}")
            # Set timer for new boss so countdown starts immediately
            await set_boss_remaining(self.cid, name, respawn_seconds)

        await update_dashboard_message(self.cid)
        await interaction.response.send_message(f"✅ Boss '{name}' added ({self.respawn.value}).", ephemeral=True, delete_after=10)

class AddBossButton(discord.ui.Button):
    def __init__(self, cid: str):
        super().__init__(label="➕ Add Boss", style=discord.ButtonStyle.green)
        self.cid = cid
    async def callback(self, interaction: discord.Interaction):
        logger.info(f"AddBossButton clicked in channel {self.cid}")
        await interaction.response.send_modal(AddBossModal(self.cid))

class RemoveBossDropdown(discord.ui.Select):
    def __init__(self, cid: str):
        self.cid = cid
        options = [discord.SelectOption(label=b["name"]) for b in get_channel_bosses(cid)]
        if not options:
            options = [discord.SelectOption(label="(No bosses)", default=True)]
        super().__init__(placeholder="Select boss to remove", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        logger.info(f"RemoveBossDropdown action: Removing {choice} from channel {self.cid}")
        if choice == "(No bosses)":
            await interaction.response.send_message("No bosses to remove.", ephemeral=True, delete_after=10)
            logger.info("No bosses available to remove")
            return
        ensure_channel_record(self.cid)
        channel_data[self.cid]["bosses"] = [b for b in channel_data[self.cid]["bosses"] if b["name"] != choice]
        channel_data[self.cid]["timers"].pop(choice, None)
        await save_json(CHANNEL_DATA_FILE, channel_data)
        await update_dashboard_message(self.cid)
        await interaction.response.send_message(f"🗑 Removed '{choice}' from this channel.", ephemeral=True, delete_after=10)
        logger.info(f"Removed boss {choice} from channel {self.cid}")

class RemoveBossButton(discord.ui.Button):
    def __init__(self, cid: str):
        super().__init__(label="🗑 Remove Boss", style=discord.ButtonStyle.danger)
        self.cid = cid
    async def callback(self, interaction: discord.Interaction):
        logger.info(f"RemoveBossButton clicked in channel {self.cid}")
        view = discord.ui.View(timeout=60)
        view.add_item(RemoveBossDropdown(self.cid))
        await interaction.response.send_message("Choose a boss to remove:", view=view, ephemeral=True, delete_after=30)

class DashboardView(discord.ui.View):
    def __init__(self, cid: str):
        super().__init__(timeout=None)
        self.cid = cid
        bosses = get_channel_bosses(cid)
        # Limit to 23 bosses to leave space for AddBossButton and RemoveBossButton (25 component limit)
        max_dropdowns = 23
        for i, b in enumerate(bosses[:max_dropdowns]):
            self.add_item(BossDropdown(cid, b["name"]))
        self.add_item(AddBossButton(cid))
        self.add_item(RemoveBossButton(cid))
        # Log component count for debugging
        component_count = len(self.children)
        logger.info(f"DashboardView for channel {cid}: {component_count} components ({len(bosses[:max_dropdowns])} bosses, 2 buttons)")
        if len(bosses) > max_dropdowns:
            logger.warning(f"Channel {cid} has {len(bosses)} bosses, but only {max_dropdowns} included in DashboardView due to 25-component limit")

# ----------------------------
# Dashboard render/update
# ----------------------------
async def update_dashboard_message(channel_id: str):
    if channel_id not in dashboards:
        logger.warning(f"No dashboard found for channel {channel_id}")
        return
    channel = bot.get_channel(int(channel_id))
    if not channel:
        logger.warning(f"Channel {channel_id} not found, removing dashboard")
        dashboards.pop(channel_id, None)
        await save_json(DASHBOARDS_FILE, dashboards)
        return
    try:
        msg = await channel.fetch_message(int(dashboards[channel_id]))
    except discord.NotFound:
        logger.warning(f"Dashboard message {dashboards[channel_id]} not found in channel {channel_id}, removing")
        dashboards.pop(channel_id, None)
        await save_json(DASHBOARDS_FILE, dashboards)
        return
    except discord.Forbidden:
        logger.error(f"Bot lacks permission to fetch message {dashboards[channel_id]} in channel {channel_id}")
        return
    except discord.HTTPException as e:
        logger.error(f"HTTP error fetching message {dashboards[channel_id]} in channel {channel_id}: {e}")
        return

    ensure_channel_record(channel_id)
    bosses = get_channel_bosses(channel_id)
    timers = get_channel_timers(channel_id)

    lines = []
    # Track which bosses have already received the 60s warning per channel
    if not hasattr(update_dashboard_message, "warned_bosses"):
        update_dashboard_message.warned_bosses = {}
    warned_bosses = update_dashboard_message.warned_bosses.setdefault(channel_id, set())

    for b in bosses:
        name = b["name"]
        if name in timers:
            remaining = timers[name] - now_ts()
            hms = fmt_hms(remaining)
            respawn_ts = int(timers[name])
            lines.append(f"**{name}** — Respawns <t:{respawn_ts}:R> (`{hms}`)")
            # Send a warning if timer enters 1-60s window and hasn't been warned yet
            if 1 <= remaining <= 90 and name not in warned_bosses:
                try:
                    await channel.send(f"{name} will be ready in {remaining} seconds", delete_after=25)
                    logger.info(f"Sent warning for boss {name} in channel {channel_id}")
                    warned_bosses.add(name)
                except Exception as e:
                    logger.error(f"Failed to send 60 second warning for boss {name} in channel {channel_id}: {e}")
            # Reset warning if timer is above 60s (for next cycle)
            elif remaining > 60 and name in warned_bosses:
                warned_bosses.remove(name)
        else:
            lines.append(f"**{name}** — READY (`00:00:00`)")

    if not lines:
        lines = ["No bosses yet. Use ➕ **Add Boss** to get started."]

    embed = discord.Embed(title="Boss Timers", description="\n".join(lines), color=0x00ff00)
    # Add warning if bosses are excluded from the view
    if len(bosses) > 23:
        embed.set_footer(text="Some bosses excluded due to component limit. Use /updatetime or /reset for others.")

    files = []
    logo_path = "logo.png"
    if os.path.exists(logo_path):
        embed.set_thumbnail(url="attachment://logo.png")
        files = [discord.File(logo_path, filename="logo.png")]
        logger.info(f"Added logo to dashboard for channel {channel_id}")

    try:
        await msg.edit(embed=embed, view=DashboardView(channel_id), attachments=files)
        logger.info(f"Updated dashboard message for channel {channel_id}")
    except discord.Forbidden:
        logger.error(f"Bot lacks permission to edit message {dashboards[channel_id]} in channel {channel_id}")
    except discord.HTTPException as e:
        logger.error(f"HTTP error editing dashboard message {dashboards[channel_id]} in channel {channel_id}: {e}")
    except ValueError as e:
        logger.error(f"Failed to create DashboardView for channel {channel_id}: {e}")
        # Fallback: Update without view to prevent task crash
        await msg.edit(embed=embed, attachments=files)
        logger.info(f"Fallback: Updated dashboard message for channel {channel_id} without view due to ValueError")
    except Exception as e:
        logger.error(f"Unexpected error updating dashboard for channel {channel_id}: {e}")

@tasks.loop(seconds=60)  # Update every minute
async def update_dashboards():
    logger.info("Starting dashboard update cycle")
    await refresh_all_dashboards()

# ----------------------------
# Slash Commands
# ----------------------------
@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        logger.info(f"Bot logged in as {bot.user} and command tree synced")
    except Exception as e:
        logger.error(f"Failed to sync command tree: {e}")
    update_dashboards.start()

@bot.tree.command(description="Create a boss dashboard in this channel.")
async def setdashboard(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    logger.info(f"/setdashboard called in channel {channel_id} by {interaction.user}")
    if channel_id in dashboards:
        msg_id = dashboards[channel_id]
        await interaction.response.send_message(
            f"Dashboard already exists: <https://discord.com/channels/{interaction.guild.id}/{channel_id}/{msg_id}>",
            ephemeral=True
        )
        logger.info(f"Dashboard already exists for channel {channel_id}: {msg_id}")
        return

    ensure_channel_record(channel_id)
    bosses = get_channel_bosses(channel_id)
    timers = get_channel_timers(channel_id)

    lines = []
    for b in bosses:
        name = b["name"]
        if name in timers:
            remaining = timers[name] - now_ts()
            hms = fmt_hms(remaining)
            respawn_ts = int(timers[name])
            lines.append(f"**{name}** — Respawns <t:{respawn_ts}:R> (`{hms}`)")
        else:
            lines.append(f"**{name}** — READY (`00:00:00`)")
    if not lines:
        lines = ["No bosses yet. Use ➕ **Add Boss** to get started."]

    embed = discord.Embed(title="Boss Timers", description="\n".join(lines), color=0x00ff00)
    # Add warning if bosses are excluded from the view
    if len(bosses) > 23:
        embed.set_footer(text="Some bosses excluded due to component limit. Use /updatetime or /reset for others.")

    files = []
    logo_path = "logo.png"
    if os.path.exists(logo_path):
        embed.set_thumbnail(url="attachment://logo.png")
        files = [discord.File(logo_path, filename="logo.png")]
        logger.info(f"Added logo to new dashboard for channel {channel_id}")

    try:
        msg = await interaction.channel.send(embed=embed, view=DashboardView(channel_id), files=files)
        dashboards[channel_id] = str(msg.id)
        await save_json(DASHBOARDS_FILE, dashboards)
        logger.info(f"Created dashboard for channel {channel_id}, message ID: {msg.id}")
    except Exception as e:
        logger.error(f"Failed to create dashboard for channel {channel_id}: {e}")
        await interaction.response.send_message("❌ Failed to create dashboard.", ephemeral=True, delete_after=10)
        return

    if interaction.channel.permissions_for(interaction.guild.me).manage_messages:
        try:
            await msg.pin(reason="Boss Timers Dashboard")
            logger.info(f"Pinned dashboard message {msg.id} in channel {channel_id}")
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.channel.send("⚠️ Could not pin the dashboard (missing permissions or pin limit reached).")
            logger.warning(f"Could not pin dashboard in channel {channel_id}: {e}")
    else:
        await interaction.channel.send("⚠️ Bot lacks 'Manage Messages' permission to pin the dashboard.")
        logger.warning(f"Bot lacks permission to pin dashboard in channel {channel_id}")

    await interaction.response.send_message(f"Dashboard created: {msg.jump_url}", ephemeral=True, delete_after=10)

@bot.tree.command(description="Set remaining time for a boss in this channel (e.g., 1h30m).")
@app_commands.describe(name="Exact boss name", time="Time left, e.g., 1h, 30m, or 1h30m")
async def updatetime(interaction: discord.Interaction, name: str, time: str):
    cid = str(interaction.channel.id)
    logger.info(f"/updatetime called for boss {name} with time {time} in channel {cid} by {interaction.user}")
    if not any(b["name"].lower() == name.lower() for b in get_channel_bosses(cid)):
        await interaction.response.send_message("❌ Boss not tracked in this channel.", ephemeral=True, delete_after=10)
        logger.warning(f"Boss {name} not tracked in channel {cid}")
        return
    try:
        secs = parse_time(time)
    except Exception as e:
        logger.error(f"Invalid time format '{time}' for boss {name} in channel {cid}: {e}")
        await interaction.response.send_message(f"❌ {e}", ephemeral=True, delete_after=10)
        return

    await set_boss_remaining(cid, name, secs)
    await update_dashboard_message(cid)
    await interaction.response.send_message(f"⏱ Set **{name}** to `{time}` remaining.", ephemeral=True, delete_after=10)
    logger.info(f"Successfully set {name} to {time} remaining in channel {cid}")

@bot.tree.command(description="Add a boss (admin). Also updates master list if needed.")
@app_commands.describe(name="Boss name", respawn_time="Default respawn time, e.g., 8h, 30m, or 1h30m")
@app_commands.checks.has_permissions(administrator=True)
async def addboss(interaction: discord.Interaction, name: str, respawn_time: str):
    cid = str(interaction.channel.id)
    logger.info(f"/addboss called for boss {name} with respawn {respawn_time} in channel {cid} by {interaction.user}")

    try:
        respawn_seconds = parse_time(respawn_time)
    except ValueError as e:
        logger.error(f"Invalid respawn time '{respawn_time}' for boss {name}: {e}")
        await interaction.response.send_message(f"❌ {e}", ephemeral=True, delete_after=10)
        return

    if not find_master_boss(name):
        bosses_master.append({"name": name, "respawn": respawn_seconds})
        await save_json(BOSSES_FILE, bosses_master)
        logger.info(f"Added {name} to master boss list with respawn {respawn_seconds}s")

    ensure_channel_record(cid)
    if not any(b["name"].lower() == name.lower() for b in channel_data[cid]["bosses"]):
        channel_data[cid]["bosses"].append({"name": name, "respawn": respawn_seconds})
        await save_json(CHANNEL_DATA_FILE, channel_data)
        logger.info(f"Added boss {name} to channel {cid}")

    await update_dashboard_message(cid)
    await interaction.response.send_message(f"✅ Boss '{name}' added ({respawn_time}).", ephemeral=True, delete_after=10)

@bot.tree.command(description="Remove a boss from THIS channel only.")
@app_commands.describe(name="Boss name to remove")
@app_commands.checks.has_permissions(administrator=True)
async def removeboss(interaction: discord.Interaction, name: str):
    cid = str(interaction.channel.id)
    logger.info(f"/removeboss called for boss {name} in channel {cid} by {interaction.user}")
    ensure_channel_record(cid)
    before = len(channel_data[cid]["bosses"])
    channel_data[cid]["bosses"] = [b for b in channel_data[cid]["bosses"] if b["name"].lower() != name.lower()]
    channel_data[cid]["timers"].pop(name, None)
    await save_json(CHANNEL_DATA_FILE, channel_data)
    await update_dashboard_message(cid)
    after = len(channel_data[cid]["bosses"])
    if before == after:
        await interaction.response.send_message("❌ Boss not found in this channel.", ephemeral=True, delete_after=10)
        logger.warning(f"Boss {name} not found in channel {cid}")
    else:
        await interaction.response.send_message(f"🗑 Removed '{name}' from this channel.", ephemeral=True, delete_after=10)
        logger.info(f"Removed boss {name} from channel {cid}")

@bot.tree.command(description="Mark a boss as killed (uses default respawn).")
@app_commands.describe(name="Exact boss name")
async def reset(interaction: discord.Interaction, name: str):
    cid = str(interaction.channel.id)
    logger.info(f"/reset called for boss {name} in channel {cid} by {interaction.user}")
    ok = await reset_boss_timer(cid, name)
    await update_dashboard_message(cid)
    await interaction.response.send_message(
        f"{'✅' if ok else '❌'} {name} {'timer reset.' if ok else 'not found.'}",
        ephemeral=True
    )
    logger.info(f"Reset action for {name} in channel {cid}: {'success' if ok else 'failed'}")

# ----------------------------
# Run
# ----------------------------
async def main():
    if not TOKEN:
        logger.error("DISCORD_TOKEN not found in environment variables")
        print("Error: DISCORD_TOKEN not found in environment variables.")
        return
    set_file_permissions()  # Set JSON file permissions before loading
    await load_initial_data()
    try:
        await bot.start(TOKEN)
    except discord.LoginFailure:
        logger.error("Invalid Discord token. Please check your DISCORD_TOKEN.")
        print("Error: Invalid Discord token. Please check your DISCORD_TOKEN.")
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        print(f"Error starting bot: {e}")

if __name__ == "__main__":
    asyncio.run(main())

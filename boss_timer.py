import discord
from discord import app_commands
from discord.ext import commands, tasks
import os, json, asyncio, tempfile
from datetime import datetime
from dotenv import load_dotenv

# ----------------------------
# Setup
# ----------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

BOSSES_FILE = "bosses.json"            # master defaults (global)
CHANNEL_DATA_FILE = "channel_data.json"  # per-channel bosses + timers
DASHBOARDS_FILE = "dashboards.json"    # {channel_id: message_id}

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
            with open(path, "r") as f:
                return json.load(f)
        return default

async def save_json(path, data):
    async with _get_lock(path):
        # Write to a temporary file and rename atomically
        with tempfile.NamedTemporaryFile("w", delete=False, dir=os.path.dirname(path) or ".") as tmp:
            json.dump(data, tmp, indent=4)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, path)

# Initial async load at startup
async def load_initial_data():
    global bosses_master, channel_data, dashboards
    bosses_master = await load_json(BOSSES_FILE, [])          # [{name, respawn}]
    channel_data = await load_json(CHANNEL_DATA_FILE, {})     # {cid: {bosses:[{name, respawn}], timers:{name: ts}}}
    dashboards = await load_json(DASHBOARDS_FILE, {})         # {cid: msg_id}

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
    return int(datetime.utcnow().timestamp())

def ensure_channel_record(cid: str):
    if cid not in channel_data:
        channel_data[cid] = {"bosses": [], "timers": {}}
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

def parse_hms(text: str) -> int:
    """Return seconds for 'HH:MM:SS' (tolerates H:MM:SS). Raises ValueError on bad format."""
    parts = text.strip().split(":")
    if len(parts) != 3:
        raise ValueError("Use HH:MM:SS")
    h, m, s = [int(x) for x in parts]
    if m < 0 or m >= 60 or s < 0 or s >= 60 or h < 0:
        raise ValueError("Invalid time range")
    return h * 3600 + m * 60 + s

async def reset_boss_timer(cid: str, boss_name: str):
    ensure_channel_record(cid)
    local = next((b for b in channel_data[cid]["bosses"]
                  if b["name"].lower() == boss_name.lower()), None)
    base = local or find_master_boss(boss_name)
    if not base:
        return False
    channel_data[cid]["timers"][base["name"]] = now_ts() + int(base["respawn"])
    await save_json(CHANNEL_DATA_FILE, channel_data)
    return True

async def set_boss_remaining(cid: str, boss_name: str, remaining_seconds: int):
    ensure_channel_record(cid)
    channel_data[cid]["timers"][boss_name] = now_ts() + int(remaining_seconds)
    await save_json(CHANNEL_DATA_FILE, channel_data)

async def refresh_all_dashboards():
    for channel_id in list(dashboards.keys()):
        await update_dashboard_message(channel_id)

# ----------------------------
# UI Components
# ----------------------------
class EditTimeModal(discord.ui.Modal):
    def __init__(self, cid: str, boss_name: str):
        super().__init__(title=f"Edit Time for {boss_name} (HH:MM:SS)")
        self.cid = cid
        self.boss_name = boss_name
        self.time_input = discord.ui.TextInput(
            label="New Remaining Time",
            placeholder="HH:MM:SS (e.g., 00:02:00)",
            required=True
        )
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            secs = parse_hms(self.time_input.value)
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await set_boss_remaining(self.cid, self.boss_name, secs)
        await update_dashboard_message(self.cid)
        await interaction.response.send_message(f"⏱ Set **{self.boss_name}** to `{self.time_input.value}` remaining.", ephemeral=True)

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
        if choice == "Killed":
            ok = await reset_boss_timer(self.cid, self.boss_name)
            await update_dashboard_message(self.cid)
            msg = "timer reset." if ok else "boss not found."
            await interaction.response.send_message(f"✅ **{self.boss_name}** {msg}", ephemeral=True)
        elif choice == "Edit Time":
            await interaction.response.send_modal(EditTimeModal(self.cid, self.boss_name))

class AddBossModal(discord.ui.Modal, title="Add New Boss"):
    def __init__(self, cid: str):
        super().__init__()
        self.cid = cid
        self.boss_name = discord.ui.TextInput(label="Boss Name", placeholder="Enter the boss name", required=True)
        self.respawn = discord.ui.TextInput(label="Respawn (seconds)", placeholder="e.g., 28800", required=True)
        self.add_item(self.boss_name)
        self.add_item(self.respawn)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.boss_name.value.strip()
        try:
            respawn_seconds = int(self.respawn.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ Respawn time must be a number of seconds.", ephemeral=True)
            return

        if not find_master_boss(name):
            bosses_master.append({"name": name, "respawn": respawn_seconds})
            await save_json(BOSSES_FILE, bosses_master)

        ensure_channel_record(self.cid)
        if not any(b["name"].lower() == name.lower() for b in channel_data[self.cid]["bosses"]):
            channel_data[self.cid]["bosses"].append({"name": name, "respawn": respawn_seconds})
            await save_json(CHANNEL_DATA_FILE, channel_data)

        await update_dashboard_message(self.cid)
        await interaction.response.send_message(f"✅ Boss '{name}' added ({respawn_seconds}s).", ephemeral=True)

class AddBossButton(discord.ui.Button):
    def __init__(self, cid: str):
        super().__init__(label="➕ Add Boss", style=discord.ButtonStyle.green)
        self.cid = cid
    async def callback(self, interaction: discord.Interaction):
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
        if choice == "(No bosses)":
            await interaction.response.send_message("No bosses to remove.", ephemeral=True)
            return
        ensure_channel_record(self.cid)
        channel_data[self.cid]["bosses"] = [b for b in channel_data[self.cid]["bosses"] if b["name"] != choice]
        channel_data[self.cid]["timers"].pop(choice, None)
        await save_json(CHANNEL_DATA_FILE, channel_data)
        await update_dashboard_message(self.cid)
        await interaction.response.send_message(f"🗑 Removed '{choice}' from this channel.", ephemeral=True)

class RemoveBossButton(discord.ui.Button):
    def __init__(self, cid: str):
        super().__init__(label="🗑 Remove Boss", style=discord.ButtonStyle.danger)
        self.cid = cid
    async def callback(self, interaction: discord.Interaction):
        view = discord.ui.View(timeout=60)
        view.add_item(RemoveBossDropdown(self.cid))
        await interaction.response.send_message("Choose a boss to remove:", view=view, ephemeral=True)

class DashboardView(discord.ui.View):
    def __init__(self, cid: str):
        super().__init__(timeout=None)
        self.cid = cid
        for b in get_channel_bosses(cid):
            self.add_item(BossDropdown(cid, b["name"]))
        self.add_item(AddBossButton(cid))
        self.add_item(RemoveBossButton(cid))

# ----------------------------
# Dashboard render/update
# ----------------------------
async def update_dashboard_message(channel_id: str):
    if channel_id not in dashboards:
        return
    channel = bot.get_channel(int(channel_id))
    if not channel:
        dashboards.pop(channel_id, None)
        await save_json(DASHBOARDS_FILE, dashboards)
        return
    try:
        msg = await channel.fetch_message(int(dashboards[channel_id]))
    except discord.NotFound:
        dashboards.pop(channel_id, None)
        await save_json(DASHBOARDS_FILE, dashboards)
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

    files = []
    logo_path = "logo.png"
    if os.path.exists(logo_path):
        embed.set_thumbnail(url="attachment://logo.png")
        files = [discord.File(logo_path, filename="logo.png")]

    await msg.edit(embed=embed, view=DashboardView(channel_id), attachments=files)

@tasks.loop(seconds=60)  # Update every minute
async def update_dashboards():
    for channel_id in list(dashboards.keys()):
        await update_dashboard_message(channel_id)

# ----------------------------
# Slash Commands
# ----------------------------
@bot.event
async def on_ready():
    await bot.tree.sync()
    update_dashboards.start()
    print(f"Logged in as {bot.user}")

@bot.tree.command(description="Create a boss dashboard in this channel.")
async def setdashboard(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    if channel_id in dashboards:
        msg_id = dashboards[channel_id]
        await interaction.response.send_message(
            f"Dashboard already exists: <https://discord.com/channels/{interaction.guild.id}/{channel_id}/{msg_id}>",
            ephemeral=True
        )
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

    files = []
    logo_path = "logo.png"
    if os.path.exists(logo_path):
        embed.set_thumbnail(url="attachment://logo.png")
        files = [discord.File(logo_path, filename="logo.png")]

    msg = await interaction.channel.send(embed=embed, view=DashboardView(channel_id), files=files)
    dashboards[channel_id] = str(msg.id)
    await save_json(DASHBOARDS_FILE, dashboards)

    if interaction.channel.permissions_for(interaction.guild.me).manage_messages:
        try:
            await msg.pin(reason="Boss Timers Dashboard")
        except (discord.Forbidden, discord.HTTPException):
            await interaction.channel.send("⚠️ Could not pin the dashboard (missing permissions or pin limit reached).")
    else:
        await interaction.channel.send("⚠️ Bot lacks 'Manage Messages' permission to pin the dashboard.")

    await interaction.response.send_message(f"Dashboard created: {msg.jump_url}", ephemeral=True)

@bot.tree.command(description="Set remaining time for a boss in this channel (HH:MM:SS).")
@app_commands.describe(name="Exact boss name", hhmmss="Time left, e.g. 00:02:00")
async def updatetime(interaction: discord.Interaction, name: str, hhmmss: str):
    cid = str(interaction.channel.id)
    if not any(b["name"].lower() == name.lower() for b in get_channel_bosses(cid)):
        await interaction.response.send_message("❌ Boss not tracked in this channel.", ephemeral=True)
        return
    try:
        secs = parse_hms(hhmmss)
    except Exception as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    await set_boss_remaining(cid, name, secs)
    await update_dashboard_message(cid)
    await interaction.response.send_message(f"⏱ Set **{name}** to `{hhmmss}` remaining.", ephemeral=True)

@bot.tree.command(description="Add a boss (admin). Also updates master list if needed.")
@app_commands.describe(name="Boss name", respawn_seconds="Default respawn time in seconds")
@app_commands.checks.has_permissions(administrator=True)
async def addboss(interaction: discord.Interaction, name: str, respawn_seconds: int):
    cid = str(interaction.channel.id)

    if not find_master_boss(name):
        bosses_master.append({"name": name, "respawn": respawn_seconds})
        await save_json(BOSSES_FILE, bosses_master)

    ensure_channel_record(cid)
    if not any(b["name"].lower() == name.lower() for b in channel_data[cid]["bosses"]):
        channel_data[cid]["bosses"].append({"name": name, "respawn": respawn_seconds})
        await save_json(CHANNEL_DATA_FILE, channel_data)

    await update_dashboard_message(cid)
    await interaction.response.send_message(f"✅ Boss '{name}' added ({respawn_seconds}s).", ephemeral=True)

@bot.tree.command(description="Remove a boss from THIS channel only.")
@app_commands.describe(name="Boss name to remove")
@app_commands.checks.has_permissions(administrator=True)
async def removeboss(interaction: discord.Interaction, name: str):
    cid = str(interaction.channel.id)
    ensure_channel_record(cid)
    before = len(channel_data[cid]["bosses"])
    channel_data[cid]["bosses"] = [b for b in channel_data[cid]["bosses"] if b["name"].lower() != name.lower()]
    channel_data[cid]["timers"].pop(name, None)
    await save_json(CHANNEL_DATA_FILE, channel_data)
    await update_dashboard_message(cid)
    after = len(channel_data[cid]["bosses"])
    if before == after:
        await interaction.response.send_message("❌ Boss not found in this channel.", ephemeral=True)
    else:
        await interaction.response.send_message(f"🗑 Removed '{name}' from this channel.", ephemeral=True)

@bot.tree.command(description="Mark a boss as killed (uses default respawn).")
@app_commands.describe(name="Exact boss name")
async def reset(interaction: discord.Interaction, name: str):
    cid = str(interaction.channel.id)
    ok = await reset_boss_timer(cid, name)
    await update_dashboard_message(cid)
    await interaction.response.send_message(
        f"{'✅' if ok else '❌'} {name} {'timer reset.' if ok else 'not found.'}",
        ephemeral=True
    )

# ----------------------------
# Run
# ----------------------------
async def main():
    if not TOKEN:
        print("Error: DISCORD_TOKEN not found in environment variables.")
        return
    await load_initial_data()
    try:
        await bot.start(TOKEN)
    except discord.LoginFailure:
        print("Error: Invalid Discord token. Please check your DISCORD_TOKEN.")
    except Exception as e:
        print(f"Error starting bot: {e}")

if __name__ == "__main__":
    asyncio.run(main())
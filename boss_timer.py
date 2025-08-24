import asyncio
import json
import os
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ----------------------------
# Build tag (visible in logs)
# ----------------------------
BUILD_TAG = "2025-08-24-compactview-rowfix-fallback-v2"

# ----------------------------
# Setup
# ----------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

BOSSES_FILE = "bosses.json"        # master defaults (global)
CHANNEL_DATA_FILE = "channel_data.json"  # per-channel bosses + timers + subs + alerts + creators
DASHBOARDS_FILE = "dashboards.json"      # {channel_id: message_id}

# ----------------------------
# Async JSON I/O with locks
# ----------------------------
_locks = {}

def _get_lock(path: str) -> asyncio.Lock:
    if path not in _locks:
        _locks[path] = asyncio.Lock()
    return _locks[path]

def _load_sync(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default

async def load_json(path, default):
    async with _get_lock(path):
        return _load_sync(path, default)

async def save_json(path, data):
    async with _get_lock(path):
        with open(path, "w") as f:
            json.dump(data, f, indent=4)

# initial sync load (safe at startup)
bosses_master = _load_sync(BOSSES_FILE, [])  # [{name, respawn}]
# channel_data[cid] = {
#   "bosses":[{name, respawn}],
#   "timers":{name: ts},
#   "subs":{name: [user_id,...]},
#   "creators":{name: user_id},
#   "alerts":{name: {"warn60": bool, "respawned": bool}}
# }
channel_data = _load_sync(CHANNEL_DATA_FILE, {})
dashboards = _load_sync(DASHBOARDS_FILE, {})  # {cid: msg_id}

# ----------------------------
# Bot
# ----------------------------
intents = discord.Intents.default()
intents.message_content = False
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
    # Py 3.13+: use timezone-aware UTC (no utcnow)
    return int(datetime.now(timezone.utc).timestamp())

def ensure_channel_record(cid: str):
    if cid not in channel_data:
        channel_data[cid] = {"bosses": [], "timers": {}, "subs": {}, "creators": {}, "alerts": {}}
    cd = channel_data[cid]
    cd.setdefault("bosses", [])
    cd.setdefault("timers", {})
    cd.setdefault("subs", {})
    cd.setdefault("creators", {})
    cd.setdefault("alerts", {})

def get_channel_bosses(cid: str):
    ensure_channel_record(cid)
    return channel_data[cid]["bosses"]

def get_channel_timers(cid: str):
    ensure_channel_record(cid)
    return channel_data[cid]["timers"]

def get_channel_subs(cid: str, boss_name: str):
    ensure_channel_record(cid)
    return set(channel_data[cid]["subs"].get(boss_name, []))

def set_channel_subs(cid: str, boss_name: str, subs_set):
    ensure_channel_record(cid)
    channel_data[cid]["subs"][boss_name] = list({int(x) for x in subs_set})

def set_creator(cid: str, boss_name: str, user_id: int):
    ensure_channel_record(cid)
    channel_data[cid]["creators"][boss_name] = int(user_id)

def clear_alert_flags(cid: str, boss_name: str):
    ensure_channel_record(cid)
    channel_data[cid]["alerts"][boss_name] = {"warn60": False, "respawned": False}

def parse_tokens_duration(text: str) -> int:
    """
    Accepts ONLY unit tokens: '1h', '30m', '45s', '1h30m', '1.5h'.
    Returns total seconds (int). Raises ValueError on bad format.
    """
    t = text.strip().lower().replace(" ", "")
    if not t:
        raise ValueError("Empty time.")
    tokens = re.findall(r'(\d+(?:\.\d+)?)([hms])', t)
    if not tokens:
        raise ValueError("Use unit tokens like 1h, 30m, 45s, or combos like 1h30m.")
    reconstructed = "".join(f"{v}{u}" for v, u in tokens)
    if reconstructed != t:
        raise ValueError("Unrecognized format. Valid examples: 1h, 30m, 45s, 1h30m, 1.5h.")
    total = 0.0
    for val, unit in tokens:
        v = float(val)
        if unit == "h":
            total += v * 3600
        elif unit == "m":
            total += v * 60
        else:
            total += v
    return int(round(total))

async def reset_boss_timer(cid: str, boss_name: str, created_by: int | None = None):
    ensure_channel_record(cid)
    local = next((b for b in channel_data[cid]["bosses"] if b["name"].lower() == boss_name.lower()), None)
    base = local or find_master_boss(boss_name)
    if not base:
        return False
    channel_data[cid]["timers"][base["name"]] = now_ts() + int(base["respawn"])
    if created_by is not None:
        set_creator(cid, base["name"], created_by)
    clear_alert_flags(cid, base["name"])
    await save_json(CHANNEL_DATA_FILE, channel_data)
    return True

async def set_boss_remaining(cid: str, boss_name: str, remaining_seconds: int, created_by: int | None = None):
    ensure_channel_record(cid)
    channel_data[cid]["timers"][boss_name] = now_ts() + int(remaining_seconds)
    if created_by is not None:
        set_creator(cid, boss_name, created_by)
    clear_alert_flags(cid, boss_name)
    await save_json(CHANNEL_DATA_FILE, channel_data)

def build_dashboard_embed_and_files(cid: str) -> tuple[discord.Embed, list[discord.File]]:
    ensure_channel_record(cid)
    bosses = get_channel_bosses(cid)
    timers = get_channel_timers(cid)

    lines = []
    for b in bosses:
        name = b["name"]
        subs_count = len(get_channel_subs(cid, name))
        subs_suffix = f" · {subs_count} subs" if subs_count else ""
        if name in timers:
            remaining = timers[name] - now_ts()
            if remaining > 0:
                respawn_ts = int(timers[name])
                lines.append(f"**{name}** — Respawns <t:{respawn_ts}:R>{subs_suffix}")
            else:
                lines.append(f"**{name}** — READY{subs_suffix}")
        else:
            lines.append(f"**{name}** — READY{subs_suffix}")

    if not lines:
        lines = ["No bosses yet. Use ➕ **Add Boss** to get started."]

    embed = discord.Embed(title="Boss Timers", description="\n".join(lines), color=0x00FF00)

    files = []
    logo_path = "mh.png"
    if os.path.exists(logo_path):
        embed.set_thumbnail(url="attachment://mh.png")
        files = [discord.File(logo_path, filename="mh.png")]

    return embed, files

def build_mentions(cid: str, boss_name: str) -> str:
    ensure_channel_record(cid)
    creator_id = channel_data[cid]["creators"].get(boss_name)
    subs = get_channel_subs(cid, boss_name)
    ids = set(subs)
    if creator_id:
        ids.add(int(creator_id))
    if not ids:
        return ""
    return " ".join(f"<@{uid}>" for uid in sorted(ids))

# ----------------------------
# UI Components (Compact, fixed rows)
# ----------------------------
class BossSelector(discord.ui.Select):
    def __init__(self, cid: str):
        self.cid = cid
        bosses = [b["name"] for b in get_channel_bosses(cid)]
        options = [discord.SelectOption(label=name, value=name) for name in bosses[:25]]
        super().__init__(placeholder="Select a boss…", min_values=1, max_values=1, options=options)
        self.row = 0  # occupies row 0 entirely

    async def callback(self, interaction: discord.Interaction):
        chosen = self.values[0]
        self.view.selected_boss = chosen  # type: ignore[attr-defined]
        await interaction.response.send_message(f"Selected **{chosen}**.", ephemeral=True)

class KilledSelectedButton(discord.ui.Button):
    def __init__(self, cid: str):
        super().__init__(label="Killed (Reset)", style=discord.ButtonStyle.primary)
        self.cid = cid
        self.row = 1
    async def callback(self, interaction: discord.Interaction):
        boss = getattr(self.view, "selected_boss", None)  # type: ignore[attr-defined]
        if not boss:
            await interaction.response.send_message("Select a boss first.", ephemeral=True)
            return
        ok = await reset_boss_timer(self.cid, boss, created_by=interaction.user.id)
        await update_dashboard_message(self.cid)
        await interaction.response.send_message(
            f"{'✅' if ok else '❌'} {boss} {'timer reset.' if ok else 'not found.'}",
            ephemeral=True,
        )

class EditSelectedButton(discord.ui.Button):
    def __init__(self, cid: str):
        super().__init__(label="Edit Time", style=discord.ButtonStyle.secondary)
        self.cid = cid
        self.row = 1
    async def callback(self, interaction: discord.Interaction):
        boss = getattr(self.view, "selected_boss", None)  # type: ignore[attr-defined]
        if not boss:
            await interaction.response.send_message("Select a boss first.", ephemeral=True)
            return
        await interaction.response.send_modal(EditTimeModal(self.cid, boss))

class SubscribeSelectedButton(discord.ui.Button):
    def __init__(self, cid: str):
        super().__init__(label="🔔 Subscribe", style=discord.ButtonStyle.success)
        self.cid = cid
        self.row = 1
    async def callback(self, interaction: discord.Interaction):
        boss = getattr(self.view, "selected_boss", None)  # type: ignore[attr-defined]
        if not boss:
            await interaction.response.send_message("Select a boss first.", ephemeral=True)
            return
        subs = get_channel_subs(self.cid, boss)
        subs.add(interaction.user.id)
        set_channel_subs(self.cid, boss, subs)
        await save_json(CHANNEL_DATA_FILE, channel_data)
        await update_dashboard_message(self.cid)
        await interaction.response.send_message(f"🔔 Subscribed to **{boss}**.", ephemeral=True)

class UnsubscribeSelectedButton(discord.ui.Button):
    def __init__(self, cid: str):
        super().__init__(label="🔕 Unsubscribe", style=discord.ButtonStyle.secondary)
        self.cid = cid
        self.row = 1
    async def callback(self, interaction: discord.Interaction):
        boss = getattr(self.view, "selected_boss", None)  # type: ignore[attr-defined]
        if not boss:
            await interaction.response.send_message("Select a boss first.", ephemeral=True)
            return
        subs = get_channel_subs(self.cid, boss)
        if interaction.user.id in subs:
            subs.remove(interaction.user.id)
            set_channel_subs(self.cid, boss, subs)
            await save_json(CHANNEL_DATA_FILE, channel_data)
            await update_dashboard_message(self.cid)
            await interaction.response.send_message(f"🔕 Unsubscribed from **{boss}**.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ You were not subscribed to **{boss}**.", ephemeral=True)

class EditTimeModal(discord.ui.Modal, title="Edit Boss Time (h/m/s)"):
    def __init__(self, cid: str, boss_name: str):
        super().__init__()
        self.cid = cid
        self.boss_name = boss_name
        self.time_input = discord.ui.TextInput(
            label="New Remaining Time",
            placeholder="e.g., 1h, 30m, 45s, 1h30m, 1.5h",
            required=True,
        )
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            secs = parse_tokens_duration(self.time_input.value)
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await set_boss_remaining(self.cid, self.boss_name, secs, created_by=interaction.user.id)
        await update_dashboard_message(self.cid)
        await interaction.response.send_message(
            f"⏱ Set **{self.boss_name}** to `{self.time_input.value}` (~{fmt_hms(secs)}).",
            ephemeral=True,
        )

class AddBossModal(discord.ui.Modal, title="Add New Boss"):
    def __init__(self, cid: str):
        super().__init__()
        self.cid = cid
        self.boss_name = discord.ui.TextInput(
            label="Boss Name", placeholder="Enter the boss name", required=True
        )
        self.respawn = discord.ui.TextInput(
            label="Default Respawn (h/m/s)",
            placeholder="e.g., 8h, 30m, 45s, 2h30m",
            required=True
        )
        self.add_item(self.boss_name)
        self.add_item(self.respawn)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.boss_name.value.strip()
        try:
            respawn_seconds = parse_tokens_duration(self.respawn.value.strip())
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        if not find_master_boss(name):
            bosses_master.append({"name": name, "respawn": respawn_seconds})
            await save_json(BOSSES_FILE, bosses_master)

        ensure_channel_record(self.cid)
        if not any(b["name"].lower() == name.lower() for b in channel_data[self.cid]["bosses"]):
            channel_data[self.cid]["bosses"].append({"name": name, "respawn": respawn_seconds})
            await save_json(CHANNEL_DATA_FILE, channel_data)

        await update_dashboard_message(self.cid)
        await interaction.response.send_message(
            f"✅ Boss '{name}' added ({fmt_hms(respawn_seconds)} default).", ephemeral=True
        )

class RemoveBossDropdown(discord.ui.Select):
    def __init__(self, cid: str):
        self.cid = cid
        options = [discord.SelectOption(label=b["name"]) for b in get_channel_bosses(cid)][:25]
        if not options:
            options = [discord.SelectOption(label="(No bosses)", default=True)]
        super().__init__(placeholder="Select boss to remove", min_values=1, max_values=1, options=options)
        self.row = 0  # for the ephemeral view only

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        if choice == "(No bosses)":
            await interaction.response.send_message("No bosses to remove.", ephemeral=True)
            return
        ensure_channel_record(self.cid)
        channel_data[self.cid]["bosses"] = [b for b in channel_data[self.cid]["bosses"] if b["name"] != choice]
        channel_data[self.cid]["timers"].pop(choice, None)
        channel_data[self.cid]["subs"].pop(choice, None)
        channel_data[self.cid]["creators"].pop(choice, None)
        channel_data[self.cid]["alerts"].pop(choice, None)
        await save_json(CHANNEL_DATA_FILE, channel_data)
        await update_dashboard_message(self.cid)
        await interaction.response.send_message(f"🗑 Removed '{choice}' from this channel.", ephemeral=True)

class AddBossButton(discord.ui.Button):
    def __init__(self, cid: str):
        super().__init__(label="➕ Add Boss", style=discord.ButtonStyle.green)
        self.cid = cid
        self.row = 2
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddBossModal(self.cid))

class RemoveBossButton(discord.ui.Button):
    def __init__(self, cid: str):
        super().__init__(label="🗑 Remove Boss", style=discord.ButtonStyle.danger)
        self.cid = cid
        self.row = 2
    async def callback(self, interaction: discord.Interaction):
        view = discord.ui.View(timeout=60)
        view.add_item(RemoveBossDropdown(self.cid))
        await interaction.response.send_message("Choose a boss to remove:", view=view, ephemeral=True)

class DashboardView(discord.ui.View):
    def __init__(self, cid: str):
        super().__init__(timeout=None)
        self.cid = cid
        self.selected_boss: str | None = None

        bosses = get_channel_bosses(cid)
        if bosses:
            self.add_item(BossSelector(cid))            # row 0
            self.add_item(KilledSelectedButton(cid))    # row 1
            self.add_item(EditSelectedButton(cid))      # row 1
            self.add_item(SubscribeSelectedButton(cid)) # row 1
            self.add_item(UnsubscribeSelectedButton(cid)) # row 1

        self.add_item(AddBossButton(cid))               # row 2
        self.add_item(RemoveBossButton(cid))            # row 2

class MinimalView(discord.ui.View):
    """Fallback view if the main DashboardView cannot be constructed for any reason."""
    def __init__(self, cid: str):
        super().__init__(timeout=None)
        self.add_item(AddBossButton(cid))   # row 2
        self.add_item(RemoveBossButton(cid))# row 2

def build_dashboard_view(cid: str) -> discord.ui.View:
    # If anything goes wrong constructing the main view, fall back to a minimal, safe view.
    try:
        return DashboardView(cid)
    except Exception:
        return MinimalView(cid)

# ----------------------------
# Dashboard render/update
# ----------------------------
async def update_dashboard_message(channel_id: str):
    channel = bot.get_channel(int(channel_id))
    if not channel or channel_id not in dashboards:
        return
    try:
        msg = await channel.fetch_message(int(dashboards[channel_id]))
    except discord.NotFound:
        del dashboards[channel_id]
        await save_json(DASHBOARDS_FILE, dashboards)
        return

    embed, files = build_dashboard_embed_and_files(channel_id)

    # On edit: don't pass "attachments=files" (wrong type). Either re-upload files or leave as-is.
    # We just edit embed + view; the original attachment remains unless you explicitly remove it.
    view = build_dashboard_view(channel_id)
    await msg.edit(embed=embed, view=view)

@tasks.loop(minutes=1)
async def update_dashboards():
    for channel_id in list(dashboards.keys()):
        await process_alerts_for_channel(channel_id)
        await update_dashboard_message(channel_id)

async def process_alerts_for_channel(channel_id: str):
    ensure_channel_record(channel_id)
    channel = bot.get_channel(int(channel_id))
    if not channel:
        return

    timers = channel_data[channel_id]["timers"]
    alerts = channel_data[channel_id]["alerts"]

    for boss_name, ts in list(timers.items()):
        remaining = ts - now_ts()
        boss_alerts = alerts.setdefault(boss_name, {"warn60": False, "respawned": False})

        # Warn at T-60s (only once) — WITH MENTIONS
        if 0 < remaining <= 60 and not boss_alerts.get("warn60", False):
            mentions = build_mentions(channel_id, boss_name)
            mention_prefix = f"{mentions} " if mentions else ""
            try:
                await channel.send(f"{mention_prefix}⏳ **{boss_name}** respawns in ~60s.")
            except Exception:
                pass
            boss_alerts["warn60"] = True

        # Final respawn alert at or after due time (only once)
        if remaining <= 0 and not boss_alerts.get("respawned", False):
            mentions = build_mentions(channel_id, boss_name)
            mention_prefix = f"{mentions} " if mentions else ""
            try:
                await channel.send(f"{mention_prefix}**{boss_name}** Has Respawned! GO GO GO!")
            except Exception:
                pass
            boss_alerts["respawned"] = True

    await save_json(CHANNEL_DATA_FILE, channel_data)

# ----------------------------
# Slash Commands
# ----------------------------
@bot.event
async def on_ready():
    await bot.tree.sync()
    update_dashboards.start()
    print(f"Logged in as {bot.user} | BUILD_TAG={BUILD_TAG}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        if interaction.response.is_done():
            await interaction.followup.send("❌ You need administrator permission to run this command.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ You need administrator permission to run this command.", ephemeral=True)
    else:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)

@bot.tree.command(name="setdashboard", description="Create a boss dashboard in this channel.")
@app_commands.guild_only()
async def setdashboard(interaction: discord.Interaction):
    channel_id = str(interaction.channel.id)
    ensure_channel_record(channel_id)

    if channel_id in dashboards:
        msg_id = dashboards[channel_id]
        try:
            msg = await interaction.channel.fetch_message(int(msg_id))
            await interaction.response.send_message(f"Dashboard already exists: {msg.jump_url}", ephemeral=True)
            return
        except discord.NotFound:
            pass

    embed, files = build_dashboard_embed_and_files(channel_id)
    msg = await interaction.channel.send(embed=embed, view=build_dashboard_view(channel_id), files=files)
    dashboards[channel_id] = str(msg.id)
    await save_json(DASHBOARDS_FILE, dashboards)

    try:
        await msg.pin(reason="Boss Timers Dashboard")
    except (discord.Forbidden, discord.HTTPException):
        pass

    await interaction.response.send_message(f"Dashboard created: {msg.jump_url}", ephemeral=True)

@bot.tree.command(description="Edit remaining time for a boss in this channel (use h/m/s tokens).")
@app_commands.describe(name="Exact boss name", duration="e.g., 1h, 30m, 45s, 1h30m, 1.5h")
@app_commands.guild_only()
async def edittime(interaction: discord.Interaction, name: str, duration: str):
    cid = str(interaction.channel.id)
    if not any(b["name"].lower() == name.lower() for b in get_channel_bosses(cid)):
        await interaction.response.send_message("❌ Boss not tracked in this channel.", ephemeral=True)
        return
    try:
        secs = parse_tokens_duration(duration)
    except Exception as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    await set_boss_remaining(cid, name, secs, created_by=interaction.user.id)
    await update_dashboard_message(cid)
    await interaction.response.send_message(f"⏱ Set **{name}** to `{duration}` (~{fmt_hms(secs)}).", ephemeral=True)

@bot.tree.command(description="Add a boss (admin). Also updates master list if needed.")
@app_commands.describe(name="Boss name", respawn="Default respawn (h/m/s tokens, e.g., 8h, 30m, 2h30m)")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def addboss(interaction: discord.Interaction, name: str, respawn: str):
    cid = str(interaction.channel.id)
    try:
        respawn_seconds = parse_tokens_duration(respawn)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    if not find_master_boss(name):
        bosses_master.append({"name": name, "respawn": respawn_seconds})
        await save_json(BOSSES_FILE, bosses_master)

    ensure_channel_record(cid)
    if not any(b["name"].lower() == name.lower() for b in channel_data[cid]["bosses"]):
        channel_data[cid]["bosses"].append({"name": name, "respawn": respawn_seconds})
        await save_json(CHANNEL_DATA_FILE, channel_data)

    await update_dashboard_message(cid)
    await interaction.response.send_message(f"✅ Boss '{name}' added ({fmt_hms(respawn_seconds)} default).", ephemeral=True)

@bot.tree.command(description="Remove a boss from THIS channel only.")
@app_commands.describe(name="Boss name to remove")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
async def removeboss(interaction: discord.Interaction, name: str):
    cid = str(interaction.channel.id)
    ensure_channel_record(cid)
    before = len(channel_data[cid]["bosses"])
    channel_data[cid]["bosses"] = [b for b in channel_data[cid]["bosses"] if b["name"].lower() != name.lower()]
    channel_data[cid]["timers"].pop(name, None)
    channel_data[cid]["subs"].pop(name, None)
    channel_data[cid]["creators"].pop(name, None)
    channel_data[cid]["alerts"].pop(name, None)
    await save_json(CHANNEL_DATA_FILE, channel_data)
    await update_dashboard_message(cid)
    after = len(channel_data[cid]["bosses"])
    if before == after:
        await interaction.response.send_message("❌ Boss not found in this channel.", ephemeral=True)
    else:
        await interaction.response.send_message(f"🗑 Removed '{name}' from this channel.", ephemeral=True)

@bot.tree.command(description="Reset a boss timer to its default respawn.")
@app_commands.describe(name="Exact boss name")
@app_commands.guild_only()
async def reset(interaction: discord.Interaction, name: str):
    cid = str(interaction.channel.id)
    ok = await reset_boss_timer(cid, name, created_by=interaction.user.id)
    await update_dashboard_message(cid)
    await interaction.response.send_message(
        f"{'✅' if ok else '❌'} {name} {'timer reset.' if ok else 'not found.'}",
        ephemeral=True,
    )

@bot.tree.command(description="List bosses tracked in this channel with default respawns.")
@app_commands.guild_only()
async def listbosses(interaction: discord.Interaction):
    cid = str(interaction.channel.id)
    bosses = get_channel_bosses(cid)
    if not bosses:
        await interaction.response.send_message(
            "No bosses are tracked in this channel. Use **/addboss** or the **➕ Add Boss** button.",
            ephemeral=True,
        )
        return

    lines = []
    for b in bosses:
        name = b.get("name", "Unknown")
        resp = b.get("respawn")
        if resp is None:
            master = find_master_boss(name)
            resp = int(master["respawn"]) if master else 0
        subs_count = len(get_channel_subs(cid, name))
        lines.append(f"• **{name}** — default `{fmt_hms(int(resp))}` · {subs_count} subs")

    embed = discord.Embed(title="Tracked Bosses", description="\n".join(lines), color=0x3B82F6)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="about", description="About this bot.")
@app_commands.guild_only()
async def about(interaction: discord.Interaction):
    await interaction.response.send_message("This is based off a true story...")

# ----------------------------
# Run
# ----------------------------
bot.run(TOKEN)

import discord
from discord import app_commands
from discord.ext import commands, tasks
import os, json, asyncio
from datetime import datetime
from dotenv import load_dotenv

# ----------------------------
# Setup
# ----------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

BOSSES_FILE = "bosses.json"              # master defaults (global)
CHANNEL_DATA_FILE = "channel_data.json"  # per-channel bosses + timers
DASHBOARDS_FILE = "dashboards.json"      # {channel_id: message_id}
TRACKING_FILE = "tracking.json"          # optional: per-user filters

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
bosses_master = _load_sync(BOSSES_FILE, [])          # [{name, respawn}]
channel_data = _load_sync(CHANNEL_DATA_FILE, {})     # {cid: {...}}
dashboards   = _load_sync(DASHBOARDS_FILE, {})       # {cid: msg_id}
tracking     = _load_sync(TRACKING_FILE, {})         # {user_id: [names...]}

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
    """Return seconds for 'HH:MM:SS'. Raises ValueError on bad format."""
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
class EditTimeModal(discord.ui.Modal, title="Edit Boss Time (HH:MM:SS)"):
    def __init__(self, cid: str, boss_name: str):
        super().__init__()
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
        await interaction.response.send_message(
            f"⏱ Set **{self.boss_name}** to `{self.time_input.value}` remaining.",
            ephemeral=True
        )

class BossDropdown(discord.ui.Select):
    """Per-boss dropdown with actions: Killed or Edit Time."""
    def __init__(self, cid: str, boss_name: str):
        self.cid = cid
        self.boss_name = boss_

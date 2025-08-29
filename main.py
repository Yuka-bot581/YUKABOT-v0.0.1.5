import os
import json
import re
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from myserver import server_on

# ---------------------- CONFIG & INTENTS ----------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = False
intents.reactions = True

bot = commands.Bot(command_prefix="/", intents=intents)
TREE_SYNCED = False

# ---------------------- DATA FILES ----------------------
REACTION_FILE = "reaction_roles.json"
VERIFY_FILE = "verify_config.json"

def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

reaction_roles = load_json(REACTION_FILE)
verify_config = load_json(VERIFY_FILE)

# ---------------------- HELPERS ----------------------
EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_~]+:(\d+)>$")

def normalize_emoji_key(s: str) -> str:
    s = s.strip()
    m = EMOJI_RE.match(s)
    if m:
        return f"e:{m.group(1)}"
    return s

def emoji_key_from_payload(payload_emoji) -> str:
    if payload_emoji.id:
        return f"e:{payload_emoji.id}"
    return str(payload_emoji)

async def ensure_react_permissions(channel: discord.TextChannel):
    perms = channel.permissions_for(channel.guild.me)
    needed = [
        (perms.manage_roles, "Manage Roles"),
        (perms.add_reactions, "Add Reactions"),
        (perms.read_message_history, "Read Message History"),
        (perms.send_messages, "Send Messages"),
        (perms.embed_links, "Embed Links"),
        (perms.read_messages, "View Channel"),
    ]
    missing = [name for ok, name in needed if not ok]
    if missing:
        raise PermissionError("บอทขาดสิทธิ์: " + ", ".join(missing))

async def add_reactions_safely(message: discord.Message, emoji_keys):
    for key in emoji_keys:
        try:
            if key.startswith("e:"):
                emoji_id = int(key.split(":", 1)[1])
                emoji = discord.utils.get(message.guild.emojis, id=emoji_id)
                if emoji is None:
                    continue
                await message.add_reaction(emoji)
            else:
                await message.add_reaction(key)
            await asyncio.sleep(0.3)
        except Exception:
            continue

# ---------------------- EVENTS ----------------------
@bot.event
async def on_ready():
    global TREE_SYNCED
    if not TREE_SYNCED:
        try:
            await bot.tree.sync()
            TREE_SYNCED = True
            print("/commands synced")
        except Exception as e:
            print("Failed to sync commands:", e)
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    data = reaction_roles.get(str(payload.message_id))
    if not data:
        return
    key = emoji_key_from_payload(payload.emoji)
    role_id = data.get("emoji_map", {}).get(key)
    if not role_id:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    member = guild.get_member(payload.user_id)
    role = guild.get_role(role_id)
    if member and role:
        try:
            await member.add_roles(role, reason=f"Reaction role via {key}")
            await send_log(guild, f"✅ {member.mention} ได้รับ role {role.mention} ผ่าน reaction")
        except discord.Forbidden:
            pass

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    data = reaction_roles.get(str(payload.message_id))
    if not data:
        return
    key = emoji_key_from_payload(payload.emoji)
    role_id = data.get("emoji_map", {}).get(key)
    if not role_id:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    member = guild.get_member(payload.user_id)
    role = guild.get_role(role_id)
    if member and role:
        try:
            await member.remove_roles(role, reason=f"Reaction role removed via {key}")
            await send_log(guild, f"❌ {member.mention} ถูกลบ role {role.mention} ผ่าน reaction")
        except discord.Forbidden:
            pass

# ---------------------- VERIFY SYSTEM ----------------------
class VerifyView(discord.ui.View):
    def __init__(self, role_id: int):
        super().__init__(timeout=None)
        self.role_id = role_id

    @discord.ui.button(label="✅ ยืนยันตัวตน", style=discord.ButtonStyle.green, custom_id="verify_button")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = interaction.guild.get_role(self.role_id)
        if not role:
            await interaction.response.send_message("❌ role ไม่ถูกต้อง", ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="Verify button clicked")
            await interaction.response.send_message(f"✅ คุณได้รับ role {role.mention} แล้ว", ephemeral=True)
            await send_log(interaction.guild, f"🟢 {interaction.user.mention} ยืนยันตัวตนและได้รับ {role.mention}")
        except discord.Forbidden:
            await interaction.response.send_message("❌ บอทไม่มีสิทธิ์ให้ role นี้", ephemeral=True)

@bot.tree.command(name="verifysetup", description="สร้างระบบยืนยันตัวตน (กดปุ่ม)")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(channel="ห้องที่จะโพสต์", role="role สำหรับผู้ผ่านการยืนยัน", log_channel="ห้องสำหรับ log แจ้งเตือน")
async def verifysetup(interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role, log_channel: discord.TextChannel):
    embed = discord.Embed(title="บอทยืนยันตัวตน", description="กดปุ่มด้านล่างเพื่อยืนยันตัวตนและรับยศ")
    view = VerifyView(role.id)
    msg = await channel.send(embed=embed, view=view)
    verify_config[str(interaction.guild.id)] = {"role_id": role.id, "log_channel": log_channel.id, "message_id": msg.id, "channel_id": channel.id}
    save_json(VERIFY_FILE, verify_config)
    await interaction.response.send_message(f"✅ สร้างระบบ Verify ใน {channel.mention} แล้ว", ephemeral=True)


# ---------------------- LOG FUNCTION ----------------------
async def send_log(guild: discord.Guild, text: str):
    config = verify_config.get(str(guild.id), {})
    log_channel_id = config.get("log_channel")
    if log_channel_id:
        ch = guild.get_channel(log_channel_id)
        if ch:
            await ch.send(text)

# ---------------------- SLASH COMMANDS ----------------------
@bot.tree.command(name="createrole", description="สร้างโพสต์กดอิโมจิรับยศ (reaction role)")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(
    channel="จะโพสต์ลงห้องไหน",
    title="หัวข้อของ embed",
    description="คำอธิบายใต้หัวข้อ",
    pairs="รายการคู่ EMOJI=ROLE คั่นด้วยคอมมา หรือขึ้นบรรทัดใหม่",
    image_url="ลิงก์รูป/ไฟล์ gif (ถ้ามี)",
)
async def createrole(interaction: discord.Interaction, channel: discord.TextChannel, title: str, description: str, pairs: str, image_url: str | None = None):
    await interaction.response.defer(ephemeral=True)
    try:
        await ensure_react_permissions(channel)

        # parse input pairs
        items = []
        for raw in re.split(r",|\n", pairs):
            item = raw.strip()
            if not item:
                continue
            if "=" not in item:
                raise ValueError(f"รูปแบบไม่ถูกต้อง: '{item}' (ควรเป็น EMOJI=ROLE)")
            emoji_part, role_part = [p.strip() for p in item.split("=", 1)]
            role = None
            m = re.search(r"<@&(?P<id>\d+)>", role_part)
            if m:
                role = interaction.guild.get_role(int(m.group("id")))
            elif role_part.isdigit():
                role = interaction.guild.get_role(int(role_part))
            else:
                role = discord.utils.get(interaction.guild.roles, name=role_part)
            if not role:
                raise ValueError(f"ไม่พบ role: {role_part}")
            emoji_key = normalize_emoji_key(emoji_part)
            items.append((emoji_key, role.id, emoji_part))

        # embed
        embed = discord.Embed(title=title, description=description)
        lines = [f"{emoji} → {interaction.guild.get_role(role_id).mention}" for _, role_id, emoji in items]
        embed.add_field(name="กดอิโมจิเพื่อรับยศ", value="\n".join(lines), inline=False)
        embed.set_footer(text="เอาอิโมจิออก = ถอนยศ")
        if image_url:
            embed.set_image(url=image_url)

        msg = await channel.send(embed=embed)
        emoji_map = {ek: rid for ek, rid, _ in items}
        reaction_roles[str(msg.id)] = {"emoji_map": emoji_map, "guild_id": interaction.guild.id, "channel_id": channel.id}
        save_json(REACTION_FILE, reaction_roles)
        await add_reactions_safely(msg, emoji_map.keys())

        await interaction.followup.send(f"✅ สร้างข้อความรับยศแล้วใน {channel.mention} (message id: {msg.id})", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาด: {e}", ephemeral=True)



# ---------------------- RUN ----------------------
server_on()
bot.run(os.getenv("TOKEN"))



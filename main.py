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
intents.message_content = False  # not needed
intents.reactions = True

bot = commands.Bot(command_prefix="/", intents=intents)
TREE_SYNCED = False

DATA_FILE = "reaction_roles.json"

# ---------------------- STORAGE UTIL ----------------------
def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

reaction_roles = load_data()  # {message_id: {"emoji_map": {key: role_id}, "guild_id": int, "channel_id": int}}

# ---------------------- HELPERS ----------------------
EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_~]+:(\d+)>$")


def normalize_emoji_key(s: str) -> str:
    """
    Turn an emoji string from user input or event into a comparable key.
    - For custom emoji like <a:name:1234567890>, store as f"e:{id}"
    - For unicode emoji, store the raw character(s)
    """
    s = s.strip()
    m = EMOJI_RE.match(s)
    if m:
        return f"e:{m.group(1)}"
    return s  # assume unicode


def emoji_key_from_payload(payload_emoji) -> str:
    # payload_emoji can be PartialEmoji
    if payload_emoji.id:  # custom
        return f"e:{payload_emoji.id}"
    return str(payload_emoji)  # unicode


def parse_pairs(pairs_str: str):
    """
    Parse input like: "😀=@Member, 🎮=@Gamer, <:cool:123456789>=@Cool"
    Returns list of tuples (emoji_key, role_id) and a list of user-facing labels for the embed.
    """
    pairs = []
    labels = []
    for raw in re.split(r",|\n", pairs_str):
        item = raw.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"รูปแบบไม่ถูกต้อง: '{item}' (ควรเป็น EMOJI=ROLE)")
        emoji_part, role_part = [p.strip() for p in item.split("=", 1)]
        # role can be mention <@&id> or plain name; prefer mention
        role_id = None
        m = re.search(r"<@&(?P<id>\d+)>", role_part)
        if m:
            role_id = int(m.group("id"))
        # Fallback: try to parse numeric id
        if role_id is None and role_part.isdigit():
            role_id = int(role_part)
        if role_id is None:
            # leave name; we'll resolve later during command execution
            role_id = role_part  # temporary marker
        emoji_key = normalize_emoji_key(emoji_part)
        pairs.append((emoji_key, role_id, emoji_part))
        labels.append((emoji_part, role_part))
    return pairs, labels


async def resolve_role_ids(guild: discord.Guild, items):
    """
    Convert any role names to IDs. Keeps ints as-is.
    items: list of (emoji_key, role_id_or_name, emoji_render)
    Returns list of (emoji_key, role_id, emoji_render)
    """
    resolved = []
    for emoji_key, role_ref, emoji_render in items:
        role_id = None
        if isinstance(role_ref, int):
            role_id = role_ref
        else:
            # try exact name match (case-sensitive then insensitive)
            role = discord.utils.get(guild.roles, name=role_ref)
            if role is None:
                role = discord.utils.find(lambda r: r.name.lower() == str(role_ref).lower(), guild.roles)
            if role:
                role_id = role.id
        if role_id is None:
            raise ValueError(f"ไม่พบยศ/Role: '{role_ref}' ในเซิร์ฟเวอร์นี้")
        resolved.append((emoji_key, role_id, emoji_render))
    return resolved


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
            await asyncio.sleep(0.3)  # avoid rate limits
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
        except discord.Forbidden:
            pass

# ---------------------- SLASH COMMAND ----------------------
@bot.tree.command(name="createrole", description="สร้างโพสต์กดอิโมจิรับยศ (reaction role)")
@app_commands.describe(
    channel="จะโพสต์ลงห้องไหน",
    title="หัวข้อของ embed",
    description="คำอธิบายใต้หัวข้อ",
    pairs="รายการคู่ EMOJI=ROLE คั่นด้วยคอมมา หรือขึ้นบรรทัดใหม่",
    image_url="ลิงก์รูป/ไฟล์ gif (ถ้ามี)",
)
async def createrole(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    description: str,
    pairs: str,
    image_url: str | None = None,
):
    """
    Example pairs:
    "😀=@Member, 🎮=@Gamer, <:cool:123456789012345678>=@Cool"
    หรือขึ้นบรรทัดละหนึ่งคู่ก็ได้
    """
    await interaction.response.defer(ephemeral=True)
    try:
        await ensure_react_permissions(channel)
        raw_items, labels = parse_pairs(pairs)
        items = await resolve_role_ids(interaction.guild, raw_items)

        # Build embed
        embed = discord.Embed(title=title, description=description)
        lines = []
        for _emoji_key, role_id, emoji_render in items:
            role = interaction.guild.get_role(role_id)
            if role is None:
                raise ValueError("พบ role ที่ไม่ถูกต้องหลังการตรวจสอบ")
            lines.append(f"{emoji_render}  →  {role.mention}")
        embed.add_field(name="กดอิโมจิเพื่อรับยศ", value="\n".join(lines), inline=False)
        embed.set_footer(text="เอาอิโมจิออก = ถอนยศ")
        if image_url:
            embed.set_image(url=image_url)

        msg = await channel.send(embed=embed)

        # Save mapping
        emoji_map = {ek: rid for ek, rid, _ in items}
        reaction_roles[str(msg.id)] = {
            "emoji_map": emoji_map,
            "guild_id": interaction.guild.id,
            "channel_id": channel.id,
        }
        save_data(reaction_roles)

        # Add the reactions
        await add_reactions_safely(msg, emoji_map.keys())

        await interaction.followup.send(
            f"สร้างข้อความรับยศแล้วใน {channel.mention} (message id: {msg.id})",
            ephemeral=True,
        )
    except PermissionError as pe:
        await interaction.followup.send(str(pe), ephemeral=True)
    except ValueError as ve:
        await interaction.followup.send(f"รูปแบบอินพุตผิดพลาด: {ve}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}", ephemeral=True)

server_on()
bot.run(os.getenv('TOKEN'))


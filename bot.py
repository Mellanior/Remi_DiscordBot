"""
Discord Reminder Bot (discord.py 2.x + SQLite)

Lệnh:
  /remind add     - tạo nhắc nhở (1 lần hoặc lặp lại)
  /remind list    - xem danh sách nhắc nhở của bạn
  /remind edit    - sửa nhắc nhở
  /remind delete  - xóa 1 nhắc nhở
  /remind clear   - xóa tất cả nhắc nhở của bạn
  /remind pause   - tạm dừng
  /remind resume  - tiếp tục
"""

import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Optional, Union
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # tùy chọn: để lệnh hiện ngay trong server của bạn
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh"))
DB_FILE = "reminders.db"
MIN_INTERVAL = 60  # lặp tối thiểu 60 giây để tránh spam

# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------


def db(sql, args=()):
    """Chạy 1 câu SQL. Trả về (rows, lastrowid, rowcount)."""
    con = sqlite3.connect(DB_FILE)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(sql, args)
        rows = cur.fetchall()
        con.commit()
        return rows, cur.lastrowid, cur.rowcount
    finally:
        con.close()


def next_num(user_id: int) -> int:
    """Số thứ tự nhỏ nhất chưa dùng của 1 người (#1, #2, #3...)."""
    rows, _, _ = db("SELECT num FROM reminders WHERE user_id = ?", (user_id,))
    used = {r["num"] for r in rows if r["num"] is not None}
    n = 1
    while n in used:
        n += 1
    return n


def init_db():
    db(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            guild_id   INTEGER,
            channel_id INTEGER,          -- NULL = nhắc qua tin nhắn riêng (DM)
            message    TEXT NOT NULL,
            next_ts    INTEGER NOT NULL, -- thời điểm nhắc kế tiếp (unix giây)
            interval   INTEGER NOT NULL DEFAULT 0,  -- 0 = chỉ nhắc 1 lần
            remaining  INTEGER,          -- số lần còn lại (NULL = vô hạn)
            ping_role  INTEGER,
            paused     INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Nâng cấp DB cũ: thêm cột ping (lưu dạng <@id>, <@&id> hoặc @everyone)
    try:
        db("ALTER TABLE reminders ADD COLUMN ping TEXT")
    except sqlite3.OperationalError:
        pass  # cột đã có rồi
    # Số thứ tự riêng của từng người (dùng lại số khi nhắc nhở cũ đã hết)
    try:
        db("ALTER TABLE reminders ADD COLUMN num INTEGER")
    except sqlite3.OperationalError:
        pass
    rows, _, _ = db("SELECT id, user_id FROM reminders WHERE num IS NULL ORDER BY id")
    for r in rows:
        db("UPDATE reminders SET num = ? WHERE id = ?", (next_num(r["user_id"]), r["id"]))


# ----------------------------------------------------------------------------
# Xử lý thời gian
# ----------------------------------------------------------------------------

UNITS = {
    "d": 86400, "day": 86400, "days": 86400, "ngay": 86400, "ngày": 86400,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600, "g": 3600, "gio": 3600, "giờ": 3600,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "p": 60, "phut": 60, "phút": 60,
    "s": 1, "sec": 1, "secs": 1, "giay": 1, "giây": 1,
}
_PART = re.compile(r"(\d+)\s*([^\d\s]+)")


def parse_duration(text: str) -> Optional[int]:
    """'3h45m' -> 13500 (giây). Trả None nếu sai định dạng."""
    text = text.strip().lower()
    parts = _PART.findall(text)
    if not parts or _PART.sub("", text).strip():
        return None
    total = 0
    for n, unit in parts:
        if unit not in UNITS:
            return None
        total += int(n) * UNITS[unit]
    return total or None


def parse_when(text: str) -> Optional[int]:
    """Nhận '30m', '3h45m' hoặc giờ cụ thể -> unix timestamp. None nếu sai."""
    secs = parse_duration(text)
    if secs:
        return int(time.time()) + secs

    text = text.strip()
    now = datetime.now(TZ)
    for fmt in ("%H:%M", "%d/%m %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%H:%M":
            dt = now.replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)
        elif fmt == "%d/%m %H:%M":
            dt = dt.replace(year=now.year, tzinfo=TZ)
            if dt <= now:
                dt = dt.replace(year=now.year + 1)
        else:
            dt = dt.replace(tzinfo=TZ)
        return int(dt.timestamp())
    return None


def fmt_dur(s: int) -> str:
    parts = []
    for name, sec in (("ngày", 86400), ("giờ", 3600), ("phút", 60), ("giây", 1)):
        q, s = divmod(s, sec)
        if q:
            parts.append(f"{q} {name}")
    return " ".join(parts) or "0 giây"


TIME_HELP = (
    "❌ Không hiểu thời gian. Ví dụ hợp lệ: `30m`, `1h`, `3h45m`, `2d`, "
    "`21:30`, `25/12 08:00`, `25/12/2026 08:00`"
)

# ----------------------------------------------------------------------------
# Bot
# ----------------------------------------------------------------------------


class ReminderBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        init_db()
        self.tree.add_command(remind)
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        checker.start()

    async def on_ready(self):
        print(f"✅ Đã đăng nhập: {self.user} (ID: {self.user.id})")


client = ReminderBot()
remind = app_commands.Group(name="remind", description="Quản lý nhắc nhở")


# ----------------------------------------------------------------------------
# Vòng lặp kiểm tra & gửi nhắc nhở
# ----------------------------------------------------------------------------


async def fire(r: sqlite3.Row):
    embed = discord.Embed(
        title="⏰ Nhắc nhở", description=r["message"], colour=discord.Colour.orange()
    )
    footer = f"ID #{r['num']}"
    if r["interval"] > 0:
        footer += f" • lặp mỗi {fmt_dur(r['interval'])}"
    embed.set_footer(text=footer)

    if r["channel_id"]:
        channel = client.get_channel(r["channel_id"]) or await client.fetch_channel(r["channel_id"])
        content = f"<@{r['user_id']}>"
        users = [discord.Object(id=r["user_id"])]
        roles = []
        everyone = False
        ping = r["ping"] or (f"<@&{r['ping_role']}>" if r["ping_role"] else None)
        if ping:
            content += f" {ping}"
            m = re.fullmatch(r"<@(&?)(\d+)>", ping)
            if ping == "@everyone":
                everyone = True
            elif m:
                (roles if m.group(1) else users).append(discord.Object(id=int(m.group(2))))
        await channel.send(
            content=content,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=users, roles=roles, everyone=everyone),
        )
    else:
        user = client.get_user(r["user_id"]) or await client.fetch_user(r["user_id"])
        await user.send(embed=embed)


@tasks.loop(seconds=15)
async def checker():
    now = int(time.time())
    rows, _, _ = db("SELECT * FROM reminders WHERE paused = 0 AND next_ts <= ?", (now,))
    for r in rows:
        try:
            await fire(r)
        except discord.NotFound:
            db("DELETE FROM reminders WHERE id = ?", (r["id"],))  # kênh/user không còn
            continue
        except discord.Forbidden as e:
            print(f"⚠️ Không gửi được reminder #{r['num']} (user {r['user_id']}): {e}")
            if e.code == 50278:  # bot và người dùng không còn server chung -> xóa luôn
                db("DELETE FROM reminders WHERE id = ?", (r["id"],))
                print("   -> Đã tự xóa nhắc nhở này vì không còn server chung.")
                continue
        except Exception as e:  # lỗi mạng...
            print(f"⚠️ Không gửi được reminder #{r['num']} (user {r['user_id']}): {e}")

        interval, remaining = r["interval"], r["remaining"]
        if interval <= 0 or (remaining is not None and remaining <= 1):
            db("DELETE FROM reminders WHERE id = ?", (r["id"],))
        else:
            nxt = r["next_ts"] + interval
            if nxt <= now:  # bot tắt lâu -> bỏ qua các lần đã lỡ, không spam
                nxt += ((now - nxt) // interval + 1) * interval
            new_remaining = None if remaining is None else remaining - 1
            db(
                "UPDATE reminders SET next_ts = ?, remaining = ? WHERE id = ?",
                (nxt, new_remaining, r["id"]),
            )


@checker.before_loop
async def before_checker():
    await client.wait_until_ready()


# ----------------------------------------------------------------------------
# Hàm phụ cho các lệnh
# ----------------------------------------------------------------------------


def describe(r: sqlite3.Row) -> str:
    where = f"<#{r['channel_id']}>" if r["channel_id"] else "tin nhắn riêng (DM)"
    line = f"**#{r['num']}** • <t:{r['next_ts']}:R> (<t:{r['next_ts']}:f>) • {where}"
    if r["interval"] > 0:
        line += f" • 🔁 mỗi {fmt_dur(r['interval'])}"
        if r["remaining"] is not None:
            line += f" (còn {r['remaining']} lần)"
    if r["paused"]:
        line += " • ⏸ tạm dừng"
    text = r["message"].replace("\n", " ")
    return line + "\n> " + (text[:80] + "…" if len(text) > 80 else text)


async def get_owned(interaction: discord.Interaction, num: int) -> Optional[sqlite3.Row]:
    """Lấy nhắc nhở số `num` của chính người dùng. Không có thì báo lỗi."""
    rows, _, _ = db(
        "SELECT * FROM reminders WHERE user_id = ? AND num = ?", (interaction.user.id, num)
    )
    if not rows:
        await interaction.response.send_message(
            f"❌ Bạn không có nhắc nhở #{num}.", ephemeral=True
        )
        return None
    return rows[0]


async def id_autocomplete(interaction: discord.Interaction, current: str):
    rows, _, _ = db(
        "SELECT num, message FROM reminders WHERE user_id = ? ORDER BY paused, next_ts LIMIT 100",
        (interaction.user.id,),
    )
    cur = current.lower()
    return [
        app_commands.Choice(name=f"#{r['num']} {r['message'][:60]}", value=r["num"])
        for r in rows
        if cur in str(r["num"]) or cur in r["message"].lower()
    ][:25]


MessageStr = app_commands.Range[str, 1, 1000]
# Các loại kênh bot có thể gửi tin vào (chat, thông báo, thoại, stage, thread)
SendableChannel = Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread]

# ----------------------------------------------------------------------------
# Các lệnh /remind ...
# ----------------------------------------------------------------------------


@remind.command(name="add", description="Tạo nhắc nhở mới")
@app_commands.describe(
    message="Nội dung cần nhắc",
    when="Khi nào nhắc: 30m, 1h, 3h45m, 2d, 21:30, 25/12 08:00",
    repeat="(Tùy chọn) Lặp lại mỗi bao lâu: 30m, 1h, 3h45m, 1d...",
    times="(Tùy chọn) Tổng số lần nhắc tối đa (bỏ trống = lặp mãi)",
    channel="(Tùy chọn) Kênh để nhắc (mặc định: kênh hiện tại)",
    dm="Nhắc qua tin nhắn riêng (DM) thay vì kênh",
    ping="(Tùy chọn) Người hoặc role muốn tag thêm",
    everyone="Tag @everyone (cần quyền Mention Everyone)",
)
async def add(
    interaction: discord.Interaction,
    message: MessageStr,
    when: str,
    repeat: Optional[str] = None,
    times: Optional[app_commands.Range[int, 1, 1000]] = None,
    channel: Optional[SendableChannel] = None,
    dm: bool = False,
    ping: Optional[Union[discord.User, discord.Member, discord.Role]] = None,
    everyone: bool = False,
):
    ts = parse_when(when)
    if ts is None or ts <= time.time():
        return await interaction.response.send_message(TIME_HELP, ephemeral=True)

    interval = 0
    if repeat:
        interval = parse_duration(repeat) or 0
        if interval == 0:
            return await interaction.response.send_message(
                "❌ Không hiểu khoảng lặp. Ví dụ: `30m`, `1h`, `3h45m`, `1d`", ephemeral=True
            )
        if interval < MIN_INTERVAL:
            return await interaction.response.send_message(
                f"❌ Khoảng lặp tối thiểu là {MIN_INTERVAL} giây.", ephemeral=True
            )

    channel_id = None
    if interaction.guild and not dm:
        target = channel or interaction.channel
        perms_bot = target.permissions_for(interaction.guild.me)
        perms_user = target.permissions_for(interaction.user)
        if not (perms_bot.view_channel and perms_bot.send_messages and perms_bot.embed_links):
            return await interaction.response.send_message(
                f"❌ Bot thiếu quyền gửi tin/embed trong {target.mention}.", ephemeral=True
            )
        # Kênh hiện tại (nơi bạn gõ lệnh) thì luôn cho phép; chỉ kiểm tra quyền
        # khi bạn chọn một kênh KHÁC bằng tùy chọn `channel`
        picked_other = channel is not None and channel.id != interaction.channel_id
        # Quyền do chính Discord gửi kèm lệnh (đáng tin hơn tự tính)
        is_admin = interaction.permissions.administrator
        if picked_other and not (is_admin or (perms_user.view_channel and perms_user.send_messages)):
            return await interaction.response.send_message(
                f"❌ Bạn không có quyền gửi tin trong {target.mention}.", ephemeral=True
            )
        channel_id = target.id
        if everyone and not ((is_admin or perms_user.mention_everyone) and perms_bot.mention_everyone):
            return await interaction.response.send_message(
                "❌ Cả bạn và bot đều cần quyền **Mention Everyone** trong kênh này để tag @everyone.",
                ephemeral=True,
            )

    ping_text = None
    if everyone:
        if channel_id is None:
            return await interaction.response.send_message(
                "❌ @everyone chỉ dùng được khi nhắc trong kênh server.", ephemeral=True
            )
        ping_text = "@everyone"
    elif ping:
        ping_text = f"<@&{ping.id}>" if isinstance(ping, discord.Role) else f"<@{ping.id}>"

    new_num = next_num(interaction.user.id)
    db(
        "INSERT INTO reminders (user_id, num, guild_id, channel_id, message, next_ts, interval, remaining, ping) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            interaction.user.id,
            new_num,
            interaction.guild.id if interaction.guild else None,
            channel_id,
            message.replace("\\n", "\n"),
            ts,
            interval,
            times if interval else None,
            ping_text,
        ),
    )

    text = f"✅ Đã tạo nhắc nhở **#{new_num}**\nLần đầu: <t:{ts}:F> (<t:{ts}:R>)"
    if interval:
        text += f"\n🔁 Lặp mỗi **{fmt_dur(interval)}**"
        text += f", tổng **{times}** lần" if times else ", lặp mãi đến khi bạn xóa"
    if ping_text and channel_id:
        text += f"\n📣 Tag thêm: {ping_text}"
    text += "\n📍 " + (f"<#{channel_id}>" if channel_id else "Tin nhắn riêng (DM)")
    await interaction.response.send_message(text)


@remind.command(name="list", description="Xem danh sách nhắc nhở của bạn")
async def list_cmd(interaction: discord.Interaction):
    rows, _, _ = db("SELECT * FROM reminders WHERE user_id = ? ORDER BY paused, next_ts", (interaction.user.id,))
    if not rows:
        return await interaction.response.send_message("📭 Bạn chưa có nhắc nhở nào.", ephemeral=True)

    lines, size = [], 0
    for i, r in enumerate(rows):
        line = describe(r)
        if size + len(line) > 3800:
            lines.append(f"… và {len(rows) - i} nhắc nhở khác")
            break
        lines.append(line)
        size += len(line) + 2
    embed = discord.Embed(
        title=f"📋 Nhắc nhở của bạn ({len(rows)})",
        description="\n\n".join(lines),
        colour=discord.Colour.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@remind.command(name="delete", description="Xóa 1 nhắc nhở")
@app_commands.describe(reminder_id="Chọn nhắc nhở cần xóa")
@app_commands.rename(reminder_id="id")
async def delete(interaction: discord.Interaction, reminder_id: int):
    r = await get_owned(interaction, reminder_id)
    if not r:
        return
    db("DELETE FROM reminders WHERE id = ?", (r["id"],))
    await interaction.response.send_message(f"🗑️ Đã xóa nhắc nhở #{reminder_id}.", ephemeral=True)


delete.autocomplete("reminder_id")(id_autocomplete)


@remind.command(name="clear", description="Xóa TẤT CẢ nhắc nhở của bạn")
async def clear(interaction: discord.Interaction):
    _, _, n = db("DELETE FROM reminders WHERE user_id = ?", (interaction.user.id,))
    await interaction.response.send_message(f"🗑️ Đã xóa {n} nhắc nhở.", ephemeral=True)


@remind.command(name="pause", description="Tạm dừng 1 nhắc nhở")
@app_commands.rename(reminder_id="id")
async def pause(interaction: discord.Interaction, reminder_id: int):
    r = await get_owned(interaction, reminder_id)
    if not r:
        return
    db("UPDATE reminders SET paused = 1 WHERE id = ?", (r["id"],))
    await interaction.response.send_message(f"⏸ Đã tạm dừng #{reminder_id}.", ephemeral=True)


pause.autocomplete("reminder_id")(id_autocomplete)


@remind.command(name="resume", description="Tiếp tục 1 nhắc nhở đã tạm dừng")
@app_commands.rename(reminder_id="id")
async def resume(interaction: discord.Interaction, reminder_id: int):
    r = await get_owned(interaction, reminder_id)
    if not r:
        return
    now = int(time.time())
    nxt = r["next_ts"]
    if nxt <= now:
        if r["interval"] > 0:
            nxt += ((now - nxt) // r["interval"] + 1) * r["interval"]
        else:
            nxt = now  # nhắc 1 lần đã quá hạn -> nhắc ngay
    db("UPDATE reminders SET paused = 0, next_ts = ? WHERE id = ?", (nxt, r["id"]))
    await interaction.response.send_message(
        f"▶️ Đã tiếp tục #{reminder_id}. Lần tới: <t:{nxt}:R>", ephemeral=True
    )


resume.autocomplete("reminder_id")(id_autocomplete)


@remind.command(name="edit", description="Sửa nhắc nhở (chỉ điền phần muốn đổi)")
@app_commands.describe(
    reminder_id="Chọn nhắc nhở cần sửa",
    message="Nội dung mới",
    when="Thời điểm nhắc kế tiếp mới (30m, 1h, 21:30, 25/12 08:00...)",
    repeat="Khoảng lặp mới (30m, 1h...) hoặc gõ 'off' để tắt lặp",
    times="Số lần nhắc còn lại",
)
@app_commands.rename(reminder_id="id")
async def edit(
    interaction: discord.Interaction,
    reminder_id: int,
    message: Optional[MessageStr] = None,
    when: Optional[str] = None,
    repeat: Optional[str] = None,
    times: Optional[app_commands.Range[int, 1, 1000]] = None,
):
    r = await get_owned(interaction, reminder_id)
    if not r:
        return

    updates = {}
    if message:
        updates["message"] = message.replace("\\n", "\n")
    if when:
        ts = parse_when(when)
        if ts is None or ts <= time.time():
            return await interaction.response.send_message(TIME_HELP, ephemeral=True)
        updates["next_ts"] = ts
    if repeat is not None:
        if repeat.strip().lower() in ("off", "tat", "tắt", "0", "none"):
            updates["interval"] = 0
            updates["remaining"] = None
        else:
            secs = parse_duration(repeat)
            if not secs or secs < MIN_INTERVAL:
                return await interaction.response.send_message(
                    f"❌ Khoảng lặp không hợp lệ (tối thiểu {MIN_INTERVAL} giây).", ephemeral=True
                )
            updates["interval"] = secs
    if times is not None:
        updates["remaining"] = times

    if not updates:
        return await interaction.response.send_message("ℹ️ Bạn chưa điền gì để sửa.", ephemeral=True)

    # Tên cột lấy từ code ở trên (không phải từ người dùng) nên an toàn
    sql = "UPDATE reminders SET " + ", ".join(f"{k} = ?" for k in updates) + " WHERE id = ?"
    db(sql, (*updates.values(), r["id"]))
    rows, _, _ = db("SELECT * FROM reminders WHERE id = ?", (r["id"],))
    await interaction.response.send_message(f"✏️ Đã cập nhật:\n{describe(rows[0])}", ephemeral=True)


edit.autocomplete("reminder_id")(id_autocomplete)


# ----------------------------------------------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ Chưa có DISCORD_TOKEN. Hãy tạo file .env (xem .env.example)")
    client.run(TOKEN)

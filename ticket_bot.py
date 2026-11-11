import asyncio
import io
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

import importlib
import importlib.util
import subprocess
import sys

if importlib.util.find_spec("disnake") is None:
    print("[Pulse] disnake не найдена; устанавливаю в текущее окружение Python...", flush=True)
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "--disable-pip-version-check",
                "--timeout",
                "120",
                "--retries",
                "5",
                "disnake==2.12.2",
            ],
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise SystemExit(
            "[Pulse] Не удалось установить disnake при запуске. "
            "Хостинг должен разрешать pip install или установить disnake "
            "во время Docker-сборки."
        ) from exc
    importlib.invalidate_caches()
    print("[Pulse] disnake установлена успешно.", flush=True)

import aiohttp
import disnake
from disnake.ext import commands


TOKEN = os.getenv("TICKET_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")

if not TOKEN:
    TOKEN_FILE = Path(__file__).resolve().with_name("ticket_token.txt")

    if TOKEN_FILE.exists():
        TOKEN = TOKEN_FILE.read_text(encoding="utf-8").strip() or None

GUILD_ID = int(
    os.getenv("GUILD_ID", "1526991373742379069") or 0
) or None

TICKET_CATEGORY_ID = int(
    os.getenv("TICKET_CATEGORY_ID", "1527046712349429982") or 0
) or None

CLOSED_CATEGORY_ID = int(
    os.getenv("TICKET_CLOSED_CATEGORY_ID", "1526991374950469653") or 0
) or None

VERIFICATION_CLOSED_CATEGORY_ID = int(
    os.getenv("TICKET_VERIFICATION_CLOSED_CATEGORY_ID", "1527046387907428422") or 0
) or None

DELETE_AFTER = int(os.getenv("TICKET_DELETE_AFTER", "0") or 0)

STAFF_ROLE_IDS = (
    1527390250748608562,
    1526991435834982550,
    1527269547181211768,
)

GW_ROLE_IDS = (
    1527269547181211768,
    1527329319544950784,
    1526991435834982550,
)

PING_ROLE_ID = 1527270088774914250

SUPPORT_PING_ROLE_ID = 1526993734816764046

GUEST_ROLE_IDS = (1527764392660176996,)

MEMBER_ROLE_IDS = (
    1544408786922381322,
    1527270088774914250,
    1527269369216761886,
    1527268176495378554,
    1526993847496736778,
    1527269112601116672,
    1527413008211640590,
    1527269170566266891,
    1526993541765660732,
    1526993734816764046,
    1526993470638522588,
)

ALLY_EXTRA_ROLE_IDS = (1527764675205533807,)

REMOVE_ROLE_IDS = (1527334034818990161,)

KIND_NAMES = {
    "member": "Member",
    "guest": "Guest",
    "ally": "Ally Member",
}


TRYOUT_CATEGORY_ID = 1527219102773542994

TRYOUT_STAFF_ROLE_ID = 1527330169730105514
TRYOUT_LOG_GIF_URL = "https://static2.klipy.com/ii/c3a19a0b747a76e98651f2b9a3cca5ff/75/63/AnCODnnC.gif"
TRYOUT_LOG_CHANNEL_ID = 1527664670532567123

TRYOUT_UNRANKED_ROLE_ID = 1527413008211640590

TRYOUT_TIERS = {
    "S tier": 1551560699224793198,
    "S- tier": 1551560520199438486,
    "A+ tier": 1551560540684550165,
    "A tier": 1551560578194219038,
    "B+ tier": 1551560599866048683,
    "B tier": 1551560623811338251,
    "B- tier": 1551560642354487296,
}

TRYOUT_COOLDOWN_SECONDS = 7 * 24 * 60 * 60


DATA_DIR = Path(
    os.getenv("DATA_DIR") or Path(__file__).resolve().parent
)
DATA_DIR.mkdir(parents=True, exist_ok=True)


def data_file(name):
    target = DATA_DIR / name
    legacy = Path(__file__).resolve().with_name(name)

    if (
        not target.exists()
        and legacy.exists()
        and legacy.resolve() != target.resolve()
    ):
        try:
            shutil.copy2(legacy, target)
            logging.info("Данные перенесены: %s -> %s", legacy, target)

        except OSError:
            logging.exception("Не удалось перенести %s", legacy)

    return target


DB_PATH = data_file("tickets.sqlite3")

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS tickets (
    channel_id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS tickets_open
    ON tickets(guild_id, user_id, state);

CREATE TABLE IF NOT EXISTS ticket_counter (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    value INTEGER NOT NULL
);
""")

COLUMNS = {
    row["name"]
    for row in db.execute("PRAGMA table_info(tickets)")
}

if "number" not in COLUMNS:
    db.execute("ALTER TABLE tickets ADD COLUMN number INTEGER")

counter = db.execute(
    """
    SELECT MAX(
        COALESCE((SELECT MAX(number) FROM tickets), 0),
        COALESCE((SELECT value FROM ticket_counter WHERE id = 1), 0)
    )
    """
).fetchone()[0]

for offset, row in enumerate(
    db.execute(
        """
        SELECT channel_id
        FROM tickets
        WHERE number IS NULL
        ORDER BY created_at, channel_id
        """
    ).fetchall(),
    start=counter + 1,
):
    db.execute(
        "UPDATE tickets SET number=? WHERE channel_id=?",
        (offset, row["channel_id"]),
    )
    counter = offset

db.execute(
    """
    INSERT INTO ticket_counter(id, value) VALUES(1, ?)
    ON CONFLICT(id) DO UPDATE SET value=excluded.value
    """,
    (counter,),
)
db.execute("UPDATE tickets SET state='open' WHERE state='processing'")
db.commit()


def next_ticket_number():
    with db:
        db.execute(
            "UPDATE ticket_counter SET value = value + 1 WHERE id = 1"
        )
        return db.execute(
            "SELECT value FROM ticket_counter WHERE id = 1"
        ).fetchone()[0]


def free_ticket_number(number):
    with db:
        db.execute(
            "UPDATE ticket_counter SET value=? WHERE id=1 AND value=?",
            (number - 1, number),
        )


ticket_lock = asyncio.Lock()

db.executescript("""
CREATE TABLE IF NOT EXISTS tryouts (
    channel_id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL,
    tier_name TEXT,
    moderator_id INTEGER,
    loadout TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tryouts_lookup
    ON tryouts(guild_id, user_id, created_at);
CREATE TABLE IF NOT EXISTS tryout_counter (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    value INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO tryout_counter(id, value) VALUES(1, 0);
UPDATE tryout_counter SET value = (
    SELECT MAX(COALESCE(MAX(number), 0),
               (SELECT value FROM tryout_counter WHERE id = 1))
    FROM tryouts
) WHERE id = 1;
""")
tryout_columns = {
    row["name"] for row in db.execute("PRAGMA table_info(tryouts)")
}
if "loadout" not in tryout_columns:
    db.execute("ALTER TABLE tryouts ADD COLUMN loadout TEXT NOT NULL DEFAULT ''")

db.execute("""
    CREATE TABLE IF NOT EXISTS tryout_cooldowns (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        last_created_at INTEGER NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )
""")

for entry in db.execute(
    """SELECT guild_id, user_id, MAX(created_at) AS last_created_at
       FROM tryouts GROUP BY guild_id, user_id"""
).fetchall():
    db.execute(
        """INSERT INTO tryout_cooldowns(guild_id, user_id, last_created_at)
           VALUES (?, ?, ?)
           ON CONFLICT(guild_id, user_id) DO UPDATE SET
               last_created_at = MAX(
                   tryout_cooldowns.last_created_at,
                   excluded.last_created_at
               )""",
        (entry["guild_id"], entry["user_id"], entry["last_created_at"]),
    )
db.commit()
tryout_lock = asyncio.Lock()


INTENTS = disnake.Intents.default()
INTENTS.members = True

BACKGROUND_TASKS = set()


def run_background(coro):
    task = asyncio.create_task(coro)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return task


COMMAND_SYNC = commands.CommandSyncFlags.default()
COMMAND_SYNC.sync_commands_debug = True
COMMAND_SYNC.sync_guild_commands = True
COMMAND_SYNC.allow_command_deletion = True

bot = commands.InteractionBot(
    intents=INTENTS,
    test_guilds=[GUILD_ID] if GUILD_ID else None,
    allowed_mentions=disnake.AllowedMentions.none(),
    command_sync_flags=COMMAND_SYNC,
)


def is_staff(inter):
    if inter.guild is None:
        return False

    member = inter.author

    return (
        member.guild_permissions.manage_guild
        or any(
            role.id in STAFF_ROLE_IDS
            for role in getattr(member, "roles", [])
        )
    )


def has_staff_role(member):
    return any(
        role.id in STAFF_ROLE_IDS
        for role in getattr(member, "roles", [])
    )


def has_gw_role(member):
    return any(
        role.id in GW_ROLE_IDS
        for role in getattr(member, "roles", [])
    )


async def require_staff(inter):
    if is_staff(inter):
        return True

    if not inter.response.is_done():
        await inter.response.send_message(
            "Доступно только стаффу.",
            ephemeral=True,
        )

    return False


def role_ids_for(kind):
    if kind == "guest":
        add_ids = list(GUEST_ROLE_IDS)

    elif kind == "ally":
        add_ids = list(dict.fromkeys(MEMBER_ROLE_IDS + ALLY_EXTRA_ROLE_IDS))

    else:
        add_ids = list(MEMBER_ROLE_IDS)

    return add_ids, list(REMOVE_ROLE_IDS)


FORMS = {
    "member": {
        "title": "Заявка — Member",
        "fields": (
            ("roblox", "What's your username on Roblox?", "roblox_user3242", True, False),
            ("gangs", "Are you in the gangs?", "Yes/No", True, False),
            ("source", "How did you get on this server?", "?", True, False),
            ("rate", "What's your rate?", "D/C/B/A/S", True, False),
            ("device", "What device are you playing on?", "PC/Phone", True, False),
        ),
    },
    "guest": {
        "title": "Заявка — Guest",
        "fields": (
            ("reason", "Why do you need a guest?", "Например: хочу общаться в чате", True, True),
            ("active", "Will you be active in the chat?", "Yes/No", True, False),
        ),
    },
    "ally": {
        "title": "Заявка — Ally Member",
        "fields": (
            ("roblox", "What's your username on Roblox?", "roblox_user3242", True, False),
            ("wars", "Will you be going to the Wars?", "Yes/No", True, False),
            ("active", "Will you be active in the chat?", "Yes/No", False, False),
        ),
    },
}


class TicketModal(disnake.ui.Modal):
    def __init__(self, kind):
        self.kind = kind
        form = FORMS[kind]

        super().__init__(
            title=form["title"],
            timeout=900,
            components=[
                disnake.ui.Label(
                    label,
                    disnake.ui.TextInput(
                        custom_id=field_id,
                        placeholder=placeholder,
                        required=required,
                        max_length=500,
                        style=(
                            disnake.TextInputStyle.paragraph
                            if paragraph
                            else disnake.TextInputStyle.short
                        ),
                    ),
                )
                for field_id, label, placeholder, required, paragraph
                in form["fields"]
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        await create_ticket(inter, self.kind, inter.text_values)


BANNER_URL = (
    "https://static2.klipy.com/ii/"
    "d7aec6f6f171607374b2065c836f92f4/b4/d0/6oGlkELa.gif"
)
BANNER_FILENAME = "pulse_banner.gif"
BANNER_SIZE_LIMIT = 10 * 1024 * 1024

TICKET_GIF_NAME = "ticket"
LOCAL_GIF_EXTENSIONS = (".gif", ".mp4", ".webp", ".png", ".jpg", ".jpeg")


def find_local_gif(name):
    """Файл `name` с одним из допустимых расширений рядом с ботом."""
    for folder in (DATA_DIR, Path(__file__).resolve().parent):
        for extension in LOCAL_GIF_EXTENSIONS:
            local = folder / f"{name}{extension}"
            if local.is_file():
                return local
    return None


async def load_gif_banner():
    local = find_local_gif(TICKET_GIF_NAME)
    if local is not None:
        if local.stat().st_size <= BANNER_SIZE_LIMIT:
            return f"attachment://{local.name}", disnake.File(local, filename=local.name)
        logging.warning("%s больше 10 МБ, баннер берём по ссылке", local.name)

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(BANNER_URL) as response:
                response.raise_for_status()
                data = await response.content.read(BANNER_SIZE_LIMIT + 1)

        if len(data) > BANNER_SIZE_LIMIT:
            raise ValueError("GIF превышает лимит размера 10 МБ")
        if not data.startswith((b"GIF87a", b"GIF89a")):
            raise ValueError("Вместо GIF сервер вернул другой формат")

        file = disnake.File(io.BytesIO(data), filename=BANNER_FILENAME)
        return f"attachment://{BANNER_FILENAME}", file
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as exc:
        logging.warning("Не удалось прикрепить GIF, использую URL: %s", exc)
        return BANNER_URL, None


def make_panel(banner_url=BANNER_URL):
    return disnake.ui.Container(
        disnake.ui.MediaGallery(
            disnake.MediaGalleryItem(banner_url)
        ),
        disnake.ui.TextDisplay(
            "## 👋 Путь в Pulse gang начинается здесь!"
        ),
        disnake.ui.TextDisplay(
            "> Заявка создаёт отдельный канал, который видят "
            "только вы и стафф.\n"
            "> Выберите тип заявки ниже и заполните форму."
        ),
        disnake.ui.Separator(
            divider=False,
            spacing=disnake.SeparatorSpacing.large,
        ),
        disnake.ui.TextDisplay("**• TICKET**"),
        disnake.ui.ActionRow(
            disnake.ui.StringSelect(
                custom_id="ticket:create",
                placeholder="🎟️| 𝑇𝐼𝐶𝐾𝐸𝑇",
                min_values=1,
                max_values=1,
                options=[
                    disnake.SelectOption(label="〔🦇〕𝑀𝑒𝑚𝑏𝑒𝑟", value="member"),
                    disnake.SelectOption(label="〔👻〕𝐺𝑢𝑒𝑠𝑡", value="guest"),
                    disnake.SelectOption(label="〔🐍〕𝐴𝑙𝑙𝑦 𝑀𝑒𝑚𝑏𝑒𝑟", value="ally"),
                ],
            )
        ),
    )


PANEL_KWARGS = {"guild_ids": [GUILD_ID]} if GUILD_ID else {}


async def post_panel(inter):
    if not await require_staff(inter):
        return

    await inter.response.defer(ephemeral=True, with_message=True)

    try:
        banner_url, banner_file = await load_gif_banner()
        kwargs = {"components": [make_panel(banner_url)]}
        if banner_file is not None:
            kwargs["file"] = banner_file
        await inter.channel.send(**kwargs)

        text = "✅ Панель тикетов опубликована."
        local = find_local_gif(TICKET_GIF_NAME)
        if local is None:
            text += (
                f"\n⚠️ Файл `{TICKET_GIF_NAME}.gif` не найден рядом с ботом, "
                "баннер взят по старой ссылке и может не отображаться."
            )
        elif banner_url != f"attachment://{local.name}":
            text += (
                f"\n⚠️ Файл `{local.name}` больше 10 МБ, "
                "баннер взят по старой ссылке и может не отображаться."
            )
        await inter.edit_original_response(content=text)

    except disnake.HTTPException:
        logging.exception("Не удалось опубликовать панель тикетов")
        await inter.edit_original_response(
            content="Не удалось опубликовать панель. Проверьте права бота "
            "в этом канале."
        )


@bot.slash_command(
    name="ticket",
    description="Опубликовать панель тикетов",
    default_member_permissions=None,
    **PANEL_KWARGS,
)
async def ticket_command(inter: disnake.ApplicationCommandInteraction):
    await post_panel(inter)


@bot.slash_command(
    name="ticket-panel",
    description="Опубликовать панель тикетов",
    default_member_permissions=None,
    **PANEL_KWARGS,
)
async def ticket_panel(inter: disnake.ApplicationCommandInteraction):
    await post_panel(inter)


@bot.listen("on_dropdown")
async def ticket_dropdown(inter: disnake.MessageInteraction):
    if (inter.data.custom_id or "") != "ticket:create":
        return

    if inter.guild is None:
        await inter.response.send_message(
            "Доступно только на сервере.",
            ephemeral=True,
        )
        return

    kind = inter.values[0] if inter.values else ""

    if kind not in FORMS:
        await inter.response.send_message(
            "Неизвестный тип заявки.",
            ephemeral=True,
        )
        return

    await inter.response.send_modal(TicketModal(kind))


def ticket_overwrites(guild, applicant):
    overwrites = {
        guild.default_role: disnake.PermissionOverwrite(
            view_channel=False,
        ),
        applicant: disnake.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
        guild.me: disnake.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
            manage_channels=True,
            attach_files=True,
            embed_links=True,
        ),
    }

    for role_id in STAFF_ROLE_IDS:
        role = guild.get_role(role_id)

        if role is not None:
            overwrites[role] = disnake.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                manage_messages=True,
            )

    return overwrites


async def create_ticket(inter, kind, answers):
    if inter.guild is None:
        await inter.response.send_message(
            "Форма доступна только на сервере.",
            ephemeral=True,
        )
        return

    await inter.response.defer(ephemeral=True, with_message=True)

    guild = inter.guild
    applicant = inter.author

    async with ticket_lock:
        existing = db.execute(
            """
            SELECT channel_id
            FROM tickets
            WHERE guild_id=? AND user_id=? AND state IN ('open', 'processing')
            """,
            (guild.id, applicant.id),
        ).fetchone()

        if existing:
            channel = guild.get_channel(existing["channel_id"])

            if channel is not None:
                await inter.edit_original_response(
                    content=(
                        "У вас уже есть открытая заявка: "
                        f"{channel.mention}. Дождитесь решения стаффа."
                    )
                )
                return

            with db:
                db.execute(
                    "UPDATE tickets SET state='closed' WHERE channel_id=?",
                    (existing["channel_id"],),
                )

        category = None

        if TICKET_CATEGORY_ID:
            candidate = guild.get_channel(TICKET_CATEGORY_ID)

            if isinstance(candidate, disnake.CategoryChannel):
                category = candidate

        if category is None:
            category = getattr(inter.channel, "category", None)

        number = next_ticket_number()

        try:
            channel = await guild.create_text_channel(
                name=f"ticket {number}",
                category=category,
                overwrites=ticket_overwrites(guild, applicant),
                reason=f"Заявка {KIND_NAMES[kind]}: {applicant.id}",
            )

        except disnake.Forbidden:
            free_ticket_number(number)
            logging.exception("Нет прав на создание канала тикета")
            await inter.edit_original_response(
                content=(
                    "Боту не хватает прав на создание канала. "
                    "Нужны «Управлять каналами» и доступ к категории."
                )
            )
            return

        except disnake.HTTPException:
            free_ticket_number(number)
            logging.exception("Не удалось создать канал тикета")
            await inter.edit_original_response(
                content="Не удалось создать канал заявки. Попробуйте позже."
            )
            return

        embed = disnake.Embed(
            title=f"Заявка — {KIND_NAMES[kind]}",
            description=(
                f"Заявитель: {applicant.mention}\n"
                f"ID: `{applicant.id}`"
            ),
            colour=0x000000,
            timestamp=disnake.utils.utcnow(),
        )

        embed.set_thumbnail(url=applicant.display_avatar.url)
        embed.set_footer(
            text=f"Тикет №{number} • {KIND_NAMES[kind]}"
        )

        for field_id, label, *_ in FORMS[kind]["fields"]:
            answer = (answers.get(field_id) or "").strip() or "—"
            if "\n" in answer or "\r" in answer:
                safe_answer = answer.replace("```", "`\u200b``")
                formatted_answer = f"```\n{safe_answer[:1016]}\n```"
            else:
                safe_answer = answer.replace("`", "ˋ")
                formatted_answer = f"`{safe_answer[:1022]}`"

            embed.add_field(
                name=label,
                value=formatted_answer,
                inline=False,
            )

        components = [
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Принять",
                    emoji="✅",
                    style=disnake.ButtonStyle.success,
                    custom_id="ticket:accept",
                ),
                disnake.ui.Button(
                    label="Отклонить",
                    emoji="❌",
                    style=disnake.ButtonStyle.danger,
                    custom_id="ticket:reject",
                ),
            )
        ]

        try:
            message = await channel.send(
                content=(
                    f"<@&{PING_ROLE_ID}> — новая заявка "
                    f"**{KIND_NAMES[kind]}** №{number} "
                    f"от {applicant.mention}"
                ),
                embed=embed,
                components=components,
                allowed_mentions=disnake.AllowedMentions(
                    roles=[disnake.Object(id=PING_ROLE_ID)],
                    users=[disnake.Object(id=applicant.id)],
                    everyone=False,
                    replied_user=False,
                ),
            )

        except disnake.HTTPException:
            free_ticket_number(number)
            logging.exception("Не удалось отправить карточку заявки")

            try:
                await channel.delete(reason="Тикет не создан")
            except disnake.HTTPException:
                pass

            await inter.edit_original_response(
                content="Не удалось отправить заявку в канал. Проверьте права бота."
            )
            return

        with db:
            db.execute(
                """
                INSERT INTO tickets(
                    channel_id, message_id, guild_id,
                    user_id, kind, state, created_at, number
                )
                VALUES(?,?,?,?,?,'open',?,?)
                """,
                (
                    channel.id,
                    message.id,
                    guild.id,
                    applicant.id,
                    kind,
                    int(time.time()),
                    number,
                ),
            )

    await inter.edit_original_response(
        content=f"✅ Заявка создана: {channel.mention}"
    )


def open_ticket(channel_id):
    return db.execute(
        """
        SELECT *
        FROM tickets
        WHERE channel_id=? AND state='open'
        """,
        (channel_id,),
    ).fetchone()


def claim_ticket(channel_id):
    with db:
        cursor = db.execute(
            "UPDATE tickets SET state='processing' "
            "WHERE channel_id=? AND state='open'",
            (channel_id,),
        )

    if cursor.rowcount != 1:
        return None

    return db.execute(
        "SELECT * FROM tickets WHERE channel_id=?",
        (channel_id,),
    ).fetchone()


def set_ticket_state(channel_id, state):
    with db:
        db.execute(
            "UPDATE tickets SET state=? WHERE channel_id=?",
            (state, channel_id),
        )


async def apply_roles(guild, member, kind):
    add_ids, remove_ids = role_ids_for(kind)

    # Снимаемые роли не обязаны существовать: удалённую роль просто нечего снимать.
    missing = [
        str(role_id)
        for role_id in add_ids
        if guild.get_role(role_id) is None
    ]

    if missing:
        raise ValueError(
            "На сервере не найдены роли: " + ", ".join(missing)
        )

    if not guild.me.guild_permissions.manage_roles:
        raise ValueError("Боту нужно право «Управлять ролями».")

    add = [
        role
        for role in (guild.get_role(role_id) for role_id in add_ids)
        if role not in member.roles
    ]
    blocked = [role.name for role in add if not role.is_assignable()]

    if blocked:
        raise ValueError(
            "Бот не может выдать роли (поднимите роль бота выше): "
            + ", ".join(blocked)
        )

    remove = [
        role
        for role in (guild.get_role(role_id) for role_id in remove_ids)
        if role in member.roles
    ]

    problems = []
    blocked_remove = [
        role.name for role in remove if not role.is_assignable()
    ]

    if blocked_remove:
        problems.append(
            "не удалось снять роли: " + ", ".join(blocked_remove)
        )
        remove = [role for role in remove if role.is_assignable()]

    reason = f"Принятие заявки {KIND_NAMES[kind]}"

    if add:
        await member.add_roles(*add, atomic=True, reason=reason)

    if remove:
        try:
            await member.remove_roles(*remove, atomic=True, reason=reason)

        except disnake.HTTPException:
            logging.exception("Не удалось снять роли при принятии")
            problems.append("Discord не дал снять роль")
            remove = []

    return add, remove, problems


async def delete_later(channel, seconds):
    if seconds <= 0:
        return

    await asyncio.sleep(seconds)

    try:
        await channel.delete(reason="Заявка закрыта")

    except disnake.HTTPException:
        logging.warning(
            "Не удалось удалить канал тикета %s",
            channel.id,
        )


async def close_channel(channel, row):
    guild = channel.guild
    problems = []

    closed_category = None

    if VERIFICATION_CLOSED_CATEGORY_ID:
        candidate = guild.get_channel(VERIFICATION_CLOSED_CATEGORY_ID)

        if isinstance(candidate, disnake.CategoryChannel):
            closed_category = candidate
        else:
            problems.append("категория закрытых тикетов не найдена")

    overwrites = {
        target: overwrite
        for target, overwrite in channel.overwrites.items()
        if getattr(target, "id", None) != row["user_id"]
    }

    payload = {
        "name": f"closed {row['number']}",
        "overwrites": overwrites,
        "reason": "Заявка закрыта",
    }

    if closed_category is not None:
        payload["category"] = closed_category

    try:
        await channel.edit(**payload)

    except disnake.HTTPException:
        logging.exception(
            "Не удалось закрыть канал тикета %s",
            channel.id,
        )
        problems.append("не удалось перенести канал")

    return problems


async def finish_ticket(inter, row, title, colour, description):
    channel = inter.channel

    try:
        message = await channel.fetch_message(row["message_id"])
        await message.edit(components=[])

    except (disnake.NotFound, disnake.HTTPException):
        logging.warning(
            "Не удалось обновить сообщение тикета %s",
            row["channel_id"],
        )

    embed = disnake.Embed(
        title=title,
        description=description,
        colour=colour,
        timestamp=disnake.utils.utcnow(),
    )
    embed.set_footer(
        text=f"Тикет №{row['number']} • {KIND_NAMES[row['kind']]}"
    )

    try:
        await channel.send(embed=embed)

    except disnake.HTTPException:
        logging.exception("Не удалось отправить итог тикета")

    problems = await close_channel(channel, row)

    text = (
        f"✅ Готово. Тикет №{row['number']} закрыт и переименован "
        f"в «closed {row['number']}». Заявитель больше не видит канал."
    )

    if problems:
        text += "\n⚠️ " + "; ".join(problems)

    if DELETE_AFTER > 0:
        text += f"\nКанал будет удалён через {DELETE_AFTER} сек."
        run_background(delete_later(channel, DELETE_AFTER))

    await inter.edit_original_response(content=text)


async def accept_ticket(inter):
    await inter.response.defer(ephemeral=True, with_message=True)

    row = claim_ticket(inter.channel.id)

    if row is None:
        await inter.edit_original_response(
            content="Эта заявка уже обработана или не найдена."
        )
        return

    guild = inter.guild
    error_text = None

    try:
        member = guild.get_member(row["user_id"])

        if member is None:
            member = await guild.fetch_member(row["user_id"])

        added, removed, problems = await apply_roles(
            guild, member, row["kind"]
        )

    except disnake.NotFound:
        error_text = "Заявитель уже вышел с сервера — роли выдать нельзя."

    except ValueError as error:
        error_text = str(error)

    except disnake.Forbidden:
        logging.exception("Discord запретил выдачу ролей")
        error_text = (
            "Discord запретил выдачу ролей. Проверьте право "
            "«Управлять ролями» и положение роли бота."
        )

    except disnake.HTTPException:
        logging.exception("Ошибка выдачи ролей")
        error_text = "Не удалось выдать роли. Попробуйте ещё раз."

    except Exception:
        logging.exception("Непредвиденная ошибка при принятии заявки")
        error_text = "Непредвиденная ошибка. Подробности в логах бота."

    if error_text is not None:
        set_ticket_state(row["channel_id"], "open")
        await inter.edit_original_response(content=error_text)
        return

    set_ticket_state(row["channel_id"], "accepted")

    lines = [
        f"Заявка **{KIND_NAMES[row['kind']]}** от <@{row['user_id']}> принята.",
        f"Решение: {inter.author.mention}",
    ]

    if added:
        lines.append(
            "Выданы роли: " + ", ".join(role.mention for role in added)
        )

    if removed:
        lines.append(
            "Сняты роли: " + ", ".join(role.mention for role in removed)
        )

    if problems:
        lines.append("⚠️ " + "; ".join(problems))

    await finish_ticket(
        inter,
        row,
        "✅ Заявка принята",
        0x57F287,
        "\n".join(lines),
    )


class RejectModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(
            title="Отклонить заявку",
            timeout=600,
            components=[
                disnake.ui.Label(
                    "Причина отказа",
                    disnake.ui.TextInput(
                        custom_id="reason",
                        placeholder="Например: не подходит по требованиям",
                        required=False,
                        max_length=500,
                        style=disnake.TextInputStyle.paragraph,
                    ),
                )
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        if inter.guild is None:
            await inter.response.send_message(
                "Доступно только на сервере.",
                ephemeral=True,
            )
            return

        await inter.response.defer(ephemeral=True, with_message=True)

        row = claim_ticket(inter.channel.id)

        if row is None:
            await inter.edit_original_response(
                content="Эта заявка уже обработана или не найдена."
            )
            return

        reason = (inter.text_values.get("reason") or "").strip()

        set_ticket_state(row["channel_id"], "rejected")

        description = (
            f"Заявка **{KIND_NAMES[row['kind']]}** от <@{row['user_id']}> отклонена.\n"
            f"Решение: {inter.author.mention}"
        )

        if reason:
            description += f"\nПричина: {reason}"

        await finish_ticket(
            inter,
            row,
            "❌ Заявка отклонена",
            0xED4245,
            description,
        )


@bot.listen("on_button_click")
async def ticket_buttons(inter: disnake.MessageInteraction):
    custom_id = inter.data.custom_id or ""

    if custom_id not in ("ticket:accept", "ticket:reject"):
        return

    if inter.guild is None:
        await inter.response.send_message(
            "Доступно только на сервере.",
            ephemeral=True,
        )
        return

    if not await require_staff(inter):
        return

    if custom_id == "ticket:accept":
        await accept_ticket(inter)
        return

    row = open_ticket(inter.channel.id)

    if row is None:
        await inter.response.send_message(
            "Эта заявка уже обработана или не найдена.",
            ephemeral=True,
        )
        return

    await inter.response.send_modal(RejectModal())


def is_tryout_staff(inter):
    return (
        inter.guild is not None
        and any(
            role.id == TRYOUT_STAFF_ROLE_ID
            for role in getattr(inter.author, "roles", ())
        )
    )


async def require_tryout_staff(inter):
    if is_tryout_staff(inter):
        return True

    if not inter.response.is_done():
        await inter.response.send_message(
            f"⛔ Только роль <@&{TRYOUT_STAFF_ROLE_ID}> может рассматривать траяуты.",
            ephemeral=True,
        )
    return False


def make_tryout_panel():
    embed = disnake.Embed(
        title="🎟️ › TryoutTicket",
        description=(
            "To create a ticket use the **Create** ticket button.\n\n"
            "**КАЖДУЮ НЕДЕЛЮ МОЖНО ДЕЛАТЬ ПОВТОРНЫЙ ТРАЯУТ**\n\n"
            "✧ **Траяут** — это обязательный PvP с траяутером на **3 раунда**, "
            "после которого ваш ранг назначается на основе навыков.\n\n"
            "*Повторная запись доступна через 7 дней после предыдущего траяута.*"
        ),
        colour=0x57F287,
    )
    embed.set_footer(text="Pulse • TryoutTicket")

    components = [
        disnake.ui.ActionRow(
            disnake.ui.Button(
                label="Create",
                emoji="✅",
                style=disnake.ButtonStyle.success,
                custom_id="tryout:create",
            )
        )
    ]
    return embed, components


@bot.slash_command(
    name="tryout",
    description="Опубликовать панель для записи на траяут",
    default_member_permissions=None,
    **PANEL_KWARGS,
)
async def tryout_command(inter: disnake.ApplicationCommandInteraction):
    allowed = is_staff(inter) or is_tryout_staff(inter)
    await inter.response.defer(with_message=True, ephemeral=not allowed)

    if not allowed:
        await inter.edit_original_response(
            content="⛔ Опубликовать панель может только стафф или траяутер."
        )
        return

    try:
        embed, components = make_tryout_panel()
        await inter.edit_original_response(embed=embed, components=components)
    except disnake.HTTPException:
        logging.exception("Ошибка отправки панели /tryout")
        await inter.edit_original_response(
            content="Не удалось опубликовать панель траяута. Проверьте права бота."
        )


def current_tryout(channel_id):
    return db.execute(
        "SELECT * FROM tryouts WHERE channel_id=? AND state='open'",
        (channel_id,),
    ).fetchone()


def next_tryout_time(guild_id: int, user_id: int) -> int | None:
    entry = db.execute(
        """SELECT last_created_at
           FROM tryout_cooldowns
           WHERE guild_id = ? AND user_id = ?""",
        (int(guild_id), int(user_id)),
    ).fetchone()
    if entry is None:
        return None
    return int(entry["last_created_at"]) + TRYOUT_COOLDOWN_SECONDS


def tryout_is_on_cooldown(guild_id: int, user_id: int, now: int) -> tuple[bool, int | None]:
    ready_at = next_tryout_time(guild_id, user_id)
    return ready_at is not None and ready_at > now, ready_at


def tryout_overwrites(guild, applicant):
    role = guild.get_role(TRYOUT_STAFF_ROLE_ID)
    if role is None:
        raise ValueError(
            f"Роль траяутеров {TRYOUT_STAFF_ROLE_ID} не найдена на сервере."
        )

    return {
        guild.default_role: disnake.PermissionOverwrite(view_channel=False),
        applicant: disnake.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
        guild.me: disnake.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
            manage_channels=True,
            embed_links=True,
        ),
        role: disnake.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
    }


class TryoutApplicationModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(
            title="Please fill this out",
            timeout=900,
            components=[
                disnake.ui.Label(
                    "На какой связке ты будешь играть?",
                    disnake.ui.TextInput(
                        custom_id="loadout",
                        placeholder="Сп бокс и тд.",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        min_length=1,
                        max_length=250,
                    ),
                )
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        loadout = (inter.text_values.get("loadout") or "").strip()
        if not loadout:
            await inter.response.send_message(
                "Укажи, на какой связке будешь играть.",
                ephemeral=True,
            )
            return
        await create_tryout(inter, loadout)


async def create_tryout(inter, loadout: str):
    await inter.response.defer(ephemeral=True, with_message=True)
    guild = inter.guild
    applicant = inter.author
    now = int(time.time())

    async with tryout_lock:
        open_rows = db.execute(
            """SELECT channel_id FROM tryouts
               WHERE guild_id=? AND user_id=? AND state='open'
               ORDER BY created_at DESC""",
            (guild.id, applicant.id),
        ).fetchall()

        for old in open_rows:
            old_channel = guild.get_channel(old["channel_id"])
            if old_channel is not None:
                await inter.edit_original_response(
                    content=(
                        "У вас уже есть открытый траяут: "
                        f"{old_channel.mention}. Дождитесь решения траяутера."
                    )
                )
                return
            with db:
                db.execute(
                    "UPDATE tryouts SET state='deleted' WHERE channel_id=?",
                    (old["channel_id"],),
                )

        limited, ready_at = tryout_is_on_cooldown(guild.id, applicant.id, now)
        if limited:
            await inter.edit_original_response(
                content=(
                    "Вы уже подавали заявку на траяут за последние 7 дней. "
                    f"Следующая запись: <t:{ready_at}:F> (<t:{ready_at}:R>)."
                )
            )
            return

        category = guild.get_channel(TRYOUT_CATEGORY_ID)
        if not isinstance(category, disnake.CategoryChannel):
            await inter.edit_original_response(
                content=(
                    "Не найдена категория траяутов с ID "
                    f"`{TRYOUT_CATEGORY_ID}`. Проверьте настройки."
                )
            )
            return

        try:
            overwrites = tryout_overwrites(guild, applicant)
        except ValueError as error:
            await inter.edit_original_response(content=str(error))
            return

        number = db.execute(
            "SELECT value FROM tryout_counter WHERE id=1"
        ).fetchone()[0] + 1

        try:
            channel = await guild.create_text_channel(
                name=f"tryout-{number}",
                category=category,
                overwrites=overwrites,
                reason=f"Траяут #{number}: {applicant.id}",
            )
        except disnake.HTTPException:
            logging.exception("Не удалось создать канал траяута")
            await inter.edit_original_response(
                content="Не удалось создать канал. Проверьте права бота в категории траяутов."
            )
            return

        embed = disnake.Embed(
            title=f"🎟️ Траяут №{number}",
            description=(
                f"**Кандидат:** {applicant.mention}\n"
                f"**ID:** `{applicant.id}`\n\n"
                "**Траяут:** PvP с траяутером на 3 раунда.\n"
                "После проведения траяутер нажимает **Принять**, "
                "выбирает тир, и бот заменяет старый тир новым."
            ),
            colour=0x57F287,
        )
        embed.add_field(
            name="На какой связке ты будешь играть?",
            value=disnake.utils.escape_mentions(loadout),
            inline=False,
        )
        embed.set_thumbnail(url=applicant.display_avatar.url)
        embed.set_footer(text=f"Pulse • Tryout №{number}")

        buttons = [
            disnake.ui.ActionRow(
                disnake.ui.Button(
                    label="Принять / Выбрать тир",
                    emoji="✅",
                    style=disnake.ButtonStyle.success,
                    custom_id="tryout:accept",
                ),
                disnake.ui.Button(
                    label="Отклонить",
                    emoji="❌",
                    style=disnake.ButtonStyle.danger,
                    custom_id="tryout:reject",
                ),
            )
        ]

        try:
            msg = await channel.send(
                content=(
                    f"<@&{TRYOUT_STAFF_ROLE_ID}> — новый траяут "
                    f"№{number} от {applicant.mention}"
                ),
                embed=embed,
                components=buttons,
                allowed_mentions=disnake.AllowedMentions(
                    roles=[disnake.Object(id=TRYOUT_STAFF_ROLE_ID)],
                    users=[disnake.Object(id=applicant.id)],
                    everyone=False,
                ),
            )
        except disnake.HTTPException:
            logging.exception("Не удалось отправить сообщение траяута")
            try:
                await channel.delete(reason="Не удалось опубликовать траяут")
            except disnake.HTTPException:
                logging.warning("Не удалось удалить пустой канал %s", channel.id)
            await inter.edit_original_response(
                content="Канал создан, но не удалось опубликовать заявку. Проверьте права бота."
            )
            return

        with db:
            db.execute(
                "UPDATE tryout_counter SET value=? WHERE id=1", (number,)
            )
            db.execute(
                """INSERT INTO tryouts(
                    channel_id, message_id, guild_id, user_id,
                    number, state, created_at, loadout
                ) VALUES(?,?,?,?,?,'open',?,?)""",
                (channel.id, msg.id, guild.id, applicant.id, number, now, loadout),
            )
            db.execute(
                """INSERT INTO tryout_cooldowns(guild_id, user_id, last_created_at)
                   VALUES(?, ?, ?)
                   ON CONFLICT(guild_id, user_id) DO UPDATE SET
                       last_created_at=excluded.last_created_at""",
                (guild.id, applicant.id, now),
            )

    await inter.edit_original_response(
        content=f"✅ Траяут №{number} открыт: {channel.mention}"
    )


async def replace_tryout_tier(guild, member, tier_name, moderator):
    new_role_id = TRYOUT_TIERS[tier_name]
    new_role = guild.get_role(new_role_id)
    if new_role is None:
        raise ValueError(f"На сервере нет роли для {tier_name}: `{new_role_id}`.")
    if not guild.me.guild_permissions.manage_roles:
        raise ValueError("Боту требуется разрешение «Управлять ролями».")

    tier_role_ids = set(TRYOUT_TIERS.values())
    old_roles = [
        role for role in member.roles
        if role.id in tier_role_ids or role.id == TRYOUT_UNRANKED_ROLE_ID
    ]

    changed_roles = [role for role in old_roles if role.id != new_role_id]
    if new_role not in member.roles:
        changed_roles.append(new_role)

    inaccessible = [role.name for role in changed_roles if not role.is_assignable()]
    if inaccessible:
        raise ValueError(
            "Бот не может изменить роли: " + ", ".join(inaccessible)
            + ". Переместите роль бота выше указанных ролей."
        )

    new_roles = [
        role for role in member.roles
        if role.id not in tier_role_ids
        and role.id != TRYOUT_UNRANKED_ROLE_ID
        and role.id != guild.default_role.id
    ]
    new_roles.append(new_role)
    await member.edit(
        roles=new_roles,
        reason=f"Траяут {tier_name}; назначил {moderator.id}",
    )
    return old_roles, new_role


async def finish_tryout(inter, row, *, accepted, tier_name=None, count=None):
    channel = inter.channel

    with db:
        db.execute(
            """UPDATE tryouts
               SET state=?, tier_name=?, moderator_id=?
               WHERE channel_id=? AND state='open'""",
            (
                "accepted" if accepted else "rejected",
                tier_name,
                inter.author.id,
                row["channel_id"],
            ),
        )

    try:
        source_message = await channel.fetch_message(row["message_id"])
        await source_message.edit(components=[])
    except disnake.HTTPException:
        logging.exception("Не удалось отключить кнопки траяута %s", row["number"])

    if accepted:
        result_embed = disnake.Embed(
            title=f"🩸 Tryout Log No{row['number']}",
            description=(
                f"> Tryoutee: <@{row['user_id']}>\n"
                f"> Tryouter: {inter.author.mention}\n"
                f"> Count : **{count}**\n"
                f"> Rank : **{tier_name}**"
            ),
            colour=0x2B2D31,
        )
        result_embed.set_image(url=TRYOUT_LOG_GIF_URL)
        result_embed.set_footer(text=f"Tryout Log No{row['number']}")
    else:
        result_embed = disnake.Embed(
            title=f"❌ Траяут №{row['number']} отклонён",
            description=(
                f"Игрок: <@{row['user_id']}>\n"
                f"Траяутер: {inter.author.mention}"
            ),
            colour=0xED4245,
        )
    try:
        log_channel = inter.guild.get_channel(TRYOUT_LOG_CHANNEL_ID)
        if log_channel is None:
            log_channel = await bot.fetch_channel(TRYOUT_LOG_CHANNEL_ID)
        await log_channel.send(embed=result_embed)
    except (disnake.HTTPException, AttributeError):
        logging.exception(
            "Не удалось отправить лог траяута в канал %s",
            TRYOUT_LOG_CHANNEL_ID,
        )
    category = None
    if CLOSED_CATEGORY_ID:
        candidate = inter.guild.get_channel(CLOSED_CATEGORY_ID)
        if isinstance(candidate, disnake.CategoryChannel):
            category = candidate

    overwrites = {
        target: overwrite
        for target, overwrite in channel.overwrites.items()
        if getattr(target, "id", None) != row["user_id"]
    }
    kwargs = {
        "name": f"tryout-closed-{row['number']}",
        "overwrites": overwrites,
        "reason": "Траяут закрыт",
    }
    if category is not None:
        kwargs["category"] = category

    notes = []
    try:
        await channel.edit(**kwargs)
    except disnake.HTTPException:
        logging.exception("Не удалось закрыть канал траяута %s", row["number"])
        notes.append("⚠️ Не удалось убрать доступ заявителя или переместить канал")

    if DELETE_AFTER > 0:
        run_background(delete_later(channel, DELETE_AFTER))

    response = f"✅ Траяут №{row['number']} обработан."
    if accepted:
        response += f" Назначен тир **{tier_name}**."
    if notes:
        response += "\n" + "\n".join(notes)
    await inter.edit_original_response(content=response)


async def accept_tryout_tier(inter, channel_id, tier_name, count):
    await inter.response.defer(ephemeral=True, with_message=True)

    if not is_tryout_staff(inter):
        await inter.edit_original_response(
            content=f"⛔ Только роль <@&{TRYOUT_STAFF_ROLE_ID}> может выдавать тиры."
        )
        return

    if inter.channel_id != channel_id or tier_name not in TRYOUT_TIERS:
        await inter.edit_original_response(content="Недопустимый выбор тира.")
        return

    async with tryout_lock:
        row = current_tryout(channel_id)
        if row is None:
            await inter.edit_original_response(content="Траяут уже обработан.")
            return

        try:
            member = inter.guild.get_member(row["user_id"])
            if member is None:
                member = await inter.guild.fetch_member(row["user_id"])

            _, new_role = await replace_tryout_tier(
                inter.guild, member, tier_name, inter.author
            )
            await finish_tryout(
                inter, row, accepted=True, tier_name=tier_name, count=count
            )

        except ValueError as error:
            await inter.edit_original_response(content=f"❌ {error}")
        except disnake.NotFound:
            await inter.edit_original_response(
                content="❌ Игрок уже вышел с сервера — тир выдать нельзя."
            )
        except disnake.HTTPException:
            logging.exception("Не удалось назначить тир %s", tier_name)
            await inter.edit_original_response(
                content="❌ Не удалось изменить тир. Проверьте доступ к ролям и права бота."
            )
        except Exception:
            logging.exception("Непредвиденная ошибка выдачи тира")
            await inter.edit_original_response(
                content="❌ Ошибка выдачи тира. Подробности в логах бота."
            )


class TryoutCountModal(disnake.ui.Modal):
    def __init__(self, channel_id, tier_name):
        self.channel_id = channel_id
        self.tier_name = tier_name

        super().__init__(
            title="Tryout count",
            timeout=600,
            components=[
                disnake.ui.Label(
                    "Count:",
                    disnake.ui.TextInput(
                        custom_id="count",
                        placeholder="3-1 (sp box vs kcr box)",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        max_length=100,
                    ),
                )
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        count = disnake.utils.escape_mentions(
            (inter.text_values.get("count") or "").strip()
        )
        if not count:
            await inter.response.send_message(
                "Укажи счёт, например 3-1.", ephemeral=True
            )
            return

        await accept_tryout_tier(inter, self.channel_id, self.tier_name, count)


class TryoutRejectModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(
            title="Отклонение траяута",
            components=[
                disnake.ui.Label(
                    "Причина (необязательно)",
                    disnake.ui.TextInput(
                        custom_id="reason",
                        placeholder="Например: недостаточно навыков",
                        required=False,
                        max_length=400,
                        style=disnake.TextInputStyle.paragraph,
                    ),
                )
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        if not await require_tryout_staff(inter):
            return

        await inter.response.defer(ephemeral=True, with_message=True)
        async with tryout_lock:
            row = current_tryout(inter.channel_id)
            if row is None:
                await inter.edit_original_response(content="Траяут уже обработан.")
                return

            reason = (inter.text_values.get("reason") or "").strip()
            if reason:
                try:
                    await inter.channel.send(
                        embed=disnake.Embed(
                            title="Причина отказа по траяуту",
                            description=disnake.utils.escape_markdown(reason),
                            colour=0xED4245,
                        )
                    )
                except disnake.HTTPException:
                    logging.exception("Не удалось опубликовать причину отказа")

            await finish_tryout(inter, row, accepted=False)


@bot.listen("on_button_click")
async def tryout_buttons(inter: disnake.MessageInteraction):
    custom_id = inter.data.custom_id or ""
    if custom_id not in ("tryout:create", "tryout:accept", "tryout:reject"):
        return

    if inter.guild is None:
        await inter.response.send_message("Доступно только на сервере.", ephemeral=True)
        return

    if custom_id == "tryout:create":
        limited, ready_at = tryout_is_on_cooldown(
            inter.guild.id, inter.author.id, int(time.time())
        )
        if limited:
            await inter.response.send_message(
                "Вы уже делали траяут в последние 7 дней. "
                f"Повторно сможете подать заявку <t:{ready_at}:F> "
                f"(<t:{ready_at}:R>).",
                ephemeral=True,
            )
            return
        await inter.response.send_modal(TryoutApplicationModal())
        return

    if not await require_tryout_staff(inter):
        return

    row = current_tryout(inter.channel_id)
    if row is None:
        await inter.response.send_message(
            "Этот траяут уже обработан или не найден.", ephemeral=True
        )
        return

    if custom_id == "tryout:reject":
        await inter.response.send_modal(TryoutRejectModal())
        return

    options = [
        disnake.SelectOption(label=tier, value=tier)
        for tier in TRYOUT_TIERS
    ]
    await inter.response.send_message(
        "Выберите тир для игрока:",
        components=[
            disnake.ui.ActionRow(
                disnake.ui.StringSelect(
                    custom_id=f"tryout:tier:{inter.channel_id}:{inter.author.id}",
                    placeholder="Выдать тир",
                    options=options,
                    min_values=1,
                    max_values=1,
                )
            )
        ],
        ephemeral=True,
    )


@bot.listen("on_dropdown")
async def tryout_tier_dropdown(inter: disnake.MessageInteraction):
    custom_id = inter.data.custom_id or ""
    if not custom_id.startswith("tryout:tier:"):
        return

    parts = custom_id.split(":")
    if (
        len(parts) != 4
        or not parts[2].isdigit()
        or not parts[3].isdigit()
    ):
        await inter.response.send_message("Некорректное меню тиров.", ephemeral=True)
        return

    if inter.guild is None:
        await inter.response.send_message("Доступно только на сервере.", ephemeral=True)
        return

    if inter.author.id != int(parts[3]):
        await inter.response.send_message(
            "Это меню открыто другим траяутером.", ephemeral=True
        )
        return

    if not await require_tryout_staff(inter):
        return

    channel_id = int(parts[2])
    tier_name = inter.values[0] if inter.values else ""
    if inter.channel_id != channel_id or tier_name not in TRYOUT_TIERS:
        await inter.response.send_message("Недопустимый выбор тира.", ephemeral=True)
        return

    await inter.response.send_modal(TryoutCountModal(channel_id, tier_name))


SUPPORT_CHANNEL_ID = 1527030251446599680

SUPPORT_SEPARATOR = (
    "༺✿𓄂𓆩✦𓆪༺♛𓄂𓆩𓆪༺✥𓄂𓆩☆𓆪༺✬𓄂𓆩✿𓆪༺☆𓄂𓆪✧𓆜✥𓄂"
)


def support_answer(value: str) -> str:
    return disnake.utils.escape_mentions(
        " ".join(value.split())
    )


def render_help_fields(link: str, enemy: str, place: str) -> str:
    return (
        f"> Link: {link}\n"
        f"> Enemy: {enemy}\n"
        f"> Place: {place}"
    )


class SupportModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(
            title="NEED HELP",
            timeout=900,
            components=[
                disnake.ui.Label(
                    "Link",
                    disnake.ui.TextInput(
                        custom_id="server_link",
                        placeholder="roqol.io/....",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        min_length=1,
                        max_length=250,
                    ),
                ),
                disnake.ui.Label(
                    "Enemy",
                    disnake.ui.TextInput(
                        custom_id="fight",
                        placeholder="сп гер твох",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        min_length=1,
                        max_length=100,
                    ),
                ),
                disnake.ui.Label(
                    "Place",
                    disnake.ui.TextInput(
                        custom_id="place",
                        placeholder="мост",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        min_length=1,
                        max_length=100,
                    ),
                ),
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        if inter.guild is None:
            await inter.response.send_message(
                "Команда доступна только на сервере Discord.",
                ephemeral=True,
            )
            return

        if inter.channel_id != SUPPORT_CHANNEL_ID:
            await inter.response.send_message(
                f"Форму /supp можно отправлять только в канале <#{SUPPORT_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return

        link = support_answer(inter.text_values.get("server_link", ""))
        fight = support_answer(inter.text_values.get("fight", ""))
        place = support_answer(inter.text_values.get("place", ""))

        if not link or not fight or not place:
            await inter.response.send_message(
                "Заполни все три поля.", ephemeral=True,
            )
            return

        await inter.response.defer(ephemeral=True, with_message=True)

        message = (
            f"# {inter.author.mention} NEED HELP\n\n"
            f"{SUPPORT_SEPARATOR}\n"
            f"{render_help_fields(link, fight, place)}\n"
            f"{SUPPORT_SEPARATOR}\n\n"
            f"<@&{SUPPORT_PING_ROLE_ID}>"
        )

        try:
            await inter.channel.send(
                content=message,
                allowed_mentions=disnake.AllowedMentions(
                    users=[disnake.Object(id=inter.author.id)],
                    roles=[disnake.Object(id=SUPPORT_PING_ROLE_ID)],
                    everyone=False,
                    replied_user=False,
                ),
                suppress_embeds=True,
            )
        except disnake.HTTPException:
            logging.exception("Не удалось опубликовать сообщение /supp")
            await inter.edit_original_response(
                content="Не удалось отправить запрос. Проверь права бота в этом канале."
            )
            return

        await inter.edit_original_response(
            content="Запрос помощи опубликован в этом канале."
        )


@bot.slash_command(
    name="supp",
    description="Запросить помощь: ссылка, файт и место",
    **PANEL_KWARGS,
)
async def supp_command(inter: disnake.ApplicationCommandInteraction):
    if inter.guild is None:
        await inter.response.send_message(
            "Команда доступна только на сервере Discord.", ephemeral=True
        )
        return

    if inter.channel_id != SUPPORT_CHANNEL_ID:
        await inter.response.send_message(
            f"Команда /supp доступна только в канале <#{SUPPORT_CHANNEL_ID}>.",
            ephemeral=True,
        )
        return

    await inter.response.send_modal(SupportModal())


GW_TARGET_ROLE_ID = 1526993541765660732

GW_DM_INTERVAL_SECONDS = 0.8
GW_RUNNING_TASKS = set()


def gw_modal_checkbox():
    if hasattr(disnake.ui, "Checkbox"):
        return disnake.ui.Label(
            "Разослать участникам в ЛС",
            disnake.ui.Checkbox(
                custom_id="gw_broadcast",
                default=False,
            ),
        )

    return disnake.ui.Label(
        "Разослать участникам в ЛС",
        disnake.ui.StringSelect(
            custom_id="gw_broadcast",
            placeholder="Нужно ли отправлять в ЛС?",
            min_values=1,
            max_values=1,
            options=[
                disnake.SelectOption(
                    label="Нет — только сообщение в канале", value="no", default=True
                ),
                disnake.SelectOption(
                    label="Да — участникам с ролью", value="yes"
                ),
            ],
        ),
    )


class GWModal(disnake.ui.Modal):
    def __init__(self):
        super().__init__(
            title="GANG WAR JOIN EVERYONE",
            timeout=900,
            components=[
                disnake.ui.Label(
                    "Link",
                    disnake.ui.TextInput(
                        custom_id="server_link",
                        placeholder="roqol.io/....",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        min_length=1,
                        max_length=250,
                    ),
                ),
                disnake.ui.Label(
                    "Enemy",
                    disnake.ui.TextInput(
                        custom_id="fight",
                        placeholder="сп гер твох",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        min_length=1,
                        max_length=100,
                    ),
                ),
                disnake.ui.Label(
                    "Place",
                    disnake.ui.TextInput(
                        custom_id="place",
                        placeholder="мост",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        min_length=1,
                        max_length=100,
                    ),
                ),
                gw_modal_checkbox(),
            ],
        )

    async def callback(self, inter: disnake.ModalInteraction):
        if inter.guild is None:
            await inter.response.send_message(
                "Команда доступна только на сервере Discord.", ephemeral=True
            )
            return

        if not has_gw_role(inter.author):
            await inter.response.send_message(
                "Команда /gw доступна только указанным ролям.",
                ephemeral=True,
            )
            return

        link = support_answer(inter.text_values.get("server_link", ""))
        fight = support_answer(inter.text_values.get("fight", ""))
        place = support_answer(inter.text_values.get("place", ""))

        if not link or not fight or not place:
            await inter.response.send_message(
                "Заполни все три поля.", ephemeral=True
            )
            return

        selection = inter.resolved_values.get("gw_broadcast", False)
        if isinstance(selection, bool):
            broadcast = selection
        elif isinstance(selection, (list, tuple)):
            broadcast = "yes" in selection
        else:
            broadcast = selection == "yes"

        await inter.response.defer(ephemeral=True, with_message=True)

        announcement = (
            "# GANG WAR JOIN EVERYONE\n\n"
            f"{SUPPORT_SEPARATOR}\n"
            f"{render_help_fields(link, fight, place)}\n"
            f"{SUPPORT_SEPARATOR}\n\n"
            f"<@&{GW_TARGET_ROLE_ID}>"
        )
        try:
            posted = await inter.channel.send(
                content=announcement,
                allowed_mentions=disnake.AllowedMentions(
                    users=False,
                    roles=[disnake.Object(id=GW_TARGET_ROLE_ID)],
                    everyone=False,
                    replied_user=False,
                ),
                suppress_embeds=True,
            )
        except disnake.HTTPException:
            logging.exception("Не удалось опубликовать сообщение /gw")
            await inter.edit_original_response(
                content="Не удалось отправить /gw в канал. Проверь права бота."
            )
            return

        if not broadcast:
            await inter.edit_original_response(
                content=f"GW опубликован: {posted.jump_url}\nРассылка в ЛС выключена."
            )
            return

        dm_text = (
            "GANG WAR JOIN EVERYONE\n\n"
            f"{SUPPORT_SEPARATOR}\n"
            f"{render_help_fields(link, fight, place)}\n"
            f"{SUPPORT_SEPARATOR}\n"
            f"Сообщение на сервере: {posted.jump_url}"
        )
        task = asyncio.create_task(
            gw_send_to_members(inter.guild, dm_text, inter.author.id)
        )
        GW_RUNNING_TASKS.add(task)
        task.add_done_callback(GW_RUNNING_TASKS.discard)

        await inter.edit_original_response(
            content=(
                f"GW опубликован: {posted.jump_url}\n"
                f"Рассылка в ЛС участникам с ролью <@&{GW_TARGET_ROLE_ID}> запущена."
            )
        )


async def gw_send_to_members(guild, text, sender):
    sent = 0
    failed = 0
    skipped_bots = 0
    try:
        if not guild.chunked:
            await guild.chunk()

        role = guild.get_role(GW_TARGET_ROLE_ID)
        if role is None:
            logging.error("/gw: роль %s не найдена", GW_TARGET_ROLE_ID)
            return

        for member in list(role.members):
            if member.bot:
                skipped_bots += 1
                continue
            try:
                await member.send(
                    text,
                    allowed_mentions=disnake.AllowedMentions.none(),
                    suppress_embeds=True,
                )
                sent += 1
            except disnake.HTTPException:
                failed += 1
            await asyncio.sleep(GW_DM_INTERVAL_SECONDS)

    except Exception:
        logging.exception("/gw: ошибка массовой рассылки, отправлено %s", sent)
    finally:
        logging.info(
            "/gw: рассылка завершена. Организатор: %s, сервер: %s, "
            "отправлено: %s, закрытые ЛС/ошибки: %s, боты: %s",
            sender,
            guild.id,
            sent,
            failed,
            skipped_bots,
        )


@bot.slash_command(
    name="gw",
    description="GW — запрос помощи и рассылка участникам",
    **PANEL_KWARGS,
)
async def gw_command(inter: disnake.ApplicationCommandInteraction):
    if inter.guild is None:
        await inter.response.send_message(
            "Команда доступна только на сервере Discord.", ephemeral=True
        )
        return

    if not has_gw_role(inter.author):
        await inter.response.send_message(
            "Команда /gw доступна только указанным ролям.",
            ephemeral=True,
        )
        return

    await inter.response.send_modal(GWModal())


MASSLOG_ROLE_ID = 1526993470638522588

MS_ALLOWED_ROLE_IDS = (
    1526991435834982550,
    1527329319544950784,
    1541576139896782918,
    1527269547181211768,
)

MASSLOG_DECORATION = (
    "✬𓄂𓆩✿𓆪༺☆𓄂𓆪✧𓆜𓄂❀༻𓆪✥𓆜𓄂✬𓄂𓆩✿𓆪༺☆𓄂𓆪✧𓄂༺"
)


@bot.slash_command(
    name="ms",
    description="Опубликовать MASSLOG EVERYONE",
    **PANEL_KWARGS,
)
async def masslog_command(inter: disnake.ApplicationCommandInteraction):
    if inter.guild is None:
        await inter.response.send_message(
            "Команда доступна только на сервере Discord.",
            ephemeral=True,
        )
        return

    author_roles = {
        role.id for role in getattr(inter.author, "roles", [])
    }
    if not author_roles.intersection(MS_ALLOWED_ROLE_IDS):
        await inter.response.send_message(
            "Команда /ms доступна только участникам с разрешёнными ролями.",
            ephemeral=True,
        )
        return

    role = inter.guild.get_role(MASSLOG_ROLE_ID)
    if role is None:
        await inter.response.send_message(
            "Не найдена роль MASSLOG на этом сервере. Проверь ID роли.",
            ephemeral=True,
        )
        return

    content = (
        "# MASSLOG EVERYONE\n\n"
        f"{MASSLOG_DECORATION}\n\n"
        "> МАССЛОГ — ЭТО СИГНАЛ ВЫЙТИ С СЕРВЕРА\n\n"
        f"{MASSLOG_DECORATION}\n\n"
        f"{role.mention}"
    )

    try:
        await inter.response.send_message(
            content=content,
            allowed_mentions=disnake.AllowedMentions(
                users=False,
                roles=[disnake.Object(id=MASSLOG_ROLE_ID)],
                everyone=False,
                replied_user=False,
            ),
            suppress_embeds=True,
        )
    except disnake.HTTPException:
        logging.exception("Не удалось отправить MASSLOG через /ms")
        if not inter.response.is_done():
            await inter.response.send_message(
                "Не удалось отправить MASSLOG. Проверь права бота в этом канале.",
                ephemeral=True,
            )


RULES_GIF_URL = (
    "https://cdn.discordapp.com/attachments/1465749396233912392/"
    "1527960036666638376/dd6a1d77b4d288db.gif"
    "?ex=6ae99fac&is=6ae84e2c"
    "&hm=3a0247323e63146226b009c2f890e053aab570c8346258f9da6c22cccef49f1b&"
)
RULES_GIF_FILENAME = "rules.gif"

RULES_INTRO = (
    "Правила могут меняться только овнерами. Любое неправильное наказание "
    "с стороны администрации наказуемо, если вам выдали неправильное "
    "наказание (мут больше чем надо и тп) напишите об этом людям с ролью "
    "овнер, они помогут вам."
)

RULES = {
    "ru": {
        "header": "🚀 • ПРАВИЛА",
        "sections": (
            (
                "🕶️ • ЧАТ ПРАВИЛА — ЧП.",
                (
                    "☁️ 1.0 Нарушение: Спам от 4 до 6.\n"
                    "⏰ Наказание: Мут на 15 минут. После 2 мутов будет даваться варн. (Стакается)",

                    "❋ 1.1 Нарушение: Чрезмерный буллинг.\n"
                    "⏰ Наказание: Мут на 1-4 часа (На усмотрение стаффа). "
                    "После 2 мутов будет даваться варн. (Не стакается)",

                    "✄ 1.2 Нарушение: Запрещена порнуха любого вида. Видео, фото, гиф, стики.\n"
                    "⏰ Наказание: Мут на 1 час. После 2 мутов будет даваться варн. (Не стакается)",

                    "♛ 1.3 Нарушение: Распространение личной информации участников без их разрешения.\n"
                    "⏰ Наказание: Мут на 12-24 часа. Зависит от информации. (Не стакается)",

                    "☂ 1.4 Нарушение: В чат каналах запрещается пиар всякого инородного контента "
                    "по типу всяких сайтов, интернет-магазинов, дс-серверов, групп в контакте и "
                    "сообществ, групп в телеге и каналов в телеге, а также запрещается проводить "
                    "всякие сборы по типу \"Ой смотрите на эту кавайную стримершу давайте "
                    "задонатим ей на гарнитуру\".\n"
                    "⏰ Наказание: Мут на один день. После 2 мутов будет даваться варн. (Не стакается)",

                    "✝ 1.5 Нарушение: Клип союзников любого вида.\n"
                    "⏰ Наказание: Мут на 30-60 минут. (Не стакается)",

                    "⛓ 1.6 Нарушение: Фрипинг команды /supp и прочих команд, которые пингуют много людей.\n"
                    "⏰ Наказание: Мут на 3 часа. После 2 мутов будет даваться варн. (Стакается)",

                    "☯ 1.7 Нарушение: Бить союзников 1в2, 1в3 и тд.\n"
                    "⏰ Наказание: Варн либо мут на 1-5 часов. (Стакается)",
                ),
            ),
            (
                "🎙️ • ГОЛОСОВЫЕ ПРАВИЛА — ГП.",
                (
                    "❀ 1.0 Нарушение: Не спамить саундбордом.\n"
                    "⏰ Наказание: Мут на 30 минут. После 2 мутов будет даваться варн.\n"
                    "➕ Дополнение: Саундпад использовать можно, но спамить нельзя! (Не стакается)",

                    "✧ 1.1 Нарушение: В голосовых каналах запрещается пиар всякого инородного "
                    "контента по типу всяких сайтов, интернет-магазинов, дс-серверов, групп в "
                    "контакте и сообществ, групп в телеге и каналов в телеге, а также запрещается "
                    "проводить всякие сборы по типу \"Ой смотрите на эту кавайную стримершу "
                    "давайте задонатим ей на гарнитуру\".\n"
                    "⏰ Наказание: Мут на 1 день. После 2 мутов будет даваться варн. (Не стакается)",

                    "★ 1.2 Нарушение: Запрещается оглашать личную информацию об участниках в "
                    "голосовом канале. (Можно только с разрешения участника)\n"
                    "⏰ Наказание: Мут на 12-24 часа. Зависит от информации. (Не стакается)",
                ),
            ),
        ),
    },
    "en": {
        "header": "🚀 • RULES",
        "sections": (
            (
                "🕶️ • CHAT RULES — CR.",
                (
                    "☁️ 1.0 Violation: Spam from 4 to 6.\n"
                    "⏰ Punishment: Mute for 15 minutes. After 2 mutes, a warn will be issued. (Stacks)",

                    "❋ 1.1 Violation: Excessive bullying.\n"
                    "⏰ Punishment: Mute for 1-4 hours (at staff discretion). "
                    "After 2 mutes, a warn will be issued. (Does not stack)",

                    "✄ 1.2 Violation: Pornography of any kind is prohibited. Videos, photos, GIFs, stickers.\n"
                    "⏰ Punishment: Mute for 1 hour. After 2 mutes, a warn will be issued. (Does not stack)",

                    "♛ 1.3 Violation: Sharing members' personal information without their permission.\n"
                    "⏰ Punishment: Mute for 12-24 hours, depending on the information. (Does not stack)",

                    "☂ 1.4 Violation: In chat channels, it is prohibited to promote any outside content "
                    "such as websites, online stores, Discord servers, VK groups and communities, "
                    "Telegram groups and channels. It is also prohibited to hold any fundraisers like "
                    "\"Oh, look at this cute streamer, let's donate to her for a headset.\"\n"
                    "⏰ Punishment: Mute for 1 day. After 2 mutes, a warn will be issued. (Does not stack)",

                    "✝ 1.5 Violation: Clipping allies of any kind.\n"
                    "⏰ Punishment: Mute for 30-60 minutes. (Does not stack)",

                    "⛓ 1.6 Violation: Free-pinging with the /supp command and other commands "
                    "that ping a lot of people.\n"
                    "⏰ Punishment: Mute for 3 hours. After 2 mutes, a warn will be issued. (Stacks)",

                    "☯ 1.7 Violation: Attacking allies 1v2, 1v3, etc.\n"
                    "⏰ Punishment: Warn or mute for 1-5 hours. (Stacks)",
                ),
            ),
            (
                "🎙️ • VOICE RULES — VR.",
                (
                    "❀ 1.0 Violation: Spamming the Soundboard.\n"
                    "⏰ Punishment: Mute for 30 minutes. After 2 mutes, a warn will be issued.\n"
                    "➕ Addition: You can use a SoundPad, but you can't spam! (Does not stack)",

                    "✧ 1.1 Violation: In voice channels, it is prohibited to promote any outside content "
                    "such as websites, online stores, Discord servers, VK groups and communities, "
                    "Telegram groups and channels. It is also prohibited to hold any fundraisers like "
                    "\"Oh, look at this cute streamer, let's donate to her for a headset.\"\n"
                    "⏰ Punishment: Mute for 1 day. After 2 mutes, a warn will be issued. (Does not stack)",

                    "★ 1.2 Violation: Disclosing personal information about members in a voice channel "
                    "is prohibited. (Allowed only with the member's permission)\n"
                    "⏰ Punishment: Mute for 12-24 hours, depending on the information. (Does not stack)",
                ),
            ),
        ),
    },
}


def code_block(text):
    return "```\n" + text.replace("```", "`​``") + "\n```"


async def load_rules_gif():
    for folder in (DATA_DIR, Path(__file__).resolve().parent):
        local = folder / RULES_GIF_FILENAME
        if local.is_file():
            return disnake.File(local, filename=RULES_GIF_FILENAME)

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(RULES_GIF_URL) as response:
                response.raise_for_status()
                data = await response.content.read(BANNER_SIZE_LIMIT + 1)

        if len(data) > BANNER_SIZE_LIMIT:
            raise ValueError("GIF больше 10 МБ")
        if not data.startswith((b"GIF87a", b"GIF89a")):
            raise ValueError("по ссылке не GIF")

        return disnake.File(io.BytesIO(data), filename=RULES_GIF_FILENAME)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as exc:
        logging.warning("Не удалось загрузить GIF правил: %s", exc)
        return None


def make_rules_panel(with_gif):
    items = []
    if with_gif:
        items.append(
            disnake.ui.MediaGallery(
                disnake.MediaGalleryItem(f"attachment://{RULES_GIF_FILENAME}")
            )
        )

    items.extend((
        disnake.ui.TextDisplay("## 🦇 Server Rules."),
        disnake.ui.TextDisplay("> " + RULES_INTRO),
        disnake.ui.Separator(divider=True, spacing=disnake.SeparatorSpacing.small),
        disnake.ui.ActionRow(
            disnake.ui.Button(
                label="Ru",
                emoji="🇷🇺",
                style=disnake.ButtonStyle.secondary,
                custom_id="rules:ru",
            ),
            disnake.ui.Button(
                label="Eng",
                emoji="🇺🇸",
                style=disnake.ButtonStyle.secondary,
                custom_id="rules:en",
            ),
        ),
    ))

    return [disnake.ui.Container(*items, accent_colour=disnake.Colour(0x000000))]


def make_rules_text(lang):
    data = RULES[lang]
    containers = []

    for index, (title, rules) in enumerate(data["sections"]):
        items = []
        if index == 0:
            items.append(disnake.ui.TextDisplay(f"**{data['header']}**"))
        items.append(disnake.ui.TextDisplay(f"## {title}"))
        items.extend(disnake.ui.TextDisplay(code_block(rule)) for rule in rules)
        containers.append(
            disnake.ui.Container(*items, accent_colour=disnake.Colour(0x000000))
        )

    return containers


@bot.slash_command(
    name="rules",
    description="Опубликовать правила сервера",
    **PANEL_KWARGS,
)
async def rules_command(inter: disnake.ApplicationCommandInteraction):
    if inter.guild is None:
        await inter.response.send_message(
            "Команда доступна только на сервере Discord.", ephemeral=True
        )
        return

    if not is_staff(inter):
        await inter.response.send_message(
            "Команда /rules доступна только стаффу.", ephemeral=True
        )
        return

    await inter.response.defer(ephemeral=True, with_message=True)

    gif = await load_rules_gif()
    kwargs = {"components": make_rules_panel(gif is not None)}
    if gif is not None:
        kwargs["file"] = gif

    try:
        await inter.channel.send(**kwargs)
    except disnake.HTTPException:
        logging.exception("Не удалось опубликовать правила")
        await inter.edit_original_response(
            content="Не удалось опубликовать правила. Проверь права бота в этом канале."
        )
        return

    text = "✅ Правила опубликованы."
    if gif is None:
        text += "\n⚠️ GIF не загрузилась, сообщение отправлено без неё."
    await inter.edit_original_response(content=text)


@bot.listen("on_button_click")
async def rules_buttons(inter: disnake.MessageInteraction):
    custom_id = inter.data.custom_id or ""
    if custom_id not in ("rules:ru", "rules:en"):
        return

    lang = custom_id.split(":", 1)[1]
    await inter.response.send_message(
        components=make_rules_text(lang),
        ephemeral=True,
    )


GUIDE_GIF_NAME = "guide"

# Картинки пунктов этой страницы: пункт N берёт ссылку из шаблона, файлами они не
# прикладываются. Discord пускает в одно сообщение около 25 МБ вложений, а у ссылок
# такого предела нет. Файлы лежат в папке guide/ репозитория: 1.gif, 2.gif…
# Другой хостинг задаётся переменной GUIDE_MEDIA_URL, пустая — без картинок.
GUIDE_MEDIA_PAGE = "teamwork"
GUIDE_MEDIA_URL = os.getenv(
    "GUIDE_MEDIA_URL",
    "https://raw.githubusercontent.com/pidoryahocuzdoxnut2-hash/midnight-bot"
    "/main/guide/{number}.gif",
)
GUIDE_MEDIA_TTL = 5 * 60
GUIDE_MEDIA_STATE = {"checked_at": float("-inf"), "available": set()}

GUIDE_INTRO = (
    "Здесь вы можете ознакомиться с командами сервера, "
    "юба сленгом и командной игрой."
)

# Discord: не больше 40 компонентов и 4000 символов текста в одном сообщении.
MESSAGE_COMPONENT_LIMIT = 40
MESSAGE_TEXT_LIMIT = 4000

GUIDE_PAGES = {
    "teamwork": (
        "### Не сосредотачивайтесь на одном враге с более чем тремя людьми:\n"
        "✧ Держите давление сбалансированным и избегайте перегруженности одной цели. "
        "Это позволяет остальной части команды эффективно справляться с другими врагами.\n\n"
        "✧ Избегайте использования навыков рагдолла во время командных комбо "
        "(например, Jawbreaker, HHA и т.д.):\n"
        "Эти способности могут нарушить синергию вашей команды, затрудняя контроль над врагом.",

        "### Максимизируйте оглушения в комбо:\n"
        "✧ Используйте способности, такие как Star Finger, Liver Shot или удары ногами, "
        "чтобы удерживать врага в оглушении и уязвимости на протяжении всего комбо.",

        "### Никогда не накладывайте Barrage, если ваш товарищ по команде уже атакует врага:\n"
        "✧ Позвольте вашему товарищу завершить свою атаку. Дублирование тратит возможность "
        "и рискует оставить врага без оглушения.",

        "### Если возможно, атакуйте врага, чтобы зажать его:\n"
        "Хорошо таймированная атака может позволить вашей команде сосредоточиться "
        "на зажиме и легче устранить цель.\n\n"
        "✧ Используйте оглушающие способности в конце атаки вашего товарища по команде:\n"
        "Таймируйте ваше оглушение прямо в конце атаки, чтобы ваша способность сработала, "
        "давая вашей команде преимущество.\n"
        "> Если у вас есть GER, обойдите врага, пока он в атаке:\n"
        "✧ Займите позицию за врагом и выпустите птиц и скорпионов для дополнительного "
        "урона, пока они зафиксированы в атаке.",

        "### Пока ваш товарищ по команде атакует врага, наносите удары ногами:\n"
        "✧ Однако будьте осторожны: удары ногами имеют финальные удары, которые могут "
        "вызвать рагдолл. Убедитесь, что вы направляете их в сторону от врага, чтобы "
        "случайно не вызвать рагдолл.",

        "### КАК ИЗБЕЖАТЬ ТИМ КОМБО?\n"
        "**Избегайте атак и блокбрейкеров:**\n"
        "✧ Держитесь на расстоянии от врагов, пытающихся нанести блокбрейкер "
        "или поймать вас в атаке.",

        "### Ответьте атакой, если вас поймали:\n"
        "✧ Сразу же атакуйте, прицеливаясь на того, кто атакует, и тех, кто использует "
        "способности. Это нарушит их комбо.",

        "### Используйте способности для побега:\n"
        "✧ Способности, такие как дерево или ртз, могут помочь вам вырваться из комбо "
        "и вернуть контроль.\n\n"
        "**Блокируйте и уклоняйтесь, если в комбо нет блокбрейкера:**\n"
        "✧ Если никто не ломает вашу защиту, блокируйте удары и уклоняйтесь, "
        "чтобы избежать комбо.",
    ),
    "commands": (
        "### Полезные и обязательные команды сервера:\n"
        "**Пинг.**\n"
        f"> /supp — команда, чтобы дать пинг в <#{SUPPORT_CHANNEL_ID}>. "
        "Важно, чтобы в сообщении было как можно больше информации о том, кто вас бьёт, "
        "сколько их и где (стенды писать необязательно).\n\n"
        "**Примечание:**\n"
        "**За фрипинг пинга вы получите 3 часа мута.**",

        "### Масслог.\n"
        f"> /ms — команда, чтобы дать сигнал выхода с сервера. Обычно используется "
        f"после победы и в <#{SUPPORT_CHANNEL_ID}>. Важно, чтобы у вас было фото (пик), "
        "только после него можно выходить.",
    ),
    "slang": (
        "### [⚙️] ROPRO/ROQOL\n"
        "Расширение на ваш браузер, с помощью которого можно получить ссылку на сервер "
        "в юбе, на котором вы находитесь.\n"
        "Как ходить на генг вары? Нужно перейти по ссылке, которую скинули в чат гв "
        "пингов, вас перекинет на сервак с битвой, зачастую надо стоять лайн (очередь).",

        "### [🔪] MASSLOG/LOG\n"
        "Означает массовый лив с сервера. Если вы видите, что в чате один человек "
        "из стаффа пишет это, значит, вам нужно немедленно выйти с сервера, "
        "дабы избежать наказания.",

        "### [🌴] GW\n"
        "Сражение 2 или более генгов, которое не было запланировано. На гв участники "
        "не ограничены, и чем их будет больше — тем больше шансов победить на этом генг варе.\n"
        "> Расшифровка ГВ — Генг Вар.",

        "### [🦖] SW/SUPP\n"
        "Когда участники попросили помощь у генга против ркеров, чьё количество больше, "
        "чем участников генга, это называется св, и здесь логика такая же: чем больше "
        "людей, тем лучше. Также на св против вас может быть слабый генг или же фг "
        "(френд группа), тогда название будет не св, а сгв. Ещё бывает так, что после "
        "св может произойти гв, так как враги, на которых вы позвали генг, тоже могут "
        "быть участниками других генгов и тоже могут позвать свой генг — так может "
        "начаться генг вар.\n"
        "> Расшифровка СВ — Смалл Вар.",

        "### [👥] Line/Q\n"
        "Очередь/Q — это ваша позиция в очереди, когда сервер полон. Важно сообщать "
        "о вашей позиции в канале queue, чтобы мы могли отслеживать, наша ли это "
        "очередь или очередь врага.\n"
        "> Line/Q — Очередь на сервер.",

        "### [📸] Пик/Pic\n"
        "Означает фото/брать фото. Обычно его делают, когда противники на гв/св больше "
        "не приходят и не пытаются убить вас. После пика обычно происходит масслог, "
        "после чего победа засчитана.",

        "### [❌] Фпик/FPIC\n"
        "Это когда скриншот победы сделан, но на сервере осталось 2+ союзников.\n"
        "> Фпик/FPIC — Фейковый GW пик.",

        "### [🤝] Allied, Enemy Gangs, and True List\n"
        "✧ Союзные Банды — это банды, с которыми у нас есть альянс. Нападение на них "
        "запрещено, даже если они нападают первыми. Если союзная банда начинает атаку, "
        "сообщите об этом владельцам.\n\n"
        "✧ Вражеские Банды — это банды, с которыми мы в конфликте. Нападайте на них, "
        "когда это возможно, и готовьтесь к генг войнам против них.\n\n"
        "✧ Тру лист — это банды, с которыми у нас дружеские отношения. Если они "
        "нападают первыми, вы можете упомянуть банду для поддержки.",

        "### [💡] Tips\n"
        "✧ Скачайте OBS/MEDAL — это программы для записи. Во время генг варов старайтесь "
        "записывать события, чтобы поймать злоупотребляющих ошибками или фейковые "
        "GW пики от вражеской банды.\n\n"
        "✧ Работайте в команде — эффективная командная работа — ключ к победе "
        "над противниками.\n\n"
        "✧ Если вы находитесь в GW и не можете упомянуть о генг варе, оставьте ссылку "
        "RoPro и упомяните банду о помощи, указав, что это GW.\n\n"
        "✧ Следуйте указаниям владельца — если владелец инструктирует вас использовать "
        "определённые стойки во время GW, используйте те, которые наносят урон "
        "по площади, такие как Hierophant Green, MiH, VA, BTD или Purple Haze.\n\n"
        "✧ Помогайте соклановцам — если вы видите, что один из наших членов "
        "подвергается зажиму, используйте навыки, которые сбивают врагов, "
        "чтобы помочь им избежать зажима.\n\n"
        "✧ Установите расширение RoPro — это расширение позволяет вам отправлять "
        "быстрые ссылки на сервер для быстрого входа и помощи во время генг варов.",
    ),
}


def make_guide_panel(gif_filename):
    items = []
    if gif_filename:
        items.append(
            disnake.ui.MediaGallery(
                disnake.MediaGalleryItem(f"attachment://{gif_filename}")
            )
        )

    items.extend((
        disnake.ui.TextDisplay("## 🦇 Server Guide."),
        disnake.ui.TextDisplay("> " + GUIDE_INTRO),
        disnake.ui.Separator(divider=True, spacing=disnake.SeparatorSpacing.small),
        disnake.ui.ActionRow(
            disnake.ui.Button(
                label="Commands",
                emoji="💫",
                style=disnake.ButtonStyle.secondary,
                custom_id="guide:commands",
            ),
            disnake.ui.Button(
                label="Team work",
                emoji="🌈",
                style=disnake.ButtonStyle.secondary,
                custom_id="guide:teamwork",
            ),
            disnake.ui.Button(
                label="Yba slang",
                emoji="🌟",
                style=disnake.ButtonStyle.secondary,
                custom_id="guide:slang",
            ),
        ),
    ))

    return [disnake.ui.Container(*items, accent_colour=disnake.Colour(0x000000))]


def guide_media_url(number):
    return GUIDE_MEDIA_URL.format(number=number)


async def guide_media_reachable(session, url):
    try:
        async with session.head(url, allow_redirects=True) as response:
            return response.status < 400
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


async def available_guide_media(fresh=False):
    """Номера пунктов Team work, чья картинка открывается по ссылке.

    Результат держится несколько минут, чтобы не ходить в сеть на каждое нажатие.
    """
    if not GUIDE_MEDIA_URL:
        return set()

    state = GUIDE_MEDIA_STATE
    if not fresh and time.monotonic() - state["checked_at"] < GUIDE_MEDIA_TTL:
        return state["available"]

    numbers = range(1, len(GUIDE_PAGES[GUIDE_MEDIA_PAGE]) + 1)
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        results = await asyncio.gather(
            *(guide_media_reachable(session, guide_media_url(number)) for number in numbers)
        )

    state["available"] = {number for number, ok in zip(numbers, results) if ok}
    state["checked_at"] = time.monotonic()
    return state["available"]


def make_guide_container(block, media_url=None):
    items = [disnake.ui.TextDisplay(block)]
    if media_url:
        items.append(disnake.ui.MediaGallery(disnake.MediaGalleryItem(media_url)))

    return disnake.ui.Container(*items, accent_colour=disnake.Colour(0x000000))


def make_guide_messages(page, media=()):
    """Раскладывает пункты по сообщениям так, чтобы не упереться в лимиты Discord.

    media — номера пунктов, к которым можно приложить картинку.
    """
    messages = [[]]
    components = text = 0

    for number, block in enumerate(GUIDE_PAGES[page], start=1):
        url = None
        if page == GUIDE_MEDIA_PAGE and number in media:
            url = guide_media_url(number)

        used = 4 if url else 2

        if messages[-1] and (
            components + used > MESSAGE_COMPONENT_LIMIT
            or text + len(block) > MESSAGE_TEXT_LIMIT
        ):
            messages.append([])
            components = text = 0

        messages[-1].append(make_guide_container(block, url))
        components += used
        text += len(block)

    return messages


@bot.slash_command(
    name="guide",
    description="Опубликовать гайд по серверу",
    **PANEL_KWARGS,
)
async def guide_command(inter: disnake.ApplicationCommandInteraction):
    if inter.guild is None:
        await inter.response.send_message(
            "Команда доступна только на сервере Discord.", ephemeral=True
        )
        return

    if not is_staff(inter):
        await inter.response.send_message(
            "Команда /guide доступна только стаффу.", ephemeral=True
        )
        return

    await inter.response.defer(ephemeral=True, with_message=True)

    gif_path = find_local_gif(GUIDE_GIF_NAME)
    kwargs = {"components": make_guide_panel(gif_path.name if gif_path else None)}
    if gif_path is not None:
        kwargs["file"] = disnake.File(gif_path, filename=gif_path.name)

    try:
        await inter.channel.send(**kwargs)
    except disnake.HTTPException:
        logging.exception("Не удалось опубликовать гайд")
        await inter.edit_original_response(
            content="Не удалось опубликовать гайд. Проверь права бота в этом канале."
        )
        return

    text = "✅ Гайд опубликован."
    if gif_path is None:
        text += (
            f"\n⚠️ Файл `{GUIDE_GIF_NAME}.gif` не найден рядом с ботом, "
            "сообщение отправлено без гифки."
        )

    if GUIDE_MEDIA_URL:
        try:
            available = await available_guide_media(fresh=True)
        except Exception:
            logging.exception("Не удалось проверить картинки гайда")
            text += "\n⚠️ Не смог проверить картинки Team work, подробности в логах бота."
        else:
            missing = [
                f"{number}.gif"
                for number in range(1, len(GUIDE_PAGES[GUIDE_MEDIA_PAGE]) + 1)
                if number not in available
            ]
            if missing:
                text += (
                    "\n⚠️ Team work: не открываются картинки " + ", ".join(missing)
                    + ", эти пункты будут без картинок."
                )
    await inter.edit_original_response(content=text)


@bot.listen("on_button_click")
async def guide_buttons(inter: disnake.MessageInteraction):
    custom_id = inter.data.custom_id or ""
    if not custom_id.startswith("guide:"):
        return

    page = custom_id.split(":", 1)[1]
    if page not in GUIDE_PAGES:
        return

    # Проверка картинок идёт по сети, а на ответ Discord даёт всего 3 секунды.
    await inter.response.defer(ephemeral=True, with_message=True)

    media = ()
    if page == GUIDE_MEDIA_PAGE:
        try:
            media = await available_guide_media()
        except Exception:
            logging.exception("Не удалось проверить картинки гайда, шлю без них")

    for components in make_guide_messages(page, media):
        await inter.followup.send(components=components, ephemeral=True)


async def deny(inter, text):
    if inter.response.is_done():
        await inter.edit_original_response(content=text)
    else:
        await inter.response.send_message(text, ephemeral=True)


async def require_moderator(inter):
    if inter.guild is None:
        await deny(inter, "Команда доступна только на сервере.")
        return False
    if not is_staff(inter):
        await deny(inter, "Эта команда только для стаффа.")
        return False
    return True


@bot.slash_command(name="clear", description="Удалить сообщения в канале", **PANEL_KWARGS)
async def clear_command(
    inter: disnake.ApplicationCommandInteraction,
    amount: int = commands.Param(description="Сколько сообщений удалить (1-100)", ge=1, le=100),
    user: disnake.User = commands.Param(None, description="Удалить только сообщения этого участника"),
):
    if not await require_moderator(inter):
        return

    await inter.response.defer(ephemeral=True)
    left = [amount]

    def check(message):
        if user is not None and message.author.id != user.id:
            return False
        if left[0] <= 0:
            return False
        left[0] -= 1
        return True

    try:
        deleted = await inter.channel.purge(
            limit=amount if user is None else 300,
            check=check,
        )
    except disnake.HTTPException:
        logging.exception("Ошибка очистки канала")
        await deny(inter, "Не удалось удалить сообщения. Проверь право бота «Управлять сообщениями».")
        return

    await inter.edit_original_response(content=f"🧹 Удалено сообщений: {len(deleted)}.")


# ===== ВРЕМЕННО: команда /ha1to — выдать роль =====
HA1TO_ROLE_ID = 1526991435834982550


@bot.slash_command(
    name="ha1to",
    description="Выдать роль участнику",
    **PANEL_KWARGS,
)
async def ha1to_command(
    inter: disnake.ApplicationCommandInteraction,
    user: disnake.Member = commands.Param(description="Кому выдать роль"),
):
    if inter.guild is None:
        await inter.response.send_message(
            "Команда доступна только на сервере.", ephemeral=True
        )
        return

    # пользоваться может только стафф
    if not is_staff(inter):
        await inter.response.send_message(
            "Команда доступна только стаффу.", ephemeral=True
        )
        return

    await inter.response.defer(ephemeral=True, with_message=True)

    role = inter.guild.get_role(HA1TO_ROLE_ID)
    if role is None:
        await inter.edit_original_response(content="Роль не найдена на сервере.")
        return

    if not inter.guild.me.guild_permissions.manage_roles or not role.is_assignable():
        await inter.edit_original_response(
            content="Не могу выдать роль — проверь право «Управлять ролями» "
            "и что роль бота выше выдаваемой."
        )
        return

    try:
        await user.add_roles(role, reason=f"/ha1to от {inter.author.id}")
    except disnake.HTTPException:
        logging.exception("Не удалось выдать роль через /ha1to")
        await inter.edit_original_response(content="Не удалось выдать роль.")
        return

    await inter.edit_original_response(
        content=f"✅ Выдал роль {role.mention} для {user.mention}."
    )
# ===== конец временного блока =====


@bot.event
async def on_ready():
    logging.info("Бот тикетов %s запущен.", bot.user)
    logging.info("Файл бота: %s", Path(__file__).resolve())
    logging.info("База тикетов: %s", DB_PATH)
    logging.info("GUILD_ID регистрации: %s", GUILD_ID or "глобально")
    logging.info(
        "Локальные slash-команды: %s",
        ", ".join(sorted(command.name for command in bot.application_commands)),
    )

    if GUILD_ID:
        if bot.get_guild(GUILD_ID) is None:
            logging.error(
                "Бот не видит сервер GUILD_ID=%s! Серверы бота: %s",
                GUILD_ID,
                ", ".join(str(guild.id) for guild in bot.guilds),
            )
        try:
            registered = await bot.fetch_guild_commands(GUILD_ID)
        except disnake.HTTPException:
            logging.exception("Не удалось получить команды сервера %s", GUILD_ID)
        else:
            registered_names = {command.name for command in registered}
            logging.info(
                "Команды Discord на сервере %s: %s",
                GUILD_ID,
                ", ".join(sorted(registered_names)) or "нет",
            )
            if "tryout" not in registered_names:
                logging.error(
                    "Команда /tryout отсутствует в Discord. Убедись, что "
                    "запущен именно этот файл, GUILD_ID правильный, "
                    "а другие копии бота с тем же токеном выключены."
                )
            for required_name in ("supp", "gw", "ms", "rules", "guide"):
                if required_name not in registered_names:
                    logging.error(
                        "Команда /%s отсутствует в Discord. Проверь синхронизацию, "
                        "GUILD_ID и отсутствие второй копии бота.",
                        required_name,
                    )


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "Не задан TICKET_BOT_TOKEN (или DISCORD_TOKEN). "
            "Укажите токен бота в переменных окружения."
        )

    bot.run(TOKEN)

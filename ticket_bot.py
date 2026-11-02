"""Бот тикетов: заявки Member / Guest / Ally Member.

Каждая заявка открывает отдельный канал, доступный автору и стафф-ролям.
После решения канал закрывается автоматически (TICKET_DELETE_AFTER секунд).
"""

import asyncio
import aiohttp
import io
import logging
import os
import secrets
import shutil
import sqlite3
import time
from pathlib import Path

# Bothost может собирать образ со стандартными Telegram-зависимостями
# и игнорировать requirements.txt. Устанавливаем disnake в Python,
# которым непосредственно запущен этот файл, только если её нет.
import importlib
import importlib.util
import subprocess
import sys

if importlib.util.find_spec("disnake") is None:
    print("[Midnight] disnake не найдена; устанавливаю в текущее окружение Python...", flush=True)
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
            "[Midnight] Не удалось установить disnake при запуске. "
            "Хостинг должен разрешать pip install или установить disnake "
            "во время Docker-сборки."
        ) from exc
    importlib.invalidate_caches()
    print("[Midnight] disnake установлена успешно.", flush=True)

import disnake
from disnake.ext import commands


# ================= НАСТРОЙКИ =================

# Отдельная переменная, чтобы бот тикетов не конфликтовал с основным ботом.
TOKEN = os.getenv("TICKET_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")

# Если переменная не задана, токен читается из ticket_token.txt рядом с файлом.
if not TOKEN:
    TOKEN_FILE = Path(__file__).resolve().with_name("ticket_token.txt")

    if TOKEN_FILE.exists():
        TOKEN = TOKEN_FILE.read_text(encoding="utf-8").strip() or None

# Если указать GUILD_ID, команды появятся на сервере сразу. Без него — глобально.
GUILD_ID = int(
    os.getenv("GUILD_ID", "1526991373742379069") or 0
) or None

# Категория с открытыми тикетами.
TICKET_CATEGORY_ID = int(
    os.getenv("TICKET_CATEGORY_ID", "1527046712349429982") or 0
) or None

# Категория, куда переносятся закрытые тикеты.
CLOSED_CATEGORY_ID = int(
    os.getenv("TICKET_CLOSED_CATEGORY_ID", "1526991374950469653") or 0
) or None

# Через сколько секунд удалять канал после решения. 0 — оставить закрытым.
DELETE_AFTER = int(os.getenv("TICKET_DELETE_AFTER", "0") or 0)

STAFF_ROLE_IDS = (
    1527390250748608562,
    1526991435834982550,
    1527269547181211768,
)

# Только эти роли могут использовать /gw.
GW_ROLE_IDS = (
    1527269547181211768,
    1527329319544950784,
    1526991435834982550,
)

# Роль, которую пингуем при создании заявки.
PING_ROLE_ID = 1527270088774914250

# Кого упоминать в сообщениях о помощи, создаваемых через /supp.
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

# Роль, которая снимается при принятии ЛЮБОЙ заявки.
REMOVE_ROLE_IDS = (1527334034818990161,)

KIND_NAMES = {
    "member": "Member",
    "guest": "Guest",
    "ally": "Ally Member",
}

# ================= ТРАЯУТЫ MIDNIGHT =================

# Отдельная категория для открытых траяут-тикетов.
TRYOUT_CATEGORY_ID = 1527219102773542994

# Только эта роль вправе принимать траяуты и назначать тиры.
TRYOUT_STAFF_ROLE_ID = 1527330169730105514
TRYOUT_LOG_GIF_URL = "https://static2.klipy.com/ii/c3a19a0b747a76e98651f2b9a3cca5ff/75/63/AnCODnnC.gif"
TRYOUT_LOG_CHANNEL_ID = 1527664670532567123

# При назначении любого тира снимается эта роль.
TRYOUT_UNRANKED_ROLE_ID = 1527413008211640590

# Выбираемый тир -> ID Discord-роли. Порядок соответствует меню.
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


# ================= ХРАНИЛИЩЕ =================

DATA_DIR = Path(
    os.getenv("DATA_DIR") or Path(__file__).resolve().parent
)
DATA_DIR.mkdir(parents=True, exist_ok=True)


def data_file(name):
    """Путь к базе в DATA_DIR. Старый файл рядом с bot.py переносится один раз."""
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

# Номер тикета: ticket 1 → closed 1, ticket 2 → closed 2 и так далее.
COLUMNS = {
    row["name"]
    for row in db.execute("PRAGMA table_info(tickets)")
}

if "number" not in COLUMNS:
    db.execute("ALTER TABLE tickets ADD COLUMN number INTEGER")

counter = db.execute(
    "SELECT COALESCE(MAX(number), 0) FROM tickets"
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
db.commit()


def next_ticket_number():
    """Выдаёт следующий номер и увеличивает счётчик."""
    with db:
        db.execute(
            "UPDATE ticket_counter SET value = value + 1 WHERE id = 1"
        )
        return db.execute(
            "SELECT value FROM ticket_counter WHERE id = 1"
        ).fetchone()[0]


def free_ticket_number(number):
    """Возвращает номер, если канал создать не удалось."""
    with db:
        db.execute(
            "UPDATE ticket_counter SET value=? WHERE id=1 AND value=?",
            (number - 1, number),
        )


ticket_lock = asyncio.Lock()

# Траяуты храним в той же базе, но независимо от обычных тикетов.
# Миграция безопасна для существующего tickets.sqlite3.
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
# Старые траяуты остаются в базе; новое поле добавляется без её очистки.
tryout_columns = {
    row["name"] for row in db.execute("PRAGMA table_info(tryouts)")
}
if "loadout" not in tryout_columns:
    db.execute("ALTER TABLE tryouts ADD COLUMN loadout TEXT NOT NULL DEFAULT ''")

# Личный кулдаун траяутов: независимая запись на каждого игрока сервера.
# Составной PRIMARY KEY исключает общий лимит для всех участников.
db.execute("""
    CREATE TABLE IF NOT EXISTS tryout_cooldowns (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        last_created_at INTEGER NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )
""")

# Переносим сроки из уже существующих траяутов: старые ограничения не теряются.
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


# ================= БОТ =================

INTENTS = disnake.Intents.default()
# Для оставшейся текстовой команды !ms нужен Message Content Intent.
INTENTS.message_content = True
# Для выборки участников при рассылке !gw (включить также в Developer Portal).
INTENTS.members = True

# Явно включаем регистрацию команд сервера и диагностику при запуске.
# Это важно после добавления /tryout: если Discord показывает неизвестную
# команду, в консоли будет видно, что именно зарегистрировано.
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
    """Строгая проверка именно стафф-роли, без обхода через manage_guild."""
    return any(
        role.id in STAFF_ROLE_IDS
        for role in getattr(member, "roles", [])
    )


def has_gw_role(member):
    """Проверка доступа именно к команде /gw."""
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
    """Какие роли выдать и снять при принятии заявки."""
    if kind == "guest":
        add_ids = list(GUEST_ROLE_IDS)

    elif kind == "ally":
        add_ids = list(dict.fromkeys(MEMBER_ROLE_IDS + ALLY_EXTRA_ROLE_IDS))

    else:
        add_ids = list(MEMBER_ROLE_IDS)

    # Роль 1527334034818990161 снимается при принятии любой заявки.
    return add_ids, list(REMOVE_ROLE_IDS)


# ================= ФОРМЫ ЗАЯВОК =================

# (custom_id, label, placeholder, required, paragraph)
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


# ================= ПАНЕЛЬ =================

# Оригинальную GIF скачиваем и прикрепляем к сообщению при публикации панели.
# Оригинальная анимированная GIF, не статичный скриншот.
BANNER_URL = (
    "https://static2.klipy.com/ii/"
    "d7aec6f6f171607374b2065c836f92f4/b4/d0/6oGlkELa.gif"
)
BANNER_FILENAME = "midnight_banner.gif"
BANNER_SIZE_LIMIT = 10 * 1024 * 1024


async def load_gif_banner():
    """Загружает настоящую GIF для прикрепления к панели.

    Если внешний сервер временно недоступен, панель откроется по прямому URL.
    """
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
    """Панель в стиле прошлого бота: баннер, текст и меню TICKET."""
    return disnake.ui.Container(
        disnake.ui.MediaGallery(
            disnake.MediaGalleryItem(banner_url)
        ),
        disnake.ui.TextDisplay(
            "## 👋 Путь в Midnight gang начинается здесь!"
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

    # Загрузка GIF может занять несколько секунд — отвечаем Discord сразу.
    await inter.response.defer(ephemeral=True, with_message=True)

    try:
        banner_url, banner_file = await load_gif_banner()
        kwargs = {"components": [make_panel(banner_url)]}
        if banner_file is not None:
            kwargs["file"] = banner_file
        await inter.channel.send(**kwargs)
        await inter.edit_original_response(content="✅ Панель тикетов опубликована.")

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


# ================= СОЗДАНИЕ ТИКЕТА =================

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
            WHERE guild_id=? AND user_id=? AND state='open'
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
            # Discord оформляет inline-code тёмным фоном, как на примере.
            # Для многострочного ответа используем отдельный code block.
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


# ================= РЕШЕНИЕ ПО ЗАЯВКЕ =================

def open_ticket(channel_id):
    return db.execute(
        """
        SELECT *
        FROM tickets
        WHERE channel_id=? AND state='open'
        """,
        (channel_id,),
    ).fetchone()


async def apply_roles(guild, member, kind):
    add_ids, remove_ids = role_ids_for(kind)

    missing = [
        str(role_id)
        for role_id in add_ids + remove_ids
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

    # Снятие роли не должно срывать принятие: если не получилось — сообщим.
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
    """Переносит канал в закрытую категорию и убирает доступ заявителя."""
    guild = channel.guild
    problems = []

    closed_category = None

    if CLOSED_CATEGORY_ID:
        candidate = guild.get_channel(CLOSED_CATEGORY_ID)

        if isinstance(candidate, disnake.CategoryChannel):
            closed_category = candidate
        else:
            problems.append("категория закрытых тикетов не найдена")

    # Убираем персональный доступ заявителя: канал останется только у стаффа.
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
        asyncio.create_task(delete_later(channel, DELETE_AFTER))

    await inter.edit_original_response(content=text)


async def accept_ticket(inter):
    await inter.response.defer(ephemeral=True, with_message=True)

    row = open_ticket(inter.channel.id)

    if row is None:
        await inter.edit_original_response(
            content="Эта заявка уже обработана или не найдена."
        )
        return

    guild = inter.guild
    member = guild.get_member(row["user_id"])

    if member is None:
        try:
            member = await guild.fetch_member(row["user_id"])

        except disnake.NotFound:
            await inter.edit_original_response(
                content="Заявитель уже вышел с сервера — роли выдать нельзя."
            )
            return

    try:
        added, removed, problems = await apply_roles(
            guild, member, row["kind"]
        )

    except ValueError as error:
        await inter.edit_original_response(content=str(error))
        return

    except disnake.Forbidden:
        logging.exception("Discord запретил выдачу ролей")
        await inter.edit_original_response(
            content=(
                "Discord запретил выдачу ролей. Проверьте право "
                "«Управлять ролями» и положение роли бота."
            )
        )
        return

    except disnake.HTTPException:
        logging.exception("Ошибка выдачи ролей")
        await inter.edit_original_response(
            content="Не удалось выдать роли. Попробуйте ещё раз."
        )
        return

    with db:
        db.execute(
            "UPDATE tickets SET state='accepted' WHERE channel_id=?",
            (row["channel_id"],),
        )

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

        row = open_ticket(inter.channel.id)

        if row is None:
            await inter.edit_original_response(
                content="Эта заявка уже обработана или не найдена."
            )
            return

        reason = (inter.text_values.get("reason") or "").strip()

        with db:
            db.execute(
                "UPDATE tickets SET state='rejected' WHERE channel_id=?",
                (row["channel_id"],),
            )

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


# ================= КНОПКИ =================

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


# ================= TRYOUT TICKETS =================


def is_tryout_staff(inter):
    """Для принятия/отказа траяута требуется именно указанная роль."""
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
    """Весь текст и кнопка — в одном сообщении, как на примере."""
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
    embed.set_footer(text="Midnight • TryoutTicket")

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
    # Подтверждаем интеракцию сразу: Discord разрешает максимум 3 секунды.
    # Публичная панель публикуется в исходном сообщении команды.
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
    """Личный срок игрока. Чужие траяуты на него не влияют."""
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
    """Возвращает статус ограничений только для одного Discord ID."""
    ready_at = next_tryout_time(guild_id, user_id)
    return ready_at is not None and ready_at > now, ready_at



def tryout_overwrites(guild, applicant):
    """Канал виден заявителю, боту и только роли траяутеров."""
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
    """Единственный вопрос перед созданием траяута."""

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
    """Создать приватный траяут после анкеты с ограничением раз в 7 дней."""
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
            # Если канал удалён вручную, освобождаем запись.
            with db:
                db.execute(
                    "UPDATE tryouts SET state='deleted' WHERE channel_id=?",
                    (old["channel_id"],),
                )

        # Кулдаун проверяем именно для автора заявки, не для всего сервера.
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
        embed.set_footer(text=f"Midnight • Tryout №{number}")

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
            # Ставим кулдаун только создавшему траяут игроку.
            # Другие участники сервера не затрагиваются.
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
    """Заменить все старые тиры на один выбранный за одно изменение ролей."""
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
    # Одним запросом снимаем старые тиры и устанавливаем новый.
    await member.edit(
        roles=new_roles,
        reason=f"Траяут {tier_name}; назначил {moderator.id}",
    )
    return old_roles, new_role


async def finish_tryout(inter, row, *, accepted, tier_name=None):
    """Закрывает траяут и сохраняет результат в SQLite."""
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
    except (disnake.HTTPException, disnake.Forbidden):
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
        asyncio.create_task(delete_later(channel, DELETE_AFTER))

    response = f"✅ Траяут №{row['number']} обработан."
    if accepted:
        response += f" Назначен тир **{tier_name}**."
    if notes:
        response += "\n" + "\n".join(notes)
    await inter.edit_original_response(content=response)


async def accept_tryout_tier(inter, channel_id, tier_name):
    """Повторно проверяем права и состояние, не доверяя значениям из меню."""
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
            await finish_tryout(inter, row, accepted=True, tier_name=tier_name)

        except ValueError as error:
            await inter.edit_original_response(content=f"❌ {error}")
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
        # Проверяем личный кулдаун ДО показа анкеты, чтобы игрок не заполнял
        # форму зря. В create_tryout есть повторная проверка под блокировкой.
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

    tier_name = inter.values[0] if inter.values else ""
    await accept_tryout_tier(inter, int(parts[2]), tier_name)


# ================= ЗАПРОС ПОМОЩИ /SUPP =================

SUPPORT_CHANNEL_ID = 1527030251446599680

SUPPORT_SEPARATOR = (
    "༺✿𓄂𓆩✦𓆪༺♛𓄂𓆩𓆪༺✥𓄂𓆩☆𓆪༺✬𓄂𓆩✿𓆪༺☆𓄂𓆪✧𓆜✥𓄂"
)


def support_answer(value: str) -> str:
    """Однострочный ответ: не даём переносами сломать текст объявления."""
    return disnake.utils.escape_mentions(
        " ".join(value.split())
    )


class SupportModal(disnake.ui.Modal):
    """Три поля для одного сообщения о помощи."""

    def __init__(self):
        super().__init__(
            title="NEED HELP 🎃",
            timeout=900,
            components=[
                disnake.ui.Label(
                    "*Link*",
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
                    "*Enemy*",
                    disnake.ui.TextInput(
                        custom_id="fight",
                        placeholder="сп,гер,твох",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        min_length=1,
                        max_length=100,
                    ),
                ),
                disnake.ui.Label(
                    "*Place*",
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

        # Сначала подтверждаем форму, затем публикуем сообщение в этот канал.
        await inter.response.defer(ephemeral=True, with_message=True)

        message = (
            f"# {inter.author.mention} NEED HELP 🎃\n"
            f"{SUPPORT_SEPARATOR}\n"
            f"> {link}\n"
            f"> {fight}\n"
            f"> {place}\n"
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
            content="✅ Запрос помощи опубликован в этом канале."
        )


@bot.slash_command(
    name="supp",
    description="Запросить помощь: ссылка, враги и место",
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

    # Первым ответом должен быть modal: не делаем defer перед открытием формы.
    await inter.response.send_modal(SupportModal())


# ================= ЗАПРОС ПОМОЩИ /GW =================

# /gw пингует эту роль в канале и рассылает ЛС только её участникам.
GW_TARGET_ROLE_ID = 1526993541765660732

# Чтобы не запустить массовую отправку по ошибке, требуется явное подтверждение.
# Рассылку может включить стафф, обычный /gw доступен как /supp.
GW_CONFIRM_EXPIRES = 5 * 60
GW_DM_INTERVAL_SECONDS = 0.8
GW_CONFIRMATIONS = {}
GW_RUNNING_TASKS = set()


def gw_modal_checkbox():
    """Галочка рассылки без описания; выбор для старой версии disnake."""
    if hasattr(disnake.ui, "Checkbox"):
        return disnake.ui.Label(
            "Разослать участникам в ЛС",
            disnake.ui.Checkbox(
                custom_id="gw_broadcast",
                default=False,
            ),
        )

    # disnake 2.11: альтернативное меню вместо недоступной галочки.
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
            title="GW — NEED HELP 🎃",
            timeout=900,
            components=[
                disnake.ui.Label(
                    "*Link*",
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
                    "*Enemy*",
                    disnake.ui.TextInput(
                        custom_id="fight",
                        placeholder="сп,гер,твох",
                        style=disnake.TextInputStyle.short,
                        required=True,
                        min_length=1,
                        max_length=100,
                    ),
                ),
                disnake.ui.Label(
                    "*Place*",
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

        if broadcast and not has_gw_role(inter.author):
            await inter.response.send_message(
                "Отправлять сообщения всем в ЛС может только стафф. "
                "Сними галочку или выбери «Нет».",
                ephemeral=True,
            )
            return

        await inter.response.defer(ephemeral=True, with_message=True)

        announcement = (
            f"# {inter.author.mention} NEED HELP 🎃\n"
            f"{SUPPORT_SEPARATOR}\n"
            f"> {link}\n"
            f"> {fight}\n"
            f"> {place}\n"
            f"{SUPPORT_SEPARATOR}\n\n"
            f"<@&{GW_TARGET_ROLE_ID}>"
        )
        try:
            posted = await inter.channel.send(
                content=announcement,
                allowed_mentions=disnake.AllowedMentions(
                    users=[disnake.Object(id=inter.author.id)],
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
                content=f"✅ GW опубликован: {posted.jump_url}\nРассылка в ЛС выключена."
            )
            return

        # Ссылка на исходное сообщение приходит всем вместе с текстом заявки.
        dm_text = (
            f"{inter.author.mention} NEED HELP 🎃\n"
            f"{SUPPORT_SEPARATOR}\n"
            f"> {link}\n"
            f"> {fight}\n"
            f"> {place}\n"
            f"{SUPPORT_SEPARATOR}\n"
            f"Сообщение на сервере: {posted.jump_url}"
        )
        now = time.monotonic()
        for token, entry in list(GW_CONFIRMATIONS.items()):
            if now - entry["created"] > GW_CONFIRM_EXPIRES:
                del GW_CONFIRMATIONS[token]

        token = secrets.token_hex(8)
        GW_CONFIRMATIONS[token] = {
            "guild_id": inter.guild.id,
            "author_id": inter.author.id,
            "text": dm_text,
            "created": now,
        }
        await inter.edit_original_response(
            content=(
                f"✅ Сообщение опубликовано: {posted.jump_url}\n\n"
                "**Подтвердить рассылку в ЛС только участникам с ролью** "
                f"<@&{GW_TARGET_ROLE_ID}>?\n"
                "Ботов пропустим; пользователи с закрытыми ЛС не получат сообщение. "
                "Передумал — нажми «Отмена»."
            ),
            components=[
                disnake.ui.ActionRow(
                    disnake.ui.Button(
                        label="Подтвердить рассылку",
                        style=disnake.ButtonStyle.danger,
                        custom_id=f"gw:confirm:{token}",
                    ),
                    disnake.ui.Button(
                        label="Отмена",
                        style=disnake.ButtonStyle.secondary,
                        custom_id=f"gw:cancel:{token}",
                    ),
                )
            ],
        )


async def gw_send_to_members(guild, text, sender):
    """Отправляем ЛС только участникам с GW_TARGET_ROLE_ID, с паузами."""
    sent = 0
    skipped_roles = 0
    failed = 0
    skipped_bots = 0
    try:
        async for member in guild.fetch_members(limit=None):
            if member.bot:
                skipped_bots += 1
                continue
            if not any(role.id == GW_TARGET_ROLE_ID for role in member.roles):
                skipped_roles += 1
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
            "отправлено: %s, закрытые ЛС/ошибки: %s, исключено по роли: %s, боты: %s",
            sender,
            guild.id,
            sent,
            failed,
            skipped_roles,
            skipped_bots,
        )


@bot.listen("on_button_click")
async def gw_confirmation_buttons(inter: disnake.MessageInteraction):
    custom_id = inter.data.custom_id or ""
    if not custom_id.startswith("gw:"):
        return

    parts = custom_id.split(":")
    if len(parts) != 3 or parts[1] not in ("confirm", "cancel"):
        return

    entry = GW_CONFIRMATIONS.get(parts[2])
    if not entry or time.monotonic() - entry["created"] > GW_CONFIRM_EXPIRES:
        GW_CONFIRMATIONS.pop(parts[2], None)
        await inter.response.send_message(
            "Подтверждение устарело. Создай новую заявку через /gw.",
            ephemeral=True,
        )
        return

    if not inter.guild or inter.guild.id != entry["guild_id"]:
        await inter.response.send_message("Другая гильдия.", ephemeral=True)
        return

    if inter.author.id != entry["author_id"]:
        await inter.response.send_message(
            "Подтвердить может только автор заявки.", ephemeral=True
        )
        return

    if parts[1] == "cancel":
        GW_CONFIRMATIONS.pop(parts[2], None)
        await inter.response.edit_message(
            content="Рассылка отменена. Сообщение в канале осталось.",
            components=[],
        )
        return

    if not has_gw_role(inter.author):
        await inter.response.send_message(
            "Рассылка доступна только указанным ролям.", ephemeral=True
        )
        return

    if any(not task.done() for task in GW_RUNNING_TASKS):
        await inter.response.send_message(
            "Уже выполняется другая рассылка. Повтори попытку позже.",
            ephemeral=True,
        )
        return

    # Без Server Members Intent бот не сможет надёжно определить всех получателей.
    if not bot.intents.members:
        await inter.response.send_message(
            "Включи Server Members Intent для рассылки.", ephemeral=True
        )
        return

    GW_CONFIRMATIONS.pop(parts[2], None)
    await inter.response.defer(ephemeral=True, with_message=True)
    task = asyncio.create_task(
        gw_send_to_members(inter.guild, entry["text"], inter.author.id)
    )
    GW_RUNNING_TASKS.add(task)
    task.add_done_callback(GW_RUNNING_TASKS.discard)
    await inter.edit_original_response(
        content=(
            "✅ Рассылка запущена. Бот отправит сообщения в ЛС "
            f"участникам с ролью <@&{GW_TARGET_ROLE_ID}>; ход выполнения виден в консоли."
        ),
        components=[],
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

    # Открываем форму сразу, без промежуточного сообщения.
    await inter.response.send_modal(GWModal())


# ================= МАССЛОГ /MS =================

MASSLOG_ROLE_ID = 1526993470638522588

# Только эти роли могут вызывать slash-команду /ms.
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
    """Публикует MASSLOG. Доступ только у четырёх разрешённых ролей."""
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
        "# MASSLOG EVERYONE 🍭\\n\\n"
        f"{MASSLOG_DECORATION}\\n\\n"
        "> МАССЛОГ — ЭТО СИГНАЛ ВЫЙТИ С СЕРВЕРА\\n\\n"
        f"{MASSLOG_DECORATION}\\n\\n"
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


# ================= ЗАПУСК =================

@bot.event
async def on_ready():
    logging.info("Бот тикетов %s запущен.", bot.user)
    logging.info("Файл бота: %s", Path(__file__).resolve())
    logging.info("База тикетов: %s", DB_PATH)
    logging.info("GUILD_ID регистрации: %s", GUILD_ID or "глобально")
    logging.info(
        "Локальные slash-команды: %s",
        ", ".join(sorted(command.name for command in bot.application_commands_iterator())),
    )

    # При ошибке 'This command could not be processed' смотри эти строки.
    # Discord может показывать устаревшую команду из другой версии бота.
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
            for required_name in ("supp", "gw"):
                if required_name not in registered_names:
                    logging.error(
                        "Команда /%s отсутствует в Discord. Проверь синхронизацию, "
                        "GUILD_ID и отсутствие второй копии бота.",
                        required_name,
                    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if not TOKEN:
        raise SystemExit(
            "Не задан TICKET_BOT_TOKEN (или DISCORD_TOKEN). "
            "Укажите токен бота в переменных окружения."
        )

    bot.run(TOKEN)

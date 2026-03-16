"""
Telegram‑бот с курсами валют и выбором источника через reply‑клавиатуру.

Возможности:
- 3 источника курсов (ЦБ РФ, ExchangeRate-API, open.er-api.com).
- Кнопки выбора источника внизу (ReplyKeyboardMarkup).
- Хранение активного источника в settings.json (переживает перезапуск).
- Периодические уведомления с курсами (APScheduler).
- Уведомления только подписчикам, chat_id хранятся в subscribers.json.
- Сообщения не редактируются и не удаляются — только новые ответы.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Tuple, Optional, Dict, Any

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChatAdministrators,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from api_client import fetch_rates
from aiogram.exceptions import TelegramForbiddenError  # обработчик ошибок
from aiogram.enums import ChatMemberStatus

# ==========================
# Загрузка настроек и логирование
# ==========================


load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

raw_admin_ids = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {int(x) for x in raw_admin_ids.split(",") if x.strip().isdigit()}

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Проверь .env: BOT_TOKEN должен быть задан.")

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

# Интервал отправки уведомлений с курсами (минуты)
NOTIFICATION_INTERVAL_MINUTES = 30

# Порог по изменению курсов (в рублях)
MIN_DIFF_USD_RUB = 0.5
MIN_DIFF_EUR_RUB = 0.5
MIN_DIFF_CNY_RUB = 0.5

LAST_RATES_PATH = Path("last_rates.json")


# Порог курса USD для уведомлений (если выше — шлём уведомление)
USD_THRESHOLD = 78.0

# Файлы для хранения настроек (переживают перезапуски)
SETTINGS_PATH = Path("settings.json")
SUBSCRIBERS_PATH = Path("subscribers.json")
DEFAULT_SOURCE_KEY = "cbr"


# Функция преобразования формата интервала
def format_interval(minutes: int) -> str:
    """Преобразовать интервал в минутах в человекочитаемый текст на русском."""
    hours = minutes // 60
    mins = minutes % 60

    parts = []
    if hours == 1:
        parts.append("1 час")
    elif hours in (2, 3, 4):
        parts.append(f"{hours} часа")
    elif hours > 0:
        parts.append(f"{hours} часов")

    if mins == 1:
        parts.append("1 минуту")
    elif mins in (2, 3, 4):
        parts.append(f"{mins} минуты")
    elif mins > 0:
        parts.append(f"{mins} минут")

    return " и ".join(parts) if parts else "менее минуты"


# ==========================
# Описание источников курсов
# ==========================


SOURCES: Dict[str, Dict[str, Any]] = {
    "cbr": {
        "title": "ЦБ РФ",
        "label": "Курсы валют (ЦБ РФ)",
        "mode": "direct_cbr",
        "url": "https://www.cbr-xml-daily.ru/daily_json.js",
    },
    "exchangerate_api": {
        "title": "ExchangeRate-API",
        "label": "Курсы валют (ExchangeRate-API)",
        "mode": "rub_base_inverted",
        "url": "https://api.exchangerate-api.com/v4/latest/RUB",
    },
    "open_er_api": {
        "title": "open.er-api.com",
        "label": "Курсы валют (open.er-api.com)",
        "mode": "rub_base_inverted",
        "url": "https://open.er-api.com/v6/latest/RUB",
    },
}


# ==========================
# Клавиатура с источниками (reply-клавиатура)
# ==========================


sources_reply_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="ЦБ РФ"),
            KeyboardButton(text="ExchangeRate-API"),
            KeyboardButton(text="open.er-api.com"),
        ],
    ],
    resize_keyboard=True,
)


# ==========================
# Работа с конфигом (active source)
# ==========================


def load_current_source() -> str:
    """Загрузить ключ активного источника из settings.json.

    Returns:
        str: Ключ источника из словаря SOURCES или DEFAULT_SOURCE_KEY
        при отсутствии файла/ошибке.
    """
    if not SETTINGS_PATH.exists():
        return DEFAULT_SOURCE_KEY
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("current_source_key", DEFAULT_SOURCE_KEY)
    except Exception as e:
        logger.error(f"Ошибка чтения settings.json: {e}")
        return DEFAULT_SOURCE_KEY


def save_current_source(source_key: str) -> None:
    """Сохранить ключ активного источника в settings.json.

    Args:
        source_key: Ключ источника из словаря SOURCES.
    """
    data = {"current_source_key": source_key}
    try:
        with SETTINGS_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка записи settings.json: {e}")


CURRENT_SOURCE_KEY = DEFAULT_SOURCE_KEY  # обновляется в main()


# ==========================
# Подписчики (chat_id)
# ==========================


def load_subscribers() -> set[int]:
    """Загрузить множество подписчиков из subscribers.json.

    Returns:
        set[int]: Множество chat_id или пустое множество при ошибке.
    """
    if not SUBSCRIBERS_PATH.exists():
        return set()
    try:
        with SUBSCRIBERS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return set(map(int, data.get("subscribers", [])))
    except Exception as e:
        logger.error(f"Ошибка чтения subscribers.json: {e}")
        return set()


def save_subscribers(subscribers: set[int]) -> None:
    """Сохранить множество подписчиков в subscribers.json.

    Args:
        subscribers: Множество chat_id подписанных чатов.
    """
    try:
        with SUBSCRIBERS_PATH.open("w", encoding="utf-8") as f:
            json.dump(
                {"subscribers": list(subscribers)}, f, ensure_ascii=False, indent=2
            )
    except Exception as e:
        logger.error(f"Ошибка записи subscribers.json: {e}")


SUBSCRIBERS: set[int] = set()  # обновляется в main()


# ==========================
# Получение курсов
# ==========================


def get_rates_from_source(
    source_key: str,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Получить курсы USD, EUR и CNY к RUB из выбранного источника.

    Поддерживаются два формата:
        - direct_cbr: курсы ЦБ РФ (USD/EUR/CNY к RUB).
        - rub_base_inverted: база RUB, значения инвертируются.

    Args:
        source_key: Ключ источника в словаре SOURCES.

    Returns:
        tuple[float | None, float | None, float | None]:
        (usd_rub, eur_rub, cny_rub) или (None, None, None) при ошибке.
    """
    src = SOURCES.get(source_key)
    if not src:
        logger.error(f"Неизвестный источник курсов: {source_key}")
        return None, None, None

    try:
        resp = requests.get(src["url"], timeout=10)
        data = resp.json()
    except Exception as e:
        logger.error(f"Ошибка HTTP/JSON из источника {source_key}: {e}")
        return None, None, None

    mode = src.get("mode")

    try:
        if mode == "direct_cbr":
            usd_rub = float(data["Valute"]["USD"]["Value"])
            eur_rub = float(data["Valute"]["EUR"]["Value"])
            cny_rub = float(data["Valute"]["CNY"]["Value"])
            return usd_rub, eur_rub, cny_rub

        if mode == "rub_base_inverted":
            usd_per_rub = float(data["rates"]["USD"])
            eur_per_rub = float(data["rates"]["EUR"])
            cny_per_rub = float(data["rates"]["CNY"])

            if usd_per_rub == 0 or eur_per_rub == 0 or cny_per_rub == 0:
                raise ValueError("Нулевое значение курса в rub_base_inverted")

            usd_rub = 1.0 / usd_per_rub
            eur_rub = 1.0 / eur_per_rub
            cny_rub = 1.0 / cny_per_rub
            return usd_rub, eur_rub, cny_rub

        logger.error(f"Неизвестный mode для источника {source_key}: {mode}")
        return None, None, None

    except Exception as e:
        logger.error(f"Ошибка обработки JSON из источника {source_key}: {e}")
        return None, None, None


def format_rates_block(
    source_key: str,
    usd: float,
    eur: float,
    cny: float,
    with_header: bool = True,
    precision: int = 4,
) -> Tuple[str, str, str, str]:
    """
    Сформировать блоки текста с курсами для выбранного источника.

    Returns:
        header, usd_text, eur_text, cny_text
    """
    src = SOURCES[source_key]
    fmt = f"{{:.{precision}f}}"

    header = f"📊 <b>{src['label']}</b>" if with_header else ""

    usd_text = f"🇺🇸 USD: {fmt.format(usd)} ₽"
    eur_text = f"🇪🇺 EUR: {fmt.format(eur)} ₽"
    cny_text = f"🇨🇳 CNY: {fmt.format(cny)} ₽"

    return header, usd_text, eur_text, cny_text


def load_last_rates() -> Optional[dict]:
    """Загрузить последние курсы USD/EUR/CNY из last_rates.json."""
    if not LAST_RATES_PATH.exists():
        return None
    try:
        with LAST_RATES_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception as e:
        logger.error(f"Ошибка чтения last_rates.json: {e}")
        return None


def save_last_rates(usd: float, eur: float, cny: float) -> None:
    """Сохранить последние курсы USD/EUR/CNY в last_rates.json."""
    data = {
        "last_usd": usd,
        "last_eur": eur,
        "last_cny": cny,
    }
    try:
        with LAST_RATES_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка записи last_rates.json: {e}")


from aiogram.types import BotCommandScopeDefault, BotCommandScopeChatAdministrators


async def set_commands() -> None:
    commands = [
        BotCommand(command="start", description="Запуск бота"),
        BotCommand(command="rate", description="Показать курсы по активному источнику"),
        BotCommand(command="interval", description="Показ интервала уведомлений"),
        BotCommand(command="subscribe", description="Подписаться на уведомления"),
        BotCommand(command="unsubscribe", description="Отписаться от уведомлений"),
        BotCommand(command="settings", description="Показать текущие настройки бота"),
        BotCommand(command="support", description="Чат поддержки бота"),
        BotCommand(command="stats", description="Статистика бота (для админа)"),
    ]
    await bot.set_my_commands(commands, BotCommandScopeDefault())


# ==========================
# Хэндлеры команд
# ==========================


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Обработать команду /start и показать клавиатуру с источниками."""
    await message.answer(
        "Привет! Я бот с курсами валют.\n\n"
        "Выбери источник курсов с помощью кнопок снизу,\n"
        "а затем используй /rate, чтобы посмотреть курсы.\n\n"
        "Чтобы получать уведомления по расписанию, используй /subscribe.\n"
        "Для отмены — /unsubscribe.",
        reply_markup=sources_reply_keyboard,
    )

    print("\n" + "=" * 50)
    print("ПОЛУЧЕН /start")
    print(f"Chat ID: {message.chat.id}")
    print(f"Chat type: {message.chat.type}")

    user = message.from_user
    if user is not None:
        print(f"User ID: {user.id}")
        print(f"User: {user.full_name} (@{user.username or 'no username'})")
    else:
        print("User: <unknown>")

    print("=" * 50 + "\n")


@dp.message(Command("rate"))
async def cmd_rate(message: Message) -> None:
    """Получить курсы через backend API."""
    data = await fetch_rates(CURRENT_SOURCE_KEY)
    if data is None:
        await message.answer(
            "Не удалось получить курсы. Попробуй позже.",
            reply_markup=sources_reply_keyboard,
        )
        return

    header = f"📊 <b>{data['source_title']}</b>"
    usd_text = f"🇺🇸 USD: {data['usd_rub']:.4f} ₽"
    eur_text = f"🇪🇺 EUR: {data['eur_rub']:.4f} ₽"
    cny_text = f"🇨🇳 CNY: {data['cny_rub']:.4f} ₽"

    await message.answer(header, reply_markup=sources_reply_keyboard)
    await message.answer(usd_text)
    await message.answer(eur_text)
    await message.answer(cny_text)


@dp.message(Command("interval"))
async def cmd_interval(message: Message) -> None:
    """Обработать команду /interval и показать интервал уведомлений."""
    interval_text = format_interval(NOTIFICATION_INTERVAL_MINUTES)

    text = (
        "🔔 <b>Настройки уведомлений</b>\n\n"
        "Уведомления с курсами валют приходят:\n"
        f"⏰ <b>Каждые {interval_text}</b>\n\n"
        "Изменить интервал можно в коде, параметр NOTIFICATION_INTERVAL_MINUTES."
    )
    await message.answer(text, reply_markup=sources_reply_keyboard)


@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message) -> None:
    """Обработать команду /subscribe и подписать чат на уведомления."""
    global SUBSCRIBERS
    chat_id = message.chat.id

    if chat_id in SUBSCRIBERS:
        await message.answer(
            "✅ Ты уже подписан на уведомления с курсами.",
            reply_markup=sources_reply_keyboard,
        )
        return

    SUBSCRIBERS.add(chat_id)
    save_subscribers(SUBSCRIBERS)
    logger.info(f"Подписан (через /subscribe) чат {chat_id}")

    interval_text = format_interval(NOTIFICATION_INTERVAL_MINUTES)

    await message.answer(
        "✅ Подписка на уведомления с курсами оформлена.\n"
        f"Буду присылать уведомления по расписанию: каждые {interval_text}.",
        reply_markup=sources_reply_keyboard,
    )


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message) -> None:
    """Обработать команду /unsubscribe и отписать чат от уведомлений."""
    global SUBSCRIBERS
    chat_id = message.chat.id

    if chat_id not in SUBSCRIBERS:
        await message.answer(
            "ℹ️ Ты и так не подписан на уведомления.",
            reply_markup=sources_reply_keyboard,
        )
        return

    SUBSCRIBERS.discard(chat_id)
    save_subscribers(SUBSCRIBERS)
    logger.info(f"Отписан (через /unsubscribe) чат {chat_id}")

    await message.answer(
        "❌ Подписка на уведомления отключена.",
        reply_markup=sources_reply_keyboard,
    )


@dp.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    """Показать текущие настройки бота (человекочитаемо по‑русски)."""
    text = (
        "⚙️ <b>Текущие настройки бота</b>\n\n"
        f"• Минимальное изменение USD: <b>{MIN_DIFF_USD_RUB:.2f} ₽</b>\n"
        f"• Минимальное изменение EUR: <b>{MIN_DIFF_EUR_RUB:.2f} ₽</b>\n"
        f"• Минимальное изменение CNY: <b>{MIN_DIFF_CNY_RUB:.2f} ₽</b>\n\n"
        f"• Интервал уведомлений: <b>{format_interval(NOTIFICATION_INTERVAL_MINUTES)}</b>\n"
        f"• Текущий источник: <b>{SOURCES[CURRENT_SOURCE_KEY]['title']}</b>\n"
    )

    await message.answer(text, reply_markup=sources_reply_keyboard)


@dp.message(Command("support"))
async def cmd_support(message: Message) -> None:
    """Показать ссылку на чат поддержки."""
    text = (
        "☎️ <b>Поддержка бота</b>\n\n"
        "По вопросам работы бота и предложениям по доработке "
        "пиши в чат поддержки:\n\n"
        "https://t.me/+x1k9TlQPtZ05Y2Vi\n\n"
        "После перехода по ссылке отправь запрос на вступление — "
        "администратор одобрит, и можно будет писать сообщения в чат."
    )
    await message.answer(text, reply_markup=sources_reply_keyboard)


@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Показать статистику по боту (только для админа)."""
    user = message.from_user
    if user is None or user.id not in ADMIN_IDS:
        await message.answer("Эта команда доступна только администратору.")
        return

    total_subscribers = len(SUBSCRIBERS)

    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"Подписчиков на уведомления: <b>{total_subscribers}</b>\n"
    )

    await message.answer(text, reply_markup=sources_reply_keyboard)


# ==========================
# Хэндлеры кнопок источников
# ==========================


@dp.message(F.text.in_(["ЦБ РФ", "ExchangeRate-API", "open.er-api.com"]))
async def handle_source_buttons(message: Message) -> None:
    global CURRENT_SOURCE_KEY

    if message.text is None:
        return

    mapping: dict[str, str] = {
        "ЦБ РФ": "cbr",
        "ExchangeRate-API": "exchangerate_api",
        "open.er-api.com": "open_er_api",
    }

    source_key = mapping.get(message.text)
    if source_key is None:
        return

    CURRENT_SOURCE_KEY = source_key
    save_current_source(CURRENT_SOURCE_KEY)

    await message.answer(
        f"Источник сменён на <b>{SOURCES[source_key]['title']}</b>.",
        reply_markup=sources_reply_keyboard,
    )

    data = await fetch_rates(source_key)
    if data is None:
        await message.answer(
            f"Не удалось получить курсы с источника "
            f"<b>{SOURCES[source_key]['title']}</b>. Попробуй позже."
        )
        return

    header = f"📊 <b>{data['source_title']}</b>"
    usd_text = f"🇺🇸 USD: {data['usd_rub']:.4f} ₽"
    eur_text = f"🇪🇺 EUR: {data['eur_rub']:.4f} ₽"
    cny_text = f"🇨🇳 CNY: {data['cny_rub']:.4f} ₽"

    await message.answer(header)
    await message.answer(usd_text)
    await message.answer(eur_text)
    await message.answer(cny_text)


# ==========================
# Обработчик прочих сообщений
# ==========================


@dp.message()
async def any_message(message: Message) -> None:
    """Обработать произвольное текстовое сообщение без известной команды.

    Подсказывает, как пользоваться ботом, и показывает клавиатуру источников.
    """
    if message.text and message.text.startswith("/"):
        return

    await message.answer(
        "Выбери источник курсов кнопками снизу\n"
        "или воспользуйся командами:\n"
        "• /rate — показать курсы\n"
        "• /interval — интервал уведомлений\n"
        "• /subscribe — подписаться на уведомления\n"
        "• /unsubscribe — отписаться от уведомлений.",
        reply_markup=sources_reply_keyboard,
    )


# ==========================
# Функция уведомления по расписанию
# ==========================


async def send_daily_rates() -> None:
    """Отправить уведомление с курсами всем подписчикам.

    Логика:
    - первый раз (нет файла) всегда шлём и сохраняем текущие значения;
    - дальше шлём, если по любой из валют (USD/EUR/CNY)
      изменение курса в рублях >= своего порога MIN_DIFF_*.
    """
    if not SUBSCRIBERS:
        logger.info("Нет подписчиков, уведомления не отправляем.")
        return

    usd, eur, cny = get_rates_from_source(CURRENT_SOURCE_KEY)
    if usd is None or eur is None or cny is None:
        logger.warning("Не удалось получить курсы для уведомления.")
        return

    last = load_last_rates()

    if last is None:
        logger.info(
            "Первое уведомление: файла last_rates.json нет, "
            f"USD = {usd:.4f} ₽, EUR = {eur:.4f} ₽, CNY = {cny:.4f} ₽."
        )
        change_lines: list[str] = [
            "Первое уведомление, сохранены базовые значения курсов."
        ]
        send_reason = "Первое уведомление: нет сохранённых прошлых значений."
    else:
        last_usd = float(last.get("last_usd", usd))
        last_eur = float(last.get("last_eur", eur))
        last_cny = float(last.get("last_cny", cny))

        usd_diff = usd - last_usd
        eur_diff = eur - last_eur
        cny_diff = cny - last_cny

        usd_abs = abs(usd_diff)
        eur_abs = abs(eur_diff)
        cny_abs = abs(cny_diff)

        change_lines: list[str] = []

        if usd_abs >= MIN_DIFF_USD_RUB:
            direction = "📈 вырос" if usd_diff > 0 else "📉 снизился"
            change_lines.append(
                f"{direction} USD на {usd_abs:.2f} ₽ "
                f"(с {last_usd:.2f} ₽ до {usd:.2f} ₽)"
            )

        if eur_abs >= MIN_DIFF_EUR_RUB:
            direction = "📈 вырос" if eur_diff > 0 else "📉 снизился"
            change_lines.append(
                f"{direction} EUR на {eur_abs:.2f} ₽ "
                f"(с {last_eur:.2f} ₽ до {eur:.2f} ₽)"
            )

        if cny_abs >= MIN_DIFF_CNY_RUB:
            direction = "📈 вырос" if cny_diff > 0 else "📉 снизился"
            change_lines.append(
                f"{direction} CNY на {cny_abs:.2f} ₽ "
                f"(с {last_cny:.2f} ₽ до {cny:.2f} ₽)"
            )

        if not change_lines:
            logger.info(
                "Изменения курсов слишком маленькие: "
                f"USD Δ={usd_abs:.4f} ₽, EUR Δ={eur_abs:.4f} ₽, CNY Δ={cny_abs:.4f} ₽ — "
                "уведомление не отправляем."
            )
            return

        send_reason = " / ".join(change_lines)

    src = SOURCES[CURRENT_SOURCE_KEY]

    # 1. Отдельное сообщение с причиной уведомления
    reason_text = (
        f"📅 <b>{src['label']}</b>\n\n"
        + "\n".join(change_lines)
        + f"\n\nПричина уведомления:\n{send_reason}"
    )

    # 2. Сообщения с курсами через хелпер
    header_text, usd_text, eur_text, cny_text = format_rates_block(
        CURRENT_SOURCE_KEY,
        usd,
        eur,
        cny,
        with_header=True,
        precision=2,
    )

    sent = 0
    removed: set[int] = set()

    for chat_id in list(SUBSCRIBERS):
        try:
            # Сначала причина
            await bot.send_message(chat_id=chat_id, text=reason_text)
            # Потом шапка и три валюты
            await bot.send_message(chat_id=chat_id, text=header_text)
            await bot.send_message(chat_id=chat_id, text=usd_text)
            await bot.send_message(chat_id=chat_id, text=eur_text)
            await bot.send_message(chat_id=chat_id, text=cny_text)
            sent += 1
        except TelegramForbiddenError as e:
            logger.warning(
                f"Чат {chat_id} заблокировал бота, удаляем из подписчиков: {e}"
            )
            removed.add(chat_id)
        except Exception as e:
            logger.error(f"Ошибка отправки в чат {chat_id}: {e}")
    if removed:
        for chat_id in removed:
            SUBSCRIBERS.discard(chat_id)
        save_subscribers(SUBSCRIBERS)
        logger.info(f"Удалено из подписчиков (заблокировали бота): {len(removed)}")

    save_last_rates(usd, eur, cny)
    logger.info(
        f"Уведомление отправлено {sent} подписчикам, "
        f"last_rates обновлены до USD={usd:.4f}, EUR={eur:.4f}, CNY={cny:.4f}."
    )


# ==========================
# Точка входа
# ==========================


async def main() -> None:
    """Инициализировать состояние и запустить бота.

    Действия:
        1. Загрузить активный источник и подписчиков.
        2. Зарегистрировать команды бота.
        3. Настроить и запустить планировщик уведомлений.
        4. Запустить polling aiogram.
    """
    global CURRENT_SOURCE_KEY, SUBSCRIBERS

    CURRENT_SOURCE_KEY = load_current_source()
    SUBSCRIBERS = load_subscribers()
    logger.info(f"Текущий источник при старте бота: {CURRENT_SOURCE_KEY}")
    logger.info(f"Загружено подписчиков: {len(SUBSCRIBERS)}")

    await set_commands()

    scheduler.add_job(
        send_daily_rates,
        trigger="interval",
        minutes=NOTIFICATION_INTERVAL_MINUTES,
    )
    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")

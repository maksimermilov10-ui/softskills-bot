import os
import logging
import threading
import http.server
import socketserver
from datetime import datetime, timedelta
from typing import List, Union, Optional

from fastapi import FastAPI, Request, Response
from http import HTTPStatus
from contextlib import asynccontextmanager

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ChatAction

# ===== Логи =====
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("softskills-bot")

# ===== Конфигурация =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit('Не задан BOT_TOKEN. Выполнить: export BOT_TOKEN="<токен>"')

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

TEST_LINK = "https://softskills.rsv.ru/"

# callback-ключи
CB_TEST = "test"
CB_GUIDE_OPEN = "guide_open"
CB_GUIDE_FAST = "guide_fast"
CB_GUIDE_NEXT = "guide_next"
CB_GUIDE_PREV = "guide_prev"
CB_GUIDE_MENU = "guide_menu"
CB_EVENTS = "events"

# ===== Данные по анонсам =====
EVENTS: List[dict] = [
    # Пример:
    # {"title": "Бизнес‑день в Губкинском", "date": "09.11, 14:00", "link": "https://t.me/gubkinsoft"},
]

# Фото капибары
CAPYBARA_PHOTO_URL = (
    "https://drive.google.com/uc?export=view&id=1iMD-ztr-hyo3GRn-z-XpJGevGeg0Pswh"
)

# ===== Разметка =====
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Пройти тестирование", callback_data=CB_TEST)],
            [InlineKeyboardButton("Инструкция (по шагам)", callback_data=CB_GUIDE_OPEN)],
            [InlineKeyboardButton("Уже зарегистрирован(а)", callback_data=CB_GUIDE_FAST)],
            [InlineKeyboardButton("Ближайшие анонсы и мероприятия", callback_data=CB_EVENTS)],
        ]
    )


def kb_guide(idx: int, last: int) -> InlineKeyboardMarkup:
    row = []
    if idx > 0:
        row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"{CB_GUIDE_PREV}:{idx-1}"))
    if idx < last:
        row.append(InlineKeyboardButton("Далее ➡️", callback_data=f"{CB_GUIDE_NEXT}:{idx+1}"))
    nav = [row] if row else []
    return InlineKeyboardMarkup(nav + [[InlineKeyboardButton("Главное меню", callback_data=CB_GUIDE_MENU)]])


# ===== Контент шагов =====
GUIDE_TEXTS: List[str] = [
    "1) Регистрация на платформе\n\n"
    "Создай личный кабинет на платформе «Россия – страна возможностей». "
    "Нажми «Зарегистрироваться» и заполни форму. На почту придёт код — введи его в поле «Код подтверждения».",
    "2) Заполнение анкеты\n\n"
    "Внимательно заполни все обязательные поля. В блоке «Образование» и в разделе «Прочее» "
    "обязательно укажи свой Центр компетенций.",
    "3) Прохождение тестирования\n\n"
    "• Этапы: регистрация, базовая диагностика, дополнительная.\n"
    "• База: 5 базовых тестов + анкета. Дополнительно: 4 теста (добавляют компетенции в сводный отчёт).\n"
    "• Тесты можно проходить в любой последовательности и удобное время.\n"
    "• Используй ноутбук/ПК и стабильный интернет.\n"
    "• Перед каждым тестом есть инструкция; часть тестов с лимитом времени.\n"
    "• Отчёты появятся в личном кабинете в течение 48 часов; затем можно выгрузить на hh.ru.",
    f"4) Перейти на сайт\n\nЗайди с компьютера: {TEST_LINK}\n"
    "Вводи данные и ОБЯЗАТЕЛЬНО указывай e‑mail (не телефон).",
    "5) Нажать «Начать» и заполнить анкету\n\n"
    "Заполни ФИО и остальные данные. В «Образовании» укажи:\n"
    "— Учебное заведение: ФГАОУ ВО «РОССИЙСКИЙ ГОСУДАРСТВЕННЫЙ УНИВЕРСИТЕТ НЕФТИ И ГАЗА "
    "(НАЦИОНАЛЬНЫЙ ИССЛЕДОВАТЕЛЬСКИЙ УНИВЕРСИТЕТ) ИМЕНИ И.М. ГУБКИНА» (Губкинский университет)\n"
    "— Центр компетенций: ЦЕНТР КОМПЕТЕНЦИЙ РГУ НЕФТИ И ГАЗА (НИУ) ИМЕНИ И.М. ГУБКИНА (РГУНГ)",
    "6) Финишная прямая! 🏁\n\n"
    "Пройди 5 базовых инструмента (синие маркеры):\n"
    "— Резильентность для студентов\n"
    "— Тест «Комплексный тест на анализ информации»\n"
    "— Личностный опросник\n"
    "— Ценностные ориентации\n"
    "— Тест на оценку мотиваторов и демотиваторов\n\n"
    "Остальные инструменты — откроются после прохождения основных.",
]


LAST_STEP = len(GUIDE_TEXTS) - 1


# ===== Прогресс пользователя =====
def get_saved_step(context: ContextTypes.DEFAULT_TYPE) -> int:
    return int(context.user_data.get("guide_step", 0))


def set_saved_step(context: ContextTypes.DEFAULT_TYPE, idx: int) -> None:
    context.user_data["guide_step"] = max(0, min(idx, LAST_STEP))


# ===== Показ шага =====
async def send_guide_step(msg_target, idx: int):
    header = f"Шаг {idx+1}/{LAST_STEP+1}"
    text = f"{header}\n\n{GUIDE_TEXTS[idx]}"
    kb = kb_guide(idx, LAST_STEP)
    media = None
    if idx < len(GUIDE_TEXTS) and isinstance(GUIDE_TEXTS[idx], list):
        media = GUIDE_TEXTS[idx]  # на случай если в твоем коде медиа списком
    else:
        media = None

    if isinstance(media, str):
        await msg_target.reply_photo(photo=media)
    elif isinstance(media, list) and media:
        await msg_target.reply_media_group(media=[InputMediaPhoto(m) for m in media])

    await msg_target.reply_text(text, reply_markup=kb)


# ===== Главное меню =====
async def show_main_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int, target_message_id: Optional[int] = None) -> None:
    text = "Главное меню. Выбирай действие:"

    if target_message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=target_message_id, text=text, reply_markup=kb_main()
            )
            context.user_data["last_menu_id"] = target_message_id
            return
        except Exception as e:
            log.info("Редактирование placeholder не удалось: %s — отправляем новое меню", e)
            context.user_data["last_menu_id"] = None

    sent = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb_main())
    context.user_data["last_menu_id"] = sent.message_id


# ===== /start =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    name = ((user.first_name or "").strip()) or "друг"

    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)

    greeting = f"Привет, {name}! Это бот для тестирования.\n\nГотовлю главное меню…"
    await context.bot.send_message(chat.id, greeting)
    await show_main_menu(context, chat.id)


# ===== /help =====
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команды:\n/start — главное меню\n/help — эта справка")


# ===== Обработчик кнопок =====
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_msg = query.message
    chat_id = chat_msg.chat.id

    if data == CB_TEST:
        msg = (
            "Тестирование проходит на компьютере.\n\n"
            f"1) Перейти на сайт: {TEST_LINK}\n"
            "2) Зарегистрироваться (или войти), указать e‑mail.\n"
            "3) Открыть раздел «Оценка компетенций» и нажать «Пройти тестирование».\n\n"
            "Нужна пошаговая инструкция? Нажми «Инструкция (по шагам)»."
        )
        sent = await chat_msg.reply_text(msg, reply_markup=kb_main())
        context.user_data["last_menu_id"] = sent.message_id
        return

    if data == CB_GUIDE_OPEN:
        idx = get_saved_step(context)
        await send_guide_step(chat_msg, idx)
        return

    if data == CB_GUIDE_FAST:
        idx = 3
        set_saved_step(context, idx)
        await send_guide_step(chat_msg, idx)
        return

    if data.startswith(CB_GUIDE_NEXT) or data.startswith(CB_GUIDE_PREV):
        try:
            _, idx_str = data.split(":")
            idx = int(idx_str)
        except Exception:
            idx = get_saved_step(context)
        set_saved_step(context, idx)
        await send_guide_step(chat_msg, idx)
        return

    if data == CB_EVENTS:
        try:
            await chat_msg.reply_photo(photo=CAPYBARA_PHOTO_URL)
        except Exception as e:
            log.warning("Не удалось отправить фото анонсов: %s", e)

        if not EVENTS:
            placeholder = (
                "Пока здесь пусто — команда уже подбирает самые интересные события. "
                "Как только появятся ближайшие мероприятия, бот первым сообщит ✨"
            )
            sent = await chat_msg.reply_text(placeholder, reply_markup=kb_main())
            context.user_data["last_menu_id"] = sent.message_id
            return

        lines = []
        buttons: List[List[InlineKeyboardButton]] = []
        for i, e in enumerate(EVENTS, start=1):
            title = e.get("title", "Событие")
            date = e.get("date", "")
            link = e.get("link")
            lines.append(f"{i}) {title}" + (f" — {date}" if date else ""))
            if link:
                buttons.append(
                    [InlineKeyboardButton(f"Открыть: {title}", url=link)]
                )
        kb = InlineKeyboardMarkup(
            buttons + [[InlineKeyboardButton("Главное меню", callback_data=CB_GUIDE_MENU)]]
        )
        sent = await chat_msg.reply_text("\n".join(lines), reply_markup=kb)
        context.user_data["last_menu_id"] = sent.message_id
        return

    if data == CB_GUIDE_MENU:
        await show_main_menu(context, chat_id)
        return


# ===== Основное инициализирующее приложение FastAPI и PTB =====

ptb = (
    Application.builder()
    .token(BOT_TOKEN)
    .updater(None)  # для webhook критично
    .build()
)

# Регистрируем обработчики
ptb.add_handler(CommandHandler("start", start))
ptb.add_handler(CommandHandler("help", help_cmd))
ptb.add_handler(CallbackQueryHandler(on_button))


async def post_init(application):
    try:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Запуск бота"),
                BotCommand("help", "Справка"),
            ]
        )
    except Exception as e:
        log.warning(f"set_my_commands не применены: {e}")


ptb.post_init = post_init


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not PUBLIC_BASE_URL:
        log.warning("PUBLIC_BASE_URL не задан — webhook не будет установлен")
    else:
        url = f"{PUBLIC_BASE_URL}/telegram-webhook"
        await ptb.bot.set_webhook(url)
        log.info(f"Webhook установлен по адресу {url}")

    async with ptb:
        await ptb.start()
        yield
        await ptb.stop()


app = FastAPI(lifespan=lifespan)


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, ptb.bot)
    await ptb.process_update(update)
    return Response(status_code=HTTPStatus.OK)


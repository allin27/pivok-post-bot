"""
PÎVOK Post Constructor Bot — Standalone Separate Project & Bot (RU + TR)
Supports BOTH:
  - 24/7 Cloud Mode (Render Free Web Service via Webhook, ~45 MB RAM, zero PC needed)
  - Local Windows Mode (Polling + auto Cloudflare Tunnel)
"""

import asyncio
import collections
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    Update,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.utils.web_app import safe_parse_webapp_init_data
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("pivok-post-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip() or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
SECRET_PASSWORD = os.environ.get("BOT_SECRET_PASSWORD", "pivok").strip()
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
SPACE_HOST = os.environ.get("SPACE_HOST", "").strip()
PUBLIC_BASE_URL = (
    os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    or RENDER_EXTERNAL_URL
    or (f"https://{SPACE_HOST}" if SPACE_HOST else "")
)
LOCAL_PORT = int(os.environ.get("PORT", "8085"))
WEBHOOK_SECRET = hashlib.sha256((BOT_TOKEN + ":pivok_post_wh").encode()).hexdigest()[:32] if BOT_TOKEN else "pivok"

DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "post_bot_state.json")

_TUNNEL_URL: Optional[str] = None
_SEEN_UPDATES = collections.deque(maxlen=300)


def get_active_webapp_base_url() -> str:
    if PUBLIC_BASE_URL.startswith("https://"):
        return PUBLIC_BASE_URL
    if _TUNNEL_URL and _TUNNEL_URL.startswith("https://"):
        return _TUNNEL_URL
    return f"http://localhost:{LOCAL_PORT}"


def _load_store() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_store(store: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


STORE = _load_store()
_env_ids = [
    int(x.strip())
    for x in os.environ.get("ALLOWED_USER_IDS", "7042397101").split(",")
    if x.strip().isdigit()
]
ALLOWED_USERS: list[int] = list(set(STORE.get("allowed_users", []) + _env_ids))
DRAFTS: Dict[str, dict] = STORE.setdefault("drafts", {})
USER_LANGS: Dict[str, str] = STORE.setdefault("user_langs", {})


def persist_store():
    STORE["allowed_users"] = ALLOWED_USERS
    STORE["drafts"] = DRAFTS
    STORE["user_langs"] = USER_LANGS
    _save_store(STORE)


# ==========================================
# RU + TR LOCALIZATION
# ==========================================

STRINGS = {
    "ru": {
        "btn_template": "✨ Создать шаблон PÎVOK",
        "btn_lang": "🇹🇷 Türkçe / Язык",
        "btn_cancel": "❌ Отмена",
        "btn_open_editor": "✏️ Открыть редактор кнопок",
        "btn_edit_text": "📝 Изменить текст поста",
        "btn_send_channel": "📨 Отправить в канал",
        "btn_send_pin": "📌 Отправить и закрепить",
        "btn_update_existing": "🔄 Обновить этот пост в канале",
        "btn_set_channel": "📡 Канал: {ch} (сменить)",
        "btn_no_channel": "📡 Указать канал для публикации",
        "welcome": (
            "🌸 **Конструктор постов и закрепки PÎVOK**\n\n"
            "• Нажмите **«✨ Создать шаблон PÎVOK»**, чтобы получить готовую шапку канала с цветными кнопками.\n"
            "• Или отправьте любой свой **текст / фото**, либо **перешлите готовый пост из канала** — бот сохранит все его ряды и цвета кнопок для редактирования!"
        ),
        "lang_prompt": "🌐 **Выберите язык / Lütfen dil seçin:**",
        "lang_switched": "🇷🇺 Язык переключён на **Русский**!",
        "preview_hint": "👆 **Предпросмотр вашего поста.** Нажмите **«✏️ Открыть редактор кнопок»**, чтобы добавить, перетащить или перекрасить кнопки.",
        "ask_new_text": "📝 Отправьте новый текст (или фото с подписью) — все настроенные кнопки сохранятся!",
        "ask_channel": (
            "📡 **Укажите канал PÎVOK для публикации:**\n\n"
            "Отправьте `@username` канала (например `@pivokstore`) или **перешлите сюда любое сообщение из нужного канала**.\n"
            "*(Не забудьте добавить этого бота в Администраторы канала!)*"
        ),
        "channel_saved": "✅ Канал сохранён: **{ch}**!",
        "draft_missing": "⚠️ Черновик не найден. Отправьте пост заново.",
        "sent_ok": "✅ Пост успешно опубликован в канале **{ch}**!",
        "sent_pinned_ok": "📌 Пост опубликован и **закреплён** в канале **{ch}**!",
        "updated_ok": "🔄 Кнопки у существующего поста в канале успешно обновлены!",
        "cancelled": "❌ Действие отменено.",
        "send_err": (
            "❌ Не удалось отправить в канал **{ch}**.\n"
            "Проверьте, что бот добавлен в **Администраторы** канала (с правом публикации и закрепления).\n\n"
            "Детали: `{err}`"
        ),
    },
    "tr": {
        "btn_template": "✨ Hazır PÎVOK Şablonu",
        "btn_lang": "🇷🇺 Русский / Dil",
        "btn_cancel": "❌ İptal",
        "btn_open_editor": "✏️ Buton Editörünü Aç",
        "btn_edit_text": "📝 Post Metnini Değiştir",
        "btn_send_channel": "📨 Kanala Gönder",
        "btn_send_pin": "📌 Gönder ve Sabitle",
        "btn_update_existing": "🔄 Kanaldaki Postu Güncelle",
        "btn_set_channel": "📡 Kanal: {ch} (değiştir)",
        "btn_no_channel": "📡 Yayın Kanalını Belirt",
        "welcome": (
            "🌸 **PÎVOK Sabit Post & Buton Tasarımcısı**\n\n"
            "• Hazır renkli butonlu karşılama postu için **«✨ Hazır PÎVOK Şablonu»** butonuna basın.\n"
            "• Veya kendi **metninizi / fotoğrafınızı** gönderin ya da **kanaldan mevcut bir postu iletin** — tüm butonları ve renkleri düzenlemek için aktarılır!"
        ),
        "lang_prompt": "🌐 **Lütfen dil seçin / Выберите язык:**",
        "lang_switched": "🇹🇷 Dil **Türkçe** olarak değiştirildi!",
        "preview_hint": "👆 **Post önizlemesi.** Buton eklemek, sıralamak veya renklendirmek için **«✏️ Buton Editörünü Aç»** butonuna basın.",
        "ask_new_text": "📝 Yeni metni (veya açıklamalı fotoğrafı) gönderin — ayarladığınız tüm butonlar korunacaktır!",
        "ask_channel": (
            "📡 **Yayınlanacak PÎVOK kanalını belirtin:**\n\n"
            "Kanalın `@kullaniciadi` adresini yazın (örnek: `@pivokstore`) veya **kanaldan herhangi bir mesajı buraya iletin**.\n"
            "*(Lütfen bu botu kanala Yönetici olarak eklemeyi unutmayın!)*"
        ),
        "channel_saved": "✅ Kanal kaydedildi: **{ch}**!",
        "draft_missing": "⚠️ Taslak bulunamadı. Lütfen postu tekrar gönderin.",
        "sent_ok": "✅ Post **{ch}** kanalında başarıyla yayınlandı!",
        "sent_pinned_ok": "📌 Post **{ch}** kanalında yayınlandı ve **sabitlendi**!",
        "updated_ok": "🔄 Kanaldaki mevcut postun butonları başarıyla güncellendi!",
        "cancelled": "❌ İşlem iptal edildi.",
        "send_err": (
            "❌ **{ch}** kanalına gönderilemedi.\n"
            "Botun kanalda **Yönetici** olduğundan (mesaj gönderme ve sabitleme yetkisiyle) emin olun.\n\n"
            "Detay: `{err}`"
        ),
    },
}

TEMPLATE_TEXTS = {STRINGS["ru"]["btn_template"], STRINGS["tr"]["btn_template"]}
LANG_TEXTS = {STRINGS["ru"]["btn_lang"], STRINGS["tr"]["btn_lang"]}
CANCEL_TEXTS = {"❌ Отмена", "Отмена", "❌ İptal", "İptal", "iptal", "/cancel", "/stop"}

DEFAULT_PIVOK_TEMPLATE_TEXT = (
    "🛍 Добро пожаловать в PÎVOK!\n\n"
    "Оптовая женская одежда премиум-класса из Стамбула.\n"
    "✨ Эксклюзивные коллекции • 🆕 Новинки каждый день\n\n"
    "Выбирайте модели, уточняйте наличие и оформляйте заказ через WhatsApp или Telegram. 🚀\n\n"
    "🛍 Welcome to PÎVOK!\n\n"
    "Wholesale women's fashion from Istanbul.\n"
    "✨ Exclusive collections • 🆕 New arrivals every day\n\n"
    "Browse our collections, check availability and place your order via WhatsApp or Telegram. 🚀\n\n"
    "🛍 مرحباً بكم في PÎVOK!\n\n"
    "ملابس نسائية بالجملة من إسطنبول.\n"
    "✨ مجموعات حصرية • 🆕 موديلات جديدة يومياً\n\n"
    "تصفحوا مجموعاتنا، تحققوا من التوفر واطلبوا عبر واتساب أو تيليجرام. 🚀"
)

DEFAULT_PIVOK_TEMPLATE_ROWS = [
    [
        {"text": "WHATSAPP ORDER", "url": "https://wa.me/905000000000", "style": "success"},
        {"text": "TELEGRAM ORDER", "url": "https://t.me/pivok", "style": "primary"},
    ],
    [
        {"text": "📍 STORE LOCATION", "url": "https://maps.google.com", "style": None},
        {"text": "👗 BIG SIZE", "url": "https://t.me/pivok", "style": "danger"},
    ],
    [
        {"text": "🔥 SPECIAL OFFERS", "url": "https://t.me/pivok", "style": "danger"},
        {"text": "🌐 SHOP ONLINE", "url": "https://t.me/pivok", "style": None},
    ],
    [
        {"text": "📷 VIDEO CATALOG", "url": "https://t.me/pivok", "style": None},
        {"text": "📱 INSTAGRAM", "url": "https://instagram.com", "style": None},
    ],
]


def get_lang(uid: Optional[int], tg_code: Optional[str] = None) -> str:
    if uid is not None:
        saved = USER_LANGS.get(str(uid))
        if saved in ("ru", "tr"):
            return saved
    if tg_code and tg_code.lower().startswith("tr"):
        return "tr"
    return "ru"


def set_lang(uid: int, lang: str):
    if lang in ("ru", "tr"):
        USER_LANGS[str(uid)] = lang
        persist_store()


def _t(lang: str, key: str, **kwargs) -> str:
    s = STRINGS.get(lang, STRINGS["ru"]).get(key, STRINGS["ru"][key])
    return s.format(**kwargs) if kwargs else s


def reply_kb(items: list[str], cols: int = 2):
    b = ReplyKeyboardBuilder()
    for it in items:
        b.add(KeyboardButton(text=it))
    b.adjust(cols)
    return b.as_markup(resize_keyboard=True)


def main_menu_kb(lang: str):
    return reply_kb([_t(lang, "btn_template"), _t(lang, "btn_lang")], 2)


def lang_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="setlang_ru"),
                InlineKeyboardButton(text="🇹🇷 Türkçe", callback_data="setlang_tr"),
            ]
        ]
    )


class EditorStates(StatesGroup):
    waiting_for_post = State()
    editing_post = State()
    editing_text = State()
    waiting_for_channel = State()


class SecurityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        uid = user.id if user else None
        if uid and uid not in ALLOWED_USERS:
            if hasattr(event, "text") and event.text:
                if event.text.strip().lower() in {SECRET_PASSWORD.lower(), "пивок", "pivok"}:
                    ALLOWED_USERS.append(uid)
                    lang = get_lang(uid, getattr(user, "language_code", None))
                    set_lang(uid, lang)
                    await event.answer(
                        "✅ **Доступ открыт! / Erişim açıldı!**",
                        reply_markup=lang_inline_kb(),
                        parse_mode="Markdown",
                    )
                    await event.answer(
                        _t(lang, "welcome"),
                        reply_markup=main_menu_kb(lang),
                        parse_mode="Markdown",
                    )
                    return
                await event.answer(
                    "🔒 Бот защищён. Введите пароль:\n"
                    "🔒 Bot korumalıdır. Lütfen şifreyi girin:"
                )
                return
            if hasattr(event, "data") and event.data:
                await event.answer("🔒 Введите пароль / Şifreyi girin", show_alert=True)
                return
            return
        return await handler(event, data)


bot = Bot(token=BOT_TOKEN) if (BOT_TOKEN and "PASTE_YOUR" not in BOT_TOKEN) else None
dp = Dispatcher()
dp.message.outer_middleware(SecurityMiddleware())
dp.callback_query.outer_middleware(SecurityMiddleware())


def get_saved_channel(uid: int) -> tuple[Optional[Any], Optional[str]]:
    d = DRAFTS.get(str(uid), {})
    ch_id = d.get("target_channel") or STORE.get("default_channel")
    ch_lbl = d.get("target_channel_label") or STORE.get("default_channel_label")
    if ch_id and not ch_lbl:
        ch_lbl = str(ch_id)
    return ch_id, ch_lbl


def set_saved_channel(uid: int, ch_id: Any, ch_lbl: str):
    STORE["default_channel"] = ch_id
    STORE["default_channel_label"] = ch_lbl
    if str(uid) in DRAFTS:
        DRAFTS[str(uid)]["target_channel"] = ch_id
        DRAFTS[str(uid)]["target_channel_label"] = ch_lbl
    persist_store()


def extract_rows_from_markup(markup: Optional[InlineKeyboardMarkup]) -> list[list[dict]]:
    extracted = []
    if markup and markup.inline_keyboard:
        for row in markup.inline_keyboard:
            row_btns = []
            for btn in row:
                if btn.url:
                    style = getattr(btn, "style", None)
                    if not style and getattr(btn, "model_extra", None):
                        style = btn.model_extra.get("style")
                    row_btns.append({"text": btn.text, "url": btn.url, "style": style or None})
            if row_btns:
                extracted.append(row_btns)
    return extracted


def extract_forward_channel_info(message: Message) -> tuple[Optional[Any], Optional[str], Optional[int]]:
    origin = getattr(message, "forward_origin", None)
    if origin and getattr(origin, "type", None) == "channel" and getattr(origin, "chat", None):
        ch = origin.chat
        ch_ref = f"@{ch.username}" if getattr(ch, "username", None) else ch.id
        ch_title = f"@{ch.username}" if getattr(ch, "username", None) else (ch.title or str(ch.id))
        return ch_ref, ch_title, getattr(origin, "message_id", None)

    fwd_chat = getattr(message, "forward_from_chat", None)
    if fwd_chat and getattr(fwd_chat, "type", None) == "channel":
        ch_ref = f"@{fwd_chat.username}" if getattr(fwd_chat, "username", None) else fwd_chat.id
        ch_title = f"@{fwd_chat.username}" if getattr(fwd_chat, "username", None) else (fwd_chat.title or str(fwd_chat.id))
        return ch_ref, ch_title, getattr(message, "forward_from_message_id", None)

    return None, None, None


def build_post_keyboard(uid: int, lang: str, include_controls: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    d = DRAFTS.get(str(uid), {})
    for row_data in d.get("rows", []):
        row_btns = []
        for btn in row_data:
            txt = (btn.get("text") or "").strip()
            url = (btn.get("url") or "").strip()
            if not txt or not url:
                continue
            kwargs: Dict[str, Any] = {"text": txt, "url": url}
            if btn.get("style"):
                kwargs["style"] = btn["style"]
            row_btns.append(InlineKeyboardButton(**kwargs))
        if row_btns:
            builder.row(*row_btns)

    if include_controls:
        base_url = get_active_webapp_base_url()
        if base_url.startswith("https://"):
            ed_url = f"{base_url}/?lang={lang}&uid={uid}&v={int(time.time())}"
            builder.row(
                InlineKeyboardButton(text=_t(lang, "btn_open_editor"), web_app=WebAppInfo(url=ed_url))
            )
        builder.row(InlineKeyboardButton(text=_t(lang, "btn_edit_text"), callback_data="pc_edit_text"))
        builder.row(
            InlineKeyboardButton(text=_t(lang, "btn_send_channel"), callback_data="pc_send_new"),
            InlineKeyboardButton(text=_t(lang, "btn_send_pin"), callback_data="pc_send_pin"),
        )
        if d.get("origin_channel_id") and d.get("origin_message_id"):
            builder.row(
                InlineKeyboardButton(
                    text=_t(lang, "btn_update_existing"), callback_data="pc_update_inplace"
                )
            )
        _, ch_lbl = get_saved_channel(uid)
        ch_txt = _t(lang, "btn_set_channel", ch=ch_lbl) if ch_lbl else _t(lang, "btn_no_channel")
        builder.row(InlineKeyboardButton(text=ch_txt, callback_data="pc_change_channel"))

    return builder.as_markup()


async def render_draft_preview(chat_id: int, uid: int, lang: str):
    if not bot:
        return
    d = DRAFTS.get(str(uid))
    if not d:
        return

    old_prev = d.get("preview_message_id")
    if old_prev:
        try:
            await bot.delete_message(chat_id, old_prev)
        except Exception:
            pass

    kb = build_post_keyboard(uid, lang, include_controls=True)
    if d.get("template_text"):
        prev_msg = await bot.send_message(
            chat_id, d["template_text"], reply_markup=kb, disable_web_page_preview=True
        )
    else:
        prev_msg = await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=d["chat_id"],
            message_id=d["original_message_id"],
            reply_markup=kb,
        )
    d["preview_message_id"] = prev_msg.message_id
    persist_store()


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(EditorStates.waiting_for_post)
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    await message.answer(
        _t(lang, "welcome"),
        reply_markup=main_menu_kb(lang),
        parse_mode="Markdown",
    )


@dp.message(Command("lang", "language", "dil"))
@dp.message(StateFilter("*"), F.text.in_(LANG_TEXTS))
async def cmd_language(message: Message):
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    await message.answer(
        _t(lang, "lang_prompt"),
        reply_markup=lang_inline_kb(),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.in_({"setlang_ru", "setlang_tr"}))
async def cb_set_language(callback: CallbackQuery, state: FSMContext):
    new_lang = "tr" if callback.data.endswith("_tr") else "ru"
    uid = callback.from_user.id
    set_lang(uid, new_lang)
    await callback.answer("🇹🇷 Türkçe" if new_lang == "tr" else "🇷🇺 Русский")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await bot.send_message(
        callback.message.chat.id,
        _t(new_lang, "lang_switched") + "\n\n" + _t(new_lang, "welcome"),
        reply_markup=main_menu_kb(new_lang),
        parse_mode="Markdown",
    )
    if str(uid) in DRAFTS:
        await render_draft_preview(callback.message.chat.id, uid, new_lang)


@dp.message(Command("cancel", "stop"))
@dp.message(StateFilter("*"), F.text.in_(CANCEL_TEXTS))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.set_state(EditorStates.waiting_for_post)
    lang = get_lang(message.from_user.id, message.from_user.language_code)
    await message.answer(_t(lang, "cancelled"), reply_markup=main_menu_kb(lang))


@dp.message(StateFilter("*"), F.text.in_(TEMPLATE_TEXTS))
async def cmd_create_pivok_template(message: Message, state: FSMContext):
    uid = message.from_user.id
    lang = get_lang(uid, message.from_user.language_code)
    ch_id, ch_lbl = get_saved_channel(uid)
    DRAFTS[str(uid)] = {
        "chat_id": message.chat.id,
        "template_text": DEFAULT_PIVOK_TEMPLATE_TEXT,
        "original_message_id": None,
        "rows": [list(map(dict, r)) for r in DEFAULT_PIVOK_TEMPLATE_ROWS],
        "auto_sort": True,
        "target_channel": ch_id,
        "target_channel_label": ch_lbl,
    }
    persist_store()
    await state.set_state(EditorStates.editing_post)
    await render_draft_preview(message.chat.id, uid, lang)
    await message.answer(
        _t(lang, "preview_hint"),
        reply_markup=main_menu_kb(lang),
        parse_mode="Markdown",
    )


@dp.message(EditorStates.waiting_for_channel)
async def handle_channel_input(message: Message, state: FSMContext):
    uid = message.from_user.id
    lang = get_lang(uid, message.from_user.language_code)

    fwd_id, fwd_title, _ = extract_forward_channel_info(message)
    if fwd_id is not None:
        ch_id, ch_label = fwd_id, fwd_title or str(fwd_id)
    else:
        raw = (message.text or "").strip()
        if not raw:
            await message.answer(_t(lang, "ask_channel"), parse_mode="Markdown")
            return
        if "t.me/" in raw:
            slug = raw.split("t.me/", 1)[1].split("/")[0].split("?")[0].strip()
            raw = f"@{slug}" if slug and not slug.startswith("+") else raw
        if not raw.startswith("@") and not raw.lstrip("-").isdigit():
            raw = f"@{raw}"
        ch_id = int(raw) if raw.lstrip("-").isdigit() else raw
        ch_label = str(ch_id)

    set_saved_channel(uid, ch_id, ch_label)
    pending_mode = (await state.get_data()).get("pending_publish_mode")
    await state.set_state(EditorStates.editing_post)
    await message.answer(
        _t(lang, "channel_saved", ch=ch_label),
        reply_markup=main_menu_kb(lang),
        parse_mode="Markdown",
    )

    if pending_mode in ("new", "pin"):
        await _publish_to_channel(message.chat.id, uid, lang, pin=(pending_mode == "pin"))
    else:
        await render_draft_preview(message.chat.id, uid, lang)


@dp.message(EditorStates.editing_text)
async def handle_updated_post_text(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        return
    uid = message.from_user.id
    lang = get_lang(uid, message.from_user.language_code)
    d = DRAFTS.get(str(uid))
    if not d:
        await state.set_state(EditorStates.waiting_for_post)
        await message.answer(_t(lang, "draft_missing"))
        return

    d["chat_id"] = message.chat.id
    d["original_message_id"] = message.message_id
    d["template_text"] = None
    persist_store()
    await state.set_state(EditorStates.editing_post)
    await render_draft_preview(message.chat.id, uid, lang)
    await message.answer(_t(lang, "preview_hint"), reply_markup=main_menu_kb(lang), parse_mode="Markdown")


@dp.message(StateFilter(EditorStates.waiting_for_post, EditorStates.editing_post, None))
async def handle_any_post(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        return
    uid = message.from_user.id
    lang = get_lang(uid, message.from_user.language_code)

    extracted_rows = extract_rows_from_markup(message.reply_markup)
    fwd_ch_id, fwd_ch_title, fwd_msg_id = extract_forward_channel_info(message)

    saved_ch_id, saved_ch_lbl = get_saved_channel(uid)
    if fwd_ch_id is not None and not saved_ch_id:
        set_saved_channel(uid, fwd_ch_id, fwd_ch_title or str(fwd_ch_id))
        saved_ch_id, saved_ch_lbl = fwd_ch_id, fwd_ch_title or str(fwd_ch_id)

    DRAFTS[str(uid)] = {
        "chat_id": message.chat.id,
        "original_message_id": message.message_id,
        "template_text": None,
        "rows": extracted_rows,
        "auto_sort": True,
        "origin_channel_id": fwd_ch_id,
        "origin_message_id": fwd_msg_id,
        "target_channel": saved_ch_id,
        "target_channel_label": saved_ch_lbl,
    }
    persist_store()

    try:
        await state.set_state(EditorStates.editing_post)
        await render_draft_preview(message.chat.id, uid, lang)
        await message.answer(
            _t(lang, "preview_hint"),
            reply_markup=main_menu_kb(lang),
            parse_mode="Markdown",
        )
    except TelegramAPIError as e:
        await message.answer(f"❌ Error: {e}")


@dp.callback_query(F.data == "pc_edit_text")
async def cb_edit_text(callback: CallbackQuery, state: FSMContext):
    lang = get_lang(callback.from_user.id, callback.from_user.language_code)
    await state.set_state(EditorStates.editing_text)
    await callback.answer()
    await callback.message.answer(
        _t(lang, "ask_new_text"),
        reply_markup=reply_kb([_t(lang, "btn_cancel")], 1),
    )


@dp.callback_query(F.data == "pc_change_channel")
async def cb_change_channel(callback: CallbackQuery, state: FSMContext):
    lang = get_lang(callback.from_user.id, callback.from_user.language_code)
    await state.update_data(pending_publish_mode=None)
    await state.set_state(EditorStates.waiting_for_channel)
    await callback.answer()
    await callback.message.answer(
        _t(lang, "ask_channel"),
        reply_markup=reply_kb([_t(lang, "btn_cancel")], 1),
        parse_mode="Markdown",
    )


async def _publish_to_channel(chat_id: int, uid: int, lang: str, pin: bool = False):
    if not bot:
        return
    d = DRAFTS.get(str(uid))
    if not d:
        await bot.send_message(chat_id, _t(lang, "draft_missing"))
        return

    ch_id, ch_lbl = get_saved_channel(uid)
    clean_kb = build_post_keyboard(uid, lang, include_controls=False)
    try:
        if d.get("template_text"):
            sent = await bot.send_message(
                chat_id=ch_id,
                text=d["template_text"],
                reply_markup=clean_kb,
                disable_web_page_preview=True,
            )
            sent_msg_id = sent.message_id
        else:
            copied = await bot.copy_message(
                chat_id=ch_id,
                from_chat_id=d["chat_id"],
                message_id=d["original_message_id"],
                reply_markup=clean_kb,
            )
            sent_msg_id = copied.message_id

        if pin and sent_msg_id:
            try:
                await bot.pin_chat_message(chat_id=ch_id, message_id=sent_msg_id, disable_notification=True)
            except Exception:
                pass
            await bot.send_message(chat_id, _t(lang, "sent_pinned_ok", ch=ch_lbl), parse_mode="Markdown")
        else:
            await bot.send_message(chat_id, _t(lang, "sent_ok", ch=ch_lbl), parse_mode="Markdown")
    except TelegramAPIError as e:
        await bot.send_message(chat_id, _t(lang, "send_err", ch=ch_lbl, err=str(e)), parse_mode="Markdown")


@dp.callback_query(F.data.in_({"pc_send_new", "pc_send_pin"}))
async def cb_send_to_channel(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    lang = get_lang(uid, callback.from_user.language_code)
    if str(uid) not in DRAFTS:
        await callback.answer(_t(lang, "draft_missing"), show_alert=True)
        return

    pin = callback.data == "pc_send_pin"
    ch_id, _ = get_saved_channel(uid)
    await callback.answer()
    if not ch_id:
        await state.update_data(pending_publish_mode="pin" if pin else "new")
        await state.set_state(EditorStates.waiting_for_channel)
        await callback.message.answer(
            _t(lang, "ask_channel"),
            reply_markup=reply_kb([_t(lang, "btn_cancel")], 1),
            parse_mode="Markdown",
        )
        return

    await _publish_to_channel(callback.message.chat.id, uid, lang, pin=pin)


@dp.callback_query(F.data == "pc_update_inplace")
async def cb_update_inplace(callback: CallbackQuery):
    if not bot:
        return
    uid = callback.from_user.id
    lang = get_lang(uid, callback.from_user.language_code)
    d = DRAFTS.get(str(uid))
    if not d or not d.get("origin_channel_id") or not d.get("origin_message_id"):
        await callback.answer(_t(lang, "draft_missing"), show_alert=True)
        return

    await callback.answer()
    clean_kb = build_post_keyboard(uid, lang, include_controls=False)
    try:
        await bot.edit_message_reply_markup(
            chat_id=d["origin_channel_id"],
            message_id=d["origin_message_id"],
            reply_markup=clean_kb,
        )
        await callback.message.answer(_t(lang, "updated_ok"))
    except TelegramAPIError as e:
        await callback.message.answer(
            _t(lang, "send_err", ch=str(d["origin_channel_id"]), err=str(e)),
            parse_mode="Markdown",
        )


# ==========================================
# FASTAPI + WEBAPP + CLOUD/LOCAL LIFECYCLE
# ==========================================

def _start_local_tunnel_thread():
    global _TUNNEL_URL
    candidates = [
        os.path.join(BASE_DIR, "cloudflared.exe"),
        r"C:\Users\allca\Desktop\08.10.2026\Telegram Channels\post_constructor_bot\cloudflared.exe",
        r"C:\Users\allca\Desktop\08.10.2026\New Sola\Mix\cloudflared.exe",
        shutil.which("cloudflared") or "",
    ]
    cf_path = next((p for p in candidates if p and os.path.exists(p)), None)
    if not cf_path:
        logger.warning("cloudflared not found locally; set PUBLIC_BASE_URL or deploy to Render.")
        return

    def _runner():
        global _TUNNEL_URL
        while True:
            proc = subprocess.Popen(
                [cf_path, "tunnel", "--url", f"http://127.0.0.1:{LOCAL_PORT}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in iter(proc.stdout.readline, ""):
                if "trycloudflare.com" in line:
                    m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                    if m and _TUNNEL_URL != m.group(0):
                        _TUNNEL_URL = m.group(0)
                        logger.info(f"🌐 Cloudflare Tunnel active: {_TUNNEL_URL}")
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            time.sleep(2)

    threading.Thread(target=_runner, daemon=True).start()


_polling_task = None


@asynccontextmanager
async def lifespan(app_inst: FastAPI):
    global _polling_task
    if bot:
        if PUBLIC_BASE_URL.startswith("https://"):
            wh_url = f"{PUBLIC_BASE_URL}/telegram/webhook"
            await bot.set_webhook(wh_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=False)
            logger.info(f"✅ Cloud Webhook registered at {wh_url}")
        else:
            _start_local_tunnel_thread()
            await bot.delete_webhook(drop_pending_updates=False)
            _polling_task = asyncio.create_task(dp.start_polling(bot))
            logger.info("🌸 Started local polling mode")
    else:
        logger.warning("⚠️ BOT_TOKEN is not configured yet!")
    yield
    if _polling_task:
        _polling_task.cancel()
    if bot:
        await bot.session.close()


app = FastAPI(title="PIVOK Post Constructor Bot", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/webapp", StaticFiles(directory=os.path.join(BASE_DIR, "webapp")), name="webapp")


@app.get("/")
async def serve_editor_root():
    return FileResponse(os.path.join(BASE_DIR, "webapp", "index.html"))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "PIVOK Post Constructor Bot",
        "languages": ["ru", "tr"],
        "webhook_mode": PUBLIC_BASE_URL.startswith("https://"),
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not bot:
        return {"ok": False}
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    payload = await request.json()
    upd_id = payload.get("update_id")
    if upd_id is not None:
        if upd_id in _SEEN_UPDATES:
            return {"ok": True}
        _SEEN_UPDATES.append(upd_id)
    update = Update.model_validate(payload, context={"bot": bot})
    asyncio.create_task(dp.feed_update(bot, update))
    return {"ok": True}


def _resolve_webapp_uid(payload: dict) -> Optional[int]:
    raw_init = payload.get("initData") or ""
    if raw_init and BOT_TOKEN:
        try:
            parsed = safe_parse_webapp_init_data(token=BOT_TOKEN, init_data=raw_init)
            if parsed.user and parsed.user.id:
                return int(parsed.user.id)
        except Exception:
            pass
    uid_fb = str(payload.get("uid") or "").strip()
    if uid_fb.isdigit() and int(uid_fb) in ALLOWED_USERS:
        return int(uid_fb)
    return None


@app.post("/api/get_draft")
async def api_get_draft(request: Request):
    data = await request.json()
    uid = _resolve_webapp_uid(data)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=403)
    d = DRAFTS.get(str(uid), {})
    return {"rows": d.get("rows", []), "auto_sort": d.get("auto_sort", True)}


@app.post("/api/save_draft")
async def api_save_draft(request: Request):
    data = await request.json()
    uid = _resolve_webapp_uid(data)
    if not uid:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=403)

    d = DRAFTS.get(str(uid))
    if not d:
        return JSONResponse({"success": False, "error": "Draft not found"})

    d["rows"] = data.get("rows", [])
    if "auto_sort" in data:
        d["auto_sort"] = bool(data["auto_sort"])
    persist_store()

    lang = get_lang(uid, None)
    if bot and d.get("preview_message_id") and d.get("chat_id"):
        try:
            await bot.edit_message_reply_markup(
                chat_id=d["chat_id"],
                message_id=d["preview_message_id"],
                reply_markup=build_post_keyboard(uid, lang, include_controls=True),
            )
        except Exception as e:
            if "message is not modified" not in str(e).lower():
                logger.warning(f"Preview update error: {e}")

    return {"success": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=LOCAL_PORT)

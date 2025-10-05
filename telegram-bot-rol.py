#!/usr/bin/env python3
"""
Bot de Telegram para crear partidas con /partida y permitir que la gente se
apunte o se desapunte con botones. La lista de apuntados aparece editada en el
mismo mensaje del evento. Persistencia con SQLite.

Requisitos:
  - Python 3.10+
  - python-telegram-bot >= 21

Instalación:
  pip install "python-telegram-bot>=21,<22"
  # o con requirements.txt: python-telegram-bot>=21,<22

Ejecución:
  export BOT_TOKEN="<tu token>"
  python telegram_partidas_bot.py
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
)

# --------------------------- Configuración & Logging ---------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("partidas-bot")

DB_PATH = os.environ.get("PARTIDAS_DB", "partidas.db")

# --------------------------- Capa de base de datos ----------------------------
SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER,
    description TEXT NOT NULL,
    creator_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendees (
    event_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    first_name TEXT,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (event_id, user_id),
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
);
"""


def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with closing(get_db()) as con:
        con.executescript(SCHEMA_SQL)
        con.commit()


# -------------------------- Utilidades de formato -----------------------------

def human_name(username: str | None, first_name: str | None) -> str:
    if username:
        return f"@{username}"
    if first_name:
        return first_name
    return "(sin nombre)"


def render_event_text(event_row: sqlite3.Row, con: sqlite3.Connection) -> str:
    """Devuelve el texto del mensaje del evento con la lista de apuntados."""
    cur = con.execute(
        "SELECT user_id, username, first_name, joined_at FROM attendees WHERE event_id = ? ORDER BY joined_at ASC",
        (event_row["id"],),
    )
    attendees = cur.fetchall()

    lines = [
        f"📅 <b>Evento</b>\n{html.escape(event_row['description'])}",
        "",
        "👥 <b>Apuntados</b>:",
    ]

    if attendees:
        for i, row in enumerate(attendees, start=1):
            when = row["joined_at"]
            name = human_name(row["username"], row["first_name"])
            lines.append(f"{i}. {html.escape(name)}")
    else:
        lines.append("(nadie apuntado aún)")

    lines.append("")
    lines.append(f"🕒 Creado: {event_row['created_at']}")
    lines.append(f"🆔 Evento #{event_row['id']}")

    return "\n".join(lines)


def event_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Apuntarme", callback_data=f"join:{event_id}"),
                InlineKeyboardButton("❌ Borrarme", callback_data=f"leave:{event_id}"),
            ]
        ]
    )


# ------------------------------- Handlers ------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hola! Usa /partida <descripción> para crear un evento con botones de apuntarse.\n"
        "Ejemplo: /partida Sesión 0 Aquinoth: viernes 11/10 a las 16:00 en la biblioteca"
    )


async def create_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    description = (update.message.text or "").partition(" ")[2].strip()
    if not description:
        await update.message.reply_text(
            "Formato: /partida <descripción del evento>"
        )
        return

    with closing(get_db()) as con:
        now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        cur = con.execute(
            "INSERT INTO events (chat_id, message_id, description, creator_id, created_at) VALUES (?,?,?,?,?)",
            (
                update.message.chat_id,
                None,
                description,
                update.message.from_user.id,
                now,
            ),
        )
        event_id = cur.lastrowid
        con.commit()

    # Enviar el mensaje inicial con botones
    text = render_event_by_id(event_id)
    sent = await update.message.reply_html(text, reply_markup=event_keyboard(event_id))

    # Guardar el message_id para poder referenciarlo luego si se quisiera
    with closing(get_db()) as con:
        con.execute(
            "UPDATE events SET message_id = ? WHERE id = ?",
            (sent.message_id, event_id),
        )
        con.commit()


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    if ":" not in data:
        return

    action, id_str = data.split(":", 1)
    try:
        event_id = int(id_str)
    except ValueError:
        return

    user = query.from_user

    with closing(get_db()) as con:
        # Comprobar que el evento existe y es del mismo chat
        ev = con.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if not ev:
            await query.edit_message_text(
                "Este evento ya no existe."
            )
            return

        if action == "join":
            try:
                con.execute(
                    "INSERT INTO attendees (event_id, user_id, username, first_name, joined_at) VALUES (?,?,?,?,?)",
                    (
                        event_id,
                        user.id,
                        user.username,
                        user.first_name,
                        datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M"),
                    ),
                )
                con.commit()
            except sqlite3.IntegrityError:
                # Ya estaba apuntado, no pasa nada
                pass
        elif action == "leave":
            con.execute(
                "DELETE FROM attendees WHERE event_id = ? AND user_id = ?",
                (event_id, user.id),
            )
            con.commit()

    # Re-renderizar el texto y editar el mensaje
    new_text = render_event_by_id(event_id)
    try:
        await query.edit_message_text(new_text, parse_mode=ParseMode.HTML, reply_markup=event_keyboard(event_id))
    except Exception as e:
        log.exception("No se pudo editar el mensaje: %s", e)


# --------------------------- Funciones auxiliares -----------------------------

def render_event_by_id(event_id: int) -> str:
    with closing(get_db()) as con:
        ev = con.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if not ev:
            return "(evento no encontrado)"
        return render_event_text(ev, con)


# --------------------------------- Main --------------------------------------
def main() -> None:
    init_db()

    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Debes definir la variable de entorno BOT_TOKEN")

    defaults = Defaults(parse_mode=ParseMode.HTML)

    app = (
        ApplicationBuilder()
        .token(token)
        .defaults(defaults)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("partida", create_event))
    app.add_handler(CallbackQueryHandler(on_button))

    log.info("Bot arrancado. Esperando actualizaciones…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        log.info("Apagando…")

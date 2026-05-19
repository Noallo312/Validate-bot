"""
validation_bot.py — Bot de validation centralisé
Gère les commandes de B4U Deals ET KFC depuis un seul bot Telegram.

Variables d'environnement requises :
  VALIDATION_BOT_TOKEN  — token du nouveau bot de validation
  B4U_BOT_TOKEN         — token du bot B4U Deals (pour notifier les clients)
  KFC_BOT_TOKEN         — token du bot KFC (pour notifier les clients)
  B4U_DATABASE_URL      — SQLite ou Postgres de B4U (ex: sqlite:///b4u_orders.db)
  KFC_DATABASE_URL      — Postgres de KFC
  ADMIN_IDS             — IDs admins séparés par virgule (ex: 6976573567,5174507979)
"""

import asyncio
import logging
import os
import requests
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ─── CONFIG ───────────────────────────────────────────────────────────────────
VALIDATION_BOT_TOKEN = os.environ.get("VALIDATION_BOT_TOKEN", "METS_TON_TOKEN_ICI")
B4U_BOT_TOKEN        = os.environ.get("B4U_BOT_TOKEN", "")
KFC_BOT_TOKEN        = os.environ.get("KFC_BOT_TOKEN", "")

_raw_admins = os.environ.get("ADMIN_IDS", "6976573567,5174507979")
ADMIN_IDS = [int(x.strip()) for x in _raw_admins.split(",") if x.strip()]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ─── HELPERS DB B4U (SQLAlchemy / SQLite ou Postgres) ─────────────────────────
def get_b4u_engine():
    from sqlalchemy import create_engine
    url = os.environ.get("B4U_DATABASE_URL", "")
    if not url:
        return None
    args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=args, future=True)

def b4u_get_order(order_id: int):
    engine = get_b4u_engine()
    if not engine:
        return None
    with engine.connect() as conn:
        from sqlalchemy import text
        row = conn.execute(
            text("SELECT id, user_id, username, service, plan, price, cost, "
                 "payment_method, status FROM orders WHERE id = :id"),
            {"id": order_id}
        ).fetchone()
    return dict(row._mapping) if row else None

def b4u_set_status(order_id: int, status: str, extra: dict = None):
    engine = get_b4u_engine()
    if not engine:
        return
    fields = {"status": status}
    if extra:
        fields.update(extra)
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = order_id
    with engine.begin() as conn:
        from sqlalchemy import text
        conn.execute(text(f"UPDATE orders SET {set_clause} WHERE id = :id"), fields)

def b4u_update_cumulative(price: float, cost: float):
    engine = get_b4u_engine()
    if not engine:
        return
    with engine.begin() as conn:
        from sqlalchemy import text
        conn.execute(
            text("UPDATE cumulative_stats SET "
                 "total_revenue = total_revenue + :rev, "
                 "total_profit = total_profit + :profit, "
                 "last_updated = :ts WHERE id = 1"),
            {"rev": price, "profit": price - cost, "ts": datetime.now().isoformat()}
        )

# ─── HELPERS DB KFC (psycopg2 / Postgres) ─────────────────────────────────────
def kfc_get_db():
    import psycopg2, psycopg2.extras
    url = os.environ.get("KFC_DATABASE_URL", "")
    if not url:
        return None
    conn = psycopg2.connect(url)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn

def kfc_get_deposit(deposit_id: int):
    db = kfc_get_db()
    if not db:
        return None
    cur = db.cursor()
    cur.execute("SELECT id, user_id, amount, status FROM deposits WHERE id = %s", (deposit_id,))
    row = cur.fetchone()
    cur.close(); db.close()
    return dict(row) if row else None

def kfc_set_deposit_status(deposit_id: int, status: str):
    db = kfc_get_db()
    if not db:
        return
    cur = db.cursor()
    cur.execute("UPDATE deposits SET status = %s WHERE id = %s", (status, deposit_id))
    db.commit(); cur.close(); db.close()

def kfc_add_balance(user_id: int, amount: float):
    db = kfc_get_db()
    if not db:
        return
    cur = db.cursor()
    cur.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, user_id))
    db.commit(); cur.close(); db.close()

def kfc_get_balance(user_id: int) -> float:
    db = kfc_get_db()
    if not db:
        return 0.0
    cur = db.cursor()
    cur.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close(); db.close()
    return float(row["balance"]) if row else 0.0

# ─── NOTIFIER CLIENT VIA SON BOT ──────────────────────────────────────────────
async def notify_client(bot_token: str, user_id: int, text: str):
    if not bot_token:
        logger.warning("Token bot manquant, impossible de notifier le client")
        return
    try:
        bot = Bot(token=bot_token)
        await bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Erreur notification client {user_id}: {e}")

# ─── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Accès réservé aux admins.")
        return
    await update.message.reply_text(
        "✅ *Bot de validation centralisé actif*\n\n"
        "Ce bot reçoit et traite les commandes de :\n"
        "• 🎬 *B4U Deals* — abonnements streaming\n"
        "• 🍗 *KFC* — cartes de fidélité\n\n"
        "Les notifications arrivent ici automatiquement.",
        parse_mode="Markdown"
    )

# ─── CALLBACKS B4U ────────────────────────────────────────────────────────────
async def handle_b4u_take(query, order_id: int):
    admin_id = query.from_user.id
    admin_username = query.from_user.username or f"Admin_{admin_id}"
    order = b4u_get_order(order_id)
    if not order:
        await query.answer("❌ Commande introuvable", show_alert=True); return
    if order["status"] != "en_attente":
        await query.answer("❌ Commande déjà prise ou terminée", show_alert=True); return

    b4u_set_status(order_id, "en_cours", {
        "admin_id": admin_id,
        "admin_username": admin_username,
        "taken_at": datetime.now().isoformat()
    })

    text = (
        f"🔒 *[B4U] COMMANDE #{order_id} — PRISE EN CHARGE*\n\n"
        f"👤 @{order['username'] or order['user_id']}\n"
        f"📦 {order['service']}\n📋 {order['plan']}\n"
        f"💰 {order['price']}€  |  💵 Coût: {order['cost']}€  |  📈 Bénéf: {order['price']-order['cost']:.2f}€\n"
        f"💳 {order.get('payment_method','N/A')}\n\n"
        f"✅ Pris par @{admin_username}\n🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Terminer", callback_data=f"b4u_complete_{order_id}"),
        InlineKeyboardButton("❌ Annuler",  callback_data=f"b4u_cancel_{order_id}"),
    ], [
        InlineKeyboardButton("🔄 Remettre", callback_data=f"b4u_restore_{order_id}"),
    ]])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
    await query.answer("✅ Commande prise en charge")


async def handle_b4u_complete(query, order_id: int):
    order = b4u_get_order(order_id)
    if not order:
        await query.answer("❌ Introuvable", show_alert=True); return
    if order["status"] == "terminee":
        await query.answer("⚠️ Déjà terminée", show_alert=True); return

    b4u_set_status(order_id, "terminee")
    b4u_update_cumulative(order["price"], order["cost"])

    await query.edit_message_text(
        f"✅ *[B4U] COMMANDE #{order_id} — TERMINÉE*\n\n"
        f"Terminée par @{query.from_user.username or 'Admin'}\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        parse_mode="Markdown"
    )
    await query.answer("✅ Terminée")


async def handle_b4u_cancel(query, order_id: int):
    b4u_set_status(order_id, "annulee", {
        "cancelled_by": query.from_user.id,
        "cancelled_at": datetime.now().isoformat()
    })
    await query.edit_message_text(
        f"❌ *[B4U] COMMANDE #{order_id} — ANNULÉE*\n\n"
        f"Annulée par @{query.from_user.username or 'Admin'}\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        parse_mode="Markdown"
    )
    await query.answer("❌ Annulée")


async def handle_b4u_restore(query, order_id: int):
    order = b4u_get_order(order_id)
    if not order:
        await query.answer("❌ Introuvable", show_alert=True); return

    b4u_set_status(order_id, "en_attente", {
        "admin_id": None, "admin_username": None,
        "taken_at": None, "cancelled_by": None, "cancelled_at": None
    })

    # Renvoyer la notif à tous les admins
    text = (
        f"🔔 *[B4U] COMMANDE #{order_id} REMISE EN LIGNE*\n\n"
        f"👤 @{order['username'] or order['user_id']}\n"
        f"📦 {order['service']}\n📋 {order['plan']}\n"
        f"💰 {order['price']}€  |  Coût: {order['cost']}€\n"
        f"💳 {order.get('payment_method','N/A')}\n\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✋ Prendre", callback_data=f"b4u_take_{order_id}"),
        InlineKeyboardButton("❌ Annuler", callback_data=f"b4u_cancel_{order_id}"),
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await query.get_bot().send_message(admin_id, text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception:
            pass
    await query.answer("🔄 Remise en ligne")

# ─── CALLBACKS KFC ────────────────────────────────────────────────────────────
async def handle_kfc_validate(query, deposit_id: int, user_id: int, amount: float):
    dep = kfc_get_deposit(deposit_id)
    if not dep:
        await query.answer("❌ Dépôt introuvable", show_alert=True); return
    if dep["status"] != "pending":
        await query.answer("⚠️ Déjà traité", show_alert=True); return

    kfc_add_balance(user_id, amount)
    kfc_set_deposit_status(deposit_id, "completed")
    balance = kfc_get_balance(user_id)

    await notify_client(
        KFC_BOT_TOKEN, user_id,
        f"✅ *{amount:.2f}€* ajoutés à ton solde.\n💰 Nouveau solde : *{balance:.2f}€*"
    )
    await query.edit_message_text(
        f"✅ *[KFC] Dépôt #{deposit_id} validé*\n\n"
        f"👤 User ID: `{user_id}`\n"
        f"💰 {amount:.2f}€ crédités\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        parse_mode="Markdown"
    )
    await query.answer("✅ Dépôt validé")


async def handle_kfc_reject(query, deposit_id: int, user_id: int):
    dep = kfc_get_deposit(deposit_id)
    if not dep:
        await query.answer("❌ Dépôt introuvable", show_alert=True); return

    kfc_set_deposit_status(deposit_id, "rejected")
    await notify_client(
        KFC_BOT_TOKEN, user_id,
        "❌ Ton dépôt a été refusé. Contacte l'admin si c'est une erreur."
    )
    await query.edit_message_text(
        f"❌ *[KFC] Dépôt #{deposit_id} rejeté*\n\n"
        f"👤 User ID: `{user_id}`\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        parse_mode="Markdown"
    )
    await query.answer("❌ Rejeté")

# ─── ROUTER PRINCIPAL ─────────────────────────────────────────────────────────
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("⛔ Non autorisé", show_alert=True)
        return
    await query.answer()

    data = query.data

    # ── B4U callbacks ──
    if data.startswith("b4u_take_"):
        await handle_b4u_take(query, int(data.split("_")[-1]))

    elif data.startswith("b4u_complete_"):
        await handle_b4u_complete(query, int(data.split("_")[-1]))

    elif data.startswith("b4u_cancel_"):
        await handle_b4u_cancel(query, int(data.split("_")[-1]))

    elif data.startswith("b4u_restore_"):
        await handle_b4u_restore(query, int(data.split("_")[-1]))

    # ── KFC callbacks ──
    elif data.startswith("validatedep_"):
        parts = data.split("_")
        await handle_kfc_validate(query, int(parts[1]), int(parts[2]), float(parts[3]))

    elif data.startswith("rejectdep_"):
        parts = data.split("_")
        await handle_kfc_reject(query, int(parts[1]), int(parts[2]))

    else:
        await query.answer("❓ Action inconnue", show_alert=True)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    if not VALIDATION_BOT_TOKEN or VALIDATION_BOT_TOKEN == "METS_TON_TOKEN_ICI":
        logger.error("❌ VALIDATION_BOT_TOKEN non configuré !")
        return

    app = Application.builder().token(VALIDATION_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_router))

    logger.info("🤖 Bot de validation centralisé démarré")
    await app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())

"""
validation_bot.py — Bot de validation centralisé (httpx pur, sans python-telegram-bot)
Compatible Python 3.13+
"""

import asyncio
import logging
import os
import httpx
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
VALIDATION_BOT_TOKEN = os.environ.get("VALIDATION_BOT_TOKEN", "")
B4U_BOT_TOKEN        = os.environ.get("B4U_BOT_TOKEN", "")
KFC_BOT_TOKEN        = os.environ.get("KFC_BOT_TOKEN", "")

_raw_admins = os.environ.get("ADMIN_IDS", "6976573567,5174507979")
ADMIN_IDS = [int(x.strip()) for x in _raw_admins.split(",") if x.strip()]

TG_API = "https://api.telegram.org/bot"

# ─── TELEGRAM HELPERS ─────────────────────────────────────────────────────────
async def tg_call(token: str, method: str, payload: dict) -> dict:
    url = f"{TG_API}{token}/{method}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload)
        return r.json()

async def answer_callback(query_id: str, text: str = "", alert: bool = False):
    await tg_call(VALIDATION_BOT_TOKEN, "answerCallbackQuery", {
        "callback_query_id": query_id,
        "text": text,
        "show_alert": alert
    })

async def edit_text(chat_id: int, message_id: int, text: str, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    await tg_call(VALIDATION_BOT_TOKEN, "editMessageText", payload)

async def edit_caption(chat_id: int, message_id: int, text: str, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "caption": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    r = await tg_call(VALIDATION_BOT_TOKEN, "editMessageCaption", payload)
    if not r.get("ok"):
        await edit_text(chat_id, message_id, text, keyboard)

async def send_message(token: str, chat_id: int, text: str, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    await tg_call(token, "sendMessage", payload)

async def notify_client(token: str, user_id: int, text: str):
    if not token:
        return
    try:
        await send_message(token, user_id, text)
    except Exception as e:
        logger.warning(f"Erreur notif client {user_id}: {e}")

# ─── DB B4U ───────────────────────────────────────────────────────────────────
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
            text("SELECT id, user_id, username, service, plan, price, cost, payment_method, status FROM orders WHERE id = :id"),
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
            text("UPDATE cumulative_stats SET total_revenue = total_revenue + :rev, total_profit = total_profit + :profit, last_updated = :ts WHERE id = 1"),
            {"rev": price, "profit": price - cost, "ts": datetime.now().isoformat()}
        )

# ─── DB KFC ───────────────────────────────────────────────────────────────────
def kfc_connect():
    import psycopg2, psycopg2.extras
    url = os.environ.get("KFC_DATABASE_URL", "")
    if not url:
        return None
    conn = psycopg2.connect(url)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    conn.autocommit = False
    return conn

def kfc_get_order(order_id: int):
    db = kfc_connect()
    if not db:
        return None
    try:
        cur = db.cursor()
        cur.execute("SELECT id, user_id, product_id, payment_method, status, admin_id, admin_username FROM orders WHERE id = %s", (order_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        db.close()

def kfc_set_order_status(order_id: int, status: str, extra: dict = None):
    db = kfc_connect()
    if not db:
        return
    try:
        fields = {"status": status}
        if extra:
            fields.update(extra)
        set_clause = ", ".join(f"{k} = %s" for k in fields)
        values = list(fields.values()) + [order_id]
        cur = db.cursor()
        cur.execute(f"UPDATE orders SET {set_clause} WHERE id = %s", values)
        db.commit()
    except Exception as e:
        db.rollback(); raise
    finally:
        db.close()

def kfc_validate_deposit_atomic(deposit_id: int, user_id: int, amount: float) -> float:
    db = kfc_connect()
    if not db:
        raise Exception("KFC_DATABASE_URL non configure")
    try:
        cur = db.cursor()
        # Accepte 'pending' ET 'awaiting_proof' (les dépôts PayPal sont créés avec awaiting_proof)
        cur.execute(
            "UPDATE deposits SET status = 'completed' WHERE id = %s AND status IN ('pending', 'awaiting_proof')",
            (deposit_id,)
        )
        if cur.rowcount == 0:
            raise Exception("Depot introuvable ou deja traite")
        cur.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, user_id))
        cur.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        new_balance = float(row["balance"]) if row else amount
        db.commit()
        return new_balance
    except Exception:
        db.rollback(); raise
    finally:
        db.close()

def kfc_reject_deposit_atomic(deposit_id: int):
    db = kfc_connect()
    if not db:
        raise Exception("KFC_DATABASE_URL non configure")
    try:
        cur = db.cursor()
        cur.execute("UPDATE deposits SET status = 'rejected' WHERE id = %s", (deposit_id,))
        db.commit()
    except Exception:
        db.rollback(); raise
    finally:
        db.close()

# ─── HANDLERS B4U ─────────────────────────────────────────────────────────────
async def handle_b4u_take(chat_id, message_id, query_id, order_id, admin_username):
    order = b4u_get_order(order_id)
    if not order:
        await answer_callback(query_id, "Commande introuvable", True); return
    if order["status"] != "en_attente":
        await answer_callback(query_id, "Commande deja prise ou terminee", True); return

    b4u_set_status(order_id, "en_cours", {
        "admin_username": admin_username,
        "taken_at": datetime.now().isoformat()
    })
    text = ("*[B4U] COMMANDE #" + str(order_id) + " PRISE EN CHARGE*\n\n"
        + ("@" + str(order["username"]) if order["username"] else "ID: " + str(order["user_id"])) + "\n"
        + str(order["service"]) + " - " + str(order["plan"]) + "\n"
        + str(order["price"]) + "€  |  Cout: " + str(order["cost"]) + "€\n\n"
        + "Pris par @" + admin_username + "\n"
        + datetime.now().strftime("%d/%m/%Y %H:%M"))
    kb = [[
        {"text": "Terminer", "callback_data": f"b4u_complete_{order_id}"},
        {"text": "Annuler",  "callback_data": f"b4u_cancel_{order_id}"}
    ],[
        {"text": "Remettre", "callback_data": f"b4u_restore_{order_id}"}
    ]]
    await edit_text(chat_id, message_id, text, kb)
    await answer_callback(query_id, "Prise en charge")

async def handle_b4u_complete(chat_id, message_id, query_id, order_id, admin_username):
    order = b4u_get_order(order_id)
    if not order:
        await answer_callback(query_id, "Introuvable", True); return
    if order["status"] == "terminee":
        await answer_callback(query_id, "Deja terminee", True); return
    b4u_set_status(order_id, "terminee")
    b4u_update_cumulative(order["price"], order["cost"])
    await edit_text(chat_id, message_id,
        "*[B4U] COMMANDE #" + str(order_id) + " TERMINEE*\n\nPar @" + admin_username + "\n" + datetime.now().strftime("%d/%m/%Y %H:%M"))
    await answer_callback(query_id, "Terminee")

async def handle_b4u_cancel(chat_id, message_id, query_id, order_id, admin_username):
    b4u_set_status(order_id, "annulee", {"cancelled_at": datetime.now().isoformat()})
    await edit_text(chat_id, message_id,
        "*[B4U] COMMANDE #" + str(order_id) + " ANNULEE*\n\nPar @" + admin_username + "\n" + datetime.now().strftime("%d/%m/%Y %H:%M"))
    await answer_callback(query_id, "Annulee")

async def handle_b4u_restore(chat_id, message_id, query_id, order_id):
    order = b4u_get_order(order_id)
    if not order:
        await answer_callback(query_id, "Introuvable", True); return
    b4u_set_status(order_id, "en_attente", {"admin_id": None, "admin_username": None, "taken_at": None})
    text = ("*[B4U] COMMANDE #" + str(order_id) + " REMISE EN LIGNE*\n\n"
        + ("@" + str(order["username"]) if order["username"] else "ID: " + str(order["user_id"])) + "\n"
        + str(order["service"]) + " - " + str(order["plan"]) + "\n"
        + str(order["price"]) + "€\n" + datetime.now().strftime("%d/%m/%Y %H:%M"))
    kb = [[{"text": "Prendre", "callback_data": f"b4u_take_{order_id}"}, {"text": "Annuler", "callback_data": f"b4u_cancel_{order_id}"}]]
    for admin_id in ADMIN_IDS:
        await send_message(VALIDATION_BOT_TOKEN, admin_id, text, kb)
    await answer_callback(query_id, "Remise en ligne")

# ─── HANDLERS KFC COMMANDES ───────────────────────────────────────────────────
async def handle_kfc_take(chat_id, message_id, query_id, order_id, user_id, product_id, admin_username):
    order = kfc_get_order(order_id)
    if not order:
        await answer_callback(query_id, "Commande introuvable", True); return
    if order["status"] not in ("pending", "awaiting_proof"):
        await answer_callback(query_id, "Deja prise ou terminee", True); return
    kfc_set_order_status(order_id, "processing", {"admin_username": admin_username, "taken_at": datetime.now().isoformat()})
    text = ("*[KFC] COMMANDE #" + str(order_id) + " PRISE EN CHARGE*\n\n"
        + "User ID: `" + str(user_id) + "`\n"
        + "Produit: " + str(product_id) + "\n\n"
        + "Pris par @" + admin_username + "\n"
        + datetime.now().strftime("%d/%m/%Y %H:%M"))
    kb = [[
        {"text": "Valider & Livrer", "callback_data": f"kfc_complete_{order_id}_{user_id}_{product_id}"},
        {"text": "Annuler", "callback_data": f"kfc_cancel_{order_id}_{user_id}"}
    ],[
        {"text": "Remettre", "callback_data": f"kfc_restore_{order_id}_{user_id}_{product_id}"}
    ]]
    await edit_caption(chat_id, message_id, text, kb)
    await answer_callback(query_id, "Prise en charge")

async def handle_kfc_complete(chat_id, message_id, query_id, order_id, user_id, product_id, admin_username):
    order = kfc_get_order(order_id)
    if not order:
        await answer_callback(query_id, "Introuvable", True); return
    if order["status"] == "completed":
        await answer_callback(query_id, "Deja terminee", True); return
    kfc_set_order_status(order_id, "completed")
    await notify_client(KFC_BOT_TOKEN, user_id, "Commande validee ! Ton compte KFC est en cours de livraison...")
    await edit_caption(chat_id, message_id,
        "*[KFC] COMMANDE #" + str(order_id) + " VALIDEE*\n\nPar @" + admin_username + "\n" + datetime.now().strftime("%d/%m/%Y %H:%M"))
    await answer_callback(query_id, "Validee")

async def handle_kfc_cancel(chat_id, message_id, query_id, order_id, user_id, admin_username):
    kfc_set_order_status(order_id, "rejected", {"cancelled_at": datetime.now().isoformat()})
    await notify_client(KFC_BOT_TOKEN, user_id, "Ta commande a ete annulee. Contacte l'admin si c'est une erreur.")
    await edit_caption(chat_id, message_id,
        "*[KFC] COMMANDE #" + str(order_id) + " ANNULEE*\n\nPar @" + admin_username + "\n" + datetime.now().strftime("%d/%m/%Y %H:%M"))
    await answer_callback(query_id, "Annulee")

async def handle_kfc_restore(chat_id, message_id, query_id, order_id, user_id, product_id):
    kfc_set_order_status(order_id, "awaiting_proof", {"admin_id": None, "admin_username": None, "taken_at": None})
    text = ("*[KFC] COMMANDE #" + str(order_id) + " REMISE EN LIGNE*\n\n"
        + "User ID: `" + str(user_id) + "`\nProduit: " + str(product_id) + "\n"
        + datetime.now().strftime("%d/%m/%Y %H:%M"))
    kb = [[{"text": "Prendre", "callback_data": f"kfc_take_{order_id}_{user_id}_{product_id}"}, {"text": "Annuler", "callback_data": f"kfc_cancel_{order_id}_{user_id}"}]]
    for admin_id in ADMIN_IDS:
        await send_message(VALIDATION_BOT_TOKEN, admin_id, text, kb)
    await answer_callback(query_id, "Remise en ligne")

# ─── HANDLERS KFC DEPOTS ──────────────────────────────────────────────────────
async def handle_kfc_validate_dep(chat_id, message_id, query_id, deposit_id, user_id, amount):
    try:
        new_balance = kfc_validate_deposit_atomic(deposit_id, user_id, amount)
    except Exception as e:
        logger.error(f"Erreur validation depot #{deposit_id}: {e}")
        await answer_callback(query_id, "Erreur: " + str(e), True); return
    await notify_client(KFC_BOT_TOKEN, user_id,
        str(amount) + "€ ajoutes a ton solde.\nNouveau solde: " + str(round(new_balance, 2)) + "€")
    await edit_caption(chat_id, message_id,
        "*[KFC] Depot #" + str(deposit_id) + " valide*\n\n`" + str(user_id) + "`\n"
        + str(amount) + "€ credites | Solde: " + str(round(new_balance, 2)) + "€\n"
        + datetime.now().strftime("%d/%m/%Y %H:%M"))
    await answer_callback(query_id, "Depot valide")

async def handle_kfc_reject_dep(chat_id, message_id, query_id, deposit_id, user_id):
    try:
        kfc_reject_deposit_atomic(deposit_id)
    except Exception as e:
        logger.error(f"Erreur rejet depot #{deposit_id}: {e}")
        await answer_callback(query_id, "Erreur: " + str(e), True); return
    await notify_client(KFC_BOT_TOKEN, user_id, "Ton depot a ete refuse. Contacte l'admin si c'est une erreur.")
    await edit_caption(chat_id, message_id,
        "*[KFC] Depot #" + str(deposit_id) + " rejete*\n\n`" + str(user_id) + "`\n"
        + datetime.now().strftime("%d/%m/%Y %H:%M"))
    await answer_callback(query_id, "Rejete")

# ─── HANDLERS KFC PAIEMENTS ───────────────────────────────────────────────────
async def handle_kfc_payment_callbacks(cq: dict):
    query_id = cq["id"]
    data = cq.get("data", "")
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    from_id = cq["from"]["id"]

    if from_id not in ADMIN_IDS:
        await answer_callback(query_id, "Non autorisé", True)
        return

    try:
        import psycopg2, psycopg2.extras, os
        KFC_DB = os.environ.get("DATABASE_URL", "")

        def kfc_db():
            conn = psycopg2.connect(KFC_DB)
            conn.cursor_factory = psycopg2.extras.RealDictCursor
            return conn

        # ── Valider commande PayPal ──
        if data.startswith("validate_") and not data.startswith("validatedep_"):
            order_id = int(data.split("_")[1])
            db = kfc_db(); cur = db.cursor()
            cur.execute("SELECT user_id, product_id FROM orders WHERE id = %s", (order_id,))
            row = cur.fetchone()
            if not row:
                await answer_callback(query_id, "Introuvable", True); db.close(); return
            cur.execute("UPDATE orders SET status = 'completed' WHERE id = %s", (order_id,))
            db.commit(); db.close()
            await send_message(KFC_BOT_TOKEN, row["user_id"],
                "✅ Ta commande PayPal a été validée ! Tu vas recevoir ton compte sous peu.")
            await edit_caption(chat_id, message_id, f"✅ Commande #{order_id} validée.")
            await answer_callback(query_id, "Validée")

        # ── Rejeter commande PayPal ──
        elif data.startswith("reject_") and not data.startswith("rejectdep_"):
            order_id = int(data.split("_")[1])
            db = kfc_db(); cur = db.cursor()
            cur.execute("SELECT user_id FROM orders WHERE id = %s", (order_id,))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE orders SET status = 'rejected' WHERE id = %s", (order_id,))
                db.commit()
                await send_message(KFC_BOT_TOKEN, row["user_id"],
                    "❌ Ta preuve PayPal a été refusée. Contacte l'admin si erreur.")
            db.close()
            await edit_caption(chat_id, message_id, f"❌ Commande #{order_id} rejetée.")
            await answer_callback(query_id, "Rejetée")

        # ── Valider dépôt PayPal ──
        elif data.startswith("validatedep_"):
            parts = data.split("_")
            deposit_id, user_id, amount = int(parts[1]), int(parts[2]), float(parts[3])
            db = kfc_db(); cur = db.cursor()
            cur.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, user_id))
            cur.execute("UPDATE deposits SET status = 'completed' WHERE id = %s", (deposit_id,))
            cur.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            new_balance = float(row["balance"]) if row else amount
            db.commit(); db.close()
            await send_message(KFC_BOT_TOKEN, user_id,
                f"✅ *{amount:.2f}€* ajoutés à ton solde.\n💰 Nouveau solde : *{round(new_balance,2):.2f}€*")
            await edit_caption(chat_id, message_id,
                f"✅ Dépôt #{deposit_id} de {amount:.2f}€ crédité.")
            await answer_callback(query_id, "Crédité")

        # ── Rejeter dépôt PayPal ──
        elif data.startswith("rejectdep_"):
            parts = data.split("_")
            deposit_id, user_id = int(parts[1]), int(parts[2])
            db = kfc_db(); cur = db.cursor()
            cur.execute("UPDATE deposits SET status = 'rejected' WHERE id = %s", (deposit_id,))
            db.commit(); db.close()
            await send_message(KFC_BOT_TOKEN, user_id,
                "❌ Ton dépôt PayPal a été refusé. Contacte l'admin si erreur.")
            await edit_caption(chat_id, message_id, f"❌ Dépôt #{deposit_id} rejeté.")
            await answer_callback(query_id, "Rejeté")

    except Exception as e:
        logger.error(f"[KFC_PAYMENT] Erreur: {e}")
        await answer_callback(query_id, f"Erreur: {str(e)[:50]}", True)

# ─── ROUTER ───────────────────────────────────────────────────────────────────
async def handle_callback_query(cq: dict):
    query_id  = cq["id"]
    data      = cq.get("data", "")
    chat_id   = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    from_id   = cq["from"]["id"]
    admin_username = cq["from"].get("username") or f"Admin_{from_id}"

    if from_id not in ADMIN_IDS:
        await answer_callback(query_id, "Non autorise", True); return

    try:
        # B4U
        if data.startswith("b4u_take_"):
            await handle_b4u_take(chat_id, message_id, query_id, int(data.split("_")[-1]), admin_username)
        elif data.startswith("b4u_complete_"):
            await handle_b4u_complete(chat_id, message_id, query_id, int(data.split("_")[-1]), admin_username)
        elif data.startswith("b4u_cancel_"):
            await handle_b4u_cancel(chat_id, message_id, query_id, int(data.split("_")[-1]), admin_username)
        elif data.startswith("b4u_restore_"):
            await handle_b4u_restore(chat_id, message_id, query_id, int(data.split("_")[-1]))

        # KFC commandes — kfc_xxx_<order_id>_<user_id>_<product_id>
        elif data.startswith("kfc_take_"):
            p = data.split("_")
            await handle_kfc_take(chat_id, message_id, query_id, int(p[2]), int(p[3]), p[4], admin_username)
        elif data.startswith("kfc_complete_"):
            p = data.split("_")
            await handle_kfc_complete(chat_id, message_id, query_id, int(p[2]), int(p[3]), p[4], admin_username)
        elif data.startswith("kfc_cancel_"):
            p = data.split("_")
            await handle_kfc_cancel(chat_id, message_id, query_id, int(p[2]), int(p[3]), admin_username)
        elif data.startswith("kfc_restore_"):
            p = data.split("_")
            await handle_kfc_restore(chat_id, message_id, query_id, int(p[2]), int(p[3]), p[4])

        # KFC depots — validatedep_<dep_id>_<user_id>_<amount>
        elif data.startswith("validatedep_"):
            p = data.split("_")
            await handle_kfc_validate_dep(chat_id, message_id, query_id, int(p[1]), int(p[2]), float(p[3]))
        elif data.startswith("rejectdep_"):
            p = data.split("_")
            await handle_kfc_reject_dep(chat_id, message_id, query_id, int(p[1]), int(p[2]))

        # KFC paiements PayPal (validate_ / reject_)
        elif data.startswith("validate_") or data.startswith("reject_"):
            await handle_kfc_payment_callbacks(cq)
        else:
            await answer_callback(query_id, "Action inconnue", True)

    except Exception as e:
        logger.error(f"Erreur handler {data}: {e}")
        await answer_callback(query_id, "Erreur interne: " + str(e)[:50], True)

async def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    from_id = msg["from"]["id"]
    text = msg.get("text", "")
    if text == "/start" and from_id in ADMIN_IDS:
        await send_message(VALIDATION_BOT_TOKEN, chat_id,
            "*Bot de validation centralise actif*\n\nReçoit les commandes de B4U Deals et KFC.")

# ─── POLLING LOOP ─────────────────────────────────────────────────────────────
async def polling_loop():
    offset = 0
    logger.info("Bot de validation centralise demarre (httpx polling)")
    while True:
        try:
            async with httpx.AsyncClient(timeout=35) as client:
                r = await client.post(
                    f"{TG_API}{VALIDATION_BOT_TOKEN}/getUpdates",
                    json={"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]}
                )
                data = r.json()
                if not data.get("ok"):
                    logger.error(f"getUpdates error: {data}")
                    await asyncio.sleep(5)
                    continue
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    if "callback_query" in update:
                        await handle_callback_query(update["callback_query"])
                    elif "message" in update:
                        await handle_message(update["message"])
        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    if not VALIDATION_BOT_TOKEN:
        logger.error("VALIDATION_BOT_TOKEN non configure !")
    else:
        asyncio.run(polling_loop())

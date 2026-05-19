"""
notify_bot.py — Bot de notifications paiements (@ValidB4uDeals_bot)
Gère uniquement les callbacks validatedep_ et rejectdep_.
Tourne en parallèle du bot principal.
"""
import asyncio
import logging
import os
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from config import NOTIFY_BOT_TOKEN, ADMIN_ID
from database import get_db, add_balance, get_balance

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

MAIN_BOT_TOKEN = os.environ.get("BOT_TOKEN", "")


async def handle_deposit_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_ids = ADMIN_ID if isinstance(ADMIN_ID, tuple) else (ADMIN_ID,)
    if query.from_user.id not in admin_ids:
        await query.answer("Non autorisé")
        return

    await query.answer()
    data = query.data

    try:
        if data.startswith("validatedep_"):
            parts = data.split("_")
            deposit_id, user_id, amount = int(parts[1]), int(parts[2]), float(parts[3])

            add_balance(user_id, amount)
            db = get_db()
            cur = db.cursor()
            cur.execute("UPDATE deposits SET status = 'completed' WHERE id = %s", (deposit_id,))
            db.commit()
            cur.close()
            db.close()

            balance = get_balance(user_id)

            # Notifier le client via le bot principal
            if MAIN_BOT_TOKEN:
                from telegram import Bot
                main_bot = Bot(token=MAIN_BOT_TOKEN)
                try:
                    await main_bot.send_message(
                        user_id,
                        f"✅ *{amount:.2f}€* ajoutés à ton solde.\n💰 Nouveau solde : *{balance:.2f}€*",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.warning(f"Impossible de notifier l'utilisateur {user_id}: {e}")

            try:
                await query.edit_message_caption(f"✅ Dépôt #{deposit_id} de {amount:.2f}€ crédité.")
            except Exception:
                await query.edit_message_text(f"✅ Dépôt #{deposit_id} de {amount:.2f}€ crédité.")

            logger.info(f"Dépôt #{deposit_id} validé — {amount}€ crédités à {user_id}")

        elif data.startswith("rejectdep_"):
            parts = data.split("_")
            deposit_id, user_id = int(parts[1]), int(parts[2])

            db = get_db()
            cur = db.cursor()
            cur.execute("UPDATE deposits SET status = 'rejected' WHERE id = %s", (deposit_id,))
            db.commit()
            cur.close()
            db.close()

            if MAIN_BOT_TOKEN:
                from telegram import Bot
                main_bot = Bot(token=MAIN_BOT_TOKEN)
                try:
                    await main_bot.send_message(
                        user_id,
                        "❌ Ton dépôt PayPal a été refusé. Contacte l'admin si erreur."
                    )
                except Exception as e:
                    logger.warning(f"Impossible de notifier l'utilisateur {user_id}: {e}")

            try:
                await query.edit_message_caption(f"❌ Dépôt #{deposit_id} rejeté.")
            except Exception:
                await query.edit_message_text(f"❌ Dépôt #{deposit_id} rejeté.")

            logger.info(f"Dépôt #{deposit_id} rejeté")

    except Exception as e:
        logger.error(f"Erreur callback dépôt: {e}", exc_info=True)
        try:
            await query.edit_message_text(f"❌ Erreur interne : {e}")
        except Exception:
            pass


async def main():
    if not NOTIFY_BOT_TOKEN:
        logger.error("NOTIFY_BOT_TOKEN non configuré")
        return

    app = Application.builder().token(NOTIFY_BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(
        handle_deposit_callbacks,
        pattern=r'^(validatedep_|rejectdep_)'
    ))

    logger.info("Notify bot démarré")
    await app.run_polling(allowed_updates=["callback_query"])


if __name__ == "__main__":
    asyncio.run(main())

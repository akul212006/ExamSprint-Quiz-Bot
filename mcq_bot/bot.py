import os
import logging
import threading
import signal
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler, PollAnswerHandler, TypeHandler

from config import BOT_TOKEN, WEBHOOK_URL
from handlers.admin import (
    CANCEL,
    STATUS,
    WAIT_LAUNCH_GROUPS,
    launch_start,
    launch_groups,
    register_group_chat,
    start_quiz,
    handle_pdf,
    handle_count,
    handle_timer,
)
from handlers.leaderboard import leaderboard_command
from handlers.quiz import join_quiz, handle_poll_answer

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def start_command(update: Update, context) -> None:
    await update.message.reply_text("Bot is running. Use /startquiz to begin.")


def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("startquiz", start_quiz),
        ],
        states={
            1: [MessageHandler(filters.Document.PDF, handle_pdf)],
            2: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_count)],
            3: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_timer)],
        },
        fallbacks=[CommandHandler("cancel", CANCEL)],
        per_user=True,
        per_chat=False,
    )

    launch_handler = ConversationHandler(
        entry_points=[CommandHandler("launch", launch_start)],
        states={
            WAIT_LAUNCH_GROUPS: [MessageHandler(filters.TEXT & ~filters.COMMAND, launch_groups)],
        },
        fallbacks=[CommandHandler("cancel", CANCEL)],
        per_user=True,
        per_chat=False,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("join", join_quiz))
    application.add_handler(launch_handler)
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("status", STATUS))
    application.add_handler(PollAnswerHandler(handle_poll_answer))

    async def debug_all(update, context):
        logger.info(f"UPDATE RECEIVED: {update.update_id} - {update.effective_message}")

    application.add_handler(TypeHandler(Update, debug_all), group=999)

    return application


def main() -> None:
    application = build_app()

    logger.info("Bot is running...")

    if WEBHOOK_URL:
        application.run_webhook(
            listen="0.0.0.0",
            port=8080,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        )
    else:
        signal.signal(signal.SIGTERM, lambda s, f: logger.warning("Ignoring SIGTERM to keep bot running"))
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
            close_loop=False,
            stop_signals=None
        )


# ===== RENDER PORT BINDING - DO NOT REMOVE =====
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/ping' or self.path == '/':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, format, *args):
        pass

def run_ping_server():
    port = int(os.environ.get('PORT', 8080))
    server = HTTPServer(('0.0.0.0', port), PingHandler)
    server.serve_forever()

threading.Thread(target=run_ping_server, daemon=True).start()
# ===== END RENDER PORT BINDING =====


if __name__ == '__main__':
    while True:
        try:
            main()
        except Exception as e:
            logger.error(f"Bot crashed: {e}. Restarting in 5 seconds...")
            import time
            time.sleep(5)

import asyncio
import logging
import os
import signal
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    PollAnswerHandler,
    filters,
)

from config import BOT_TOKEN, WEBHOOK_URL
from handlers.admin import (
    CANCEL,
    STATUS,
    WAIT_LAUNCH_GROUPS,
    admin_menu,
    menu_action_callback,
    menu_start_quiz_callback,
    launch_action_callback,
    launch_toggle_callback,
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
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("startquiz", start_quiz),
            CallbackQueryHandler(menu_start_quiz_callback, pattern=r"^menu_start_quiz$"),
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
    application.add_handler(launch_handler)
    application.add_handler(CommandHandler("menu", admin_menu))
    application.add_handler(CommandHandler("status", STATUS))
    application.add_handler(CommandHandler("join", join_quiz))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(MessageHandler(filters.ChatType.GROUPS, register_group_chat))
    application.add_handler(
        CallbackQueryHandler(launch_toggle_callback, pattern=r"^launch_toggle:")
    )
    application.add_handler(
        CallbackQueryHandler(launch_action_callback, pattern=r"^launch_(selected|all|cancel)$")
    )
    application.add_handler(CallbackQueryHandler(menu_action_callback, pattern=r"^menu_(?!start_quiz$)"))
    application.add_handler(CallbackQueryHandler(join_quiz, pattern=r"^join_quiz$"))
    application.add_handler(PollAnswerHandler(handle_poll_answer))

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
        def _signal_handler(signum, frame):
            logger.warning("Ignoring signal %s to keep bot running.", signum)

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
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


if __name__ == "__main__":
    main()

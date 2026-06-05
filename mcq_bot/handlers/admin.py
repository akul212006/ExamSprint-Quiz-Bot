import asyncio
import logging
import os
import random
import tempfile
import uuid
from typing import Dict, List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import ContextTypes, ConversationHandler

from config import ADMIN_USER_IDS, JOIN_WINDOW_SECONDS
from handlers.quiz import send_group_question
from services.pdf_parser import parse_mcqs_from_pdf
from services import redis_service

logger = logging.getLogger(__name__)

WAIT_PDF = 1
WAIT_COUNT = 2
WAIT_TIMER = 3
WAIT_LAUNCH_GROUPS = 4


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


async def _can_launch_in_group(context: ContextTypes.DEFAULT_TYPE, user_id: int, group_id: int) -> bool:
    if _is_admin(user_id):
        return True
    try:
        member = await context.bot.get_chat_member(group_id, user_id)
    except Exception:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def register_group_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    title = update.effective_chat.title or str(update.effective_chat.id)
    await redis_service.register_group(update.effective_chat.id, title)


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else 0
    if not _is_admin(user_id):
        await update.message.reply_text("You are not authorized to use this menu.")
        return

    keyboard = [
        [InlineKeyboardButton("Start Quiz", callback_data="menu_start_quiz")],
        [InlineKeyboardButton("Upload PDF", callback_data="menu_upload_pdf")],
        [InlineKeyboardButton("Set Question Count", callback_data="menu_set_count")],
        [InlineKeyboardButton("Set Timer", callback_data="menu_set_timer")],
        [InlineKeyboardButton("Launch Quiz", callback_data="menu_launch")],
        [InlineKeyboardButton("Status", callback_data="menu_status")],
        [InlineKeyboardButton("Cancel", callback_data="menu_cancel")],
    ]
    await update.message.reply_text("Admin Menu", reply_markup=InlineKeyboardMarkup(keyboard))


async def menu_start_quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    user_id = query.from_user.id if query.from_user else 0
    if not _is_admin(user_id):
        await query.edit_message_text("You are not authorized to start a quiz.")
        return ConversationHandler.END

    await query.edit_message_text("Send the PDF containing MCQs.")
    return WAIT_PDF


async def menu_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user_id = query.from_user.id if query.from_user else 0
    if not _is_admin(user_id):
        await query.edit_message_text("You are not authorized to use this menu.")
        return

    action = query.data or ""
    if action == "menu_status":
        await query.edit_message_text("Use /status to view activity.")
        return
    if action == "menu_launch":
        await query.edit_message_text("Use /launch in DM to select groups.")
        return
    if action == "menu_cancel":
        await redis_service.clear_quiz_config()
        await query.edit_message_text("Quiz cancelled.")
        return
    if action == "menu_upload_pdf":
        await query.edit_message_text("Send the PDF file here in DM.")
        return
    if action == "menu_set_count":
        await query.edit_message_text("After uploading the PDF, send question count or 'all'.")
        return
    if action == "menu_set_timer":
        await query.edit_message_text("After count, send timer in seconds (5-120).")
        return

    await query.edit_message_text("Use /startquiz to begin.")


async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info(f"startquiz called by user_id: {update.effective_user.id}")

    await update.message.reply_text("Send the PDF containing MCQs.")
    return WAIT_PDF


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id if update.effective_user else 0
    if not _is_admin(user_id):
        await update.message.reply_text("You are not authorized to start a quiz.")
        return ConversationHandler.END
    if not update.message or not update.message.document:
        await update.message.reply_text("Please send a PDF file.")
        return WAIT_PDF

    document = update.message.document
    file = await document.get_file()

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
            temp_path = temp_file.name
            await file.download_to_drive(custom_path=temp_path)

        questions = parse_mcqs_from_pdf(temp_path)

        if len(questions) < 5:
            await update.message.reply_text("Not enough questions found in PDF. Try again.")
            return ConversationHandler.END

        context.user_data["questions"] = questions
        await update.message.reply_text(
            f"Found {len(questions)} questions! How many do you want to use? (send a number, or 'all')"
        )
        return WAIT_COUNT
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                logger.exception("Failed to delete temp PDF")


async def handle_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id if update.effective_user else 0
    if not _is_admin(user_id):
        await update.message.reply_text("You are not authorized to start a quiz.")
        return ConversationHandler.END
    text = (update.message.text or "").strip().lower()
    questions: List[dict] = context.user_data.get("questions", [])

    if text == "all":
        count = len(questions)
    else:
        if not text.isdigit():
            await update.message.reply_text("Please send a number or 'all'.")
            return WAIT_COUNT
        count = int(text)
        if count < 1 or count > len(questions):
            await update.message.reply_text("Invalid number of questions. Try again.")
            return WAIT_COUNT

    context.user_data["question_count"] = count
    await update.message.reply_text("Set timer per question in seconds? (e.g. 10, 15, 30)")
    return WAIT_TIMER


async def handle_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id if update.effective_user else 0
    if not _is_admin(user_id):
        await update.message.reply_text("You are not authorized to start a quiz.")
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("Please send a number between 5 and 120.")
        return WAIT_TIMER

    timer = int(text)
    if timer < 5 or timer > 120:
        await update.message.reply_text("Please send a number between 5 and 120.")
        return WAIT_TIMER

    questions: List[dict] = context.user_data.get("questions", [])
    count = int(context.user_data.get("question_count", len(questions)))

    random.shuffle(questions)
    selected = questions[:count]

    quiz_config = {
        "questions": selected,
        "timer_seconds": timer,
        "total_questions": len(selected),
    }

    await redis_service.set_quiz_config(quiz_config)

    await update.message.reply_text(
        "Ready! Send /launch in DM to choose groups, or /cancel to abort"
    )
    return ConversationHandler.END


async def _start_after_window(context: ContextTypes.DEFAULT_TYPE, group_id: int) -> None:
    await asyncio.sleep(JOIN_WINDOW_SECONDS)

    pending = await redis_service.get_pending_quiz(group_id)
    if not pending:
        return

    joiners = await redis_service.get_joiners(group_id)
    if not joiners:
        await context.bot.send_message(
            chat_id=group_id,
            text="No one joined the quiz. Cancelled.",
        )
        await redis_service.clear_pending_quiz(group_id)
        await redis_service.clear_joiners(group_id)
        return

    quiz_id = pending["quiz_id"]
    await redis_service.set_current_quiz_id(group_id, quiz_id)

    for user_id in joiners:
        await redis_service.add_quiz_user(quiz_id, user_id)

    await redis_service.set_group_quiz_state(
        group_id,
        {
            "group_id": group_id,
            "quiz_id": quiz_id,
            "questions": pending["questions"],
            "timer_seconds": pending["timer_seconds"],
            "total_questions": pending["total_questions"],
            "current_index": 0,
            "poll_id": None,
        },
    )

    await send_group_question(context, group_id)

    await redis_service.clear_pending_quiz(group_id)
    await redis_service.clear_joiners(group_id)


def _build_group_keyboard(groups: List[Dict[str, int]], selected: List[int]) -> InlineKeyboardMarkup:
    buttons = []
    selected_set = set(selected)
    for group in groups:
        group_id = int(group["id"])
        label = f"[x] {group['title']}" if group_id in selected_set else f"[ ] {group['title']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"launch_toggle:{group_id}")])

    buttons.append(
        [
            InlineKeyboardButton("Launch Selected", callback_data="launch_selected"),
            InlineKeyboardButton("Launch All", callback_data="launch_all"),
        ]
    )
    buttons.append([InlineKeyboardButton("Cancel", callback_data="launch_cancel")])
    return InlineKeyboardMarkup(buttons)


async def launch_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id if update.effective_user else 0
    if update.effective_chat and update.effective_chat.type != "private":
        await update.message.reply_text("Use /launch in DM to choose groups.")
        return ConversationHandler.END

    quiz_config = await redis_service.get_quiz_config()
    if not quiz_config:
        await update.message.reply_text("No pending quiz to launch.")
        return ConversationHandler.END

    groups = await redis_service.list_groups()
    if not groups:
        await update.message.reply_text("No groups found yet. Add the bot to a group and send any message.")
        return ConversationHandler.END

    group_map: Dict[str, int] = {}
    for idx, group in enumerate(groups, start=1):
        group_map[str(idx)] = int(group["id"])

    context.user_data["launch_groups"] = group_map
    context.user_data["launch_group_list"] = groups
    context.user_data["launch_selected"] = []

    await update.message.reply_text(
        "Select groups to launch:",
        reply_markup=_build_group_keyboard(groups, []),
    )
    await update.message.reply_text("You can also reply with numbers like: 1,3 or 'all'.")
    return WAIT_LAUNCH_GROUPS


async def _launch_to_groups(context: ContextTypes.DEFAULT_TYPE, user_id: int, selected_ids: List[int]) -> List[str]:
    quiz_config = await redis_service.get_quiz_config()
    if not quiz_config:
        return ["No pending quiz to launch."]

    launched = []
    skipped = []
    for group_id in selected_ids:
        if not await _can_launch_in_group(context, user_id, group_id):
            skipped.append(str(group_id))
            continue

        pending_quiz = {
            "quiz_id": str(uuid.uuid4()),
            "questions": quiz_config["questions"],
            "timer_seconds": quiz_config["timer_seconds"],
            "total_questions": quiz_config["total_questions"],
        }

        await redis_service.set_pending_quiz(group_id, pending_quiz)
        await redis_service.clear_joiners(group_id)
        await redis_service.set_joining_active(group_id, JOIN_WINDOW_SECONDS)

        try:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Join Quiz", callback_data="join_quiz")]]
            )
            await context.bot.send_message(
                chat_id=group_id,
                text=(
                    "🚨 Quiz starting! Send /join to participate. "
                    f"You have {JOIN_WINDOW_SECONDS} seconds to join."
                ),
                reply_markup=keyboard,
            )
            asyncio.create_task(_start_after_window(context, group_id))
            launched.append(str(group_id))
        except Exception:
            logger.exception("Failed to send launch message to group %s", group_id)
            skipped.append(str(group_id))

    await redis_service.clear_quiz_config()

    launched_names = [await redis_service.get_group_title(int(group_id)) for group_id in launched]
    skipped_names = [await redis_service.get_group_title(int(group_id)) for group_id in skipped]

    response_lines = ["Launch complete."]
    if launched_names:
        response_lines.append(f"Launched in: {', '.join(launched_names)}")
    if skipped_names:
        response_lines.append(f"Skipped (no permission): {', '.join(skipped_names)}")

    return response_lines


async def launch_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id if update.effective_user else 0
    group_map: Dict[str, int] = context.user_data.get("launch_groups", {})
    text = (update.message.text or "").strip().lower()

    if not group_map:
        await update.message.reply_text("No groups available. Use /launch again.")
        return ConversationHandler.END

    if text == "all":
        selected_ids = list(group_map.values())
    else:
        tokens = [token.strip() for token in text.replace(" ", "").split(",") if token.strip()]
        selected_ids = [group_map[token] for token in tokens if token in group_map]

    if not selected_ids:
        await update.message.reply_text("Invalid selection. Reply with numbers or 'all'.")
        return WAIT_LAUNCH_GROUPS

    response_lines = await _launch_to_groups(context, user_id, selected_ids)
    await update.message.reply_text("\n".join(response_lines))
    return ConversationHandler.END


async def launch_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    group_list = context.user_data.get("launch_group_list", [])
    selected = context.user_data.get("launch_selected", [])
    if not group_list:
        await query.edit_message_text("No groups available. Use /launch again.")
        return

    data = query.data or ""
    try:
        group_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return

    if group_id in selected:
        selected.remove(group_id)
    else:
        selected.append(group_id)

    context.user_data["launch_selected"] = selected
    await query.edit_message_reply_markup(reply_markup=_build_group_keyboard(group_list, selected))


async def launch_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user_id = query.from_user.id if query.from_user else 0
    group_list = context.user_data.get("launch_group_list", [])
    selected = context.user_data.get("launch_selected", [])

    if not group_list:
        await query.edit_message_text("No groups available. Use /launch again.")
        return

    if query.data == "launch_cancel":
        await query.edit_message_text("Launch cancelled.")
        return

    if query.data == "launch_all":
        selected_ids = [int(group["id"]) for group in group_list]
    else:
        selected_ids = list(selected)

    if not selected_ids:
        await query.edit_message_text("No groups selected.")
        return

    response_lines = await _launch_to_groups(context, user_id, selected_ids)
    await query.edit_message_text("\n".join(response_lines))


async def CANCEL(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else 0
    if not _is_admin(user_id):
        await update.message.reply_text("You are not authorized to cancel a quiz.")
        return

    await redis_service.clear_quiz_config()
    await update.message.reply_text("Quiz cancelled.")


async def STATUS(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else 0
    if not _is_admin(user_id):
        await update.message.reply_text("You are not authorized to check status.")
        return

    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        group_id = update.effective_chat.id
        quiz_id = await redis_service.get_current_quiz_id(group_id)
        if quiz_id:
            count = await redis_service.quiz_user_count(quiz_id)
            await update.message.reply_text(f"Active users in quiz: {count}")
            return
        if await redis_service.is_joining_active(group_id):
            count = await redis_service.joiner_count(group_id)
            await update.message.reply_text(f"Users joined so far: {count}")
            return
        await update.message.reply_text("No active quiz right now.")
        return

    groups = await redis_service.list_groups()
    active_lines = []
    for group in groups:
        group_id = int(group["id"])
        quiz_id = await redis_service.get_current_quiz_id(group_id)
        if quiz_id:
            count = await redis_service.quiz_user_count(quiz_id)
            active_lines.append(f"{group['title']}: active ({count} users)")
            continue
        if await redis_service.is_joining_active(group_id):
            count = await redis_service.joiner_count(group_id)
            active_lines.append(f"{group['title']}: joining ({count} users)")

    if active_lines:
        await update.message.reply_text("\n".join(active_lines))
        return

    await update.message.reply_text("No active quiz right now.")

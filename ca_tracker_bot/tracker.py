import re
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError

# Configuration
TOKEN = "8224822340:AAHBwPhk4i9K7jLVz_V-6z7zIVjGYhAdkeY"

# Pump.fun CA format (base58 encoded address ending with "pump")
# Base58: includes 0-9 and letters, excluding 0, O, I, l
CA_PATTERN = r'[1-9A-HJ-NP-Za-km-z]{32,44}pump'

# In-memory storage (use database for persistence)
tracked_users = {}  # {group_id: {username: user_id_who_tracked}}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start command"""
    await update.message.reply_text(
        "🤖 Memecoin CA Tracker Bot\n\n"
        "Commands:\n"
        "/track @username - Track a user's CAs\n"
        "/untrack @username - Stop tracking a user\n"
        "/list - Show tracked users in this group\n"
        "/help - Show this message"
    )

async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track a user's contract addresses"""
    if not context.args:
        await update.message.reply_text("❌ Usage: /track @username")
        return
    
    username = context.args[0].lstrip('@').lower()
    group_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if group_id not in tracked_users:
        tracked_users[group_id] = {}
    
    tracked_users[group_id][username] = user_id
    print(f"✅ Tracking @{username} for user {user_id} in group {group_id}")
    print(f"Tracked users now: {tracked_users[group_id]}")
    await update.message.reply_text(f"✅ Now tracking @{username}'s CAs. Forwards go to your private chat!")

async def untrack_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop tracking a user"""
    if not context.args:
        await update.message.reply_text("❌ Usage: /untrack @username")
        return
    
    username = context.args[0].lstrip('@').lower()
    group_id = update.effective_chat.id
    
    if group_id in tracked_users and username in tracked_users[group_id]:
        del tracked_users[group_id][username]
        await update.message.reply_text(f"✅ Stopped tracking @{username}")
    else:
        await update.message.reply_text(f"❌ @{username} is not being tracked")

async def list_tracked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all tracked users in this group"""
    group_id = update.effective_chat.id
    
    if group_id not in tracked_users or not tracked_users[group_id]:
        await update.message.reply_text("📭 No users are being tracked in this group")
        return
    
    user_list = "\n".join(f"• @{user}" for user in tracked_users[group_id])
    await update.message.reply_text(f"📋 Tracked users:\n{user_list}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming messages and forward CAs from tracked users"""
    message = update.message
    if not message or not message.text:
        return
    
    group_id = message.chat.id
    username = message.from_user.username.lower() if message.from_user.username else None
    
    # Debug: print incoming messages
    print(f"Message from @{username}: {message.text}")
    print(f"Tracked users in group {group_id}: {tracked_users.get(group_id, set())}")
    
    # Check if user is tracked
    if group_id not in tracked_users or username not in tracked_users[group_id]:
        print(f"User @{username} not being tracked")
        return
    
    # Search for contract addresses in message
    print(f"Searching in text: '{message.text}'")
    print(f"Using pattern: {CA_PATTERN}")
    cas = re.findall(CA_PATTERN, message.text)
    print(f"CAs found: {cas}")
    
    if not cas:
        return
    
    # Get the user ID who is tracking this person
    user_id = tracked_users[group_id][username]
    
    # Forward to that user's private chat
    try:
        ca_list = "\n".join(f"• {ca}" for ca in cas)
        forward_text = (
            f"📌 New CA from @{username} in {message.chat.title}\n\n"
            f"Contract Addresses:\n{ca_list}\n\n"
            f"Message: {message.text}"
        )
        print(f"Attempting to send to {user_id}: {forward_text}")
        await context.bot.send_message(chat_id=user_id, text=forward_text)
        print(f"✅ Message sent successfully!")
    except TelegramError as e:
        print(f"❌ Error forwarding message: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Help command"""
    await start(update, context)

def main():
    """Start the bot"""
    app = Application.builder().token(TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("track", track_user))
    app.add_handler(CommandHandler("untrack", untrack_user))
    app.add_handler(CommandHandler("list", list_tracked))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Start bot
    print("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()

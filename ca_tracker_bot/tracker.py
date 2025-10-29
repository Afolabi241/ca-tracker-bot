import re
import json
import time
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError, RetryAfter, TimedOut

# Configuration
TOKEN = "8224822340:AAHBwPhk4i9K7jLVz_V-6z7zIVjGYhAdkeY"

# Contract address patterns for different blockchains
CA_PATTERNS = {
    'Ethereum/BSC/Base/Polygon': r'0x[a-fA-F0-9]{40}',  # EVM chains
    'Solana': r'[1-9A-HJ-NP-Za-km-z]{32,44}',  # Base58
    'Tron': r'T[1-9A-HJ-NP-Za-km-z]{33}',  # Tron addresses start with T
    'Sui': r'0x[a-fA-F0-9]{64}',  # Sui uses 64-char hex
}

def detect_blockchain(address):
    """Detect which blockchain a CA belongs to"""
    # Check Tron first (starts with T)
    if re.match(CA_PATTERNS['Tron'], address):
        return 'tron', 'Tron', address
    
    # Check Sui (64-char hex)
    if re.match(CA_PATTERNS['Sui'], address):
        return 'sui', 'Sui', address
    
    # Check EVM chains (40-char hex with 0x)
    if re.match(CA_PATTERNS['Ethereum/BSC/Base/Polygon'], address):
        return 'ethereum', 'EVM (ETH/BSC/Base/Polygon)', address
    
    # Check Solana (base58, 32-44 chars)
    if re.match(CA_PATTERNS['Solana'], address):
        # Filter out common words that match the pattern
        if len(address) >= 32 and not address.lower() in ['pump', 'moon', 'ape']:
            return 'solana', 'Solana', address
    
    return None, None, None

async def get_token_info(chain_id, address):
    """Fetch token info from DexScreener API"""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    if data.get('pairs') and len(data['pairs']) > 0:
                        # Get the most liquid pair
                        pair = data['pairs'][0]
                        
                        name = pair.get('baseToken', {}).get('name', 'Unknown')
                        symbol = pair.get('baseToken', {}).get('symbol', 'N/A')
                        mcap = pair.get('marketCap', 0)
                        
                        # Format market cap
                        if mcap >= 1_000_000:
                            mcap_str = f"${mcap / 1_000_000:.2f}M"
                        elif mcap >= 1_000:
                            mcap_str = f"${mcap / 1_000:.2f}K"
                        else:
                            mcap_str = f"${mcap:.2f}"
                        
                        return {
                            'name': name,
                            'symbol': symbol,
                            'mcap': mcap_str,
                            'price': pair.get('priceUsd', 'N/A')
                        }
        
        return None
    except Exception as e:
        print(f"Error fetching token info: {e}")
        return None

# Storage file
STORAGE_FILE = 'tracked_users.json'

# In-memory storage (use database for persistence)
tracked_users = {}  # {group_id: {username: [user_ids_tracking]}}

def load_tracked_users():
    """Load tracked users from file"""
    global tracked_users
    try:
        with open(STORAGE_FILE, 'r') as f:
            # Convert string keys back to integers for group_ids
            data = json.load(f)
            tracked_users = {int(k): v for k, v in data.items()}
            print(f"✅ Loaded {len(tracked_users)} groups from storage")
    except FileNotFoundError:
        print("ℹ️ No storage file found, starting fresh")
        tracked_users = {}
    except Exception as e:
        print(f"❌ Error loading storage: {e}")
        tracked_users = {}

def save_tracked_users():
    """Save tracked users to file"""
    try:
        with open(STORAGE_FILE, 'w') as f:
            json.dump(tracked_users, f)
        print("✅ Saved tracked users to storage")
    except Exception as e:
        print(f"❌ Error saving storage: {e}")

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
    # Only allow tracking in groups
    if update.effective_chat.type == 'private':
        await update.message.reply_text("❌ Please use this command in a group chat, not in DMs.")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Usage: /track @username")
        return
    
    username = context.args[0].lstrip('@').lower()
    group_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if group_id not in tracked_users:
        tracked_users[group_id] = {}
    
    if username not in tracked_users[group_id]:
        tracked_users[group_id][username] = []
    
    if user_id not in tracked_users[group_id][username]:
        tracked_users[group_id][username].append(user_id)
        save_tracked_users()
        print(f"✅ User {user_id} tracking @{username} in group {group_id}")
        print(f"Tracked users now: {tracked_users[group_id]}")
        await update.message.reply_text(f"✅ Now tracking @{username}'s CAs. Forwards go to your private chat!")
    else:
        await update.message.reply_text(f"ℹ️ You're already tracking @{username}")

async def untrack_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop tracking a user"""
    # Only allow untracking in groups
    if update.effective_chat.type == 'private':
        await update.message.reply_text("❌ Please use this command in a group chat, not in DMs.")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Usage: /untrack @username")
        return
    
    username = context.args[0].lstrip('@').lower()
    group_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if group_id in tracked_users and username in tracked_users[group_id]:
        if user_id in tracked_users[group_id][username]:
            tracked_users[group_id][username].remove(user_id)
            if not tracked_users[group_id][username]:
                del tracked_users[group_id][username]
            save_tracked_users()
            await update.message.reply_text(f"✅ Stopped tracking @{username}")
        else:
            await update.message.reply_text(f"❌ You're not tracking @{username}")
    else:
        await update.message.reply_text(f"❌ @{username} is not being tracked")

async def list_tracked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all tracked users in this group"""
    # Only allow listing in groups
    if update.effective_chat.type == 'private':
        await update.message.reply_text("❌ Please use this command in a group chat, not in DMs.")
        return
    
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
    
    detected_cas = []
    words = message.text.split()
    
    for word in words:
        # Clean up the word (remove common punctuation)
        clean_word = word.strip('.,!?()[]{}')
        chain_id, blockchain, ca = detect_blockchain(clean_word)
        
        if blockchain and ca:
            detected_cas.append((chain_id, blockchain, ca))
            print(f"Found {blockchain} CA: {ca}")
    
    if not detected_cas:
        print("No CAs found")
        return
    
    # Get all user IDs tracking this person
    user_ids = tracked_users[group_id][username]
    
    # Forward to all users tracking this person
    for user_id in user_ids:
        try:
            # Send info first
            info_text = f"📌 New CA from @{username} in {message.chat.title}"
            await context.bot.send_message(chat_id=user_id, text=info_text)
            
            # Send each CA with blockchain info and token details
            for chain_id, blockchain, ca in detected_cas:
                # Fetch token info
                token_info = await get_token_info(chain_id, ca)
                
                if token_info:
                    # Send token info first
                    info_msg = (
                        f"🔗 {blockchain}\n"
                        f"💎 {token_info['name']} (${token_info['symbol']})\n"
                        f"💰 Market Cap: {token_info['mcap']}"
                    )
                    await context.bot.send_message(chat_id=user_id, text=info_msg)
                    
                    # Send CA as separate message for easy copying
                    await context.bot.send_message(chat_id=user_id, text=ca)
                else:
                    # If no token info, send blockchain + CA together
                    await context.bot.send_message(chat_id=user_id, text=f"🔗 {blockchain}")
                    await context.bot.send_message(chat_id=user_id, text=ca)
                
                # Retry logic for sending messages
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        await context.bot.send_message(chat_id=user_id, text=ca_text)
                        break
                    except RetryAfter as e:
                        print(f"Rate limited, waiting {e.retry_after} seconds...")
                        time.sleep(e.retry_after)
                    except TimedOut:
                        print(f"Timeout on attempt {attempt + 1}, retrying...")
                        if attempt < max_retries - 1:
                            time.sleep(2)
                        else:
                            raise
            
            print(f"✅ Message sent to {user_id} successfully!")
        except TelegramError as e:
            print(f"❌ Error forwarding to {user_id}: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Help command"""
    await start(update, context)

def main():
    """Start the bot"""
    # Load tracked users from storage
    load_tracked_users()
    
    app = Application.builder().token(TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("track", track_user))
    app.add_handler(CommandHandler("untrack", untrack_user))
    app.add_handler(CommandHandler("list", list_tracked))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Start bot with better error handling
    print("Bot started...")
    app.run_polling(
        allowed_updates=["message"],
        drop_pending_updates=False
    )

if __name__ == "__main__":
    main()

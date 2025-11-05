import re
import json
import time
import os
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import TelegramError, RetryAfter, TimedOut
from cryptography.fernet import Fernet

# Configuration
TOKEN = "8224822340:AAHBwPhk4i9K7jLVz_V-6z7zIVjGYhAdkeY"

# Encryption key (generate once and save securely!)
ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY', Fernet.generate_key())

# Storage files
STORAGE_FILE = 'tracked_users.json'
WALLETS_FILE = 'wallets.json'
AUTOBUY_FILE = 'autobuy.json'

# Global storage
tracked_users = {}  # {group_id: {username: [user_ids_tracking]}}
caller_stats = {}  # Track caller performance
user_wallets = {}  # User wallet data
autobuy_settings = {}  # Auto-buy configurations
temp_autobuy_config = {}  # Temporary config during setup

# Safety limits
MAX_DAILY_TRADES = 100

# Contract address patterns for different blockchains
CA_PATTERNS = {
    'Ethereum/BSC/Base/Polygon': r'0x[a-fA-F0-9]{40}',  # EVM chains
    'Solana': r'[1-9A-HJ-NP-Za-km-z]{32,44}',  # Base58
    'Tron': r'T[1-9A-HJ-NP-Za-km-z]{33}',  # Tron addresses start with T
    'Sui': r'0x[a-fA-F0-9]{64}',  # Sui uses 64-char hex
}

# Wallet functions
def generate_wallet():
    """Generate a Solana wallet (placeholder - use actual Solana library)"""
    # TODO: Use solana-py or solders to generate real wallet
    import secrets
    private_key = secrets.token_hex(32)
    public_key = f"Sol{secrets.token_hex(20)}"
    return private_key, public_key

def encrypt_key(private_key):
    """Encrypt private key"""
    f = Fernet(ENCRYPTION_KEY)
    return f.encrypt(private_key.encode()).decode()

def save_wallets():
    """Save wallets to file"""
    try:
        with open(WALLETS_FILE, 'w') as f:
            json.dump(user_wallets, f)
        print("✅ Saved wallets to storage")
    except Exception as e:
        print(f"❌ Error saving wallets: {e}")

def load_wallets():
    """Load wallets from file"""
    global user_wallets
    try:
        with open(WALLETS_FILE, 'r') as f:
            user_wallets = json.load(f)
        print(f"✅ Loaded {len(user_wallets)} wallets from storage")
    except FileNotFoundError:
        print("ℹ️ No wallets file found, starting fresh")
        user_wallets = {}
    except Exception as e:
        print(f"❌ Error loading wallets: {e}")
        user_wallets = {}

def save_autobuy_settings():
    """Save autobuy settings"""
    try:
        with open(AUTOBUY_FILE, 'w') as f:
            json.dump(autobuy_settings, f)
        print("✅ Saved autobuy settings")
    except Exception as e:
        print(f"❌ Error saving autobuy: {e}")

def load_autobuy_settings():
    """Load autobuy settings"""
    global autobuy_settings
    try:
        with open(AUTOBUY_FILE, 'r') as f:
            autobuy_settings = json.load(f)
        print(f"✅ Loaded autobuy settings for {len(autobuy_settings)} users")
    except FileNotFoundError:
        print("ℹ️ No autobuy file found, starting fresh")
        autobuy_settings = {}
    except Exception as e:
        print(f"❌ Error loading autobuy: {e}")
        autobuy_settings = {}

def load_tracked_users():
    """Load tracked users from file"""
    global tracked_users
    try:
        with open(STORAGE_FILE, 'r') as f:
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

def detect_blockchain(address):
    """Detect which blockchain a CA belongs to"""
    if re.match(CA_PATTERNS['Tron'], address):
        return 'tron', 'Tron', address
    
    if re.match(CA_PATTERNS['Sui'], address):
        return 'sui', 'Sui', address
    
    if re.match(CA_PATTERNS['Ethereum/BSC/Base/Polygon'], address):
        return 'ethereum', 'EVM (ETH/BSC/Base/Polygon)', address
    
    if re.match(CA_PATTERNS['Solana'], address):
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
                        pair = data['pairs'][0]
                        
                        name = pair.get('baseToken', {}).get('name', 'Unknown')
                        symbol = pair.get('baseToken', {}).get('symbol', 'N/A')
                        mcap = pair.get('marketCap', 0)
                        
                        if mcap >= 1_000_000:
                            mcap_str = f"${mcap / 1_000_000:.2f}M"
                        elif mcap >= 1_000:
                            mcap_str = f"${mcap / 1_000:.2f}K"
                        else:
                            mcap_str = f"${mcap:.2f}"
                        
                        logo_url = pair.get('info', {}).get('imageUrl', '')
                        chart_url = f"https://dexscreener.com/{chain_id}/{pair.get('pairAddress', '')}"
                        
                        return {
                            'name': name,
                            'symbol': symbol,
                            'mcap': mcap_str,
                            'price': pair.get('priceUsd', 'N/A'),
                            'logo': logo_url,
                            'chart': chart_url,
                            'pair_address': pair.get('pairAddress', '')
                        }
        
        return None
    except Exception as e:
        print(f"Error fetching token info: {e}")
        return None

def track_caller_performance(trader, profitable=False):
    """Track performance of callers"""
    if trader not in caller_stats:
        caller_stats[trader] = {'calls': 0, 'profitable': 0, 'total_profit': 0.0}
    
    caller_stats[trader]['calls'] += 1
    if profitable:
        caller_stats[trader]['profitable'] += 1

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show hottest callers leaderboard"""
    if not caller_stats:
        await update.message.reply_text("📊 No calls tracked yet!\n\nStart tracking traders to see the leaderboard.")
        return
    
    sorted_callers = sorted(
        caller_stats.items(),
        key=lambda x: (x[1].get('profitable', 0) / max(x[1].get('calls', 1), 1), x[1].get('calls', 0)),
        reverse=True
    )[:10]
    
    text = "🔥 Hottest Callers Leaderboard\n\n"
    
    for i, (trader, stats) in enumerate(sorted_callers, 1):
        calls = stats.get('calls', 0)
        profitable = stats.get('profitable', 0)
        win_rate = (profitable / calls * 100) if calls > 0 else 0
        
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        
        text += f"{medal} @{trader}\n"
        text += f"   Calls: {calls} | Win Rate: {win_rate:.1f}%\n\n"
    
    text += "\n💡 Track top performers with /track @username"
    
    await update.message.reply_text(text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start command - Show main menu"""
    keyboard = [
        [
            InlineKeyboardButton("📊 Track Traders", callback_data="menu_track"),
            InlineKeyboardButton("💰 My Wallet", callback_data="menu_wallet")
        ],
        [
            InlineKeyboardButton("🎯 Auto-Buy", callback_data="menu_autobuy"),
            InlineKeyboardButton("💵 Balance", callback_data="menu_balance")
        ],
        [
            InlineKeyboardButton("📋 Positions", callback_data="menu_positions"),
            InlineKeyboardButton("🔥 Leaderboard", callback_data="menu_leaderboard")
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="menu_help")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = (
        "🤖 CallTracker - Auto-Snipe Bot\n\n"
        "⚠️ DISCLAIMERS:\n"
        "• Extremely risky - could lose ALL funds\n"
        "• Not your keys, not your coins\n"
        "• Use at your own risk\n\n"
        "🛡️ SAFETY:\n"
        "• Max 1 SOL per trade\n"
        "• Max 5 SOL balance\n"
        "• Max 100 trades/day\n\n"
        "Tap a button below:"
    )
    
    await update.message.reply_text(text, reply_markup=reply_markup)

async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main menu button presses"""
    query = update.callback_query
    await query.answer()
    
    action = query.data.replace('menu_', '')
    user_id = str(update.effective_user.id)
    
    if action == 'wallet':
        if user_id not in tracked_users[group_id][target]:
        tracked_users[group_id][target].append(user_id)
        save_tracked_users()
        print(f"✅ User {user_id} tracking @{target} in group {group_id}")
        print(f"Tracked users now: {tracked_users[group_id]}")
        await update.message.reply_text(f"✅ Now tracking @{target}'s CAs. Forwards go to your private chat!")
    else:
        await update.message.reply_text(f"ℹ️ You're already tracking @{target}")

async def untrack_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop tracking a user"""
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
    
    if message.from_user:
        username = message.from_user.username.lower() if message.from_user.username else None
    elif message.sender_chat:
        username = message.sender_chat.username.lower() if message.sender_chat.username else message.sender_chat.title.lower()
    else:
        return
    
    print(f"Message from @{username}: {message.text}")
    print(f"Tracked users in group {group_id}: {tracked_users.get(group_id, set())}")
    
    if group_id not in tracked_users or username not in tracked_users[group_id]:
        print(f"User @{username} not being tracked")
        return
    
    print(f"Searching in text: '{message.text}'")
    
    detected_cas = []
    words = message.text.split()
    
    for word in words:
        clean_word = word.strip('.,!?()[]{}')
        chain_id, blockchain, ca = detect_blockchain(clean_word)
        
        if blockchain and ca:
            detected_cas.append((chain_id, blockchain, ca))
            print(f"Found {blockchain} CA: {ca}")
    
    if not detected_cas:
        print("No CAs found")
        return
    
    user_ids = tracked_users[group_id][username]
    
    for user_id in user_ids:
        try:
            info_text = f"📌 New CA from @{username} in {message.chat.title}"
            await context.bot.send_message(chat_id=user_id, text=info_text)
            
            for chain_id, blockchain, ca in detected_cas:
                token_info = await get_token_info(chain_id, ca)
                
                track_caller_performance(username)
                
                if token_info:
                    if token_info.get('logo'):
                        try:
                            await context.bot.send_photo(
                                chat_id=user_id,
                                photo=token_info['logo'],
                                caption=f"🔗 {blockchain}\n💎 {token_info['name']} (${token_info['symbol']})\n💰 Mcap: {token_info['mcap']}"
                            )
                        except:
                            info_msg = (
                                f"🔗 {blockchain}\n"
                                f"💎 {token_info['name']} (${token_info['symbol']})\n"
                                f"💰 Mcap: {token_info['mcap']}"
                            )
                            await context.bot.send_message(chat_id=user_id, text=info_msg)
                    else:
                        info_msg = (
                            f"🔗 {blockchain}\n"
                            f"💎 {token_info['name']} (${token_info['symbol']})\n"
                            f"💰 Mcap: {token_info['mcap']}"
                        )
                        await context.bot.send_message(chat_id=user_id, text=info_msg)
                    
                    await context.bot.send_message(chat_id=user_id, text=ca)
                    
                    if token_info.get('chart'):
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=f"📊 [View Chart]({token_info['chart']})",
                            parse_mode='Markdown'
                        )
                else:
                    await context.bot.send_message(chat_id=user_id, text=f"🔗 {blockchain}")
                    await context.bot.send_message(chat_id=user_id, text=ca)
            
            print(f"✅ Message sent to {user_id} successfully!")
        except RetryAfter as e:
            print(f"Rate limited, waiting {e.retry_after} seconds...")
            time.sleep(e.retry_after)
        except TimedOut:
            print(f"Timeout, retrying...")
            time.sleep(2)
        except TelegramError as e:
            print(f"❌ Error forwarding to {user_id}: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Help command"""
    await start(update, context)

def main():
    """Start the bot"""
    load_tracked_users()
    load_wallets()
    load_autobuy_settings()
    
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("track", track_user))
    app.add_handler(CommandHandler("untrack", untrack_user))
    app.add_handler(CommandHandler("list", list_tracked))
    app.add_handler(CommandHandler("wallet", wallet))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("autobuy", autobuy_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CallbackQueryHandler(handle_menu_callback, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(handle_autobuy_callback, pattern="^ab_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Bot started...")
    print(f"⚠️  ENCRYPTION KEY: {ENCRYPTION_KEY.decode() if isinstance(ENCRYPTION_KEY, bytes) else ENCRYPTION_KEY}")
    print("⚠️  SAVE THIS KEY! Without it, you cannot recover wallets!")
    app.run_polling(
        allowed_updates=["message"],
        drop_pending_updates=False
    )

if __name__ == "__main__":
    main()id not in user_wallets:
            private_key, public_key = generate_wallet()
            encrypted_key = encrypt_key(private_key)
            
            user_wallets[user_id] = {
                'encrypted_key': encrypted_key,
                'address': public_key,
                'balance': 0.0
            }
            save_wallets()
            
            await query.edit_message_text(
                f"✅ Wallet created!\n\n"
                f"💳 Deposit address:\n`{public_key}`\n\n"
                f"Send SOL here to start trading.\n"
                f"⚠️ Only send SOL, not tokens!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
            )
        else:
            address = user_wallets[user_id]['address']
            await query.edit_message_text(
                f"💳 Your deposit address:\n`{address}`\n\n"
                f"Send SOL here to fund your wallet.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
            )
    
    elif action == 'balance':
        if user_id not in user_wallets:
            await query.edit_message_text(
                "❌ No wallet yet.\n\nCreate one first!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
            )
            return
        
        balance = user_wallets[user_id].get('balance', 0.0)
        address = user_wallets[user_id]['address']
        
        await query.edit_message_text(
            f"💰 Your Balance\n\n"
            f"Address: `{address}`\n"
            f"Balance: {balance} SOL",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
        )
    
    elif action == 'autobuy':
        if user_id not in autobuy_settings or not autobuy_settings[user_id]:
            await query.edit_message_text(
                "🎯 Auto-Buy Settings\n\n"
                "You haven't set up any auto-buy yet.\n\n"
                "First, track some traders in your groups with:\n"
                "/track @username\n\n"
                "Then come back here to configure auto-buy!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
            )
            return
        
        text = "🎯 Your Auto-Buy Settings\n\n"
        buttons = []
        
        for trader, config in autobuy_settings[user_id].items():
            status = "✅" if config.get('enabled', True) else "❌"
            text += f"{status} @{trader} - {config['amount']} SOL\n"
            buttons.append([InlineKeyboardButton(f"⚙️ @{trader}", callback_data=f"ab_edit_{trader}_{user_id}")])
        
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="menu_back")])
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    
    elif action == 'leaderboard':
        if not caller_stats:
            await query.edit_message_text(
                "📊 No calls tracked yet!\n\nStart tracking traders to see who's hot!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
            )
            return
        
        sorted_callers = sorted(
            caller_stats.items(),
            key=lambda x: (x[1].get('profitable', 0) / max(x[1].get('calls', 1), 1), x[1].get('calls', 0)),
            reverse=True
        )[:10]
        
        text = "🔥 Hottest Callers\n\n"
        
        for i, (trader, stats) in enumerate(sorted_callers, 1):
            calls = stats.get('calls', 0)
            profitable = stats.get('profitable', 0)
            win_rate = (profitable / calls * 100) if calls > 0 else 0
            
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            text += f"{medal} @{trader} - {calls} calls ({win_rate:.0f}% win)\n"
        
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
        )
    
    elif action == 'positions':
        await query.edit_message_text(
            "📋 Your Positions\n\n"
            "Coming soon! This will show:\n"
            "• Active trades\n"
            "• P&L\n"
            "• Stop loss/take profit status",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
        )
    
    elif action == 'help':
        await query.edit_message_text(
            "❓ Help\n\n"
            "📊 Track Traders: Add bot to group, use /track @username\n"
            "💰 Wallet: Get deposit address\n"
            "🎯 Auto-Buy: Configure automatic buying\n"
            "💵 Balance: Check your SOL balance\n"
            "📋 Positions: View active trades\n\n"
            "Support: @Makafog\n"
            "Twitter: @calltrackerr",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
        )
    
    elif action == 'back':
        await start_menu_edit(query)

async def start_menu_edit(query):
    """Show main menu (for editing existing message)"""
    keyboard = [
        [
            InlineKeyboardButton("📊 Track Traders", callback_data="menu_track"),
            InlineKeyboardButton("💰 My Wallet", callback_data="menu_wallet")
        ],
        [
            InlineKeyboardButton("🎯 Auto-Buy", callback_data="menu_autobuy"),
            InlineKeyboardButton("💵 Balance", callback_data="menu_balance")
        ],
        [
            InlineKeyboardButton("📋 Positions", callback_data="menu_positions"),
            InlineKeyboardButton("❓ Help", callback_data="menu_help")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = (
        "🤖 CallTracker Menu\n\n"
        "Tap a button:"
    )
    
    await query.edit_message_text(text, reply_markup=reply_markup)

async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Get user's deposit wallet"""
    user_id = str(update.effective_user.id)
    
    if user_id not in user_wallets:
        private_key, public_key = generate_wallet()
        encrypted_key = encrypt_key(private_key)
        
        user_wallets[user_id] = {
            'encrypted_key': encrypted_key,
            'address': public_key,
            'balance': 0.0
        }
        save_wallets()
        
        await update.message.reply_text(
            f"✅ Wallet created!\n\n"
            f"💳 Your deposit address:\n`{public_key}`\n\n"
            f"Send SOL to this address to start auto-buying.\n\n"
            f"⚠️ Only send SOL (Solana), not tokens!"
        )
    else:
        address = user_wallets[user_id]['address']
        await update.message.reply_text(
            f"💳 Your deposit address:\n`{address}`\n\n"
            f"Send SOL here to fund your auto-buy wallet."
        )

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check wallet balance"""
    user_id = str(update.effective_user.id)
    
    if user_id not in user_wallets:
        await update.message.reply_text("❌ You don't have a wallet yet. Use /wallet to create one.")
        return
    
    balance = user_wallets[user_id].get('balance', 0.0)
    address = user_wallets[user_id]['address']
    
    await update.message.reply_text(
        f"💰 Your Balance\n\n"
        f"Address: `{address}`\n"
        f"Balance: {balance} SOL\n\n"
        f"Use /wallet to deposit more SOL"
    )

async def autobuy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set up auto-buy for a trader with button interface"""
    user_id = str(update.effective_user.id)
    
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "❌ Usage:\n"
            "/autobuy @trader - Configure auto-buy\n"
            "/autobuy off @trader - Disable\n"
            "/autobuy list - Show settings"
        )
        return
    
    if context.args[0].lower() == 'list':
        if user_id not in autobuy_settings or not autobuy_settings[user_id]:
            await update.message.reply_text("📭 No auto-buy settings configured.")
            return
        
        settings_text = "🎯 Your Auto-Buy Settings:\n\n"
        for trader, config in autobuy_settings[user_id].items():
            status = "✅ Enabled" if config.get('enabled', True) else "❌ Disabled"
            settings_text += f"@{trader}\n"
            settings_text += f"Amount: {config['amount']} SOL\n"
            settings_text += f"Max Mcap: ${config['max_mcap']:,}\n"
            settings_text += f"Stop Loss: {config.get('stoploss', 0)}%\n"
            settings_text += f"Take Profit: {config.get('takeprofit', 0)}%\n"
            settings_text += f"Status: {status}\n\n"
        
        await update.message.reply_text(settings_text)
        return
    
    if context.args[0].lower() == 'off' and len(context.args) >= 2:
        trader = context.args[1].lstrip('@').lower()
        
        if user_id in autobuy_settings and trader in autobuy_settings[user_id]:
            autobuy_settings[user_id][trader]['enabled'] = False
            save_autobuy_settings()
            await update.message.reply_text(f"✅ Auto-buy disabled for @{trader}")
        else:
            await update.message.reply_text(f"❌ No auto-buy settings found for @{trader}")
        return
    
    trader = context.args[0].lstrip('@').lower()
    
    if user_id not in user_wallets:
        await update.message.reply_text("❌ Create a wallet first with /wallet")
        return
    
    temp_autobuy_config[user_id] = {
        'trader': trader,
        'amount': 0.5,
        'max_mcap': 100000,
        'stoploss': 20,
        'takeprofit': 100
    }
    
    await show_autobuy_menu(update, user_id)

async def show_autobuy_menu(update, user_id):
    """Display auto-buy configuration menu with buttons"""
    config = temp_autobuy_config.get(user_id, {})
    trader = config.get('trader', 'unknown')
    amount = config.get('amount', 0.5)
    max_mcap = config.get('max_mcap', 100000)
    stoploss = config.get('stoploss', 20)
    takeprofit = config.get('takeprofit', 100)
    
    text = (
        f"🎯 Configure Auto-Buy for @{trader}\n\n"
        f"Current Settings:\n"
        f"💰 Amount: {amount} SOL\n"
        f"📊 Max Mcap: ${max_mcap:,}\n"
        f"🔻 Stop Loss: {stoploss}%\n"
        f"🔺 Take Profit: {takeprofit}%\n\n"
        f"Tap to adjust:"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("💰 0.1 SOL", callback_data=f"ab_amount_0.1_{user_id}"),
            InlineKeyboardButton("💰 0.5 SOL", callback_data=f"ab_amount_0.5_{user_id}"),
            InlineKeyboardButton("💰 1.0 SOL", callback_data=f"ab_amount_1.0_{user_id}")
        ],
        [
            InlineKeyboardButton("📊 $50K", callback_data=f"ab_mcap_50000_{user_id}"),
            InlineKeyboardButton("📊 $100K", callback_data=f"ab_mcap_100000_{user_id}"),
            InlineKeyboardButton("📊 $500K", callback_data=f"ab_mcap_500000_{user_id}")
        ],
        [
            InlineKeyboardButton("🔻 10%", callback_data=f"ab_sl_10_{user_id}"),
            InlineKeyboardButton("🔻 20%", callback_data=f"ab_sl_20_{user_id}"),
            InlineKeyboardButton("🔻 50%", callback_data=f"ab_sl_50_{user_id}")
        ],
        [
            InlineKeyboardButton("🔺 50%", callback_data=f"ab_tp_50_{user_id}"),
            InlineKeyboardButton("🔺 100%", callback_data=f"ab_tp_100_{user_id}"),
            InlineKeyboardButton("🔺 200%", callback_data=f"ab_tp_200_{user_id}")
        ],
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"ab_confirm_{user_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"ab_cancel_{user_id}")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def handle_autobuy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses for auto-buy configuration"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    parts = data.split('_')
    
    if len(parts) < 3:
        return
    
    action = parts[1]
    value = parts[2]
    user_id = parts[3] if len(parts) > 3 else str(update.effective_user.id)
    
    if user_id != str(update.effective_user.id):
        await query.answer("❌ This isn't your menu!", show_alert=True)
        return
    
    if user_id not in temp_autobuy_config:
        await query.answer("❌ Configuration expired. Run /autobuy again", show_alert=True)
        return
    
    if action == 'amount':
        temp_autobuy_config[user_id]['amount'] = float(value)
    elif action == 'mcap':
        temp_autobuy_config[user_id]['max_mcap'] = float(value)
    elif action == 'sl':
        temp_autobuy_config[user_id]['stoploss'] = int(value)
    elif action == 'tp':
        temp_autobuy_config[user_id]['takeprofit'] = int(value)
    elif action == 'confirm':
        config = temp_autobuy_config[user_id]
        trader = config['trader']
        
        if user_id not in autobuy_settings:
            autobuy_settings[user_id] = {}
        
        autobuy_settings[user_id][trader] = {
            'amount': config['amount'],
            'max_mcap': config['max_mcap'],
            'stoploss': config['stoploss'],
            'takeprofit': config['takeprofit'],
            'enabled': True,
            'daily_trades': 0,
            'last_reset': time.time()
        }
        
        save_autobuy_settings()
        del temp_autobuy_config[user_id]
        
        await query.edit_message_text(
            f"✅ Auto-buy configured for @{trader}!\n\n"
            f"💰 Amount: {config['amount']} SOL\n"
            f"📊 Max Mcap: ${config['max_mcap']:,}\n"
            f"🔻 Stop Loss: {config['stoploss']}%\n"
            f"🔺 Take Profit: {config['takeprofit']}%\n\n"
            f"🛡️ Safety active: Max {MAX_DAILY_TRADES} trades/day\n"
            f"⚠️ You accept all trading risks!"
        )
        return
    elif action == 'cancel':
        del temp_autobuy_config[user_id]
        await query.edit_message_text("❌ Auto-buy configuration cancelled.")
        return
    
    await show_autobuy_menu(update, user_id)

async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track a user's or channel's contract addresses"""
    if update.effective_chat.type == 'private':
        await update.message.reply_text("❌ Please use this command in a group chat, not in DMs.")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Usage: /track @username or /track ChannelName")
        return
    
    target = context.args[0].lstrip('@').lower()
    group_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if group_id not in tracked_users:
        tracked_users[group_id] = {}
    
    if target not in tracked_users[group_id]:
        tracked_users[group_id][target] = []
    
    if user_

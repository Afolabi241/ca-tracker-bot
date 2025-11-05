import re
import json
import time
import aiohttp
import os
import base58
import asyncio
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.system_program import transfer, TransferParams
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from cryptography.fernet import Fernet
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.error import TelegramError, RetryAfter, TimedOut

# Configuration
TOKEN = "8224822340:AAHBwPhk4i9K7jLVz_V-6z7zIVjGYhAdkeY"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
JUPITER_API = "https://quote-api.jup.ag/v6"

# Encryption
ENCRYPTION_KEY = os.getenv('WALLET_ENCRYPTION_KEY', Fernet.generate_key())
if isinstance(ENCRYPTION_KEY, str):
    ENCRYPTION_KEY = ENCRYPTION_KEY.encode()
cipher = Fernet(ENCRYPTION_KEY)

# Files
STORAGE_FILE = 'tracked_users.json'
WALLETS_FILE = 'user_wallets.json'
AUTOBUY_FILE = 'autobuy_settings.json'
POSITIONS_FILE = 'user_positions.json'

# Safety
MAX_AUTOBUY_AMOUNT = 1.0
MAX_WALLET_BALANCE = 5.0
MAX_DAILY_TRADES = 100
MAX_SLIPPAGE = 10

# Storage
tracked_users = {}
user_wallets = {}
autobuy_settings = {}
user_positions = {}
temp_autobuy_config = {}
caller_stats = {}

# Patterns
CA_PATTERNS = {
    'Ethereum/BSC/Base/Polygon': r'0x[a-fA-F0-9]{40}',
    'Solana': r'[1-9A-HJ-NP-Za-km-z]{32,44}',
    'Tron': r'T[1-9A-HJ-NP-Za-km-z]{33}',
    'Sui': r'0x[a-fA-F0-9]{64}',
}

solana_client = None

async def init_solana_client():
    """Initialize Solana"""
    global solana_client
    solana_client = AsyncClient(SOLANA_RPC)
    print(f"✅ Solana connected")

def generate_solana_wallet():
    """Generate wallet"""
    keypair = Keypair()
    private_key = base58.b58encode(bytes(keypair)).decode()
    public_key = str(keypair.pubkey())
    return private_key, public_key

def encrypt_key(key):
    """Encrypt"""
    return cipher.encrypt(key.encode()).decode()

def decrypt_key(key):
    """Decrypt"""
    return cipher.decrypt(key.encode()).decode()

async def get_sol_balance(address):
    """Get balance"""
    try:
        pubkey = Pubkey.from_string(address)
        response = await solana_client.get_balance(pubkey, commitment=Confirmed)
        return response.value / 1_000_000_000
    except:
        return 0.0

async def execute_jupiter_swap(user_id, token_address, amount_sol, slippage=10):
    """Execute swap via Jupiter"""
    try:
        # Get user wallet
        if str(user_id) not in user_wallets:
            return None, "No wallet"
        
        wallet_data = user_wallets[str(user_id)]
        private_key = decrypt_key(wallet_data['encrypted_key'])
        
        # Create keypair from private key
        keypair = Keypair.from_bytes(base58.b58decode(private_key))
        
        # Get quote from Jupiter
        amount_lamports = int(amount_sol * 1_000_000_000)
        
        quote_url = f"{JUPITER_API}/quote"
        params = {
            'inputMint': 'So11111111111111111111111111111111111111112',  # SOL
            'outputMint': token_address,
            'amount': amount_lamports,
            'slippageBps': slippage * 100  # Convert to basis points
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(quote_url, params=params) as response:
                if response.status != 200:
                    return None, "Quote failed"
                quote = await response.json()
        
        # Get swap transaction
        swap_url = f"{JUPITER_API}/swap"
        swap_body = {
            'quoteResponse': quote,
            'userPublicKey': str(keypair.pubkey()),
            'wrapAndUnwrapSol': True
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(swap_url, json=swap_body) as response:
                if response.status != 200:
                    return None, "Swap failed"
                swap_data = await response.json()
        
        # Sign and send transaction
        swap_tx = swap_data['swapTransaction']
        tx = Transaction.from_bytes(base58.b58decode(swap_tx))
        
        # Sign transaction
        tx.sign(keypair)
        
        # Send transaction
        result = await solana_client.send_transaction(
            tx,
            keypair,
            opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        )
        
        tx_signature = str(result.value)
        
        # Store position
        position = {
            'token_address': token_address,
            'entry_price': float(quote.get('outAmount', 0)) / amount_lamports,
            'amount_sol': amount_sol,
            'timestamp': time.time(),
            'tx_signature': tx_signature
        }
        
        if str(user_id) not in user_positions:
            user_positions[str(user_id)] = []
        user_positions[str(user_id)].append(position)
        
        save_positions()
        
        return tx_signature, None
        
    except Exception as e:
        print(f"Swap error: {e}")
        return None, str(e)

def save_positions():
    """Save positions"""
    try:
        with open(POSITIONS_FILE, 'w') as f:
            json.dump(user_positions, f)
    except Exception as e:
        print(f"Error saving positions: {e}")

def load_positions():
    """Load positions"""
    global user_positions
    try:
        with open(POSITIONS_FILE, 'r') as f:
            user_positions = json.load(f)
        print(f"✅ Loaded positions")
    except FileNotFoundError:
        user_positions = {}

def load_wallets():
    """Load wallets"""
    global user_wallets
    try:
        with open(WALLETS_FILE, 'r') as f:
            user_wallets = json.load(f)
        print(f"✅ Loaded {len(user_wallets)} wallets")
    except FileNotFoundError:
        user_wallets = {}

def save_wallets():
    """Save wallets"""
    try:
        with open(WALLETS_FILE, 'w') as f:
            json.dump(user_wallets, f)
    except Exception as e:
        print(f"Error saving wallets: {e}")

def load_autobuy_settings():
    """Load autobuy"""
    global autobuy_settings
    try:
        with open(AUTOBUY_FILE, 'r') as f:
            autobuy_settings = json.load(f)
        print(f"✅ Loaded autobuy")
    except FileNotFoundError:
        autobuy_settings = {}

def save_autobuy_settings():
    """Save autobuy"""
    try:
        with open(AUTOBUY_FILE, 'w') as f:
            json.dump(autobuy_settings, f)
    except Exception as e:
        print(f"Error saving autobuy: {e}")

def load_tracked_users():
    """Load tracked"""
    global tracked_users
    try:
        with open(STORAGE_FILE, 'r') as f:
            data = json.load(f)
            tracked_users = {int(k): v for k, v in data.items()}
        print(f"✅ Loaded {len(tracked_users)} groups")
    except FileNotFoundError:
        tracked_users = {}

def save_tracked_users():
    """Save tracked"""
    try:
        with open(STORAGE_FILE, 'w') as f:
            json.dump(tracked_users, f)
    except Exception as e:
        print(f"Error saving: {e}")

def detect_blockchain(address):
    """Detect chain"""
    if re.match(CA_PATTERNS['Tron'], address):
        return 'tron', 'Tron', address
    if re.match(CA_PATTERNS['Sui'], address):
        return 'sui', 'Sui', address
    if re.match(CA_PATTERNS['Ethereum/BSC/Base/Polygon'], address):
        return 'ethereum', 'EVM', address
    if re.match(CA_PATTERNS['Solana'], address):
        if len(address) >= 32:
            return 'solana', 'Solana', address
    return None, None, None

async def get_token_info(chain_id, address):
    """Get token info"""
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
                        
                        return {
                            'name': name,
                            'symbol': symbol,
                            'mcap': mcap_str,
                            'mcap_raw': mcap,
                            'logo': pair.get('info', {}).get('imageUrl', ''),
                            'chart': f"https://dexscreener.com/{chain_id}/{pair.get('pairAddress', '')}"
                        }
        return None
    except Exception as e:
        print(f"Token info error: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start"""
    keyboard = [
        [
            InlineKeyboardButton("💰 Wallet", callback_data="menu_wallet"),
            InlineKeyboardButton("💵 Balance", callback_data="menu_balance")
        ],
        [
            InlineKeyboardButton("🎯 Auto-Buy", callback_data="menu_autobuy"),
            InlineKeyboardButton("📋 Positions", callback_data="menu_positions")
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="menu_help")
        ]
    ]
    
    text = (
        "🤖 CallTracker Auto-Snipe\n\n"
        "⚠️ EXTREME RISK:\n"
        "• Can lose ALL funds\n"
        "• Not your keys, not your coins\n"
        "• Use at own risk\n\n"
        "🛡️ Limits:\n"
        "• Max 1 SOL/trade\n"
        "• Max 100 trades/day\n\n"
        "You accept all risks!"
    )
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu handler"""
    query = update.callback_query
    await query.answer()
    
    action = query.data.replace('menu_', '')
    user_id = str(update.effective_user.id)
    
    if action == 'wallet':
        if user_id not in user_wallets:
            priv, pub = generate_solana_wallet()
            enc = encrypt_key(priv)
            user_wallets[user_id] = {
                'encrypted_key': enc,
                'address': pub,
                'balance': 0.0
            }
            save_wallets()
            await query.edit_message_text(
                f"✅ Wallet Created!\n\n💳 `{pub}`\n\nSend SOL here!",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
            )
        else:
            addr = user_wallets[user_id]['address']
            await query.edit_message_text(
                f"💳 Your Wallet\n\n`{addr}`",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
            )
    
    elif action == 'balance':
        if user_id not in user_wallets:
            await query.edit_message_text(
                "❌ No wallet!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
            )
            return
        
        addr = user_wallets[user_id]['address']
        bal = await get_sol_balance(addr)
        user_wallets[user_id]['balance'] = bal
        save_wallets()
        
        await query.edit_message_text(
            f"💰 Balance: {bal:.4f} SOL\n\n`{addr}`",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
        )
    
    elif action == 'positions':
        if user_id not in user_positions or not user_positions[user_id]:
            await query.edit_message_text(
                "📋 No active positions",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
            )
            return
        
        text = "📋 Your Positions:\n\n"
        for i, pos in enumerate(user_positions[user_id][-5:], 1):
            text += f"{i}. {pos.get('amount_sol', 0)} SOL\n"
            text += f"   Token: {pos.get('token_address', '')[:8]}...\n\n"
        
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
        )
    
    elif action == 'autobuy':
        text = "🎯 Auto-Buy\n\nUse /autobuy @trader in groups to configure!"
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
        )
    
    elif action == 'help':
        text = (
            "📖 Commands:\n\n"
            "In Groups:\n"
            "/track @user - Track CAs\n"
            "/autobuy @user - Configure\n\n"
            "Anywhere:\n"
            "/wallet - Your wallet\n"
            "/balance - Check balance\n\n"
            "Support: @Makafog"
        )
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
        )
    
    elif action == 'back':
        await start_from_callback(query)

async def start_from_callback(query):
    """Start menu for callbacks"""
    keyboard = [
        [
            InlineKeyboardButton("💰 Wallet", callback_data="menu_wallet"),
            InlineKeyboardButton("💵 Balance", callback_data="menu_balance")
        ],
        [
            InlineKeyboardButton("🎯 Auto-Buy", callback_data="menu_autobuy"),
            InlineKeyboardButton("📋 Positions", callback_data="menu_positions")
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="menu_help")
        ]
    ]
    
    await query.edit_message_text(
        "🤖 CallTracker Menu",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def autobuy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Autobuy setup"""
    user_id = str(update.effective_user.id)
    
    if not context.args:
        await update.message.reply_text(
            "Usage:\n/autobuy @trader\n/autobuy list\n/autobuy off @trader"
        )
        return
    
    if context.args[0].lower() == 'list':
        if user_id not in autobuy_settings or not autobuy_settings[user_id]:
            await update.message.reply_text("No autobuy configured")
            return
        
        text = "🎯 Your Auto-Buys:\n\n"
        for trader, cfg in autobuy_settings[user_id].items():
            status = "✅" if cfg.get('enabled') else "❌"
            text += f"{status} @{trader}: {cfg['amount']} SOL\n"
        
        await update.message.reply_text(text)
        return
    
    if context.args[0].lower() == 'off':
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /autobuy off @trader")
            return
        trader = context.args[1].lstrip('@').lower()
        if user_id in autobuy_settings and trader in autobuy_settings[user_id]:
            autobuy_settings[user_id][trader]['enabled'] = False
            save_autobuy_settings()
            await update.message.reply_text(f"✅ Disabled for @{trader}")
        return
    
    trader = context.args[0].lstrip('@').lower()
    
    if user_id not in user_wallets:
        await update.message.reply_text("❌ Create wallet first: /wallet")
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
    """Show autobuy config"""
    cfg = temp_autobuy_config.get(user_id, {})
    trader = cfg.get('trader', '?')
    amount = cfg.get('amount', 0.5)
    mcap = cfg.get('max_mcap', 100000)
    sl = cfg.get('stoploss', 20)
    tp = cfg.get('takeprofit', 100)
    
    text = (
        f"🎯 Auto-Buy: @{trader}\n\n"
        f"💰 Amount: {amount} SOL\n"
        f"📊 Max Mcap: ${mcap:,}\n"
        f"🔻 Stop Loss: {sl}%\n"
        f"🔺 Take Profit: {tp}%"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("💰 0.1", callback_data=f"ab_amt_0.1_{user_id}"),
            InlineKeyboardButton("💰 0.5", callback_data=f"ab_amt_0.5_{user_id}"),
            InlineKeyboardButton("💰 1.0", callback_data=f"ab_amt_1.0_{user_id}")
        ],
        [
            InlineKeyboardButton("📊 50K", callback_data=f"ab_mc_50000_{user_id}"),
            InlineKeyboardButton("📊 100K", callback_data=f"ab_mc_100000_{user_id}"),
            InlineKeyboardButton("📊 500K", callback_data=f"ab_mc_500000_{user_id}")
        ],
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"ab_confirm_{user_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"ab_cancel_{user_id}")
        ]
    ]
    
    markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)

async def handle_autobuy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle autobuy buttons"""
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split('_')
    if len(parts) < 3:
        return
    
    action = parts[1]
    value = parts[2]
    user_id = parts[3] if len(parts) > 3 else str(update.effective_user.id)
    
    if user_id != str(update.effective_user.id):
        await query.answer("Not your menu!", show_alert=True)
        return
    
    if user_id not in temp_autobuy_config:
        await query.answer("Expired, run /autobuy again", show_alert=True)
        return
    
    if action == 'amt':
        temp_autobuy_config[user_id]['amount'] = float(value)
    elif action == 'mc':
        temp_autobuy_config[user_id]['max_mcap'] = float(value)
    elif action == 'confirm':
        cfg = temp_autobuy_config[user_id]
        trader = cfg['trader']
        
        if user_id not in autobuy_settings:
            autobuy_settings[user_id] = {}
        
        autobuy_settings[user_id][trader] = {
            'amount': cfg['amount'],
            'max_mcap': cfg['max_mcap'],
            'stoploss': cfg.get('stoploss', 20),
            'takeprofit': cfg.get('takeprofit', 100),
            'enabled': True,
            'daily_trades': 0
        }
        
        save_autobuy_settings()
        del temp_autobuy_config[user_id]
        
        await query.edit_message_text(
            f"✅ Auto-buy set for @{trader}!\n\n"
            f"💰 {cfg['amount']} SOL\n"
            f"📊 Max ${cfg['max_mcap']:,}\n\n"
            f"Bot will auto-buy when @{trader} shares CAs!"
        )
        return
    elif action == 'cancel':
        del temp_autobuy_config[user_id]
        await query.edit_message_text("❌ Cancelled")
        return
    
    await show_autobuy_menu(update, user_id)

async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track"""
    if update.effective_chat.type == 'private':
        await update.message.reply_text("❌ Use in group only")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Usage: /track @username")
        return
    
    target = context.args[0].lstrip('@').lower()
    group_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if group_id not in tracked_users:
        tracked_users[group_id] = {}
    if target not in tracked_users[group_id]:
        tracked_users[group_id][target] = []
    
    if user_id not in tracked_users[group_id][target]:
        tracked_users[group_id][target].append(user_id)
        save_tracked_users()
        await update.message.reply_text(f"✅ Tracking @{target}")
    else:
        await update.message.reply_text(f"ℹ️ Already tracking")

async def untrack_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Untrack"""
    if update.effective_chat.type == 'private':
        await update.message.reply_text("❌ Group only")
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
            await update.message.reply_text(f"✅ Stopped tracking")
        else:
            await update.message.reply_text(f"❌ Not tracking")
    else:
        await update.message.reply_text(f"❌ Not tracked")

async def list_tracked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List"""
    if update.effective_chat.type == 'private':
        await update.message.reply_text("❌ Group only")
        return
    
    group_id = update.effective_chat.id
    
    if group_id not in tracked_users or not tracked_users[group_id]:
        await update.message.reply_text("📭 None tracked")
        return
    
    lst = "\n".join(f"• @{u}" for u in tracked_users[group_id])
    await update.message.reply_text(f"📋 Tracked:\n{lst}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle msgs"""
    msg = update.message
    if not msg or not msg.text:
        return
    
    group_id = msg.chat.id
    
    if msg.from_user:
        username = msg.from_user.username.lower() if msg.from_user.username else None
    elif msg.sender_chat:
        username = msg.sender_chat.username.lower() if msg.sender_chat.username else None
    else:
        return
    
    if group_id not in tracked_users or username not in tracked_users[group_id]:
        return
    
    detected = []
    for word in msg.text.split():
        clean = word.strip('.,!?()[]{}')
        chain_id, blockchain, ca = detect_blockchain(clean)
        if blockchain and ca:
            detected.append((chain_id, blockchain, ca))
    
    if not detected:
        return
    
    user_ids = tracked_users[group_id][username]
    
    for uid in user_ids:
        try:
            await context.bot.send_message(uid, f"📌 CA from @{username}")
            
            for chain_id, blockchain, ca in detected:
                token_info = await get_token_info(chain_id, ca)
                
                if token_info and token_info.get('logo'):
                    try:
                        await context.bot.send_photo(
                            uid,
                            photo=token_info['logo'],
                            caption=f"🔗 {blockchain}\n💎 {token_info['name']}\n💰 {token_info['mcap']}"
                        )
                    except:
                        await context.bot.send_message(
                            uid,
                            text=f"🔗 {blockchain}\n💎 {token_info['name']}\n💰 {token_info['mcap']}"
                        )
                elif token_info:
                    await context.bot.send_message(
                        uid,
                        text=f"🔗 {blockchain}\n💎 {token_info['name']}\n💰 {token_info['mcap']}"
                    )
                
                await context.bot.send_message(uid, ca)
                
                if token_info and token_info.get('chart'):
                    await context.bot.send_message(
                        uid,
                        text=f"📊 [Chart]({token_info['chart']})",
                        parse_mode='Markdown'
                    )
                
                # AUTO-BUY EXECUTION
                if chain_id == 'solana':
                    await check_and_execute_autobuy(uid, username, ca, token_info, context)
            
        except TelegramError as e:
            print(f"Error: {e}")

async def check_and_execute_autobuy(uid, trader, ca, token_info, context):
    """Check and execute autobuy"""
    user_id = str(uid)
    
    if user_id not in autobuy_settings:
        return
    if trader not in autobuy_settings[user_id]:
        return
    
    cfg = autobuy_settings[user_id][trader]
    
    if not cfg.get('enabled'):
        return
    
    # Check mcap limit
    if token_info and token_info.get('mcap_raw', 0) > cfg.get('max_mcap', 100000):
        await context.bot.send_message(uid, "⚠️ Mcap too high, skipping")
        return
    
    # Check daily limit
    if cfg.get('daily_trades', 0) >= MAX_DAILY_TRADES:
        await context.bot.send_message(uid, "⚠️ Daily limit reached!")
        return
    
    # Check balance
    if user_id not in user_wallets:
        return
    
    balance = user_wallets[user_id].get('balance', 0)
    amount = cfg.get('amount', 0.5)
    
    if balance < amount:
        await context.bot.send_message(
            uid,
            f"❌ Low balance!\nNeed: {amount} SOL\nHave: {balance:.4f} SOL"
        )
        return
    
    # EXECUTE SWAP
    await context.bot.send_message(uid, f"🎯 AUTO-BUYING {amount} SOL...")
    
    tx_sig, error = await execute_jupiter_swap(user_id, ca, amount)
    
    if tx_sig:
        await context.bot.send_message(
            uid,
            f"✅ BOUGHT!\n\n"
            f"Amount: {amount} SOL\n"
            f"TX: `{tx_sig[:16]}...`\n\n"
            f"[View on Solscan](https://solscan.io/tx/{tx_sig})",
            parse_mode='Markdown'
        )
        
        # Update trade count
        cfg['daily_trades'] = cfg.get('daily_trades', 0) + 1
        save_autobuy_settings()
        
        # Update balance
        new_balance = await get_sol_balance(user_wallets[user_id]['address'])
        user_wallets[user_id]['balance'] = new_balance
        save_wallets()
    else:
        await context.bot.send_message(
            uid,
            f"❌ SWAP FAILED!\n\nError: {error}\n\nCheck your balance and try again."
        )

async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wallet cmd"""
    user_id = str(update.effective_user.id)
    
    if user_id not in user_wallets:
        priv, pub = generate_solana_wallet()
        enc = encrypt_key(priv)
        user_wallets[user_id] = {
            'encrypted_key': enc,
            'address': pub,
            'balance': 0.0
        }
        save_wallets()
        await update.message.reply_text(
            f"✅ Wallet Created!\n\n💳 `{pub}`\n\nSend SOL here!",
            parse_mode='Markdown'
        )
    else:
        addr = user_wallets[user_id]['address']
        bal = await get_sol_balance(addr)
        user_wallets[user_id]['balance'] = bal
        save_wallets()
        await update.message.reply_text(
            f"💳 Wallet\n\n`{addr}`\n\nBalance: {bal:.4f} SOL",
            parse_mode='Markdown'
        )

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Balance cmd"""
    user_id = str(update.effective_user.id)
    
    if user_id not in user_wallets:
        await update.message.reply_text("❌ No wallet. Use /wallet")
        return
    
    addr = user_wallets[user_id]['address']
    bal = await get_sol_balance(addr)
    user_wallets[user_id]['balance'] = bal
    save_wallets()
    
    await update.message.reply_text(f"💰 Balance: {bal:.4f} SOL\n\n`{addr}`", parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help"""
    await start(update, context)

def main():
    """Main"""
    print("🚀 CallTracker Auto-Snipe FULL VERSION")
    
    load_tracked_users()
    load_wallets()
    load_autobuy_settings()
    load_positions()
    
    app = Application.builder().token(TOKEN).build()
    
    # Init Solana
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_solana_client())
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("track", track_user))
    app.add_handler(CommandHandler("untrack", untrack_user))
    app.add_handler(CommandHandler("list", list_tracked))
    app.add_handler(CommandHandler("wallet", wallet_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("autobuy", autobuy_command))
    app.add_handler(CallbackQueryHandler(handle_menu_callback, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(handle_autobuy_callback, pattern="^ab_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print(f"⚠️ ENCRYPTION KEY: {ENCRYPTION_KEY.decode()}")
    print("⚠️ SAVE THIS KEY SECURELY!")
    print("✅ FULL AUTO-SNIPE BOT READY!")
    print("✅ Real Solana wallets")
    print("✅ Jupiter swaps")
    print("✅ Auto-execution")
    print("✅ Position tracking")
    
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False
    )

if __name__ == "__main__":
    main()

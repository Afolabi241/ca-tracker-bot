import re
import json
import time
import aiohttp
import os
import base58
import base64
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

# Fee Collection (YOUR WALLET)
FEE_WALLET = "BXTasZ9F4TVvgvcscE42d7RwLDZ127i4gPwUuGMB8wjS"  # Your fee collection wallet
FEE_PERCENTAGE = 0.5  # 0.5% fee per trade (adjustable: 0.5 - 2.0)

# Encryption - USE YOUR SAVED KEY
ENCRYPTION_KEY = "ylGgy0aCCXZSFkHPbid6QV3Gfmw0zWK0HR4DbnkJoKk="
cipher = Fernet(ENCRYPTION_KEY.encode())

# Files
STORAGE_FILE = 'tracked_users.json'
WALLETS_FILE = 'user_wallets.json'
AUTOBUY_FILE = 'autobuy_settings.json'
POSITIONS_FILE = 'user_positions.json'
FEE_STATS_FILE = 'fee_statistics.json'

# Safety
MAX_AUTOBUY_AMOUNT = 1.0
MAX_WALLET_BALANCE = 5.0
MAX_DAILY_TRADES = 100
MAX_SLIPPAGE = 50
MIN_SLIPPAGE = 1

# Storage
tracked_users = {}
user_wallets = {}
autobuy_settings = {}
user_positions = {}
temp_autobuy_config = {}
caller_stats = {}
withdraw_requests = {}
fee_stats = {
    'total_collected': 0.0,
    'total_trades': 0,
    'last_collection': None
}

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
        tx = Transaction.from_bytes(base64.b64decode(swap_tx))
        
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

async def collect_fee(user_id, trade_amount_sol):
    """Collect fee and send to fee wallet"""
    try:
        if not FEE_WALLET or FEE_WALLET == "YOUR_SOLANA_WALLET_ADDRESS_HERE":
            print("⚠️ Fee wallet not configured, skipping fee collection")
            return None, "Fee wallet not set"
        
        if FEE_PERCENTAGE <= 0:
            return None, "Fee disabled"
        
        # Calculate fee
        fee_amount = trade_amount_sol * (FEE_PERCENTAGE / 100)
        
        # Minimum fee check (don't collect if less than 0.0001 SOL)
        if fee_amount < 0.0001:
            return None, "Fee too small"
        
        # Get user wallet
        if str(user_id) not in user_wallets:
            return None, "No wallet"
        
        wallet_data = user_wallets[str(user_id)]
        private_key = decrypt_key(wallet_data['encrypted_key'])
        keypair = Keypair.from_bytes(base58.b58decode(private_key))
        
        # Check balance
        balance = await get_sol_balance(str(keypair.pubkey()))
        if balance < fee_amount + 0.000005:  # Need fee + gas
            return None, "Insufficient balance for fee"
        
        # Create transfer
        fee_lamports = int(fee_amount * 1_000_000_000)
        fee_pubkey = Pubkey.from_string(FEE_WALLET)
        
        transfer_ix = transfer(
            TransferParams(
                from_pubkey=keypair.pubkey(),
                to_pubkey=fee_pubkey,
                lamports=fee_lamports
            )
        )
        
        # Create and sign transaction
        recent_blockhash = await solana_client.get_latest_blockhash()
        tx = Transaction(recent_blockhash=recent_blockhash.value.blockhash)
        tx.add(transfer_ix)
        tx.sign(keypair)
        
        # Send transaction
        result = await solana_client.send_transaction(
            tx,
            keypair,
            opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        )
        
        tx_sig = str(result.value)
        
        # Update fee stats
        fee_stats['total_collected'] += fee_amount
        fee_stats['total_trades'] += 1
        fee_stats['last_collection'] = time.time()
        save_fee_stats()
        
        print(f"✅ Fee collected: {fee_amount:.6f} SOL from user {user_id}")
        
        return tx_sig, None
        
    except Exception as e:
        print(f"❌ Fee collection error: {e}")
        return None, str(e)

async def execute_withdrawal(user_id, amount_sol, destination_address):
    """Execute SOL withdrawal"""
    try:
        if str(user_id) not in user_wallets:
            return None, "No wallet found"
        
        wallet_data = user_wallets[str(user_id)]
        private_key = decrypt_key(wallet_data['encrypted_key'])
        keypair = Keypair.from_bytes(base58.b58decode(private_key))
        
        # Check balance
        balance = await get_sol_balance(str(keypair.pubkey()))
        
        # Reserve 0.001 SOL for fees
        if balance < amount_sol + 0.001:
            return None, f"Insufficient balance. Have {balance:.4f} SOL"
        
        # Create transfer instruction
        amount_lamports = int(amount_sol * 1_000_000_000)
        destination_pubkey = Pubkey.from_string(destination_address)
        
        transfer_ix = transfer(
            TransferParams(
                from_pubkey=keypair.pubkey(),
                to_pubkey=destination_pubkey,
                lamports=amount_lamports
            )
        )
        
        # Create and sign transaction
        recent_blockhash = await solana_client.get_latest_blockhash()
        tx = Transaction(recent_blockhash=recent_blockhash.value.blockhash)
        tx.add(transfer_ix)
        tx.sign(keypair)
        
        # Send transaction
        result = await solana_client.send_transaction(
            tx,
            keypair,
            opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        )
        
        return str(result.value), None
        
    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        print(f"❌ WITHDRAWAL ERROR DETAILS:")
        print(f"Error type: {type(e).__name__}")
        print(f"Error message: {str(e)}")
        print(f"Full traceback:\n{error_detail}")
        return None, f"{type(e).__name__}: {str(e)}"

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

def save_fee_stats():
    """Save fee statistics"""
    try:
        with open(FEE_STATS_FILE, 'w') as f:
            json.dump(fee_stats, f, indent=2)
    except Exception as e:
        print(f"Error saving fee stats: {e}")

def load_fee_stats():
    """Load fee statistics"""
    global fee_stats
    try:
        with open(FEE_STATS_FILE, 'r') as f:
            fee_stats = json.load(f)
        print(f"✅ Loaded fee stats: {fee_stats['total_collected']:.6f} SOL from {fee_stats['total_trades']} trades")
    except FileNotFoundError:
        fee_stats = {
            'total_collected': 0.0,
            'total_trades': 0,
            'last_collection': None
        }

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
    """Start with banner"""
    keyboard = [
        [
            InlineKeyboardButton("💰 Wallet", callback_data="menu_wallet"),
            InlineKeyboardButton("💵 Balance", callback_data="menu_balance")
        ],
        [
            InlineKeyboardButton("🎯 Auto-Snipe", callback_data="menu_autobuy"),
            InlineKeyboardButton("📋 Positions", callback_data="menu_positions")
        ],
        [
            InlineKeyboardButton("💸 Withdraw", callback_data="menu_withdraw"),
            InlineKeyboardButton("❓ Help", callback_data="menu_help")
        ]
    ]
    
    text = (
        "🤖 **CallTracker Auto-Snipe**\n\n"
        "⚡ Lightning-fast execution\n"
        "🎯 Track unlimited callers\n"
        "💰 Secure wallet system\n\n"
        "⚠️ **Risk Warning:**\n"
        "Trading crypto is high risk. Only invest what you can afford to lose.\n\n"
        "Select an option below:"
    )
    
    # Try to send with banner image
    try:
        await update.message.reply_photo(
            photo="https://i.imgur.com/BzdzJON.png",
            caption=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    except:
        # Fallback to text if image fails
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

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
            
            keyboard = [
                [InlineKeyboardButton("🔄 Refresh Balance", callback_data="menu_balance")],
                [InlineKeyboardButton("🔙 Back", callback_data="menu_back")]
            ]
            
            await query.edit_message_text(
                f"✅ Wallet Created!\n\n💳 `{pub}`\n\n⬇️ Send SOL to this address\n\nTap Refresh to update balance",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            addr = user_wallets[user_id]['address']
            keyboard = [
                [InlineKeyboardButton("🔄 Refresh Balance", callback_data="menu_balance")],
                [InlineKeyboardButton("🔙 Back", callback_data="menu_back")]
            ]
            
            await query.edit_message_text(
                f"💳 Your Wallet\n\n`{addr}`\n\nTap Refresh to check balance",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    
    elif action == 'balance':
        if user_id not in user_wallets:
            await query.edit_message_text(
                "❌ No wallet! Create one first.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
            )
            return
        
        # Show loading message
        await query.answer("Refreshing balance...", show_alert=False)
        
        addr = user_wallets[user_id]['address']
        bal = await get_sol_balance(addr)
        user_wallets[user_id]['balance'] = bal
        save_wallets()
        
        keyboard = [
            [InlineKeyboardButton("🔄 Refresh", callback_data="menu_balance")],
            [InlineKeyboardButton("💸 Withdraw", callback_data="menu_withdraw")],
            [InlineKeyboardButton("🔙 Back", callback_data="menu_back")]
        ]
        
        await query.edit_message_text(
            f"💰 Balance: {bal:.4f} SOL\n\n💳 Address:\n`{addr}`",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif action == 'withdraw':
        if user_id not in user_wallets:
            await query.edit_message_text(
                "❌ No wallet found!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
            )
            return
        
        bal = user_wallets[user_id].get('balance', 0)
        
        if bal < 0.001:
            await query.edit_message_text(
                f"❌ Insufficient balance!\n\nCurrent: {bal:.6f} SOL\nMinimum: 0.001 SOL",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
            )
            return
        
        # Start withdrawal process
        withdraw_requests[user_id] = {'step': 'amount'}
        
        await query.edit_message_text(
            f"💸 Withdraw SOL\n\n"
            f"Available: {bal:.6f} SOL\n"
            f"Fee: ~0.000005 SOL\n\n"
            f"Enter amount to withdraw:\n"
            f"Example: 0.5\n\n"
            f"Or send /cancel to cancel",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="withdraw_cancel")]])
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
        text = "🎯 Auto-Snipe Settings\n\nUse /autobuy @trader in groups to configure!\n\nYou can customize:\n• Market cap filters\n• Buy amount\n• Stop-loss %\n• Take-profit %\n• Slippage %"
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_back")]])
        )
    
    elif action == 'help':
        text = (
            "📖 Commands:\n\n"
            "In Groups:\n"
            "/track @user - Track user\n"
            "/track @channel - Track channel\n"
            "/track -100xxx - Track by ID\n"
            "/autobuy @user - Configure auto-snipe\n"
            "/untrack @user - Stop tracking\n"
            "/list - Show tracked\n"
            "/chatid - Get channel ID\n\n"
            "Anywhere:\n"
            "/wallet - Your wallet\n"
            "/balance - Check balance\n"
            "/withdraw - Withdraw SOL\n\n"
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
            InlineKeyboardButton("🎯 Auto-Snipe", callback_data="menu_autobuy"),
            InlineKeyboardButton("📋 Positions", callback_data="menu_positions")
        ],
        [
            InlineKeyboardButton("💸 Withdraw", callback_data="menu_withdraw"),
            InlineKeyboardButton("❓ Help", callback_data="menu_help")
        ]
    ]
    
    await query.edit_message_text(
        "🤖 CallTracker Menu",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def autobuy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Autobuy setup - DM only for privacy"""
    user_id = str(update.effective_user.id)
    
    # Force DM only for autobuy configuration
    if update.effective_chat.type != 'private':
        await update.message.reply_text(
            "⚠️ Auto-buy must be configured in DM for security!\n\n"
            "👉 Click here to open DM: @" + (await context.bot.get_me()).username
        )
        return
    
    if not context.args:
        await update.message.reply_text(
            "Usage:\n/autobuy @trader\n/autobuy list\n/autobuy off @trader"
        )
        return
    
    if context.args[0].lower() == 'list':
        if user_id not in autobuy_settings or not autobuy_settings[user_id]:
            await update.message.reply_text("No autobuy configured")
            return
        
        text = "🎯 Your Auto-Snipes:\n\n"
        for trader, cfg in autobuy_settings[user_id].items():
            status = "✅" if cfg.get('enabled') else "❌"
            text += f"{status} @{trader}\n"
            text += f"   💰 {cfg['amount']} SOL\n"
            text += f"   📊 Max Mcap: ${cfg['max_mcap']:,}\n"
            text += f"   🔻 SL: {cfg['stoploss']}%\n"
            text += f"   🔺 TP: {cfg['takeprofit']}%\n"
            text += f"   ⚡ Slip: {cfg['slippage']}%\n\n"
        
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
        'takeprofit': 100,
        'slippage': 10
    }
    
    await show_autobuy_menu(update, user_id)

async def show_autobuy_menu(update, user_id):
    """Show autobuy config menu with ALL customization options"""
    cfg = temp_autobuy_config.get(user_id, {})
    trader = cfg.get('trader', '?')
    amount = cfg.get('amount', 0.5)
    mcap = cfg.get('max_mcap', 100000)
    sl = cfg.get('stoploss', 20)
    tp = cfg.get('takeprofit', 100)
    slippage = cfg.get('slippage', 10)
    
    # Format mcap nicely
    if mcap >= 1_000_000:
        mcap_display = f"${mcap / 1_000_000:.1f}M"
    elif mcap >= 1_000:
        mcap_display = f"${mcap / 1_000:.0f}K"
    else:
        mcap_display = f"${mcap:,}"
    
    text = (
        f"🎯 **Auto-Snipe Config:** @{trader}\n\n"
        f"💰 Buy Amount: {amount} SOL\n"
        f"📊 Max Market Cap: {mcap_display}\n"
        f"🔻 Stop Loss: {sl}%\n"
        f"🔺 Take Profit: {tp}%\n"
        f"⚡ Slippage: {slippage}%\n\n"
        f"📝 Tap to customize:"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("💰 Buy Amount", callback_data=f"ab_set_amount_{user_id}"),
            InlineKeyboardButton("📊 Market Cap", callback_data=f"ab_set_mcap_{user_id}")
        ],
        [
            InlineKeyboardButton("🔻 Stop Loss", callback_data=f"ab_set_sl_{user_id}"),
            InlineKeyboardButton("🔺 Take Profit", callback_data=f"ab_set_tp_{user_id}")
        ],
        [
            InlineKeyboardButton("⚡ Slippage", callback_data=f"ab_set_slip_{user_id}")
        ],
        [
            InlineKeyboardButton("✅ Save & Enable", callback_data=f"ab_confirm_{user_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"ab_cancel_{user_id}")
        ]
    ]
    
    markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)

async def handle_autobuy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle autobuy configuration buttons"""
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split('_')
    if len(parts) < 3:
        return
    
    action = parts[1]  # 'set', 'confirm', 'cancel', 'quick'
    setting = parts[2]  # 'amount', 'mcap', 'sl', 'tp', 'slip' OR user_id
    user_id = parts[3] if len(parts) > 3 else str(update.effective_user.id)
    
    if user_id != str(update.effective_user.id):
        await query.answer("Not your menu!", show_alert=True)
        return
    
    if user_id not in temp_autobuy_config:
        await query.answer("Session expired. Run /autobuy again", show_alert=True)
        return
    
    # Handle setting selections
    if action == 'set':
        if setting == 'amount':
            temp_autobuy_config[user_id]['waiting_for'] = 'amount'
            
            # Show quick amounts + custom
            keyboard = [
                [
                    InlineKeyboardButton("0.1 SOL", callback_data=f"ab_quick_amount_0.1_{user_id}"),
                    InlineKeyboardButton("0.25 SOL", callback_data=f"ab_quick_amount_0.25_{user_id}")
                ],
                [
                    InlineKeyboardButton("0.5 SOL", callback_data=f"ab_quick_amount_0.5_{user_id}"),
                    InlineKeyboardButton("1.0 SOL", callback_data=f"ab_quick_amount_1.0_{user_id}")
                ],
                [
                    InlineKeyboardButton("✏️ Custom Amount", callback_data=f"ab_custom_amount_{user_id}")
                ],
                [
                    InlineKeyboardButton("🔙 Back", callback_data=f"ab_back_{user_id}")
                ]
            ]
            
            await query.edit_message_text(
                f"💰 Select Buy Amount:\n\n"
                f"Current: {temp_autobuy_config[user_id]['amount']} SOL\n"
                f"Max: {MAX_AUTOBUY_AMOUNT} SOL\n\n"
                f"Choose quick amount or enter custom:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        elif setting == 'mcap':
            temp_autobuy_config[user_id]['waiting_for'] = 'mcap'
            
            keyboard = [
                [
                    InlineKeyboardButton("$50K", callback_data=f"ab_quick_mcap_50000_{user_id}"),
                    InlineKeyboardButton("$100K", callback_data=f"ab_quick_mcap_100000_{user_id}")
                ],
                [
                    InlineKeyboardButton("$250K", callback_data=f"ab_quick_mcap_250000_{user_id}"),
                    InlineKeyboardButton("$500K", callback_data=f"ab_quick_mcap_500000_{user_id}")
                ],
                [
                    InlineKeyboardButton("$1M", callback_data=f"ab_quick_mcap_1000000_{user_id}"),
                    InlineKeyboardButton("No Limit", callback_data=f"ab_quick_mcap_999999999_{user_id}")
                ],
                [
                    InlineKeyboardButton("✏️ Custom Value", callback_data=f"ab_custom_mcap_{user_id}")
                ],
                [
                    InlineKeyboardButton("🔙 Back", callback_data=f"ab_back_{user_id}")
                ]
            ]
            
            await query.edit_message_text(
                f"📊 Select Max Market Cap:\n\n"
                f"Current: ${temp_autobuy_config[user_id]['max_mcap']:,}\n\n"
                f"Only snipe tokens below this market cap:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        elif setting == 'sl':
            temp_autobuy_config[user_id]['waiting_for'] = 'stoploss'
            
            keyboard = [
                [
                    InlineKeyboardButton("-10%", callback_data=f"ab_quick_sl_10_{user_id}"),
                    InlineKeyboardButton("-20%", callback_data=f"ab_quick_sl_20_{user_id}"),
                    InlineKeyboardButton("-30%", callback_data=f"ab_quick_sl_30_{user_id}")
                ],
                [
                    InlineKeyboardButton("-50%", callback_data=f"ab_quick_sl_50_{user_id}"),
                    InlineKeyboardButton("-75%", callback_data=f"ab_quick_sl_75_{user_id}")
                ],
                [
                    InlineKeyboardButton("✏️ Custom %", callback_data=f"ab_custom_sl_{user_id}")
                ],
                [
                    InlineKeyboardButton("🔙 Back", callback_data=f"ab_back_{user_id}")
                ]
            ]
            
            await query.edit_message_text(
                f"🔻 Select Stop Loss:\n\n"
                f"Current: {temp_autobuy_config[user_id]['stoploss']}%\n\n"
                f"Auto-sell if price drops by this %:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        elif setting == 'tp':
            temp_autobuy_config[user_id]['waiting_for'] = 'takeprofit'
            
            keyboard = [
                [
                    InlineKeyboardButton("+50%", callback_data=f"ab_quick_tp_50_{user_id}"),
                    InlineKeyboardButton("+100%", callback_data=f"ab_quick_tp_100_{user_id}")
                ],
                [
                    InlineKeyboardButton("+200%", callback_data=f"ab_quick_tp_200_{user_id}"),
                    InlineKeyboardButton("+500%", callback_data=f"ab_quick_tp_500_{user_id}")
                ],
                [
                    InlineKeyboardButton("+1000%", callback_data=f"ab_quick_tp_1000_{user_id}")
                ],
                [
                    InlineKeyboardButton("✏️ Custom %", callback_data=f"ab_custom_tp_{user_id}")
                ],
                [
                    InlineKeyboardButton("🔙 Back", callback_data=f"ab_back_{user_id}")
                ]
            ]
            
            await query.edit_message_text(
                f"🔺 Select Take Profit:\n\n"
                f"Current: {temp_autobuy_config[user_id]['takeprofit']}%\n\n"
                f"Auto-sell if price gains this %:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        elif setting == 'slip':
            temp_autobuy_config[user_id]['waiting_for'] = 'slippage'
            
            keyboard = [
                [
                    InlineKeyboardButton("5%", callback_data=f"ab_quick_slip_5_{user_id}"),
                    InlineKeyboardButton("10%", callback_data=f"ab_quick_slip_10_{user_id}"),
                    InlineKeyboardButton("15%", callback_data=f"ab_quick_slip_15_{user_id}")
                ],
                [
                    InlineKeyboardButton("20%", callback_data=f"ab_quick_slip_20_{user_id}"),
                    InlineKeyboardButton("30%", callback_data=f"ab_quick_slip_30_{user_id}"),
                    InlineKeyboardButton("50%", callback_data=f"ab_quick_slip_50_{user_id}")
                ],
                [
                    InlineKeyboardButton("✏️ Custom %", callback_data=f"ab_custom_slip_{user_id}")
                ],
                [
                    InlineKeyboardButton("🔙 Back", callback_data=f"ab_back_{user_id}")
                ]
            ]
            
            await query.edit_message_text(
                f"⚡ Select Slippage Tolerance:\n\n"
                f"Current: {temp_autobuy_config[user_id]['slippage']}%\n\n"
                f"Higher = faster execution, more price impact:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    
    # Handle quick selections (button clicks)
    elif action == 'quick':
        value = float(parts[3])
        
        if setting == 'amount':
            if value > MAX_AUTOBUY_AMOUNT:
                await query.answer(f"Max {MAX_AUTOBUY_AMOUNT} SOL!", show_alert=True)
                return
            temp_autobuy_config[user_id]['amount'] = value
            temp_autobuy_config[user_id].pop('waiting_for', None)
            await query.answer(f"✅ Set to {value} SOL")
            await show_autobuy_menu(update, user_id)
            
        elif setting == 'mcap':
            temp_autobuy_config[user_id]['max_mcap'] = int(value)
            temp_autobuy_config[user_id].pop('waiting_for', None)
            await query.answer(f"✅ Market cap updated")
            await show_autobuy_menu(update, user_id)
            
        elif setting == 'sl':
            temp_autobuy_config[user_id]['stoploss'] = int(value)
            temp_autobuy_config[user_id].pop('waiting_for', None)
            await query.answer(f"✅ Stop loss: {value}%")
            await show_autobuy_menu(update, user_id)
            
        elif setting == 'tp':
            temp_autobuy_config[user_id]['takeprofit'] = int(value)
            temp_autobuy_config[user_id].pop('waiting_for', None)
            await query.answer(f"✅ Take profit: {value}%")
            await show_autobuy_menu(update, user_id)
            
        elif setting == 'slip':
            if value < MIN_SLIPPAGE or value > MAX_SLIPPAGE:
                await query.answer(f"Slippage must be {MIN_SLIPPAGE}-{MAX_SLIPPAGE}%", show_alert=True)
                return
            temp_autobuy_config[user_id]['slippage'] = int(value)
            temp_autobuy_config[user_id].pop('waiting_for', None)
            await query.answer(f"✅ Slippage: {value}%")
            await show_autobuy_menu(update, user_id)
    
    # Handle custom input requests
    elif action == 'custom':
        temp_autobuy_config[user_id]['waiting_for'] = setting
        
        prompts = {
            'amount': f"💰 Enter amount in SOL (0.01-{MAX_AUTOBUY_AMOUNT}):\n\nExample: 0.35",
            'mcap': "📊 Enter max market cap:\n\nExamples:\n• 75000 (for $75K)\n• 300000 (for $300K)",
            'sl': "🔻 Enter stop loss %:\n\nExample: 25 (for -25%)",
            'tp': "🔺 Enter take profit %:\n\nExample: 300 (for +300%)",
            'slip': f"⚡ Enter slippage % ({MIN_SLIPPAGE}-{MAX_SLIPPAGE}):\n\nExample: 12"
        }
        
        await query.edit_message_text(
            f"{prompts.get(setting, 'Enter value:')}\n\n"
            f"Send /cancel to cancel"
        )
    
    # Handle back button
    elif action == 'back':
        temp_autobuy_config[user_id].pop('waiting_for', None)
        await show_autobuy_menu(update, user_id)
    
    # Handle confirm
    elif action == 'confirm':
        cfg = temp_autobuy_config[user_id]
        trader = cfg['trader']
        
        if user_id not in autobuy_settings:
            autobuy_settings[user_id] = {}
        
        autobuy_settings[user_id][trader] = {
            'amount': cfg['amount'],
            'max_mcap': cfg['max_mcap'],
            'stoploss': cfg['stoploss'],
            'takeprofit': cfg['takeprofit'],
            'slippage': cfg['slippage'],
            'enabled': True,
            'daily_trades': 0
        }
        
        save_autobuy_settings()
        del temp_autobuy_config[user_id]
        
        # Don't show fees to user
        amount = cfg['amount']
        
        mcap_display = f"${cfg['max_mcap']:,}" if cfg['max_mcap'] < 999999999 else "No Limit"
        
        await query.edit_message_text(
            f"✅ **Auto-Snipe Enabled!**\n\n"
            f"🎯 Trader: @{trader}\n"
            f"💰 Buy: {amount} SOL\n"
            f"📊 Max Mcap: {mcap_display}\n"
            f"🔻 Stop Loss: -{cfg['stoploss']}%\n"
            f"🔺 Take Profit: +{cfg['takeprofit']}%\n"
            f"⚡ Slippage: {cfg['slippage']}%\n\n"
            f"🚀 Bot will auto-snipe when @{trader} posts CAs!",
            parse_mode='Markdown'
        )
    
    # Handle cancel
    elif action == 'cancel':
        del temp_autobuy_config[user_id]
        await query.edit_message_text("❌ Configuration cancelled")

async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track user or channel"""
    if update.effective_chat.type == 'private':
        await update.message.reply_text("❌ Use in group/channel only")
        return
    
    if not context.args:
        await update.message.reply_text(
            "📋 Track CAs from users/channels:\n\n"
            "**Usage:**\n"
            "/track @username - Track a user\n"
            "/track @channelname - Track a channel\n"
            "/track https://t.me/channelname - Track via link\n"
            "/track -1001234567890 - Track by channel ID\n\n"
            "**How to get channel ID:**\n"
            "1. Forward message from channel\n"
            "2. Use /chatid in channel\n"
            "3. Or use @userinfobot",
            parse_mode='Markdown'
        )
        return
    
    target = context.args[0]
    group_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Handle t.me links (e.g., https://t.me/DalasCrypto)
    if 't.me/' in target:
        # Extract username from link
        target = target.split('t.me/')[-1].split('?')[0].split('/')[0].lower()
    else:
        target = target.lstrip('@').lower()
    
    # Check if it's a channel ID (negative number)
    is_channel_id = target.startswith('-') or (target.startswith('-100') and target[1:].isdigit())
    
    if group_id not in tracked_users:
        tracked_users[group_id] = {}
    if target not in tracked_users[group_id]:
        tracked_users[group_id][target] = []
    
    if user_id not in tracked_users[group_id][target]:
        tracked_users[group_id][target].append(user_id)
        save_tracked_users()
        
        if is_channel_id:
            await update.message.reply_text(
                f"✅ **Tracking Channel**\n\n"
                f"Channel ID: `{target}`\n"
                f"Type: Premium/Private Channel\n\n"
                f"I'll notify you when this channel posts CAs!",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                f"✅ **Now Tracking**\n\n"
                f"@{target}\n\n"
                f"I'll notify you when they post CAs!",
                parse_mode='Markdown'
            )
    else:
        await update.message.reply_text(f"ℹ️ Already tracking {'channel' if is_channel_id else '@' + target}")

async def untrack_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Untrack user or channel"""
    if update.effective_chat.type == 'private':
        await update.message.reply_text("❌ Group only")
        return
    
    if not context.args:
        await update.message.reply_text(
            "❌ Usage:\n"
            "/untrack @username\n"
            "/untrack @channelname\n"
            "/untrack channel_id"
        )
        return
    
    target = context.args[0].lstrip('@').lower()
    group_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if group_id in tracked_users and target in tracked_users[group_id]:
        if user_id in tracked_users[group_id][target]:
            tracked_users[group_id][target].remove(user_id)
            if not tracked_users[group_id][target]:
                del tracked_users[group_id][target]
            save_tracked_users()
            await update.message.reply_text(f"✅ Stopped tracking")
        else:
            await update.message.reply_text(f"❌ Not tracking")
    else:
        await update.message.reply_text(f"❌ Not tracked")

async def list_tracked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List tracked users and channels"""
    if update.effective_chat.type == 'private':
        await update.message.reply_text("❌ Group only")
        return
    
    group_id = update.effective_chat.id
    
    if group_id not in tracked_users or not tracked_users[group_id]:
        await update.message.reply_text("📭 No users or channels tracked\n\nUse /track @username or /track -1001234567890")
        return
    
    # Separate users and channels
    users = []
    channels = []
    
    for identifier in tracked_users[group_id]:
        if identifier.startswith('-'):
            channels.append(identifier)
        else:
            users.append(identifier)
    
    text = "📋 **Tracked in This Group:**\n\n"
    
    if users:
        text += "👥 **Users:**\n"
        for u in users:
            subscriber_count = len(tracked_users[group_id][u])
            text += f"• @{u} ({subscriber_count} subscriber{'s' if subscriber_count != 1 else ''})\n"
        text += "\n"
    
    if channels:
        text += "📢 **Channels:**\n"
        for c in channels:
            subscriber_count = len(tracked_users[group_id][c])
            text += f"• `{c}` ({subscriber_count} subscriber{'s' if subscriber_count != 1 else ''})\n"
    
    await update.message.reply_text(text, parse_mode='Markdown')

async def chatinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current chat/channel info - useful for getting IDs"""
    chat = update.effective_chat
    
    text = "📊 **Chat Information:**\n\n"
    text += f"**Type:** {chat.type}\n"
    text += f"**ID:** `{chat.id}`\n"
    
    if chat.title:
        text += f"**Title:** {chat.title}\n"
    if chat.username:
        text += f"**Username:** @{chat.username}\n"
    
    text += f"\n💡 **To track this chat:**\n"
    if chat.username:
        text += f"`/track @{chat.username}`\n"
    text += f"`/track {chat.id}`"
    
    await update.message.reply_text(text, parse_mode='Markdown')
    await update.message.reply_text(text)

async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get current chat/channel ID - useful for tracking channels"""
    chat = update.effective_chat
    
    info = f"📊 **Chat Info:**\n\n"
    info += f"**Chat ID:** `{chat.id}`\n"
    info += f"**Type:** {chat.type}\n"
    
    if chat.username:
        info += f"**Username:** @{chat.username}\n"
    if chat.title:
        info += f"**Title:** {chat.title}\n"
    
    # If message is from a channel
    if update.message and update.message.sender_chat:
        sender = update.message.sender_chat
        info += f"\n📢 **Forwarded from:**\n"
        info += f"Channel ID: `{sender.id}`\n"
        if sender.username:
            info += f"Username: @{sender.username}\n"
        if sender.title:
            info += f"Title: {sender.title}\n"
        info += f"\n💡 **To track this channel:**\n"
        info += f"`/track {sender.id}`"
    else:
        info += f"\n💡 **To track this chat:**\n"
        if chat.username:
            info += f"`/track @{chat.username}` or\n"
        info += f"`/track {chat.id}`"
    
    await update.message.reply_text(info, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle messages"""
    msg = update.message
    if not msg or not msg.text:
        return
    
    user_id = str(update.effective_user.id)
    
    # Handle withdrawal process
    if user_id in withdraw_requests:
        step = withdraw_requests[user_id].get('step')
        
        if step == 'amount':
            try:
                amount = float(msg.text)
                balance = user_wallets[user_id].get('balance', 0)
                
                if amount < 0.001:
                    await msg.reply_text("❌ Minimum withdrawal: 0.001 SOL")
                    return
                
                if amount > balance - 0.001:
                    await msg.reply_text(f"❌ Insufficient balance!\n\nAvailable: {balance:.6f} SOL\nNeed to keep 0.001 SOL for fees")
                    return
                
                withdraw_requests[user_id]['amount'] = amount
                withdraw_requests[user_id]['step'] = 'address'
                
                await msg.reply_text(
                    f"✅ Amount: {amount} SOL\n\n"
                    f"Now send destination address:\n"
                    f"(Must be a valid Solana address)\n\n"
                    f"Or /cancel to cancel"
                )
                return
                
            except ValueError:
                await msg.reply_text("❌ Invalid amount. Enter a number.\nExample: 0.5")
                return
        
        elif step == 'address':
            address = msg.text.strip()
            
            # Basic validation
            if len(address) < 32 or len(address) > 44:
                await msg.reply_text("❌ Invalid Solana address length")
                return
            
            # Check it's base58
            try:
                Pubkey.from_string(address)
            except:
                await msg.reply_text("❌ Invalid Solana address format")
                return
            
            amount = withdraw_requests[user_id]['amount']
            
            await msg.reply_text(f"⏳ Processing withdrawal...\n\n{amount} SOL → `{address[:8]}...{address[-8:]}`", parse_mode='Markdown')
            
            tx_sig, error = await execute_withdrawal(user_id, amount, address)
            
            if tx_sig:
                # Update balance
                new_balance = await get_sol_balance(user_wallets[user_id]['address'])
                user_wallets[user_id]['balance'] = new_balance
                save_wallets()
                
                await msg.reply_text(
                    f"✅ Withdrawal Successful!\n\n"
                    f"Amount: {amount} SOL\n"
                    f"To: `{address[:8]}...{address[-8:]}`\n\n"
                    f"TX: `{tx_sig[:16]}...`\n\n"
                    f"[View on Solscan](https://solscan.io/tx/{tx_sig})\n\n"
                    f"New Balance: {new_balance:.6f} SOL",
                    parse_mode='Markdown'
                )
            else:
                await msg.reply_text(f"❌ Withdrawal Failed!\n\nError: {error}")
            
            del withdraw_requests[user_id]
            return
    
    # Handle autobuy custom input
    if user_id in temp_autobuy_config and temp_autobuy_config[user_id].get('waiting_for'):
        field = temp_autobuy_config[user_id]['waiting_for']
        
        try:
            value = float(msg.text)
            
            if field == 'amount':
                if value < 0.01 or value > MAX_AUTOBUY_AMOUNT:
                    await msg.reply_text(f"❌ Amount must be 0.01-{MAX_AUTOBUY_AMOUNT} SOL")
                    return
                temp_autobuy_config[user_id]['amount'] = value
                await msg.reply_text(f"✅ Buy amount set to {value} SOL")
                
            elif field == 'mcap':
                if value < 0:
                    await msg.reply_text("❌ Market cap must be positive")
                    return
                temp_autobuy_config[user_id]['max_mcap'] = int(value)
                await msg.reply_text(f"✅ Max market cap set to ${int(value):,}")
                
            elif field == 'stoploss':
                if value < 0 or value > 100:
                    await msg.reply_text("❌ Stop loss must be 0-100%")
                    return
                temp_autobuy_config[user_id]['stoploss'] = int(value)
                await msg.reply_text(f"✅ Stop loss set to {int(value)}%")
                
            elif field == 'takeprofit':
                if value < 0:
                    await msg.reply_text("❌ Take profit must be positive")
                    return
                temp_autobuy_config[user_id]['takeprofit'] = int(value)
                await msg.reply_text(f"✅ Take profit set to {int(value)}%")
                
            elif field == 'slippage':
                if value < MIN_SLIPPAGE or value > MAX_SLIPPAGE:
                    await msg.reply_text(f"❌ Slippage must be {MIN_SLIPPAGE}-{MAX_SLIPPAGE}%")
                    return
                temp_autobuy_config[user_id]['slippage'] = int(value)
                await msg.reply_text(f"✅ Slippage set to {int(value)}%")
            
            temp_autobuy_config[user_id].pop('waiting_for', None)
            await show_autobuy_menu(update, user_id)
            return
            
        except ValueError:
            await msg.reply_text("❌ Invalid number. Try again or /cancel")
            return
    
    # Regular CA detection
    group_id = msg.chat.id
    
    # Detect if message is from user or channel
    username = None
    channel_id = None
    
    if msg.from_user:
        # Message from a regular user
        username = msg.from_user.username.lower() if msg.from_user.username else None
    elif msg.sender_chat:
        # Message from a channel
        if msg.sender_chat.username:
            username = msg.sender_chat.username.lower()
        # Also track by channel ID
        channel_id = str(msg.sender_chat.id)
    else:
        return
    
    # Check if we're tracking this user/channel
    tracked_identifier = None
    if group_id in tracked_users:
        # Check username
        if username and username in tracked_users[group_id]:
            tracked_identifier = username
        # Check channel ID
        elif channel_id and channel_id in tracked_users[group_id]:
            tracked_identifier = channel_id
    
    if not tracked_identifier:
        return
    
    detected = []
    for word in msg.text.split():
        clean = word.strip('.,!?()[]{}')
        chain_id, blockchain, ca = detect_blockchain(clean)
        if blockchain and ca:
            detected.append((chain_id, blockchain, ca))
    
    if not detected:
        return
    
    user_ids = tracked_users[group_id][tracked_identifier]
    
    # Get display name for notifications
    display_name = username if username else f"Channel {channel_id}"
    
    for uid in user_ids:
        try:
            await context.bot.send_message(uid, f"📌 CA from @{display_name}")
            
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
                
                # AUTO-SNIPE
                if chain_id == 'solana':
                    await check_and_execute_autobuy(uid, tracked_identifier, ca, token_info, context)
            
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
    
    # Check mcap
    if token_info and token_info.get('mcap_raw', 0) > cfg.get('max_mcap', 100000):
        await context.bot.send_message(uid, "⚠️ Market cap too high, skipping")
        return
    
    # Check daily limit
    if cfg.get('daily_trades', 0) >= MAX_DAILY_TRADES:
        await context.bot.send_message(uid, "⚠️ Daily trade limit reached!")
        return
    
    # Check balance
    if user_id not in user_wallets:
        return
    
    balance = user_wallets[user_id].get('balance', 0)
    amount = cfg.get('amount', 0.5)
    fee_amount = amount * (FEE_PERCENTAGE / 100)
    total_needed = amount + fee_amount + 0.002  # trade + fee + gas buffer
    
    if balance < total_needed:
        await context.bot.send_message(
            uid,
            f"❌ Insufficient balance!\n\n"
            f"Need: {total_needed:.4f} SOL\n"
            f"• Trade: {amount} SOL\n"
            f"• Fee: {fee_amount:.4f} SOL ({FEE_PERCENTAGE}%)\n"
            f"• Gas: ~0.002 SOL\n\n"
            f"Have: {balance:.4f} SOL"
        )
        return
    
    # EXECUTE
    await context.bot.send_message(
        uid, 
        f"🎯 AUTO-SNIPING {amount} SOL...\n"
        f"⚡ Slippage: {cfg.get('slippage', 10)}%\n"
        f"💰 Fee: {fee_amount:.4f} SOL ({FEE_PERCENTAGE}%)"
    )
    
    tx_sig, error = await execute_jupiter_swap(user_id, ca, amount, cfg.get('slippage', 10))
    
    if tx_sig:
        # Collect fee after successful trade
        fee_tx, fee_error = await collect_fee(user_id, amount)
        
        fee_status = ""
        if fee_tx:
            fee_status = f"\n💸 Fee collected: {fee_amount:.4f} SOL"
        elif fee_error and fee_error not in ["Fee wallet not set", "Fee disabled", "Fee too small"]:
            fee_status = f"\n⚠️ Fee collection pending"
        
        await context.bot.send_message(
            uid,
            f"✅ SNIPE SUCCESSFUL!\n\n"
            f"💰 Amount: {amount} SOL\n"
            f"🎯 Token: {token_info.get('symbol', 'Unknown') if token_info else 'Unknown'}\n"
            f"📊 Mcap: {token_info.get('mcap', 'N/A') if token_info else 'N/A'}\n"
            f"🔻 Stop Loss: -{cfg['stoploss']}%\n"
            f"🔺 Take Profit: +{cfg['takeprofit']}%{fee_status}\n\n"
            f"TX: `{tx_sig[:16]}...`\n\n"
            f"[View on Solscan](https://solscan.io/tx/{tx_sig})",
            parse_mode='Markdown'
        )
        
        cfg['daily_trades'] = cfg.get('daily_trades', 0) + 1
        save_autobuy_settings()
        
        # Update balance after trade + fee
        new_balance = await get_sol_balance(user_wallets[user_id]['address'])
        user_wallets[user_id]['balance'] = new_balance
        save_wallets()
    else:
        await context.bot.send_message(
            uid,
            f"❌ SNIPE FAILED!\n\nError: {error}\n\nCheck balance and settings."
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
            f"✅ Wallet Created!\n\n💳 `{pub}`\n\n⬇️ Send SOL to this address\n\nUse /balance to check",
            parse_mode='Markdown'
        )
    else:
        addr = user_wallets[user_id]['address']
        bal = await get_sol_balance(addr)
        user_wallets[user_id]['balance'] = bal
        save_wallets()
        await update.message.reply_text(
            f"💳 Your Wallet\n\n`{addr}`\n\nBalance: {bal:.4f} SOL",
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
    
    await update.message.reply_text(
        f"💰 Balance: {bal:.4f} SOL\n\n💳 `{addr}`",
        parse_mode='Markdown'
    )

async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start withdrawal"""
    user_id = str(update.effective_user.id)
    
    if user_id not in user_wallets:
        await update.message.reply_text("❌ No wallet found! Use /wallet first")
        return
    
    balance = user_wallets[user_id].get('balance', 0)
    
    if balance < 0.001:
        await update.message.reply_text(
            f"❌ Insufficient balance!\n\nCurrent: {balance:.6f} SOL\nMinimum: 0.001 SOL"
        )
        return
    
    withdraw_requests[user_id] = {'step': 'amount'}
    
    await update.message.reply_text(
        f"💸 Withdraw SOL\n\n"
        f"Available: {balance:.6f} SOL\n"
        f"Fee: ~0.000005 SOL\n\n"
        f"Enter amount to withdraw:\n"
        f"Example: 0.5\n\n"
        f"Or /cancel to cancel"
    )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel operations"""
    user_id = str(update.effective_user.id)
    
    if user_id in withdraw_requests:
        del withdraw_requests[user_id]
        await update.message.reply_text("❌ Withdrawal cancelled")
    elif user_id in temp_autobuy_config:
        del temp_autobuy_config[user_id]
        await update.message.reply_text("❌ Auto-snipe config cancelled")
    else:
        await update.message.reply_text("Nothing to cancel")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help - Show all commands"""
    text = (
        "📖 **Bot Commands**\n\n"
        "**In Groups:**\n"
        "/track @user - Track CA alerts\n"
        "/track @channel - Track channel\n"
        "/track -100xxx - Track by ID\n"
        "/chatid - Get channel/chat ID\n"
        "/untrack @user - Stop tracking\n"
        "/list - Show tracked\n\n"
        "**In DM (Private):**\n"
        "/wallet - Your wallet\n"
        "/balance - Check balance\n"
        "/autobuy @trader - Setup auto-snipe\n"
        "/autobuy list - Your configs\n"
        "/withdraw - Withdraw SOL\n"
        "/cancel - Cancel operation\n\n"
        "**Main Menu:**\n"
        "/start - Show menu\n\n"
        "Support: @Makafog"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def handle_withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle withdraw cancellation"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    
    if query.data == 'withdraw_cancel':
        if user_id in withdraw_requests:
            del withdraw_requests[user_id]
        await query.edit_message_text("❌ Withdrawal cancelled")

def main():
    """Main"""
    print("🚀 CallTracker Auto-Snipe v2.0 ENHANCED + FEE COLLECTION")
    
    load_tracked_users()
    load_wallets()
    load_autobuy_settings()
    load_positions()
    load_fee_stats()
    
    app = Application.builder().token(TOKEN).build()
    
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_solana_client())
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("track", track_user))
    app.add_handler(CommandHandler("untrack", untrack_user))
    app.add_handler(CommandHandler("list", list_tracked))
    app.add_handler(CommandHandler("chatid", get_chat_id))
    app.add_handler(CommandHandler("wallet", wallet_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("withdraw", withdraw_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("autobuy", autobuy_command))
    app.add_handler(CallbackQueryHandler(handle_menu_callback, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(handle_autobuy_callback, pattern="^ab_"))
    app.add_handler(CallbackQueryHandler(handle_withdraw_callback, pattern="^withdraw_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print(f"⚠️ ENCRYPTION KEY: {ENCRYPTION_KEY.decode()}")
    print("⚠️ SAVE THIS KEY SECURELY!")
    print("=" * 50)
    print("✅ ENHANCED AUTO-SNIPE BOT READY!")
    print("=" * 50)
    print("✅ Market cap filters")
    print("✅ Custom amounts")
    print("✅ Custom stop-loss/take-profit")
    print("✅ Slippage control")
    print("✅ Withdrawal function")
    print("✅ Balance refresh button")
    print("=" * 50)
    if FEE_WALLET and FEE_WALLET != "YOUR_SOLANA_WALLET_ADDRESS_HERE":
        print(f"💰 FEE COLLECTION ENABLED")
        print(f"   Fee Wallet: {FEE_WALLET[:8]}...{FEE_WALLET[-8:]}")
        print(f"   Fee Rate: {FEE_PERCENTAGE}% per trade")
        print(f"   Total Collected: {fee_stats.get('total_collected', 0):.6f} SOL")
        print(f"   Total Trades: {fee_stats.get('total_trades', 0)}")
    else:
        print("⚠️  FEE COLLECTION DISABLED - Set FEE_WALLET in config")
    print("=" * 50)
    
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False
    )

if __name__ == "__main__":
    main()

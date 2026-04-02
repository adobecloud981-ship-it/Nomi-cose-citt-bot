"""
Bot Telegram per Nomi, Cose, Città
Un gioco divertente da giocare in gruppo!
"""

import os
import random
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dotenv import load_dotenv
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# Load environment variables
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot Token
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')

# MongoDB connection
mongo_url = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
db_name = os.environ.get('DB_NAME', 'test_database')
client = AsyncIOMotorClient(mongo_url)
db = client[db_name]

# Default categories
DEFAULT_CATEGORIES = ["Nomi", "Cose", "Città", "Animali", "Frutta"]
EXTENDED_CATEGORIES = ["Nomi", "Cose", "Città", "Animali", "Frutta", "Professioni", "Film", "Cantanti", "Sport", "Fiori"]

# Italian alphabet (without foreign letters)
ITALIAN_LETTERS = list("ABCDEFGHILMNOPQRSTUVZ")

# Points system
POINTS_CORRECT = 10
POINTS_DUPLICATE = 5

# Game states
class GameState:
    WAITING = "waiting"
    CATEGORIES_SETUP = "categories_setup"
    PLAYING = "playing"
    REVIEWING = "reviewing"
    FINISHED = "finished"

# In-memory game storage (backed by MongoDB for persistence)
games: Dict[int, dict] = {}
player_answers: Dict[str, dict] = {}  # key: f"{chat_id}_{user_id}"


async def save_game_to_db(chat_id: int):
    """Save game state to MongoDB"""
    if chat_id in games:
        game = games[chat_id].copy()
        game['chat_id'] = chat_id
        game['updated_at'] = datetime.now(timezone.utc).isoformat()
        await db.games.update_one(
            {'chat_id': chat_id},
            {'$set': game},
            upsert=True
        )


async def load_game_from_db(chat_id: int) -> Optional[dict]:
    """Load game state from MongoDB"""
    game = await db.games.find_one({'chat_id': chat_id}, {'_id': 0})
    return game


# ============= COMMAND HANDLERS =============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message and help"""
    welcome_text = """
86|🎮 *Benvenuto a Nomi, Cose, Città!*

Questo è il classico gioco di parole italiano, ora in versione Telegram!

*Come funziona:*
91|1️⃣ Crea una partita con /nuova\\_partita
92|2️⃣ I giocatori si uniscono con /partecipa
93|3️⃣ Il creatore avvia il gioco con /inizia
94|4️⃣ Viene estratta una lettera casuale
95|5️⃣ Scrivi le tue risposte in chat privata al bot
96|6️⃣ Conferma quando hai finito
97|7️⃣ Vedi le risposte e contesta quelle sbagliate!

*Comandi:*
/nuova\\_partita \\- Crea una nuova partita
/partecipa \\- Unisciti alla partita
/inizia \\- Avvia il gioco \\(solo creatore\\)
/stato \\- Mostra lo stato della partita
/classifica \\- Mostra la classifica
/annulla \\- Annulla la partita \\(solo creatore\\)
/help \\- Mostra questo messaggio

Buon divertimento\\! 🎉
"""
    await update.message.reply_text(welcome_text, parse_mode='MarkdownV2')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help"""
    await start(update, context)


async def new_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a new game"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if update.effective_chat.type == 'private':
        await update.message.reply_text("⚠️ Questo comando funziona solo nei gruppi! Aggiungi il bot a un gruppo per giocare.")
        return
    
    if chat_id in games and games[chat_id]['state'] != GameState.FINISHED:
        await update.message.reply_text("⚠️ C'è già una partita in corso! Usa /annulla per terminarla.")
        return
    
    # Create new game
    games[chat_id] = {
        'state': GameState.WAITING,
        'creator_id': user.id,
        'creator_name': user.first_name,
        'players': {str(user.id): {'name': user.first_name, 'score': 0}},
        'target_score': 300,
        'timer': None,
        'categories': EXTENDED_CATEGORIES.copy(),
        'custom_categories': [],
        'current_letter': None,
        'round': 0,
        'answers': {},
        'finished_players': [],
        'disputes': {},
        'created_at': datetime.now(timezone.utc).isoformat()
    }
    
    await save_game_to_db(chat_id)
    
    # Show setup keyboard
    keyboard = [
        [InlineKeyboardButton("🎯 Punti Obiettivo", callback_data="setup_points")],
        [InlineKeyboardButton("⏱️ Timer (opzionale)", callback_data="setup_timer")],
        [InlineKeyboardButton("📝 Categorie", callback_data="setup_categories")],
        [InlineKeyboardButton("✅ Conferma Impostazioni", callback_data="confirm_setup")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🎮 *Nuova partita creata da {user.first_name}\\!*\n\n"
        f"*Impostazioni attuali:*\n"
        f"🎯 Punti obiettivo: {games[chat_id]['target_score']}\n"
        f"⏱️ Timer: Disattivato\n"
        f"📝 Categorie: {len(games[chat_id]['categories'])}\n\n"
        f"Configura la partita o premi *Conferma Impostazioni* per continuare\\.",
        parse_mode='MarkdownV2',
        reply_markup=reply_markup
    )


async def join_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Join an existing game"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if chat_id not in games:
        await update.message.reply_text("⚠️ Non c'è nessuna partita attiva! Creane una con /nuova_partita")
        return
    
    game = games[chat_id]
    
    if game['state'] != GameState.WAITING:
        await update.message.reply_text("⚠️ La partita è già iniziata! Aspetta il prossimo round.")
        return
    
    user_id = str(user.id)
    if user_id in game['players']:
        await update.message.reply_text("✅ Sei già nella partita!")
        return
    
    game['players'][user_id] = {'name': user.first_name, 'score': 0}
    await save_game_to_db(chat_id)
    
    player_list = "\n".join([f"• {p['name']}" for p in game['players'].values()])
    
    await update.message.reply_text(
        f"✅ *{user.first_name}* si è unito alla partita!\n\n"
        f"*Giocatori ({len(game['players'])}):*\n{player_list}",
        parse_mode='Markdown'
    )


async def start_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the game (only creator)"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if chat_id not in games:
        await update.message.reply_text("⚠️ Non c'è nessuna partita attiva!")
        return
    
    game = games[chat_id]
    
    if game['creator_id'] != user.id:
        await update.message.reply_text("⚠️ Solo il creatore della partita può avviarla!")
        return
    
    if game['state'] != GameState.WAITING:
        await update.message.reply_text("⚠️ La partita è già iniziata!")
        return
    
    if len(game['players']) < 2:
        await update.message.reply_text("⚠️ Servono almeno 2 giocatori per iniziare! Invita altri amici con /partecipa")
        return
    
    await start_new_round(chat_id, context)


async def start_new_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Start a new round"""
    game = games[chat_id]
    game['round'] += 1
    game['state'] = GameState.PLAYING
    game['current_letter'] = random.choice(ITALIAN_LETTERS)
    game['answers'] = {}
    game['finished_players'] = []
    game['disputes'] = {}
    
    # Clear player answers
    for player_id in game['players']:
        key = f"{chat_id}_{player_id}"
        if key in player_answers:
            del player_answers[key]
    
    await save_game_to_db(chat_id)
    
    categories_text = "\n".join([f"• {cat}" for cat in game['categories']])
    
    round_message = (
        f"🎲 *ROUND {game['round']}*\n\n"
        f"📌 La lettera è: *{game['current_letter']}*\n\n"
        f"📝 *Categorie:*\n{categories_text}\n\n"
        f"Scrivi le tue risposte in *chat privata* al bot!\n"
        f"Quando hai finito, premi il pulsante per confermare."
    )
    
    # Notify each player privately
    for player_id, player_data in game['players'].items():
        try:
            keyboard = [[InlineKeyboardButton("📝 Rispondi", callback_data=f"answer_{chat_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await context.bot.send_message(
                chat_id=int(player_id),
                text=round_message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Couldn't send private message to {player_id}: {e}")
    
    # Send in group
    await context.bot.send_message(
        chat_id=chat_id,
        text=round_message + f"\n\n⏳ Giocatori: {len(game['players'])} | Completati: 0",
        parse_mode='Markdown'
    )
    
    # Start timer if set
    if game['timer']:
        asyncio.create_task(round_timer(chat_id, context, game['timer']))


async def round_timer(chat_id: int, context: ContextTypes.DEFAULT_TYPE, seconds: int):
    """Timer for round"""
    await asyncio.sleep(seconds)
    
    if chat_id in games and games[chat_id]['state'] == GameState.PLAYING:
        await end_round(chat_id, context, forced=True)


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current game status"""
    chat_id = update.effective_chat.id
    
    if chat_id not in games:
        await update.message.reply_text("⚠️ Non c'è nessuna partita attiva!")
        return
    
    game = games[chat_id]
    
    status_text = f"📊 *Stato Partita*\n\n"
    status_text += f"🎯 Obiettivo: {game['target_score']} punti\n"
    status_text += f"⏱️ Timer: {game['timer'] if game['timer'] else 'Disattivato'}s\n"
    status_text += f"📝 Categorie: {len(game['categories'])}\n"
    status_text += f"🔄 Round: {game['round']}\n"
    status_text += f"📌 Lettera: {game['current_letter'] or 'N/A'}\n\n"
    
    status_text += f"*Giocatori:*\n"
    for player_id, player_data in game['players'].items():
        finished = "✅" if player_id in game.get('finished_players', []) else "⏳"
        status_text += f"{finished} {player_data['name']}: {player_data['score']} punti\n"
    
    await update.message.reply_text(status_text, parse_mode='Markdown')


async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show leaderboard"""
    chat_id = update.effective_chat.id
    
    if chat_id not in games:
        await update.message.reply_text("⚠️ Non c'è nessuna partita attiva!")
        return
    
    game = games[chat_id]
    
    # Sort players by score
    sorted_players = sorted(
        game['players'].items(),
        key=lambda x: x[1]['score'],
        reverse=True
    )
    
    leaderboard = "🏆 *CLASSIFICA*\n\n"
    medals = ["🥇", "🥈", "🥉"]
    
    for i, (player_id, player_data) in enumerate(sorted_players):
        medal = medals[i] if i < 3 else f"{i+1}."
        leaderboard += f"{medal} {player_data['name']}: {player_data['score']} punti\n"
    
    leaderboard += f"\n🎯 Obiettivo: {game['target_score']} punti"
    
    await update.message.reply_text(leaderboard, parse_mode='Markdown')


async def cancel_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel current game (only creator)"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if chat_id not in games:
        await update.message.reply_text("⚠️ Non c'è nessuna partita attiva!")
        return
    
    game = games[chat_id]
    
    if game['creator_id'] != user.id:
        await update.message.reply_text("⚠️ Solo il creatore può annullare la partita!")
        return
    
    del games[chat_id]
    await db.games.delete_one({'chat_id': chat_id})
    
    await update.message.reply_text("❌ Partita annullata!")


# ============= CALLBACK HANDLERS =============

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user = update.effective_user
    
    if data == "setup_points":
        keyboard = [
            [InlineKeyboardButton("30", callback_data="points_30"),
             InlineKeyboardButton("50", callback_data="points_50"),
             InlineKeyboardButton("100", callback_data="points_100")],
            [InlineKeyboardButton("🔙 Indietro", callback_data="back_setup")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🎯 *Seleziona i punti obiettivo:*\n\nIl gioco termina quando un giocatore raggiunge questo punteggio.",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    elif data.startswith("points_"):
        chat_id = query.message.chat_id
        points = int(data.split("_")[1])
        if chat_id in games:
            games[chat_id]['target_score'] = points
            await save_game_to_db(chat_id)
        await show_setup_menu(query, games.get(chat_id))
    
    elif data == "setup_timer":
        keyboard = [
            [InlineKeyboardButton("60s", callback_data="timer_60"),
             InlineKeyboardButton("90s", callback_data="timer_90"),
             InlineKeyboardButton("120s", callback_data="timer_120")],
            [InlineKeyboardButton("❌ Nessun timer", callback_data="timer_none")],
            [InlineKeyboardButton("🔙 Indietro", callback_data="back_setup")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "⏱️ *Seleziona il timer per ogni round:*\n\nIl timer è facoltativo.",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    elif data.startswith("timer_"):
        chat_id = query.message.chat_id
        timer_value = data.split("_")[1]
        if chat_id in games:
            games[chat_id]['timer'] = int(timer_value) if timer_value != "none" else None
            await save_game_to_db(chat_id)
        await show_setup_menu(query, games.get(chat_id))
    
    elif data == "setup_categories":
        chat_id = query.message.chat_id
        keyboard = [
            [InlineKeyboardButton("📋 Classiche (5)", callback_data="cat_classic")],
            [InlineKeyboardButton("📋 Estese (10)", callback_data="cat_extended")],
            [InlineKeyboardButton("✏️ Personalizza", callback_data="cat_custom")],
            [InlineKeyboardButton("🔙 Indietro", callback_data="back_setup")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        current_cats = "\n".join([f"• {c}" for c in games.get(chat_id, {}).get('categories', [])])
        
        await query.edit_message_text(
            f"📝 *Seleziona le categorie:*\n\n*Categorie attuali:*\n{current_cats}",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    elif data == "cat_classic":
        chat_id = query.message.chat_id
        if chat_id in games:
            games[chat_id]['categories'] = DEFAULT_CATEGORIES.copy()
            await save_game_to_db(chat_id)
        await show_setup_menu(query, games.get(chat_id))
    
    elif data == "cat_extended":
        chat_id = query.message.chat_id
        if chat_id in games:
            games[chat_id]['categories'] = EXTENDED_CATEGORIES.copy()
            await save_game_to_db(chat_id)
        await show_setup_menu(query, games.get(chat_id))
    
    elif data == "cat_custom":
        chat_id = query.message.chat_id
        if chat_id in games:
            games[chat_id]['state'] = GameState.CATEGORIES_SETUP
            await save_game_to_db(chat_id)
        
        current_cats = ", ".join(games.get(chat_id, {}).get('categories', []))
        
        keyboard = [[InlineKeyboardButton("✅ Conferma Categorie", callback_data="cat_confirm")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"✏️ *Personalizza le categorie*\n\n"
            f"Scrivi le nuove categorie separate da virgola.\n"
            f"Esempio: `Nomi, Città, Film, Colori`\n\n"
            f"*Categorie attuali:*\n{current_cats}",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    elif data == "cat_confirm":
        chat_id = query.message.chat_id
        if chat_id in games:
            games[chat_id]['state'] = GameState.WAITING
            await save_game_to_db(chat_id)
        await show_setup_menu(query, games.get(chat_id))
    
    elif data == "back_setup" or data == "confirm_setup":
        chat_id = query.message.chat_id
        if chat_id in games:
            games[chat_id]['state'] = GameState.WAITING
        await show_setup_menu(query, games.get(chat_id), confirmed=(data == "confirm_setup"))
    
    elif data.startswith("answer_"):
        # Player wants to answer
        chat_id = int(data.split("_")[1])
        user_id = str(user.id)
        
        if chat_id not in games:
            await query.edit_message_text("⚠️ La partita non è più attiva!")
            return
        
        game = games[chat_id]
        
        if user_id not in game['players']:
            await query.edit_message_text("⚠️ Non sei in questa partita!")
            return
        
        if game['state'] != GameState.PLAYING:
            await query.edit_message_text("⚠️ Il round non è attivo!")
            return
        
        # Initialize answers for this player
        key = f"{chat_id}_{user_id}"
        if key not in player_answers:
            player_answers[key] = {
                'chat_id': chat_id,
                'categories': {cat: '' for cat in game['categories']},
                'current_category': 0
            }
        
        await show_answer_prompt(query, chat_id, user_id)
    
    elif data.startswith("submit_"):
        # Player submits answers
        chat_id = int(data.split("_")[1])
        user_id = str(user.id)
        
        await submit_player_answers(query, context, chat_id, user_id)
    
    elif data.startswith("dispute_"):
        # Dispute an answer
        parts = data.split("_")
        chat_id = int(parts[1])
        target_user_id = parts[2]
        category = parts[3]
        
        await handle_dispute(query, context, chat_id, target_user_id, category, str(user.id))
    
    elif data.startswith("calculate_"):
        # Calculate scores
        chat_id = int(data.split("_")[1])
        if chat_id in games:
            await calculate_round_scores(chat_id, context)
    
    elif data.startswith("nextround_"):
        # Start next round
        chat_id = int(data.split("_")[1])
        if chat_id in games and games[chat_id]['creator_id'] == user.id:
            await start_new_round(chat_id, context)


async def show_setup_menu(query, game: dict, confirmed: bool = False):
    """Show the setup menu"""
    if not game:
        await query.edit_message_text("⚠️ Errore: partita non trovata!")
        return
    
    keyboard = [
        [InlineKeyboardButton("🎯 Punti Obiettivo", callback_data="setup_points")],
        [InlineKeyboardButton("⏱️ Timer (opzionale)", callback_data="setup_timer")],
        [InlineKeyboardButton("📝 Categorie", callback_data="setup_categories")],
        [InlineKeyboardButton("✅ Conferma Impostazioni", callback_data="confirm_setup")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    timer_text = f"{game['timer']}s" if game['timer'] else "Disattivato"
    confirmed_text = "\n\n✅ *Impostazioni confermate!* Ora i giocatori possono unirsi con /partecipa e il creatore può avviare con /inizia" if confirmed else ""
    
    await query.edit_message_text(
        f"🎮 *Impostazioni Partita*\n\n"
        f"🎯 Punti obiettivo: {game['target_score']}\n"
        f"⏱️ Timer: {timer_text}\n"
        f"📝 Categorie: {len(game['categories'])}\n"
        f"👥 Giocatori: {len(game['players'])}"
        f"{confirmed_text}",
        parse_mode='Markdown',
        reply_markup=reply_markup if not confirmed else None
    )


async def show_answer_prompt(query, chat_id: int, user_id: str):
    """Show prompt to enter answers"""
    key = f"{chat_id}_{user_id}"
    game = games[chat_id]
    answers = player_answers[key]
    
    categories_list = []
    for cat in game['categories']:
        answer = answers['categories'].get(cat, '')
        status = "✅" if answer else "❓"
        categories_list.append(f"{status} *{cat}*: {answer or '_______'}")
    
    categories_text = "\n".join(categories_list)
    
    keyboard = [[InlineKeyboardButton("✅ Invia Risposte", callback_data=f"submit_{chat_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"📝 *Lettera: {game['current_letter']}*\n\n"
        f"Scrivi le tue risposte una per volta.\n"
        f"Formato: `Categoria: Risposta`\n"
        f"Esempio: `Nomi: Marco`\n\n"
        f"{categories_text}\n\n"
        f"Quando hai finito, premi *Invia Risposte*",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )


async def submit_player_answers(query, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: str):
    """Submit player's answers"""
    key = f"{chat_id}_{user_id}"
    
    if chat_id not in games:
        await query.edit_message_text("⚠️ La partita non è più attiva!")
        return
    
    game = games[chat_id]
    
    if user_id in game['finished_players']:
        await query.edit_message_text("✅ Hai già inviato le tue risposte!")
        return
    
    # Save answers
    if key in player_answers:
        game['answers'][user_id] = player_answers[key]['categories']
    else:
        game['answers'][user_id] = {cat: '' for cat in game['categories']}
    
    game['finished_players'].append(user_id)
    await save_game_to_db(chat_id)
    
    player_name = game['players'][user_id]['name']
    
    await query.edit_message_text(
        f"✅ *Risposte inviate!*\n\n"
        f"Attendi che tutti i giocatori finiscano..."
    )
    
    # Notify group
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ *{player_name}* ha finito! ({len(game['finished_players'])}/{len(game['players'])})",
        parse_mode='Markdown'
    )
    
    # Check if all players finished
    if len(game['finished_players']) >= len(game['players']):
        await end_round(chat_id, context)


async def end_round(chat_id: int, context: ContextTypes.DEFAULT_TYPE, forced: bool = False):
    """End the current round and show results"""
    game = games[chat_id]
    game['state'] = GameState.REVIEWING
    await save_game_to_db(chat_id)
    
    if forced:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏰ *Tempo scaduto!*",
            parse_mode='Markdown'
        )
    
    # Show all answers
    results_text = f"📊 *RISULTATI ROUND {game['round']}*\n"
    results_text += f"📌 Lettera: *{game['current_letter']}*\n\n"
    
    # Calculate points and create dispute buttons
    keyboard = []
    
    for player_id, player_data in game['players'].():
        player_name = player_data['name']
        results_text += f"👤 *{player_name}:*\n"
        
        answers = game['answers'].get(player_id, {})
        
        for category in game['categories']:
            answer = answers.get(category, '')
            
            # Check if answer is valid
            status, points = evaluate_answer(answer, game['current_letter'], category, player_id, game)
            
            results_text += f"  • {category}: {answer or '❌ Mancante'} {status}\n"
            
            # Add dispute button if answer exists and is not already invalid
            if answer and status != "❌":
                keyboard.append([InlineKeyboardButton(
                    f"⚠️ Contesta {player_name}: {category}",
                    callback_data=f"dispute_{chat_id}_{player_id}_{category}"
                )])
        
        results_text += "\n"
    
    keyboard.append([InlineKeyboardButton("📊 Calcola Punteggi", callback_data=f"calculate_{chat_id}")])
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=results_text + "\n⚠️ Hai 30 secondi per contestare le risposte!",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )
    
    # Wait for disputes then calculate
    await asyncio.sleep(30)
    await calculate_round_scores(chat_id, context)


def evaluate_answer(answer: str, letter: str, category: str, player_id: str, game: dict) -> tuple:
    """Evaluate an answer and return status and points"""
    if not answer:
        return "❌", 0
    
    # Check if starts with correct letter
    if not answer.upper().startswith(letter):
        return "❌ (lettera sbagliata)", 0
    
    # Check for duplicates
    duplicates = 0
    for other_id, other_answers in game['answers'].items():
        if other_id != player_id:
            other_answer = other_answers.get(category, '')
            if other_answer.lower().strip() == answer.lower().strip():
                duplicates += 1
    
    if duplicates > 0:
        return f"⚡ (x{duplicates+1})", POINTS_DUPLICATE
    
    return "✅", POINTS_CORRECT


async def handle_dispute(query, context: ContextTypes.DEFAULT_TYPE, chat_id: int, target_user_id: str, category: str, disputer_id: str):
    """Handle a dispute"""
    if chat_id not in games:
        await query.answer("Partita non trovata!")
        return
    
    game = games[chat_id]
    
    dispute_key = f"{target_user_id}_{category}"
    if dispute_key not in game['disputes']:
        game['disputes'][dispute_key] = []
    
    if disputer_id in game['disputes'][dispute_key]:
        await query.answer("Hai già contestato questa risposta!")
        return
    
    game['disputes'][dispute_key].append(disputer_id)
    await save_game_to_db(chat_id)
    
    disputer_name = game['players'].get(disputer_id, {}).get('name', 'Sconosciuto')
    target_name = game['players'].get(target_user_id, {}).get('name', 'Sconosciuto')
    answer = game['answers'].get(target_user_id, {}).get(category, '')
    
    disputes_count = len(game['disputes'][dispute_key])
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⚠️ *{disputer_name}* contesta la risposta di *{target_name}*!\n"
             f"📝 {category}: {answer}\n"
             f"Contestazioni: {disputes_count}/3",
        parse_mode='Markdown'
    )
    
    await query.answer(f"Contestazione registrata! ({disputes_count}/3)")


async def calculate_round_scores(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Calculate and apply round scores"""
    if chat_id not in games:
        return
    
    game = games[chat_id]
    
    scores_text = f"🏆 *PUNTEGGI ROUND {game['round']}*\n\n"
    
    for player_id, player_data in game['players'].items():
        round_score = 0
        answers = game['answers'].get(player_id, {})
        
        for category in game['categories']:
            answer = answers.get(category, '')
            dispute_key = f"{player_id}_{category}"
            disputes_count = len(game['disputes'].get(dispute_key, []))
            
            # Check if disputed (3+ disputes = invalid)
            if disputes_count >= 2:
                continue
            
            status, points = evaluate_answer(answer, game['current_letter'], category, player_id, game)
            
            if "❌" not in status:
                round_score += points
        
        game['players'][player_id]['score'] += round_score
        scores_text += f"• {player_data['name']}: +{round_score} punti (Totale: {game['players'][player_id]['score']})\n"
    
    await save_game_to_db(chat_id)
    
    # Check for winner
    winner = None
    for player_id, player_data in game['players'].items():
        if player_data['score'] >= game['target_score']:
            winner = player_data
            break
    
    if winner:
        game['state'] = GameState.FINISHED
        await save_game_to_db(chat_id)
        
        scores_text += f"\n\n🎉 *{winner['name']} HA VINTO!* 🎉\n"
        scores_text += f"Punteggio finale: {winner['score']} punti!"
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=scores_text,
            parse_mode='Markdown'
        )
    else:
        keyboard = [[InlineKeyboardButton("▶️ Prossimo Round", callback_data=f"nextround_{chat_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=scores_text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )


# ============= MESSAGE HANDLERS =============

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages"""
    if update.effective_chat.type == 'private':
        await handle_private_message(update, context)
    else:
        await handle_group_message(update, context)


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle private messages (answers)"""
    user_id = str(update.effective_user.id)
    text = update.message.text.strip()
    
    # Find active game for this player
    active_game_chat_id = None
    for chat_id, game in games.items():
        if user_id in game['players'] and game['state'] == GameState.PLAYING:
            active_game_chat_id = chat_id
            break
    
    if not active_game_chat_id:
        await update.message.reply_text(
            "ℹ️ Non sei in nessuna partita attiva.\n"
            "Unisciti a una partita in un gruppo con /partecipa!"
        )
        return
    
    key = f"{active_game_chat_id}_{user_id}"
    game = games[active_game_chat_id]
    
    # Initialize if needed
    if key not in player_answers:
        player_answers[key] = {
            'chat_id': active_game_chat_id,
            'categories': {cat: '' for cat in game['categories']},
            'current_category': 0
        }
    
    # Parse answer format "Categoria: Risposta"
    if ':' in text:
        parts = text.split(':', 1)
        category = parts[0].strip()
        answer = parts[1].strip()
        
        # Find matching category (case insensitive)
        matched_category = None
        for cat in game['categories']:
            if cat.lower() == category.lower():
                matched_category = cat
                break
        
        if matched_category:
            player_answers[key]['categories'][matched_category] = answer
            
            # Show updated answers
            categories_list = []
            for cat in game['categories']:
                ans = player_answers[key]['categories'].get(cat, '')
                status = "✅" if ans else "❓"
                categories_list.append(f"{status} *{cat}*: {ans or '_______'}")
            
            categories_text = "\n".join(categories_list)
            
            keyboard = [[InlineKeyboardButton("✅ Invia Risposte", callback_data=f"submit_{active_game_chat_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"✅ Registrato!\n\n"
                f"📌 Lettera: *{game['current_letter']}*\n\n"
                f"{categories_text}\n\n"
                f"Continua a scrivere o premi *Invia Risposte*",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                f"⚠️ Categoria '{category}' non trovata!\n\n"
                f"Categorie disponibili: {', '.join(game['categories'])}"
            )
    else:
        await update.message.reply_text(
            f"ℹ️ Usa il formato: `Categoria: Risposta`\n"
            f"Esempio: `Nomi: Marco`\n\n"
            f"Categorie: {', '.join(game['categories'])}",
            parse_mode='Markdown'
        )


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle group messages (custom categories)"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    if chat_id not in games:
        return
    
    game = games[chat_id]
    
    # Check if in category setup mode and user is creator
    if game['state'] == GameState.CATEGORIES_SETUP and game['creator_id'] == user_id:
        # Parse categories
        categories = [cat.strip() for cat in text.split(',') if cat.strip()]
        
        if categories:
            game['categories'] = categories
            await save_game_to_db(chat_id)
            
            await update.message.reply_text(
                f"✅ Categorie aggiornate!\n\n"
                f"Nuove categorie: {', '.join(categories)}\n\n"
                f"Usa il menu impostazioni per confermare."
           )


# ============= MAIN =============

def main():
    """Start the bot"""
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN not found in environment!")
        return
    
    # Create application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("nuova_partita", new_game))
    application.add_handler(CommandHandler("partecipa", join_game))
    application.add_handler(CommandHandler("inizia", start_game))
    application.add_handler(CommandHandler("stato", show_status))
    application.add_handler(CommandHandler("classifica", show_leaderboard))
    application.add_handler(CommandHandler("annulla", cancel_game))
    
    # Callback query handler
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Start polling
    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
[End of file]
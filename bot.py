import os
import uuid
import telebot
import time
import datetime
import sqlite3
from telebot import types
from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont
from docx2pdf import convert
from pdf2docx import Converter
from PyPDF2 import PdfReader, PdfWriter, PdfMerger
import sys
import cv2
import numpy as np
from dotenv import load_dotenv
import logging
import io
import qrcode
import zipfile
from rembg import remove

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    logger.error("TELEGRAM_TOKEN not found in .env file")
    sys.exit("Error: TELEGRAM_TOKEN not found. Please create a .env file.")

bot = telebot.TeleBot(TOKEN)

FONT_PATH = os.path.join(os.path.dirname(__file__), "QECarolineMutiboko.ttf")
OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

LINES_PER_PAGE = 25
FONT_SIZE = 20
PAGE_WIDTH, PAGE_HEIGHT = 595, 842
MARGIN = 50
LINE_HEIGHT = 30
MAX_LINE_WIDTH = PAGE_WIDTH - (2 * MARGIN)

user_context = {}
user_temp_files = {}
user_settings = {}
user_states = {}  # To track user states for screenshot editing
user_templates = {}  # To store custom templates uploaded by users

# Create a new directory for logs and database
LOGS_DIR = "logs"
DB_PATH = os.path.join(LOGS_DIR, "user_data.db")
os.makedirs(LOGS_DIR, exist_ok=True)

# Set your Telegram ID here for admin access
ADMIN_ID = int(os.getenv("ADMIN_ID", "5526206982"))

# Initialize database
def init_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Create users table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        chat_id INTEGER,
        language_code TEXT,
        first_seen TIMESTAMP,
        last_seen TIMESTAMP
    )
    ''')
    
    # Create actions table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action_type TEXT,
        details TEXT,
        file_name TEXT,
        timestamp TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')
    
    conn.commit()
    conn.close()

# Log user data
def log_user(message):
    user = message.from_user
    chat_id = message.chat.id
    now = datetime.datetime.now().isoformat()

    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user.id,))
        if cursor.fetchone():
            cursor.execute(
                "UPDATE users SET last_seen = ?, username = ? WHERE user_id = ?",
                (now, user.username, user.id)
            )
        else:
            cursor.execute(
                "INSERT INTO users (user_id, username, first_name, last_name, chat_id, language_code, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user.id, user.username, user.first_name, user.last_name, chat_id, user.language_code, now, now)
            )
        conn.commit()
    except sqlite3.IntegrityError as e:
        logger.error(f"[ERROR] Integrity error while logging user: {e}")
    finally:
        conn.close()

    return user.id


def log_action(user_id, action_type, details="", file_name=""):
    now = datetime.datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

    try:
        cursor.execute(
            "INSERT INTO actions (user_id, action_type, details, file_name, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user_id, action_type, details, file_name, now)
        )
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"[ERROR] Logging action failed: {e}")
    finally:
        conn.close()


# Initialize database on startup
init_database()

class HandwrittenPDF(FPDF):
    def header(self):
        self.set_font("Arial", size=12)
        self.cell(0, 10, '', 0, 1, 'C')

def get_text_width(text, font):
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0]

def split_text_to_fit_width(text, font, max_width):
    words = text.split()
    lines, current_line, current_width = [], [], 0
    for word in words:
        word_width = get_text_width(word + ' ', font)
        if current_width + word_width <= max_width:
            current_line.append(word)
            current_width += word_width
        else:
            lines.append(' '.join(current_line))
            current_line = [word]
            current_width = word_width
    if current_line:
        lines.append(' '.join(current_line))
    return lines

def create_handwritten_pdf(text, output_path):
    pdf = HandwrittenPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)

    original_lines = text.splitlines()
    processed_lines = []
    for line in original_lines:
        if line.strip():
            processed_lines.extend(split_text_to_fit_width(line, font, MAX_LINE_WIDTH))
        else:
            processed_lines.append('')

    pages = [processed_lines[i:i + LINES_PER_PAGE] for i in range(0, len(processed_lines), LINES_PER_PAGE)]

    for page_num, page_lines in enumerate(pages):
        img = Image.new('RGB', (PAGE_WIDTH, PAGE_HEIGHT), color='white')
        draw = ImageDraw.Draw(img)
        y = MARGIN
        for line in page_lines:
            draw.text((MARGIN, y), line, font=font, fill='black')
            y += LINE_HEIGHT
        image_path = os.path.join(OUTPUT_DIR, f"temp_page_{page_num}.jpg")
        img.save(image_path)
        pdf.add_page()
        pdf.image(image_path, x=0, y=0, w=210, h=297)
        os.remove(image_path)

    pdf.output(output_path)

    pdf.output(output_path)

def merge_pdfs(file_paths, output_path):
    try:
        # Verify all files exist
        for path in file_paths:
            if not os.path.exists(path):
                raise FileNotFoundError(f"File not found: {path}")
            if not path.lower().endswith('.pdf'):
                raise ValueError(f"Only PDF files can be merged: {path}")
        
        # Create the merger object
        merger = PdfMerger()
        
        # Add each PDF to the merger
        for path in file_paths:
            try:
                # Try to open the file to verify it's a valid PDF
                with open(path, 'rb') as f:
                    reader = PdfReader(f)
                    if len(reader.pages) > 0:
                        merger.append(path)
                    else:
                        raise ValueError(f"PDF has no pages: {path}")
            except Exception as e:
                raise ValueError(f"Error processing PDF {path}: {str(e)}")
        
        # Write the merged PDF to the output path
        with open(output_path, "wb") as f:
            merger.write(f)
        
        # Close the merger to free resources
        merger.close()
        
        return True
    except Exception as e:
        # Re-raise the exception with additional context
        raise Exception(f"Failed to merge PDFs: {str(e)}")

def generate_qr(text, output_path):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(text)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    img.save(output_path)

def read_qr(image_path):
    img = cv2.imread(image_path)
    detect = cv2.QRCodeDetector()
    value, points, straight_qrcode = detect.detectAndDecode(img)
    return value

def split_pdf_range(input_path, output_path, start_page, end_page):
    reader = PdfReader(input_path)
    writer = PdfWriter()
    
    # Validate range
    total_pages = len(reader.pages)
    if start_page < 1 or end_page > total_pages or start_page > end_page:
        raise ValueError(f"Invalid page range. PDF has {total_pages} pages.")
        
    for i in range(start_page - 1, end_page):
        writer.add_page(reader.pages[i])
        
    with open(output_path, "wb") as f:
        writer.write(f)

def split_pdf_every_x(input_path, output_dir, step):
    reader = PdfReader(input_path)
    total_pages = len(reader.pages)
    generated_files = []
    
    for i in range(0, total_pages, step):
        writer = PdfWriter()
        end = min(i + step, total_pages)
        for j in range(i, end):
            writer.add_page(reader.pages[j])
            
        output_filename = f"split_{i+1}-{end}.pdf"
        output_path = os.path.join(output_dir, output_filename)
        with open(output_path, "wb") as f:
            writer.write(f)
        generated_files.append(output_path)
        
    return generated_files

def organize_pdf(input_path, output_path, pages_list):
    reader = PdfReader(input_path)
    writer = PdfWriter()
    total_pages = len(reader.pages)
    
    for page_num in pages_list:
        # Adjust for 0-based index
        idx = page_num - 1
        if 0 <= idx < total_pages:
            writer.add_page(reader.pages[idx])
        else:
            # Skip invalid pages or raise error? Let's skip and log
            logger.warning(f"Skipping invalid page number: {page_num}")
            
    with open(output_path, "wb") as f:
        writer.write(f)

@bot.message_handler(commands=['admin'])
def admin_commands(message):
    if message.from_user.id == ADMIN_ID:
        help_text = """
🔐 Admin Commands:
/stats - View bot usage statistics
/export - Export user data to CSV
/admin - Show this help message
        """
        bot.reply_to(message, help_text)

@bot.message_handler(commands=['stats'])
def show_stats(message):
    if message.from_user.id == ADMIN_ID:
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Get user count
            cursor.execute("SELECT COUNT(*) FROM users")
            user_count = cursor.fetchone()[0]
            
            # Get action count
            cursor.execute("SELECT COUNT(*) FROM actions")
            action_count = cursor.fetchone()[0]
            
            # Get most recent users
            cursor.execute("SELECT username, first_name, last_name, last_seen FROM users ORDER BY last_seen DESC LIMIT 5")
            recent_users = cursor.fetchall()
            
            # Get most common actions
            cursor.execute("SELECT action_type, COUNT(*) as count FROM actions GROUP BY action_type ORDER BY count DESC LIMIT 5")
            common_actions = cursor.fetchall()
            
            conn.close()
            
            # Format message
            stats = f"📊 Bot Statistics:\n\n"
            stats += f"👥 Total Users: {user_count}\n"
            stats += f"🔄 Total Actions: {action_count}\n\n"
            
            stats += "📆 Recent Users:\n"
            for user in recent_users:
                username, first_name, last_name, last_seen = user
                name = username or f"{first_name} {last_name}"
                stats += f"- {name}: {last_seen[:16]}\n"
            
            stats += "\n🔝 Common Actions:\n"
            for action in common_actions:
                action_type, count = action
                stats += f"- {action_type}: {count}\n"
            
            bot.reply_to(message, stats)
        except Exception as e:
            bot.reply_to(message, f"Error fetching stats: {str(e)}")
    else:
        # Don't respond to unauthorized users
        pass

@bot.message_handler(commands=['export'])
def export_data(message):
    if message.from_user.id == ADMIN_ID:
        try:
            conn = sqlite3.connect(DB_PATH)
            
            # Export users to CSV
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users")
            users = cursor.fetchall()
            
            users_csv_path = os.path.join(LOGS_DIR, "users_export.csv")
            with open(users_csv_path, 'w', encoding='utf-8') as f:
                # Write header
                columns = [description[0] for description in cursor.description]
                f.write(','.join(columns) + '\n')
                
                # Write data
                for user in users:
                    f.write(','.join(str(item).replace(',', ';') for item in user) + '\n')
            
            # Export actions to CSV
            cursor.execute("SELECT * FROM actions")
            actions = cursor.fetchall()
            
            actions_csv_path = os.path.join(LOGS_DIR, "actions_export.csv")
            with open(actions_csv_path, 'w', encoding='utf-8') as f:
                # Write header
                columns = [description[0] for description in cursor.description]
                f.write(','.join(columns) + '\n')
                
                # Write data
                for action in actions:
                    f.write(','.join(str(item).replace(',', ';') for item in action) + '\n')
            
            conn.close()
            
            # Send files to the admin
            with open(users_csv_path, 'rb') as f:
                bot.send_document(message.chat.id, f, caption="📊 Users data export")
            
            with open(actions_csv_path, 'rb') as f:
                bot.send_document(message.chat.id, f, caption="📊 Actions data export")
                
            # Clean up
            os.remove(users_csv_path)
            os.remove(actions_csv_path)
            
        except Exception as e:
            bot.reply_to(message, f"Error exporting data: {str(e)}")
    else:
        # Don't respond to unauthorized users
        pass

@bot.message_handler(commands=['help'])
def send_help(message):
    user_id = log_user(message)
    log_action(user_id, "help", "User requested help")
    
    help_text = """
📋 Available commands:

/start - Start the bot and show main menu
/help - Show this help message
/admin - Admin commands (if you're an admin)

Select an option from the menu to get started!
"""
    bot.reply_to(message, help_text)

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = log_user(message)
    log_action(user_id, "start", "User started the bot")
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(
        types.KeyboardButton("✍️ Handwritten PDF"),
        types.KeyboardButton("📋 Main Menu")
    )
    bot.send_message(message.chat.id, "Welcome! Choose an option:", reply_markup=markup)
    show_main_menu(message.chat.id, "Or use the inline menu below:")

@bot.message_handler(func=lambda message: message.text == "✍️ Handwritten PDF")
def handle_handwritten_request(message):
    user_id = log_user(message)
    log_action(user_id, "handwritten_menu", "User selected handwritten PDF option")
    
    chat_id = message.chat.id
    user_context[chat_id] = 'handwritten'
    bot.send_message(chat_id, "📤 Send a `.txt` file to convert to handwritten PDF.")

@bot.message_handler(func=lambda message: message.text == "📋 Main Menu")
def handle_main_menu(message):
    user_id = log_user(message)
    log_action(user_id, "main_menu", "User returned to main menu")
    show_main_menu(message.chat.id, "📋 Main Menu:")



def show_main_menu(chat_id, text):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📝 Text to Handwritten PDF", callback_data='handwritten'),
        types.InlineKeyboardButton(" Word to PDF", callback_data='word_to_pdf'),
        types.InlineKeyboardButton("🔁 PDF to Word", callback_data='pdf_to_word'),
        types.InlineKeyboardButton(" JPG to PNG", callback_data='jpg_to_png'),
        types.InlineKeyboardButton("🖼 PNG to JPG", callback_data='png_to_jpg'),
        types.InlineKeyboardButton("📚 Merge PDFs", callback_data='merge_pdfs'),
        types.InlineKeyboardButton("✂️ Split PDF", callback_data='split_pdf_menu'),
        types.InlineKeyboardButton("📑 Organize PDF", callback_data='organize_pdf_menu'),
        types.InlineKeyboardButton("🖼️ Remove BG", callback_data='remove_bg'),
        types.InlineKeyboardButton("📱 QR Tools", callback_data='qr_menu')
    )
    bot.send_message(chat_id, text, reply_markup=markup)

@bot.message_handler(func=lambda message: user_context.get(message.chat.id) == 'generate_qr' and message.content_type == 'text')
def handle_qr_text(message):
    chat_id = message.chat.id
    text = message.text.strip()
    
    if not text:
        bot.reply_to(message, "❌ Please send some text.")
        return

    try:
        out_path = os.path.join(OUTPUT_DIR, f"qr_{uuid.uuid4()}.png")
        generate_qr(text, out_path)
        with open(out_path, 'rb') as f:
            bot.send_photo(chat_id, f, caption=f"📱 QR Code for: {text[:20]}...")
        os.remove(out_path)
        
        # Reset context
        if chat_id in user_context:
            del user_context[chat_id]
            
        # Show menu again
        show_main_menu(chat_id, "What would you like to do next?")
        
    except Exception as e:
        bot.reply_to(message, f"❌ Error generating QR: {str(e)}")
        logger.error(f"QR Generation Error: {e}")

@bot.message_handler(func=lambda message: user_context.get(message.chat.id) in ['split_range_input', 'split_every_x_input'])
def handle_split_input(message):
    chat_id = message.chat.id
    context = user_context.get(chat_id)
    text = message.text.strip()
    
    # Get the uploaded file path
    files = user_temp_files.get(chat_id, [])
    if not files:
        bot.reply_to(message, "❌ File not found. Please start over.")
        return
    file_path = files[0]
    
    try:
        if context == 'split_range_input':
            # Expect format like "1-5"
            parts = text.split('-')
            if len(parts) != 2:
                bot.reply_to(message, "❌ Invalid format. Please use 'Start-End' (e.g., 1-5).")
                return
            
            start = int(parts[0])
            end = int(parts[1])
            
            out_path = os.path.join(OUTPUT_DIR, f"split_{start}-{end}_{uuid.uuid4()}.pdf")
            split_pdf_range(file_path, out_path, start, end)
            
            with open(out_path, 'rb') as f:
                bot.send_document(chat_id, f, caption=f"✅ Split PDF (Pages {start}-{end})")
            
            os.remove(out_path)
            
        elif context == 'split_every_x_input':
            # Expect a number
            step = int(text)
            if step < 1:
                bot.reply_to(message, "❌ Please enter a number greater than 0.")
                return
                
            generated_files = split_pdf_every_x(file_path, OUTPUT_DIR, step)
            
            if len(generated_files) > 5:
                # Zip them if too many
                zip_path = os.path.join(OUTPUT_DIR, f"split_files_{uuid.uuid4()}.zip")
                with zipfile.ZipFile(zip_path, 'w') as zipf:
                    for f in generated_files:
                        zipf.write(f, os.path.basename(f))
                        
                with open(zip_path, 'rb') as f:
                    bot.send_document(chat_id, f, caption=f"✅ Split every {step} pages")
                os.remove(zip_path)
            else:
                for f_path in generated_files:
                    with open(f_path, 'rb') as f:
                        bot.send_document(chat_id, f)
            
            # Cleanup generated files
            for f in generated_files:
                if os.path.exists(f):
                    os.remove(f)
                    
        # Cleanup original file
        if os.path.exists(file_path):
            os.remove(file_path)
        user_temp_files[chat_id] = []
        del user_context[chat_id]
        
        show_main_menu(chat_id, "Done! What's next?")
        
    except ValueError as ve:
        bot.reply_to(message, f"❌ Invalid input: {str(ve)}")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")
        logger.error(f"Split error: {e}")

@bot.message_handler(func=lambda message: user_context.get(message.chat.id) in ['org_remove_input', 'org_reorder_input', 'org_extract_input'])
def handle_organize_input(message):
    chat_id = message.chat.id
    context = user_context.get(chat_id)
    text = message.text.strip()
    
    files = user_temp_files.get(chat_id, [])
    if not files:
        bot.reply_to(message, "❌ File not found. Please start over.")
        return
    file_path = files[0]
    
    try:
        # Parse input "1,2,3" or "1-3" or mixed "1, 3-5"
        # For simplicity, let's support comma separated for now, maybe ranges too if easy
        # Let's stick to comma separated integers as requested in plan for Remove/Reorder
        # But Extract might want ranges. Let's try to be flexible.
        
        page_nums = []
        parts = text.replace(' ', '').split(',')
        for part in parts:
            if '-' in part:
                start, end = map(int, part.split('-'))
                page_nums.extend(range(start, end + 1))
            else:
                page_nums.append(int(part))
        
        reader = PdfReader(file_path)
        total_pages = len(reader.pages)
        all_pages = list(range(1, total_pages + 1))
        
        final_pages = []
        
        if context == 'org_remove_input':
            # Remove specified pages
            final_pages = [p for p in all_pages if p not in page_nums]
            action_name = "Removed Pages"
            
        elif context == 'org_reorder_input':
            # Use specified order
            final_pages = page_nums
            action_name = "Reordered Pages"
            
        elif context == 'org_extract_input':
            # Keep only specified pages
            final_pages = page_nums
            action_name = "Extracted Pages"
            
        if not final_pages:
            bot.reply_to(message, "❌ Resulting PDF would be empty.")
            return

        out_path = os.path.join(OUTPUT_DIR, f"organized_{uuid.uuid4()}.pdf")
        organize_pdf(file_path, out_path, final_pages)
        
        with open(out_path, 'rb') as f:
            bot.send_document(chat_id, f, caption=f"✅ PDF Organized ({action_name})")
            
        os.remove(out_path)
        
        # Cleanup original
        if os.path.exists(file_path):
            os.remove(file_path)
        user_temp_files[chat_id] = []
        del user_context[chat_id]
        
        show_main_menu(chat_id, "Done! What's next?")
        
    except ValueError:
        bot.reply_to(message, "❌ Invalid format. Please use numbers separated by commas (e.g., '1,3,5').")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")
        logger.error(f"Organize error: {e}")

@bot.callback_query_handler(func=lambda call: True)
def handle_menu_selection(call):
    chat_id = call.message.chat.id
    user_id = log_user(call.message)
    log_action(user_id, "menu_selection", f"User selected menu option: {call.data}")
    
    if call.data == 'main_menu':
        return show_main_menu(chat_id, "📋 Main menu:")

    if call.data == 'qr_menu':
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("📤 Generate QR", callback_data='generate_qr'),
            types.InlineKeyboardButton("📥 Read QR", callback_data='read_qr'),
            types.InlineKeyboardButton("🔙 Back", callback_data='main_menu')
        )
        return bot.send_message(chat_id, "📱 QR Code Tools:", reply_markup=markup)
    elif call.data == 'generate_qr':
        user_context[chat_id] = 'generate_qr'
        msg = "✍️ Send the text or link you want to convert to a QR code."
    elif call.data == 'read_qr':
        user_context[chat_id] = 'read_qr'
        msg = "📸 Send an image containing a QR code."
    
    elif call.data == 'split_pdf_menu':
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("📄 Split by Range", callback_data='split_range'),
            types.InlineKeyboardButton("📑 Split Every X Pages", callback_data='split_every_x'),
            types.InlineKeyboardButton("🔙 Back", callback_data='main_menu')
        )
        return bot.send_message(chat_id, "✂️ How would you like to split the PDF?", reply_markup=markup)
    
    elif call.data == 'split_range':
        user_context[chat_id] = 'split_range'
        msg = "📤 Send the PDF file you want to split."
    
    elif call.data == 'split_every_x':
        user_context[chat_id] = 'split_every_x'
        msg = "📤 Send the PDF file you want to split."

    elif call.data == 'organize_pdf_menu':
        user_context[chat_id] = 'organize_pdf_start'
        msg = "📤 Send the PDF file you want to organize (remove/reorder/extract pages)."

    elif call.data == 'remove_bg':
        user_context[chat_id] = 'remove_bg'
        msg = "📤 Send an image to remove its background."
        
    elif call.data in ['org_remove', 'org_reorder', 'org_extract']:
        user_context[chat_id] = f"{call.data}_input"
        if call.data == 'org_remove':
            msg = "❌ Enter page numbers to REMOVE (e.g., '1,3,5')."
        elif call.data == 'org_reorder':
            msg = "🔄 Enter page numbers in the NEW ORDER (e.g., '3,1,2')."
        elif call.data == 'org_extract':
            msg = "📑 Enter page numbers to EXTRACT (e.g., '1,2,5')."
        
        bot.send_message(chat_id, msg)
        return

    user_context[chat_id] = call.data
    user_temp_files[chat_id] = []
    user_settings[chat_id] = user_settings.get(chat_id, {"watermark": False, "compress": False})

    if call.data in ['split_range', 'split_every_x']:
        msg = "📤 Send the PDF file you want to split."
    elif call.data == 'organize_pdf_menu':
        # This is handled above, but just in case
        msg = "📤 Send the PDF file you want to organize."
    elif call.data == 'remove_bg':
        msg = "📤 Send an image to remove its background."
    else:
        msg = "📤 Send the required file(s). You can send multiple files."

    if call.data == 'handwritten':
        msg = "📤 Send a `.txt` file."
    elif call.data == 'merge_pdfs':
        # Set a special context to indicate we're collecting PDFs
        user_context[chat_id] = 'merge_pdfs_collecting'
        msg = "📤 Send the FIRST PDF file you want to merge."
        logger.info(f"User {chat_id} started merge_pdfs operation")
    
    bot.send_message(chat_id, msg)

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📋 Menu", callback_data='main_menu'))
    bot.send_message(chat_id, "📋 Use the menu to switch tasks:", reply_markup=markup)

@bot.message_handler(content_types=['document'])
def handle_files(message):
    chat_id = message.chat.id
    user_id = log_user(message)
    
    # Initialize user context if not exists
    if chat_id not in user_context:
        bot.reply_to(message, "❌ Please select an option from the menu first.")
        return
        
    context = user_context.get(chat_id)
    settings = user_settings.get(chat_id, {})
    
    # Initialize temp files list if not exists
    if chat_id not in user_temp_files:
        user_temp_files[chat_id] = []
    
    try:
        file_info = bot.get_file(message.document.file_id)
        file_data = bot.download_file(file_info.file_path)
        ext = os.path.splitext(message.document.file_name)[-1].lower()
        file_path = os.path.join(OUTPUT_DIR, f"{uuid.uuid4()}{ext}")
        
        with open(file_path, 'wb') as f:
            f.write(file_data)

        user_temp_files[chat_id].append(file_path)

        # Log the file upload
        if message.document and message.document.file_name:
            log_action(user_id, f"file_upload_{context}", 
                      f"User uploaded file for {context}", 
                      message.document.file_name)

        if context == 'handwritten':
            if not file_path.endswith(".txt"):
                bot.reply_to(message, "❌ Please send a `.txt` file.")
                return
                
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    text = f.read()
                
                if not text.strip():
                    bot.reply_to(message, "❌ The text file is empty. Please send a file with content.")
                    return

                out_path = os.path.join(OUTPUT_DIR, 'handwritten.pdf')
                create_handwritten_pdf(text, out_path)
                with open(out_path, 'rb') as f:
                    bot.send_document(chat_id, f)
                # Cleanup
                os.remove(out_path)
            except Exception as e:
                bot.reply_to(message, f"❌ Error creating handwritten PDF: {str(e)}")

        elif context == 'merge_pdfs_collecting':
            # First, check if this is a PDF file
            if not file_path.lower().endswith('.pdf'):
                bot.reply_to(message, "❌ Only PDF files can be merged. Please send a PDF file.")
                if os.path.exists(file_path):
                    os.remove(file_path)
                if file_path in user_temp_files[chat_id]:
                    user_temp_files[chat_id].remove(file_path)
                return
            
            # Store the original file name for user display
            original_name = message.document.file_name if message.document else "Unknown PDF"
            
            # Count how many PDFs we have now
            num_files = len(user_temp_files[chat_id])
            
            # Log what we received
            logger.info(f"PDF {num_files} received: {original_name} -> {file_path}")
            
            # Now set the context to wait for the second file
            if num_files == 1:
                user_context[chat_id] = 'merge_pdfs_second'
                bot.reply_to(message, f"✅ First PDF received: {original_name}\n\nNow send the SECOND PDF file to merge with it.")
            else:
                # This shouldn't happen, but let's handle it just in case
                bot.reply_to(message, f"⚠️ Unexpected file. Please follow the step-by-step process.")
                
        elif context == 'merge_pdfs_second':
            # Check if this is a PDF file
            if not file_path.lower().endswith('.pdf'):
                bot.reply_to(message, "❌ Only PDF files can be merged. Please send a PDF file.")
                if os.path.exists(file_path):
                    os.remove(file_path)
                if file_path in user_temp_files[chat_id]:
                    user_temp_files[chat_id].remove(file_path)
                return
            
            # Store the original file name for user display
            original_name = message.document.file_name if message.document else "Unknown PDF"
            
            # We should now have exactly 2 PDF files
            if len(user_temp_files[chat_id]) != 2:
                bot.reply_to(message, "❌ Something went wrong with file tracking.")
                return
                
            # Get file names for both PDFs
            first_path = user_temp_files[chat_id][0]
            second_path = user_temp_files[chat_id][1]
            
            try:
                # Check if files exist
                if not os.path.exists(first_path) or not os.path.exists(second_path):
                    raise ValueError("One of the PDF files is missing.")
                
                # Create output path
                out_path = os.path.join(OUTPUT_DIR, f"merged_{uuid.uuid4()}.pdf")
                
                # Perform the merge
                logger.info(f"Merging: {first_path} + {second_path} -> {out_path}")
                merge_pdfs([first_path, second_path], out_path)
                
                # Send the result
                bot.reply_to(message, "⏳ Merging PDFs... Please wait.")
                
                with open(out_path, 'rb') as f:
                    bot.send_document(chat_id, f, caption="✅ PDFs merged successfully!")
                
                # Cleanup
                for path in [first_path, second_path, out_path]:
                    if os.path.exists(path):
                        os.remove(path)
            except Exception as e:
                bot.reply_to(message, f"❌ Error merging PDFs: {str(e)}")
                logger.error(f"Error during PDF merge: {str(e)}")
                
                # Clean up
                for path in user_temp_files[chat_id]:
                    if os.path.exists(path):
                        os.remove(path)
                user_temp_files[chat_id] = []

        elif context == 'merge_pdfs':
            # This is for backward compatibility with the old implementation
            bot.reply_to(message, "Please use the 'Merge PDFs' option from the main menu.")
            
            # Clear the files
            for temp_file in user_temp_files[chat_id]:
                try:
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                except Exception as e:
                    logger.error(f"Error removing temp file {temp_file}: {str(e)}")
            
            user_temp_files[chat_id] = []
            
            # Redirect to main menu
            show_main_menu(chat_id, "Please select an option:")



        elif context == 'generate_qr':
            # This block handles text messages for QR generation, but if they send a file with text?
            # Actually, text messages are handled in a different handler usually. 
            # But if they send a text file, we can read it.
            if file_path.endswith('.txt'):
                with open(file_path, 'r', encoding='utf-8') as f:
                    text = f.read()
                if not text.strip():
                    bot.reply_to(message, "❌ File is empty.")
                    return
                
                out_path = os.path.join(OUTPUT_DIR, f"qr_{uuid.uuid4()}.png")
                generate_qr(text, out_path)
                with open(out_path, 'rb') as f:
                    bot.send_photo(chat_id, f, caption=f"📱 QR Code for your text")
                os.remove(out_path)
            else:
                 bot.reply_to(message, "❌ Please send a text message or a .txt file for QR generation.")

        elif context == 'read_qr':
            # Check if it's an image
            if not (file_path.lower().endswith(('.png', '.jpg', '.jpeg'))):
                bot.reply_to(message, "❌ Please send an image file.")
                return
            
            try:
                data = read_qr(file_path)
                if data:
                    bot.reply_to(message, f"✅ QR Code Content:\n\n{data}")
                else:
                    bot.reply_to(message, "❌ No QR code detected in the image.")
            except Exception as e:
                bot.reply_to(message, f"❌ Error reading QR: {str(e)}")

        elif context in ['word_to_pdf', 'pdf_to_word', 'jpg_to_png', 'png_to_jpg']:
            try:
                out_path = file_path
                if context == 'word_to_pdf':
                    out_path = file_path.replace(".docx", ".pdf")
                    convert(file_path, out_path)
                elif context == 'pdf_to_word':
                    out_path = file_path.replace(".pdf", ".docx")
                    cv = Converter(file_path)
                    cv.convert(out_path, start=0, end=None)
                    cv.close()
                elif context == 'jpg_to_png':
                    img = Image.open(file_path)
                    out_path = file_path.replace(".jpg", ".png")
                    img.save(out_path, 'PNG')
                elif context == 'png_to_jpg':
                    img = Image.open(file_path)
                    out_path = file_path.replace(".png", ".jpg")
                    img.convert("RGB").save(out_path, 'JPEG')

                with open(out_path, 'rb') as f:
                    bot.send_document(chat_id, f)
                    
            except Exception as e:
                bot.reply_to(message, f"❌ Error processing file: {str(e)}")
            finally:
                # Cleanup temporary files
                try:
                    os.remove(file_path)
                    if out_path != file_path:
                        os.remove(out_path)
                except:
                    pass
                
    except Exception as e:
        bot.reply_to(message, f"❌ Error handling file: {str(e)}")
        logger.error(f"File handling error: {str(e)}")
    finally:
        # We only want to clean up files in certain contexts
        # For merge_pdfs_* contexts, we need to keep the files for the merging process
        if context not in ['merge_pdfs_collecting', 'merge_pdfs_second'] and chat_id in user_temp_files:
            # For other operations, clean up immediately
            for temp_file in user_temp_files[chat_id]:
                try:
                    if os.path.exists(temp_file) and temp_file != file_path:  # Don't delete the file we just added
                        os.remove(temp_file)
                except Exception as e:
                    logger.error(f"Error cleaning up file {temp_file}: {str(e)}")
            
            # Only clear the list for non-merge and non-split contexts (split needs file for next step)
            if not context.startswith('merge_pdfs') and not context.startswith('split_') and not context.startswith('organize_'):
                user_temp_files[chat_id] = []
        
        # Handle specific contexts after file upload
        if context == 'split_range':
            if file_path.lower().endswith('.pdf'):
                user_context[chat_id] = 'split_range_input'
                # Get page count
                try:
                    reader = PdfReader(file_path)
                    num_pages = len(reader.pages)
                    bot.reply_to(message, f"📄 PDF has {num_pages} pages.\n\nType the range you want to extract (e.g., '1-5').")
                except:
                    bot.reply_to(message, "📄 Received PDF. Type the range you want to extract (e.g., '1-5').")
            else:
                bot.reply_to(message, "❌ Please send a PDF file.")
                
        elif context == 'split_every_x':
            if file_path.lower().endswith('.pdf'):
                user_context[chat_id] = 'split_every_x_input'
                bot.reply_to(message, "📄 Received PDF. Enter the number of pages per split (e.g., '2' to split every 2 pages).")
            else:
                bot.reply_to(message, "❌ Please send a PDF file.")

        elif context == 'organize_pdf_start':
            if file_path.lower().endswith('.pdf'):
                # Show organize menu
                try:
                    reader = PdfReader(file_path)
                    num_pages = len(reader.pages)
                    
                    markup = types.InlineKeyboardMarkup(row_width=1)
                    markup.add(
                        types.InlineKeyboardButton("🗑️ Remove Pages", callback_data='org_remove'),
                        types.InlineKeyboardButton("🔄 Reorder Pages", callback_data='org_reorder'),
                        types.InlineKeyboardButton("📑 Extract Pages", callback_data='org_extract')
                    )
                    bot.reply_to(message, f"📄 PDF has {num_pages} pages.\nChoose an action:", reply_markup=markup)
                    
                except Exception as e:
                    bot.reply_to(message, f"❌ Error reading PDF: {str(e)}")
            else:
                bot.reply_to(message, "❌ Please send a PDF file.")

        elif context == 'remove_bg':
            if file_path.lower().endswith(('.png', '.jpg', '.jpeg')):
                try:
                    bot.reply_to(message, "⏳ Removing background... This may take a moment.")
                    
                    with open(file_path, 'rb') as i:
                        input_data = i.read()
                        output_data = remove(input_data)
                    
                    out_path = os.path.join(OUTPUT_DIR, f"no_bg_{uuid.uuid4()}.png")
                    with open(out_path, 'wb') as o:
                        o.write(output_data)
                        
                    with open(out_path, 'rb') as f:
                        bot.send_document(chat_id, f, caption="✅ Background removed!")
                        
                    os.remove(out_path)
                    os.remove(file_path)
                    user_temp_files[chat_id] = []
                    del user_context[chat_id]
                    show_main_menu(chat_id, "What's next?")
                    
                except Exception as e:
                    bot.reply_to(message, f"❌ Error removing background: {str(e)}")
            else:
                bot.reply_to(message, "❌ Please send an image file.")

# Start bot with error handling
while True:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        logger.error(f"Bot polling error: {e}")
        time.sleep(2)
        continue

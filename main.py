"""Business chat offer prototype. No payments, transfers or fake Telegram sale confirmations."""
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN', '').strip()
DB_PATH = Path('offers.sqlite3')
GIFT_RE = re.compile(r'^https://t\.me/nft/([A-Za-z][A-Za-z0-9]*-\d+)/?$', re.I)
BUY_RE = re.compile(r'^\.buy\s+(\S+)\s+(\d{1,9})(?:\s+(?:звезд|звёзд|stars|⭐))?\s*$', re.I)
TTL = 6 * 3600
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger('offers')


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS offers (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
          connection_id TEXT NOT NULL, buyer_id INTEGER NOT NULL,
          recipient_id INTEGER NOT NULL, slug TEXT NOT NULL, url TEXT NOT NULL,
          stars INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          created_at INTEGER NOT NULL, UNIQUE(chat_id,message_id))''')


def keyboard(offer_id, status, buyer_id):
    if status == 'pending':
        return InlineKeyboardMarkup([[
            InlineKeyboardButton('❌ Отклонить', callback_data=f'offer:{offer_id}:decline'),
            InlineKeyboardButton('✅ Принять', callback_data=f'offer:{offer_id}:accept')
        ]])
    if status == 'accepted':
        return InlineKeyboardMarkup([
            [InlineKeyboardButton('👤 Профиль покупателя', url=f'tg://user?id={buyer_id}')],
            [InlineKeyboardButton('ℹ️ Как передать?', callback_data=f'offer:{offer_id}:help')]
        ])
    return None


def offer_text(row):
    name, num = row['slug'].rsplit('-', 1)
    pretty = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
    if row['status'] == 'pending':
        return (f'🎁 Предложение о покупке подарка\n\n'
                f'{pretty} #{num}\n\n'
                f'Покупатель предлагает {row["stars"]:,} ⭐ за подарок.\n'
                f'Срок действия: 6 часов.\n\n'
                f'Подарок: {row["url"]}\n\n'
                'Это частное предложение, не подтверждённая продажа Telegram.')
    if row['status'] == 'accepted':
        return (f'✅ Предложение принято\n\n{pretty} #{num}\n'
                f'Предложенная цена: {row["stars"]:,} ⭐\n\n'
                'Оплата не подтверждена. Не передавайте подарок до получения оплаты.\n'
                f'Подарок: {row["url"]}')
    if row['status'] == 'declined':
        return f'❌ Предложение отклонено\n\n{pretty} #{num}\n{row["url"]}'
    return f'⌛ Срок предложения истёк\n\n{pretty} #{num}\n{row["url"]}'


async def business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.business_message
    if not msg or not msg.text or not msg.business_connection_id:
        return
    if not msg.text.strip().startswith('.buy'):
        return
    match = BUY_RE.fullmatch(msg.text.strip())
    if not match:
        await context.bot.send_message(msg.chat_id, 'Формат: .buy https://t.me/nft/RestlessJar-5940 2500', business_connection_id=msg.business_connection_id)
        return
    url, stars_str = match.groups()
    gift = GIFT_RE.fullmatch(url)
    if not gift:
        await context.bot.send_message(msg.chat_id, 'Нужна ссылка формата https://t.me/nft/RestlessJar-5940', business_connection_id=msg.business_connection_id)
        return
    stars = int(stars_str)
    if stars < 1:
        return
    try:
        connection = await context.bot.get_business_connection(msg.business_connection_id)
        buyer_id = connection.user.id
    except TelegramError:
        log.exception('Could not resolve business account')
        return
    # Only commands sent by the business account owner are accepted.
    if not msg.from_user or msg.from_user.id != buyer_id:
        return
    if not msg.chat or msg.chat.type != 'private' or msg.chat.id == buyer_id:
        return
    now = int(time.time())
    with connect() as db:
        cur = db.execute('''INSERT OR IGNORE INTO offers
            (chat_id,message_id,connection_id,buyer_id,recipient_id,slug,url,stars,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (msg.chat_id,msg.message_id,msg.business_connection_id,buyer_id,msg.chat.id,gift.group(1),url,stars,now))
        row = db.execute('SELECT * FROM offers WHERE chat_id=? AND message_id=?', (msg.chat_id,msg.message_id)).fetchone()
    if not row:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=msg.chat_id, message_id=msg.message_id,
            business_connection_id=msg.business_connection_id,
            text=offer_text(row), reply_markup=keyboard(row['id'],row['status'],buyer_id),
            link_preview_options=LinkPreviewOptions(is_disabled=False, url=url, prefer_large_media=True))
    except TelegramError as e:
        log.warning('Edit failed; trying a new message: %s', e)
        try:
            sent = await context.bot.send_message(
                chat_id=msg.chat_id, business_connection_id=msg.business_connection_id,
                text=offer_text(row), reply_markup=keyboard(row['id'],row['status'],buyer_id),
                link_preview_options=LinkPreviewOptions(is_disabled=False, url=url, prefer_large_media=True))
            with connect() as db:
                db.execute('UPDATE offers SET message_id=? WHERE id=?', (sent.message_id,row['id']))
        except TelegramError:
            log.exception('Sending offer failed')


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    match = re.fullmatch(r'offer:(\d+):(accept|decline|help)', q.data)
    if not match:
        return
    offer_id, action = int(match.group(1)), match.group(2)
    with connect() as db:
        row = db.execute('SELECT * FROM offers WHERE id=?', (offer_id,)).fetchone()
    if not row:
        await q.answer('Предложение не найдено.', show_alert=True)
        return
    if q.from_user.id != row['recipient_id']:
        await q.answer('Это предложение адресовано другому пользователю.', show_alert=True)
        return
    if action == 'help':
        await q.answer('Откройте профиль покупателя. Передавайте подарок только после подтверждения оплаты.', show_alert=True)
        return
    if row['status'] != 'pending':
        await q.answer('Предложение уже обработано.', show_alert=True)
        return
    status = 'expired' if time.time() - row['created_at'] >= TTL else ('accepted' if action == 'accept' else 'declined')
    with connect() as db:
        changed = db.execute('UPDATE offers SET status=? WHERE id=? AND status=?', (status,offer_id,'pending')).rowcount
        row = db.execute('SELECT * FROM offers WHERE id=?', (offer_id,)).fetchone()
    if not changed:
        await q.answer('Предложение уже обработано.', show_alert=True)
        return
    try:
        await context.bot.edit_message_text(
            chat_id=row['chat_id'], message_id=row['message_id'],
            business_connection_id=row['connection_id'], text=offer_text(row),
            reply_markup=keyboard(row['id'],row['status'],row['buyer_id']),
            link_preview_options=LinkPreviewOptions(is_disabled=False, url=row['url'], prefer_large_media=True))
    except TelegramError:
        log.exception('Could not edit offer')
    await q.answer('Предложение принято. Оплата ещё не подтверждена.' if status == 'accepted' else ('Предложение отклонено.' if status == 'declined' else 'Срок предложения истёк.'), show_alert=True)


def main():
    if not TOKEN or TOKEN == 'PUT_NEW_TOKEN_HERE':
        raise SystemExit('Set BOT_TOKEN environment variable with a NEW token from BotFather')
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE & filters.TEXT, business_message))
    app.add_handler(CallbackQueryHandler(callback, pattern=r'^offer:\d+:(accept|decline|help)$'))
    log.info('Business offers bot started')
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()

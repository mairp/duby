uv run python -c "
from telethon.sync import TelegramClient
import os
from dotenv import load_dotenv
load_dotenv()
client = TelegramClient('telegram_session', int(os.environ['TELEGRAM_API_ID']), 
os.environ['TELEGRAM_API_HASH'])
client.start()
for dialog in client.iter_dialogs():
  if dialog.is_channel and 'ALERTAS' in dialog.name.upper():
    print(f'{dialog.name} -> {dialog.id}')
"

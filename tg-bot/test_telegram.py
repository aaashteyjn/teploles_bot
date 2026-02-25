import os
from dotenv import load_dotenv
import httpx

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

url = f"https://api.telegram.org/bot{TOKEN}/getMe"

try:
    r = httpx.get(url, timeout=10)
    print("STATUS:", r.status_code)
    print("BODY:", r.text)
except Exception as e:
    print("ERROR:", repr(e))
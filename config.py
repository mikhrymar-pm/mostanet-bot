import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))

KNOWN_PORTS = [
    "Корсаков",
    "Курильск",
    "Южно-Курильск",
    "Малокурильское",
]

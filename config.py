import os

from dotenv import load_dotenv

load_dotenv()

# Entry criteria
MIN_FUNDING_RATE = 0.015
MAX_MARK_ORACLE_GAP = 2.0
MIN_OPEN_INTEREST = 50000

# Execution
AUTO_EXECUTE = False
MAX_POSITION_SIZE_USD = 200
LEVERAGE = 2

# Exit criteria
EXIT_FUNDING_THRESHOLD = 0.01
MAX_HOLD_HOURS = 48

# Polling
POLL_INTERVAL_SECONDS = 60

# Secrets — loaded from .env, never set these here
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

import os
import sys
import logging
from logging.handlers import RotatingFileHandler

# Ensure the bot directory is in Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "bot")))

# Setup logging
log_dir = os.path.join(os.path.dirname(__file__), "logs")
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(
            os.path.join(log_dir, "bot.log"),
            maxBytes=10000000,  # 10MB
            backupCount=5
        ),
        logging.StreamHandler()
    ]
)

from main import main

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"Bot crashed: {str(e)}", exc_info=True)
        sys.exit(1)

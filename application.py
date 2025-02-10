import os
import sys

# Ensure the bot directory is in Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "bot")))

from main import main  # Import the function that starts your bot

if __name__ == "__main__":
    main()

#!/bin/bash
set -euo pipefail

# Change to the bot directory (adjust path if needed)
cd /home/ubuntu/gervais_bot_telegram

# Pull latest code
git pull origin ec2-deployment

# Reinstall any new packages
source /home/ubuntu/gervais_bot_telegram/env/bin/activate
pip install -r requirements.txt

# Restart the service
sudo systemctl restart telegram-bot

# Show status
sudo systemctl status telegram-bot

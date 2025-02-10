#!/bin/bash

# Update system
sudo apt-get update
sudo apt-get upgrade -y

# Install Python and dependencies
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg

# Create directory for the bot
mkdir -p /home/ubuntu/gervais_bot_telegram

# Create virtual environment
python3 -m venv /home/ubuntu/gervais_bot_telegram/env

# Install requirements
source /home/ubuntu/gervais_bot_telegram/env/bin/activate
pip install -r /home/ubuntu/gervais_bot_telegram/requirements.txt


# Setup systemd service
sudo cp telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot

# Create logs directory
mkdir -p /home/ubuntu/gervais_bot_telegram/logs
touch /home/ubuntu/gervais_bot_telegram/logs/bot.log

# Set proper permissions
sudo chown -R ubuntu:ubuntu /home/ubuntu/gervais_bot_telegram 

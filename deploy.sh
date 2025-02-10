#!/bin/bash

# Stop the service
sudo systemctl stop telegram-bot

# Backup the .env file
cp .env .env.backup

# Pull latest changes
git pull origin main

# Restore the .env file
cp .env.backup .env

# Install any new requirements
source /home/ubuntu/gervais_bot/venv/bin/activate
pip install -r requirements.txt

# Start the service
sudo systemctl start telegram-bot

# Check the status
sudo systemctl status telegram-bot 
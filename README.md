# Gervais - Cocoa Disease Diagnostics Bot

![python-version](https://img.shields.io/badge/python-3.9-blue.svg)
[![openai-version](https://img.shields.io/badge/openai-1.58.1-orange.svg)](https://openai.com/)
[![license](https://img.shields.io/badge/License-GPL%202.0-brightgreen.svg)](LICENSE)
[![Publish Docker image](https://github.com/your-repo/gervais/actions/workflows/publish.yaml/badge.svg)](https://github.com/your-repo/gervais/actions/workflows/publish.yaml)

Gervais is a **Telegram bot** that leverages OpenAI's **ChatGPT, DALL·E, and Whisper APIs** to help farmers and agricultural experts diagnose cocoa diseases. The bot can process text, images, and voice messages to provide real-time insights on cocoa plant health and suggest treatment options.

## Features

- 🌱 **Cocoa Disease Diagnosis**: Upload images of diseased cocoa plants for AI-powered analysis.
- 🔬 **Text-Based Consultation**: Ask questions about cocoa diseases, symptoms, and treatments.
- 🎙 **Voice-to-Text Support**: Send voice messages, and Gervais will transcribe and analyze them.
- 📝 **Markdown Support**: Responses are formatted for better readability.
- 🔄 **Reset Conversation**: Use `/reset` to clear the chat history.
- 📊 **Track Usage**: Monitor API usage and costs per user.
- 🌍 **Multilingual Support**: Communicate in multiple languages.
- 🛡 **Access Control**: Restrict usage to specific users or open access to all.
- 🖼 **Image Analysis with DALL·E**: Generate visual explanations.
- 🔊 **Text-to-Speech (TTS)**: Convert text-based responses to audio output.
- 🌎 **Offline and Proxy Support**: Works behind proxies and within low-connectivity environments.

## Getting Started

### Prerequisites

- **Python 3.9+**
- A [Telegram bot](https://core.telegram.org/bots#6-botfather) with a valid token
- An [OpenAI API key](https://platform.openai.com/account/api-keys)

### Installation

Clone the repository and navigate to the project directory:

```sh
git clone https://github.com/your-repo/gervais.git
cd gervais
```

#### From Source

1. Create a virtual environment:

```sh
python -m venv venv
```

2. Activate the virtual environment:

```sh
# For Linux/macOS:
source venv/bin/activate

# For Windows:
venv\Scripts\activate
```

3. Install dependencies:

```sh
pip install -r requirements.txt
```

4. Copy the `.env.example` file and rename it to `.env`, then configure the necessary parameters.

5. Start the bot:

```sh
python bot/main.py
```

#### Using Docker

Build and run the bot using Docker:

```sh
docker compose up
```

Or pull the latest pre-built image:

```sh
docker pull your-repo/gervais:latest
docker run -it --env-file .env your-repo/gervais
```

## Configuration

Edit the `.env` file to set up the necessary credentials and configurations:

```ini
OPENAI_API_KEY=your_openai_api_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
ALLOWED_TELEGRAM_USER_IDS=*
ADMIN_USER_IDS=your_admin_user_id
ENABLE_IMAGE_ANALYSIS=true
ENABLE_TRANSCRIPTION=true
ENABLE_TEXT_TO_SPEECH=true
```

## Commands

| Command  | Description                                   |
| -------- | --------------------------------------------- |
| `/start` | Start a conversation with the bot             |
| `/reset` | Clear chat history                            |
| `/image` | Upload an image of a cocoa plant for analysis |
| `/stats` | View personal API usage statistics            |
| `/tts`   | Convert text to speech                        |

## Contributions

Contributions are welcome! Check out the [issues](https://github.com/your-repo/gervais/issues) section for open tasks.

## License

This project is licensed under **GPL 2.0**. See the [LICENSE](LICENSE) file for more details.

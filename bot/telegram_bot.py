from __future__ import annotations

import asyncio
import logging
import os
import io
import datetime
import json

from uuid import uuid4
from telegram import (
    BotCommandScopeAllGroupChats,
    Update,
    constants,
    Location,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQueryResultArticle,
)
from telegram import InputTextMessageContent, BotCommand
from telegram.error import RetryAfter, TimedOut, BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    InlineQueryHandler,
    CallbackQueryHandler,
    Application,
    ContextTypes,
    CallbackContext,
)

from pydub import AudioSegment
from PIL import Image, ExifTags
import piexif
from io import BytesIO

from utils import (
    is_group_chat,
    get_thread_id,
    message_text,
    wrap_with_indicator,
    split_into_chunks,
    edit_message_with_retry,
    get_stream_cutoff_values,
    is_allowed,
    get_remaining_budget,
    is_admin,
    is_within_budget,
    get_reply_to_message_id,
    add_chat_request_to_usage_tracker,
    error_handler,
    is_direct_result,
    handle_direct_result,
    cleanup_intermediate_files,
)
from openai_helper import OpenAIHelper, localized_text
from usage_tracker import UsageTracker
from s3_helper import S3Helper


class ChatGPTTelegramBot:
    """
    Class representing a ChatGPT Telegram Bot.
    """

    def __init__(self, config: dict, openai: OpenAIHelper):
        """
        Initializes the bot with the given configuration and GPT bot object.
        :param config: A dictionary containing the bot configuration
        :param openai: OpenAIHelper object
        """
        self.config = config
        self.openai = openai
        # Initialize S3 helper
        self.s3_helper = S3Helper(os.getenv("S3_BUCKET_NAME"))
        bot_language = self.config["bot_language"]
        self.commands = [
            BotCommand(
                command="help",
                description=localized_text("help_description", bot_language),
            ),
            BotCommand(
                command="reset",
                description=localized_text("reset_description", bot_language),
            ),
            BotCommand(
                command="stats",
                description=localized_text("stats_description", bot_language),
            ),
            BotCommand(
                command="resend",
                description=localized_text("resend_description", bot_language),
            ),
        ]
        if self.config.get("enable_tts_generation", False):
            self.commands.append(
                BotCommand(
                    command="tts",
                    description=localized_text("tts_description", bot_language),
                )
            )

        self.group_commands = [
            BotCommand(
                command="chat",
                description=localized_text("chat_description", bot_language),
            )
        ] + self.commands
        self.disallowed_message = localized_text("disallowed", bot_language)
        self.budget_limit_message = localized_text("budget_limit", bot_language)
        self.usage = {}
        self.last_message = {}
        self.inline_queries_cache = {}

        # Add user profile tracking
        self.user_profiles = {}  # In-memory cache of user profiles
        self.profile_collection_state = {}  # Track the state of profile collection

        # Add profile command
        self.commands.append(
            BotCommand(
                command="profile",
                description="Update your profile information",
            )
        )

    async def help(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Shows the help menu.
        """
        commands = self.group_commands if is_group_chat(update) else self.commands
        commands_description = [
            f"/{command.command} - {command.description}" for command in commands
        ]
        bot_language = self.config["bot_language"]
        help_text = (
            localized_text("help_text", bot_language)[0]
            + "\n\n"
            + "\n".join(commands_description)
            + "\n\n"
            + localized_text("help_text", bot_language)[1]
            + "\n\n"
            + localized_text("help_text", bot_language)[2]
        )
        await update.message.reply_text(help_text, disable_web_page_preview=True)

        # After sending help, check if user has a profile
        user_id = update.message.from_user.id
        logging.info(f"Checking profile for user {user_id} after help command")
        await self.check_user_profile(update, user_id)

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Returns token usage statistics for current day and month.
        """
        if not await is_allowed(self.config, update, context):
            logging.warning(
                f"User {update.message.from_user.name} (id: {update.message.from_user.id}) "
                "is not allowed to request their usage statistics"
            )
            await self.send_disallowed_message(update, context)
            return

        logging.info(
            f"User {update.message.from_user.name} (id: {update.message.from_user.id}) "
            "requested their usage statistics"
        )

        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

        tokens_today, tokens_month = self.usage[user_id].get_current_token_usage()
        images_today, images_month = self.usage[user_id].get_current_image_count()
        (
            transcribe_minutes_today,
            transcribe_seconds_today,
            transcribe_minutes_month,
            transcribe_seconds_month,
        ) = self.usage[user_id].get_current_transcription_duration()
        vision_today, vision_month = self.usage[user_id].get_current_vision_tokens()
        characters_today, characters_month = self.usage[user_id].get_current_tts_usage()
        current_cost = self.usage[user_id].get_current_cost()

        chat_id = update.effective_chat.id
        chat_messages, chat_token_length = self.openai.get_conversation_stats(chat_id)
        remaining_budget = get_remaining_budget(self.config, self.usage, update)
        bot_language = self.config["bot_language"]

        text_current_conversation = (
            f"*{localized_text('stats_conversation', bot_language)[0]}*:\n"
            f"{chat_messages} {localized_text('stats_conversation', bot_language)[1]}\n"
            f"{chat_token_length} {localized_text('stats_conversation', bot_language)[2]}\n"
            "----------------------------\n"
        )

        # Check if image generation is enabled and, if so, generate the image statistics for today
        text_today_images = ""
        if self.config.get("enable_image_generation", False):
            text_today_images = (
                f"{images_today} {localized_text('stats_images', bot_language)}\n"
            )

        text_today_vision = ""
        if self.config.get("enable_vision", False):
            text_today_vision = (
                f"{vision_today} {localized_text('stats_vision', bot_language)}\n"
            )

        text_today_tts = ""
        if self.config.get("enable_tts_generation", False):
            text_today_tts = (
                f"{characters_today} {localized_text('stats_tts', bot_language)}\n"
            )

        text_today = (
            f"*{localized_text('usage_today', bot_language)}:*\n"
            f"{tokens_today} {localized_text('stats_tokens', bot_language)}\n"
            f"{text_today_images}"  # Include the image statistics for today if applicable
            f"{text_today_vision}"
            f"{text_today_tts}"
            f"{transcribe_minutes_today} {localized_text('stats_transcribe', bot_language)[0]} "
            f"{transcribe_seconds_today} {localized_text('stats_transcribe', bot_language)[1]}\n"
            f"{localized_text('stats_total', bot_language)}{current_cost['cost_today']:.2f}\n"
            "----------------------------\n"
        )

        text_month_images = ""
        if self.config.get("enable_image_generation", False):
            text_month_images = (
                f"{images_month} {localized_text('stats_images', bot_language)}\n"
            )

        text_month_vision = ""
        if self.config.get("enable_vision", False):
            text_month_vision = (
                f"{vision_month} {localized_text('stats_vision', bot_language)}\n"
            )

        text_month_tts = ""
        if self.config.get("enable_tts_generation", False):
            text_month_tts = (
                f"{characters_month} {localized_text('stats_tts', bot_language)}\n"
            )

        # Check if image generation is enabled and, if so, generate the image statistics for the month
        text_month = (
            f"*{localized_text('usage_month', bot_language)}:*\n"
            f"{tokens_month} {localized_text('stats_tokens', bot_language)}\n"
            f"{text_month_images}"  # Include the image statistics for the month if applicable
            f"{text_month_vision}"
            f"{text_month_tts}"
            f"{transcribe_minutes_month} {localized_text('stats_transcribe', bot_language)[0]} "
            f"{transcribe_seconds_month} {localized_text('stats_transcribe', bot_language)[1]}\n"
            f"{localized_text('stats_total', bot_language)}{current_cost['cost_month']:.2f}"
        )

        # text_budget filled with conditional content
        text_budget = "\n\n"
        budget_period = self.config["budget_period"]
        if remaining_budget < float("inf"):
            text_budget += (
                f"{localized_text('stats_budget', bot_language)}"
                f"{localized_text(budget_period, bot_language)}: "
                f"${remaining_budget:.2f}.\n"
            )
        # No longer works as of July 21st 2023, as OpenAI has removed the billing API
        # add OpenAI account information for admin request
        # if is_admin(self.config, user_id):
        #     text_budget += (
        #         f"{localized_text('stats_openai', bot_language)}"
        #         f"{self.openai.get_billing_current_month():.2f}"
        #     )

        usage_text = text_current_conversation + text_today + text_month + text_budget
        await update.message.reply_text(
            usage_text, parse_mode=constants.ParseMode.MARKDOWN
        )

    async def resend(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Resend the last request
        """
        if not await is_allowed(self.config, update, context):
            logging.warning(
                f"User {update.message.from_user.name}  (id: {update.message.from_user.id})"
                " is not allowed to resend the message"
            )
            await self.send_disallowed_message(update, context)
            return

        chat_id = update.effective_chat.id
        if chat_id not in self.last_message:
            logging.warning(
                f"User {update.message.from_user.name} (id: {update.message.from_user.id})"
                " does not have anything to resend"
            )
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text("resend_failed", self.config["bot_language"]),
            )
            return

        # Update message text, clear self.last_message and send the request to prompt
        logging.info(
            f"Resending the last prompt from user: {update.message.from_user.name} "
            f"(id: {update.message.from_user.id})"
        )
        with update.message._unfrozen() as message:
            message.text = self.last_message.pop(chat_id)

        await self.prompt(update=update, context=context)

    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Resets the conversation.
        """
        if not await is_allowed(self.config, update, context):
            logging.warning(
                f"User {update.message.from_user.name} (id: {update.message.from_user.id}) "
                "is not allowed to reset the conversation"
            )
            await self.send_disallowed_message(update, context)
            return

        logging.info(
            f"Resetting the conversation for user {update.message.from_user.name} "
            f"(id: {update.message.from_user.id})..."
        )

        chat_id = update.effective_chat.id
        reset_content = message_text(update.message)
        self.openai.reset_chat_history(chat_id=chat_id, content=reset_content)
        await update.effective_message.reply_text(
            message_thread_id=get_thread_id(update),
            text=localized_text("reset_done", self.config["bot_language"]),
        )

    async def image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Generates an image for the given prompt using DALL·E APIs
        """
        if not self.config[
            "enable_image_generation"
        ] or not await self.check_allowed_and_within_budget(update, context):
            return

        image_query = message_text(update.message)
        if image_query == "":
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text("image_no_prompt", self.config["bot_language"]),
            )
            return

        logging.info(
            f"New image generation request received from user {update.message.from_user.name} "
            f"(id: {update.message.from_user.id})"
        )

        async def _generate():
            try:
                image_url, image_size = await self.openai.generate_image(
                    prompt=image_query
                )
                if self.config["image_receive_mode"] == "photo":
                    await update.effective_message.reply_photo(
                        reply_to_message_id=get_reply_to_message_id(
                            self.config, update
                        ),
                        photo=image_url,
                    )
                elif self.config["image_receive_mode"] == "document":
                    await update.effective_message.reply_document(
                        reply_to_message_id=get_reply_to_message_id(
                            self.config, update
                        ),
                        document=image_url,
                    )
                else:
                    raise Exception(
                        f"env variable IMAGE_RECEIVE_MODE has invalid value {self.config['image_receive_mode']}"
                    )
                # add image request to users usage tracker
                user_id = update.message.from_user.id
                self.usage[user_id].add_image_request(
                    image_size, self.config["image_prices"]
                )
                # add guest chat request to guest usage tracker
                if (
                    str(user_id) not in self.config["allowed_user_ids"].split(",")
                    and "guests" in self.usage
                ):
                    self.usage["guests"].add_image_request(
                        image_size, self.config["image_prices"]
                    )

            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=f"{localized_text('image_fail', self.config['bot_language'])}: {str(e)}",
                    parse_mode=constants.ParseMode.MARKDOWN,
                )

        await wrap_with_indicator(
            update, context, _generate, constants.ChatAction.UPLOAD_PHOTO
        )

    async def tts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Generates an speech for the given input using TTS APIs
        """
        if not self.config[
            "enable_tts_generation"
        ] or not await self.check_allowed_and_within_budget(update, context):
            return

        tts_query = message_text(update.message)
        if tts_query == "":
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text("tts_no_prompt", self.config["bot_language"]),
            )
            return

        logging.info(
            f"New speech generation request received from user {update.message.from_user.name} "
            f"(id: {update.message.from_user.id})"
        )

        async def _generate():
            try:
                speech_file, text_length = await self.openai.generate_speech(
                    text=tts_query
                )

                await update.effective_message.reply_voice(
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    voice=speech_file,
                )
                speech_file.close()
                # add image request to users usage tracker
                user_id = update.message.from_user.id
                self.usage[user_id].add_tts_request(
                    text_length, self.config["tts_model"], self.config["tts_prices"]
                )
                # add guest chat request to guest usage tracker
                if (
                    str(user_id) not in self.config["allowed_user_ids"].split(",")
                    and "guests" in self.usage
                ):
                    self.usage["guests"].add_tts_request(
                        text_length, self.config["tts_model"], self.config["tts_prices"]
                    )

            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=f"{localized_text('tts_fail', self.config['bot_language'])}: {str(e)}",
                    parse_mode=constants.ParseMode.MARKDOWN,
                )

        await wrap_with_indicator(
            update, context, _generate, constants.ChatAction.UPLOAD_VOICE
        )

    async def transcribe(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Transcribe audio messages and respond with audio.
        """
        if not self.config[
            "enable_transcription"
        ] or not await self.check_allowed_and_within_budget(update, context):
            return

        if is_group_chat(update) and self.config["ignore_group_transcriptions"]:
            logging.info("Transcription coming from group chat, ignoring...")
            return

        chat_id = update.effective_chat.id
        filename = update.message.effective_attachment.file_unique_id

        async def _execute():
            filename_mp3 = f"{filename}.mp3"
            bot_language = self.config["bot_language"]
            try:
                media_file = await context.bot.get_file(
                    update.message.effective_attachment.file_id
                )
                await media_file.download_to_drive(filename)
            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=(
                        f"{localized_text('media_download_fail', bot_language)[0]}: "
                        f"{str(e)}. {localized_text('media_download_fail', bot_language)[1]}"
                    ),
                    parse_mode=constants.ParseMode.MARKDOWN,
                )
                return

            try:
                audio_track = AudioSegment.from_file(filename)
                audio_track.export(filename_mp3, format="mp3")
                logging.info(
                    f"New transcribe request received from user {update.message.from_user.name} "
                    f"(id: {update.message.from_user.id})"
                )

            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=localized_text("media_type_fail", bot_language),
                )
                if os.path.exists(filename):
                    os.remove(filename)
                return

            user_id = update.message.from_user.id
            if user_id not in self.usage:
                self.usage[user_id] = UsageTracker(
                    user_id, update.message.from_user.name
                )

            try:
                transcript = await self.openai.transcribe(filename_mp3)

                transcription_price = self.config["transcription_price"]
                self.usage[user_id].add_transcription_seconds(
                    audio_track.duration_seconds, transcription_price
                )

                allowed_user_ids = self.config["allowed_user_ids"].split(",")
                if str(user_id) not in allowed_user_ids and "guests" in self.usage:
                    self.usage["guests"].add_transcription_seconds(
                        audio_track.duration_seconds, transcription_price
                    )

                # Get GPT's response to the transcript
                response, total_tokens = await self.openai.get_chat_response(
                    chat_id=chat_id, query=transcript
                )

                # Get message data with timestamps
                message_data = self.openai.get_last_message_data(chat_id)

                # Add user info
                message_data.update(
                    {
                        "user_id": update.message.from_user.id,
                        "username": update.message.from_user.username,
                        "chat_id": chat_id,
                    }
                )

                # Save to S3
                try:
                    self.s3_helper.save_chat_history(
                        user_id=update.message.from_user.id, message_data=message_data
                    )
                except Exception as e:
                    logging.error(f"Failed to save chat history to S3: {str(e)}")
                    # Don't raise the error to avoid interrupting chat flow

                # Generate speech from GPT's response
                speech_file, text_length = await self.openai.generate_speech(
                    text=response
                )

                # Add TTS usage to tracker
                self.usage[user_id].add_tts_request(
                    text_length, self.config["tts_model"], self.config["tts_prices"]
                )
                if str(user_id) not in allowed_user_ids and "guests" in self.usage:
                    self.usage["guests"].add_tts_request(
                        text_length, self.config["tts_model"], self.config["tts_prices"]
                    )

                # Send the audio response
                await self.send_audio_response(update, response, user_id)

                # If configured to show transcript
                if self.config["voice_reply_transcript"]:
                    transcript_output = f"_{localized_text('transcript', bot_language)}:_\n\"{transcript}\""
                    chunks = split_into_chunks(transcript_output)
                    for index, transcript_chunk in enumerate(chunks):
                        await update.effective_message.reply_text(
                            message_thread_id=get_thread_id(update),
                            reply_to_message_id=(
                                get_reply_to_message_id(self.config, update)
                                if index == 0
                                else None
                            ),
                            text=transcript_chunk,
                            parse_mode=constants.ParseMode.MARKDOWN,
                        )

            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=f"{localized_text('transcribe_fail', bot_language)}: {str(e)}",
                    parse_mode=constants.ParseMode.MARKDOWN,
                )
            finally:
                if os.path.exists(filename_mp3):
                    os.remove(filename_mp3)
                if os.path.exists(filename):
                    os.remove(filename)

        await wrap_with_indicator(
            update, context, _execute, constants.ChatAction.TYPING
        )

    async def vision(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Interpret image using vision model.
        """
        if not self.config[
            "enable_vision"
        ] or not await self.check_allowed_and_within_budget(update, context):
            return

        chat_id = update.effective_chat.id
        prompt = update.message.caption

        if is_group_chat(update):
            if self.config["ignore_group_vision"]:
                logging.info("Vision coming from group chat, ignoring...")
                return
            else:
                trigger_keyword = self.config["group_trigger_keyword"]
                if (prompt is None and trigger_keyword != "") or (
                    prompt is not None
                    and not prompt.lower().startswith(trigger_keyword.lower())
                ):
                    logging.info(
                        "Vision coming from group chat with wrong keyword, ignoring..."
                    )
                    return

        image = update.message.effective_attachment[-1]

        async def _execute():
            bot_language = self.config["bot_language"]
            try:
                # Get the file from Telegram
                media_file = await context.bot.get_file(image.file_id)
                image_bytes = await media_file.download_as_bytearray()

                # Extract metadata
                metadata = {
                    "file_id": image.file_id,
                    "file_unique_id": image.file_unique_id,
                    "width": image.width,
                    "height": image.height,
                    "file_size": image.file_size,
                    "date": datetime.datetime.now().isoformat(),
                    "user_id": update.message.from_user.id,
                    "user_name": update.message.from_user.name,
                }

                # Get location from message if available
                if update.message.location:
                    metadata.update(
                        {
                            "message_latitude": update.message.location.latitude,
                            "message_longitude": update.message.location.longitude,
                        }
                    )

                # Try to extract EXIF data including GPS
                try:
                    image_io = BytesIO(image_bytes)
                    img = Image.open(image_io)

                    if hasattr(img, "_getexif"):
                        exif = img._getexif()
                        if exif is not None:
                            labeled_exif = {}
                            for key, val in exif.items():
                                if key in ExifTags.TAGS:
                                    labeled_exif[ExifTags.TAGS[key]] = str(val)

                            # Extract GPS data if available
                            if "GPSInfo" in labeled_exif:
                                gps_info = {}
                                for key, val in exif[34853].items():
                                    if key in ExifTags.GPSTAGS:
                                        gps_info[ExifTags.GPSTAGS[key]] = str(val)
                                metadata["gps_exif"] = gps_info

                            metadata["exif"] = labeled_exif

                    # Try piexif as alternative method
                    try:
                        exif_dict = piexif.load(img.info.get("exif", b""))
                        if "0th" in exif_dict:
                            metadata["piexif_data"] = {
                                ExifTags.TAGS.get(tag_id, str(tag_id)): str(value)
                                for tag_id, value in exif_dict["0th"].items()
                            }
                        if "GPS" in exif_dict:
                            metadata["piexif_gps"] = {
                                ExifTags.GPSTAGS.get(tag_id, str(tag_id)): str(value)
                                for tag_id, value in exif_dict["GPS"].items()
                            }
                    except Exception as e:
                        logging.debug(f"Piexif extraction failed: {str(e)}")

                except Exception as e:
                    logging.debug(f"EXIF extraction failed: {str(e)}")

                # Add any available mime type
                if hasattr(image, "mime_type"):
                    metadata["mime_type"] = image.mime_type

                # Upload to S3 with metadata
                try:
                    original_filename = None
                    if hasattr(image, "file_name"):
                        original_filename = image.file_name
                        metadata["original_filename"] = original_filename
                    elif hasattr(image, "file_id"):
                        original_filename = f"{image.file_id}.jpeg"

                    self.s3_helper.upload_image(
                        image_bytes, chat_id, original_filename, metadata=metadata
                    )
                    logging.info(
                        f"Successfully uploaded image with metadata to S3 for chat_id {chat_id}"
                    )
                except Exception as e:
                    logging.error(
                        f"Failed to upload image to S3 for chat_id {chat_id}: {str(e)}",
                        exc_info=True,
                    )

                # Request location from user
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    text=localized_text(
                        "request_location", self.config["bot_language"]
                    ),
                    reply_markup=ReplyKeyboardMarkup(
                        [
                            [
                                KeyboardButton(
                                    localized_text(
                                        "share_location", self.config["bot_language"]
                                    ),
                                    request_location=True,
                                )
                            ]
                        ],
                        one_time_keyboard=True,
                    ),
                )

                # Continue with vision processing...
                temp_file = io.BytesIO(image_bytes)

                # Rest of existing vision processing code...
                temp_file_png = io.BytesIO()
                try:
                    original_image = Image.open(temp_file)
                    original_image.save(temp_file_png, format="PNG")
                    logging.info(
                        f"New vision request received from user {update.message.from_user.name} "
                        f"(id: {update.message.from_user.id})"
                    )
                except Exception as e:
                    logging.exception(e)
                    await update.effective_message.reply_text(
                        message_thread_id=get_thread_id(update),
                        reply_to_message_id=get_reply_to_message_id(
                            self.config, update
                        ),
                        text=localized_text("media_type_fail", bot_language),
                    )
                    return

                # Continue with existing vision processing code...
                user_id = update.message.from_user.id
                if user_id not in self.usage:
                    self.usage[user_id] = UsageTracker(
                        user_id, update.message.from_user.name
                    )

                if self.config["stream"]:
                    stream_response = self.openai.interpret_image_stream(
                        chat_id=chat_id, fileobj=temp_file_png, prompt=prompt
                    )
                    i = 0
                    prev = ""
                    sent_message = None
                    backoff = 0
                    stream_chunk = 0
                    accumulated_response = ""

                    async for content, tokens in stream_response:
                        if is_direct_result(content):
                            return await handle_direct_result(
                                self.config, update, content
                            )

                        if len(content.strip()) == 0:
                            continue

                        accumulated_response = content
                        stream_chunks = split_into_chunks(content)
                        if len(stream_chunks) > 1:
                            content = stream_chunks[-1]
                            if stream_chunk != len(stream_chunks) - 1:
                                stream_chunk += 1
                                try:
                                    await edit_message_with_retry(
                                        context,
                                        chat_id,
                                        str(sent_message.message_id),
                                        stream_chunks[-2],
                                    )
                                except:
                                    pass
                                try:
                                    sent_message = (
                                        await update.effective_message.reply_text(
                                            message_thread_id=get_thread_id(update),
                                            text=content if len(content) > 0 else "...",
                                        )
                                    )
                                except:
                                    pass
                                continue

                        cutoff = get_stream_cutoff_values(update, content)
                        cutoff += backoff

                        if i == 0:
                            try:
                                if sent_message is not None:
                                    await context.bot.delete_message(
                                        chat_id=sent_message.chat_id,
                                        message_id=sent_message.message_id,
                                    )
                                sent_message = (
                                    await update.effective_message.reply_text(
                                        message_thread_id=get_thread_id(update),
                                        reply_to_message_id=get_reply_to_message_id(
                                            self.config, update
                                        ),
                                        text=content,
                                    )
                                )
                            except:
                                continue

                        elif (
                            abs(len(content) - len(prev)) > cutoff
                            or tokens != "not_finished"
                        ):
                            prev = content
                            try:
                                use_markdown = tokens != "not_finished"
                                await edit_message_with_retry(
                                    context,
                                    chat_id,
                                    str(sent_message.message_id),
                                    text=content,
                                    markdown=use_markdown,
                                )

                            except RetryAfter as e:
                                backoff += 5
                                await asyncio.sleep(e.retry_after)
                                continue

                            except TimedOut:
                                backoff += 5
                                await asyncio.sleep(0.5)
                                continue

                            except Exception:
                                backoff += 5
                                continue

                            await asyncio.sleep(0.01)

                        i += 1
                        if tokens != "not_finished":
                            total_tokens = int(tokens)

                            # First generate and send audio once streaming is complete
                            await self.send_audio_response(
                                update, accumulated_response, user_id
                            )

                            # THEN save chat history AFTER sending the response
                            try:
                                message_data = {
                                    "user_id": update.message.from_user.id,
                                    "username": update.message.from_user.username,
                                    "chat_id": chat_id,
                                    "query_time": datetime.datetime.now().isoformat(),
                                    "query": (
                                        prompt
                                        if prompt
                                        else self.config.get(
                                            "vision_prompt"
                                        )
                                    ),
                                    "response_time": datetime.datetime.now().isoformat(),
                                    "response": accumulated_response,
                                    "total_tokens": str(total_tokens),
                                    "streaming": True,
                                    "image_response": True,
                                }

                                logging.debug(
                                    f"Saving streaming chat history to S3 after sending response to user {update.message.from_user.id}"
                                )
                                result = self.s3_helper.save_chat_history(
                                    user_id=update.message.from_user.id,
                                    message_data=message_data,
                                )
                                logging.info(
                                    f"Successfully saved streaming image conversation to S3: {result}"
                                )
                            except Exception as e:
                                logging.error(
                                    f"Failed to save streaming image conversation to S3: {str(e)}",
                                    exc_info=True,
                                )

                else:
                    try:
                        interpretation, total_tokens = (
                            await self.openai.interpret_image(
                                chat_id, temp_file_png, prompt=prompt
                            )
                        )

                        # First generate and send the audio response
                        await self.send_audio_response(update, interpretation, user_id)

                        # Then send the text response
                        try:
                            await update.effective_message.reply_text(
                                message_thread_id=get_thread_id(update),
                                reply_to_message_id=get_reply_to_message_id(
                                    self.config, update
                                ),
                                text=interpretation,
                                parse_mode=constants.ParseMode.MARKDOWN,
                            )
                        except BadRequest:
                            try:
                                await update.effective_message.reply_text(
                                    message_thread_id=get_thread_id(update),
                                    reply_to_message_id=get_reply_to_message_id(
                                        self.config, update
                                    ),
                                    text=interpretation,
                                )
                            except Exception as e:
                                logging.exception(e)
                                await update.effective_message.reply_text(
                                    message_thread_id=get_thread_id(update),
                                    reply_to_message_id=get_reply_to_message_id(
                                        self.config, update
                                    ),
                                    text=f"{localized_text('vision_fail', bot_language)}: {str(e)}",
                                    parse_mode=constants.ParseMode.MARKDOWN,
                                )

                        # NOW save the chat history AFTER sending responses
                        try:
                            message_data = {
                                "user_id": update.message.from_user.id,
                                "username": update.message.from_user.username,
                                "chat_id": chat_id,
                                "query_time": datetime.datetime.now().isoformat(),
                                "query": (
                                    prompt
                                    if prompt
                                    else self.config.get(
                                        "vision_prompt"
                                    )
                                ),
                                "response_time": datetime.datetime.now().isoformat(),
                                "response": interpretation,
                                "total_tokens": str(total_tokens),
                                "image_response": True,
                            }

                            logging.debug(
                                f"Saving chat history to S3 after sending response to user {update.message.from_user.id}"
                            )
                            result = self.s3_helper.save_chat_history(
                                user_id=update.message.from_user.id,
                                message_data=message_data,
                            )
                            logging.info(
                                f"Successfully saved image conversation to S3: {result}"
                            )
                        except Exception as e:
                            logging.error(
                                f"Failed to save image conversation to S3: {str(e)}",
                                exc_info=True,
                            )

                    except Exception as e:
                        logging.exception(e)
                        await update.effective_message.reply_text(
                            message_thread_id=get_thread_id(update),
                            reply_to_message_id=get_reply_to_message_id(
                                self.config, update
                            ),
                            text=f"{localized_text('vision_fail', bot_language)}: {str(e)}",
                            parse_mode=constants.ParseMode.MARKDOWN,
                        )
                vision_token_price = self.config["vision_token_price"]
                self.usage[user_id].add_vision_tokens(total_tokens, vision_token_price)

                allowed_user_ids = self.config["allowed_user_ids"].split(",")
                if str(user_id) not in allowed_user_ids and "guests" in self.usage:
                    self.usage["guests"].add_vision_tokens(
                        total_tokens, vision_token_price
                    )

            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=(
                        f"{localized_text('media_download_fail', bot_language)[0]}: "
                        f"{str(e)}. {localized_text('media_download_fail', bot_language)[1]}"
                    ),
                    parse_mode=constants.ParseMode.MARKDOWN,
                )

        await wrap_with_indicator(
            update, context, _execute, constants.ChatAction.TYPING
        )

    async def prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        React to incoming messages and respond accordingly.
        """
        if update.edited_message or not update.message or update.message.via_bot:
            return

        if not await self.check_allowed_and_within_budget(update, context):
            return

        user_id = update.message.from_user.id
        chat_id = update.effective_chat.id

        # Always check profile on every message
        logging.info(f"Checking profile for user {user_id}")
        # Only start onboarding if user is not already in the process
        if user_id not in self.profile_collection_state:
            await self.check_user_profile(update, user_id)
            # Only return if onboarding actually started (user is now in profile_collection_state)
            if user_id in self.profile_collection_state:
                return  # Return here to wait for user's response

        # Check if we're in the middle of profile collection
        if user_id in self.profile_collection_state:
            state = self.profile_collection_state[user_id]
            logging.info(f"User {user_id} is in profile collection state: {state}")

            if state == "waiting_for_first_name":
                # Save the first name
                first_name = update.message.text
                if not self.user_profiles.get(user_id):
                    self.user_profiles[user_id] = {}

                self.user_profiles[user_id]["first_name"] = first_name
                logging.info(f"Saved first name: {first_name}")

                # Now ask for last name
                await update.effective_message.reply_text(
                    "Écrivez simplement votre nom de famille:"
                )
                self.profile_collection_state[user_id] = "waiting_for_last_name"
                return

            elif state == "waiting_for_last_name":
                # Save the last name
                last_name = update.message.text
                self.user_profiles[user_id]["last_name"] = last_name
                logging.info(f"Saved last name: {last_name}")

                # Now ask for user type
                keyboard = [
                    [InlineKeyboardButton("Admin", callback_data="type_Admin")],
                    [
                        InlineKeyboardButton(
                            "Agronomist", callback_data="type_Agronomist"
                        )
                    ],
                    [InlineKeyboardButton("User", callback_data="type_User")],
                    [InlineKeyboardButton("Coach", callback_data="type_Coach")],
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await update.effective_message.reply_text(
                    "Quel est votre rôle?", reply_markup=reply_markup
                )
                self.profile_collection_state[user_id] = "waiting_for_user_type"
                return

            elif state == "waiting_for_user_type":
                # User type is handled by callback query
                logging.info("Waiting for user type selection")
                return

            elif state == "waiting_for_coops":
                # Cooperative selection is handled by callback query
                logging.info("Waiting for cooperative selection")
                return

            elif state == "waiting_for_name_verification":
                # Verify the name
                full_name = f"{self.user_profiles[user_id]['first_name']} {self.user_profiles[user_id]['last_name']}"
                if update.message.text.lower() == full_name.lower():
                    # Name verified, now ask for cooperative
                    cooperative_options = [
                        "No-Cooperative",
                        "ECAM",
                        "Groupement Agricole de Kongouanou",
                        "Groupement de Tenikro",
                        "RISO",
                    ]
                    keyboard = [
                        [InlineKeyboardButton(option, callback_data=f"coop_{option}")]
                        for option in cooperative_options
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await update.effective_message.reply_text(
                        "Merci! Veuillez sélectionner votre coopérative:",
                        reply_markup=reply_markup,
                    )
                    self.profile_collection_state[user_id] = "waiting_for_coops"
                else:
                    await update.effective_message.reply_text(
                        "Les noms ne correspondent pas. Veuillez réessayer:"
                    )
                return

        # Normal message handling continues here
        logging.info(
            f"New message received from user {update.message.from_user.name} (id: {update.message.from_user.id})"
        )

        prompt = message_text(update.message)
        self.last_message[chat_id] = prompt

        if is_group_chat(update):
            trigger_keyword = self.config["group_trigger_keyword"]

            if prompt.lower().startswith(
                trigger_keyword.lower()
            ) or update.message.text.lower().startswith("/chat"):
                if prompt.lower().startswith(trigger_keyword.lower()):
                    prompt = prompt[len(trigger_keyword) :].strip()

                if (
                    update.message.reply_to_message
                    and update.message.reply_to_message.text
                    and update.message.reply_to_message.from_user.id != context.bot.id
                ):
                    prompt = f'"{update.message.reply_to_message.text}" {prompt}'
            else:
                if (
                    update.message.reply_to_message
                    and update.message.reply_to_message.from_user.id == context.bot.id
                ):
                    logging.info("Message is a reply to the bot, allowing...")
                else:
                    logging.warning(
                        "Message does not start with trigger keyword, ignoring..."
                    )
                    return

        try:
            total_tokens = 0
            accumulated_response = ""

            async def _reply():
                nonlocal total_tokens, accumulated_response

                if self.config.get("stream", True):
                    async for content, tokens in self.openai.get_chat_response_stream(
                        chat_id=chat_id, query=prompt
                    ):
                        if tokens != "not_finished":
                            total_tokens = int(tokens)
                            accumulated_response = content

                            # Save chat history after streaming is complete
                            message_data = self.openai.get_last_message_data(chat_id)
                            message_data.update(
                                {
                                    "user_id": update.message.from_user.id,
                                    "username": update.message.from_user.username,
                                    "chat_id": chat_id,
                                    "streaming": True,
                                }
                            )

                            try:
                                self.s3_helper.save_chat_history(
                                    user_id=update.message.from_user.id,
                                    message_data=message_data,
                                )
                            except Exception as e:
                                logging.error(
                                    f"Failed to save chat history to S3: {str(e)}"
                                )

                            # Generate and send audio once streaming is complete
                            await self.send_audio_response(update, content, user_id)
                else:
                    response, total_tokens = await self.openai.get_chat_response(
                        chat_id=chat_id, query=prompt
                    )
                    accumulated_response = response

                    if is_direct_result(response):
                        return await handle_direct_result(self.config, update, response)

                    # Get message data with timestamps
                    message_data = self.openai.get_last_message_data(chat_id)
                    message_data.update(
                        {
                            "user_id": update.message.from_user.id,
                            "username": update.message.from_user.username,
                            "chat_id": chat_id,
                            "streaming": False,
                        }
                    )

                    # Save to S3
                    try:
                        self.s3_helper.save_chat_history(
                            user_id=update.message.from_user.id,
                            message_data=message_data,
                        )
                    except Exception as e:
                        logging.error(f"Failed to save chat history to S3: {str(e)}")

                    # Generate speech from the response
                    await self.send_audio_response(update, response, user_id)

                # Send the text response
                chunks = split_into_chunks(accumulated_response)
                for index, chunk in enumerate(chunks):
                    try:
                        await update.effective_message.reply_text(
                            message_thread_id=get_thread_id(update),
                            reply_to_message_id=(
                                get_reply_to_message_id(self.config, update)
                                if index == 0
                                else None
                            ),
                            text=chunk,
                            parse_mode=constants.ParseMode.MARKDOWN,
                        )
                    except Exception:
                        try:
                            await update.effective_message.reply_text(
                                message_thread_id=get_thread_id(update),
                                reply_to_message_id=(
                                    get_reply_to_message_id(self.config, update)
                                    if index == 0
                                    else None
                                ),
                                text=chunk,
                            )
                        except Exception as exception:
                            raise exception

            await wrap_with_indicator(
                update, context, _reply, constants.ChatAction.TYPING
            )

            add_chat_request_to_usage_tracker(
                self.usage, self.config, user_id, total_tokens
            )

        except Exception as e:
            logging.exception(e)
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                reply_to_message_id=get_reply_to_message_id(self.config, update),
                text=f"{localized_text('chat_fail', self.config['bot_language'])} {str(e)}",
                parse_mode=constants.ParseMode.MARKDOWN,
            )

    async def inline_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle the inline query. This is run when you type: @botusername <query>
        """
        query = update.inline_query.query
        if len(query) < 3:
            return
        if not await self.check_allowed_and_within_budget(
            update, context, is_inline=True
        ):
            return

        callback_data_suffix = "gpt:"
        result_id = str(uuid4())
        self.inline_queries_cache[result_id] = query
        callback_data = f"{callback_data_suffix}{result_id}"

        await self.send_inline_query_result(
            update, result_id, message_content=query, callback_data=callback_data
        )

    async def send_inline_query_result(
        self, update: Update, result_id, message_content, callback_data=""
    ):
        """
        Send inline query result
        """
        try:
            reply_markup = None
            bot_language = self.config["bot_language"]
            if callback_data:
                reply_markup = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text=f'🤖 {localized_text("answer_with_chatgpt", bot_language)}',
                                callback_data=callback_data,
                            )
                        ]
                    ]
                )

            inline_query_result = InlineQueryResultArticle(
                id=result_id,
                title=localized_text("ask_chatgpt", bot_language),
                input_message_content=InputTextMessageContent(message_content),
                description=message_content,
                thumbnail_url="https://user-images.githubusercontent.com/11541888/223106202-7576ff11-2c8e-408d-94ea-b02a7a32149a.png",
                reply_markup=reply_markup,
            )

            await update.inline_query.answer([inline_query_result], cache_time=0)
        except Exception as e:
            logging.error(
                f"An error occurred while generating the result card for inline query {e}"
            )

    async def handle_callback_inline_query(
        self, update: Update, context: CallbackContext
    ):
        """
        Handle the callback query from the inline query result
        """
        callback_data = update.callback_query.data
        user_id = update.callback_query.from_user.id
        inline_message_id = update.callback_query.inline_message_id
        name = update.callback_query.from_user.name
        callback_data_suffix = "gpt:"
        query = ""
        bot_language = self.config["bot_language"]
        answer_tr = localized_text("answer", bot_language)
        loading_tr = localized_text("loading", bot_language)

        try:
            if callback_data.startswith(callback_data_suffix):
                unique_id = callback_data.split(":")[1]
                total_tokens = 0

                # Retrieve the prompt from the cache
                query = self.inline_queries_cache.get(unique_id)
                if query:
                    self.inline_queries_cache.pop(unique_id)
                else:
                    error_message = (
                        f'{localized_text("error", bot_language)}. '
                        f'{localized_text("try_again", bot_language)}'
                    )
                    await edit_message_with_retry(
                        context,
                        chat_id=None,
                        message_id=inline_message_id,
                        text=f"{query}\n\n_{answer_tr}:_\n{error_message}",
                        is_inline=True,
                    )
                    return

                unavailable_message = localized_text(
                    "function_unavailable_in_inline_mode", bot_language
                )
                if self.config["stream"]:
                    stream_response = self.openai.get_chat_response_stream(
                        chat_id=user_id, query=query
                    )
                    i = 0
                    prev = ""
                    backoff = 0
                    async for content, tokens in stream_response:
                        if is_direct_result(content):
                            cleanup_intermediate_files(content)
                            await edit_message_with_retry(
                                context,
                                chat_id=None,
                                message_id=inline_message_id,
                                text=f"{query}\n\n_{answer_tr}:_\n{unavailable_message}",
                                is_inline=True,
                            )
                            return

                        if len(content.strip()) == 0:
                            continue

                        cutoff = get_stream_cutoff_values(update, content)
                        cutoff += backoff

                        if i == 0:
                            try:
                                await edit_message_with_retry(
                                    context,
                                    chat_id=None,
                                    message_id=inline_message_id,
                                    text=f"{query}\n\n{answer_tr}:\n{content}",
                                    is_inline=True,
                                )
                            except:
                                continue

                        elif (
                            abs(len(content) - len(prev)) > cutoff
                            or tokens != "not_finished"
                        ):
                            prev = content
                            try:
                                use_markdown = tokens != "not_finished"
                                divider = "_" if use_markdown else ""
                                text = f"{query}\n\n{divider}{answer_tr}:{divider}\n{content}"

                                # We only want to send the first 4096 characters. No chunking allowed in inline mode.
                                text = text[:4096]

                                await edit_message_with_retry(
                                    context,
                                    chat_id=None,
                                    message_id=inline_message_id,
                                    text=text,
                                    markdown=use_markdown,
                                    is_inline=True,
                                )

                            except RetryAfter as e:
                                backoff += 5
                                await asyncio.sleep(e.retry_after)
                                continue
                            except TimedOut:
                                backoff += 5
                                await asyncio.sleep(0.5)
                                continue
                            except Exception:
                                backoff += 5
                                continue

                            await asyncio.sleep(0.01)

                        i += 1
                        if tokens != "not_finished":
                            total_tokens = int(tokens)

                else:

                    async def _send_inline_query_response():
                        nonlocal total_tokens
                        # Edit the current message to indicate that the answer is being processed
                        await context.bot.edit_message_text(
                            inline_message_id=inline_message_id,
                            text=f"{query}\n\n_{answer_tr}:_\n{loading_tr}",
                            parse_mode=constants.ParseMode.MARKDOWN,
                        )

                        logging.info(f"Generating response for inline query by {name}")
                        response, total_tokens = await self.openai.get_chat_response(
                            chat_id=user_id, query=query
                        )

                        if is_direct_result(response):
                            cleanup_intermediate_files(response)
                            await edit_message_with_retry(
                                context,
                                chat_id=None,
                                message_id=inline_message_id,
                                text=f"{query}\n\n_{answer_tr}:_\n{unavailable_message}",
                                is_inline=True,
                            )
                            return

                        text_content = f"{query}\n\n_{answer_tr}:_\n{response}"

                        # We only want to send the first 4096 characters. No chunking allowed in inline mode.
                        text_content = text_content[:4096]

                        # Edit the original message with the generated content
                        await edit_message_with_retry(
                            context,
                            chat_id=None,
                            message_id=inline_message_id,
                            text=text_content,
                            is_inline=True,
                        )

                    await wrap_with_indicator(
                        update,
                        context,
                        _send_inline_query_response,
                        constants.ChatAction.TYPING,
                        is_inline=True,
                    )

                add_chat_request_to_usage_tracker(
                    self.usage, self.config, user_id, total_tokens
                )

        except Exception as e:
            logging.error(
                f"Failed to respond to an inline query via button callback: {e}"
            )
            logging.exception(e)
            localized_answer = localized_text("chat_fail", self.config["bot_language"])
            await edit_message_with_retry(
                context,
                chat_id=None,
                message_id=inline_message_id,
                text=f"{query}\n\n_{answer_tr}:_\n{localized_answer} {str(e)}",
                is_inline=True,
            )

    async def check_allowed_and_within_budget(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, is_inline=False
    ) -> bool:
        """
        Checks if the user is allowed to use the bot and if they are within their budget
        :param update: Telegram update object
        :param context: Telegram context object
        :param is_inline: Boolean flag for inline queries
        :return: Boolean indicating if the user is allowed to use the bot
        """
        name = (
            update.inline_query.from_user.name
            if is_inline
            else update.message.from_user.name
        )
        user_id = (
            update.inline_query.from_user.id
            if is_inline
            else update.message.from_user.id
        )

        if not await is_allowed(self.config, update, context, is_inline=is_inline):
            logging.warning(
                f"User {name} (id: {user_id}) is not allowed to use the bot"
            )
            await self.send_disallowed_message(update, context, is_inline)
            return False
        if not is_within_budget(self.config, self.usage, update, is_inline=is_inline):
            logging.warning(f"User {name} (id: {user_id}) reached their usage limit")
            await self.send_budget_reached_message(update, context, is_inline)
            return False

        return True

    async def send_disallowed_message(
        self, update: Update, _: ContextTypes.DEFAULT_TYPE, is_inline=False
    ):
        """
        Sends the disallowed message to the user.
        """
        if not is_inline:
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=self.disallowed_message,
                disable_web_page_preview=True,
            )
        else:
            result_id = str(uuid4())
            await self.send_inline_query_result(
                update, result_id, message_content=self.disallowed_message
            )

    async def send_budget_reached_message(
        self, update: Update, _: ContextTypes.DEFAULT_TYPE, is_inline=False
    ):
        """
        Sends the budget reached message to the user.
        """
        if not is_inline:
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update), text=self.budget_limit_message
            )
        else:
            result_id = str(uuid4())
            await self.send_inline_query_result(
                update, result_id, message_content=self.budget_limit_message
            )

    async def post_init(self, application: Application) -> None:
        """
        Post initialization hook for the bot.
        """
        await application.bot.set_my_commands(
            self.group_commands, scope=BotCommandScopeAllGroupChats()
        )
        await application.bot.set_my_commands(self.commands)

    def run(self):
        """
        Runs the bot indefinitely until the user presses Ctrl+C
        """
        application = (
            ApplicationBuilder()
            .token(self.config["token"])
            .proxy_url(self.config["proxy"])
            .get_updates_proxy_url(self.config["proxy"])
            .post_init(self.post_init)
            .concurrent_updates(True)
            .build()
        )

        application.add_handler(CommandHandler("reset", self.reset))
        application.add_handler(CommandHandler("help", self.help))
        application.add_handler(CommandHandler("start", self.help))
        application.add_handler(CommandHandler("image", self.image))
        application.add_handler(CommandHandler("tts", self.tts))
        application.add_handler(CommandHandler("stats", self.stats))
        application.add_handler(CommandHandler("resend", self.resend))
        application.add_handler(CommandHandler("profile", self.profile))
        application.add_handler(CallbackQueryHandler(self.handle_profile_callback))
        application.add_handler(
            CommandHandler(
                "chat",
                self.prompt,
                filters=filters.ChatType.GROUP | filters.ChatType.SUPERGROUP,
            )
        )
        application.add_handler(
            MessageHandler(filters.PHOTO | filters.Document.IMAGE, self.vision)
        )
        application.add_handler(
            MessageHandler(
                filters.AUDIO
                | filters.VOICE
                | filters.Document.AUDIO
                | filters.VIDEO
                | filters.VIDEO_NOTE
                | filters.Document.VIDEO,
                self.transcribe,
            )
        )
        application.add_handler(
            MessageHandler(filters.TEXT & (~filters.COMMAND), self.prompt)
        )
        application.add_handler(
            InlineQueryHandler(
                self.inline_query,
                chat_types=[
                    constants.ChatType.GROUP,
                    constants.ChatType.SUPERGROUP,
                    constants.ChatType.PRIVATE,
                ],
            )
        )
        application.add_handler(CallbackQueryHandler(self.handle_callback_inline_query))

        # Add new location handler
        application.add_handler(MessageHandler(filters.LOCATION, self.handle_location))

        application.add_error_handler(error_handler)

        application.run_polling()

    async def send_audio_response(self, update: Update, text: str, user_id: int):
        """
        Helper method to generate and send TTS audio response
        """
        speech_file, text_length = await self.openai.generate_speech(text=text)

        # Add TTS usage to tracker
        allowed_user_ids = self.config["allowed_user_ids"].split(",")
        self.usage[user_id].add_tts_request(
            text_length,
            self.config["tts_model"],
            self.config["tts_prices"],
        )
        if str(user_id) not in allowed_user_ids and "guests" in self.usage:
            self.usage["guests"].add_tts_request(
                text_length,
                self.config["tts_model"],
                self.config["tts_prices"],
            )

        # Send the audio response
        await update.effective_message.reply_voice(
            message_thread_id=get_thread_id(update),
            reply_to_message_id=get_reply_to_message_id(self.config, update),
            voice=speech_file,
        )
        speech_file.close()

    async def handle_location(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handles location messages sent by users
        """
        if not await is_allowed(self.config, update, context):
            logging.warning(
                f"User {update.message.from_user.name} (id: {update.message.from_user.id}) "
                "is not allowed to send location"
            )
            await self.send_disallowed_message(update, context)
            return

        try:
            user = update.message.from_user
            location = update.message.location
            chat_id = update.effective_chat.id

            logging.info(
                f"Received location from {user.name} (id: {user.id}): "
                f"lat={location.latitude}, lon={location.longitude}"
            )

            # Prepare location data
            location_data = {
                "user_id": user.id,
                "user_name": user.name,
                "chat_id": chat_id,
                "latitude": location.latitude,
                "longitude": location.longitude,
                "live_period": getattr(location, "live_period", None),
                "horizontal_accuracy": getattr(location, "horizontal_accuracy", None),
                "heading": getattr(location, "heading", None),
                "proximity_alert_radius": getattr(
                    location, "proximity_alert_radius", None
                ),
                "timestamp": datetime.datetime.now().isoformat(),
            }

            # Generate filename with timestamp
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            s3_key = f"{user.id}/locations/{timestamp}.json"

            # Upload to S3
            self.s3_helper.s3_client.put_object(
                Bucket=self.s3_helper.bucket_name,
                Key=s3_key,
                Body=json.dumps(location_data, indent=2),
                ContentType="application/json",
            )

            bot_language = self.config["bot_language"]
            await update.message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text("location_received", bot_language),
                reply_markup=ReplyKeyboardRemove(),
            )

            # If this is a live location, store it for updates
            if location.live_period:
                if not hasattr(self, "live_locations"):
                    self.live_locations = {}
                self.live_locations[chat_id] = {
                    "message_id": update.message.message_id,
                    "expires_at": datetime.datetime.now().timestamp()
                    + location.live_period,
                    "s3_key": s3_key,
                }

        except Exception as e:
            logging.error(f"Error handling location: {str(e)}")
            await update.message.reply_text(
                message_thread_id=get_thread_id(update),
                text=f"⚠️ Error saving location: {str(e)}",
                reply_markup=ReplyKeyboardRemove(),
            )

    async def check_user_profile(self, update, user_id):
        """Check if user has a profile and prompt for creation if not"""
        try:
            # Try to get profile from memory cache
            profile = self.user_profiles.get(user_id)
            logging.info(f"Profile from memory cache: {profile}")

            # If not in memory, try to get from S3
            if not profile:
                profile = self.s3_helper.get_user_profile(user_id)
                logging.info(f"Profile from S3: {profile}")
                if profile:
                    # Cache it for future use
                    self.user_profiles[user_id] = profile

            # Log the onboarding_all_the_time setting
            logging.info(
                f"onboarding_all_the_time setting: {self.config.get('onboarding_all_the_time', False)}"
            )

            # If no profile or onboarding_all_the_time is true, start profile collection
            if not profile or self.config.get("onboarding_all_the_time", False):
                logging.info(f"Starting profile collection for user {user_id}")
                await update.effective_message.reply_text(
                    "Bienvenue sur Gervais, votre assistant en diagnostic du cacao! 🌱\n\n"
                    "Pour mieux vous servir, pourriez-vous nous donner quelques informations?\n\n"
                    "Écrivez simplement votre prénom:"
                )
                self.profile_collection_state[user_id] = "waiting_for_first_name"
            else:
                logging.info(f"Using existing profile for user {user_id}")

            return profile
        except Exception as e:
            logging.error(f"Error checking user profile: {str(e)}")
            return None

    async def profile(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle the /profile command to update user profile"""
        user_id = update.message.from_user.id

        await update.effective_message.reply_text(
            "Mettons à jour votre profil.\n\n" "Écrivez simplement votre prénom:"
        )
        self.profile_collection_state[user_id] = "waiting_for_first_name"

    async def handle_profile_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle callback queries for profile updates"""
        query = update.callback_query
        user_id = query.from_user.id

        if query.data.startswith("type_"):
            user_type = query.data.replace("type_", "")
            self.user_profiles[user_id]["user_type"] = user_type

            # Ask for name verification
            full_name = f"{self.user_profiles[user_id]['first_name']} {self.user_profiles[user_id]['last_name']}"
            await query.edit_message_text(
                text=f"Merci! Pour vérifier, veuillez écrire votre nom complet: {full_name}"
            )
            self.profile_collection_state[user_id] = "waiting_for_name_verification"

        elif query.data.startswith("coop_"):
            selected_coop = query.data.replace("coop_", "")
            self.user_profiles[user_id]["cooperatives"] = [selected_coop]

            # Save to S3
            try:
                profile_data = self.user_profiles[user_id]
                profile_data["updated_at"] = datetime.datetime.now().isoformat()
                self.s3_helper.save_user_profile(user_id, profile_data)

                # Clear state
                del self.profile_collection_state[user_id]

                await query.edit_message_text(
                    text=f"Merci! Votre profil a été enregistré.\n\n"
                    f"Nom: {profile_data['first_name']} {profile_data['last_name']}\n"
                    f"Rôle: {profile_data['user_type']}\n"
                    f"Coopérative: {selected_coop}\n\n"
                    f"Vous pouvez maintenant me poser des questions sur les maladies du cacao ou "
                    f"m'envoyer des photos pour diagnostic."
                )
            except Exception as e:
                logging.error(f"Error saving user profile: {str(e)}")
                await query.edit_message_text(
                    text="Désolé, il y a eu un problème lors de l'enregistrement de votre profil. "
                    "Vous pouvez réessayer plus tard avec la commande /profile."
                )

        await query.answer()

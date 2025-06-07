import logging
import json
from datetime import datetime, timedelta
import pytz
import bot_config
import utils

logger = logging.getLogger(__name__)

CST_TIMEZONE = pytz.timezone("America/Mexico_City")

def check_whatsapp_window(phone, client):
    if client is None:
        logger.error("Twilio client not initialized, cannot check WhatsApp window.")
        return False
    try:
        messages = client.messages.list(
            from_=phone,
            to=bot_config.WHATSAPP_SENDER_NUMBER,
            date_sent_after=datetime.now(CST_TIMEZONE) - timedelta(hours=24)
        )
        if messages:
            logger.debug(f"WhatsApp 24-hour window is active for {phone}. Last message: {messages[0].date_sent}")
            return True
        else:
            logger.debug(f"WhatsApp 24-hour window is not active for {phone}.")
            return False
    except Exception as e:
        logger.error(f"Error checking WhatsApp window for {phone}: {str(e)}", exc_info=True)
        return False

def send_template_message(phone, client_name, project, client):
    try:
        message = client.messages.create(
            from_=bot_config.WHATSAPP_SENDER_NUMBER,
            to=phone,
            content_sid=bot_config.RECONTACT_TEMPLATE_NAME,
            content_variables=json.dumps({
                "1": client_name,
                "2": project
            })
        )
        logger.info(f"Template message sent to {phone}: SID {message.sid}, Status: {message.status}")
        return True
    except Exception as e:
        logger.error(f"Error sending template message to {phone}: {str(e)}")
        return False

def trigger_recontact(conversation_state, client, utils, generate_detailed_report):
    logger.info("Triggering recontact scheduling")
    current_time = datetime.now(CST_TIMEZONE)
    logger.debug(f"Current time (CST): {current_time}")

    recontact_window_start = current_time.replace(
        hour=bot_config.RECONTACT_HOUR_CST, minute=bot_config.RECONTACT_MINUTE_CST, second=0, microsecond=0
    ) - timedelta(minutes=bot_config.RECONTACT_TOLERANCE_MINUTES)
    recontact_window_end = current_time.replace(
        hour=bot_config.RECONTACT_HOUR_CST, minute=bot_config.RECONTACT_MINUTE_CST, second=0, microsecond=0
    ) + timedelta(minutes=bot_config.RECONTACT_TOLERANCE_MINUTES)

    for phone, state in list(conversation_state.items()):
        logger.debug(f"Processing client: {phone}")
        if state.get('is_gerente', False):
            logger.debug(f"Skipping {phone}: Is gerente")
            continue
        if state.get('no_interest', False):
            logger.debug(f"Skipping {phone}: No interest")
            continue

        last_response_time = state.get('last_response_time')
        logger.debug(f"Last response time for {phone}: {last_response_time}")
        if not last_response_time:
            logger.debug(f"Skipping {phone}: No last response time")
            continue

        try:
            last_response = datetime.fromisoformat(last_response_time).astimezone(CST_TIMEZONE)
        except ValueError as e:
            logger.error(f"Invalid last_response_time format for {phone}: {last_response_time}, error: {str(e)}")
            continue

        recontact_time = last_response + timedelta(days=bot_config.RECONTACT_MIN_DAYS)
        recontact_time = recontact_time.replace(
            hour=bot_config.RECONTACT_HOUR_CST, minute=bot_config.RECONTACT_MINUTE_CST, second=0, microsecond=0
        )
        logger.debug(f"Scheduled recontact time for {phone}: {recontact_time}")

        if not (recontact_window_start <= current_time <= recontact_window_end):
            logger.debug(f"Skipping {phone}: Current time {current_time} is outside recontact window ({recontact_window_start} to {recontact_window_end})")
            continue

        if recontact_time.date() != current_time.date():
            logger.debug(f"Skipping {phone}: Recontact date {recontact_time.date()} does not match today {current_time.date()}")
            continue

        if state.get('recontact_attempts', 0) >= 3:
            logger.debug(f"Marking {phone} as no interest: Max recontact attempts reached")
            state['no_interest'] = True
            continue

        if check_whatsapp_window(phone, client):
            logger.debug(f"{phone} is within 24-hour window")
            client_name = state.get('client_name', 'Cliente')
            last_mentioned_project = state.get('last_mentioned_project', 'uno de nuestros proyectos')
            messages = [
                f"Hola {client_name}, soy Giselle de FAV Living. Quería dar seguimiento a nuestra conversación sobre {last_mentioned_project}.",
                "¿Te gustaría saber más detalles o prefieres que hagas un análisis financiero de la inversión?"
            ]

            utils.send_consecutive_messages(phone, messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
            for msg in messages:
                state['history'].append(f"Giselle: {msg}")
        else:
            logger.debug(f"{phone} is outside 24-hour window, sending template message")
            client_name = state.get('client_name', 'Cliente')
            last_mentioned_project = state.get('last_mentioned_project', 'uno de nuestros proyectos')
            if send_template_message(phone, client_name, last_mentioned_project, client):
                state['history'].append(f"Giselle: [Template] Hola {client_name}, soy Giselle de FAV Living. Quería dar seguimiento a nuestra conversación sobre {last_mentioned_project}.")
            else:
                logger.error(f"Failed to send template message to {phone}")

        state['recontact_attempts'] = state.get('recontact_attempts', 0) + 1
        state['schedule_next'] = None

        utils.save_conversation(phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)

    for gerente_phone, gerente_state in list(conversation_state.items()):
        if not gerente_state.get('is_gerente', False):
            continue

        last_report = gerente_state.get('last_weekly_report')
        if last_report:
            last_report_time = datetime.fromisoformat(last_report).astimezone(CST_TIMEZONE)
            if (current_time - last_report_time).days < 7:
                continue

        if current_time.strftime('%A') == bot_config.WEEKLY_REPORT_DAY and current_time.strftime('%H:%M') >= bot_config.WEEKLY_REPORT_TIME:
            report_messages = generate_detailed_report(conversation_state)
            utils.send_consecutive_messages(gerente_phone, report_messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
            gerente_state['last_weekly_report'] = current_time.isoformat()

    logger.info("Recontact scheduling completed")
    return "Recontact scheduling triggered"

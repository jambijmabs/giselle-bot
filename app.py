import os
import logging
import sys
from flask import Flask, request
from twilio.rest import Client
import bot_config
import utils
import message_handler
import gerente_handler
import client_handler
import report_handler
import recontact_handler
import pytz
from datetime import datetime
import re  # Añadido para la lógica de respaldo de extracción de nombre

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('giselle_activity.log')
    ]
)

# Initialize Flask app
app = Flask(__name__)

# Configure logger
logger = logging.getLogger(__name__)

# Mensaje de depuración al inicio del script
logger.info("Script app.py iniciado")

# Configuration Section
CST_TIMEZONE = pytz.timezone("America/Mexico_City")
WHATSAPP_SENDER_NUMBER = bot_config.WHATSAPP_SENDER_NUMBER
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GCS_BUCKET_NAME = bot_config.GCS_BUCKET_NAME
GCS_CONVERSATIONS_PATH = bot_config.GCS_CONVERSATIONS_PATH
DEFAULT_PORT = 8080

# Log registered routes after app initialization
with app.app_context():
    logger.info("Registered routes:")
    for rule in app.url_map.iter_rules():
        logger.info(f"Route: {rule.endpoint} -> {rule}")

# Initialize Twilio client
client = None
try:
    logger.debug("Initializing Twilio client")
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.warning("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set in environment variables. Twilio client will not be initialized.")
    else:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        logger.info("Twilio client initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Twilio client: {str(e)}", exc_info=True)
    client = None

# Initialize OpenAI client
if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY not set in environment variables. Some functionality may not work.")

# Global conversation state
conversation_state = {}

@app.route('/whatsapp', methods=['POST'])
def whatsapp():
    logger.debug("Entered /whatsapp route")
    try:
        if client is None:
            logger.error("Twilio client not initialized. Cannot process WhatsApp messages.")
            return "Error: Twilio client not initialized", 500

        utils.load_conversation_state(conversation_state, GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
        logger.debug("Conversation state reloaded")

        logger.debug(f"Request headers: {dict(request.headers)}")
        logger.debug(f"Request form data: {request.form}")
        logger.debug(f"Request values: {dict(request.values)}")

        logger.debug("Extracting message content")
        phone = request.values.get('From', '')
        incoming_msg = request.values.get('Body', '').strip()
        num_media = int(request.values.get('NumMedia', '0'))
        media_url = request.values.get('MediaUrl0', None) if num_media > 0 else None
        profile_name = request.values.get('ProfileName', None)

        logger.debug(f"From phone: {phone}, Message: {incoming_msg}, NumMedia: {num_media}, MediaUrl: {media_url}, ProfileName: {profile_name}")

        if not phone:
            logger.error("No se encontró 'From' en la solicitud")
            return "Error: Solicitud incompleta", 400

        normalized_phone = phone.replace("whatsapp:", "").strip()
        is_gerente = normalized_phone in bot_config.GERENTE_NUMBERS
        logger.debug(f"Comparando número: phone='{phone}', normalized_phone='{normalized_phone}', GERENTE_NUMBERS={bot_config.GERENTE_NUMBERS}, is_gerente={is_gerente}")

        if is_gerente:
            logger.info(f"Identificado como gerente: {phone}")
            if phone not in conversation_state:
                conversation_state[phone] = {
                    'history': [],
                    'is_gerente': True,
                    'last_contact': datetime.now

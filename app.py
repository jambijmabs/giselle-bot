import os
import logging
import sys
from flask import Flask
import bot_config
import utils
from routes import init_routes

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(formatter)

file_handler = logging.FileHandler('giselle_activity.log')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)

logger.info("Script app.py iniciado")

# Configuration Section
CST_TIMEZONE = pytz.timezone("America/Mexico_City")
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GCS_BUCKET_NAME = bot_config.GCS_BUCKET_NAME
GCS_CONVERSATIONS_PATH = bot_config.GCS_CONVERSATIONS_PATH
DEFAULT_PORT = 8080

logger.debug("Variables de configuración cargadas")

# Initialize Flask app
app = Flask(__name__)

# Initialize global conversation state
conversation_state = {}
logger.debug("Conversation state inicializado")

# Initialize routes
init_routes(app, conversation_state)

if __name__ == '__main__':
    try:
        logger.debug("Starting application initialization - Step 1: Loading conversation state")
        utils.load_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        logger.info("Conversation state loaded")

        logger.debug("Step 2: Downloading projects from storage")
        utils.download_projects_from_storage(GCS_BUCKET_NAME, GCS_BASE_PATH)
        logger.info("Projects downloaded from storage")

        logger.debug("Step 3: Loading projects from folder")
        utils.load_projects_from_folder(GCS_BASE_PATH)
        logger.info("Projects loaded from folder")

        logger.debug("Step 4: Loading gerente responses")
        utils.load_gerente_respuestas(GCS_BASE_PATH)
        logger.info("Gerente responses loaded")

        logger.debug("Step 5: Loading FAQ files")
        utils.load_faq_files(GCS_BASE_PATH)
        logger.info("FAQ files loaded")

        port = int(os.getenv("PORT", DEFAULT_PORT))
        service_url = os.getenv("SERVICE_URL", f"https://giselle-bot-250207106980.us-central1.run.app")
        logger.info(f"Puerto del servidor: {port}")
        logger.info(f"URL del servicio: {service_url}")
        logger.info(f"Configura el webhook en Twilio con: {service_url}/whatsapp")
        logger.info("Iniciando servidor Flask...")
        app.run(host='0.0.0.0', port=port, debug=False)
        logger.info(f"Servidor Flask iniciado en el puerto {port}.")
    except Exception as e:
        logger.error(f"Failed to start application: {str(e)}", exc_info=True)
        sys.exit(1)

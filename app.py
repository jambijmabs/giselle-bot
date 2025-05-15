import os
import logging
import sys
from flask import Flask, request
from twilio.rest import Client
from datetime import datetime, timedelta
import bot_config
import utils
import message_handler

# Configuration Section
WHATSAPP_SENDER_NUMBER = "whatsapp:+18188732305"
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GCS_BUCKET_NAME = "giselle-projects"
GCS_BASE_PATH = "PROYECTOS"
GCS_CONVERSATIONS_PATH = "CONVERSATIONS"
STATE_FILE = "conversation_state.json"
DEFAULT_PORT = 8080
WHATSAPP_TEMPLATE_SID = "HX1234567890abcdef1234567890abcdef"
WHATSAPP_TEMPLATE_VARIABLES = {"1": "Cliente"}

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

# Initialize Twilio client
if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
    logger.error("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set in environment variables")
    raise ValueError("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set")
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Initialize OpenAI client (will be used in message_handler)
if not OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY not set in environment variables")
    raise ValueError("OPENAI_API_KEY not set")

# Global conversation state
conversation_state = {}

# Routes
@app.route('/whatsapp', methods=['POST'])
def whatsapp():
    logger.debug("Entered /whatsapp route")
    try:
        # Log the entire request data for debugging
        logger.debug(f"Request headers: {dict(request.headers)}")
        logger.debug(f"Request form data: {request.form}")
        logger.debug(f"Request values: {dict(request.values)}")

        logger.debug("Extracting message content")
        num_media = int(request.values.get('NumMedia', '0'))
        phone = request.values.get('From', '')

        # Check if the message contains audio
        if num_media > 0:
            media_url = request.values.get('MediaUrl0', '')
            media_content_type = request.values.get('MediaContentType0', '')
            logger.debug(f"Media detected: URL={media_url}, Content-Type={media_content_type}")

            if 'audio' in media_content_type:
                messages, incoming_msg = message_handler.handle_audio_message(
                    media_url, phone, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
                )
                if messages:
                    utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
                    return "Mensaje enviado"
            else:
                messages = ["Lo siento, solo puedo procesar mensajes de texto o audio. ¿Puedes enviar tu mensaje de otra forma?"]
                utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
                return "Mensaje enviado"
        else:
            incoming_msg = request.values.get('Body', '').strip()

        logger.debug(f"Incoming message: {incoming_msg}, Phone: {phone}")

        if not incoming_msg or not phone:
            logger.error("No se encontraron 'Body' o 'From' en la solicitud")
            return "Error: Solicitud incompleta", 400

        # Normalize the phone number
        phone = phone.strip()
        if not phone.startswith('whatsapp:+'):
            if phone.startswith('whatsapp:'):
                phone = f"whatsapp:+{phone[len('whatsapp:'):]}"
            else:
                phone = f"whatsapp:+{phone}"

        if not phone.startswith('whatsapp:+'):
            logger.error(f"Invalid phone number format after normalization: {repr(phone)}")
            return "Error: Invalid phone number format", 400

        logger.info(f"Mensaje recibido de {phone}: {incoming_msg}")

        # Load conversation history with error handling
        try:
            history = utils.load_conversation_history(phone, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            logger.debug(f"Conversation history loaded: {history}")
        except Exception as e:
            logger.error(f"Failed to load conversation history: {str(e)}")
            history = []  # Proceed with an empty history to avoid failing the entire request

        # Initialize conversation state with error handling
        try:
            if phone not in conversation_state:
                conversation_state[phone] = {
                    'history': history,
                    'name_asked': 0,
                    'budget_asked': 0,
                    'contact_time_asked': 0,
                    'messages_since_budget_ask': 0,
                    'messages_without_response': 0,
                    'preferred_time': None,
                    'preferred_days': None,
                    'client_name': None,
                    'client_budget': None,
                    'last_contact': datetime.now().isoformat(),
                    'recontact_attempts': 0,
                    'no_interest': False,
                    'schedule_next': None,
                    'last_incoming_time': datetime.now().isoformat(),
                    'introduced': False,
                    'project_info_shared': {}
                }
            else:
                conversation_state[phone]['history'] = history
                conversation_state[phone]['messages_without_response'] = 0
                conversation_state[phone]['last_incoming_time'] = datetime.now().isoformat()
        except Exception as e:
            logger.error(f"Error initializing conversation state: {str(e)}")
            conversation_state[phone] = {
                'history': [],
                'name_asked': 0,
                'budget_asked': 0,
                'contact_time_asked': 0,
                'messages_since_budget_ask': 0,
                'messages_without_response': 0,
                'preferred_time': None,
                'preferred_days': None,
                'client_name': None,
                'client_budget': None,
                'last_contact': datetime.now().isoformat(),
                'recontact_attempts': 0,
                'no_interest': False,
                'schedule_next': None,
                'last_incoming_time': datetime.now().isoformat(),
                'introduced': False,
                'project_info_shared': {}
            }

        conversation_state[phone]['history'].append(f"Cliente: {incoming_msg}")
        conversation_state[phone]['history'] = conversation_state[phone]['history'][-10:]
        conversation_state[phone]['last_contact'] = datetime.now().isoformat()
        conversation_state[phone]['messages_since_budget_ask'] += 1

        # Check for client name in the message
        if "mi nombre es" in incoming_msg.lower():
            name = incoming_msg.lower().split("mi nombre es")[-1].strip()
            conversation_state[phone]['client_name'] = name.capitalize()
            logger.info(f"Client name set to: {conversation_state[phone]['client_name']}")
            utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)

        # Check for client budget in the message
        if "mi presupuesto es" in incoming_msg.lower() or "presupuesto de" in incoming_msg.lower():
            budget = incoming_msg.lower().split("presupuesto")[-1].strip()
            conversation_state[phone]['client_budget'] = budget
            logger.info(f"Client budget set to: {budget}")
            utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)

        # Check for preferred days and time in the message
        if "prefiero ser contactado" in incoming_msg.lower() or "horario" in incoming_msg.lower():
            if "prefiero ser contactado" in incoming_msg.lower():
                days = incoming_msg.lower().split("prefiero ser contactado")[-1].strip()
                conversation_state[phone]['preferred_days'] = days
                logger.info(f"Preferred days set to: {days}")
            if "horario" in incoming_msg.lower():
                time = incoming_msg.lower().split("horario")[-1].strip()
                conversation_state[phone]['preferred_time'] = time
                logger.info(f"Preferred time set to: {time}")
            utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)

        # Check for no-interest phrases
        if any(phrase in incoming_msg.lower() for phrase in bot_config.NO_INTEREST_PHRASES):
            conversation_state[phone]['no_interest'] = True
            messages = bot_config.handle_no_interest_response()
            logger.info(f"Sending no-interest response: {messages}")
            utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
            conversation_state[phone]['history'].append(f"Giselle: {messages[0]}")
            utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            return "Mensaje enviado"

        # Check for recontact request
        recontact_response = bot_config.handle_recontact_request(incoming_msg, conversation_state[phone])
        if recontact_response:
            messages = recontact_response
            logger.info(f"Sending recontact response: {messages}")
            utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
            conversation_state[phone]['history'].append(f"Giselle: {messages[0]}")
            utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            return "Mensaje enviado"

        # Prepare project information
        project_info = ""
        mentioned_project = None
        try:
            for project, data in utils.projects_data.items():
                project_info += f"Proyecto: {project}\n"
                project_info += "Es un desarrollo que creo que te va a interesar.\n"
                project_info += "\n"
                if project.lower() in incoming_msg.lower():
                    mentioned_project = project
            logger.debug(f"Project info prepared: {project_info}")
        except Exception as project_info_e:
            logger.error(f"Error preparing project information: {str(project_info_e)}")
            project_info = "Información de proyectos no disponible."

        # Build conversation history
        conversation_history = "\n".join(conversation_state[phone]['history'])

        # Check 24-hour session window
        last_incoming_time = datetime.fromisoformat(conversation_state[phone]['last_incoming_time'])
        time_since_last_incoming = datetime.now() - last_incoming_time
        use_template = time_since_last_incoming > timedelta(hours=24)

        if use_template:
            logger.debug("Sending template message")
            message = client.messages.create(
                from_=WHATSAPP_SENDER_NUMBER,
                to=phone,
                content_sid=WHATSAPP_TEMPLATE_SID,
                content_variables=json.dumps(WHATSAPP_TEMPLATE_VARIABLES)
            )
            logger.info(f"Mensaje de plantilla enviado a través de Twilio: SID {message.sid}, Estado: {message.status}")
            updated_message = client.messages(message.sid).fetch()
            logger.info(f"Estado del mensaje actualizado: {updated_message.status}")
            if updated_message.status == "failed":
                logger.error(f"Error al enviar mensaje de plantilla: {updated_message.error_code} - {updated_message.error_message}")

            template_response = bot_config.TEMPLATE_RESPONSE
            conversation_state[phone]['history'].append(f"Giselle: {template_response}")
            conversation_state[phone]['history'] = conversation_state[phone]['history'][-10:]
            utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            return "Mensaje enviado"

        # Process the message and generate a response
        messages, mentioned_project = message_handler.process_message(
            incoming_msg, phone, conversation_state, project_info, conversation_history
        )

        utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)

        for msg in messages:
            conversation_state[phone]['history'].append(f"Giselle: {msg}")
        conversation_state[phone]['history'] = conversation_state[phone]['history'][-10:]

        utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)

        logger.debug("Returning success response")
        return "Mensaje enviado"
    except Exception as e:
        logger.error(f"Error inesperado en /whatsapp: {str(e)}", exc_info=True)
        try:
            phone = phone.strip()
            if not phone.startswith('whatsapp:+'):
                phone = phone.replace('whatsapp:', '').strip()
                phone = f"whatsapp:+{phone.replace(' ', '')}"
            logger.debug(f"Phone number in exception handler: {repr(phone)}")
            if not phone.startswith('whatsapp:+'):
                logger.error(f"Invalid phone number format in exception handler: {repr(phone)}")
                return "Error: Invalid phone number format in exception handler", 400
            message = client.messages.create(
                from_=WHATSAPP_SENDER_NUMBER,
                body="Lo siento, ocurrió un error. ¿En qué más puedo ayudarte?",
                to=phone
            )
            logger.info(f"Fallback message sent: SID {message.sid}, Estado: {message.status}")
            conversation_state[phone]['history'].append("Giselle: Lo siento, ocurrió un error. ¿En qué más puedo ayudarte?")
            utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        except Exception as twilio_e:
            logger.error(f"Error sending fallback message: {str(twilio_e)}")
        return "Error interno del servidor", 500

@app.route('/', methods=['GET'])
def root():
    logger.debug("Solicitud GET recibida en /")
    return "Servidor Flask está funcionando!"

@app.route('/test', methods=['GET'])
def test():
    logger.debug("Solicitud GET recibida en /test")
    return "Servidor Flask está funcionando correctamente!"

@app.route('/schedule_recontact', methods=['GET'])
def trigger_recontact():
    logger.info("Triggering recontact scheduling")
    current_time = datetime.now()
    for phone, state in list(conversation_state.items()):
        messages, should_update = bot_config.handle_recontact(phone, state, current_time)
        if messages:
            utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
            for msg in messages:
                conversation_state[phone]['history'].append(f"Giselle: {msg}")
            utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
    return "Recontact scheduling triggered"

# Application Startup
if __name__ == '__main__':
    utils.load_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
    utils.download_projects_from_storage(GCS_BUCKET_NAME, GCS_BASE_PATH)
    utils.load_projects_from_folder(GCS_BASE_PATH)
    message_handler.initialize_message_handler(
        OPENAI_API_KEY, utils.projects_data, utils.downloadable_urls
    )
    port = int(os.getenv("PORT", DEFAULT_PORT))
    service_url = os.getenv("SERVICE_URL", f"https://giselle-bot-250207106980.us-central1.run.app")
    logger.info(f"Puerto del servidor: {port}")
    logger.info(f"URL del servicio: {service_url}")
    logger.info(f"Configura el webhook en Twilio con: {service_url}/whatsapp")
    logger.info("Iniciando servidor Flask...")
    app.run(host='0.0.0.0', port=port, debug=True)
    logger.info(f"Servidor Flask iniciado en el puerto {port}.")

import os
import logging
import sys
import json
import time
from flask import Flask, request
from twilio.rest import Client
from datetime import datetime, timedelta
import bot_config
import utils
import message_handler
from google.cloud import storage

# Configuration Sections
WHATSAPP_SENDER_NUMBER = "whatsapp:+18188732305"
# Lista de números de gerentes (solo el número en formato E.164, con prefijo +)
GERENTE_NUMBERS = ["+5218110665094"]  # Número del gerente corregido
GERENTE_ROLE = bot_config.GERENTE_ROLE
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GCS_BUCKET_NAME = "giselle-projects"
GCS_BASE_PATH = "PROYECTOS"
GCS_CONVERSATIONS_PATH = "CONVERSATIONS"
STATE_FILE = "conversation_state.json"
FAQ_RESPONSE_DELAY = 30  # 30 seconds delay for FAQ response
FAQ_RESPONSE_PREFIX = "respuestafaq:"  # Prefix for gerente FAQ responses
DEFAULT_PORT = 8080

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
client = None
try:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.warning("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set in environment variables. Twilio client will not be initialized.")
    else:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        logger.info("Twilio client initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Twilio client: {str(e)}")
    client = None

# Initialize OpenAI client (will be used in message_handler)
if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY not set in environment variables. Some functionality may not work.")

# Global conversation state
conversation_state = {}

def handle_gerente_message(phone, incoming_msg):
    """Handle messages from the gerente."""
    logger.info(f"Handling message from gerente ({phone})")

    # Check if the gerente is requesting a report of interested clients
    if "reporte de interesados" in incoming_msg.lower():
        logger.info(f"Gerente ({phone}) requested a report of interested clients")
        report_messages = utils.generate_interested_report(conversation_state)
        utils.send_consecutive_messages(phone, report_messages, client, WHATSAPP_SENDER_NUMBER)
        logger.debug(f"Sent report to gerente: {report_messages}")
        return "Reporte enviado", 200

    # Process the gerente's response to a client's question
    client_phone, gerente_messages = message_handler.handle_gerente_response(incoming_msg, phone, conversation_state, GCS_BUCKET_NAME)
    if gerente_messages:
        logger.debug(f"Gerente response received from {phone}. Sending response to client {client_phone}: {gerente_messages}")
        if client_phone:
            # Send the response to the client
            utils.send_consecutive_messages(client_phone, gerente_messages, client, WHATSAPP_SENDER_NUMBER)
            # Update the client's conversation history
            if client_phone in conversation_state:
                conversation_state[client_phone]['history'].append(f"Giselle: {gerente_messages[0]}")
                conversation_state[client_phone]['pending_response_time'] = None
                logger.debug(f"Updated client {client_phone} history: {conversation_state[client_phone]['history']}")
                # Save state to GCS
                utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                utils.save_conversation_history(client_phone, conversation_state[client_phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                utils.save_client_info(client_phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            else:
                logger.warning(f"Client {client_phone} not found in conversation_state after gerente response.")
        else:
            logger.error("Failed to find client phone to send gerente response.")
    else:
        logger.debug(f"No valid gerente response to process: {incoming_msg}")
        # Simply return without further processing
        return "Mensaje recibido", 200

    return "Mensaje enviado", 200

def handle_client_message(phone, incoming_msg, num_media, media_url=None):
    """Handle messages from clients."""
    logger.info(f"Handling message from client ({phone})")

    # Load conversation history with error handling
    try:
        history = utils.load_conversation_history(phone, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        logger.debug("Conversation history loaded")
    except Exception as e:
        logger.error(f"Failed to load conversation history: {str(e)}")
        history = []

    # Update conversation state for the client
    conversation_state[phone]['history'] = history
    conversation_state[phone]['history'].append(f"Cliente: {incoming_msg}")
    conversation_state[phone]['history'] = conversation_state[phone]['history'][-10:]
    conversation_state[phone]['last_contact'] = datetime.now().isoformat()
    conversation_state[phone]['messages_since_budget_ask'] += 1

    # Check for pending responses (client side)
    if conversation_state[phone].get('pending_response_time'):
        current_time = time.time()
        elapsed_time = current_time - conversation_state[phone]['pending_response_time']
        if elapsed_time >= FAQ_RESPONSE_DELAY:
            # Enough time has passed; fetch the response from FAQ
            question = conversation_state[phone].get('pending_question', {}).get('question')
            mentioned_project = conversation_state[phone].get('pending_question', {}).get('mentioned_project')
            if question:
                logger.debug(f"Fetching FAQ answer for question '{question}' about project '{mentioned_project}'")
                answer = utils.get_faq_answer(question, mentioned_project)
                if answer:
                    messages = [f"Gracias por esperar. Sobre tu pregunta: {answer}"]
                    utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
                    conversation_state[phone]['history'].append(f"Giselle: {messages[0]}")
                    conversation_state[phone]['pending_question'] = None
                    conversation_state[phone]['pending_response_time'] = None
                    logger.debug(f"Sent gerente response to client {phone}: {messages}")
                else:
                    logger.error(f"Could not find answer for question '{question}' in FAQ.")
                    messages = ["Lo siento, no pude encontrar una respuesta. ¿En qué más puedo ayudarte?"]
                    utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
                    conversation_state[phone]['history'].append(f"Giselle: {messages[0]}")
                    conversation_state[phone]['pending_question'] = None
                    conversation_state[phone]['pending_response_time'] = None
            else:
                logger.error(f"No pending question found for {phone} despite pending_response_time.")
                conversation_state[phone]['pending_response_time'] = None
        else:
            logger.debug(f"Waiting for FAQ response delay to complete for {phone}. Elapsed time: {elapsed_time} seconds")
            utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            return "Waiting for gerente response", 200

    # Check for client name in the message
    if "mi nombre es" in incoming_msg.lower():
        name = incoming_msg.lower().split("mi nombre es")[-1].strip()
        conversation_state[phone]['client_name'] = name.capitalize()
        logger.info(f"Client name set to: {conversation_state[phone]['client_name']}")
        utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
    elif conversation_state[phone].get('name_asked', 0) > 0 and not conversation_state[phone].get('client_name'):
        name = incoming_msg.strip()
        if name and name.lower() != 'hola':
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
        return "Mensaje enviado", 200

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
        return "Mensaje enviado", 200

    # Prepare project information
    project_info = ""
    try:
        for project, data in utils.projects_data.items():
            project_info += f"Proyecto: {project}\n"
            project_info += "Es un desarrollo que creo que te va a interesar.\n"
            project_info += "\n"
            if project.lower() in incoming_msg.lower():
                conversation_state[phone]['last_mentioned_project'] = project
    except Exception as project_info_e:
        logger.error(f"Error preparing project information: {str(project_info_e)}")
        project_info = "Información de proyectos no disponible."

    # Build conversation history
    conversation_history = "\n".join(conversation_state[phone]['history'])

    # Check FAQ for an existing answer
    mentioned_project = conversation_state[phone].get('last_mentioned_project')
    faq_answer = utils.get_faq_answer(incoming_msg, mentioned_project)
    if faq_answer:
        messages = [f"Según lo que ya hemos investigado: {faq_answer}"]
    else:
        # Process the message and generate a response
        messages, mentioned_project = message_handler.process_message(
            incoming_msg, phone, conversation_state, project_info, conversation_history
        )
        logger.debug(f"Messages generated: {messages}")
        logger.debug(f"Mentioned project after processing: {mentioned_project}")

        # If the bot needs to contact the gerente
        if "Permíteme, déjame revisar esto con el gerente." in messages:
            conversation_state[phone]['pending_question'] = {
                'question': incoming_msg,
                'mentioned_project': mentioned_project,
                'client_phone': phone  # Store the client phone explicitly
            }
            logger.debug(f"Set pending question for {phone}: {conversation_state[phone]['pending_question']}")
        else:
            logger.debug(f"No gerente contact needed for message: {incoming_msg}")

    # Update the last mentioned project in conversation state
    if mentioned_project:
        conversation_state[phone]['last_mentioned_project'] = mentioned_project
        logger.debug(f"Updated last_mentioned_project to: {mentioned_project}")

    utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)

    for msg in messages:
        conversation_state[phone]['history'].append(f"Giselle: {msg}")
    conversation_state[phone]['history'] = conversation_state[phone]['history'][-10:]

    utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
    utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
    utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)

    logger.debug("Returning success response")
    return "Mensaje enviado", 200

@app.route('/whatsapp', methods=['POST'])
def whatsapp():
    logger.debug("Entered /whatsapp route")
    try:
        if client is None:
            logger.error("Twilio client not initialized. Cannot process WhatsApp messages.")
            return "Error: Twilio client not initialized", 500

        # Reload conversation state from GCS
        utils.load_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        logger.debug("Conversation state reloaded")

        # Log request data for debugging
        logger.debug(f"Request headers: {dict(request.headers)}")
        logger.debug(f"Request form data: {request.form}")
        logger.debug(f"Request values: {dict(request.values)}")

        # Extract phone, message, and media details
        logger.debug("Extracting message content")
        phone = request.values.get('From', '')
        incoming_msg = request.values.get('Body', '').strip()
        num_media = int(request.values.get('NumMedia', '0'))
        media_url = request.values.get('MediaUrl0', None) if num_media > 0 else None

        logger.debug(f"From phone: {phone}, Message: {incoming_msg}, NumMedia: {num_media}, MediaUrl: {media_url}")

        if not phone:
            logger.error("No se encontró 'From' en la solicitud")
            return "Error: Solicitud incompleta", 400

        # Primer candado: Identificar al gerente usando el número completo (From)
        # Normalizar el número eliminando el prefijo whatsapp: y asegurando el formato
        normalized_phone = phone.replace("whatsapp:", "").strip()
        is_gerente = normalized_phone in GERENTE_NUMBERS
        logger.debug(f"Comparando número: phone='{phone}', normalized_phone='{normalized_phone}', GERENTE_NUMBERS={GERENTE_NUMBERS}, is_gerente={is_gerente}")

        # Si es el gerente, inicializar su estado y dirigirlo al manejador de gerente
        if is_gerente:
            logger.info(f"Identificado como gerente: {phone}")
            if phone not in conversation_state:
                conversation_state[phone] = {
                    'history': [],
                    'is_gerente': True,
                    'last_contact': datetime.now().isoformat(),
                    'last_incoming_time': datetime.now().isoformat()
                }
            else:
                conversation_state[phone]['is_gerente'] = True

            # Si el gerente envía un mensaje de texto, procesarlo
            if incoming_msg:
                return handle_gerente_message(phone, incoming_msg)
            # Si el gerente envía un audio, procesarlo
            elif num_media > 0 and media_url:
                error_messages, transcribed_msg = message_handler.handle_audio_message(
                    media_url, phone, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
                )
                if error_messages:
                    utils.send_consecutive_messages(phone, error_messages, client, WHATSAPP_SENDER_NUMBER)
                    return "Error procesando audio", 200
                if transcribed_msg:
                    return handle_gerente_message(phone, transcribed_msg)
            else:
                logger.error("Mensaje del gerente sin contenido de texto o audio")
                return "Error: Mensaje sin contenido", 400

        # Si no es el gerente, tratar como cliente
        else:
            logger.info(f"Identificado como cliente: {phone}")
            # Inicializar estado de cliente si no existe
            if phone not in conversation_state:
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
                    'project_info_shared': {},
                    'last_mentioned_project': None,
                    'pending_question': None,
                    'pending_response_time': None,
                    'is_gerente': False
                }

            # Si el cliente envía un mensaje de texto, procesarlo
            if incoming_msg:
                return handle_client_message(phone, incoming_msg, num_media, media_url)
            # Si el cliente envía un audio, procesarlo
            elif num_media > 0 and media_url:
                error_messages, transcribed_msg = message_handler.handle_audio_message(
                    media_url, phone, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
                )
                if error_messages:
                    utils.send_consecutive_messages(phone, error_messages, client, WHATSAPP_SENDER_NUMBER)
                    return "Error procesando audio", 200
                if transcribed_msg:
                    return handle_client_message(phone, transcribed_msg, num_media=0, media_url=None)
            else:
                logger.error("Mensaje del cliente sin contenido de texto o audio")
                return "Error: Mensaje sin contenido", 400

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
            if not conversation_state[phone].get('is_gerente', False):
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
        if state.get('is_gerente', False):  # Skip gerente
            continue
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
    try:
        logger.info("Starting application initialization...")
        utils.load_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_BASE_PATH)
        logger.info("Conversation state loaded")
        utils.download_projects_from_storage(GCS_BUCKET_NAME, GCS_BASE_PATH)
        logger.info("Projects downloaded from storage")
        utils.load_projects_from_folder(GCS_BASE_PATH)
        logger.info("Projects loaded from folder")
        utils.load_gerente_respuestas(GCS_BASE_PATH)
        logger.info("Gerente responses loaded")
        utils.load_faq_files(GCS_BASE_PATH)
        logger.info("FAQ files loaded")
        message_handler.initialize_message_handler(
            OPENAI_API_KEY, utils.projects_data, utils.downloadable_urls, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
        )
        logger.info("Message handler initialized")
        port = int(os.getenv("PORT", DEFAULT_PORT))
        service_url = os.getenv("SERVICE_URL", f"https://giselle-bot-250207106980.us-central1.run.app")
        logger.info(f"Puerto del servidor: {port}")
        logger.info(f"URL del servicio: {service_url}")
        logger.info(f"Configura el webhook en Twilio con: {service_url}/whatsapp")
        logger.info("Iniciando servidor Flask...")
        app.run(host='0.0.0.0', port=port, debug=False)
        logger.info(f"Servidor Flask iniciado en el puerto {port}.")
    except Exception as e:
        logger.error(f"Failed to start application: {str(e)}")
        sys.exit(1)

import os
import logging
import sys
from flask import Flask, request
from twilio.rest import Client
from datetime import datetime, timedelta
import bot_config
import utils
import message_handler
from google.cloud import storage

# Configuration Section
WHATSAPP_SENDER_NUMBER = "whatsapp:+18188732305"
GERENTE_PHONE = bot_config.GERENTE_PHONE
GERENTE_ROLE = bot_config.GERENTE_ROLE
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GCS_BUCKET_NAME = "giselle-projects"
GCS_BASE_PATH = "PROYECTOS"
GCS_CONVERSATIONS_PATH = "CONVERSATIONS"
STATE_FILE = "conversation_state.json"
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

# Health check endpoint for Cloud Run
@app.route('/health', methods=['GET'])
def health():
    logger.debug("Health check endpoint called")
    return "Healthy", 200

# Routes
@app.route('/whatsapp', methods=['POST'])
def whatsapp():
    logger.debug("Entered /whatsapp route")
    try:
        if client is None:
            logger.error("Twilio client not initialized. Cannot process WhatsApp messages.")
            return "Error: Twilio client not initialized", 500

        # Reload conversation state from GCS to ensure we have the latest state
        utils.load_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        logger.debug(f"Conversation state reloaded: {conversation_state}")

        # Log the entire request data for debugging
        logger.debug(f"Request headers: {dict(request.headers)}")
        logger.debug(f"Request form data: {request.form}")
        logger.debug(f"Request values: {dict(request.values)}")

        logger.debug("Extracting message content")
        num_media = int(request.values.get('NumMedia', '0'))
        logger.debug(f"Number of media items: {num_media}")
        phone = request.values.get('From', '')
        logger.debug(f"From phone: {phone}")

        # Normalize the phone number early
        phone = phone.strip()
        if not phone.startswith('whatsapp:+'):
            if phone.startswith('whatsapp:'):
                phone = f"whatsapp:+{phone[len('whatsapp:'):]}"
            else:
                phone = f"whatsapp:+{phone}"

        if not phone.startswith('whatsapp:+'):
            logger.error(f"Invalid phone number format after normalization: {repr(phone)}")
            return "Error: Invalid phone number format", 400

        # Check if the message is from the gerente
        incoming_msg = request.values.get('Body', '').strip()
        logger.debug(f"Processing message from {phone}: {incoming_msg}")
        is_gerente = phone == GERENTE_PHONE
        logger.debug(f"Is sender the gerente? {is_gerente} (Role: {GERENTE_ROLE}, Phone: {GERENTE_PHONE})")

        if is_gerente:
            logger.info(f"Message identified as coming from gerente ({phone})")
            client_phone, gerente_messages = message_handler.handle_gerente_response(incoming_msg, phone, conversation_state, GCS_BUCKET_NAME)
            if gerente_messages:
                logger.debug(f"Gerente response received from {phone}. Sending response to client {client_phone}: {gerente_messages}")
                if client_phone:
                    # Send the response to the client
                    utils.send_consecutive_messages(client_phone, gerente_messages, client, WHATSAPP_SENDER_NUMBER)
                    # Update the client's conversation history
                    if client_phone in conversation_state:
                        conversation_state[client_phone]['history'].append(f"Giselle: {gerente_messages[0]}")
                        logger.debug(f"Updated client {client_phone} history: {conversation_state[client_phone]['history']}")
                    else:
                        logger.warning(f"Client {client_phone} not found in conversation_state after gerente response.")
                    # Save the updated conversation state immediately
                    utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                    if client_phone in conversation_state:
                        utils.save_conversation_history(client_phone, conversation_state[client_phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                        utils.save_client_info(client_phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                else:
                    logger.error("Failed to find client phone to send gerente response.")
            else:
                logger.warning(f"No messages generated from gerente response for {phone}")
            # Stop further processing since this is a gerente response
            logger.debug(f"Completed gerente response handling for {phone}. Exiting request.")
            return "Mensaje enviado"

        # Skip client state initialization for the gerente
        if phone == GERENTE_PHONE:
            logger.debug(f"Message from gerente {phone}, skipping client processing after gerente response handling.")
            return "Mensaje enviado"

        # Check if the message contains audio
        if num_media > 0:
            media_url = request.values.get('MediaUrl0', '')
            media_content_type = request.values.get('MediaContentType0', '')
            logger.debug(f"Media detected: {media_url}, Content-Type={media_content_type}")

            if 'audio' in media_content_type.lower():
                logger.debug("Processing audio message")
                messages, incoming_msg = message_handler.handle_audio_message(
                    media_url, phone, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
                )
                if messages:
                    logger.debug(f"Audio message processing returned messages: {messages}")
                    utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
                    utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                    utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                    utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                    return "Mensaje enviado"
                elif incoming_msg:
                    logger.debug(f"Audio transcribed to text: {incoming_msg}")
                    # Continue processing the transcribed text as a regular message
                    incoming_msg = incoming_msg.strip()
                else:
                    logger.error("Audio processing failed with no messages or transcription")
                    messages = ["Lo siento, no pude procesar tu mensaje de audio. ¿Puedes intentarlo de nuevo o escribirlo como texto?"]
                    utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
                    utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                    utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                    utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                    return "Mensaje enviado"
            else:
                logger.debug(f"Media type is not audio: {media_content_type}")
                messages = ["Lo siento, solo puedo procesar mensajes de texto o audio. ¿Puedes enviar tu mensaje de otra forma?"]
                utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
                utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                return "Mensaje enviado"
        else:
            logger.debug("No media detected, processing as text message")
            incoming_msg = request.values.get('Body', '').strip()

        logger.debug(f"Incoming message: {incoming_msg}, Phone: {phone}")

        if not incoming_msg or not phone:
            logger.error("No se encontraron 'Body' o 'From' en la solicitud")
            return "Error: Solicitud incompleta", 400

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
                    'project_info_shared': {},
                    'last_mentioned_project': None,
                    'pending_question': None
                }
            else:
                # Preserve pending_question if it exists
                existing_state = conversation_state[phone]
                if 'pending_question' not in existing_state:
                    existing_state['pending_question'] = None
                conversation_state[phone] = {
                    'history': history,
                    'name_asked': existing_state.get('name_asked', 0),
                    'budget_asked': existing_state.get('budget_asked', 0),
                    'contact_time_asked': existing_state.get('contact_time_asked', 0),
                    'messages_since_budget_ask': existing_state.get('messages_since_budget_ask', 0),
                    'messages_without_response': 0,
                    'preferred_time': existing_state.get('preferred_time'),
                    'preferred_days': existing_state.get('preferred_days'),
                    'client_name': existing_state.get('client_name'),
                    'client_budget': existing_state.get('client_budget'),
                    'last_contact': existing_state.get('last_contact', datetime.now().isoformat()),
                    'recontact_attempts': existing_state.get('recontact_attempts', 0),
                    'no_interest': existing_state.get('no_interest', False),
                    'schedule_next': existing_state.get('schedule_next'),
                    'last_incoming_time': datetime.now().isoformat(),
                    'introduced': existing_state.get('introduced', False),
                    'project_info_shared': existing_state.get('project_info_shared', {}),
                    'last_mentioned_project': existing_state.get('last_mentioned_project'),
                    'pending_question': existing_state.get('pending_question')
                }
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
                'project_info_shared': {},
                'last_mentioned_project': None,
                'pending_question': None
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
        elif conversation_state[phone].get('name_asked', 0) > 0 and not conversation_state[phone].get('client_name'):
            name = incoming_msg.strip()
            if name and name.lower() != 'hola':  # Prevent setting 'hola' as the name
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

        # Process the message and generate a response
        messages, mentioned_project = message_handler.process_message(
            incoming_msg, phone, conversation_state, project_info, conversation_history
        )
        logger.debug(f"Messages generated: {messages}")
        logger.debug(f"Mentioned project after processing: {mentioned_project}")

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
    try:
        logger.info("Starting application initialization...")
        utils.load_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        logger.info("Conversation state loaded")
        # Clear any existing state for the gerente's number
        if GERENTE_PHONE in conversation_state:
            del conversation_state[GERENTE_PHONE]
            logger.info(f"Cleared conversation state for gerente phone: {GERENTE_PHONE}")

        # Delete gerente's conversation history and client info files from GCS
        storage_client = storage.Client()
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        # Delete conversation history file
        conversation_filename = utils.get_conversation_history_filename(GERENTE_PHONE)
        conversation_blob_name = os.path.join(GCS_CONVERSATIONS_PATH, conversation_filename)
        conversation_blob = bucket.blob(conversation_blob_name)
        if conversation_blob.exists():
            conversation_blob.delete()
            logger.info(f"Deleted gerente conversation history from GCS: {conversation_blob_name}")
        # Delete client info file
        client_info_filename = utils.get_client_info_filename(GERENTE_PHONE)
        client_info_blob_name = os.path.join(GCS_CONVERSATIONS_PATH, client_info_filename)
        client_info_blob = bucket.blob(client_info_blob_name)
        if client_info_blob.exists():
            client_info_blob.delete()
            logger.info(f"Deleted gerente client info from GCS: {client_info_blob_name}")

        utils.download_projects_from_storage(GCS_BUCKET_NAME, GCS_BASE_PATH)
        logger.info("Projects downloaded from storage")
        utils.load_projects_from_folder(GCS_BASE_PATH)
        logger.info("Projects loaded from folder")
        utils.load_gerente_respuestas(GCS_BASE_PATH)
        logger.info("Gerente responses loaded")
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
        app.run(host='0.0.0.0', port=port, debug=False)  # Disable debug mode for production
        logger.info(f"Servidor Flask iniciado en el puerto {port}.")
    except Exception as e:
        logger.error(f"Failed to start application: {str(e)}")
        sys.exit(1)

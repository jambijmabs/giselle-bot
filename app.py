import os
import logging
import sys
import json
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
PENDING_QUESTIONS_FILE = "pending_questions.json"
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

# Pending questions state (client_phone -> question details)
pending_questions = {}

# Initialize Google Cloud Storage client for pending questions
storage_client = storage.Client()
bucket = storage_client.bucket(GCS_BUCKET_NAME)

def load_pending_questions():
    """Load pending questions from GCS."""
    try:
        local_file = "/tmp/pending_questions.json"
        blob_name = os.path.join(GCS_CONVERSATIONS_PATH, PENDING_QUESTIONS_FILE)
        blob = bucket.blob(blob_name)
        if blob.exists():
            blob.download_to_filename(local_file)
            with open(local_file, 'r') as f:
                data = json.load(f)
            logger.info(f"Loaded pending questions from GCS: {data}")
            return data
        else:
            logger.info("No pending questions file found in GCS; starting fresh")
            return {}
    except Exception as e:
        logger.error(f"Error loading pending questions: {str(e)}")
        return {}

def save_pending_questions(pending_questions_data):
    """Save pending questions to GCS."""
    try:
        local_file = "/tmp/pending_questions.json"
        blob_name = os.path.join(GCS_CONVERSATIONS_PATH, PENDING_QUESTIONS_FILE)
        with open(local_file, 'w') as f:
            json.dump(pending_questions_data, f)
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(local_file)
        logger.info(f"Saved pending questions to GCS: {pending_questions_data}")
    except Exception as e:
        logger.error(f"Error saving pending questions: {str(e)}")

# Health check endpoint for Cloud Run
@app.route('/health', methods=['GET'])
def health():
    logger.debug("Health check endpoint called")
    return "Healthy", 200

# Endpoint for client messages
@app.route('/whatsapp', methods=['POST'])
def whatsapp():
    logger.debug("Entered /whatsapp route")
    try:
        if client is None:
            logger.error("Twilio client not initialized. Cannot process WhatsApp messages.")
            return "Error: Twilio client not initialized", 500

        # Reload conversation state and pending questions from GCS
        utils.load_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        global pending_questions
        pending_questions = load_pending_questions()
        logger.debug(f"Conversation state reloaded: {conversation_state}")
        logger.debug(f"Pending questions reloaded: {pending_questions}")

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

        # Ensure this endpoint does not process gerente messages
        if phone == GERENTE_PHONE:
            logger.warning(f"Gerente message received on /whatsapp endpoint: {phone}. Redirecting to /gerente.")
            return "Message should be sent to /gerente endpoint", 400

        incoming_msg = request.values.get('Body', '').strip()
        logger.debug(f"Processing message from client {phone}: {incoming_msg}")

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
            history = []

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
                    'last_mentioned_project': None
                }
            else:
                existing_state = conversation_state[phone]
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
                    'last_mentioned_project': existing_state.get('last_mentioned_project')
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
                'last_mentioned_project': None
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

        # Check if the bot needs to contact the gerente
        if "Permíteme, déjame revisar esto con el gerente." in messages:
            pending_questions[phone] = {
                'question': incoming_msg,
                'mentioned_project': mentioned_project
            }
            save_pending_questions(pending_questions)
            logger.debug(f"Added pending question for {phone}: {pending_questions[phone]}")
        else:
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

# Endpoint for gerente messages
@app.route('/gerente', methods=['POST'])
def gerente():
    logger.debug("Entered /gerente route")
    try:
        if client is None:
            logger.error("Twilio client not initialized. Cannot process WhatsApp messages.")
            return "Error: Twilio client not initialized", 500

        # Reload pending questions from GCS
        global pending_questions
        pending_questions = load_pending_questions()
        logger.debug(f"Pending questions reloaded: {pending_questions}")

        # Log the entire request data for debugging
        logger.debug(f"Request headers: {dict(request.headers)}")
        logger.debug(f"Request form data: {request.form}")
        logger.debug(f"Request values: {dict(request.values)}")

        logger.debug("Extracting message content")
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

        if phone != GERENTE_PHONE:
            logger.warning(f"Non-gerente message received on /gerente endpoint: {phone}. Redirecting to /whatsapp.")
            return "Message should be sent to /whatsapp endpoint", 400

        incoming_msg = request.values.get('Body', '').strip()
        logger.debug(f"Processing message from gerente {phone}: {incoming_msg}")

        if not incoming_msg:
            logger.error("No se encontró 'Body' en la solicitud")
            return "Error: Solicitud incompleta", 400

        logger.info(f"Mensaje recibido de gerente {phone}: {incoming_msg}")

        # Find a pending question to match this response
        client_phone = None
        question_details = None
        for c_phone, details in list(pending_questions.items()):
            client_phone = c_phone
            question_details = details
            break

        if not client_phone or not question_details:
            logger.error("No pending questions found to match gerente response.")
            return "No pending questions to respond to", 400

        # Store the gerente's response
        question = question_details['question']
        mentioned_project = question_details['mentioned_project']
        utils.save_gerente_respuesta(
            "PROYECTOS",
            question,
            incoming_msg,
            GCS_BUCKET_NAME
        )
        logger.debug(f"Saved gerente response for question '{question}' with answer '{incoming_msg}'")

        # Update the global gerente_respuestas
        utils.gerente_respuestas[question] = incoming_msg
        logger.debug(f"Updated gerente_respuestas: {utils.gerente_respuestas}")

        # Prepare and send response to the client
        messages = [f"Gracias por esperar. Sobre tu pregunta: {incoming_msg}"]
        logger.debug(f"Sending gerente response to client {client_phone}: {messages}")
        utils.send_consecutive_messages(client_phone, messages, client, WHATSAPP_SENDER_NUMBER)

        # Update the client's conversation history
        utils.load_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        if client_phone in conversation_state:
            conversation_state[client_phone]['history'].append(f"Giselle: {messages[0]}")
            utils.save_conversation_history(client_phone, conversation_state[client_phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_client_info(client_phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        else:
            logger.warning(f"Client {client_phone} not found in conversation_state; history not updated.")

        # Remove the pending question
        del pending_questions[client_phone]
        save_pending_questions(pending_questions)
        logger.debug(f"Removed pending question for {client_phone}. Updated pending_questions: {pending_questions}")

        logger.debug("Gerente response processed successfully")
        return "Mensaje enviado"
    except Exception as e:
        logger.error(f"Error inesperado en /gerente: {str(e)}", exc_info=True)
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
        pending_questions = load_pending_questions()
        logger.info("Pending questions loaded")

        # Delete gerente's conversation history and client info files from GCS
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        conversation_filename = utils.get_conversation_history_filename(GERENTE_PHONE)
        conversation_blob_name = os.path.join(GCS_CONVERSATIONS_PATH, conversation_filename)
        conversation_blob = bucket.blob(conversation_blob_name)
        if conversation_blob.exists():
            conversation_blob.delete()
            logger.info(f"Deleted gerente conversation history from GCS: {conversation_blob_name}")
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
        logger.info(f"Configura el webhook en Twilio para clientes con: {service_url}/whatsapp")
        logger.info(f"Configura el webhook en Twilio para gerente con: {service_url}/gerente")
        logger.info("Iniciando servidor Flask...")
        app.run(host='0.0.0.0', port=port, debug=False)
        logger.info(f"Servidor Flask iniciado en el puerto {port}.")
    except Exception as e:
        logger.error(f"Failed to start application: {str(e)}")
        sys.exit(1)

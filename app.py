import os
import logging
import sys
import json
import re
import requests
from flask import Flask, request
from twilio.rest import Client
from google.cloud import storage
from openai import OpenAI
from datetime import datetime, timedelta
import time
import bot_config

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
CHATGPT_MODEL = "gpt-4.1-mini"  # Define the ChatGPT model here

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

# Initialize OpenAI (ChatGPT) client
if not OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY not set in environment variables")
    raise ValueError("OPENAI_API_KEY not set")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Initialize Google Cloud Storage client
storage_client = storage.Client()

# Global dictionaries for project data and conversation state
projects_data = {}
downloadable_links = {}
downloadable_urls = {}  # Dictionary for URLs from DESCARGABLES.TXT
conversation_state = {}
downloadable_files = {}

# Helper Functions
def get_conversation_history_filename(phone):
    """Generate the filename for conversation history based on phone number."""
    return f"{phone.replace('+', '').replace(':', '_')}_conversation.txt"

def get_client_info_filename(phone):
    """Generate the filename for client info based on phone number."""
    return f"client_info_{phone.replace('+', '').replace(':', '_')}.txt"

def upload_to_gcs(bucket_name, source_file_path, destination_blob_name):
    """Upload a file to Google Cloud Storage."""
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(destination_blob_name)
    blob.upload_from_filename(source_file_path)
    logger.info(f"Uploaded {source_file_path} to GCS as {destination_blob_name}")

def download_from_gcs(bucket_name, source_blob_name, destination_file_path):
    """Download a file from Google Cloud Storage."""
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(source_blob_name)
    blob.download_to_filename(destination_file_path)
    logger.info(f"Downloaded {source_blob_name} from GCS to {destination_file_path}")

def load_conversation_state():
    """Load conversation state from file in GCS."""
    global conversation_state
    try:
        local_state_file = "/tmp/conversation_state.json"
        destination_blob_name = os.path.join(GCS_CONVERSATIONS_PATH, "conversation_state.json")
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(destination_blob_name)
        if blob.exists():
            blob.download_to_filename(local_state_file)
            with open(local_state_file, 'r') as f:
                conversation_state = json.load(f)
            logger.info("Conversation state loaded from GCS")
        else:
            conversation_state = {}
            logger.info("No conversation state file found in GCS; starting fresh")
    except Exception as e:
        logger.error(f"Error loading conversation state: {str(e)}")
        conversation_state = {}

def save_conversation_state():
    """Save conversation state to file in GCS."""
    try:
        local_state_file = "/tmp/conversation_state.json"
        destination_blob_name = os.path.join(GCS_CONVERSATIONS_PATH, "conversation_state.json")
        with open(local_state_file, 'w') as f:
            json.dump(conversation_state, f)
        upload_to_gcs(GCS_BUCKET_NAME, local_state_file, destination_blob_name)
        logger.info("Conversation state saved to GCS")
    except Exception as e:
        logger.error(f"Error saving conversation state: {str(e)}")

def load_conversation_history(phone):
    """Load conversation history from file in GCS."""
    filename = get_conversation_history_filename(phone)
    destination_blob_name = os.path.join(GCS_CONVERSATIONS_PATH, filename)
    local_file_path = f"/tmp/{filename}"
    try:
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(destination_blob_name)
        if blob.exists():
            blob.download_to_filename(local_file_path)
            with open(local_file_path, 'r', encoding='utf-8') as f:
                history = f.read().strip().split('\n')
            logger.info(f"Loaded conversation history for {phone} from GCS")
            return history
        return []
    except Exception as e:
        logger.error(f"Error loading conversation history for {phone}: {str(e)}")
        return []

def save_conversation_history(phone, history):
    """Save conversation history to file in GCS."""
    filename = get_conversation_history_filename(phone)
    destination_blob_name = os.path.join(GCS_CONVERSATIONS_PATH, filename)
    local_file_path = f"/tmp/{filename}"
    try:
        with open(local_file_path, 'w', encoding='utf-8') as f:
            for msg in history:
                f.write(f"{msg}\n")
        upload_to_gcs(GCS_BUCKET_NAME, local_file_path, destination_blob_name)
        logger.info(f"Saved conversation history for {phone} to GCS")
    except Exception as e:
        logger.error(f"Error saving conversation history for {phone}: {str(e)}")

def save_client_info(phone):
    """Save client information to a text file in GCS."""
    filename = get_client_info_filename(phone)
    destination_blob_name = os.path.join(GCS_CONVERSATIONS_PATH, filename)
    local_file_path = f"/tmp/{filename}"
    try:
        client_info = conversation_state.get(phone, {})
        name = client_info.get('client_name', 'No proporcionado')
        budget = client_info.get('client_budget', 'No proporcionado')
        preferred_days = client_info.get('preferred_days', 'No proporcionado')
        preferred_time = client_info.get('preferred_time', 'No proporcionado')
        
        with open(local_file_path, 'w', encoding='utf-8') as f:
            f.write(f"Información del Cliente: {phone}\n")
            f.write(f"Nombre: {name}\n")
            f.write(f"Presupuesto: {budget}\n")
            f.write(f"Días Preferidos: {preferred_days}\n")
            f.write(f"Horario Preferido: {preferred_time}\n")
        upload_to_gcs(GCS_BUCKET_NAME, local_file_path, destination_blob_name)
        logger.info(f"Saved client info for {phone} to GCS")
    except Exception as e:
        logger.error(f"Error saving client info for {phone}: {str(e)}")

def download_projects_from_storage(bucket_name=GCS_BUCKET_NAME, base_path=GCS_BASE_PATH):
    """Download project files from Google Cloud Storage."""
    try:
        if not os.path.exists(base_path):
            os.makedirs(base_path)
            logger.debug(f"Created directory {base_path}")

        bucket = storage_client.bucket(bucket_name)
        blobs = bucket.list_blobs(prefix=base_path)

        for blob in blobs:
            local_path = blob.name
            if not os.path.exists(os.path.dirname(local_path)):
                os.makedirs(os.path.dirname(local_path))
            blob.download_to_filename(local_path)
            logger.info(f"Descargado archivo desde Cloud Storage: {local_path}")
    except Exception as e:
        logger.error(f"Error downloading projects from Cloud Storage: {str(e)}", exc_info=True)
        raise

def extract_text_from_txt(txt_path):
    """Extract text from .txt files."""
    try:
        with open(txt_path, 'r', encoding='utf-8') as file:
            text = file.read()
        logger.info(f"Archivo de texto {txt_path} leído correctamente.")
        return text
    except Exception as e:
        logger.error(f"Error al leer archivo de texto {txt_path}: {str(e)}", exc_info=True)
        return ""

def load_downloadable_urls(project):
    """Load downloadable URLs from DESCARGABLES.TXT."""
    descargables_file = os.path.join(GCS_BASE_PATH, project, "DESCARGABLES", "DESCARGABLES.TXT")
    urls = {}
    try:
        if os.path.exists(descargables_file):
            logger.debug(f"Found DESCARGABLES.TXT for project {project} at {descargables_file}")
            with open(descargables_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                logger.debug(f"Lines in DESCARGABLES.TXT: {lines}")
                for line in lines:
                    line = line.strip()
                    if line and ':' in line:
                        key, url = line.split(':', 1)
                        key = key.strip().lower()
                        url = url.strip()
                        urls[key] = url
            logger.info(f"Loaded downloadable URLs for {project}: {urls}")
        else:
            logger.warning(f"DESCARGABLES.TXT not found for project {project} at {descargables_file}")
        return urls
    except Exception as e:
        logger.error(f"Error loading DESCARGABLES.TXT for {project}: {str(e)}")
        return {}

def load_projects_from_folder(base_path=GCS_BASE_PATH):
    """Load project data from folder."""
    downloadable_files = {}

    if not os.path.exists(base_path):
        os.makedirs(base_path)
        logger.warning(f"Carpeta {base_path} creada, pero no hay proyectos.")
        return downloadable_files

    projects = [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d)) and not d.startswith('.') and d != 'DESCARGABLES']
    if not projects:
        logger.warning(f"No se encontraron proyectos en {base_path}.")
        return downloadable_files

    logger.info(f"Proyectos detectados: {', '.join(projects)}")

    for project in projects:
        downloadable_links[project] = {}
        downloadable_urls[project] = {}  # Initialize URLs dictionary
        projects_data[project] = ""

    for project in projects:
        project_path = os.path.join(base_path, project)
        file_count = 0
        txt_files = [f for f in os.listdir(project_path) if f.endswith('.txt') and os.path.isfile(os.path.join(project_path, f))]

        if not txt_files:
            logger.warning(f"No se encontraron archivos TXT para el proyecto {project}.")
            continue

        for file in txt_files:
            file_path = os.path.join(project_path, file)
            logger.info(f"Procesando archivo de texto para {project}: {file_path}")
            text = extract_text_from_txt(file_path)
            if text:
                projects_data[project] = text
                logger.info(f"Proyecto {project} procesado correctamente desde {file_path}.")
                file_count += 1

        if file_count > 0:
            logger.info(f"Proyecto {project} procesado correctamente. {file_count} archivo(s) cargado(s).")
        else:
            logger.warning(f"No se encontraron archivos TXT válidos para el proyecto {project}.")

        downloadable_path = os.path.join(project_path, 'DESCARGABLES')
        downloadable_files[project] = []
        if os.path.exists(downloadable_path):
            downloadable_count = 0
            for file in os.listdir(downloadable_path):
                if file.endswith(('.pdf', '.jpg', '.jpeg', '.png')):
                    downloadable_files[project].append(file)
                    downloadable_count += 1
            if downloadable_count > 0:
                logger.info(f"Carpeta DESCARGABLES del proyecto {project} procesada correctamente. {downloadable_count} archivo(s) encontrado(s).")
            else:
                logger.warning(f"Carpeta DESCARGABLES del proyecto {project} está vacía o no contiene archivos válidos.")
        else:
            logger.warning(f"Carpeta DESCARGABLES no encontrada para el proyecto {project}.")

        # Load URLs from DESCARGABLES.TXT
        downloadable_urls[project] = load_downloadable_urls(project)

    return downloadable_files

def send_consecutive_messages(phone, messages):
    """Send consecutive messages via Twilio."""
    for msg in messages:
        message = client.messages.create(
            from_=WHATSAPP_SENDER_NUMBER,
            body=msg,
            to=phone
        )
        logger.info(f"Mensaje enviado a través de Twilio: SID {message.sid}, Estado: {message.status}")
        updated_message = client.messages(message.sid).fetch()
        logger.info(f"Estado del mensaje actualizado: {updated_message.status}")
        if updated_message.status == "failed":
            logger.error(f"Error al enviar mensaje: {updated_message.error_code} - {updated_message.error_message}")

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
                # Download the audio file
                audio_response = requests.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
                if audio_response.status_code != 200:
                    logger.error(f"Failed to download audio: {audio_response.status_code}")
                    messages = ["Lo siento, no pude procesar tu mensaje de audio. ¿Puedes enviarlo como texto?"]
                    send_consecutive_messages(phone, messages)
                    return "Mensaje enviado"

                # Save the audio file temporarily
                audio_file_path = f"/tmp/audio_{phone.replace(':', '_')}.ogg"
                with open(audio_file_path, 'wb') as f:
                    f.write(audio_response.content)
                logger.debug(f"Audio saved to {audio_file_path}")

                # Transcribe the audio using Whisper
                try:
                    with open(audio_file_path, 'rb') as audio_file:
                        transcription = openai_client.audio.transcriptions.create(
                            model="whisper-1",
                            file=audio_file,
                            language="es"  # Assuming Spanish audio
                        )
                    incoming_msg = transcription.text.strip()
                    logger.info(f"Audio transcribed: {incoming_msg}")
                except Exception as e:
                    logger.error(f"Error transcribing audio: {str(e)}", exc_info=True)
                    messages = ["Lo siento, no pude entender tu mensaje de audio. ¿Puedes intentarlo de nuevo o escribirlo como texto?"]
                    send_consecutive_messages(phone, messages)
                    return "Mensaje enviado"
                finally:
                    # Clean up the temporary file
                    if os.path.exists(audio_file_path):
                        os.remove(audio_file_path)
            else:
                messages = ["Lo siento, solo puedo procesar mensajes de texto o audio. ¿Puedes enviar tu mensaje de otra forma?"]
                send_consecutive_messages(phone, messages)
                return "Mensaje enviado"
        else:
            incoming_msg = request.values.get('Body', '').strip()

        logger.debug(f"Incoming message: {incoming_msg}, Phone: {phone}")

        if not incoming_msg or not phone:
            logger.error("No se encontraron 'Body' o 'From' en la solicitud")
            return "Error: Solicitud incompleta", 400

        # Log the raw phone number before any processing
        logger.debug(f"Raw phone number (before strip): {repr(phone)}")

        # Strip whitespace and handle potential encoding issues
        phone = phone.strip()

        # Log the phone number after stripping
        logger.debug(f"Phone number after strip: {repr(phone)}")

        # Normalize the phone number
        if not phone.startswith('whatsapp:+'):
            if phone.startswith('whatsapp:'):
                phone = f"whatsapp:+{phone[len('whatsapp:'):]}"
            else:
                phone = f"whatsapp:+{phone}"

        # Log the normalized phone number
        logger.debug(f"Normalized phone number: {repr(phone)}")

        # Validate the phone number format
        if not phone.startswith('whatsapp:+'):
            logger.error(f"Invalid phone number format after normalization: {repr(phone)}")
            return "Error: Invalid phone number format", 400

        logger.info(f"Mensaje recibido de {phone}: {incoming_msg}")

        logger.debug("Loading conversation history")
        try:
            history = load_conversation_history(phone)
            logger.debug(f"Conversation history loaded: {history}")
        except Exception as history_e:
            logger.error(f"Error loading conversation history: {str(history_e)}", exc_info=True)
            history = []

        logger.debug("Initializing conversation state")
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
                    'project_info_shared': {}  # Track shared project info to avoid repetition
                }
            else:
                conversation_state[phone]['history'] = history
                conversation_state[phone]['messages_without_response'] = 0
                conversation_state[phone]['last_incoming_time'] = datetime.now().isoformat()
            logger.debug(f"Conversation state initialized: {conversation_state[phone]}")
        except Exception as state_e:
            logger.error(f"Error initializing conversation state: {str(state_e)}", exc_info=True)
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

        logger.debug("Updating conversation history")
        try:
            conversation_state[phone]['history'].append(f"Cliente: {incoming_msg}")
            # Limit history to 10 messages
            conversation_state[phone]['history'] = conversation_state[phone]['history'][-10:]
            logger.debug(f"Updated conversation history: {conversation_state[phone]['history']}")
        except Exception as history_update_e:
            logger.error(f"Error updating conversation history: {str(history_update_e)}", exc_info=True)

        conversation_state[phone]['last_contact'] = datetime.now().isoformat()
        conversation_state[phone]['messages_since_budget_ask'] += 1

        # Check for client name in the message
        if "mi nombre es" in incoming_msg.lower():
            name = incoming_msg.lower().split("mi nombre es")[-1].strip()
            conversation_state[phone]['client_name'] = name.capitalize()
            logger.info(f"Client name set to: {conversation_state[phone]['client_name']}")
            save_client_info(phone)

        # Check for client budget in the message
        if "mi presupuesto es" in incoming_msg.lower() or "presupuesto de" in incoming_msg.lower():
            budget = incoming_msg.lower().split("presupuesto")[-1].strip()
            conversation_state[phone]['client_budget'] = budget
            logger.info(f"Client budget set to: {budget}")
            save_client_info(phone)

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
            save_client_info(phone)

        logger.debug("Checking for no-interest phrases")
        if any(phrase in incoming_msg.lower() for phrase in bot_config.NO_INTEREST_PHRASES):
            conversation_state[phone]['no_interest'] = True
            messages = bot_config.handle_no_interest_response()
            logger.info(f"Sending no-interest response: {messages}")
            send_consecutive_messages(phone, messages)
            conversation_state[phone]['history'].append(f"Giselle: {messages[0]}")
            save_conversation_state()
            save_conversation_history(phone, conversation_state[phone]['history'])
            save_client_info(phone)
            return "Mensaje enviado"

        logger.debug("Checking for recontact request")
        recontact_response = bot_config.handle_recontact_request(incoming_msg, conversation_state[phone])
        if recontact_response:
            messages = recontact_response
            logger.info(f"Sending recontact response: {messages}")
            send_consecutive_messages(phone, messages)
            conversation_state[phone]['history'].append(f"Giselle: {messages[0]}")
            save_conversation_state()
            save_conversation_history(phone, conversation_state[phone]['history'])
            save_client_info(phone)
            return "Mensaje enviado"

        logger.debug("Preparing project information")
        project_info = ""
        mentioned_project = None
        try:
            for project, data in projects_data.items():
                # Provide minimal project info to start
                project_info += f"Proyecto: {project}\n"
                project_info += "Es un desarrollo que creo que te va a interesar.\n"
                project_info += "\n"
                if project.lower() in incoming_msg.lower():
                    mentioned_project = project
            logger.debug(f"Project info prepared: {project_info}")
        except Exception as project_info_e:
            logger.error(f"Error preparing project information: {str(project_info_e)}", exc_info=True)
            project_info = "Información de proyectos no disponible."

        logger.debug("Building conversation history")
        conversation_history = "\n".join(conversation_state[phone]['history'])
        logger.debug(f"Conversation history: {conversation_history}")

        logger.debug("Determining conversation state")
        try:
            ask_name = bot_config.should_ask_name(conversation_state[phone], conversation_history)
            ask_budget = bot_config.should_ask_budget(conversation_state[phone], conversation_history)
            ask_contact_time = bot_config.should_ask_contact_time(conversation_state[phone], conversation_history)
            logger.debug(f"Conversation state - ask_name: {ask_name}, ask_budget: {ask_budget}, ask_contact_time: {ask_contact_time}")
        except Exception as state_determination_e:
            logger.error(f"Error determining conversation state: {str(state_determination_e)}", exc_info=True)
            ask_name, ask_budget, ask_contact_time = False, False, False

        logger.debug("Checking 24-hour session window")
        last_incoming_time = datetime.fromisoformat(conversation_state[phone]['last_incoming_time'])
        time_since_last_incoming = datetime.now() - last_incoming_time
        use_template = time_since_last_incoming > timedelta(hours=24)

        logger.debug(f"Time since last incoming message: {time_since_last_incoming}, Use template: {use_template}")

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

            save_conversation_state()
            save_conversation_history(phone, conversation_state[phone]['history'])
            save_client_info(phone)
            return "Mensaje enviado"
        else:
            # Check if introduction has been sent
            if not conversation_state[phone].get('introduced', False):
                intro = bot_config.INITIAL_INTRO
                conversation_state[phone]['introduced'] = True
                conversation_state[phone]['name_asked'] = 1
            else:
                intro = ""

            # Ask for budget if needed
            if ask_name:
                conversation_state[phone]['name_asked'] += 1
            if ask_budget:
                intro += f" {bot_config.BUDGET_QUESTION}"
                conversation_state[phone]['budget_asked'] += 1
            if ask_contact_time:
                intro += f" {bot_config.CONTACT_TIME_QUESTION}"
                conversation_state[phone]['contact_time_asked'] += 1

            logger.debug("Generating prompt for ChatGPT")
            prompt = (
                f"{bot_config.BOT_PERSONALITY}\n\n"
                f"{intro}\n\n"
                f"**Instrucciones para las respuestas:**\n"
                f"{bot_config.RESPONSE_INSTRUCTIONS}\n\n"
                f"**Información de los proyectos disponibles:**\n"
                f"{project_info}\n\n"
                f"**Historial de conversación:**\n"
                f"{conversation_history}\n\n"
                f"**Mensaje del cliente:** \"{incoming_msg}\"\n\n"
                f"Responde de forma breve y profesional, enfocándote en la venta de propiedades. Improvisa de manera natural, utilizando únicamente la información de los proyectos y archivos descargables proporcionados."
            )
            logger.debug(f"ChatGPT prompt: {prompt}")

            messages = []
            logger.debug("Attempting to generate response with ChatGPT")
            try:
                logger.debug("Generating response with ChatGPT")
                response = openai_client.chat.completions.create(
                    model=CHATGPT_MODEL,  # Use the constant defined at the top
                    messages=[
                        {"role": "system", "content": bot_config.BOT_PERSONALITY},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=50,  # Reduced to enforce shorter responses
                    temperature=0.7
                )
                reply = response.choices[0].message.content.strip()
                logger.debug(f"Generated response: {reply}")
            except Exception as openai_e:
                logger.error(f"Fallo con OpenAI API: {str(openai_e)}", exc_info=True)
                if "rate_limit" in str(openai_e).lower() or "insufficient_quota" in str(openai_e).lower():
                    reply = "Lo siento, estoy teniendo problemas para procesar tu mensaje debido a un límite en mi sistema."
                elif "authentication" in str(openai_e).lower():
                    reply = "Parece que hay un problema con mi configuración."
                else:
                    reply = "Lo siento, no entiendo bien tu pregunta."

            logger.debug(f"ChatGPT reply: {reply}")

            current_message = ""
            sentences = reply.split('. ')
            for i, sentence in enumerate(sentences):
                if not sentence:
                    continue
                sentence = sentence.strip()
                if sentence:
                    if len(current_message.split('\n')) < 2 and len(current_message) < 50:
                        current_message += (sentence + '. ') if current_message else sentence + '. '
                    else:
                        messages.append(current_message.strip())
                        current_message = sentence + '. '
            if current_message:
                messages.append(current_message.strip())

            logger.debug(f"Mensajes generados para enviar: {messages}")

            if not messages:
                messages = ["No sé exactamente, pero déjame investigarlo."]

            # Check for file requests or location requests
            requested_file = None
            project = None
            for proj in downloadable_urls.keys():
                for file in downloadable_urls[proj].keys():
                    # Normalize file name for comparison
                    normalized_file = file.replace(" ", "").lower()
                    normalized_msg = incoming_msg.replace(" ", "").lower()
                    if normalized_file in normalized_msg:
                        requested_file = file
                        project = proj
                        break
                if requested_file:
                    break

            # Check if the message explicitly requests a file or location
            explicit_file_request = any(keyword in incoming_msg.lower() for keyword in [
                "mándame", "envíame", "pásame", "quiero el archivo", "presentación", "especificaciones", "entrega"
            ])
            location_request = "ubicación" in incoming_msg.lower() or "location" in incoming_msg.lower() or "google maps" in incoming_msg.lower()

            if requested_file and project:
                try:
                    file_urls = downloadable_urls.get(project, {})
                    logger.debug(f"File URLs for project {project}: {file_urls}")
                    file_url = file_urls.get(requested_file)
                    if file_url:
                        messages.append(bot_config.FILE_SENT_MESSAGE.format(requested_file=requested_file, file_url=file_url))
                    else:
                        logger.error(f"URL for {requested_file} not found in DESCARGABLES.TXT for {project}")
                        messages.append(bot_config.FILE_ERROR_MESSAGE.format(requested_file=requested_file))
                except Exception as e:
                    logger.error(f"Error al obtener URL del archivo: {str(e)}", exc_info=True)
                    messages.append(bot_config.FILE_ERROR_MESSAGE.format(requested_file=requested_file))
            elif location_request:
                # Find the project mentioned in the message or use the first available project
                mentioned_project = None
                for proj in downloadable_urls.keys():
                    if proj.lower() in incoming_msg.lower():
                        mentioned_project = proj
                        break
                if not mentioned_project and downloadable_urls:
                    mentioned_project = list(downloadable_urls.keys())[0]

                if mentioned_project:
                    file_urls = readable_urls.get(mentioned_project, {})
                    location_url = file_urls.get("ubicación en google maps")
                    if location_url:
                        messages.append(bot_config.LOCATION_MESSAGE.format(location_url=location_url))
                    else:
                        messages.append("Lo siento, no tengo la URL de la ubicación para este proyecto.")
            elif mentioned_project and bot_config.should_offer_files(conversation_state[phone], conversation_history, mentioned_project):
                messages.append(bot_config.OFFER_FILES_MESSAGE.format(project=mentioned_project))

            send_consecutive_messages(phone, messages)

            for msg in messages:
                conversation_state[phone]['history'].append(f"Giselle: {msg}")
            conversation_state[phone]['history'] = conversation_state[phone]['history'][-10:]

            save_conversation_state()
            save_conversation_history(phone, conversation_state[phone]['history'])
            save_client_info(phone)

            logger.debug("Returning success response")
            return "Mensaje enviado"
    except Exception as e:
        logger.error(f"Error inesperado en /whatsapp: {str(e)}", exc_info=True)
        try:
            # Normalize the phone number in the exception handler
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
            save_conversation_state()
            save_conversation_history(phone, conversation_state[phone]['history'])
            save_client_info(phone)
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
            send_consecutive_messages(phone, messages)
            for msg in messages:
                conversation_state[phone]['history'].append(f"Giselle: {msg}")
            save_conversation_state()
            save_conversation_history(phone, conversation_state[phone]['history'])
            save_client_info(phone)
    return "Recontact scheduling triggered"

# Application Startup
if __name__ == '__main__':
    load_conversation_state()
    download_projects_from_storage()
    downloadable_files = load_projects_from_folder()
    port = int(os.getenv("PORT", DEFAULT_PORT))
    service_url = os.getenv("SERVICE_URL", f"https://giselle-bot-250207106980.us-central1.run.app")
    logger.info(f"Puerto del servidor: {port}")
    logger.info(f"URL del servicio: {service_url}")
    logger.info(f"Configura el webhook en Twilio con: {service_url}/whatsapp")
    logger.info("Iniciando servidor Flask...")
    app.run(host='0.0.0.0', port=port, debug=True)
    logger.info(f"Servidor Flask iniciado en el puerto {port}.")

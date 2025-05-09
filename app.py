import os
import logging
import sys
import json
import time
import tempfile
from flask import Flask, request
from twilio.rest import Client
from google.cloud import storage
from openai import OpenAI
from datetime import datetime, timedelta
import re
import threading
from collections import deque

# Configure logging to output only to stdout/stderr (Cloud Run captures these)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),  # Log to stdout for Cloud Run
        # Removed FileHandler to avoid file access issues during startup
    ]
)

# Initialize Flask app
app = Flask(__name__)

# Configure logger
logger = logging.getLogger(__name__)

# Log startup
logger.info("Starting GISELLE service...")

# Configuration for Twilio (using environment variables)
try:
    account_sid = os.getenv('TWILIO_ACCOUNT_SID')
    auth_token = os.getenv('TWILIO_AUTH_TOKEN')
    logger.info(f"TWILIO_ACCOUNT_SID: {account_sid}")
    logger.info(f"TWILIO_AUTH_TOKEN: {'<set>' if auth_token else '<not set>'}")
    if not account_sid or not auth_token:
        logger.error("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set in environment variables")
        raise ValueError("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set")
    client = Client(account_sid, auth_token)
    logger.info("Twilio client initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Twilio client: {str(e)}", exc_info=True)
    raise

# Configuration for Grok API (using environment variables)
try:
    grok_api_key = os.getenv('GROK_API_KEY')
    logger.info(f"GROK_API_KEY: {'<set>' if grok_api_key else '<not set>'}")
    if not grok_api_key:
        logger.error("GROK_API_KEY not set in environment variables")
        raise ValueError("GROK_API_KEY not set")
    grok_client = OpenAI(
        api_key=grok_api_key,
        base_url='https://api.x.ai/v1'
    )
    logger.info("Grok client initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Grok client: {str(e)}", exc_info=True)
    raise

# Dictionary to store project data, downloadable links, and conversation state
projects_data = {}
downloadable_links = {}
conversation_state = {}
message_locks = {}  # To handle concurrency per phone number
message_queues = {}  # To queue messages per phone number

# File to store conversation state for recontact scheduling (use /tmp)
STATE_FILE = '/tmp/conversation_state.json'

# Load conversation state from file
def load_conversation_state():
    global conversation_state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                conversation_state = json.load(f)
            logger.info("Conversation state loaded from file")
        else:
            conversation_state = {}
            logger.info("No conversation state file found; starting fresh")
    except Exception as e:
        logger.error(f"Error loading conversation state: {str(e)}", exc_info=True)
        conversation_state = {}

# Save conversation state to file
def save_conversation_state():
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(conversation_state, f)
        logger.info("Conversation state saved to file")
    except Exception as e:
        logger.error(f"Error saving conversation state: {str(e)}", exc_info=True)

# Load conversation history from file
def load_conversation_history(phone):
    filename = f"/tmp/{phone.replace('+', '').replace(':', '_')}_conversation.txt"
    try:
        if os.path.exists(filename):
            with open(filename, 'r', encoding='utf-8') as f:
                history = f.read().strip().split('\n')
            logger.info(f"Loaded conversation history for {phone}")
            return history
        return []
    except Exception as e:
        logger.error(f"Error loading conversation history for {phone}: {str(e)}", exc_info=True)
        return []

# Save conversation history to file
def save_conversation_history(phone, history):
    filename = f"/tmp/{phone.replace('+', '').replace(':', '_')}_conversation.txt"
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write('\n'.join(history))
        logger.info(f"Saved conversation history for {phone}")
    except Exception as e:
        logger.error(f"Error saving conversation history for {phone}: {str(e)}", exc_info=True)

# Function to download files from Cloud Storage
def download_projects_from_storage(bucket_name='giselle-projects', base_path='/tmp/PROYECTOS'):
    global projects_data, downloadable_links
    try:
        if not os.path.exists(base_path):
            os.makedirs(base_path)
            logger.debug(f"Created directory {base_path}")

        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blobs = bucket.list_blobs(prefix='PROYECTOS')

        for blob in blobs:
            local_path = os.path.join(base_path, blob.name[len('PROYECTOS/'):])
            if not os.path.exists(os.path.dirname(local_path)):
                os.makedirs(os.path.dirname(local_path))
            blob.download_to_filename(local_path)
            logger.info(f"Descargado archivo desde Cloud Storage: {local_path}")
    except Exception as e:
        logger.error(f"Error downloading projects from Cloud Storage: {str(e)}", exc_info=True)
        # Reset projects_data and downloadable_links to empty to avoid using stale data
        projects_data.clear()
        downloadable_links.clear()

# Function to extract text from .txt files
def extract_text_from_txt(txt_path):
    try:
        with open(txt_path, 'r', encoding='utf-8') as file:
            text = file.read()
        logger.info(f"Archivo de texto {txt_path} leído correctamente.")
        return text
    except Exception as e:
        logger.error(f"Error al leer archivo de texto {txt_path}: {str(e)}", exc_info=True)
        return ""

# Load projects from folder (dynamically detect projects)
def load_projects_from_folder(base_path='/tmp/PROYECTOS'):
    global projects_data, downloadable_links
    downloadable_files = {}

    try:
        if not os.path.exists(base_path):
            os.makedirs(base_path)
            logger.warning(f"Carpeta {base_path} creada, pero no hay proyectos.")
            return downloadable_files

        # Detect projects dynamically, ignoring unwanted directories
        projects = [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d)) and not d.startswith('.') and d != 'DESCARGABLES']
        if not projects:
            logger.warning(f"No se encontraron proyectos en {base_path}.")
            return downloadable_files

        logger.info(f"Proyectos detectados: {', '.join(projects)}")

        # Initialize downloadable_links and projects_data for each detected project
        for project in projects:
            downloadable_links[project] = {}
            projects_data[project] = ""

        # Load project information (only .txt files outside DESCARGABLES)
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

            # Process the DESCARGABLES folder
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

        return downloadable_files
    except Exception as e:
        logger.error(f"Error loading projects: {str(e)}", exc_info=True)
        # Reset projects_data and downloadable_links to empty to avoid using stale data
        projects_data.clear()
        downloadable_links.clear()
        return {}

# Split long messages into shorter consecutive messages
def send_consecutive_messages(phone, messages):
    for msg in messages:
        try:
            message = client.messages.create(
                from_='whatsapp:+15557684099',
                body=msg,
                to=phone
            )
            logger.info(f"Mensaje enviado a través de Twilio: SID {message.sid}, Estado: {message.status}")
            updated_message = client.messages(message.sid).fetch()
            logger.info(f"Estado del mensaje actualizado: {updated_message.status}")
            if updated_message.status == "failed":
                logger.error(f"Error al enviar mensaje: {updated_message.error_code} - {updated_message.error_message}")
        except Exception as e:
            logger.error(f"Error al enviar mensaje con Twilio: {str(e)}")

# Schedule recontact for clients
def schedule_recontact():
    current_time = datetime.now()
    for phone, state in list(conversation_state.items()):
        # Skip if client has indicated no interest
        if state.get('no_interest', False):
            continue

        last_contact = state.get('last_contact')
        recontact_attempts = state.get('recontact_attempts', 0)
        schedule_next = state.get('schedule_next')

        # Handle scheduled recontact (e.g., "búscame la próxima semana")
        if schedule_next:
            schedule_time = datetime.fromisoformat(schedule_next['time'])
            if current_time >= schedule_time:
                preferred_time = state.get('preferred_time', '10:00 AM')
                messages = [
                    f"Hola, soy Giselle de FAV Living.",
                    f"Me pediste que te contactara. ¿Te interesa seguir hablando sobre el proyecto KABAN Holbox?"
                ]
                send_consecutive_messages(phone, messages)
                state['schedule_next'] = None
                state['last_contact'] = current_time.isoformat()
                state['recontact_attempts'] = 0
                conversation_state[phone]['history'].append(f"Giselle: Hola, soy Giselle de FAV Living.")
                conversation_state[phone]['history'].append(f"Giselle: Me pediste que te contactara. ¿Te interesa seguir hablando sobre el proyecto KABAN Holbox?")
                save_conversation_state()
                save_conversation_history(phone, conversation_state[phone]['history'])
            continue

        # Regular recontact every 3 days
        if last_contact and recontact_attempts < 3:
            last_contact_time = datetime.fromisoformat(last_contact)
            if (current_time - last_contact_time).days >= 3:
                preferred_time = state.get('preferred_time', '10:00 AM')
                messages = [
                    f"Hola, soy Giselle de FAV Living.",
                    f"No hemos hablado en unos días. ¿Te gustaría saber más sobre KABAN Holbox?"
                ]
                send_consecutive_messages(phone, messages)
                state['recontact_attempts'] = recontact_attempts + 1
                state['last_contact'] = current_time.isoformat()
                conversation_state[phone]['history'].append(f"Giselle: Hola, soy Giselle de FAV Living.")
                conversation_state[phone]['history'].append(f"Giselle: No hemos hablado en unos días. ¿Te gustaría saber más sobre KABAN Holbox?")
                save_conversation_state()
                save_conversation_history(phone, conversation_state[phone]['history'])

# Process message queue for a phone number
def process_message_queue(phone):
    if phone not in message_queues or not message_queues[phone]:
        return

    while message_queues[phone]:
        incoming_msg = message_queues[phone].popleft()
        try:
            logger.info(f"Procesando mensaje en cola para {phone}: {incoming_msg}")

            # Load conversation history from file
            history = load_conversation_history(phone)

            # Initialize conversation state if not exists
            if phone not in conversation_state:
                conversation_state[phone] = {
                    'history': history,
                    'name_asked': 0,
                    'budget_asked': 0,
                    'messages_since_budget_ask': 0,
                    'messages_without_response': 0,
                    'preferred_time': None,
                    'preferred_days': None,
                    'last_contact': datetime.now().isoformat(),
                    'recontact_attempts': 0,
                    'no_interest': False,
                    'schedule_next': None
                }
            else:
                # Update history from file
                conversation_state[phone]['history'] = history
                conversation_state[phone]['messages_without_response'] = 0

            # Update conversation history
            conversation_state[phone]['history'].append(f"Cliente: {incoming_msg}")
            # Keep only the last 5 messages to avoid overloading the prompt
            conversation_state[phone]['history'] = conversation_state[phone]['history'][-5:]

            # Update last contact time
            conversation_state[phone]['last_contact'] = datetime.now().isoformat()

            # Increment messages since last budget ask
            conversation_state[phone]['messages_since_budget_ask'] += 1

            # Check if the client indicates no interest
            no_interest_phrases = [
                "no me interesa", "no estoy interesado", "no quiero comprar",
                "no gracias", "no por el momento", "no estoy buscando"
            ]
            if any(phrase in incoming_msg.lower() for phrase in no_interest_phrases):
                conversation_state[phone]['no_interest'] = True
                messages = ["Entendido, gracias por tu tiempo. Si cambias de opinión, aquí estaré."]
                send_consecutive_messages(phone, messages)
                conversation_state[phone]['history'].append("Giselle: Entendido, gracias por tu tiempo. Si cambias de opinión, aquí estaré.")
                save_conversation_state()
                save_conversation_history(phone, conversation_state[phone]['history'])
                continue

            # Check if the client requests to be contacted later
            if "próxima semana" in incoming_msg.lower() or "la próxima semana" in incoming_msg.lower():
                schedule_time = datetime.now() + timedelta(days=7)
                # Use preferred time if available, otherwise default to 10:00 AM
                preferred_time = conversation_state[phone].get('preferred_time', '10:00 AM')
                schedule_time = schedule_time.replace(
                    hour=int(preferred_time.split(':')[0]) if ':' in preferred_time else 10,
                    minute=int(preferred_time.split(':')[1].replace(' AM', '').replace(' PM', '')) if ':' in preferred_time else 0,
                    second=0,
                    microsecond=0
                )
                if 'PM' in preferred_time.upper() and schedule_time.hour < 12:
                    schedule_time = schedule_time.replace(hour=schedule_time.hour + 12)
                conversation_state[phone]['schedule_next'] = {'time': schedule_time.isoformat()}
                messages = ["Perfecto, te contactaré la próxima semana. ¡Que tengas un buen día!"]
                send_consecutive_messages(phone, messages)
                conversation_state[phone]['history'].append("Giselle: Perfecto, te contactaré la próxima semana. ¡Que tengas un buen día!")
                save_conversation_state()
                save_conversation_history(phone, conversation_state[phone]['history'])
                continue

            # Prepare project information for the prompt
            project_info = ""
            for project, data in projects_data.items():
                project_info += f"Proyecto: {project}\n"
                project_info += f"Información: {data}\n"
                if project in downloadable_files and downloadable_files[project]:
                    project_info += "Archivos descargables:\n"
                    for file in downloadable_files[project]:
                        link = downloadable_links.get(project, {}).get(file, "Enlace no disponible")
                        project_info += f"- {file}: {link}\n"
                project_info += "\n"

            # Prepare conversation history for the prompt
            conversation_history = "\n".join(conversation_state[phone]['history'])

            # Determine if we should ask for the name, budget, or preferred contact time
            ask_name = (
                conversation_state[phone]['name_asked'] < 2 and
                "Cliente: Hola" in conversation_history and
                not any("Mi nombre es" in msg for msg in conversation_history)
            )
            ask_budget = (
                conversation_state[phone]['budget_asked'] < 2 and
                conversation_state[phone]['messages_since_budget_ask'] >= 2 and
                not any("Mi presupuesto es" in msg or "presupuesto de" in msg.lower() for msg in conversation_history)
            )
            ask_contact_time = (
                conversation_state[phone]['messages_without_response'] >= 2 and
                not conversation_state[phone].get('preferred_time') and
                not conversation_state[phone].get('preferred_days')
            )

            # Build the prompt
            prompt = (
                f"Eres Giselle, una asesora de ventas de FAV Living, una empresa inmobiliaria. "
                f"Tu objetivo es vender propiedades inmobiliarias de manera natural e improvisada, como lo haría una vendedora real. "
                f"No uses respuestas predefinidas ni intentes estructurar la conversación de manera rígida. "
                f"Responde únicamente basándote en la información de los proyectos que tienes disponible, sin inventar información adicional. "
                f"Actúa como Grok, respondiendo de forma fluida y profesional, enfocándote en la venta de propiedades. "
                f"Si el cliente hace una pregunta y no tienes la información exacta para responder, di algo como 'No sé exactamente, pero déjame investigarlo' "
                f"y continúa la conversación de manera natural. "
                f"No uses emoticones ni compartas información personal sobre ti más allá de tu rol en FAV Living.\n\n"
                f"**Instrucciones para las respuestas:**\n"
                f"- Responde de manera breve y profesional, como lo haría un humano en WhatsApp (1-2 frases por mensaje).\n"
                f"- Si la respuesta tiene más de 2 frases, divídela en mensajes consecutivos (separa el texto en varias partes, cada una de 1-2 frases).\n"
                f"- No uses viñetas ni formatos estructurados; escribe de forma fluida como un humano.\n"
                f"- Si es la primera interacción, preséntate brevemente como asesora de ventas de FAV Living.\n"
                f"- Si el cliente solicita información adicional o documentos (como presentaciones, precios, renders), incluye los nombres of the "
                f"archivos descargables correspondientes si están disponibles, sin inventar enlaces.\n"
                f"- Pregunta por el nombre del cliente de manera natural, pero no más de 1-2 veces en toda la conversación si no responde.\n"
                f"- Pregunta por el presupuesto del cliente de manera natural, pero no insistas; si no responde, vuelve a preguntar solo después de 2-3 mensajes "
                f"si es oportuno y relevante para la conversación.\n"
                f"- Si el cliente no ha respondido después de 2 mensajes, pregunta por su horario y días preferidos de contacto de manera natural, "
                f"para intentar recontactarlo más tarde.\n\n"
                f"**Información de los proyectos disponibles:**\n"
                f"{project_info}\n\n"
                f"**Historial de conversación:**\n"
                f"{conversation_history}\n\n"
                f"**Mensaje del cliente:** \"{incoming_msg}\"\n\n"
                f"Responde de forma breve y profesional, enfocándote en la venta de propiedades. Improvisa de manera natural, utilizando únicamente la información de los proyectos y archivos descargables proporcionados."
            )

            # Update conversation state if asking for name, budget, or contact time
            if ask_name:
                conversation_state[phone]['name_asked'] += 1
            if ask_budget:
                conversation_state[phone]['budget_asked'] += 1
                conversation_state[phone]['messages_since_budget_ask'] = 0
            if ask_contact_time:
                conversation_state[phone]['messages_without_response'] = 0

            # Generate response with Grok (with retries)
            logger.debug("Generating response with Grok")
            reply = None
            for attempt in range(3):  # Retry up to 3 times
                try:
                    response = grok_client.chat.completions.create(
                        model="grok-beta",
                        messages=[
                            {"role": "system", "content": "Eres Giselle, una asesora de ventas de FAV Living, utilizando la IA de Grok."},
                            {"role": "user", "content": prompt}
                        ],
                        timeout=5  # Reduced timeout to 5 seconds per attempt
                    )
                    reply = response.choices[0].message.content.strip()
                    logger.debug(f"Generated response: {reply}")
                    break
                except Exception as e:
                    logger.warning(f"Attempt {attempt + 1} failed: {str(e)}")
                    if attempt == 2:  # Last attempt
                        logger.error(f"Failed to generate response after 3 attempts: {str(e)}")
                        reply = None
                    time.sleep(1)  # Wait before retrying

            # Split the response into shorter messages if necessary
            messages = []
            if reply:
                current_message = ""
                sentences = reply.split('. ')
                for i, sentence in enumerate(sentences):
                    if not sentence:
                        continue
                    sentence = sentence.strip() + ('.' if i < len(sentences) - 1 else '')
                    if len(current_message.split('\n')) < 2:
                        current_message += (sentence + ' ') if current_message else sentence
                    else:
                        messages.append(current_message.strip())
                        current_message = sentence
                if current_message:
                    messages.append(current_message.strip())
            else:
                messages = ["dame unos minutos.."]

            # Send consecutive messages
            send_consecutive_messages(phone, messages)

            # Update conversation history with the response
            for msg in messages:
                conversation_state[phone]['history'].append(f"Giselle: {msg}")
            conversation_state[phone]['history'] = conversation_state[phone]['history'][-5:]

            # Save conversation state and history
            save_conversation_state()
            save_conversation_history(phone, conversation_state[phone]['history'])

            logger.debug("Returning success response")
            return "Mensaje enviado"
        except Exception as e:
            logger.error(f"Error inesperado en process_message_queue: {str(e)}", exc_info=True)
            # Send a fallback message to the user
            try:
                message = client.messages.create(
                    from_='whatsapp:+15557684099',
                    body="dame unos minutos..",
                    to=phone
                )
                logger.info(f"Fallback message sent: SID {message.sid}, Estado: {message.status}")
                if phone in conversation_state:
                    conversation_state[phone]['history'].append("Giselle: dame unos minutos..")
                    save_conversation_state()
                    save_conversation_history(phone, conversation_state[phone]['history'])
            except Exception as twilio_e:
                logger.error(f"Error sending fallback message: {str(twilio_e)}")
            return "Error interno del servidor", 500

# Webhook route for WhatsApp messages (enqueue messages)
@app.route('/whatsapp', methods=['POST'])
def whatsapp():
    logger.debug("Entered /whatsapp route")
    try:
        incoming_msg = request.values.get('Body', '').strip()
        phone = request.values.get('From', '')

        if not incoming_msg or not phone:
            logger.error("No se encontraron 'Body' o 'From' en la solicitud")
            return "Error: Solicitud incompleta", 400

        logger.info(f"Mensaje recibido de {phone}: {incoming_msg}")

        # Enqueue the message
        if phone not in message_queues:
            message_queues[phone] = deque()
        message_queues[phone].append(incoming_msg)

        # Process the queue
        threading.Thread(target=process_message_queue, args=(phone,)).start()

        return "Mensaje en cola", 200
    except Exception as e:
        logger.error(f"Error al encolar mensaje: {str(e)}", exc_info=True)
        return "Error interno del servidor", 500

# Route to trigger recontact scheduling (can be called periodically)
@app.route('/schedule_recontact', methods=['GET'])
def trigger_recontact():
    logger.info("Triggering recontact scheduling")
    schedule_recontact()
    return "Recontact scheduling triggered"

# Route to initialize project data (call after deployment)
@app.route('/initialize', methods=['GET'])
def initialize():
    logger.info("Initializing project data")
    try:
        download_projects_from_storage()
        global downloadable_files
        downloadable_files = load_projects_from_folder()
        return "Project data initialized", 200
    except Exception as e:
        logger.error(f"Error initializing project data: {str(e)}")
        return f"Error initializing project data: {str(e)}", 500

# Health check endpoint to verify the service is running
@app.route('/health', methods=['GET'])
def health():
    return "Service is running", 200

# Load conversation state on startup
try:
    load_conversation_state()
except Exception as e:
    logger.error(f"Failed to load conversation state on startup: {str(e)}", exc_info=True)

# Get the dynamic port from Cloud Run (default to 8080)
port = int(os.getenv("PORT", 8080))

# Debug: Print environment variables to confirm they are being read
logger.info(f"Puerto del servidor: {port}")
logger.info("Nota: Cloud Run asignará una URL pública al deploy (por ejemplo, https://giselle-bot-abc123-uc.a.run.app)")
logger.info("Configura el webhook en Twilio con la URL pública del deployment + /whatsapp")

if __name__ == '__main__':
    logger.info("Iniciando servidor Flask...")
    app.run(host='0.0.0.0', port=port, debug=True)
    logger.info(f"Servidor Flask iniciado en el puerto {port}.")

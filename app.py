import os
import logging
import sys
import json
from flask import Flask, request
from twilio.rest import Client
from google.cloud import storage
from openai import OpenAI
from datetime import datetime, timedelta

# Configuration Section
# Define all variables that are prone to change here
WHATSAPP_SENDER_NUMBER = "whatsapp:+15557571247"  # WhatsApp sender number
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
GROK_API_KEY = os.getenv('GROK_API_KEY')
GROK_API_BASE_URL = "https://api.x.ai/v1"
GCS_BUCKET_NAME = "giselle-projects"
GCS_BASE_PATH = "PROYECTOS"
STATE_FILE = "conversation_state.json"
DEFAULT_PORT = 8080
NO_INTEREST_PHRASES = [
    "no me interesa", "no estoy interesado", "no quiero comprar",
    "no gracias", "no por el momento", "no estoy buscando"
]
WHATSAPP_TEMPLATE_SID = "HX1234567890abcdef1234567890abcdef"  # Replace with your template SID
WHATSAPP_TEMPLATE_VARIABLES = {"1": "Cliente"}

# Configure logging to output to stdout/stderr (Cloud Run captures these)
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

# Initialize Grok API client
if not GROK_API_KEY:
    logger.error("GROK_API_KEY not set in environment variables")
    raise ValueError("GROK_API_KEY not set")
grok_client = OpenAI(
    api_key=GROK_API_KEY,
    base_url=GROK_API_BASE_URL
)

# Global dictionaries for project data and conversation state
projects_data = {}
downloadable_links = {}
conversation_state = {}

# Helper Functions
def get_conversation_history_filename(phone):
    """Generate the filename for conversation history based on phone number."""
    return f"{phone.replace('+', '').replace(':', '_')}_conversation.txt"

def load_conversation_state():
    """Load conversation state from file."""
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
        logger.error(f"Error loading conversation state: {str(e)}")
        conversation_state = {}

def save_conversation_state():
    """Save conversation state to file."""
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(conversation_state, f)
        logger.info("Conversation state saved to file")
    except Exception as e:
        logger.error(f"Error saving conversation state: {str(e)}")

def load_conversation_history(phone):
    """Load conversation history from file."""
    filename = get_conversation_history_filename(phone)
    try:
        if os.path.exists(filename):
            with open(filename, 'r', encoding='utf-8') as f:
                history = f.read().strip().split('\n')
            logger.info(f"Loaded conversation history for {phone}")
            return history
        return []
    except Exception as e:
        logger.error(f"Error loading conversation history for {phone}: {str(e)}")
        return []

def save_conversation_history(phone, history):
    """Save conversation history to file."""
    filename = get_conversation_history_filename(phone)
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write('\n'.join(history))
        logger.info(f"Saved conversation history for {phone}")
    except Exception as e:
        logger.error(f"Error saving conversation history for {phone}: {str(e)}")

def download_projects_from_storage(bucket_name=GCS_BUCKET_NAME, base_path=GCS_BASE_PATH):
    """Download project files from Google Cloud Storage."""
    try:
        if not os.path.exists(base_path):
            os.makedirs(base_path)
            logger.debug(f"Created directory {base_path}")

        storage_client = storage.Client()
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

def schedule_recontact():
    """Schedule recontact for clients."""
    current_time = datetime.now()
    for phone, state in conversation_state.items():
        if state.get('no_interest', False):
            continue

        last_contact = state.get('last_contact')
        recontact_attempts = state.get('recontact_attempts', 0)
        schedule_next = state.get('schedule_next')

        if schedule_next:
            schedule_time = datetime.fromisoformat(schedule_next['time'])
            if current_time >= schedule_time:
                preferred_time = state.get('preferred_time', '10:00 AM')
                messages = [
                    "Hola, soy Giselle de FAV Living.",
                    "Me pediste que te contactara. ¿Te interesa seguir hablando sobre el proyecto KABAN Holbox?"
                ]
                send_consecutive_messages(phone, messages)
                state['schedule_next'] = None
                state['last_contact'] = current_time.isoformat()
                state['recontact_attempts'] = 0
                conversation_state[phone]['history'].append("Giselle: Hola, soy Giselle de FAV Living.")
                conversation_state[phone]['history'].append("Giselle: Me pediste que te contactara. ¿Te interesa seguir hablando sobre el proyecto KABAN Holbox?")
                save_conversation_state()
                save_conversation_history(phone, conversation_state[phone]['history'])
            continue

        if last_contact and recontact_attempts < 3:
            last_contact_time = datetime.fromisoformat(last_contact)
            if (current_time - last_contact_time).days >= 3:
                preferred_time = state.get('preferred_time', '10:00 AM')
                messages = [
                    "Hola, soy Giselle de FAV Living.",
                    "No hemos hablado en unos días. ¿Te gustaría saber más sobre KABAN Holbox?"
                ]
                send_consecutive_messages(phone, messages)
                state['recontact_attempts'] = recontact_attempts + 1
                state['last_contact'] = current_time.isoformat()
                conversation_state[phone]['history'].append("Giselle: Hola, soy Giselle de FAV Living.")
                conversation_state[phone]['history'].append("Giselle: No hemos hablado en unos días. ¿Te gustaría saber más sobre KABAN Holbox?")
                save_conversation_state()
                save_conversation_history(phone, conversation_state[phone]['history'])

# Routes
@app.route('/whatsapp', methods=['POST'])
def whatsapp():
    logger.debug("Entered /whatsapp route")
    try:
        logger.debug(f"Request form data: {request.form}")
        incoming_msg = request.values.get('Body', '').strip()
        phone = request.values.get('From', '')

        if not incoming_msg or not phone:
            logger.error("No se encontraron 'Body' o 'From' en la solicitud")
            return "Error: Solicitud incompleta", 400

        # Ensure the phone number is in E.164 format (starts with "whatsapp:+")
        if not phone.startswith('whatsapp:+'):
            logger.error(f"Invalid phone number format: {phone}")
            return "Error: Invalid phone number format", 400

        logger.info(f"Mensaje recibido de {phone}: {incoming_msg}")

        history = load_conversation_history(phone)

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
                'schedule_next': None,
                'last_incoming_time': datetime.now().isoformat()
            }
        else:
            conversation_state[phone]['history'] = history
            conversation_state[phone]['messages_without_response'] = 0
            conversation_state[phone]['last_incoming_time'] = datetime.now().isoformat()

        conversation_state[phone]['history'].append(f"Cliente: {incoming_msg}")
        conversation_state[phone]['history'] = conversation_state[phone]['history'][-5:]

        conversation_state[phone]['last_contact'] = datetime.now().isoformat()
        conversation_state[phone]['messages_since_budget_ask'] += 1

        if any(phrase in incoming_msg.lower() for phrase in NO_INTEREST_PHRASES):
            conversation_state[phone]['no_interest'] = True
            messages = ["Entendido, gracias por tu tiempo. Si cambias de opinión, aquí estaré."]
            logger.info(f"Sending no-interest response: {messages}")
            send_consecutive_messages(phone, messages)
            conversation_state[phone]['history'].append("Giselle: Entendido, gracias por tu tiempo. Si cambias de opinión, aquí estaré.")
            save_conversation_state()
            save_conversation_history(phone, conversation_state[phone]['history'])
            return "Mensaje enviado"

        if "próxima semana" in incoming_msg.lower() or "la próxima semana" in incoming_msg.lower():
            schedule_time = datetime.now() + timedelta(days=7)
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
            logger.info(f"Sending scheduled contact response: {messages}")
            send_consecutive_messages(phone, messages)
            conversation_state[phone]['history'].append("Giselle: Perfecto, te contactaré la próxima semana. ¡Que tengas un buen día!")
            save_conversation_state()
            save_conversation_history(phone, conversation_state[phone]['history'])
            return "Mensaje enviado"

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

        conversation_history = "\n".join(conversation_state[phone]['history'])

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

        last_incoming_time = datetime.fromisoformat(conversation_state[phone]['last_incoming_time'])
        time_since_last_incoming = datetime.now() - last_incoming_time
        use_template = time_since_last_incoming > timedelta(hours=24)

        logger.debug(f"Time since last incoming message: {time_since_last_incoming}, Use template: {use_template}")

        if use_template:
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

            template_response = "Hola Cliente, soy Giselle de FAV Living. ¿Te gustaría saber más sobre nuestros proyectos inmobiliarios?"
            conversation_state[phone]['history'].append(f"Giselle: {template_response}")
            conversation_state[phone]['history'] = conversation_state[phone]['history'][-5:]

            save_conversation_state()
            save_conversation_history(phone, conversation_state[phone]['history'])
            return "Mensaje enviado"
        else:
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
                f"- Si el cliente solicita información adicional o documentos (como presentaciones, precios, renders), incluye los nombres de los "
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

            if ask_name:
                conversation_state[phone]['name_asked'] += 1
            if ask_budget:
                conversation_state[phone]['budget_asked'] += 1
                conversation_state[phone]['messages_since_budget_ask'] = 0
            if ask_contact_time:
                conversation_state[phone]['messages_without_response'] = 0

            logger.debug("Generating response with Grok")
            response = grok_client.chat.completions.create(
                model="grok-beta",
                messages=[
                    {"role": "system", "content": "Eres Giselle, una asesora de ventas de FAV Living, utilizando la IA de Grok."},
                    {"role": "user", "content": prompt}
                ]
            )
            reply = response.choices[0].message.content.strip()
            logger.debug(f"Generated response: {reply}")

            messages = []
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

            logger.debug(f"Mensajes generados para enviar: {messages}")

            if not messages:
                messages = ["No sé exactamente, pero déjame investigarlo."]

            send_consecutive_messages(phone, messages)

            for msg in messages:
                conversation_state[phone]['history'].append(f"Giselle: {msg}")
            conversation_state[phone]['history'] = conversation_state[phone]['history'][-5:]

            save_conversation_state()
            save_conversation_history(phone, conversation_state[phone]['history'])

            logger.debug("Returning success response")
            return "Mensaje enviado"
    except Exception as e:
        logger.error(f"Error inesperado en /whatsapp: {str(e)}", exc_info=True)
        try:
            if not phone.startswith('whatsapp:+'):
                phone = f"whatsapp:+{phone.replace('whatsapp:', '')}"
            message = client.messages.create(
                from_=WHATSAPP_SENDER_NUMBER,
                body="Lo siento, ocurrió un error. ¿En qué más puedo ayudarte?",
                to=phone
            )
            logger.info(f"Fallback message sent: SID {message.sid}, Estado: {message.status}")
            conversation_state[phone]['history'].append("Giselle: Lo siento, ocurrió un error. ¿En qué más puedo ayudarte?")
            save_conversation_state()
            save_conversation_history(phone, conversation_state[phone]['history'])
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
    schedule_recontact()
    return "Recontact scheduling triggered"

# Application Startup
if __name__ == '__main__':
    load_conversation_state()
    download_projects_from_storage()
    downloadable_files = load_projects_from_folder()
    port = int(os.getenv("PORT", DEFAULT_PORT))
    logger.info(f"Puerto del servidor: {port}")
    logger.info("Nota: Cloud Run asignará una URL pública al deploy (por ejemplo, https://giselle-bot-abc123-uc.a.run.app)")
    logger.info("Configura el webhook en Twilio con la URL pública del deployment + /whatsapp")
    logger.info("Iniciando servidor Flask...")
    app.run(host='0.0.0.0', port=port, debug=True)
    logger.info(f"Servidor Flask iniciado en el puerto {port}.")

import os
import logging
import sys
from flask import Flask, request
from twilio.rest import Client
from openai import OpenAI
from google.cloud import storage
import json

# Configuration Section
WHATSAPP_SENDER_NUMBER = "whatsapp:+18188732305"
GCS_BUCKET_NAME = "giselle-projects"
GCS_BASE_PATH = "PROYECTOS"

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
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
try:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.warning("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set in environment variables. Twilio client will not be initialized.")
    else:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        logger.info("Twilio client initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Twilio client: {str(e)}")
    client = None

# Initialize OpenAI client
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY not set in environment variables. Some functionality may not work.")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Initialize Google Cloud Storage client
try:
    storage_client = storage.Client()
except Exception as e:
    logger.error(f"Error initializing Google Cloud Storage client: {str(e)}")
    storage_client = None

# Global project data
projects_data = {}

# Global conversation state
conversation_state = {}

# Download project files from GCS
def download_projects_from_storage(bucket_name, base_path):
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
        logger.error(f"Error downloading projects from Cloud Storage: {str(e)}")
        raise

# Extract text from .txt files
def extract_text_from_txt(txt_path):
    try:
        with open(txt_path, 'r', encoding='utf-8') as file:
            text = file.read()
        logger.info(f"Archivo de texto {txt_path} leído correctamente.")
        return text
    except Exception as e:
        logger.error(f"Error al leer archivo de texto {txt_path}: {str(e)}")
        return ""

# Load project data from folder
def load_projects_from_folder(base_path):
    global projects_data
    projects_data = {}

    if not os.path.exists(base_path):
        os.makedirs(base_path)
        logger.warning(f"Carpeta {base_path} creada, pero no hay proyectos.")
        return

    projects = [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d)) and not d.startswith('.')]
    if not projects:
        logger.warning(f"No se encontraron proyectos en {base_path}.")
        return

    logger.info(f"Proyectos detectados: {', '.join(projects)}")

    for project in projects:
        project_path = os.path.join(base_path, project)
        project_file = f"{project}.txt"
        file_path = os.path.join(project_path, project_file)

        if os.path.isfile(file_path):
            logger.info(f"Procesando archivo de texto para {project}: {file_path}")
            text = extract_text_from_txt(file_path)
            if text:
                projects_data[project] = text
                logger.info(f"Proyecto {project} procesado correctamente desde {file_path}.")
                logger.debug(f"Contenido de {project_file}:\n{text}")
        else:
            logger.warning(f"No se encontró el archivo {project_file} para el proyecto {project}.")

# Health check endpoint for Cloud Run
@app.route('/health', methods=['GET'])
def health():
    logger.debug("Health check endpoint called")
    return "Healthy", 200

# Main WhatsApp route
@app.route('/whatsapp', methods=['POST'])
def whatsapp():
    logger.debug("Entered /whatsapp route")
    try:
        if client is None:
            logger.error("Twilio client not initialized. Cannot process WhatsApp messages.")
            return "Error: Twilio client not initialized", 500

        if openai_client is None:
            logger.error("OpenAI client not initialized. Cannot process messages.")
            return "Error: OpenAI client not initialized", 500

        logger.debug(f"Request headers: {dict(request.headers)}")
        logger.debug(f"Request form data: {request.form}")
        logger.debug(f"Request values: {dict(request.values)}")

        logger.debug("Extracting message content")
        num_media = int(request.values.get('NumMedia', '0'))
        phone = request.values.get('From', '')

        if num_media > 0:
            media_url = request.values.get('MediaUrl0', '')
            media_content_type = request.values.get('MediaContentType0', '')
            logger.debug(f"Media detected: URL={media_url}, Content-Type={media_content_type}")

            if 'audio' in media_content_type:
                audio_response = requests.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
                if audio_response.status_code != 200:
                    logger.error(f"Failed to download audio: {audio_response.status_code}")
                    messages = ["Lo siento, no pude procesar tu mensaje de audio. ¿Puedes enviarlo como texto?"]
                    for msg in messages:
                        client.messages.create(
                            from_=WHATSAPP_SENDER_NUMBER,
                            body=msg,
                            to=phone
                        )
                    return "Mensaje enviado"

                audio_file_path = f"/tmp/audio_{phone.replace(':', '_')}.ogg"
                with open(audio_file_path, 'wb') as f:
                    f.write(audio_response.content)
                logger.debug(f"Audio saved to {audio_file_path}")

                try:
                    with open(audio_file_path, 'rb') as audio_file:
                        transcription = openai_client.audio.transcriptions.create(
                            model="whisper-1",
                            file=audio_file,
                            language="es"
                        )
                    incoming_msg = transcription.text.strip()
                    logger.info(f"Audio transcribed: {incoming_msg}")
                except Exception as e:
                    logger.error(f"Error transcribing audio: {str(e)}")
                    messages = ["Lo siento, no pude entender tu mensaje de audio. ¿Puedes intentarlo de nuevo o escribirlo como texto?"]
                    for msg in messages:
                        client.messages.create(
                            from_=WHATSAPP_SENDER_NUMBER,
                            body=msg,
                            to=phone
                        )
                    return "Mensaje enviado"
                finally:
                    if os.path.exists(audio_file_path):
                        os.remove(audio_file_path)
            else:
                messages = ["Lo siento, solo puedo procesar mensajes de texto o audio. ¿Puedes enviar tu mensaje de otra forma?"]
                for msg in messages:
                    client.messages.create(
                        from_=WHATSAPP_SENDER_NUMBER,
                        body=msg,
                        to=phone
                    )
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

        # Initialize conversation state for the phone number
        if phone not in conversation_state:
            conversation_state[phone] = {
                'history': [],
                'last_message_time': datetime.now().isoformat()
            }

        # Add incoming message to history
        conversation_state[phone]['history'].append(f"Cliente: {incoming_msg}")
        conversation_state[phone]['history'] = conversation_state[phone]['history'][-10:]
        conversation_state[phone]['last_message_time'] = datetime.now().isoformat()

        # Build project information prompt
        project_info = ""
        mentioned_project = None
        for project, data in projects_data.items():
            project_info += f"Proyecto: {project}\n{data}\n\n"
            if project.lower() in incoming_msg.lower():
                mentioned_project = project

        # Build conversation history prompt
        conversation_history = "\n".join(conversation_state[phone]['history'])

        # Prepare prompt for OpenAI
        prompt = (
            "Eres Giselle, una asesora de ventas de FAV Living, una empresa inmobiliaria. "
            "Tu objetivo es vender propiedades inmobiliarias de manera natural e improvisada, como lo haría una vendedora real. "
            "Actúa de forma fluida, profesional y cercana, como si estuvieras charlando con un amigo. "
            "No des demasiada información de una vez; suelta los detalles poco a poco para mantener el interés del cliente. "
            "Usa un lenguaje que despierte curiosidad, como 'un proyecto que creo que te va a encantar' o 'una ubicación que te sorprenderá'. "
            "Responde únicamente basándote en la información de los proyectos que tienes disponible, sin inventar información adicional. "
            "Si el cliente hace una pregunta y no tienes la información exacta para responder, di algo como 'No sé exactamente, pero déjame investigarlo' "
            "y continúa la conversación de manera natural. "
            "No uses emoticones ni compartas información personal sobre ti más allá de tu rol en FAV Living.\n\n"
            "Información de los proyectos disponibles:\n"
            f"{project_info}\n\n"
            "Historial de conversación:\n"
            f"{conversation_history}\n\n"
            "Mensaje del cliente: '{incoming_msg}'\n\n"
            "Responde de forma breve y profesional, enfocándote en la venta de propiedades. Improvisa de manera natural."
        )

        # Generate response using OpenAI
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": "Eres Giselle, una asesora de ventas de FAV Living."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=150,
                temperature=0.7
            )
            reply = response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Error generating response with OpenAI: {str(e)}")
            reply = "Lo siento, ocurrió un error al procesar tu mensaje. ¿En qué más puedo ayudarte?"

        # Send response
        messages = reply.split('\n')  # Split into multiple messages if needed
        for msg in messages:
            if msg.strip():
                client.messages.create(
                    from_=WHATSAPP_SENDER_NUMBER,
                    body=msg.strip(),
                    to=phone
                )

        # Add response to history
        conversation_state[phone]['history'].append(f"Giselle: {reply}")
        conversation_state[phone]['history'] = conversation_state[phone]['history'][-10:]

        logger.debug("Returning success response")
        return "Mensaje enviado"
    except Exception as e:
        logger.error(f"Error inesperado en /whatsapp: {str(e)}", exc_info=True)
        return "Error interno del servidor", 500

@app.route('/', methods=['GET'])
def root():
    logger.debug("Solicitud GET recibida en /")
    return "Servidor Flask está funcionando!"

@app.route('/test', methods=['GET'])
def test():
    logger.debug("Solicitud GET recibida en /test")
    return "Servidor Flask está funcionando correctamente!"

# Application Startup
if __name__ == '__main__':
    try:
        logger.info("Starting application initialization...")
        download_projects_from_storage(GCS_BUCKET_NAME, GCS_BASE_PATH)
        logger.info("Projects downloaded from storage")
        load_projects_from_folder(GCS_BASE_PATH)
        logger.info("Projects loaded from folder")
        port = int(os.getenv("PORT", 8080))
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

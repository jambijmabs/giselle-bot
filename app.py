import os
import logging
import sys
from flask import Flask, request
from twilio.rest import Client
from google.cloud import storage
from openai import OpenAI
from datetime import datetime

# Configure logging to output to stdout/stderr (Cloud Run captures these)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),  # Log to stdout for Cloud Run
        logging.FileHandler('giselle_activity.log')  # Also log to file
    ]
)

# Initialize Flask app
app = Flask(__name__)

# Configure logger
logger = logging.getLogger(__name__)

# Configuration for Twilio (using environment variables)
account_sid = os.getenv('TWILIO_ACCOUNT_SID')
auth_token = os.getenv('TWILIO_AUTH_TOKEN')
if not account_sid or not auth_token:
    logger.error("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set in environment variables")
    raise ValueError("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set")
client = Client(account_sid, auth_token)

# Configuration for Grok API (using environment variables)
grok_client = OpenAI(
    api_key=os.getenv('GROK_API_KEY'),
    base_url='https://api.x.ai/v1'
)
if not os.getenv('GROK_API_KEY'):
    logger.error("GROK_API_KEY not set in environment variables")
    raise ValueError("GROK_API_KEY not set")

# Dictionary to store project data and downloadable links
projects_data = {}
downloadable_links = {}

# Function to download files from Cloud Storage
def download_projects_from_storage(bucket_name='giselle-projects', base_path='PROYECTOS'):
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
def load_projects_from_folder(base_path='PROYECTOS'):
    downloadable_files = {}

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

    # Initialize downloadable_links for each detected project
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

# Webhook route for WhatsApp messages
@app.route('/whatsapp', methods=['POST'])
def whatsapp():
    logger.debug("Entered /whatsapp route")
    try:
        logger.debug(f"Request form data: {request.form}")
        incoming_msg = request.values.get('Body', '')
        phone = request.values.get('From', '')

        if not incoming_msg or not phone:
            logger.error("No se encontraron 'Body' o 'From' en la solicitud")
            return "Error: Solicitud incompleta", 400

        logger.info(f"Mensaje recibido de {phone}: {incoming_msg}")

        # Send "typing" message
        logger.debug("Sending 'Escribiendo...' message")
        message_writing = client.messages.create(
            from_='whatsapp:+15557684099',
            body="Escribiendo...",
            to=phone
        )
        logger.info(f"Mensaje de 'Escribiendo...' enviado a través de Twilio: SID {message_writing.sid}")

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

        # Generate response using Grok API
        prompt = (
            f"Eres Giselle, una asesora de ventas de FAV Living, una empresa inmobiliaria. "
            f"Tu objetivo es vender propiedades inmobiliarias de manera natural e improvisada, como lo haría una vendedora real. "
            f"No uses respuestas predefinidas ni intentes estructurar la conversación de manera rígida. "
            f"Responde únicamente basándote en la información de los proyectos que tienes disponible, sin inventar información adicional. "
            f"Actúa como Grok, respondiendo de forma fluida y profesional, enfocándote en la venta de propiedades. "
            f"No hagas preguntas estructuradas para recolectar datos del cliente (como nombre, presupuesto, horario, etc.), "
            f"a menos que sea absolutamente necesario para avanzar en la venta y surja de manera natural en la conversación. "
            f"Si el cliente solicita información adicional o documentos (como presentaciones, precios, renders), incluye los enlaces a los "
            f"archivos descargables correspondientes si están disponibles. "
            f"No uses emoticones ni compartas información personal sobre ti más allá de tu rol en FAV Living.\n\n"
            f"Información de los proyectos disponibles:\n"
            f"{project_info}\n\n"
            f"Mensaje del cliente: \"{incoming_msg}\"\n\n"
            f"Responde de forma breve y profesional, enfocándote en la venta de propiedades. Preséntate solo si es la primera interacción o si es necesario para el contexto. "
            f"Improvisa de manera natural, utilizando únicamente la información de los proyectos y archivos descargables proporcionados."
        )

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

        # Send the actual response
        logger.debug("Sending actual response")
        message = client.messages.create(
            from_='whatsapp:+15557684099',
            body=reply,
            to=phone
        )
        logger.info(f"Mensaje enviado a través de Twilio: SID {message.sid}, Estado: {message.status}")

        # Verify message status
        logger.debug("Verifying message status")
        updated_message = client.messages(message.sid).fetch()
        logger.info(f"Estado del mensaje actualizado: {updated_message.status}")
        if updated_message.status == "failed":
            logger.error(f"Error al enviar mensaje: {updated_message.error_code} - {updated_message.error_message}")

        logger.debug("Returning success response")
        return "Mensaje enviado"
    except Exception as e:
        logger.error(f"Error inesperado en /whatsapp: {str(e)}", exc_info=True)
        return "Error interno del servidor", 500

# Root route for simple testing
@app.route('/', methods=['GET'])
def root():
    logger.debug("Solicitud GET recibida en /")
    return "Servidor Flask está funcionando!"

# Test endpoint to verify the server is running
@app.route('/test', methods=['GET'])
def test():
    logger.debug("Solicitud GET recibida en /test")
    return "Servidor Flask está funcionando correctamente!"

# Download projects from Cloud Storage on startup
download_projects_from_storage()

# Load projects and downloadable files
downloadable_files = load_projects_from_folder()

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

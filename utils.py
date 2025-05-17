import os
import logging
import json
from google.cloud import storage
from datetime import datetime, timedelta

# Configure logger
logger = logging.getLogger(__name__)

# Global dictionaries for project data
projects_data = {}
downloadable_links = {}
downloadable_urls = {}
downloadable_files = {}
gerente_respuestas = {}  # New global to store gerente responses

# Initialize Google Cloud Storage client
try:
    storage_client = storage.Client()
except Exception as e:
    logger.error(f"Error initializing Google Cloud Storage client: {str(e)}")
    storage_client = None

def get_conversation_history_filename(phone):
    """Generate the filename for conversation history based on phone number."""
    return f"{phone.replace('+', '').replace(':', '_')}_conversation.txt"

def get_client_info_filename(phone):
    """Generate the filename for client info based on phone number."""
    return f"client_info_{phone.replace('+', '').replace(':', '_')}.txt"

def get_gerente_respuestas_filename():
    """Generate the filename for gerente responses."""
    return "respuestas_gerencia.txt"

def upload_to_gcs(bucket_name, source_file_path, destination_blob_name):
    """Upload a file to Google Cloud Storage."""
    if storage_client is None:
        logger.error("Google Cloud Storage client not initialized. Cannot upload to GCS.")
        return
    try:
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(destination_blob_name)
        blob.upload_from_filename(source_file_path)
        logger.info(f"Uploaded {source_file_path} to GCS as {destination_blob_name}")
    except Exception as e:
        logger.error(f"Error uploading to GCS: {str(e)}")

def download_from_gcs(bucket_name, source_blob_name, destination_file_path):
    """Download a file from Google Cloud Storage."""
    if storage_client is None:
        logger.error("Google Cloud Storage client not initialized. Cannot download from GCS.")
        return False
    try:
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(source_blob_name)
        blob.download_to_filename(destination_file_path)
        logger.info(f"Downloaded {source_blob_name} from GCS to {destination_file_path}")
        return True
    except Exception as e:
        logger.error(f"Error downloading from GCS: {str(e)}")
        return False

def load_conversation_state(conversation_state, gcs_bucket_name, gcs_conversations_path):
    """Load conversation state from file in GCS."""
    try:
        local_state_file = "/tmp/conversation_state.json"
        destination_blob_name = os.path.join(gcs_conversations_path, "conversation_state.json")
        if download_from_gcs(gcs_bucket_name, destination_blob_name, local_state_file):
            with open(local_state_file, 'r') as f:
                state_data = json.load(f)
                conversation_state.clear()
                conversation_state.update(state_data)
            logger.info(f"Conversation state loaded from GCS: {conversation_state}")
        else:
            logger.info("No conversation state file found in GCS; starting fresh")
    except Exception as e:
        logger.error(f"Error loading conversation state: {str(e)}")
        conversation_state.clear()

def save_conversation_state(conversation_state, gcs_bucket_name, gcs_conversations_path):
    """Save conversation state to file in GCS."""
    try:
        local_state_file = "/tmp/conversation_state.json"
        destination_blob_name = os.path.join(gcs_conversations_path, "conversation_state.json")
        with open(local_state_file, 'w') as f:
            json.dump(conversation_state, f)
        upload_to_gcs(gcs_bucket_name, local_state_file, destination_blob_name)
        logger.info(f"Conversation state saved to GCS: {conversation_state}")
    except Exception as e:
        logger.error(f"Error saving conversation state: {str(e)}")

def load_conversation_history(phone, gcs_bucket_name, gcs_conversations_path):
    """Load conversation history from file in GCS."""
    filename = get_conversation_history_filename(phone)
    destination_blob_name = os.path.join(gcs_conversations_path, filename)
    local_file_path = f"/tmp/{filename}"
    try:
        if download_from_gcs(gcs_bucket_name, destination_blob_name, local_file_path):
            with open(local_file_path, 'r', encoding='utf-8') as f:
                history = f.read().strip().split('\n')
            logger.info(f"Loaded conversation history for {phone} from GCS")
            return history
        else:
            logger.info(f"No conversation history found for {phone} in GCS; starting fresh")
            return []
    except Exception as e:
        logger.error(f"Error loading conversation history for {phone}: {str(e)}")
        return []

def save_conversation_history(phone, history, gcs_bucket_name, gcs_conversations_path):
    """Save conversation history to file in GCS."""
    filename = get_conversation_history_filename(phone)
    destination_blob_name = os.path.join(gcs_conversations_path, filename)
    local_file_path = f"/tmp/{filename}"
    try:
        with open(local_file_path, 'w', encoding='utf-8') as f:
            for msg in history:
                f.write(f"{msg}\n")
        upload_to_gcs(gcs_bucket_name, local_file_path, destination_blob_name)
        logger.info(f"Saved conversation history for {phone} to GCS")
    except Exception as e:
        logger.error(f"Error saving conversation history for {phone}: {str(e)}")

def save_client_info(phone, conversation_state, gcs_bucket_name, gcs_conversations_path):
    """Save client information to a text file in GCS."""
    filename = get_client_info_filename(phone)
    destination_blob_name = os.path.join(gcs_conversations_path, filename)
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
        upload_to_gcs(gcs_bucket_name, local_file_path, destination_blob_name)
        logger.info(f"Saved client info for {phone} to GCS")
    except Exception as e:
        logger.error(f"Error saving client info for {phone}: {str(e)}")

def download_projects_from_storage(bucket_name, base_path):
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
        logger.error(f"Error downloading projects from Cloud Storage: {str(e)}")
        raise

def extract_text_from_txt(txt_path):
    """Extract text from .txt files."""
    try:
        with open(txt_path, 'r', encoding='utf-8') as file:
            text = file.read()
        logger.info(f"Archivo de texto {txt_path} leído correctamente.")
        return text
    except Exception as e:
        logger.error(f"Error al leer archivo de texto {txt_path}: {str(e)}")
        return ""

def load_gerente_respuestas(base_path):
    """Load gerente responses from respuestas_gerencia.txt in the projects folder."""
    global gerente_respuestas
    gerente_respuestas = {}
    filename = get_gerente_respuestas_filename()
    file_path = os.path.join(base_path, filename)

    try:
        if os.path.isfile(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                current_question = None
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("Pregunta:"):
                        current_question = line[len("Pregunta:"):].strip()
                    elif line.startswith("Respuesta:") and current_question:
                        answer = line[len("Respuesta:"):].strip()
                        gerente_respuestas[current_question] = answer
                        current_question = None
            logger.info(f"Loaded gerente responses from {file_path}")
        else:
            logger.info(f"No gerente responses file found at {file_path}; starting fresh")
    except Exception as e:
        logger.error(f"Error loading gerente responses: {str(e)}")
        gerente_respuestas = {}

def save_gerente_respuesta(base_path, question, answer, gcs_bucket_name):
    """Save a new gerente response to respuestas_gerencia.txt and upload to GCS."""
    filename = get_gerente_respuestas_filename()
    file_path = os.path.join(base_path, filename)
    destination_blob_name = os.path.join("PROYECTOS", filename)

    try:
        # Append the new question and answer
        with open(file_path, 'a', encoding='utf-8') as f:
            f.write(f"Pregunta: {question}\n")
            f.write(f"Respuesta: {answer}\n\n")
        logger.info(f"Saved gerente response to {file_path}")

        # Upload the updated file to GCS
        upload_to_gcs(gcs_bucket_name, file_path, destination_blob_name)
    except Exception as e:
        logger.error(f"Error saving gerente response: {str(e)}")

def load_projects_from_folder(base_path):
    """Load project data from project-specific .txt files."""
    global projects_data, downloadable_links, downloadable_urls, downloadable_files
    downloadable_files = {}

    if not os.path.exists(base_path):
        os.makedirs(base_path)
        logger.warning(f"Carpeta {base_path} creada, pero no hay proyectos.")
        return downloadable_files

    projects = [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d)) and not d.startswith('.')]
    if not projects:
        logger.warning(f"No se encontraron proyectos en {base_path}.")
        return downloadable_files

    logger.info(f"Proyectos detectados: {', '.join(projects)}")

    for project in projects:
        downloadable_links[project] = {}
        downloadable_urls[project] = {}
        projects_data[project] = ""

    for project in projects:
        project_path = os.path.join(base_path, project)
        file_count = 0
        project_file = f"{project}.txt"
        file_path = os.path.join(project_path, project_file)

        if os.path.isfile(file_path):
            logger.info(f"Procesando archivo de texto para {project}: {file_path}")
            text = extract_text_from_txt(file_path)
            if text:
                projects_data[project] = text
                logger.info(f"Proyecto {project} procesado correctamente desde {file_path}.")
                file_count += 1
                # Log the raw content for debugging
                logger.debug(f"Raw content of {project_file}:\n{text}")
            else:
                logger.warning(f"El archivo {project_file} está vacío o no se pudo leer.")
        else:
            logger.warning(f"No se encontró el archivo {project_file} para el proyecto {project}.")

        if file_count > 0:
            logger.info(f"Proyecto {project} procesado correctamente. {file_count} archivo(s) cargado(s).")
        else:
            logger.warning(f"No se encontraron archivos TXT válidos para el proyecto {project}.")

    return downloadable_files

def send_consecutive_messages(phone, messages, client, whatsapp_sender_number):
    """Send consecutive messages via Twilio."""
    for msg in messages:
        message = client.messages.create(
            from_=whatsapp_sender_number,
            body=msg,
            to=phone
        )
        logger.info(f"Mensaje enviado a través de Twilio: SID {message.sid}, Estado: {message.status}")
        updated_message = client.messages(message.sid).fetch()
        logger.info(f"Estado del mensaje actualizado: {updated_message.status}")
        if updated_message.status == "failed":
            logger.error(f"Error al enviar mensaje: {updated_message.error_code} - {updated_message.error_message}")

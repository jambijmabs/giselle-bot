import os
import logging
import json
import gcsfs
from datetime import datetime
import pandas as pd
from google.cloud import storage

# Configure logger
logger = logging.getLogger(__name__)

# Global variables for project data and FAQs
projects_data = {}
downloadable_urls = {}
faq_data = {}

def load_conversation_state(conversation_state, bucket_name, gcs_path):
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(os.path.join(gcs_path, "conversation_state.json"))
        temp_state_path = "/tmp/conversation_state.json"
        blob.download_to_filename(temp_state_path)
        with open(temp_state_path, 'r') as f:
            data = json.load(f)
            conversation_state.clear()
            conversation_state.update(data)
        logger.info("Conversation state loaded from GCS")
        os.remove(temp_state_path)
    except Exception as e:
        logger.warning(f"No existing conversation state found in GCS, initializing empty state: {str(e)}")

def save_conversation(phone, conversation_state, bucket_name, gcs_path):
    try:
        temp_state_path = "/tmp/conversation_state.json"
        with open(temp_state_path, 'w') as f:
            json.dump(conversation_state, f)
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(os.path.join(gcs_path, "conversation_state.json"))
        blob.upload_from_filename(temp_state_path)
        logger.info(f"Conversation state saved to GCS: {conversation_state}")
        os.remove(temp_state_path)

        temp_conv_path = f"/tmp/{phone.replace(':', '_')}_conversation.txt"
        with open(temp_conv_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(conversation_state[phone]['history']))
        blob = bucket.blob(os.path.join(gcs_path, f"{phone.replace(':', '_')}_conversation.txt"))
        blob.upload_from_filename(temp_conv_path)
        logger.info(f"Uploaded {temp_conv_path} to GCS as {gcs_path}/{phone.replace(':', '_')}_conversation.txt")
        os.remove(temp_conv_path)

        temp_info_path = f"/tmp/client_info_{phone.replace(':', '_')}.txt"
        client_info = [
            f"Nombre: {conversation_state[phone].get('client_name', 'Desconocido')}",
            f"Teléfono: {phone}",
            f"Presupuesto: {conversation_state[phone].get('client_budget', 'No especificado')}",
            f"Necesidades: {conversation_state[phone].get('needs', 'No especificadas')}",
            f"Última intención: {conversation_state[phone].get('intention_history', ['No especificada'])[-1] if conversation_state[phone].get('intention_history') else 'No especificada'}",
            f"Último contacto: {conversation_state[phone].get('last_contact', 'N/A')}"
        ]
        with open(temp_info_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(client_info))
        blob = bucket.blob(os.path.join(gcs_path, f"client_info_{phone.replace(':', '_')}.txt"))
        blob.upload_from_filename(temp_info_path)
        logger.info(f"Uploaded {temp_info_path} to GCS as {gcs_path}/client_info_{phone.replace(':', '_')}.txt")
        os.remove(temp_info_path)

        logger.info(f"Saved client info for {phone} to GCS")
    except Exception as e:
        logger.error(f"Failed to save conversation state to GCS: {str(e)}")

def load_conversation_history(phone, bucket_name, gcs_path):
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(os.path.join(gcs_path, f"{phone.replace(':', '_')}_conversation.txt"))
        temp_conv_path = f"/tmp/{phone.replace(':', '_')}_conversation.txt"
        blob.download_to_filename(temp_conv_path)
        with open(temp_conv_path, 'r', encoding='utf-8') as f:
            history = f.read().strip().split('\n')
        logger.info(f"Loaded conversation history for {phone} from GCS")
        os.remove(temp_conv_path)
        return history
    except Exception as e:
        logger.warning(f"No existing conversation history found for {phone} in GCS: {str(e)}")
        return []

def download_projects_from_storage(bucket_name, gcs_path):
    global projects_data
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blobs = bucket.list_blobs(prefix=gcs_path)
        for blob in blobs:
            if blob.name.endswith(('.json', '.txt')) and not blob.name.endswith(('_faq.txt', '_respuestas.txt')):
                temp_file_path = f"/tmp/{os.path.basename(blob.name)}"
                blob.download_to_filename(temp_file_path)
                logger.info(f"Descargado archivo: {blob.name} a {temp_file_path}")
                if blob.name.endswith('.json'):
                    with open(temp_file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        project_name = os.path.splitext(os.path.basename(blob.name))[0].upper()
                        projects_data[project_name] = data
                elif blob.name.endswith('.txt'):
                    with open(temp_file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        project_name = os.path.splitext(os.path.basename(blob.name))[0].upper()
                        projects_data[project_name] = {'description': content}
    except Exception as e:
        logger.error(f"Error descargando proyectos desde GCS: {str(e)}", exc_info=True)

def load_projects_from_folder(gcs_path):
    global projects_data, downloadable_urls
    projects_data.clear()
    downloadable_urls.clear()
    try:
        local_path = '/tmp'
        if os.path.exists(local_path):
            for filename in os.listdir(local_path):
                if filename.endswith(('.json', '.txt')) and not filename.startswith(('general_faq', 'anemona_faq', 'calidris_faq', 'kaban_faq', 'muwan_faq')):
                    file_path = os.path.join(local_path, filename)
                    logger.debug(f"Cargando archivo local: {file_path}")
                    if filename.endswith('.json'):
                        with open(file_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            project_name = os.path.splitext(filename)[0].upper()
                            projects_data[project_name] = data
                    elif filename.endswith('.txt'):
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                            project_name = os.path.splitext(filename)[0].upper()
                            projects_data[project_name] = {'description': content}
                    logger.info(f"Loaded project data for {project_name}: {projects_data[project_name]}")
        else:
            logger.warning(f"Carpeta no encontrada: {local_path}")
    except Exception as e:
        logger.error(f"Error cargando proyectos desde carpeta: {str(e)}", exc_info=True)

def load_gerente_respuestas(gcs_path):
    try:
        for project_file in os.listdir('/tmp'):
            if project_file.endswith('_respuestas.txt'):
                with open(os.path.join('/tmp', project_file), 'r', encoding='utf-8') as f:
                    content = f.read().strip().split('\n')
                    for line in content:
                        if line.startswith('URL:'):
                            url = line.replace('URL:', '').strip()
                            project_name = project_file.replace('_respuestas.txt', '').upper()
                            if project_name not in downloadable_urls:
                                downloadable_urls[project_name] = []
                            downloadable_urls[project_name].append(url)
                            logger.info(f"Loaded downloadable URL for {project_name}: {url}")
    except Exception as e:
        logger.error(f"Failed to load gerente respuestas: {str(e)}")

def load_faq_files(gcs_path):
    global faq_data
    faq_data.clear()
    try:
        for project_file in os.listdir('/tmp'):
            if project_file.endswith('_faq.txt'):
                project_key = project_file.replace('_faq.txt', '').lower()
                faq_data[project_key] = {}
                with open(os.path.join('/tmp', project_file), 'r', encoding='utf-8') as f:
                    content = f.read().strip().split('\n')
                    for i in range(0, len(content), 2):
                        if i + 1 < len(content):
                            question = content[i].replace('Pregunta:', '').strip().lower()
                            answer = content[i + 1].replace('Respuesta:', '').strip()
                            faq_data[project_key][question] = answer
                            logger.info(f"Loaded FAQ for {project_key}: {question} -> {answer}")
    except Exception as e:
        logger.error(f"Failed to load FAQ files: {str(e)}")

def get_faq_answer(question, project):
    question_lower = question.lower()
    project_key = project.lower() if project else None
    if project_key and project_key in faq_data and question_lower in faq_data[project_key]:
        return faq_data[project_key][question_lower]
    if 'general' in faq_data and question_lower in faq_data['general']:
        return faq_data['general'][question_lower]
    return None

def notify_gerente(messages, client, whatsapp_sender_number):
    for phone in [bot_config.GERENTE_PHONE]:
        send_consecutive_messages(phone, messages, client, whatsapp_sender_number)

def send_consecutive_messages(phone, messages, client, whatsapp_sender_number):
    for message in messages:
        try:
            msg = client.messages.create(
                body=message,
                from_=whatsapp_sender_number,
                to=phone
            )
            logger.info(f"Mensaje enviado a través de Twilio: SID {msg.sid}, Estado: {msg.status}")
        except Exception as e:
            logger.error(f"Error enviando mensaje a {phone}: {str(e)}")

def generate_daily_summary(conversation_state):
    summary = ["Resumen Diario de Actividad:"]
    today = datetime.now().date()
    total_messages = 0
    interested_clients = 0

    for phone, state in conversation_state.items():
        if state.get('is_gerente', False):
            continue

        last_contact = state.get('last_contact')
        if not last_contact:
            continue

        try:
            last_contact_date = datetime.fromisoformat(last_contact).date()
        except ValueError:
            continue

        if last_contact_date != today:
            continue

        total_messages += sum(1 for msg in state.get('history', []) if msg.startswith("Cliente:"))
        if not state.get('no_interest', False):
            interested_clients += 1

    summary.append(f"Mensajes recibidos hoy: {total_messages}")
    summary.append(f"Clientes interesados contactados hoy: {interested_clients}")
    return summary

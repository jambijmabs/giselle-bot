import os
import logging
import json
import re
from google.cloud import storage
from datetime import datetime, timedelta

# Configure logger
logger = logging.getLogger(__name__)

# Global dictionaries for project data and FAQs
projects_data = {}
downloadable_links = {}
downloadable_urls = {}
downloadable_files = {}
gerente_respuestas = {}
faq_data = {}

# Initialize Google Cloud Storage client
try:
    storage_client = storage.Client()
except Exception as e:
    logger.error(f"Error initializing Google Cloud Storage client: {str(e)}")
    storage_client = None

def get_conversation_history_filename(phone):
    return f"{phone.replace('+', '').replace(':', '_')}_conversation.txt"

def get_client_info_filename(phone):
    return f"client_info_{phone.replace('+', '').replace(':', '_')}.txt"

def get_gerente_respuestas_filename():
    return "respuestas_gerencia.txt"

def get_faq_filename(project=None):
    if project:
        return f"{project.lower()}_faq.txt"
    return "general_faq.txt"

def upload_to_gcs(bucket_name, source_file_path, destination_blob_name):
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
    filename = get_client_info_filename(phone)
    destination_blob_name = os.path.join(gcs_conversations_path, filename)
    local_file_path = f"/tmp/{filename}"
    try:
        client_info = conversation_state.get(phone, {})
        name = client_info.get('client_name', 'No proporcionado')
        budget = client_info.get('client_budget', 'No proporcionado')
        preferred_days = client_info.get('preferred_days', 'No proporcionado')
        preferred_time = client_info.get('preferred_time', 'No proporcionado')
        priority = client_info.get('priority', False)
        zoom_scheduled = client_info.get('zoom_scheduled', False)
        zoom_details = client_info.get('zoom_details', {})
        
        with open(local_file_path, 'w', encoding='utf-8') as f:
            f.write(f"Información del Cliente: {phone}\n")
            f.write(f"Nombre: {name}\n")
            f.write(f"Presupuesto: {budget}\n")
            f.write(f"Días Preferidos: {preferred_days}\n")
            f.write(f"Horario Preferido: {preferred_time}\n")
            f.write(f"Prioritario: {'Sí' if priority else 'No'}\n")
            f.write(f"Reunión Zoom Agendada: {'Sí' if zoom_scheduled else 'No'}\n")
            if zoom_scheduled and zoom_details:
                f.write(f"Detalles de Zoom: {zoom_details.get('day')} a las {zoom_details.get('time')}\n")
        upload_to_gcs(gcs_bucket_name, local_file_path, destination_blob_name)
        logger.info(f"Saved client info for {phone} to GCS")
    except Exception as e:
        logger.error(f"Error saving client info for {phone}: {str(e)}")

def save_conversation(phone, conversation_state, gcs_bucket_name, gcs_conversations_path):
    save_conversation_state(conversation_state, gcs_bucket_name, gcs_conversations_path)
    save_conversation_history(phone, conversation_state[phone]['history'], gcs_bucket_name, gcs_conversations_path)
    save_client_info(phone, conversation_state, gcs_bucket_name, gcs_conversations_path)

def notify_gerente(messages, twilio_client, whatsapp_sender_number):
    for gerente_phone in [bot_config.GERENTE_PHONE]:
        for msg in messages:
            try:
                message = twilio_client.messages.create(
                    from_=whatsapp_sender_number,
                    body=msg,
                    to=gerente_phone
                )
                logger.info(f"Notification sent to gerente {gerente_phone}: SID {message.sid}, Status: {message.status}")
            except Exception as e:
                logger.error(f"Error sending notification to gerente {gerente_phone}: {str(e)}")

def generate_interested_report(conversation_state):
    report = []
    interested_count = 0
    project_counts = {}
    status_counts = {'Interesado': 0, 'Esperando Respuesta': 0, 'No Interesado': 0}
    priority_clients = []
    project_groups = {}

    for phone, state in conversation_state.items():
        if state.get('is_gerente', False):
            continue

        if state.get('no_interest', False):
            status_counts['No Interesado'] += 1
            continue
        interested_count += 1

        project = state.get('last_mentioned_project', 'Desconocido')
        project_counts[project] = project_counts.get(project, 0) + 1

        if project not in project_groups:
            project_groups[project] = []

        if state.get('pending_question'):
            status = 'Esperando Respuesta'
        elif state.get('no_interest', False):
            status = 'No Interesado'
        else:
            status = 'Interesado'
        status_counts[status] += 1

        client_name = state.get('client_name', 'Desconocido')
        client_budget = state.get('client_budget', 'No especificado')
        last_message = state['history'][-1] if state['history'] else 'Sin mensajes'
        last_contact = state.get('last_contact', datetime.now().isoformat())
        last_contact_dt = datetime.fromisoformat(last_contact)
        time_since_contact = (datetime.now() - last_contact_dt).days
        messages_count = sum(1 for msg in state['history'] if msg.startswith("Cliente:"))

        summary = "Sin actividad reciente."
        if state['history']:
            if any("Permíteme, déjame revisar esto con el gerente." in msg for msg in state['history']):
                summary = "Cliente con preguntas pendientes 📝."
            elif any("Gracias por esperar. Sobre tu pregunta:" in msg for msg in state['history']):
                summary = "Cliente recibió respuesta del gerente ✅."
            elif any("¿Cuál es tu nombre?" in msg for msg in state['history']) and not state.get('client_name'):
                summary = "Cliente no ha proporcionado su nombre 🕵️‍♂️."
            else:
                summary = "Cliente activo, interactuando normalmente 😊."

        client_info = (
            f"- {client_name} ({phone}):\n"
            f"  Estado: {status} {'🟡' if status == 'Esperando Respuesta' else '🟢' if status == 'Interesado' else '🔴'}\n"
            f"  Presupuesto: {client_budget} 💰\n"
            f"  Último mensaje: {last_message} 💬\n"
            f"  Mensajes recibidos: {messages_count} 📩\n"
            f"  Último contacto: Hace {time_since_contact} día(s) ⏳\n"
            f"  Resumen: {summary}"
        )
        if state.get('priority', False):
            priority_clients.append(client_info)
        else:
            project_groups[project].append(client_info)

    report.append("📊 *Reporte de Interesados* 📊")
    report.append(f"👥 Total de interesados: {interested_count}")

    report.append("🏢 Por Proyecto:")
    for project, clients in project_groups.items():
        report.append(f"- {project}:")
        if clients:
            report.extend(clients)
        else:
            report.append("  No hay clientes interesados 😔.")

    if priority_clients:
        report.append("🌟 Clientes Prioritarios:")
        report.extend(priority_clients)

    report.append("📋 Resumen de Estados:")
    for status, count in status_counts.items():
        emoji = '🟢' if status == 'Interesado' else '🟡' if status == 'Esperando Respuesta' else '🔴'
        report.append(f"- {status}: {count} clientes {emoji}")

    return report

def generate_daily_summary(conversation_state):
    summary = []
    today = datetime.now().date()
    new_clients = 0
    questions_escalated = 0
    responses_sent = 0
    disinterested_clients = 0

    for phone, state in conversation_state.items():
        if state.get('is_gerente', False):
            continue

        last_contact = state.get('last_contact', datetime.now().isoformat())
        last_contact_dt = datetime.fromisoformat(last_contact).date()
        if last_contact_dt == today:
            new_clients += 1

        history = state.get('history', [])
        for msg in history:
            if "Permíteme, déjame revisar esto con el gerente." in msg and last_contact_dt == today:
                questions_escalated += 1
            elif "Gracias por esperar. Sobre tu pregunta:" in msg and last_contact_dt == today:
                responses_sent += 1

        if state.get('no_interest', False) and last_contact_dt == today:
            disinterested_clients += 1

    summary.append("🌞 *Resumen Diario de Actividad* 🌞")
    summary.append(f"📅 Fecha: {today.strftime('%Y-%m-%d')}")
    summary.append(f"👤 Nuevos clientes: {new_clients} 🆕")
    summary.append(f"❓ Preguntas escaladas al gerente: {questions_escalated} 📝")
    summary.append(f"✅ Respuestas enviadas a clientes: {responses_sent} 💬")
    summary.append(f"🚪 Clientes desinteresados: {disinterested_clients} 😔")

    return summary

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

def extract_text_from_txt(txt_path):
    try:
        with open(txt_path, 'r', encoding='utf-8') as file:
            text = file.read()
        logger.info(f"Archivo de texto {txt_path} leído correctamente.")
        return text
    except Exception as e:
        logger.error(f"Error al leer archivo de texto {txt_path}: {str(e)}")
        return ""

def load_gerente_respuestas(base_path):
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

def save_gerente_respuesta(base_path, question, answer, gcs_bucket_name, project=None):
    logger.info("Gerente response saving handled in app.py")

def load_faq_files(base_path):
    global faq_data
    faq_data = {}

    general_faq_path = os.path.join(base_path, "general_faq.txt")
    if os.path.isfile(general_faq_path):
        faq_data["general"] = {}
        with open(general_faq_path, 'r', encoding='utf-8') as f:
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
                    faq_data["general"][current_question.lower()] = answer
                    current_question = None
        logger.info(f"Loaded general_faq.txt: {faq_data['general']}")
    else:
        logger.info(f"No general_faq.txt found at {general_faq_path}; starting fresh")
        faq_data["general"] = {}

    projects = [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d)) and not d.startswith('.')]
    for project in projects:
        faq_path = os.path.join(base_path, project, f"{project.lower()}_faq.txt")
        if os.path.isfile(faq_path):
            faq_data[project.lower()] = {}
            with open(faq_path, 'r', encoding='utf-8') as f:
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
                        faq_data[project.lower()][current_question.lower()] = answer
                        current_question = None
            logger.info(f"Loaded {project.lower()}_faq.txt: {faq_data[project.lower()]}")
        else:
            logger.info(f"No FAQ file found for project {project}; starting fresh")
            faq_data[project.lower()] = {}

def get_faq_answer(question, project=None):
    question = question.lower()
    project_key = project.lower() if project else "general"
    
    if project_key in faq_data and question in faq_data[project_key]:
        return faq_data[project_key][question]
    
    if "general" in faq_data and question in faq_data["general"]:
        return faq_data["general"][question]
    
    return None

def load_projects_from_folder(base_path):
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
        projects_data[project] = {}

    for project in projects:
        project_path = os.path.join(base_path, project)
        file_count = 0
        project_file = f"{project}.txt"
        file_path = os.path.join(project_path, project_file)

        if os.path.isfile(file_path):
            logger.info(f"Procesando archivo de texto para {project}: {file_path}")
            text = extract_text_from_txt(file_path)
            if text:
                projects_data[project] = {
                    'description': text,
                    'type': 'condohotel' if 'condohotel' in text.lower() else 'desconocido',
                    'location': project_path.split('/')[-1]
                }
                logger.info(f"Proyecto {project} procesado correctamente desde {file_path}.")
                file_count += 1
                logger.debug(f"Raw content of {project_file} loaded")
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

import os
import logging
import sys
import json
import time
import re
from flask import Flask, request
from twilio.rest import Client
from datetime import datetime, timedelta
import bot_config
import utils
import message_handler
from google.cloud import storage
import pandas as pd
import gcsfs
import pytz

# Configuration Section
WHATSAPP_SENDER_NUMBER = "whatsapp:+18188732305"
GERENTE_NUMBERS = ["+5218110665094"]
GERENTE_ROLE = bot_config.GERENTE_ROLE
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GCS_BUCKET_NAME = "giselle-projects"
GCS_BASE_PATH = "PROYECTOS"
GCS_CONVERSATIONS_PATH = "CONVERSATIONS"
STATE_FILE = "conversation_state.json"
FAQ_RESPONSE_DELAY = 30
DEFAULT_PORT = 8080
WEEKLY_REPORT_DAY = "Sunday"
WEEKLY_REPORT_TIME = "18:00"
RECONTACT_TEMPLATE_NAME = "follow_up_template"
RECONTACT_MIN_DAYS = 1
RECONTACT_HOUR_CST = 18
RECONTACT_MINUTE_CST = 5
RECONTACT_TOLERANCE_MINUTES = 5
LEADS_EXCEL_PATH = "leads_giselle.xlsx"
CST_TIMEZONE = pytz.timezone("America/Mexico_City")

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

# Log registered routes after app initialization
with app.app_context():
    logger.info("Registered routes:")
    for rule in app.url_map.iter_rules():
        logger.info(f"Route: {rule.endpoint} -> {rule}")

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

def rephrase_gerente_response(answer, client_name, question):
    """Use AI to rephrase the gerente's response in a more friendly and natural way."""
    prompt = (
        f"Eres Giselle, una asesora de ventas profesional y amigable de FAV Living. "
        f"Reformula la respuesta del gerente para que sea más cálida y natural, manteniendo la información clave. "
        f"La respuesta será enviada a un cliente llamado {client_name}, quien hizo la pregunta: '{question}'. "
        f"Usa un tono profesional pero cercano, y asegúrate de que el mensaje sea breve.\n\n"
        f"Respuesta del gerente: {answer}\n\n"
        f"Reformula la respuesta:"
    )

    try:
        response = message_handler.openai_client.chat.completions.create(
            model=bot_config.CHATGPT_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": answer}
            ],
            max_tokens=50,
            temperature=0.3
        )
        rephrased = response.choices[0].message.content.strip()
        return rephrased
    except Exception as e:
        logger.error(f"Error rephrasing gerente response with OpenAI: {str(e)}")
        return f"Gracias por esperar, {client_name}. Sobre tu pregunta: {answer}"

def check_whatsapp_window(phone):
    if client is None:
        logger.error("Twilio client not initialized, cannot check WhatsApp window.")
        return False
    try:
        messages = client.messages.list(
            from_=phone,
            to=WHATSAPP_SENDER_NUMBER,
            date_sent_after=datetime.now(CST_TIMEZONE) - timedelta(hours=24)
        )
        if messages:
            logger.debug(f"WhatsApp 24-hour window is active for {phone}. Last message: {messages[0].date_sent}")
            return True
        else:
            logger.debug(f"WhatsApp 24-hour window is not active for {phone}.")
            return False
    except Exception as e:
        logger.error(f"Error checking WhatsApp window for {phone}: {str(e)}", exc_info=True)
        return False

def send_template_message(phone, client_name, project):
    try:
        message = client.messages.create(
            from_=WHATSAPP_SENDER_NUMBER,
            to=phone,
            content_sid=RECONTACT_TEMPLATE_NAME,
            content_variables=json.dumps({
                "1": client_name,
                "2": project
            })
        )
        logger.info(f"Template message sent to {phone}: SID {message.sid}, Status: {message.status}")
        return True
    except Exception as e:
        logger.error(f"Error sending template message to {phone}: {str(e)}")
        return False

def generate_detailed_report(conversation_state, filter_stage=None, filter_interest=None):
    report = ["Reporte Detallado de Clientes Interesados:"]
    for client_phone, state in conversation_state.items():
        if state.get('is_gerente', False) or state.get('no_interest', False):
            continue

        client_name = state.get('client_name', 'Desconocido')
        project = state.get('last_mentioned_project', 'No especificado')
        budget = state.get('client_budget', 'No especificado')
        needs = state.get('needs', 'No especificadas')
        stage = state.get('stage', 'Prospección')
        interest_level = state.get('interest_level', 0)
        last_contact = state.get('last_contact', 'N/A')
        last_messages = state.get('history', [])[-3:] if state.get('history') else ['Sin mensajes']
        zoom_scheduled = state.get('zoom_scheduled', False)
        zoom_details = state.get('zoom_details', {})

        if filter_stage and stage != filter_stage:
            continue
        if filter_interest is not None and interest_level != filter_interest:
            continue

        client_info = [
            f"Cliente: {client_phone}",
            f"Nombre: {client_name}",
            f"Proyecto: {project}",
            f"Presupuesto: {budget}",
            f"Necesidades: {needs}",
            f"Etapa: {stage}",
            f"Nivel de Interés: {interest_level}/10",
            f"Último Contacto: {last_contact}",
            f"Reunión Zoom Agendada: {'Sí' if zoom_scheduled else 'No'}"
        ]
        if zoom_scheduled and zoom_details:
            client_info.append(f"Detalles de Zoom: {zoom_details.get('day')} a las {zoom_details.get('time')}")
        client_info.append("Últimos Mensajes:")
        client_info.extend([f"- {msg}" for msg in last_messages])
        report.extend(client_info)
        report.append("---")

    if len(report) == 1:
        report.append("No hay clientes que coincidan con los criterios especificados.")
    return report

def update_leads_excel(conversation_state):
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(LEADS_EXCEL_PATH)

        temp_excel_path = f"/tmp/{LEADS_EXCEL_PATH}"
        try:
            blob.download_to_filename(temp_excel_path)
            df = pd.read_excel(temp_excel_path)
        except Exception as e:
            logger.warning(f"No existing Excel file found at {LEADS_EXCEL_PATH}, creating new file: {str(e)}")
            df = pd.DataFrame(columns=[
                "FECHA DE INGRESO", "NOMBRE", "TELEFONO", "CORREO",
                "PROYECTO DE INTERES", "FECHA DE ULTIMO CONTACTO",
                "NIVEL DE INTERES", "ESTATUS", "ZOOM AGENDADA", "DETALLES ZOOM"
            ])

        new_rows = []
        for client_phone, state in conversation_state.items():
            if state.get('is_gerente', False) or state.get('no_interest', False):
                continue

            client_name = state.get('client_name', 'Desconocido')
            project = state.get('last_mentioned_project', 'No especificado')
            last_contact = state.get('last_contact', 'N/A')
            interest_level = state.get('interest_level', 0)
            stage = state.get('stage', 'Prospección')
            first_contact = state.get('first_contact', last_contact)
            zoom_scheduled = state.get('zoom_scheduled', False)
            zoom_details = state.get('zoom_details', {})
            zoom_details_text = f"{zoom_details.get('day')} a las {zoom_details.get('time')}" if zoom_scheduled and zoom_details else "N/A"

            if client_phone in df['TELEFONO'].values:
                df.loc[df['TELEFONO'] == client_phone, [
                    "FECHA DE ULTIMO CONTACTO", "NIVEL DE INTERES", "ESTATUS", "PROYECTO DE INTERES",
                    "ZOOM AGENDADA", "DETALLES ZOOM"
                ]] = [last_contact, interest_level, stage, project, "Sí" if zoom_scheduled else "No", zoom_details_text]
            else:
                new_row = {
                    "FECHA DE INGRESO": first_contact,
                    "NOMBRE": client_name,
                    "TELEFONO": client_phone,
                    "CORREO": "N/A",
                    "PROYECTO DE INTERES": project,
                    "FECHA DE ULTIMO CONTACTO": last_contact,
                    "NIVEL DE INTERES": interest_level,
                    "ESTATUS": stage,
                    "ZOOM AGENDADA": "Sí" if zoom_scheduled else "No",
                    "DETALLES ZOOM": zoom_details_text
                }
                new_rows.append(new_row)

        if new_rows:
            new_df = pd.DataFrame(new_rows)
            df = pd.concat([df, new_df], ignore_index=True)

        df.to_excel(temp_excel_path, index=False)
        blob.upload_from_filename(temp_excel_path)
        logger.info(f"Updated leads Excel file at {LEADS_EXCEL_PATH}")

        os.remove(temp_excel_path)

    except Exception as e:
        logger.error(f"Failed to update leads Excel file: {str(e)}")

def show_gerente_menu(phone):
    menu = [
        "Hola, ¿cómo puedo ayudarte hoy? Por favor, selecciona una opción:",
        "1️⃣ Ver reporte de clientes interesados (puedes filtrar por etapa o interés)",
        "2️⃣ Ver nombres de clientes interesados",
        "3️⃣ Marcar un cliente como prioritario",
        "4️⃣ Ver resumen del día",
        "5️⃣ Ver resumen semanal",
        "6️⃣ Asignar una tarea (por ejemplo, 'Llamar a [teléfono] mañana')",
        "7️⃣ Buscar información de un cliente",
        "8️⃣ Añadir una FAQ",
        "Escribe el número de la opción o usa el comando directamente. 😊"
    ]
    utils.send_consecutive_messages(phone, menu, client, WHATSAPP_SENDER_NUMBER)
    conversation_state[phone]['awaiting_menu_choice'] = True

def notify_gerente_of_pending_questions(phone):
    """Notify the gerente of any pending questions."""
    pending_questions = []
    for client_phone, state in conversation_state.items():
        if not state.get('is_gerente', False) and state.get('pending_question'):
            pending_questions.append({
                'client_phone': client_phone,
                'question': state['pending_question']['question']
            })

    if pending_questions:
        for question in pending_questions:
            messages = [msg.format(client_phone=question['client_phone'], question=question['question']) for msg in bot_config.GERENTE_REMINDER_MESSAGE]
            utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
            logger.info(f"Notified gerente {phone} of pending question from {question['client_phone']}")

def handle_gerente_message(phone, incoming_msg):
    logger.info(f"Handling message from gerente ({phone})")

    incoming_msg_lower = incoming_msg.lower()

    # Notify gerente of pending questions on every interaction
    notify_gerente_of_pending_questions(phone)

    if conversation_state[phone].get('awaiting_menu_choice', False):
        if incoming_msg in ["1", "2", "3", "4", "5", "6", "7", "8"]:
            menu_commands = {
                "1": "reporte",
                "2": "nombres",
                "3": "marca prioritario",
                "4": "resumen del día",
                "5": "resumen semanal",
                "6": "llamar a mañana",
                "7": "busca a",
                "8": "añade faq"
            }
            incoming_msg_lower = menu_commands[incoming_msg]
            conversation_state[phone]['awaiting_menu_choice'] = False
        else:
            utils.send_consecutive_messages(
                phone,
                ["Por favor, selecciona una opción válida del menú (1-8)."],
                client,
                WHATSAPP_SENDER_NUMBER
            )
            show_gerente_menu(phone)
            return "Opción inválida", 200

    if "menú" in incoming_msg_lower or "opciones" in incoming_msg_lower:
        show_gerente_menu(phone)
        return "Menú enviado", 200

    pending_question = None
    for client_phone, state in conversation_state.items():
        if not state.get('is_gerente', False) and state.get('pending_question'):
            pending_question = state['pending_question']
            pending_question['client_phone'] = client_phone
            break

    if pending_question:
        logger.debug(f"Found pending question for client {pending_question['client_phone']}: {pending_question}")
        client_phone = pending_question['client_phone']
        question = pending_question['question']
        mentioned_project = pending_question.get('mentioned_project')
        answer = incoming_msg

        if len(answer) < 5 or answer.lower() in ["hola", "sí", "no", "ok"]:
            logger.warning(f"Gerente response '{answer}' seems irrelevant for question '{question}'")
            utils.send_consecutive_messages(
                phone,
                ["Tu respuesta parece poco clara. ¿Podrías dar más detalles?"],
                client,
                WHATSAPP_SENDER_NUMBER
            )
            show_gerente_menu(phone)
            return "Respuesta poco clara", 200

        client_name = conversation_state[client_phone].get('client_name', 'Cliente') or 'Cliente'
        rephrased_answer = rephrase_gerente_response(answer, client_name, question)
        gerente_messages = [rephrased_answer]
        utils.send_consecutive_messages(client_phone, gerente_messages, client, WHATSAPP_SENDER_NUMBER)

        conversation_state[client_phone]['history'].append(f"Giselle: {gerente_messages[0]}")
        conversation_state[client_phone]['pending_question'] = None
        conversation_state[client_phone]['pending_response_time'] = None
        logger.debug(f"Updated client {client_phone} history: {conversation_state[client_phone]['history']}")

        faq_entry = f"Pregunta: {question}\nRespuesta: {answer}\n"
        project_folder = mentioned_project.upper() if mentioned_project else "GENERAL"
        faq_file_name = f"{mentioned_project.lower()}_faq.txt" if mentioned_project else "general_faq.txt"
        faq_file_path = os.path.join(GCS_BASE_PATH, project_folder, faq_file_name)
        logger.debug(f"Attempting to save FAQ entry to {faq_file_path}: {faq_entry}")

        try:
            temp_faq_path = f"/tmp/{faq_file_name}"
            try:
                storage_client = storage.Client()
                bucket = storage_client.bucket(GCS_BUCKET_NAME)
                blob = bucket.blob(faq_file_path)
                blob.download_to_filename(temp_faq_path)
                logger.debug(f"Downloaded existing FAQ file from GCS: {faq_file_path}")
            except Exception as e:
                logger.warning(f"No existing FAQ file found at {faq_file_path}, creating new file: {str(e)}")
                with open(temp_faq_path, 'w') as f:
                    pass

            with open(temp_faq_path, 'a', encoding='utf-8') as f:
                f.write(faq_entry)
            logger.debug(f"Appended FAQ entry to local file: {temp_faq_path}")

            blob.upload_from_filename(temp_faq_path)
            logger.info(f"Uploaded updated FAQ file to GCS: {faq_file_path}")

            os.remove(temp_faq_path)
        except Exception as e:
            logger.error(f"Failed to save FAQ entry to {faq_file_path}: {str(e)}")

        utils.save_conversation(client_phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)

        utils.send_consecutive_messages(phone, ["Respuesta enviada al cliente. ¿Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
        show_gerente_menu(phone)
        return "Mensaje enviado", 200

    if "reporte" in incoming_msg_lower or "interesados" in incoming_msg_lower:
        logger.info(f"Gerente ({phone}) requested a report of interested clients")
        stage_filter = None
        interest_filter = None

        stage_match = re.search(r'etapa (\w+)', incoming_msg_lower)
        if stage_match:
            stage_filter = stage_match.group(1).capitalize()

        interest_match = re.search(r'interés (\d+)', incoming_msg_lower)
        if interest_match:
            interest_filter = int(interest_match.group(1))

        report_messages = generate_detailed_report(conversation_state, stage_filter, interest_filter)
        utils.send_consecutive_messages(phone, report_messages, client, WHATSAPP_SENDER_NUMBER)
        logger.debug(f"Sent report to gerente: {report_messages}")
        
        update_leads_excel(conversation_state)
        
        utils.send_consecutive_messages(phone, ["Reporte enviado y actualizado en leads_giselle.xlsx. ¿Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
        show_gerente_menu(phone)
        return "Reporte enviado", 200

    if "nombres" in incoming_msg_lower or "clientes" in incoming_msg_lower:
        logger.info(f"Gerente ({phone}) requested names of interested clients")
        interested_clients = []
        for client_phone, state in conversation_state.items():
            if not state.get('is_gerente', False) and not state.get('no_interest', False):
                client_name = state.get('client_name', 'Desconocido')
                interested_clients.append(client_name)

        if interested_clients:
            messages = [
                "Nombres de clientes interesados:",
                ", ".join(interested_clients)
            ]
        else:
            messages = ["No hay clientes interesados registrados."]
        utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
        utils.send_consecutive_messages(phone, ["¿Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
        show_gerente_menu(phone)
        return "Nombres enviados", 200

    if "marca" in incoming_msg_lower and "prioritario" in incoming_msg_lower:
        logger.info(f"Gerente ({phone}) requested to mark a client as priority")
        client_phone = None
        for number in conversation_state.keys():
            if number in incoming_msg:
                client_phone = number
                break
        if client_phone and not conversation_state[client_phone].get('is_gerente', False):
            conversation_state[client_phone]['priority'] = True
            utils.save_conversation(client_phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.send_consecutive_messages(phone, [f"Cliente {client_phone} marcado como prioritario.", "¿Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone)
            return "Cliente marcado como prioritario", 200
        else:
            utils.send_consecutive_messages(phone, ["No encontré al cliente especificado o es un gerente.", "¿En qué más puedo asistirte?"], client, WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone)
            return "Cliente no encontrado", 200

    if "resumen del día" in incoming_msg_lower:
        logger.info(f"Gerente ({phone}) requested daily activity summary")
        summary_messages = utils.generate_daily_summary(conversation_state)
        utils.send_consecutive_messages(phone, summary_messages, client, WHATSAPP_SENDER_NUMBER)
        utils.send_consecutive_messages(phone, ["¿Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
        show_gerente_menu(phone)
        return "Resumen enviado", 200

    if "resumen semanal" in incoming_msg_lower:
        logger.info(f"Gerente ({phone}) requested weekly summary")
        report_messages = generate_detailed_report(conversation_state)
        utils.send_consecutive_messages(phone, report_messages, client, WHATSAPP_SENDER_NUMBER)
        utils.send_consecutive_messages(phone, ["¿Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
        show_gerente_menu(phone)
        return "Resumen semanal enviado", 200

    if "llamar a" in incoming_msg_lower and "mañana" in incoming_msg_lower:
        logger.info(f"Gerente ({phone}) requested to assign a task")
        client_phone = None
        for number in conversation_state.keys():
            if number in incoming_msg:
                client_phone = number
                break
        if client_phone and not conversation_state[client_phone].get('is_gerente', False):
            time_str = "10:00 AM"
            time_match = re.search(r'a las (\d{1,2}(?::\d{2})?\s*(?:AM|PM))', incoming_msg_lower, re.IGNORECASE)
            if time_match:
                time_str = time_match.group(1)
            task = {
                'client_phone': client_phone,
                'action': 'Llamar',
                'time': time_str,
                'date': (datetime.now(CST_TIMEZONE) + timedelta(days=1)).strftime('%Y-%m-%d')
            }
            if 'tasks' not in conversation_state[phone]:
                conversation_state[phone]['tasks'] = []
            conversation_state[phone]['tasks'].append(task)
            utils.save_conversation(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.send_consecutive_messages(phone, [f"Tarea asignada: Llamar a {client_phone} mañana a las {time_str}.", "¿Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone)
            return "Tarea asignada", 200
        else:
            utils.send_consecutive_messages(phone, ["No encontré al cliente especificado o es un gerente.", "¿En qué más puedo asistirte?"], client, WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone)
            return "Cliente no encontrado", 200

    if "busca a" in incoming_msg_lower:
        logger.info(f"Gerente ({phone}) requested to search client information")
        client_phone = None
        for number in conversation_state.keys():
            if number in incoming_msg:
                client_phone = number
                break
        if client_phone and not conversation_state[client_phone].get('is_gerente', False):
            state = conversation_state[client_phone]
            client_name = state.get('client_name', 'Desconocido')
            project = state.get('last_mentioned_project', 'No especificado')
            budget = state.get('client_budget', 'No especificado')
            stage = state.get('stage', 'Prospección')
            interest_level = state.get('interest_level', 0)
            last_contact = state.get('last_contact', 'N/A')
            last_messages = state.get('history', [])[-3:] if state.get('history') else ['Sin mensajes']
            zoom_scheduled = state.get('zoom_scheduled', False)
            zoom_details = state.get('zoom_details', {})
            messages = [
                f"Información del Cliente {client_phone}",
                f"Nombre: {client_name}",
                f"Proyecto: {project}",
                f"Presupuesto: {budget}",
                f"Etapa: {stage}",
                f"Nivel de Interés: {interest_level}/10",
                f"Último Contacto: {last_contact}",
                f"Reunión Zoom Agendada: {'Sí' if zoom_scheduled else 'No'}"
            ]
            if zoom_scheduled and zoom_details:
                messages.append(f"Detalles de Zoom: {zoom_details.get('day')} a las {zoom_details.get('time')}")
            messages.append("Últimos Mensajes:")
            messages.extend([f"- {msg}" for msg in last_messages])
            utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
            utils.send_consecutive_messages(phone, ["¿Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone)
            return "Información enviada", 200
        else:
            utils.send_consecutive_messages(phone, ["No encontré al cliente especificado o es un gerente.", "¿En qué más puedo asistirte?"], client, WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone)
            return "Cliente no encontrado", 200

    if "añade faq" in incoming_msg_lower or "agrega faq" in incoming_msg_lower:
        logger.info(f"Gerente ({phone}) requested to add/edit an FAQ entry")
        match = re.search(r'para (\w+): Pregunta: (.+?) Respuesta: (.+)', incoming_msg, re.IGNORECASE)
        if match:
            project = match.group(1)
            question = match.group(2)
            answer = match.group(3)

            faq_entry = f"Pregunta: {question}\nRespuesta: {answer}\n"
            project_folder = project.upper()
            faq_file_name = f"{project.lower()}_faq.txt"
            faq_file_path = os.path.join(GCS_BASE_PATH, project_folder, faq_file_name)
            logger.debug(f"Attempting to save FAQ entry to {faq_file_path}: {faq_entry}")

            try:
                temp_faq_path = f"/tmp/{faq_file_name}"
                try:
                    storage_client = storage.Client()
                    bucket = storage_client.bucket(GCS_BUCKET_NAME)
                    blob = bucket.blob(faq_file_path)
                    blob.download_to_filename(temp_faq_path)
                    logger.debug(f"Downloaded existing FAQ file from GCS: {faq_file_path}")
                except Exception as e:
                    logger.warning(f"No existing FAQ file found at {faq_file_path}, creating new file: {str(e)}")
                    with open(temp_faq_path, 'w') as f:
                        pass

                with open(temp_faq_path, 'a', encoding='utf-8') as f:
                    f.write(faq_entry)
                logger.debug(f"Appended FAQ entry to local file: {temp_faq_path}")

                blob.upload_from_filename(temp_faq_path)
                logger.info(f"Uploaded updated FAQ file to GCS: {faq_file_path}")

                os.remove(temp_faq_path)

                project_key = project.lower()
                if project_key not in utils.faq_data:
                    utils.faq_data[project_key] = {}
                utils.faq_data[project_key][question.lower()] = answer
                logger.debug(f"Updated faq_data[{project_key}]")
            except Exception as e:
                logger.error(f"Failed to save FAQ entry to {faq_file_path}: {str(e)}")
                utils.send_consecutive_messages(phone, ["Ocurrió un error al guardar la FAQ.", "¿En qué más puedo asistirte?"], client, WHATSAPP_SENDER_NUMBER)
                show_gerente_menu(phone)
                return "Error al guardar FAQ", 500

            utils.send_consecutive_messages(phone, [f"FAQ añadida para {project}: {question}.", "¿Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone)
            return "FAQ añadida", 200
        else:
            utils.send_consecutive_messages(phone, ["Formato incorrecto. Usa: Añade FAQ para [Proyecto]: Pregunta: [Pregunta] Respuesta: [Respuesta]", "¿En qué más puedo asistirte?"], client, WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone)
            return "Formato incorrecto", 200

    show_gerente_menu(phone)
    return "Mensaje recibido", 200

def determine_best_contact_time(state):
    if state.get('preferred_time'):
        return state['preferred_time'], state.get('preferred_days')

    response_times = []
    for msg in state.get('history', []):
        if msg.startswith("Cliente:"):
            timestamp = state.get('last_response_time', datetime.now(CST_TIMEZONE).isoformat())
            try:
                dt = datetime.fromisoformat(timestamp).astimezone(CST_TIMEZONE)
                response_times.append(dt)
            except ValueError:
                continue

    if not response_times:
        return "10:00 AM", None

    hours = [dt.hour for dt in response_times]
    if not hours:
        return "10:00 AM", None

    most_common_hour = max(set(hours), key=hours.count)
    period = "AM" if most_common_hour < 12 else "PM"
    adjusted_hour = most_common_hour if most_common_hour <= 12 else most_common_hour - 12
    best_time = f"{adjusted_hour}:00 {period}"

    days = [dt.strftime('%A') for dt in response_times]
    most_common_day = max(set(days), key=days.count) if days else None

    return best_time, most_common_day

def handle_client_message(phone, incoming_msg, num_media, media_url=None, profile_name=None):
    logger.info(f"Handling message from client ({phone})")

    try:
        # Step 1: Load conversation history
        logger.debug(f"Loading conversation history for {phone}")
        history = utils.load_conversation_history(phone, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        if not isinstance(history, list):
            logger.warning(f"Conversation history for {phone} is not a list: {history}")
            history = []

        # Step 2: Initialize or update conversation state
        logger.debug(f"Updating conversation state for {phone}")
        if phone not in conversation_state:
            logger.warning(f"Client {phone} not found in conversation_state, initializing")
            conversation_state[phone] = {
                'history': [],
                'name_asked': 0,
                'messages_without_response': 0,
                'preferred_time': None,
                'preferred_days': None,
                'client_name': None,
                'client_budget': None,
                'last_contact': datetime.now(CST_TIMEZONE).isoformat(),
                'recontact_attempts': 0,
                'no_interest': False,
                'schedule_next': None,
                'last_incoming_time': datetime.now(CST_TIMEZONE).isoformat(),
                'last_response_time': datetime.now(CST_TIMEZONE).isoformat(),
                'first_contact': datetime.now(CST_TIMEZONE).isoformat(),
                'introduced': False,
                'project_info_shared': {},
                'last_mentioned_project': None,
                'pending_question': None,
                'pending_response_time': None,
                'is_gerente': False,
                'priority': False,
                'stage': 'Prospección',
                'interest_level': 0,
                'reminder_sent': False,
                'zoom_proposed': False,
                'zoom_scheduled': False,
                'zoom_details': {},
                'intention_history': []
            }

        state = conversation_state[phone]
        state['history'] = history
        state['history'].append(f"Cliente: {incoming_msg}")
        state['history'] = state['history'][-10:]
        state['last_contact'] = datetime.now(CST_TIMEZONE).isoformat()
        state['last_response_time'] = datetime.now(CST_TIMEZONE).isoformat()

        # Step 3: Set client name from ProfileName if available
        if profile_name and not state.get('client_name'):
            # Extract first name from ProfileName
            name_parts = profile_name.strip().split()
            if name_parts:
                state['client_name'] = name_parts[0].capitalize()
                logger.info(f"Client name set from ProfileName: {state['client_name']}")
            else:
                state['client_name'] = None

        # Step 4: Update client stage and interest level
        if any(phrase in incoming_msg.lower() for phrase in ["quiero comprar", "estoy listo", "confirmo"]):
            state['stage'] = 'Cierre'
            state['interest_level'] = max(state.get('interest_level', 0), 8)
        elif any(phrase in incoming_msg.lower() for phrase in ["me interesa", "quiero saber más", "detalles"]):
            state['stage'] = 'Negociación'
            state['interest_level'] = max(state.get('interest_level', 0), 5)
        elif any(phrase in incoming_msg.lower() for phrase in ["presupuesto", "necesidades", "qué tienes"]):
            state['stage'] = 'Calificación'
            state['interest_level'] = max(state.get('interest_level', 0), 3)

        # Step 5: Notify gerente if client shows high interest
        if state.get('interest_level', 0) >= 8 or state.get('stage') == 'Cierre':
            for gerente_phone in [p for p, s in conversation_state.items() if s.get('is_gerente', False)]:
                utils.send_consecutive_messages(
                    gerente_phone,
                    [f"Alerta: Cliente {phone} ({state.get('client_name', 'Desconocido')}) muestra alto interés (Nivel: {state.get('interest_level', 0)}). Etapa: {state.get('stage')}. Último mensaje: {incoming_msg}"],
                    client,
                    WHATSAPP_SENDER_NUMBER
                )

        if state.get('priority', False):
            for gerente_phone in [p for p, s in conversation_state.items() if s.get('is_gerente', False)]:
                utils.send_consecutive_messages(
                    gerente_phone,
                    [f"Cliente prioritario {phone} ha enviado un mensaje: {incoming_msg}"],
                    client,
                    WHATSAPP_SENDER_NUMBER
                )

        # Step 6: Handle pending responses from gerente
        logger.debug(f"Checking for pending responses for {phone}")
        if state.get('pending_response_time'):
            current_time = time.time()
            elapsed_time = current_time - state['pending_response_time']
            if elapsed_time >= FAQ_RESPONSE_DELAY:
                question = state.get('pending_question', {}).get('question')
                mentioned_project = state.get('pending_question', {}).get('mentioned_project')
                if question:
                    logger.debug(f"Fetching FAQ answer for question '{question}' about project '{mentioned_project}'")
                    answer = utils.get_faq_answer(question, mentioned_project)
                    if answer:
                        client_name = state.get('client_name', 'Cliente') or 'Cliente'
                        rephrased_answer = rephrase_gerente_response(answer, client_name, question)
                        messages = [rephrased_answer]
                        utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
                        state['history'].append(f"Giselle: {messages[0]}")
                        state['pending_question'] = None
                        state['pending_response_time'] = None
                        logger.debug(f"Sent gerente response to client {phone}: {messages}")
                    else:
                        logger.error(f"Could not find answer for question '{question}' in FAQ.")
                        messages = ["Lo siento, no pude encontrar una respuesta. ¿En qué más puedo ayudarte?"]
                        utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
                        state['history'].append(f"Giselle: {messages[0]}")
                        state['pending_question'] = None
                        state['pending_response_time'] = None
                else:
                    logger.error(f"No pending question found for {phone} despite pending_response_time.")
                    state['pending_response_time'] = None
            else:
                logger.debug(f"Waiting for FAQ response delay to complete for {phone}. Elapsed time: {elapsed_time} seconds")
                utils.save_conversation(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                return "Waiting for gerente response", 200

        # Step 7: Extract client name if not already set
        if not state.get('client_name') and state.get('name_asked', 0) > 0:
            name = message_handler.extract_name(incoming_msg, "\n".join(state['history']))
            if name:
                state['client_name'] = name.capitalize()
                logger.info(f"Client name extracted from message: {state['client_name']}")
                state['name_asked'] = state.get('name_asked', 0) + 1

        # Step 8: Prepare project information
        logger.debug("Preparing project information")
        project_info = ""
        try:
            if not hasattr(utils, 'projects_data'):
                logger.error("utils.projects_data is not defined")
                raise AttributeError("utils.projects_data is not defined")
            for project, data in utils.projects_data.items():
                project_info += f"Proyecto: {project}\n"
                project_info += "Es un desarrollo que creo que te va a interesar.\n"
                project_info += "\n"
        except Exception as project_info_e:
            logger.error(f"Error preparing project information: {str(project_info_e)}")
            project_info = "Información de proyectos no disponible."

        # Step 9: Process the message
        logger.debug("Building conversation history")
        conversation_history = "\n".join(state['history'])

        logger.debug(f"Checking FAQ for an existing answer")
        mentioned_project = state.get('last_mentioned_project')
        faq_answer = utils.get_faq_answer(incoming_msg, mentioned_project)
        if faq_answer:
            client_name = state.get('client_name', 'Cliente') or 'Cliente'
            rephrased_answer = rephrase_gerente_response(faq_answer, client_name, incoming_msg)
            messages = [rephrased_answer]
        else:
            logger.debug(f"Processing message with message_handler: {incoming_msg}")
            messages, mentioned_project, needs_gerente = message_handler.process_message(
                incoming_msg, phone, conversation_state, project_info, conversation_history
            )
            logger.debug(f"Messages generated: {messages}")
            logger.debug(f"Mentioned project after processing: {mentioned_project}")
            logger.debug(f"Needs gerente contact: {needs_gerente}")

            if needs_gerente:
                state['pending_question'] = {
                    'question': incoming_msg,
                    'mentioned_project': mentioned_project,
                    'client_phone': phone
                }
                state['pending_response_time'] = time.time()
                logger.debug(f"Set pending question for {phone}: {state['pending_question']}")
                for gerente_phone in [p for p, s in conversation_state.items() if s.get('is_gerente', False)]:
                    utils.send_consecutive_messages(
                        gerente_phone,
                        [
                            f"Nueva pregunta de cliente ({phone}): {incoming_msg}",
                            f"Contexto: Últimos mensajes - {conversation_history[-1000:]}",
                            "Por favor, responde con la información solicitada."
                        ],
                        client,
                        WHATSAPP_SENDER_NUMBER
                    )
            else:
                logger.debug(f"No gerente contact needed for message: {incoming_msg}")

        if mentioned_project:
            state['last_mentioned_project'] = mentioned_project
            logger.debug(f"Updated last_mentioned_project to: {mentioned_project}")

        # Step 10: Send a 24-hour window reminder
        last_incoming = datetime.fromisoformat(state['last_incoming_time']).astimezone(CST_TIMEZONE)
        time_since_last_incoming = (datetime.now(CST_TIMEZONE) - last_incoming).total_seconds() / 3600
        if 20 <= time_since_last_incoming < 24 and not state.get('reminder_sent', False):
            reminder = [
                f"Hola {state.get('client_name', 'Cliente')}, ha pasado un tiempo desde nuestro último mensaje.",
                "¿Tienes alguna pregunta o quieres más detalles? 😊"
            ]
            utils.send_consecutive_messages(phone, reminder, client, WHATSAPP_SENDER_NUMBER)
            state['history'].extend([f"Giselle: {msg}" for msg in reminder])
            state['reminder_sent'] = True

        # Step 11: Send the generated messages
        utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)

        for msg in messages:
            state['history'].append(f"Giselle: {msg}")
        state['history'] = state['history'][-10:]

        # Step 12: Reset recontact schedule if the client responds
        state['schedule_next'] = None
        state['recontact_attempts'] = 0
        state['reminder_sent'] = False

        # Step 13: Save conversation state
        utils.save_conversation(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)

        logger.debug("Returning success response")
        return "Mensaje enviado", 200

    except Exception as e:
        logger.error(f"Error in handle_client_message for {phone}: {str(e)}", exc_info=True)
        raise

@app.route('/whatsapp', methods=['POST'])
def whatsapp():
    logger.debug("Entered /whatsapp route")
    try:
        if client is None:
            logger.error("Twilio client not initialized. Cannot process WhatsApp messages.")
            return "Error: Twilio client not initialized", 500

        utils.load_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        logger.debug("Conversation state reloaded")

        logger.debug(f"Request headers: {dict(request.headers)}")
        logger.debug(f"Request form data: {request.form}")
        logger.debug(f"Request values: {dict(request.values)}")

        logger.debug("Extracting message content")
        phone = request.values.get('From', '')
        incoming_msg = request.values.get('Body', '').strip()
        num_media = int(request.values.get('NumMedia', '0'))
        media_url = request.values.get('MediaUrl0', None) if num_media > 0 else None
        profile_name = request.values.get('ProfileName', None)

        logger.debug(f"From phone: {phone}, Message: {incoming_msg}, NumMedia: {num_media}, MediaUrl: {media_url}, ProfileName: {profile_name}")

        if not phone:
            logger.error("No se encontró 'From' en la solicitud")
            return "Error: Solicitud incompleta", 400

        normalized_phone = phone.replace("whatsapp:", "").strip()
        is_gerente = normalized_phone in GERENTE_NUMBERS
        logger.debug(f"Comparando número: phone='{phone}', normalized_phone='{normalized_phone}', GERENTE_NUMBERS={GERENTE_NUMBERS}, is_gerente={is_gerente}")

        if is_gerente:
            logger.info(f"Identificado como gerente: {phone}")
            if phone not in conversation_state:
                conversation_state[phone] = {
                    'history': [],
                    'is_gerente': True,
                    'last_contact': datetime.now(CST_TIMEZONE).isoformat(),
                    'last_incoming_time': datetime.now(CST_TIMEZONE).isoformat(),
                    'tasks': [],
                    'last_weekly_report': None,
                    'awaiting_menu_choice': False
                }
            else:
                conversation_state[phone]['is_gerente'] = True

            if incoming_msg:
                return handle_gerente_message(phone, incoming_msg)
            elif num_media > 0 and media_url:
                error_messages, transcribed_msg = message_handler.handle_audio_message(
                    media_url, phone, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
                )
                if error_messages:
                    utils.send_consecutive_messages(phone, error_messages, client, WHATSAPP_SENDER_NUMBER)
                    return "Error procesando audio", 200
                if transcribed_msg:
                    return handle_gerente_message(phone, transcribed_msg)
            else:
                logger.error("Mensaje del gerente sin contenido de texto o audio")
                return "Error: Mensaje sin contenido", 400

        else:
            logger.info(f"Identificado como cliente: {phone}")
            if phone not in conversation_state:
                conversation_state[phone] = {
                    'history': [],
                    'name_asked': 0,
                    'messages_without_response': 0,
                    'preferred_time': None,
                    'preferred_days': None,
                    'client_name': None,
                    'client_budget': None,
                    'last_contact': datetime.now(CST_TIMEZONE).isoformat(),
                    'recontact_attempts': 0,
                    'no_interest': False,
                    'schedule_next': None,
                    'last_incoming_time': datetime.now(CST_TIMEZONE).isoformat(),
                    'last_response_time': datetime.now(CST_TIMEZONE).isoformat(),
                    'first_contact': datetime.now(CST_TIMEZONE).isoformat(),
                    'introduced': False,
                    'project_info_shared': {},
                    'last_mentioned_project': None,
                    'pending_question': None,
                    'pending_response_time': None,
                    'is_gerente': False,
                    'priority': False,
                    'stage': 'Prospección',
                    'interest_level': 0,
                    'reminder_sent': False,
                    'zoom_proposed': False,
                    'zoom_scheduled': False,
                    'zoom_details': {},
                    'intention_history': []
                }

            if not conversation_state[phone].get('introduced', False):
                conversation_state[phone]['introduced'] = True
                conversation_state[phone]['name_asked'] = 1
                messages = ["Hola, soy Giselle de FAV Living, desarrolladora inmobiliaria. ¿Podrías darme tu nombre para registrarte?"]
                utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
                conversation_state[phone]['history'].append(f"Giselle: {messages[0]}")
                utils.save_conversation(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                return "Mensaje enviado", 200

            if incoming_msg:
                return handle_client_message(phone, incoming_msg, num_media, media_url, profile_name)
            elif num_media > 0 and media_url:
                error_messages, transcribed_msg = message_handler.handle_audio_message(
                    media_url, phone, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
                )
                if error_messages:
                    utils.send_consecutive_messages(phone, error_messages, client, WHATSAPP_SENDER_NUMBER)
                    return "Error procesando audio", 200
                if transcribed_msg:
                    return handle_client_message(phone, transcribed_msg, num_media=0, media_url=None, profile_name=profile_name)
            else:
                logger.error("Mensaje del cliente sin contenido de texto o audio")
                return "Error: Mensaje sin contenido", 400

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
            if not conversation_state[phone].get('is_gerente', False):
                conversation_state[phone]['history'].append("Giselle: Lo siento, ocurrió un error. ¿En qué más puedo ayudarte?")
                utils.save_conversation(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
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
    current_time = datetime.now(CST_TIMEZONE)
    logger.debug(f"Current time (CST): {current_time}")

    recontact_window_start = current_time.replace(hour=RECONTACT_HOUR_CST, minute=RECONTACT_MINUTE_CST, second=0, microsecond=0) - timedelta(minutes=RECONTACT_TOLERANCE_MINUTES)
    recontact_window_end = current_time.replace(hour=RECONTACT_HOUR_CST, minute=RECONTACT_MINUTE_CST, second=0, microsecond=0) + timedelta(minutes=RECONTACT_TOLERANCE_MINUTES)

    for phone, state in list(conversation_state.items()):
        logger.debug(f"Processing client: {phone}")
        if state.get('is_gerente', False):
            logger.debug(f"Skipping {phone}: Is gerente")
            continue
        if state.get('no_interest', False):
            logger.debug(f"Skipping {phone}: No interest")
            continue

        last_response_time = state.get('last_response_time')
        logger.debug(f"Last response time for {phone}: {last_response_time}")
        if not last_response_time:
            logger.debug(f"Skipping {phone}: No last response time")
            continue

        try:
            last_response = datetime.fromisoformat(last_response_time).astimezone(CST_TIMEZONE)
        except ValueError as e:
            logger.error(f"Invalid last_response_time format for {phone}: {last_response_time}, error: {str(e)}")
            continue

        recontact_time = last_response + timedelta(days=RECONTACT_MIN_DAYS)
        recontact_time = recontact_time.replace(hour=RECONTACT_HOUR_CST, minute=RECONTACT_MINUTE_CST, second=0, microsecond=0)
        logger.debug(f"Scheduled recontact time for {phone}: {recontact_time}")

        if not (recontact_window_start <= current_time <= recontact_window_end):
            logger.debug(f"Skipping {phone}: Current time {current_time} is outside recontact window ({recontact_window_start} to {recontact_window_end})")
            continue

        if recontact_time.date() != current_time.date():
            logger.debug(f"Skipping {phone}: Recontact date {recontact_time.date()} does not match today {current_time.date()}")
            continue

        if state.get('recontact_attempts', 0) >= 3:
            logger.debug(f"Marking {phone} as no interest: Max recontact attempts reached")
            state['no_interest'] = True
            continue

        if check_whatsapp_window(phone):
            logger.debug(f"{phone} is within 24-hour window")
            client_name = state.get('client_name', 'Cliente')
            last_mentioned_project = state.get('last_mentioned_project', 'uno de nuestros proyectos')
            messages = [
                f"Hola {client_name}, soy Giselle de FAV Living. Quería dar seguimiento a nuestra conversación sobre {last_mentioned_project}.",
                "¿Te gustaría saber más detalles o prefieres que hagas un análisis financiero de la inversión?"
            ]

            utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
            for msg in messages:
                state['history'].append(f"Giselle: {msg}")
        else:
            logger.debug(f"{phone} is outside 24-hour window, sending template message")
            client_name = state.get('client_name', 'Cliente')
            last_mentioned_project = state.get('last_mentioned_project', 'uno de nuestros proyectos')
            if send_template_message(phone, client_name, last_mentioned_project):
                state['history'].append(f"Giselle: [Template] Hola {client_name}, soy Giselle de FAV Living. Quería dar seguimiento a nuestra conversación sobre {last_mentioned_project}.")
            else:
                logger.error(f"Failed to send template message to {phone}")

        state['recontact_attempts'] = state.get('recontact_attempts', 0) + 1
        state['schedule_next'] = None

        utils.save_conversation(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)

    for gerente_phone, gerente_state in list(conversation_state.items()):
        if not gerente_state.get('is_gerente', False):
            continue

        last_report = gerente_state.get('last_weekly_report')
        if last_report:
            last_report_time = datetime.fromisoformat(last_report).astimezone(CST_TIMEZONE)
            if (current_time - last_report_time).days < 7:
                continue

        if current_time.strftime('%A') == WEEKLY_REPORT_DAY and current_time.strftime('%H:%M') >= WEEKLY_REPORT_TIME:
            report_messages = generate_detailed_report(conversation_state)
            utils.send_consecutive_messages(gerente_phone, report_messages, client, WHATSAPP_SENDER_NUMBER)
            gerente_state['last_weekly_report'] = current_time.isoformat()

    logger.info("Recontact scheduling completed")
    return "Recontact scheduling triggered"

# Application Startup
if __name__ == '__main__':
    try:
        logger.info("Starting application initialization...")
        utils.load_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_BASE_PATH)
        logger.info("Conversation state loaded")
        utils.download_projects_from_storage(GCS_BUCKET_NAME, GCS_BASE_PATH)
        logger.info("Projects downloaded from storage")
        utils.load_projects_from_folder(GCS_BASE_PATH)
        logger.info("Projects loaded from folder")
        utils.load_gerente_respuestas(GCS_BASE_PATH)
        logger.info("Gerente responses loaded")
        utils.load_faq_files(GCS_BASE_PATH)
        logger.info("FAQ files loaded")
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
        app.run(host='0.0.0.0', port=port, debug=False)
        logger.info(f"Servidor Flask iniciado en el puerto {port}.")
    except Exception as e:
        logger.error(f"Failed to start application: {str(e)}")
        sys.exit(1)

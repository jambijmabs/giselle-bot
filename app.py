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

def handle_gerente_message(phone, incoming_msg):
    logger.info(f"Handling message from gerente ({phone})")

    incoming_msg_lower = incoming_msg.lower()

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
                ["Tu respuesta parece poco clara. Podrías proporcionar más detalles?"],
                client,
                WHATSAPP_SENDER_NUMBER
            )
            return "Respuesta poco clara", 200

        gerente_messages = [f"Gracias por esperar. Sobre tu pregunta: {answer}"]
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

        utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        utils.save_conversation_history(client_phone, conversation_state[client_phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
        utils.save_client_info(client_phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)

        utils.send_consecutive_messages(phone, ["Respuesta enviada al cliente. Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
        return "Mensaje enviado", 200

    if "reporte" in incoming_msg_lower or "interesados" in incoming_msg_lower:
        logger.info(f"Gerente ({phone}) requested a report of interested clients")
        report_messages = utils.generate_interested_report(conversation_state)
        utils.send_consecutive_messages(phone, report_messages, client, WHATSAPP_SENDER_NUMBER)
        logger.debug(f"Sent report to gerente: {report_messages}")
        utils.send_consecutive_messages(phone, ["Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
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
                "Estos son los nombres de los clientes interesados:",
                ", ".join(interested_clients)
            ]
        else:
            messages = ["No hay clientes interesados registrados en este momento."]
        utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
        utils.send_consecutive_messages(phone, ["Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
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
            utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.send_consecutive_messages(phone, [f"Cliente {client_phone} marcado como prioritario.", "Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
            return "Cliente marcado como prioritario", 200
        else:
            utils.send_consecutive_messages(phone, ["No encontré al cliente especificado o es un gerente.", "En qué más puedo asistirte?"], client, WHATSAPP_SENDER_NUMBER)
            return "Cliente no encontrado", 200

    if "resumen del día" in incoming_msg_lower:
        logger.info(f"Gerente ({phone}) requested daily activity summary")
        summary_messages = utils.generate_daily_summary(conversation_state)
        utils.send_consecutive_messages(phone, summary_messages, client, WHATSAPP_SENDER_NUMBER)
        utils.send_consecutive_messages(phone, ["Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
        return "Resumen enviado", 200

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
                'date': (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
            }
            if 'tasks' not in conversation_state[phone]:
                conversation_state[phone]['tasks'] = []
            conversation_state[phone]['tasks'].append(task)
            utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.send_consecutive_messages(phone, [f"Tarea asignada: Llamar a {client_phone} mañana a las {time_str}.", "Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
            return "Tarea asignada", 200
        else:
            utils.send_consecutive_messages(phone, ["No encontré al cliente especificado o es un gerente.", "En qué más puedo asistirte?"], client, WHATSAPP_SENDER_NUMBER)
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
            status = 'Esperando Respuesta' if state.get('pending_question') else 'No Interesado' if state.get('no_interest', False) else 'Interesado'
            last_messages = state.get('history', [])[-3:] if state.get('history') else ['Sin mensajes']
            messages = [
                f"Información del Cliente {client_phone}",
                f"Nombre: {client_name}",
                f"Proyecto: {project}",
                f"Presupuesto: {budget}",
                f"Estado: {status}",
                "Últimos mensajes:"
            ]
            messages.extend([f"- {msg}" for msg in last_messages])
            utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
            utils.send_consecutive_messages(phone, ["Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
            return "Información enviada", 200
        else:
            utils.send_consecutive_messages(phone, ["No encontré al cliente especificado o es un gerente.", "En qué más puedo asistirte?"], client, WHATSAPP_SENDER_NUMBER)
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
                utils.send_consecutive_messages(phone, ["Ocurrió un error al guardar la FAQ.", "En qué más puedo asistirte?"], client, WHATSAPP_SENDER_NUMBER)
                return "Error al guardar FAQ", 500

            utils.send_consecutive_messages(phone, [f"FAQ añadida para {project}: {question}.", "Necesitas algo más?"], client, WHATSAPP_SENDER_NUMBER)
            return "FAQ añadida", 200
        else:
            utils.send_consecutive_messages(phone, ["Formato incorrecto. Usa: Añade FAQ para [Proyecto]: Pregunta: [Pregunta] Respuesta: [Respuesta]", "En qué más puedo asistirte?"], client, WHATSAPP_SENDER_NUMBER)
            return "Formato incorrecto", 200

    messages = [
        "No entendí tu solicitud. Puedo ayudarte con reportes de interesados, nombres de clientes, responder dudas de clientes, marcar clientes como prioritarios, asignar tareas, buscar clientes, o añadir FAQs.",
        "En qué más puedo asistirte?"
    ]
    utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
    return "Mensaje recibido", 200

def handle_client_message(phone, incoming_msg, num_media, media_url=None):
    logger.info(f"Handling message from client ({phone})")

    try:
        logger.debug(f"Loading conversation history for {phone}")
        try:
            history = utils.load_conversation_history(phone, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            if not isinstance(history, list):
                logger.warning(f"Conversation history for {phone} is not a list: {history}")
                history = []
        except Exception as e:
            logger.error(f"Failed to load conversation history for {phone}: {str(e)}")
            history = []

        logger.debug(f"Updating conversation state for {phone}")
        if phone not in conversation_state:
            logger.warning(f"Client {phone} not found in conversation_state, initializing")
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
                'last_mentioned_project': None,
                'pending_question': None,
                'pending_response_time': None,
                'is_gerente': False,
                'priority': False
            }

        conversation_state[phone]['history'] = history
        conversation_state[phone]['history'].append(f"Cliente: {incoming_msg}")
        conversation_state[phone]['history'] = conversation_state[phone]['history'][-10:]
        conversation_state[phone]['last_contact'] = datetime.now().isoformat()
        conversation_state[phone]['messages_since_budget_ask'] = conversation_state[phone].get('messages_since_budget_ask', 0) + 1

        if conversation_state[phone].get('priority', False):
            for gerente_phone in [p for p, s in conversation_state.items() if s.get('is_gerente', False)]:
                utils.send_consecutive_messages(
                    gerente_phone,
                    [f"Cliente prioritario {phone} ha enviado un mensaje: {incoming_msg}"],
                    client,
                    WHATSAPP_SENDER_NUMBER
                )

        logger.debug(f"Checking for pending responses for {phone}")
        if conversation_state[phone].get('pending_response_time'):
            current_time = time.time()
            elapsed_time = current_time - conversation_state[phone]['pending_response_time']
            if elapsed_time >= FAQ_RESPONSE_DELAY:
                question = conversation_state[phone].get('pending_question', {}).get('question')
                mentioned_project = conversation_state[phone].get('pending_question', {}).get('mentioned_project')
                if question:
                    logger.debug(f"Fetching FAQ answer for question '{question}' about project '{mentioned_project}'")
                    answer = utils.get_faq_answer(question, mentioned_project)
                    if answer:
                        messages = [f"Gracias por esperar. Sobre tu pregunta: {answer}"]
                        utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
                        conversation_state[phone]['history'].append(f"Giselle: {messages[0]}")
                        conversation_state[phone]['pending_question'] = None
                        conversation_state[phone]['pending_response_time'] = None
                        logger.debug(f"Sent gerente response to client {phone}: {messages}")
                    else:
                        logger.error(f"Could not find answer for question '{question}' in FAQ.")
                        messages = ["Lo siento, no pude encontrar una respuesta. En qué más puedo ayudarte?"]
                        utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
                        conversation_state[phone]['history'].append(f"Giselle: {messages[0]}")
                        conversation_state[phone]['pending_question'] = None
                        conversation_state[phone]['pending_response_time'] = None
                else:
                    logger.error(f"No pending question found for {phone} despite pending_response_time.")
                    conversation_state[phone]['pending_response_time'] = None
            else:
                logger.debug(f"Waiting for FAQ response delay to complete for {phone}. Elapsed time: {elapsed_time} seconds")
                utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                return "Waiting for gerente response", 200

        # Use AI to extract the client name if not already set
        if not conversation_state[phone].get('client_name') and conversation_state[phone].get('name_asked', 0) > 0:
            name = message_handler.extract_name(incoming_msg, conversation_state[phone]['history'])
            if name:
                conversation_state[phone]['client_name'] = name.capitalize()
                logger.info(f"Client name set to: {conversation_state[phone]['client_name']}")
                utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                conversation_state[phone]['name_asked'] = conversation_state[phone].get('name_asked', 0) + 1

        # Check for no-interest phrases
        logger.debug(f"Checking for no-interest phrases in message: {incoming_msg}")
        if any(phrase in incoming_msg.lower() for phrase in bot_config.NO_INTEREST_PHRASES):
            conversation_state[phone]['no_interest'] = True
            messages = bot_config.handle_no_interest_response()
            logger.info(f"Sending no-interest response: {messages}")
            utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
            conversation_state[phone]['history'].append(f"Giselle: {messages[0]}")
            utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            return "Mensaje enviado", 200

        # Check for recontact request
        logger.debug(f"Checking for recontact request in message: {incoming_msg}")
        recontact_response = bot_config.handle_recontact_request(incoming_msg, conversation_state[phone])
        if recontact_response:
            messages = recontact_response
            logger.info(f"Sending recontact response: {messages}")
            utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
            conversation_state[phone]['history'].append(f"Giselle: {messages[0]}")
            utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
            return "Mensaje enviado", 200

        # Prepare project information
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
                if project.lower() in incoming_msg.lower() or ("departamentos" in incoming_msg.lower() and "condohotel" in data.get('type', '').lower()):
                    conversation_state[phone]['last_mentioned_project'] = project
        except Exception as project_info_e:
            logger.error(f"Error preparing project information: {str(project_info_e)}")
            project_info = "Información de proyectos no disponible."

        # Build conversation history
        logger.debug("Building conversation history")
        conversation_history = "\n".join(conversation_state[phone]['history'])

        # Check FAQ for an existing answer
        logger.debug(f"Checking FAQ for an existing answer")
        mentioned_project = conversation_state[phone].get('last_mentioned_project')
        faq_answer = utils.get_faq_answer(incoming_msg, mentioned_project)
        if faq_answer:
            messages = [f"Según lo que ya hemos investigado: {faq_answer}"]
        else:
            # Process the message using AI for a more natural response
            logger.debug(f"Processing message with message_handler: {incoming_msg}")
            messages, mentioned_project = message_handler.process_message(
                incoming_msg, phone, conversation_state, project_info, conversation_history
            )
            logger.debug(f"Messages generated: {messages}")
            logger.debug(f"Mentioned project after processing: {mentioned_project}")

            # If the bot needs to contact the gerente
            if "No tengo esa información a la mano, pero puedo revisarlo con el gerente, te parece?" in messages:
                conversation_state[phone]['pending_question'] = {
                    'question': incoming_msg,
                    'mentioned_project': mentioned_project,
                    'client_phone': phone
                }
                logger.debug(f"Set pending question for {phone}: {conversation_state[phone]['pending_question']}")
                # Notify the gerente about the pending question
                for gerente_phone in [p for p, s in conversation_state.items() if s.get('is_gerente', False)]:
                    utils.send_consecutive_messages(
                        gerente_phone,
                        [f"Nueva pregunta de cliente ({phone}): {incoming_msg}", "Por favor, responde con la información solicitada."],
                        client,
                        WHATSAPP_SENDER_NUMBER
                    )
            else:
                logger.debug(f"No gerente contact needed for message: {incoming_msg}")

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

        logger.debug(f"From phone: {phone}, Message: {incoming_msg}, NumMedia: {num_media}, MediaUrl: {media_url}")

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
                    'last_contact': datetime.now().isoformat(),
                    'last_incoming_time': datetime.now().isoformat(),
                    'tasks': []
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
                    'last_mentioned_project': None,
                    'pending_question': None,
                    'pending_response_time': None,
                    'is_gerente': False,
                    'priority': False
                }

            # Check if the client has been introduced; if not, introduce the bot
            if not conversation_state[phone].get('introduced', False):
                conversation_state[phone]['introduced'] = True
                conversation_state[phone]['name_asked'] = 1
                messages = ["Hola, soy Giselle de FAV Living, desarrolladora inmobiliaria. Podrías darme tu nombre para registrarte?"]
                utils.send_consecutive_messages(phone, messages, client, WHATSAPP_SENDER_NUMBER)
                conversation_state[phone]['history'].append(f"Giselle: {messages[0]}")
                utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                return "Mensaje enviado", 200

            if incoming_msg:
                return handle_client_message(phone, incoming_msg, num_media, media_url)
            elif num_media > 0 and media_url:
                error_messages, transcribed_msg = message_handler.handle_audio_message(
                    media_url, phone, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
                )
                if error_messages:
                    utils.send_consecutive_messages(phone, error_messages, client, WHATSAPP_SENDER_NUMBER)
                    return "Error procesando audio", 200
                if transcribed_msg:
                    return handle_client_message(phone, transcribed_msg, num_media=0, media_url=None)
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
                body="Lo siento, ocurrió un error. En qué más puedo ayudarte?",
                to=phone
            )
            logger.info(f"Fallback message sent: SID {message.sid}, Estado: {message.status}")
            if not conversation_state[phone].get('is_gerente', False):
                conversation_state[phone]['history'].append("Giselle: Lo siento, ocurrió un error. En qué más puedo ayudarte?")
                utils.save_conversation_state(conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                utils.save_conversation_history(phone, conversation_state[phone]['history'], GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
                utils.save_client_info(phone, conversation_state, GCS_BUCKET_NAME, GCS_CONVERSATIONS_PATH)
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
        if state.get('is_gerente', False):
            continue
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

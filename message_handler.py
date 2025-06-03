import re
import logging
import requests
import json
from openai import OpenAI
import bot_config
import traceback
import os
import utils
from twilio.rest import Client
from datetime import datetime, timedelta
import twilio

# Configure logger
logger = logging.getLogger(__name__)

# Initialize OpenAI client and global data
openai_client = None
projects_data = {}
downloadable_urls = {}

# Twilio client for sending messages to the gerente
twilio_client = None
whatsapp_sender_number = "whatsapp:+18188732305"
gerente_phone = bot_config.GERENTE_PHONE

def initialize_message_handler(openai_api_key, projects_data_ref, downloadable_urls_ref, twilio_account_sid, twilio_auth_token):
    global openai_client, projects_data, downloadable_urls, twilio_client
    openai_client = OpenAI(api_key=openai_api_key)
    projects_data = projects_data_ref
    downloadable_urls = downloadable_urls_ref
    try:
        twilio_client = Client(twilio_account_sid, twilio_auth_token)
        logger.debug(f"Twilio client initialized with account SID: {twilio_account_sid}")
        logger.info(f"Using twilio-python version: {twilio.__version__}")
    except Exception as e:
        logger.error(f"Failed to initialize Twilio client: {str(e)}", exc_info=True)
        twilio_client = None

def check_whatsapp_window(phone):
    if twilio_client is None:
        logger.error("Twilio client not initialized, cannot check WhatsApp window.")
        return False
    try:
        messages = twilio_client.messages.list(
            from_=phone,
            to=whatsapp_sender_number,
            date_sent_after=datetime.utcnow() - timedelta(hours=24)
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

def handle_gerente_response(incoming_msg, phone, conversation_state, gcs_bucket_name):
    logger.info(f"Processing gerente response from {phone}: {incoming_msg}")
    
    client_phone = None
    for client, state in conversation_state.items():
        if 'pending_question' in state and state['pending_question'] and state['pending_question'].get('client_phone') == client:
            client_phone = client
            logger.debug(f"Found pending question for client {client}: {state['pending_question']}")
            break
        else:
            logger.debug(f"No pending question for client {client}: {state.get('pending_question', 'None')}")

    if not client_phone or 'pending_question' not in conversation_state.get(client_phone, {}):
        logger.error(f"No pending question found for gerente response. Client phone: {client_phone}, Conversation state for client: {conversation_state.get(client_phone, {})}")
        return None, None

    answer = incoming_msg.strip()
    logger.debug(f"Gerente response: {answer}")

    messages = [f"Gracias por esperar, aqui tienes: {answer}"]
    logger.debug(f"Prepared response for client {client_phone}: {messages}")

    return client_phone, messages

def detect_intention(incoming_msg, conversation_history, is_gerente=False):
    logger.debug(f"Detecting intention for message: {incoming_msg}")
    
    role = "gerente" if is_gerente else "cliente"
    prompt = (
        f"Eres un asistente que identifica la intención detrás de un mensaje de un {role}. "
        f"Tu tarea es clasificar la intención del mensaje en una de las siguientes categorías y extraer información relevante:\n"
        f"- Para gerente: report (solicitar reporte), client_search (buscar cliente), add_faq (añadir FAQ), priority (marcar prioritario), task (asignar tarea), daily_summary (resumen diario), response (responder a cliente), schedule_zoom (programar Zoom), unknown (desconocido).\n"
        f"- Para cliente: question (pregunta sobre proyecto), external_question (pregunta externa al proyecto), greeting (saludo), budget (informar presupuesto), needs (informar necesidades), purchase_intent (informar interés de compra), offer_response (respuesta a oferta), contact_preference (preferencia de contacto), no_interest (desinterés), negotiation (negociar oferta), confirm_sale (confirmar venta), confirm_deposit (confirmar recepción de depósito), unknown (desconocido).\n"
        f"Si el mensaje parece ser un nombre (una sola palabra sin contexto adicional) y el cliente ya ha proporcionado un nombre en el historial, clasifícalo como 'unknown' en lugar de 'greeting'.\n"
        f"Devuelve la intención y los datos relevantes (e.g., proyecto, número de teléfono, pregunta, respuesta, interés de compra) en formato JSON.\n\n"
        f"Historial de conversación:\n{conversation_history}\n\n"
        f"Mensaje: {incoming_msg}"
    )

    try:
        response = openai_client.chat.completions.create(
            model=bot_config.CHATGPT_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": incoming_msg}
            ],
            max_tokens=100,
            temperature=0.5
        )
        result = json.loads(response.choices[0].message.content.strip())
        logger.debug(f"Intention detected: {result}")
        return result
    except Exception as e:
        logger.error(f"Error detecting intention with OpenAI: {str(e)}")
        return {"intention": "unknown", "data": {}}

def process_message(incoming_msg, phone, conversation_state, project_info, conversation_history):
    logger.debug(f"Processing message: {incoming_msg}")
    messages = []
    mentioned_project = conversation_state[phone].get('last_mentioned_project')

    # Detectar proyecto en el mensaje actual
    normalized_msg = incoming_msg.lower().replace(" ", "")
    logger.debug(f"Normalized message for project detection: {normalized_msg}")
    
    for project in projects_data.keys():
        project_data = projects_data.get(project, {})
        project_type = project_data.get('type', '').lower() if isinstance(project_data, dict) else ''
        location = project_data.get('location', '').lower() if isinstance(project_data, dict) else ''
        
        if (project.lower() in normalized_msg or
            (location and location in normalized_msg) or
            ("departamentos" in normalized_msg and "condohotel" in project_type) or
            ("condohotel" in normalized_msg and "condohotel" in project_type)):
            mentioned_project = project
            break

    if not mentioned_project:
        logger.debug("No project mentioned in message; checking conversation history")
        for msg in conversation_history.split('\n'):
            normalized_hist_msg = msg.lower().replace(" ", "")
            for project in projects_data.keys():
                project_data = projects_data.get(project, {})
                project_type = project_data.get('type', '').lower() if isinstance(project_data, dict) else ''
                location = project_data.get('location', '').lower() if isinstance(project_data, dict) else ''
                if (project.lower() in normalized_hist_msg or
                    (location and location in normalized_hist_msg) or
                    ("departamentos" in normalized_hist_msg and "condohotel" in project_type) or
                    ("condohotel" in normalized_hist_msg and "condohotel" in project_type)):
                    mentioned_project = project
                    break
            if mentioned_project:
                break

    if not mentioned_project and projects_data:
        mentioned_project = list(projects_data.keys())[0]
    logger.debug(f"Determined mentioned_project: {mentioned_project}")

    client_name = conversation_state[phone].get('client_name', 'Cliente') or 'Cliente'
    logger.debug(f"Using client_name: {client_name}")

    # Prepare the project data for the AI
    project_data_dict = projects_data.get(mentioned_project, {})
    if not isinstance(project_data_dict, dict):
        logger.warning(f"project_data for {mentioned_project} is not a dict: {project_data_dict}")
        project_data_dict = {}
    
    project_data = project_data_dict.get('description', "Información no disponible para este proyecto.")
    project_data += "\n\nInformación Adicional:\n"
    project_data += f"Tipo: {project_data_dict.get('type', 'No especificado')}\n"
    project_data += f"Ubicación: {project_data_dict.get('location', 'No especificada')}\n"

    # Detect the intention of the message
    intention_result = detect_intention(incoming_msg, conversation_history, is_gerente=False)
    intention = intention_result.get("intention", "unknown")
    intention_data = intention_result.get("data", {})

    # Handle based on intention
    if intention == "greeting":
        # This should not happen if the client has already provided a name, but we'll handle it just in case
        messages = ["Hola, soy Giselle de FAV Living, desarrolladora inmobiliaria. Podrías darme tu nombre para registrarte?"]
        conversation_state[phone]['name_asked'] = conversation_state[phone].get('name_asked', 0) + 1
    elif intention == "budget":
        budget = intention_data.get("budget", "No especificado")
        conversation_state[phone]['client_budget'] = budget
        if not conversation_state[phone].get('needs_asked'):
            messages = ["Entendido, gracias por compartir tu presupuesto.", "Qué estás buscando en un proyecto?"]
            conversation_state[phone]['needs_asked'] = True
        elif not conversation_state[phone].get('purchase_intent_asked'):
            messages = [
                "Entendido, gracias por compartir tu presupuesto.",
                "En que plazo estás pensando comprar? (listo para comprar, 1-3 meses, 3-6 meses, 6-12 meses)"
            ]
            conversation_state[phone]['purchase_intent_asked'] = True
        else:
            # Proceed to offer a project if needs and purchase intent are already known
            client_budget = conversation_state[phone].get('client_budget', 'No especificado')
            client_needs = conversation_state[phone].get('needs', 'No especificadas')
            project_match = None
            for project, data in projects_data.items():
                description = data.get('description', '').lower()
                if client_budget.lower() != "no especificado":
                    price_match = re.search(r'(\$\d{1,3}(,\d{3})*(?:\.\d+)?\s*(?:USD|MXN)?)', description)
                    if price_match:
                        price = price_match.group(0).replace('$', '').replace(',', '').replace(' USD', '').replace(' MXN', '')
                        try:
                            price_value = float(price)
                            budget_value = float(re.search(r'\d+', client_budget).group(0)) * 1000000 if "millones" in client_budget.lower() else float(re.search(r'\d+', client_budget).group(0))
                            if price_value <= budget_value:
                                project_match = project
                                break
                        except ValueError:
                            continue
                if not project_match and client_needs.lower() != "no especificadas":
                    if "departamentos" in client_needs.lower() and "condohotel" in description:
                        project_match = project
                        break
            if not project_match:
                project_match = mentioned_project if mentioned_project else list(projects_data.keys())[0]

            conversation_state[phone]['offered_project'] = project_match
            project_data_dict = projects_data.get(project_match, {})
            project_description = project_data_dict.get('description', "Información no disponible para este proyecto.")
            project_type = project_data_dict.get('type', 'No especificado')
            project_location = project_data_dict.get('location', 'No especificada')

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

def extract_name(incoming_msg, conversation_history):
    """Use AI to extract the client's name from their message."""
    logger.debug(f"Extracting name from message: {incoming_msg}")
    prompt = (
        "Eres un asistente que extrae el nombre de una persona de un mensaje. "
        "El mensaje puede contener frases como 'me llamo', 'mi nombre es', 'soy', o simplemente un nombre propio. "
        "Tu tarea es identificar y extraer únicamente el nombre propio (sin apellidos ni contexto adicional) del mensaje. "
        "Si no hay un nombre claro en el mensaje, retorna None. "
        "Devuelve el nombre en formato de texto plano.\n\n"
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
            max_tokens=50,
            temperature=0.5
        )
        name = response.choices[0].message.content.strip()
        if name.lower() == "none" or not name:
            return None
        return name
    except Exception as e:
        logger.error(f"Error extracting name with OpenAI: {str(e)}")
        return None

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

def needs_gerente_contact(response, question, project_data):
    """Use AI to determine if the response indicates the bot lacks information and needs to contact the gerente."""
    prompt = (
        "Eres un asistente que evalúa si una respuesta indica que el bot no tiene información suficiente y necesita consultar a un gerente. "
        "Analiza la respuesta generada por el bot, la pregunta del cliente, y los datos del proyecto. "
        "Si la respuesta implica que el bot no tiene la información exacta o completa para responder la pregunta (por ejemplo, si dice que algo 'no está confirmado' o que 'necesita verificar'), retorna True. "
        "Si la respuesta es clara y utiliza información disponible en los datos del proyecto, retorna False. "
        "Devuelve únicamente True o False en formato de texto plano.\n\n"
        f"Pregunta del cliente: {question}\n"
        f"Respuesta del bot: {response}\n"
        f"Datos del proyecto: {project_data}"
    )

    try:
        response = openai_client.chat.completions.create(
            model=bot_config.CHATGPT_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": ""}
            ],
            max_tokens=10,
            temperature=0.5
        )
        result = response.choices[0].message.content.strip().lower()
        return result == "true"
    except Exception as e:
        logger.error(f"Error determining if gerente contact is needed: {str(e)}")
        return False  # Fallback to False to avoid breaking the flow

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

    # Handle critical intents with minimal predefined logic
    if intention == "no_interest":
        conversation_state[phone]['no_interest'] = True
        messages = bot_config.handle_no_interest_response()
    elif intention == "confirm_sale":
        if "sí" in incoming_msg.lower() or "si" in incoming_msg.lower() or "confirmo" in incoming_msg.lower():
            messages = [
                f"¡Felicidades por tu decisión, {client_name}! La unidad 2B de MUWAN está apartada para ti.",
                "Te enviaré los datos bancarios para el depósito de $10,000 USD. Confirmaré la recepción del depósito para seguir con el proceso."
            ]
        else:
            messages = [f"Entiendo, {client_name}. Tómate tu tiempo para decidir. Si necesitas más información o ajustar algo, aquí estoy para ayudarte."]
    elif intention == "confirm_deposit":
        if "ya envié" in incoming_msg.lower() or "depositado" in incoming_msg.lower():
            messages = [
                f"¡Gracias por tu compra, {client_name}! Confirmaré la recepción del depósito y seguiremos con el proceso.",
                "Estaremos en contacto para los próximos pasos."
            ]
        else:
            messages = [f"Entendido, {client_name}. Cuando hagas el depósito, por favor avísame para confirmar. ¿Tienes alguna duda que pueda ayudarte a resolver?"]
    else:
        # Use AI to generate a natural response for all other intents
        client_budget = conversation_state[phone].get('client_budget', 'No especificado')
        client_needs = conversation_state[phone].get('needs', 'No especificadas')
        client_purchase_intent = conversation_state[phone].get('purchase_intent', 'No especificado')
        prompt = (
            f"{bot_config.BOT_PERSONALITY}\n\n"
            f"Instrucciones para las respuestas:\n"
            f"Actúa como una asesora de ventas profesional, amigable y cálida de FAV Living. Tu objetivo principal es informar al cliente sobre los proyectos, generar interés y resolver dudas de manera natural, sin apresurarte a hacer una oferta a menos que el cliente muestre un interés claro en avanzar con una compra. "
            f"Prioriza entregar información detallada y útil sobre los proyectos, invítalo a explorar más detalles o a aclarar dudas antes de sugerir una oferta. "
            f"Evita hacer preguntas rígidas o seguir un flujo predeterminado; en lugar de eso, adapta tus respuestas al contexto del mensaje del cliente para que la conversación sea fluida y amigable. "
            f"Usa un tono cálido y profesional, y siempre dirígete al cliente por su nombre ({client_name}) cuando sea posible.\n\n"
            f"Información del cliente:\n"
            f"Presupuesto: {client_budget}\n"
            f"Necesidades: {client_needs}\n"
            f"Intención de compra: {client_purchase_intent}\n\n"
            f"Información de los proyectos disponibles:\n"
            f"{project_info}\n\n"
            f"Datos específicos del proyecto {mentioned_project}:\n"
            f"{project_data}\n\n"
            f"Historial de conversación:\n"
            f"{conversation_history}\n\n"
            f"Mensaje del cliente: {incoming_msg}\n\n"
            f"Responde de forma breve, natural y profesional, enfocándote en generar interés en los proyectos. "
            f"Si el cliente pregunta por algo que no está en los datos del proyecto y es inherente al proyecto (como amenidades específicas o fechas exactas de entrega), responde indicando que no tienes esa información y que puedes consultar con el gerente."
        )
        logger.debug(f"Sending request to OpenAI for client message: '{incoming_msg}', project: {mentioned_project}")

        try:
            response = openai_client.chat.completions.create(
                model=bot_config.CHATGPT_MODEL,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": incoming_msg}
                ],
                max_tokens=150,
                temperature=0.7
            )
            reply = response.choices[0].message.content.strip()
            logger.debug(f"Generated response from OpenAI: {reply}")

            # Split the response into messages
            current_message = ""
            sentences = reply.split('. ')
            for sentence in sentences:
                if not sentence:
                    continue
                sentence = sentence.strip()
                if sentence:
                    if len(current_message.split('\n')) < 2 and len(current_message) < 100:
                        current_message += (sentence + '. ') if current_message else sentence + '. '
                    else:
                        messages.append(current_message.strip())
                        current_message = sentence + '. '
            if current_message:
                messages.append(current_message.strip())

            if not messages:
                messages = [f"No tengo esa información a la mano, {client_name}, pero puedo revisarlo con el gerente si te parece."]

            # Use AI to determine if the response indicates the bot needs to contact the gerente
            if needs_gerente_contact(reply, incoming_msg, project_data):
                messages.append(f"No tengo esa información exacta, {client_name}, pero puedo revisarlo con el gerente, ¿te parece?")
                return messages, mentioned_project, True  # Indicate that gerente contact is needed

        except Exception as openai_e:
            logger.error(f"Fallo con OpenAI API: {str(openai_e)}", exc_info=True)
            messages = [f"No tengo esa información a la mano, {client_name}, pero puedo revisarlo con el gerente si te parece."]
            return messages, mentioned_project, True  # Indicate that gerente contact is needed

    logger.debug(f"Final messages: {messages}")
    return messages, mentioned_project, False  # Default to no gerente contact needed

def handle_audio_message(media_url, phone, twilio_account_sid, twilio_auth_token):
    logger.debug("Handling audio message")
    audio_response = requests.get(media_url, auth=(twilio_account_sid, twilio_auth_token))
    if audio_response.status_code != 200:
        logger.error(f"Failed to download audio: {audio_response.status_code}")
        return ["Lo siento, no pude procesar tu mensaje de audio. Podrías enviarlo como texto?"], None

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
        return None, incoming_msg
    except Exception as e:
        logger.error(f"Error transcribing audio: {str(e)}\n{traceback.format_exc()}")
        return ["Lo siento, no pude entender tu mensaje de audio. Podrías intentarlo de nuevo o escribirlo como texto?", f"Error details for debugging: {str(e)}"], None
    finally:
        if os.path.exists(audio_file_path):
            os.remove(audio_file_path)

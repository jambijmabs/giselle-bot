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
import difflib

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

    messages = [f"Gracias por esperar, aquí tienes: {answer}. ¿En qué más puedo ayudarte?"]
    logger.debug(f"Prepared response for client {client_phone}: {messages}")

    return client_phone, messages

def correct_typo(text, known_words):
    """Correct typographical errors by finding the closest match in known_words."""
    text_lower = text.lower()
    matches = difflib.get_close_matches(text_lower, known_words, n=1, cutoff=0.8)
    if matches:
        corrected = matches[0]
        logger.debug(f"Corrected typo '{text}' to '{corrected}'")
        return corrected
    return text_lower

def extract_name(incoming_msg, conversation_history):
    """Extract the client's name from their message or history using AI."""
    logger.debug(f"Extracting name from message: {incoming_msg}")
    prompt = (
        "Eres un asistente que extrae el nombre de una persona de un mensaje o historial de conversación. "
        "El mensaje puede contener frases como 'me llamo', 'mi nombre es', 'soy', o simplemente un nombre propio. "
        "Tu tarea es identificar y extraer únicamente el nombre propio (sin apellidos ni contexto adicional). "
        "Revisa también el historial para buscar nombres mencionados previamente. "
        "Si no hay un nombre claro en el mensaje o historial, retorna None. "
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
            max_tokens=20,
            temperature=0.3
        )
        name = response.choices[0].message.content.strip()
        if name.lower() == "none" or not name:
            return None
        return name
    except Exception as e:
        logger.error(f"Error extracting name with OpenAI: {str(e)}")
        return None

def is_ready_for_zoom(phone, conversation_state):
    """Determine if the client is ready to schedule a Zoom meeting."""
    state = conversation_state.get(phone, {})
    
    # Check if client has been profiled
    has_name = bool(state.get('client_name'))
    messages_count = sum(1 for msg in state.get('history', []) if msg.startswith("Cliente:"))
    has_interacted_enough = messages_count >= 4  # Increased from 2 to 4 for more natural flow
    
    # Check if client has shown significant interest
    intention_history = state.get('intention_history', [])
    has_shown_significant_interest = (
        sum(1 for intent in intention_history if intent in ["question", "budget", "needs", "purchase_intent", "negotiation"]) >= 2
    )  # Require at least 2 meaningful intents
    
    # Check if Zoom has already been proposed
    zoom_proposed = state.get('zoom_proposed', False)
    
    ready = has_name and has_interacted_enough and has_shown_significant_interest and not zoom_proposed
    logger.debug(f"Client {phone} ready for Zoom: {ready} (has_name={has_name}, has_interacted_enough={has_interacted_enough}, has_shown_significant_interest={has_shown_significant_interest}, zoom_proposed={zoom_proposed})")
    return ready

def propose_zoom_meeting(client_name):
    """Generate messages to propose a Zoom meeting with available slots."""
    slots_text = "\n".join([f"- {slot['day']}: {', '.join(slot['times'])}" for slot in bot_config.ZOOM_AVAILABLE_SLOTS])
    messages = [msg.format(slots=slots_text, client_name=client_name) for msg in bot_config.ZOOM_PROPOSAL_MESSAGE]
    return messages

def confirm_zoom_meeting(phone, client_name, day, time, conversation_state):
    """Confirm a Zoom meeting and notify the gerente."""
    valid_slot = False
    for slot in bot_config.ZOOM_AVAILABLE_SLOTS:
        if slot['day'].lower() == day.lower():
            if time in slot['times']:
                valid_slot = True
                break
    
    if not valid_slot:
        return [f"Lo siento, {client_name}, el horario '{day} a las {time}' no está disponible. Por favor, selecciona otro horario."], False

    messages = [msg.format(client_name=client_name, day=day, time=time) for msg in bot_config.ZOOM_CONFIRMATION_MESSAGE]
    
    conversation_state[phone]['zoom_scheduled'] = True
    conversation_state[phone]['zoom_details'] = {'day': day, 'time': time}
    
    notification = [msg.format(client_name=client_name, phone=phone, day=day, time=time) for msg in bot_config.ZOOM_NOTIFICATION_TO_GERENTE]
    utils.notify_gerente(notification, twilio_client, whatsapp_sender_number)
    
    return messages, True

def detect_intention(incoming_msg, conversation_history, is_gerente=False):
    logger.debug(f"Detecting intention for message: {incoming_msg}")
    
    role = "gerente" if is_gerente else "cliente"
    prompt = (
        f"Eres un asistente que identifica la intención detrás de un mensaje de un {role}. "
        f"Tu tarea es clasificar la intención del mensaje en una de las siguientes categorías y extraer información relevante:\n"
        f"- Para gerente: report (solicitar reporte), client_search (buscar cliente), add_faq (añadir FAQ), priority (marcar prioritario), task (asignar tarea), daily_summary (resumen diario), response (responder a cliente), schedule_zoom (programar Zoom), unknown (desconocido).\n"
        f"- Para cliente: question (pregunta sobre proyecto), external_question (pregunta externa al proyecto), greeting (saludo), budget (informar presupuesto), needs (informar necesidades), purchase_intent (informar interés de compra), offer_response (respuesta a oferta), contact_preference (preferencia de contacto), no_interest (desinterés), negotiation (negociar oferta), confirm_sale (confirmar venta), confirm_deposit (confirmar recepción de depósito), schedule_zoom (agendar Zoom), zoom_response (respuesta a propuesta de Zoom), unknown (desconocido).\n"
        f"Si el mensaje incluye un día y horario (por ejemplo, 'Lunes a las 10:00 AM') y sigue a una propuesta de Zoom, clasifícalo como 'zoom_response'.\n"
        f"Si el mensaje es un nombre o carece de contexto claro, clasifícalo como 'unknown'.\n"
        f"Devuelve la intención y los datos relevantes (e.g., proyecto, número de teléfono, pregunta, respuesta, día y horario para Zoom) en formato JSON.\n\n"
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
            temperature=0.3
        )
        result = json.loads(response.choices[0].message.content.strip())
        logger.debug(f"Intention detected: {result}")
        return result
    except Exception as e:
        logger.error(f"Error detecting intention with OpenAI: {str(e)}")
        return {"intention": "unknown", "data": {}}

def needs_gerente_contact(response, question, project_data, conversation_history):
    """Determine if the bot needs to contact the gerente for more information."""
    if len(question.strip()) < 3 or question.lower() in ["sí", "si", "no", "hola", "gracias"]:
        logger.debug(f"Question '{question}' is too vague or not a question; not escalating to gerente.")
        return False

    prompt = (
        "Eres un asistente que evalúa si una respuesta indica que el bot no tiene información suficiente y necesita consultar a un gerente. "
        "Analiza la respuesta generada por el bot, la pregunta del cliente, los datos del proyecto y el historial de conversación. "
        "Si la respuesta implica que el bot no tiene la información exacta o completa para responder la pregunta (por ejemplo, si dice que algo 'no está confirmado' o que 'necesita verificar'), retorna True. "
        "Si la pregunta es ambigua o no tiene sentido en el contexto del historial, retorna False para evitar escalar preguntas sin sentido. "
        "Si la respuesta es clara y utiliza información disponible en los datos del proyecto, retorna False. "
        "Devuelve únicamente True o False en formato de texto plano.\n\n"
        f"Historial de conversación:\n{conversation_history}\n\n"
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
            temperature=0.3
        )
        result = response.choices[0].message.content.strip().lower()
        return result == "true"
    except Exception as e:
        logger.error(f"Error determining if gerente contact is needed: {str(e)}")
        return False

def ensure_question_in_response(messages, client_name):
    """Ensure the response ends with a question to keep the conversation active."""
    if not messages:
        return ["No entendí bien tu mensaje, ¿puedes darme más detalles?"]

    last_message = messages[-1]
    # Remove the generic question addition, as the OpenAI prompt now ensures a relevant question
    if not last_message.endswith('?'):
        logger.warning(f"Response does not end with a question: {last_message}")
    return messages

def process_message(incoming_msg, phone, conversation_state, project_info, conversation_history):
    logger.debug(f"Processing message: {incoming_msg}")
    messages = []
    state = conversation_state.get(phone, {})
    mentioned_project = state.get('last_mentioned_project')

    # Correct typographical errors in the message
    incoming_msg_corrected = incoming_msg.lower()
    project_names = list(projects_data.keys())
    for word in incoming_msg_corrected.split():
        corrected = correct_typo(word, project_names)
        if corrected != word:
            incoming_msg_corrected = incoming_msg_corrected.replace(word, corrected)

    # Detect project in the message
    normalized_msg = incoming_msg_corrected.replace(" ", "")
    for keyword, project in bot_config.PROJECT_KEYWORD_MAPPING.items():
        if keyword in normalized_msg:
            mentioned_project = project
            break

    if not mentioned_project:
        logger.debug("No project mentioned in message; checking conversation history")
        for msg in conversation_history.split('\n'):
            normalized_hist_msg = msg.lower().replace(" ", "")
            for keyword, project in bot_config.PROJECT_KEYWORD_MAPPING.items():
                if keyword in normalized_hist_msg:
                    mentioned_project = project
                    break
            if mentioned_project:
                break

    # Remove default project selection
    # Do not assign a default project if none is mentioned or found in history
    # Let OpenAI prompt handle the initial response with a question about preferences
    logger.debug(f"Determined mentioned_project: {mentioned_project}")

    client_name = state.get('client_name', 'Cliente') or 'Cliente'
    logger.debug(f"Using client_name: {client_name}")

    # Prepare the project data for the AI, if a project is mentioned
    project_data = "No hay un proyecto específico seleccionado aún."
    if mentioned_project:
        project_data_dict = projects_data.get(mentioned_project, {})
        if not isinstance(project_data_dict, dict):
            logger.warning(f"project_data for {mentioned_project} is not a dict: {project_data_dict}")
            project_data_dict = {}
        
        project_data = project_data_dict.get('description', "Información no disponible para este proyecto.")
        project_data += "\n\nInformación Adicional:\n"
        project_data += f"Tipo: {project_data_dict.get('type', 'No especificado')}\n"
        project_data += f"Ubicación: {project_data_dict.get('location', 'No especificada')}\n"

    # Detect the intention of the message
    intention_result = detect_intention(incoming_msg_corrected, conversation_history, is_gerente=False)
    intention = intention_result.get("intention", "unknown")
    intention_data = intention_result.get("data", {})

    # Add the detected intention to the client's state
    if 'intention_history' not in state:
        state['intention_history'] = []
    state['intention_history'].append(intention)

    # Handle Zoom-related intents
    if intention == "zoom_response":
        day = intention_data.get('day', '').capitalize()
        time = intention_data.get('time', '')
        zoom_messages, confirmed = confirm_zoom_meeting(phone, client_name, day, time, conversation_state)
        return zoom_messages, mentioned_project, False

    if intention == "schedule_zoom":
        zoom_messages = propose_zoom_meeting(client_name)
        state['zoom_proposed'] = True
        return zoom_messages, mentioned_project, False

    # Handle critical intents
    if intention == "no_interest":
        state['no_interest'] = True
        messages = bot_config.handle_no_interest_response()
    elif intention == "confirm_sale":
        if "sí" in incoming_msg_corrected or "si" in incoming_msg_corrected or "confirmo" in incoming_msg_corrected:
            messages = [
                f"¡Felicidades, {client_name}! La unidad 2B de MUWAN está apartada para ti.",
                "Te enviaré los datos bancarios para el depósito. ¿Confirmas que todo está en orden?"
            ]
        else:
            messages = [f"Entiendo, {client_name}. ¿Necesitas más tiempo para decidir?"]
    elif intention == "confirm_deposit":
        if "ya envié" in incoming_msg_corrected or "depositado" in incoming_msg_corrected:
            messages = [
                f"¡Gracias, {client_name}! Confirmaré la recepción del depósito.",
                "¿Deseas que te envíe más detalles del proceso?"
            ]
        else:
            messages = [f"Entendido, {client_name}. ¿Me avisas cuando hagas el depósito?"]
    else:
        # Use AI to generate a response
        client_budget = state.get('client_budget', 'No especificado')
        client_needs = state.get('needs', 'No especificadas')
        client_purchase_intent = state.get('purchase_intent', 'No especificado')
        prompt = (
            f"{bot_config.BOT_PERSONALITY}\n\n"
            f"Instrucciones para las respuestas:\n{bot_config.RESPONSE_INSTRUCTIONS}\n\n"
            f"Información del cliente:\n"
            f"Presupuesto: {client_budget}\n"
            f"Necesidades: {client_needs}\n"
            f"Intención de compra: {client_purchase_intent}\n\n"
            f"Información de los proyectos disponibles:\n"
            f"{project_info}\n\n"
            f"Datos específicos del proyecto (si aplica):\n"
            f"{project_data}\n\n"
            f"Historial de conversación:\n"
            f"{conversation_history}\n\n"
            f"Mensaje del cliente: {incoming_msg_corrected}\n\n"
            f"Responde de forma breve y profesional, enfocándote en el proyecto {mentioned_project if mentioned_project else 'ninguno seleccionado aún'}."
        )
        logger.debug(f"Sending request to OpenAI for client message: '{incoming_msg_corrected}', project: {mentioned_project}")

        try:
            response = openai_client.chat.completions.create(
                model=bot_config.CHATGPT_MODEL,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": incoming_msg_corrected}
                ],
                max_tokens=100,
                temperature=0.3
            )
            reply = response.choices[0].message.content.strip()
            logger.debug(f"Generated response from OpenAI: {reply}")

            # Split the response into messages, but avoid splitting mid-sentence
            messages = [reply]

            if not messages or messages == [""]:
                messages = [f"Entiendo, {client_name}. Voy a consultar eso para ti, ¿te parece bien que te cuente sobre otro proyecto mientras tanto?"]

            # Ensure the response ends with a question (handled by OpenAI prompt now)
            messages = ensure_question_in_response(messages, client_name)

            # Determine if gerente contact is needed
            if needs_gerente_contact(reply, incoming_msg_corrected, project_data, conversation_history):
                messages.append(f"Entiendo, {client_name}. Voy a consultar eso para ti, ¿te parece bien que te cuente sobre otro proyecto mientras tanto?")
                return messages, mentioned_project, True

        except Exception as openai_e:
            logger.error(f"Fallo con OpenAI API: {str(openai_e)}", exc_info=True)
            messages = [f"Entiendo, {client_name}. Voy a consultar eso para ti, ¿te parece bien que te cuente sobre otro proyecto mientras tanto?"]
            return messages, mentioned_project, True

    # Propose Zoom meeting if the client is ready
    if is_ready_for_zoom(phone, conversation_state) and not state.get('zoom_scheduled', False):
        zoom_messages = propose_zoom_meeting(client_name)
        state['zoom_proposed'] = True
        messages.extend(zoom_messages)

    # Update the mentioned project in the state if one was detected
    if mentioned_project:
        state['last_mentioned_project'] = mentioned_project

    logger.debug(f"Final messages: {messages}")
    return messages, mentioned_project, False

def handle_audio_message(media_url, phone, twilio_account_sid, twilio_auth_token):
    logger.debug("Handling audio message")
    audio_response = requests.get(media_url, auth=(twilio_account_sid, twilio_auth_token))
    if audio_response.status_code != 200:
        logger.error(f"Failed to download audio: {audio_response.status_code}")
        return ["Lo siento, no pude procesar tu mensaje de audio. ¿Puedes escribirlo?"], None

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
        return ["Lo siento, no pude entender tu mensaje de audio. ¿Puedes escribirlo?"], None
    finally:
        if os.path.exists(audio_file_path):
            os.remove(audio_file_path)

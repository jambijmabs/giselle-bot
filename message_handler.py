import re
import logging
import requests
from openai import OpenAI
import bot_config
import traceback
import os
import utils
from twilio.rest import Client
from datetime import datetime, timedelta
import twilio  # Importar twilio para verificar la versión

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
    """Check if the WhatsApp 24-hour messaging window is active for the given phone."""
    if twilio_client is None:
        logger.error("Twilio client not initialized, cannot check WhatsApp window.")
        return False
    try:
        # Buscar los mensajes recientes recibidos desde el número del gerente
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
        return False  # En caso de error, asumimos que la ventana no está activa

def handle_gerente_response(incoming_msg, phone, conversation_state, gcs_bucket_name):
    """Process a response from the gerente and prepare a message for the client."""
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

    # Check if the gerente's response starts with "respuestafaq:"
    if not incoming_msg.lower().startswith(bot_config.FAQ_RESPONSE_PREFIX.lower()):
        logger.debug(f"Gerente message does not start with '{bot_config.FAQ_RESPONSE_PREFIX}', ignoring per GERENTE_BEHAVIOR: {incoming_msg}")
        return None, None

    # Extract the actual response by removing the prefix
    answer = incoming_msg[len(bot_config.FAQ_RESPONSE_PREFIX):].strip()
    logger.debug(f"Extracted gerente FAQ response: {answer}")

    # Store the gerente's response in the appropriate FAQ file
    question = conversation_state[client_phone]['pending_question']['question']
    mentioned_project = conversation_state[client_phone]['pending_question']['mentioned_project']
    logger.debug(f"Saving gerente response for question '{question}' about project '{mentioned_project}' with answer '{answer}'")
    utils.save_gerente_respuesta(
        bot_config.GCS_BASE_PATH,
        question,
        answer,
        gcs_bucket_name,
        project=mentioned_project
    )

    # Update the global gerente_respuestas
    utils.gerente_respuestas[question] = answer
    logger.debug(f"Updated gerente_respuestas")

    # Prepare response for the client
    messages = [f"Gracias por esperar. Sobre tu pregunta: {answer}"]
    logger.debug(f"Prepared response for client {client_phone}: {messages}")

    # Clear the pending question
    conversation_state[client_phone]['pending_question'] = None
    conversation_state[client_phone]['pending_response_time'] = time.time()
    logger.debug(f"Cleared pending question for client {client_phone}")

    return client_phone, messages

def process_message(incoming_msg, phone, conversation_state, project_info, conversation_history):
    logger.debug(f"Processing message: {incoming_msg}")
    messages = []
    mentioned_project = conversation_state[phone].get('last_mentioned_project')

    # Detectar proyecto en el mensaje actual
    normalized_msg = incoming_msg.lower().replace(" ", "")
    logger.debug(f"Normalized message for project detection: {normalized_msg}")
    for project in projects_data.keys():
        if project.lower() in normalized_msg:
            mentioned_project = project
            break

    # Si no hay proyecto en el mensaje, usar el último del historial
    if not mentioned_project:
        logger.debug("No project mentioned in message; checking conversation history")
        for msg in conversation_history.split('\n'):
            for project in projects_data.keys():
                if project.lower() in msg.lower():
                    mentioned_project = project
                    break
            if mentioned_project:
                break
    if not mentioned_project and projects_data:
        mentioned_project = list(projects_data.keys())[0]
    logger.debug(f"Determined mentioned_project: {mentioned_project}")

    client_name = conversation_state[phone].get('client_name', 'Cliente') or 'Cliente'
    logger.debug(f"Using client_name: {client_name}")

    # Prepare the project data for the AI, including gerente responses
    project_data = projects_data.get(mentioned_project, "Información no disponible para este proyecto.")
    project_data += "\n\n**Respuestas del Gerente:**\n"
    for question, answer in utils.gerente_respuestas.items():
        project_data += f"Pregunta: {question}\nRespuesta: {answer}\n"

    # Build the prompt for the AI
    prompt = (
        f"{bot_config.BOT_PERSONALITY}\n\n"
        f"**Instrucciones para las respuestas:**\n"
        f"{bot_config.RESPONSE_INSTRUCTIONS}\n\n"
        f"**Información de los proyectos disponibles:**\n"
        f"{project_info}\n\n"
        f"**Datos específicos del proyecto {mentioned_project}:**\n"
        f"{project_data}\n\n"
        f"**Historial de conversación:**\n"
        f"{conversation_history}\n\n"
        f"**Mensaje del cliente:** \"{incoming_msg}\"\n\n"
        f"Responde de forma breve y profesional, enfocándote en la venta de propiedades. "
        f"Interpreta la información del proyecto de manera natural para responder a las preguntas del cliente, "
        f"como precios, URLs de archivos descargables, o cualquier otro detalle. "
        f"Si el cliente pregunta por algo que no está en los datos del proyecto, responde con una frase como "
        f"'No sé exactamente, pero déjame investigarlo con más detalle para ti.'"
    )
    # Log only relevant parts instead of the full prompt
    logger.debug(f"Sending request to OpenAI for client message: '{incoming_msg}', project: {mentioned_project}")

    try:
        response = openai_client.chat.completions.create(
            model=bot_config.CHATGPT_MODEL,
            messages=[
                {"role": "system", "content": bot_config.BOT_PERSONALITY},
                {"role": "user", "content": prompt}
            ],
            max_tokens=150,
            temperature=0.7
        )
        reply = response.choices[0].message.content.strip()
        logger.debug(f"Generated response from OpenAI: {reply}")

        # Check if the response indicates the bot doesn't know the answer
        if "no sé exactamente" in reply.lower() or "déjame investigarlo" in reply.lower():
            logger.info(f"Bot cannot answer: {incoming_msg}. Contacting gerente.")
            messages.append("Permíteme, déjame revisar esto con el gerente.")
            
            # Send message to gerente, including the project name and reminder
            project_context = f"sobre {mentioned_project}" if mentioned_project else "general"
            gerente_message = f"Pregunta de {client_name} {project_context}: {incoming_msg}\nRecuerda contestar con respuestafaq:"
            logger.debug(f"Preparing to send message to gerente: {gerente_message}")
            logger.debug(f"Sending to gerente_phone: {gerente_phone} from {whatsapp_sender_number}")

            try:
                if twilio_client is None:
                    raise Exception("Twilio client not initialized.")

                # Verificar si la ventana de 24 horas está activa
                logger.debug(f"Checking WhatsApp window for {gerente_phone}")
                window_active = check_whatsapp_window(gerente_phone)
                logger.debug(f"WhatsApp window active: {window_active}")

                # Enviar mensaje libre al gerente
                message = twilio_client.messages.create(
                    from_=whatsapp_sender_number,
                    body=gerente_message,
                    to=gerente_phone
                )
                logger.info(f"Sent message to gerente: SID {message.sid}, Estado: {message.status}")

                # Verificar el estado del mensaje
                updated_message = twilio_client.messages(message.sid).fetch()
                logger.info(f"Estado del mensaje actualizado: {updated_message.status}")
                if updated_message.status == "failed":
                    logger.error(f"Error al enviar mensaje al gerente: {updated_message.error_code} - {updated_message.error_message}")
                    messages = ["Lo siento, ocurrió un error al contactar al gerente. ¿En qué más puedo ayudarte?"]
            except Exception as twilio_e:
                logger.error(f"Error sending message to gerente via Twilio: {str(twilio_e)}", exc_info=True)
                messages = ["Lo siento, ocurrió un error al contactar al gerente. ¿En qué más puedo ayudarte?"]
        else:
            # Split the reply into messages
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
                messages = ["No sé exactamente, pero déjame investigarlo con más detalle para ti."]

    except Exception as openai_e:
        logger.error(f"Fallo con OpenAI API: {str(openai_e)}", exc_info=True)
        messages = ["Lo siento, no entiendo bien tu pregunta."]

    logger.debug(f"Final messages: {messages}")
    return messages, mentioned_project

def handle_audio_message(media_url, phone, twilio_account_sid, twilio_auth_token):
    """Handle audio messages by transcribing them."""
    logger.debug("Handling audio message")
    audio_response = requests.get(media_url, auth=(twilio_account_sid, twilio_auth_token))
    if audio_response.status_code != 200:
        logger.error(f"Failed to download audio: {audio_response.status_code}")
        return ["Lo siento, no pude procesar tu mensaje de audio. ¿Puedes enviarlo como texto?"], None

    # Save the audio file temporarily
    audio_file_path = f"/tmp/audio_{phone.replace(':', '_')}.ogg"
    with open(audio_file_path, 'wb') as f:
        f.write(audio_response.content)
    logger.debug(f"Audio saved to {audio_file_path}")

    # Transcribe the audio using Whisper
    try:
        with open(audio_file_path, 'rb') as audio_file:
            transcription = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="es"  # Assuming Spanish audio
            )
        incoming_msg = transcription.text.strip()
        logger.info(f"Audio transcribed: {incoming_msg}")
        return None, incoming_msg
    except Exception as e:
        logger.error(f"Error transcribing audio: {str(e)}\n{traceback.format_exc()}")
        return ["Lo siento, no pude entender tu mensaje de audio. ¿Puedes intentarlo de nuevo o escribirlo como texto?", f"Error details for debugging: {str(e)}"], None
    finally:
        if os.path.exists(audio_file_path):
            os.remove(audio_file_path)

import re
import logging
import requests
import openai
import bot_config
import traceback
import os

# Configure logger
logger = logging.getLogger(__name__)

# Global variables for project data
projects_data = {}
downloadable_urls = {}

def initialize_message_handler(openai_api_key, projects_data_ref, downloadable_urls_ref):
    global projects_data, downloadable_urls
    openai.api_key = openai_api_key
    projects_data = projects_data_ref
    downloadable_urls = downloadable_urls_ref
    logger.debug(f"Initialized with projects_data: {list(projects_data.keys())}")

def process_message(incoming_msg, phone, conversation_state, project_info, conversation_history):
    logger.debug(f"Processing message: {incoming_msg}")
    messages = []
    mentioned_project = conversation_state[phone].get('last_mentioned_project')

    # Detectar proyecto en el mensaje actual
    normalized_msg = incoming_msg.lower().replace(" ", "")
    for project in projects_data.keys():
        if project.lower() in normalized_msg:
            mentioned_project = project
            break

    # Si no hay proyecto en el mensaje, usar el último del historial
    if not mentioned_project:
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

    # Prepare the project data for the AI
    project_data = projects_data.get(mentioned_project, "Información no disponible para este proyecto.")

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
    logger.debug(f"ChatGPT prompt: {prompt}")

    try:
        response = openai.ChatCompletion.create(
            model=bot_config.CHATGPT_MODEL,
            messages=[
                {"role": "system", "content": bot_config.BOT_PERSONALITY},
                {"role": "user", "content": prompt}
            ],
            max_tokens=150,
            temperature=0.7
        )
        reply = response['choices'][0]['message']['content'].strip()
        logger.debug(f"Generated response: {reply}")
    except Exception as openai_e:
        logger.error(f"Fallo con OpenAI API: {str(openai_e)}")
        reply = "Lo siento, no entiendo bien tu pregunta."

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

    # Transcribe the audio using Whisper (compatible with openai==0.27.0)
    try:
        with open(audio_file_path, 'rb') as audio_file:
            transcription = openai.Audio.transcribe(
                model="whisper-1",
                file=audio_file,
                language="es"  # Assuming Spanish audio
            )
        incoming_msg = transcription['text'].strip()
        logger.info(f"Audio transcribed: {incoming_msg}")
        return None, incoming_msg
    except Exception as e:
        logger.error(f"Error transcribing audio: {str(e)}\n{traceback.format_exc()}")
        return ["Lo siento, no pude entender tu mensaje de audio. ¿Puedes intentarlo de nuevo o escribirlo como texto?"], None
    finally:
        if os.path.exists(audio_file_path):
            os.remove(audio_file_path)

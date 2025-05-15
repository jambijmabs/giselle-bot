import re
import logging
import requests
from datetime import datetime, timedelta
from openai import OpenAI
import bot_config

# Configure logger
logger = logging.getLogger(__name__)

# Initialize OpenAI client (will be passed from app.py)
openai_client = None

# Global dictionaries (will be passed from app.py)
projects_data = {}
downloadable_urls = {}

# Dictionary to store extracted project details (prices, payment plans, etc.)
project_details = {}

def initialize_message_handler(openai_api_key, projects_data_ref, downloadable_urls_ref):
    """Initialize the message handler with necessary dependencies."""
    global openai_client, projects_data, downloadable_urls
    openai_client = OpenAI(api_key=openai_api_key)
    projects_data = projects_data_ref
    downloadable_urls = downloadable_urls_ref
    extract_project_details()

def extract_project_details():
    """Extract specific details (prices, payment plans, discounts) from project info files."""
    global project_details
    project_details = {}

    for project, data in projects_data.items():
        project_details[project] = {
            'prices': [],
            'payment_plans': [],
            'discounts': {}
        }

        # Split the data into lines
        lines = data.split('\n')
        current_section = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Detect sections
            if line.startswith('Unidades Disponibles:'):
                current_section = 'prices'
                continue
            elif line.startswith('Planes de Pago:'):
                current_section = 'payment_plans'
                continue

            # Extract prices
            if current_section == 'prices' and line.startswith('-'):
                match = re.match(r'- (.*?): (\d+ recámaras), \$([\d,]+) USD', line)
                if match:
                    unit_name, bedrooms, price = match.groups()
                    price = price.replace(',', '')
                    project_details[project]['prices'].append({
                        'unit': unit_name,
                        'bedrooms': bedrooms,
                        'price': f"${price} USD"
                    })

            # Extract payment plans and discounts
            if current_section == 'payment_plans' and line.startswith('-'):
                match = re.match(r'- Opción (.*?): (.*?) \((\d+)% descuento\)', line)
                if match:
                    option, plan, discount = match.groups()
                    project_details[project]['payment_plans'].append({
                        'option': option,
                        'plan': plan
                    })
                    project_details[project]['discounts'][option] = f"{discount}%"

    logger.info(f"Extracted project details: {project_details}")

def process_message(incoming_msg, phone, conversation_state, project_info, conversation_history):
    """Process the incoming message and generate a response."""
    logger.debug("Starting process_message")
    messages = []  # Initialize messages to avoid None
    mentioned_project = None  # Initialize mentioned_project to avoid None

    try:
        # Check for specific requests (prices, payment plans, discounts)
        normalized_msg = incoming_msg.lower().replace(" ", "")
        logger.debug(f"Normalized message: {normalized_msg}")

        # Find the mentioned project
        for project in projects_data.keys():
            if project.lower() in normalized_msg:
                mentioned_project = project
                break
        if not mentioned_project:
            mentioned_project = list(projects_data.keys())[0] if projects_data else None
        logger.debug(f"Mentioned project: {mentioned_project}")

        client_name = conversation_state[phone].get('client_name', 'Cliente')
        logger.debug(f"Client name: {client_name}")

        # Check for price requests
        if any(keyword in normalized_msg for keyword in ["precio", "cost", "cuesta", "cuánto"]):
            logger.debug("Detected price request")
            if mentioned_project and mentioned_project in project_details:
                prices = project_details[mentioned_project]['prices']
                if prices:
                    messages.append(f"¡Hola {client_name}! Te comparto con gusto los precios de las unidades disponibles en {mentioned_project}:")
                    for price_info in prices:
                        messages.append(f"- {price_info['unit']}: {price_info['bedrooms']} por {price_info['price']}")
                    messages.append("¿Te interesa alguna unidad en particular o quieres que te cuente más sobre las opciones de pago?")
                    return messages, mentioned_project
                else:
                    messages.append(f"Lo siento, {client_name}, no tengo información de precios para {mentioned_project} en este momento.")
                    messages.append("¿Te gustaría que te envíe el documento con más detalles del proyecto?")
                    return messages, mentioned_project

        # Check for payment plans/forms requests
        if any(keyword in normalized_msg for keyword in ["plan de pago", "forma de pago", "pago"]):
            logger.debug("Detected payment plan request")
            if mentioned_project and mentioned_project in project_details:
                plans = project_details[mentioned_project]['payment_plans']
                if plans:
                    messages.append(f"¡Claro, {client_name}! En {mentioned_project} ofrecemos varias opciones de planes de pago para que elijas la que mejor se adapte a tus necesidades:")
                    for plan in plans:
                        messages.append(f"- Opción {plan['option']}: {plan['plan']}")
                    messages.append("Cada plan tiene beneficios distintos, ¿te gustaría saber más sobre los descuentos disponibles?")
                    return messages, mentioned_project
                else:
                    messages.append(f"Lo siento, {client_name}, no tengo información de planes de pago para {mentioned_project} en este momento.")
                    messages.append("¿Te gustaría que te envíe el documento con más detalles del proyecto?")
                    return messages, mentioned_project

        # Check for discounts requests
        if "descuento" in normalized_msg:
            logger.debug("Detected discount request")
            if mentioned_project and mentioned_project in project_details:
                discounts = project_details[mentioned_project]['discounts']
                if discounts:
                    messages.append(f"¡Por supuesto, {client_name}! En {mentioned_project} ofrecemos descuentos dependiendo del plan de pago que elijas:")
                    for option, discount in discounts.items():
                        messages.append(f"- Opción {option}: {discount} de descuento")
                    messages.append("¿Te gustaría revisar los planes de pago completos para que elijas el mejor para ti?")
                    return messages, mentioned_project
                else:
                    messages.append(f"Lo siento, {client_name}, no tengo información de descuentos para {mentioned_project} en este momento.")
                    messages.append("¿Te gustaría que te envíe el documento con más detalles del proyecto?")
                    return messages, mentioned_project

        # Check for file requests or location requests
        requested_file = None
        if mentioned_project:
            for file in downloadable_urls.get(mentioned_project, {}).keys():
                # Normalize file name for comparison
                normalized_file = file.replace(" ", "").lower()
                normalized_msg = incoming_msg.replace(" ", "").lower()
                # Handle variations in file names (e.g., "presentacion de venta", "presentacion de ventas")
                if "presentacion" in normalized_msg and "venta" in normalized_msg and "presentacion de venta" in normalized_file:
                    requested_file = file
                    break
                elif "lista de precios" in normalized_msg and "lista de precios" in normalized_file:
                    requested_file = file
                    break
                elif "especificaciones" in normalized_msg and "especificaciones" in normalized_file:
                    requested_file = file
                    break
                elif "mobiliario" in normalized_msg and "maquech" in normalized_msg and "mobiliario incluido en maquech" in normalized_file:
                    requested_file = file
                    break
                elif "mobiliario" in normalized_msg and "balam" in normalized_msg and "mobiliario incluido en balam" in normalized_file:
                    requested_file = file
                    break
                elif "mobiliario" in normalized_msg and "kai" in normalized_msg and "mobiliario incluido en kai" in normalized_file:
                    requested_file = file
                    break
                elif normalized_file in normalized_msg:
                    requested_file = file
                    break
            logger.debug(f"Requested file: {requested_file}")

        # Check if the message explicitly requests a file or location
        explicit_file_request = any(keyword in normalized_msg for keyword in [
            "mándame", "envíame", "pásame", "quiero el archivo", "presentación", "especificaciones", "entrega", "mobiliario", "pdf"
        ])
        location_request = "ubicación" in normalized_msg or "location" in normalized_msg or "google maps" in normalized_msg

        if explicit_file_request and requested_file and mentioned_project:
            logger.debug("Detected file request")
            file_urls = downloadable_urls.get(mentioned_project, {})
            logger.debug(f"File URLs for project {mentioned_project}: {file_urls}")
            file_url = file_urls.get(requested_file)
            if file_url:
                messages.append(f"¡Claro, {client_name}! Aquí tienes el enlace para que puedas revisar {requested_file} con todos los detalles:")
                messages.append(f"{file_url}")
                messages.append("Si tienes alguna pregunta sobre el documento o quieres saber más del proyecto, estaré encantada de ayudarte.")
            else:
                messages.append(bot_config.FILE_ERROR_MESSAGE.format(requested_file=requested_file))
            return messages, mentioned_project
        elif location_request:
            logger.debug("Detected location request")
            if mentioned_project:
                file_urls = downloadable_urls.get(mentioned_project, {})
                location_url = file_urls.get("ubicacion")
                if location_url:
                    messages.append(f"¡Claro, {client_name}! La ubicación de {mentioned_project} es un lugar muy especial, aquí tienes el enlace para que lo explores:")
                    messages.append(f"{location_url}")
                    messages.append("¿Te gustaría saber más sobre cómo llegar o sobre las características del entorno?")
                else:
                    messages.append(f"Lo siento, {client_name}, no tengo la URL de la ubicación para {mentioned_project}.")
                    messages.append("¿Te gustaría que te envíe más información del proyecto?")
            return messages, mentioned_project
        elif mentioned_project and bot_config.should_offer_files(conversation_state[phone], conversation_history, mentioned_project):
            logger.debug("Offering files")
            messages.append(bot_config.OFFER_FILES_MESSAGE.format(project=mentioned_project))
            return messages, mentioned_project

        # Otherwise, generate a response using OpenAI
        logger.debug("Generating response with OpenAI")
        if not conversation_state[phone].get('introduced', False):
            intro = bot_config.INITIAL_INTRO
            conversation_state[phone]['introduced'] = True
            conversation_state[phone]['name_asked'] = 1
        else:
            intro = ""

        # Ask for budget if needed
        if bot_config.should_ask_name(conversation_state[phone], conversation_history):
            conversation_state[phone]['name_asked'] += 1
        if bot_config.should_ask_budget(conversation_state[phone], conversation_history):
            intro += f" {bot_config.BUDGET_QUESTION}"
            conversation_state[phone]['budget_asked'] += 1
        if bot_config.should_ask_contact_time(conversation_state[phone], conversation_history):
            intro += f" {bot_config.CONTACT_TIME_QUESTION}"
            conversation_state[phone]['contact_time_asked'] += 1

        prompt = (
            f"{bot_config.BOT_PERSONALITY}\n\n"
            f"{intro}\n\n"
            f"**Instrucciones para las respuestas:**\n"
            f"{bot_config.RESPONSE_INSTRUCTIONS}\n\n"
            f"**Información de los proyectos disponibles:**\n"
            f"{project_info}\n\n"
            f"**Historial de conversación:**\n"
            f"{conversation_history}\n\n"
            f"**Mensaje del cliente:** \"{incoming_msg}\"\n\n"
            f"Responde de forma breve y profesional, enfocándote en la venta de propiedades. Improvisa de manera natural, utilizando únicamente la información de los proyectos y archivos descargables proporcionados."
        )
        logger.debug(f"ChatGPT prompt: {prompt}")

        try:
            logger.debug("Generating response with ChatGPT")
            response = openai_client.chat.completions.create(
                model=bot_config.CHATGPT_MODEL,
                messages=[
                    {"role": "system", "content": bot_config.BOT_PERSONALITY},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=100,
                temperature=0.7
            )
            reply = response.choices[0].message.content.strip()
            logger.debug(f"Generated response: {reply}")
        except Exception as openai_e:
            logger.error(f"Fallo con OpenAI API: {str(openai_e)}")
            if "rate_limit" in str(openai_e).lower() or "insufficient_quota" in str(openai_e).lower():
                reply = "Lo siento, estoy teniendo problemas para procesar tu mensaje debido a un límite en mi sistema."
            elif "authentication" in str(openai_e).lower():
                reply = "Parece que hay un problema con mi configuración."
            else:
                reply = "Lo siento, no entiendo bien tu pregunta."

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

    except Exception as e:
        logger.error(f"Unexpected error in process_message: {str(e)}")
        messages.append(f"Lo siento, {client_name}, ocurrió un error al procesar tu mensaje.")
        messages.append("¿En qué más puedo ayudarte?")
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
        logger.error(f"Error transcribing audio: {str(e)}")
        return ["Lo siento, no pude entender tu mensaje de audio. ¿Puedes intentarlo de nuevo o escribirlo como texto?"], None
    finally:
        if os.path.exists(audio_file_path):
            os.remove(audio_file_path)

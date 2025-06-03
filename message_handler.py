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

    # Handle based on intention using AI for more natural responses
    if intention == "greeting":
        # This should be handled in app.py, but we'll include a fallback
        messages = [f"Hola, soy Giselle de FAV Living, una desarrolladora inmobiliaria. Me encantaría conocerte, ¿cómo te llamas?"]
        conversation_state[phone]['name_asked'] = conversation_state[phone].get('name_asked', 0) + 1
    elif intention == "budget":
        budget = intention_data.get("budget", "No especificado")
        conversation_state[phone]['client_budget'] = budget
        if budget.lower() != "no especificado":
            messages = [f"Gracias por compartir tu presupuesto, {client_name}. Me gustaría saber un poco más sobre lo que buscas en un proyecto, ¿qué tienes en mente?"]
            conversation_state[phone]['needs_asked'] = True
        else:
            messages = [f"No te preocupes, {client_name}, podemos explorar opciones sin un presupuesto definido. ¿Qué tipo de proyecto te interesa? ¿Algo para invertir, vivir, o tal vez vacacionar?"]
            conversation_state[phone]['needs_asked'] = True
    elif intention == "needs":
        needs = intention_data.get("needs", "No especificadas")
        conversation_state[phone]['needs'] = needs
        # Instead of asking rigid follow-up questions, provide project info
        project_match = None
        for project, data in projects_data.items():
            description = data.get('description', '').lower()
            if needs.lower() != "no especificadas":
                if "departamentos" in needs.lower() and "condohotel" in description:
                    project_match = project
                    break
        if not project_match:
            project_match = mentioned_project if mentioned_project else list(projects_data.keys())[0]

        project_data_dict = projects_data.get(project_match, {})
        project_description = project_data_dict.get('description', "Información no disponible para este proyecto.")
        project_type = project_data_dict.get('type', 'No especificado')
        project_location = project_data_dict.get('location', 'No especificada')
        messages = [
            f"Entiendo, {client_name}. Basado en lo que buscas, te podría interesar un proyecto como {project_match}.",
            f"Está ubicado en {project_location}, es un {project_type} con detalles como: {project_description}",
            f"¿Te gustaría saber más sobre este proyecto o prefieres explorar otras opciones?"
        ]
        conversation_state[phone]['offered_project'] = project_match
        conversation_state[phone]['stage'] = "project_info"
    elif intention == "purchase_intent":
        purchase_intent = intention_data.get("purchase_intent", "No especificado")
        conversation_state[phone]['purchase_intent'] = purchase_intent
        messages = [f"Gracias por compartir tu plazo de compra, {client_name}. ¿Qué tipo de proyecto estás buscando? ¿Algo para invertir, para vivir, o tal vez para vacacionar?"]
        conversation_state[phone]['needs_asked'] = True
    elif intention == "contact_preference":
        days = intention_data.get("days", None)
        time = intention_data.get("time", None)
        if days:
            conversation_state[phone]['preferred_days'] = days
        if time:
            conversation_state[phone]['preferred_time'] = time
        messages = [f"Perfecto, {client_name}, me aseguraré de contactarte en el horario que prefieres. Mientras tanto, ¿te gustaría saber más sobre alguno de nuestros proyectos?"]
    elif intention == "no_interest":
        conversation_state[phone]['no_interest'] = True
        messages = bot_config.handle_no_interest_response()
    elif intention == "offer_response" or (conversation_state[phone].get('stage') == "project_info" and ("sí" in incoming_msg.lower() or "si" in incoming_msg.lower() or "interesa" in incoming_msg.lower())):
        if conversation_state[phone].get('stage') == "project_info":
            response = "yes"
        else:
            response = intention_data.get("response", "").lower()
        offered_project = conversation_state[phone].get('offered_project', mentioned_project)
        if "sí" in response or "si" in response or "interesa" in response:
            # Only offer after providing more info and ensuring interest
            project_data_dict = projects_data.get(offered_project, {})
            project_description = project_data_dict.get('description', "Información no disponible para este proyecto.")
            messages = [
                f"¡Qué bueno que te interesa, {client_name}! En {offered_project}, tenemos unidades disponibles que podrían ser perfectas para ti.",
                f"Por ejemplo, una unidad de 2 recámaras tiene un precio inicial de $375,000 USD, con un enganche del 20% y pagos a 12 meses.",
                f"¿Te gustaría que te prepare una oferta personalizada o prefieres saber más detalles primero?"
            ]
            conversation_state[phone]['stage'] = "offer_made"
        else:
            # Move to negotiation stage
            messages = [
                f"Entiendo, {client_name}. {offered_project} es una gran opción por su alta plusvalía y ubicación estratégica.",
                f"Si tienes alguna duda o prefieres explorar otro proyecto, estoy aquí para ayudarte. ¿Qué te gustaría hacer?"
            ]
            conversation_state[phone]['stage'] = "negotiation"
    elif intention == "negotiation":
        offered_project = conversation_state[phone].get('offered_project', mentioned_project)
        if "zoom" in incoming_msg.lower() or "sí" in incoming_msg.lower() or "si" in incoming_msg.lower():
            messages = [f"Perfecto, {client_name}. Agendaré un Zoom con el gerente para que podamos resolver todas tus dudas. ¿En qué horario te vendría bien?"]
            conversation_state[phone]['stage'] = "scheduling_zoom"
        elif "no" in incoming_msg.lower():
            # Offer an alternative project
            alternative_project = None
            for project in projects_data.keys():
                if project != offered_project:
                    alternative_project = project
                    break
            if alternative_project:
                conversation_state[phone]['offered_project'] = alternative_project
                project_data_dict = projects_data.get(alternative_project, {})
                project_description = project_data_dict.get('description', "Información no disponible para este proyecto.")
                project_type = project_data_dict.get('type', 'No especificado')
                project_location = project_data_dict.get('location', 'No especificada')
                messages = [
                    f"Entiendo, {client_name}. Si {offered_project} no es lo que buscas, te podría interesar {alternative_project}.",
                    f"Está ubicado en {project_location}, es un {project_type} con detalles como: {project_description}",
                    f"¿Te gustaría saber más sobre este proyecto?"
                ]
                conversation_state[phone]['stage'] = "project_info"
            else:
                messages = [
                    f"Entiendo, {client_name}. Tómate tu tiempo para pensar en {offered_project}.",
                    f"Si cambias de idea o quieres explorar otras opciones, aquí estoy para ayudarte. ¿Qué te gustaría hacer?"
                ]
        else:
            # Continue negotiation with AI-generated response
            prompt = (
                f"Eres Giselle, una asesora de ventas de FAV Living. "
                f"El cliente tiene dudas o no aceptó la oferta inicial para el proyecto {offered_project}. "
                f"Datos del proyecto: {project_data}\n"
                f"Tu tarea es negociar destacando atributos financieros (retorno de inversión, plusvalía) y del proyecto (ubicación, amenidades). "
                f"Responde de forma breve, natural y profesional, enfocándote en mantener el interés del cliente sin presionarlo.\n\n"
                f"Historial de conversación:\n{conversation_history}\n\n"
                f"Mensaje del cliente: {incoming_msg}"
            )
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
                logger.debug(f"Generated negotiation response from OpenAI: {reply}")

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
                    messages = [f"Entiendo, {client_name}. ¿Te gustaría que agendemos un Zoom con el gerente para resolver tus dudas?"]
            except Exception as openai_e:
                logger.error(f"Fallo con OpenAI API en negociación: {str(openai_e)}", exc_info=True)
                messages = [f"Entiendo, {client_name}. ¿Te gustaría que agendemos un Zoom con el gerente para resolver tus dudas?"]
    elif intention == "confirm_sale":
        if "sí" in incoming_msg.lower() or "si" in incoming_msg.lower() or "confirmo" in incoming_msg.lower():
            messages = [
                f"¡Felicidades por tu decisión, {client_name}! La unidad 2B de MUWAN está apartada para ti.",
                "Te enviaré los datos bancarios para el depósito de $10,000 USD. Confirmaré la recepción del depósito para seguir con el proceso."
            ]
            conversation_state[phone]['stage'] = "sale_closed"
        else:
            messages = [f"Entiendo, {client_name}. Tómate tu tiempo para decidir. Si necesitas más información o ajustar algo, aquí estoy para ayudarte."]
    elif intention == "confirm_deposit":
        if "ya envié" in incoming_msg.lower() or "depositado" in incoming_msg.lower():
            messages = [
                f"¡Gracias por tu compra, {client_name}! Confirmaré la recepción del depósito y seguiremos con el proceso.",
                "Estaremos en contacto para los próximos pasos."
            ]
            conversation_state[phone]['stage'] = "deposit_confirmed"
        else:
            messages = [f"Entendido, {client_name}. Cuando hagas el depósito, por favor avísame para confirmar. ¿Tienes alguna duda que pueda ayudarte a resolver?"]
    elif intention == "external_question":
        question = incoming_msg
        prompt = (
            f"Eres Giselle, una asesora de ventas de FAV Living. "
            f"El cliente ha hecho una pregunta externa al proyecto {mentioned_project}, pero que puede ayudar a cerrar la venta: '{question}'. "
            f"Datos del proyecto: {project_data}\n"
            f"Tu tarea es razonar una respuesta positiva que apoye la venta, basándote en el contexto del proyecto y datos generales, sin mentir ni inventar información específica del proyecto. "
            f"Por ejemplo, si preguntan 'a cuánto está un supermercado cerca de Calidris?', puedes razonar que Calidris está en una zona bien ubicada y que probablemente haya supermercados a 5-10 minutos, ya que es común en zonas residenciales. "
            f"Si preguntan 'cómo está el mercado de rentas en Pesquería?', puedes razonar que Pesquería es una zona en crecimiento con alta demanda, lo que hace que las rentas sean una buena inversión. "
            f"Si preguntan 'cómo está la ocupación en Holbox?', puedes razonar que Holbox es un destino turístico popular con alta ocupación, especialmente en temporada alta, lo que beneficia a proyectos como condohoteles. "
            f"Responde de forma breve, natural y profesional, enfocándote en apoyar la venta.\n\n"
            f"Mensaje del cliente: {incoming_msg}"
        )

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
            logger.debug(f"Generated external question response from OpenAI: {reply}")

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
        except Exception as openai_e:
            logger.error(f"Fallo con OpenAI API al responder pregunta externa: {str(openai_e)}", exc_info=True)
            messages = [f"No tengo esa información a la mano, {client_name}, pero puedo revisarlo con el gerente si te parece."]
    else:
        # Default to a conversational response using AI
        prompt = (
            f"{bot_config.BOT_PERSONALITY}\n\n"
            f"Instrucciones para las respuestas:\n"
            f"Actúa como una asesora de ventas profesional y amigable de FAV Living. Tu objetivo principal es informar al cliente sobre los proyectos, generar interés y resolver dudas de manera natural, sin apresurarte a hacer una oferta a menos que el cliente muestre un interés claro en avanzar con una compra. "
            f"Prioriza entregar información detallada y útil sobre los proyectos, invítalo a explorar más detalles o a aclarar dudas antes de sugerir una oferta. "
            f"Evita hacer preguntas rígidas o seguir un flujo predeterminado; en lugar de eso, adapta tus respuestas al contexto del mensaje del cliente para que la conversación sea fluida y amigable. "
            f"Usa un tono cálido y profesional, y siempre dirígete al cliente por su nombre cuando sea posible.\n\n"
            f"Información de los proyectos disponibles:\n"
            f"{project_info}\n\n"
            f"Datos específicos del proyecto {mentioned_project}:\n"
            f"{project_data}\n\n"
            f"Historial de conversación:\n"
            f"{conversation_history}\n\n"
            f"Mensaje del cliente: {incoming_msg}\n\n"
            f"Responde de forma breve, natural y profesional, enfocándote en generar interés en los proyectos. "
            f"Si el cliente pregunta por algo que no está en los datos del proyecto y es inherente al proyecto (como amenidades específicas), responde con una frase como "
            f"'No tengo esa información a la mano, pero puedo revisarlo con el gerente, ¿te parece?'"
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

            if "no tengo esa información" in reply.lower() or "puedo revisarlo con el gerente" in reply.lower():
                logger.info(f"Bot cannot answer: {incoming_msg}. Contacting gerente.")
                messages.append(f"No tengo esa información a la mano, {client_name}, pero puedo revisarlo con el gerente, ¿te parece?")
                
                project_context = f"sobre {mentioned_project}" if mentioned_project else "general"
                gerente_message = f"Pregunta de {client_name} {project_context}: {incoming_msg}"
                logger.debug(f"Preparing to send message to gerente: {gerente_message}")
                logger.debug(f"Sending to gerente_phone: {gerente_phone} from {whatsapp_sender_number}")

                try:
                    if twilio_client is None:
                        raise Exception("Twilio client not initialized.")

                    logger.debug(f"Checking WhatsApp window for {gerente_phone}")
                    window_active = check_whatsapp_window(gerente_phone)
                    logger.debug(f"WhatsApp window active: {window_active}")

                    message = twilio_client.messages.create(
                        from_=whatsapp_sender_number,
                        body=gerente_message,
                        to=gerente_phone
                    )
                    logger.info(f"Sent message to gerente: SID {message.sid}, Estado: {message.status}")

                    updated_message = twilio_client.messages(message.sid).fetch()
                    logger.info(f"Estado del mensaje actualizado: {updated_message.status}")
                    if updated_message.status == "failed":
                        logger.error(f"Error al enviar mensaje al gerente: {updated_message.error_code} - {updated_message.error_message}")
                        messages = [f"Lo siento, {client_name}, hubo un problema al contactar al gerente. ¿En qué más puedo ayudarte?"]
                except Exception as twilio_e:
                    logger.error(f"Error sending message to gerente via Twilio: {str(twilio_e)}", exc_info=True)
                    messages = [f"Lo siento, {client_name}, hubo un problema al contactar al gerente. ¿En qué más puedo ayudarte?"]
            else:
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

    logger.debug(f"Final messages: {messages}")
    return messages, mentioned_project  # Ensure we always return a tuple

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

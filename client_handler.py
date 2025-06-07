import logging
import time
from datetime import datetime, timedelta
import pytz
import bot_config
import utils

logger = logging.getLogger(__name__)

CST_TIMEZONE = pytz.timezone("America/Mexico_City")

def rephrase_gerente_response(answer, client_name, question, message_handler):
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
        logger.error(f"Error rephrasing gerente response with OpenAI: {str(e)}", exc_info=True)
        return f"Gracias por esperar, {client_name}. Sobre tu pregunta: {answer}"

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

def handle_client_message(phone, incoming_msg, num_media, media_url, profile_name, conversation_state, client, message_handler, utils, recontact_handler):
    logger.info(f"Handling message from client ({phone})")

    try:
        # Step 1: Load conversation history
        logger.debug(f"Loading conversation history for {phone}")
        history = utils.load_conversation_history(phone, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
        if not isinstance(history, list):
            logger.warning(f"Conversation history for {phone} is not a list: {history}")
            history = []

        # Step 2: Update conversation state
        logger.debug(f"Updating conversation state for {phone}")
        state = conversation_state[phone]
        state['history'] = history
        state['last_contact'] = datetime.now(CST_TIMEZONE).isoformat()
        state['last_response_time'] = datetime.now(CST_TIMEZONE).isoformat()

        # Step 3: Set client name from ProfileName if available (fallback)
        if profile_name and not state.get('client_name'):
            name_parts = profile_name.strip().split()
            if name_parts:
                state['client_name'] = name_parts[0].capitalize()
                logger.info(f"Client name set from ProfileName: {state['client_name']}")
            else:
                state['client_name'] = "Cliente"

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
                    bot_config.WHATSAPP_SENDER_NUMBER
                )

        if state.get('priority', False):
            for gerente_phone in [p for p, s in conversation_state.items() if s.get('is_gerente', False)]:
                utils.send_consecutive_messages(
                    gerente_phone,
                    [f"Cliente prioritario {phone} ha enviado un mensaje: {incoming_msg}"],
                    client,
                    bot_config.WHATSAPP_SENDER_NUMBER
                )

        # Step 6: Handle pending responses from gerente
        logger.debug(f"Checking for pending responses for {phone}")
        if state.get('pending_response_time'):
            current_time = time.time()
            elapsed_time = current_time - state['pending_response_time']
            if elapsed_time >= bot_config.FAQ_RESPONSE_DELAY:
                question = state.get('pending_question', {}).get('question')
                mentioned_project = state.get('pending_question', {}).get('mentioned_project')
                if question:
                    logger.debug(f"Fetching FAQ answer for question '{question}' about project '{mentioned_project}'")
                    answer = utils.get_faq_answer(question, mentioned_project)
                    if answer:
                        client_name = state.get('client_name', 'Cliente') or 'Cliente'
                        rephrased_answer = rephrase_gerente_response(answer, client_name, question, message_handler)
                        messages = [rephrased_answer]
                        utils.send_consecutive_messages(phone, messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
                        state['history'].append(f"Giselle: {messages[0]}")
                        state['pending_question'] = None
                        state['pending_response_time'] = None
                        logger.debug(f"Sent gerente response to client {phone}: {messages}")
                    else:
                        logger.error(f"Could not find answer for question '{question}' in FAQ.")
                        messages = ["Lo siento, no pude encontrar una respuesta. ¿En qué más puedo ayudarte?"]
                        utils.send_consecutive_messages(phone, messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
                        state['history'].append(f"Giselle: {messages[0]}")
                        state['pending_question'] = None
                        state['pending_response_time'] = None
                else:
                    logger.error(f"No pending question found for {phone} despite pending_response_time.")
                    state['pending_response_time'] = None
            else:
                logger.debug(f"Waiting for FAQ response delay to complete for {phone}. Elapsed time: {elapsed_time} seconds")
                utils.save_conversation(phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
                return "Waiting for gerente response", 200

        # Step 7: Prepare project information
        logger.debug("Preparing project information")
        project_info = ""
        try:
            if not hasattr(utils, 'projects_data'):
                logger.error("utils.projects_data is not defined")
                raise AttributeError("utils.projects_data is not defined")
            for project, data in utils.projects_data.items():
                project_info += f"Proyecto: {project}\n"
                project_info += f"Descripción: {data.get('description', 'No disponible')}\n"
                project_info += f"Tipo: {data.get('type', 'No especificado')}\n"
                project_info += f"Ubicación: {data.get('location', 'No especificada')}\n"
                if 'prices' in data:
                    project_info += "Precios: " + ", ".join([f"{k} ${v:,} MXN" for k, v in data['prices'].items()]) + "\n"
                if 'amenities' in data:
                    project_info += f"Amenidades: {', '.join(data['amenities'])}\n"
                project_info += "\n"
        except Exception as project_info_e:
            logger.error(f"Error preparing project information: {str(project_info_e)}", exc_info=True)
            project_info = "Información de proyectos no disponible."

        # Step 8: Process the message
        logger.debug("Building conversation history")
        conversation_history = "\n".join(state['history'])

        logger.debug(f"Checking FAQ for an existing answer")
        mentioned_project = state.get('last_mentioned_project')
        faq_answer = utils.get_faq_answer(incoming_msg, mentioned_project)
        if faq_answer:
            client_name = state.get('client_name', 'Cliente') or 'Cliente'
            rephrased_answer = rephrase_gerente_response(faq_answer, client_name, incoming_msg, message_handler)
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
                        bot_config.WHATSAPP_SENDER_NUMBER
                    )
            else:
                logger.debug(f"No gerente contact needed for message: {incoming_msg}")

        if mentioned_project:
            state['last_mentioned_project'] = mentioned_project
            logger.debug(f"Updated last_mentioned_project to: {mentioned_project}")

        # Step 9: Send a 24-hour window reminder
        last_incoming = datetime.fromisoformat(state['last_incoming_time']).astimezone(CST_TIMEZONE)
        time_since_last_incoming = (datetime.now(CST_TIMEZONE) - last_incoming).total_seconds() / 3600
        if 20 <= time_since_last_incoming < 24 and not state.get('reminder_sent', False):
            reminder = [
                f"Hola {state.get('client_name', 'Cliente')}, ha pasado un tiempo desde nuestro último mensaje.",
                "¿Tienes alguna pregunta o quieres más detalles?"
            ]
            utils.send_consecutive_messages(phone, reminder, client, bot_config.WHATSAPP_SENDER_NUMBER)
            state['history'].extend([f"Giselle: {msg}" for msg in reminder])
            state['reminder_sent'] = True

        # Step 10: Send the generated messages
        utils.send_consecutive_messages(phone, messages, client, bot_config.WHATSAPP_SENDER_NUMBER)

        for msg in messages:
            state['history'].append(f"Giselle: {msg}")
        state['history'] = state['history'][-10:]

        # Step 11: Reset recontact schedule if the client responds
        state['schedule_next'] = None
        state['recontact_attempts'] = 0
        state['reminder_sent'] = False

        # Step 12: Save conversation state
        utils.save_conversation(phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)

        logger.debug("Returning success response")
        return "Mensaje enviado", 200

    except Exception as e:
        logger.error(f"Error in handle_client_message for {phone}: {str(e)}", exc_info=True)
        raise

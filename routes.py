from flask import request
from twilio.rest import Client
import bot_config
import utils
import message_handler
import gerente_handler
import client_handler
import report_handler
import recontact_handler
import pytz
from datetime import datetime
import re

def init_routes(app, conversation_state):
    """Initialize Flask routes for the application.

    Args:
        app (Flask): The Flask application instance.
        conversation_state (dict): The global conversation state dictionary.
    """
    client = None
    try:
        if not os.getenv('TWILIO_ACCOUNT_SID') or not os.getenv('TWILIO_AUTH_TOKEN'):
            logger.warning("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set in environment variables. Twilio client will not be initialized.")
        else:
            client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
            logger.info("Twilio client initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize Twilio client: {str(e)}", exc_info=True)
        client = None

    @app.route('/whatsapp', methods=['POST'])
    def whatsapp():
        """Handle incoming WhatsApp messages.

        Returns:
            tuple: A tuple of (message, status_code) indicating the result of the operation.
        """
        messages = []  # Inicializar messages para evitar UnboundLocalError
        try:
            if client is None:
                logger.error("Twilio client not initialized. Cannot process WhatsApp messages.")
                return "Error: Twilio client not initialized", 500

            logger.debug("Cargando conversation state")
            utils.load_conversation_state(conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
            logger.debug("Conversation state reloaded")

            logger.debug(f"Request headers: {dict(request.headers)}")
            logger.debug(f"Request form data: {request.form}")
            logger.debug(f"Request values: {dict(request.values)}")

            logger.debug("Extracting message content")
            phone = request.values.get('From', '').strip()
            if not phone or not re.match(r'whatsapp:\+\d+', phone):
                logger.error(f"Formato de número de teléfono inválido o no encontrado: {phone}")
                return "Error: Formato de número de teléfono inválido", 400

            incoming_msg = request.values.get('Body', '').strip()
            num_media = int(request.values.get('NumMedia', '0'))
            media_url = request.values.get('MediaUrl0', None) if num_media > 0 else None
            profile_name = request.values.get('ProfileName', None)

            logger.debug(f"From phone: {phone}, Message: {incoming_msg}, NumMedia: {num_media}, MediaUrl: {media_url}, ProfileName: {profile_name}")

            normalized_phone = phone.replace("whatsapp:", "").strip()
            is_gerente = normalized_phone in bot_config.GERENTE_NUMBERS
            logger.debug(f"Comparando número: phone='{phone}', normalized_phone='{normalized_phone}', GERENTE_NUMBERS={bot_config.GERENTE_NUMBERS}, is_gerente={is_gerente}")

            if is_gerente:
                logger.info(f"Identificado como gerente: {phone}")
                if phone not in conversation_state:
                    conversation_state[phone] = {
                        'history': [],
                        'is_gerente': True,
                        'last_contact': datetime.now(pytz.timezone("America/Mexico_City")).isoformat(),
                        'last_incoming_time': datetime.now(pytz.timezone("America/Mexico_City")).isoformat(),
                        'tasks': [],
                        'last_weekly_report': None,
                        'awaiting_menu_choice': False
                    }
                else:
                    conversation_state[phone]['is_gerente'] = True

                if incoming_msg:
                    return gerente_handler.handle_gerente_message(
                        phone, incoming_msg, conversation_state, client,
                        client_handler.rephrase_gerente_response, report_handler.generate_detailed_report,
                        report_handler.update_leads_excel, utils
                    )
                elif num_media > 0 and media_url:
                    error_messages, transcribed_msg = message_handler.handle_audio_message(
                        media_url, phone, os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN')
                    )
                    if error_messages:
                        utils.send_consecutive_messages(phone, error_messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
                        return "Error procesando audio", 200
                    if transcribed_msg:
                        return gerente_handler.handle_gerente_message(
                            phone, transcribed_msg, conversation_state, client,
                            client_handler.rephrase_gerente_response, report_handler.generate_detailed_report,
                            report_handler.update_leads_excel, utils
                        )
                else:
                    logger.error("Mensaje del gerente sin contenido de texto o audio")
                    return "Error: Mensaje sin contenido", 400

            else:
                logger.info(f"Identificado como cliente: {phone}")
                # Load conversation history to check if it exists
                logger.debug("Cargando historial de conversación")
                history = utils.load_conversation_history(phone, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
                logger.debug(f"Historial cargado: {history}")
                if not isinstance(history, list):
                    history = []

                # Initialize or reset client state if there's no history or profile is incomplete
                if phone not in conversation_state or not history or not is_profile_complete(conversation_state.get(phone, {})):
                    logger.info(f"Initializing or resetting state for client {phone} due to no history or incomplete profile")
                    conversation_state[phone] = {
                        'history': history,
                        'name_asked': 0,
                        'messages_without_response': 0,
                        'preferred_time': None,
                        'preferred_days': None,
                        'client_name': None,
                        'client_budget': None,
                        'last_contact': datetime.now(pytz.timezone("America/Mexico_City")).isoformat(),
                        'recontact_attempts': 0,
                        'no_interest': False,
                        'schedule_next': None,
                        'last_incoming_time': datetime.now(pytz.timezone("America/Mexico_City")).isoformat(),
                        'last_response_time': datetime.now(pytz.timezone("America/Mexico_City")).isoformat(),
                        'first_contact': datetime.now(pytz.timezone("America/Mexico_City")).isoformat(),
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
                        'intention_history': [],
                        'needs_asked': False,
                        'budget_asked': False,
                        'contact_time_asked': False,
                        'purchase_intent_asked': False,
                        'needs': None,
                        'purchase_intent': None
                    }

                state = conversation_state[phone]
                logger.debug(f"Estado del cliente: {state}")

                # Guardar el mensaje del cliente en el historial inmediatamente
                if incoming_msg:
                    logger.debug(f"Guardando mensaje del cliente: {incoming_msg}")
                    state['history'].append(f"Cliente: {incoming_msg}")
                    state['history'] = state['history'][-10]  # Mantener solo los últimos 10 mensajes
                    state['last_incoming_time'] = datetime.now(pytz.timezone("America/Mexico_City")).isoformat()
                    logger.debug("Antes de guardar conversación")
                    utils.save_conversation(phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
                    logger.debug("Conversación guardada")

                # Forzar pregunta del nombre como primer paso si no hay historial o nombre confirmado
                if not state.get('client_name') or state.get('client_name') in ['Cliente', '\u200efav'] or not state.get('name_asked'):
                    if not state.get('name_asked'):
                        state['name_asked'] = 0
                    if state.get('name_asked', 0) < 2:
                        state['name_asked'] += 1
                        # Intentar extraer el nombre inmediatamente si hay mensaje
                        if incoming_msg:
                            name = message_handler.extract_name(incoming_msg, "\n".join(state['history']))
                            logger.debug(f"Extracted name: {name}")
                            if name:
                                state['client_name'] = name
                                logger.info(f"Client name updated to: {state['client_name']}")
                                utils.save_conversation(phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
                                messages = [f"¡Gracias, {state['client_name']}! ¿Estás buscando algo para inversión, para vivir, o tal vez un lugar para vacacionar?"]
                            else:
                                messages = ["¡Hola! Soy Giselle de FAV Living. ¿Me podrías decir tu nombre para conocerte mejor?"]
                        else:
                            messages = ["¡Hola! Soy Giselle de FAV Living. ¿Me podrías decir tu nombre para conocerte mejor?"]
                        utils.send_consecutive_messages(phone, messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
                        state['history'].append(f"Giselle: {messages[0]}")
                        utils.save_conversation(phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
                        return "Mensaje enviado", 200
                    else:
                        state['client_name'] = "Cliente"
                        messages = ["Gracias por tu interés. Como no me diste un nombre, te llamaré 'Cliente' por ahora. ¿Estás buscando algo para inversión, para vivir, o tal vez un lugar para vacacionar?"]
                        utils.send_consecutive_messages(phone, messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
                        state['history'].append(f"Giselle: {messages[0]}")
                        utils.save_conversation(phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
                        return "Mensaje enviado", 200

                # Force profiling questions if not yet asked
                if state.get('client_name') and not state.get('needs_asked', False):
                    state['needs_asked'] = True
                    messages = [f"¡Hola {state['client_name']}! Me encantaría ayudarte a encontrar el proyecto perfecto. ¿Estás buscando algo para inversión, para vivir, o tal vez un lugar para vacacionar?"]
                    utils.send_consecutive_messages(phone, messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
                    state['history'].append(f"Giselle: {messages[0]}")
                    logger.debug("Guardando conversación después de preguntar por necesidades")
                    utils.save_conversation(phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
                    return "Mensaje enviado", 200

                if state.get('needs_asked') and not state.get('budget_asked', False):
                    state['budget_asked'] = True
                    messages = [f"Entendido, {state['client_name']}. ¿Cuál sería tu presupuesto aproximado para este proyecto?"]
                    utils.send_consecutive_messages(phone, messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
                    state['history'].append(f"Giselle: {messages[0]}")
                    logger.debug("Guardando conversación después de preguntar por presupuesto")
                    utils.save_conversation(phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
                    return "Mensaje enviado", 200

                if state.get('budget_asked') and not state.get('contact_time_asked', False):
                    state['contact_time_asked'] = True
                    messages = [f"Gracias por compartir eso, {state['client_name']}. ¿En qué horario te vendría mejor que charlemos más a fondo?"]
                    utils.send_consecutive_messages(phone, messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
                    state['history'].append(f"Giselle: {messages[0]}")
                    logger.debug("Guardando conversación después de preguntar por horario")
                    utils.save_conversation(phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
                    return "Mensaje enviado", 200

                if state.get('contact_time_asked') and not state.get('purchase_intent_asked', False):
                    state['purchase_intent_asked'] = True
                    messages = [f"Perfecto, {state['client_name']}. Una última pregunta para entenderte mejor: ¿qué tan pronto te gustaría avanzar con este proyecto?"]
                    utils.send_consecutive_messages(phone, messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
                    state['history'].append(f"Giselle: {messages[0]}")
                    logger.debug("Guardando conversación después de preguntar por intención de compra")
                    utils.save_conversation(phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
                    return "Mensaje enviado", 200

                if incoming_msg:
                    return client_handler.handle_client_message(
                        phone, incoming_msg, num_media, media_url, profile_name, conversation_state,
                        client, message_handler, utils, recontact_handler
                    )
                elif num_media > 0 and media_url:
                    error_messages, transcribed_msg = message_handler.handle_audio_message(
                        media_url, phone, os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN')
                    )
                    if error_messages:
                        utils.send_consecutive_messages(phone, error_messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
                        return "Error procesando audio", 200
                    if transcribed_msg:
                        return client_handler.handle_client_message(
                            phone, transcribed_msg, num_media=0, media_url=None, profile_name=profile_name,
                            conversation_state=conversation_state, client=client, message_handler=message_handler,
                            utils=utils, recontact_handler=recontact_handler
                        )
                else:
                    logger.error("Mensaje del cliente sin contenido de texto o audio")
                    return "Error: Mensaje sin contenido", 400

            return "Mensaje procesado", 200

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
                messages = ["Lo siento, ocurrió un error. Por favor, intenta de nuevo o dime cómo puedo ayudarte."]
                utils.send_consecutive_messages(phone, messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
                logger.info(f"Fallback message sent: SID {messages[0].sid if hasattr(messages[0], 'sid') else 'N/A'}, Estado: sent")
                if not conversation_state.get(phone, {}).get('is_gerente', False):
                    conversation_state[phone]['history'].append(f"Giselle: {messages[0]}")
                    utils.save_conversation(phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
            except Exception as twilio_e:
                logger.error(f"Error sending fallback message: {str(twilio_e)}")
            return "Error interno del servidor", 500

    def is_profile_complete(state):
        """Check if the client's profile is complete.

        Args:
            state (dict): The client state dictionary.

        Returns:
            bool: True if the profile is complete, False otherwise.
        """
        required_fields = [
            state.get('client_name'),
            state.get('needs'),
            state.get('client_budget'),
            state.get('preferred_time') or state.get('preferred_days'),
            state.get('purchase_intent')
        ]
        return all(field and field != "No especificado" for field in required_fields)

@app.route('/reset_state', methods=['GET'])
def reset_state():
    """Reset the conversation state for all clients.

    Returns:
        tuple: A tuple of (message, status_code) indicating the result.
    """
    logger.info("Resetting conversation state for all clients")
    conversation_state.clear()
    utils.load_conversation_state(conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
    return "Conversation state reset successfully", 200

@app.route('/', methods=['GET'])
def root():
    """Handle root GET request.

    Returns:
        str: A simple status message.
    """
    logger.debug("Solicitud GET recibida en /")
    return "Servidor Flask está funcionando!"

@app.route('/test', methods=['GET'])
def test():
    """Handle test GET request.

    Returns:
        str: A simple status message.
    """
    logger.debug("Solicitud GET recibida en /test")
    return "Servidor Flask está funcionando correctamente!"

@app.route('/schedule_recontact', methods=['GET'])
def trigger_recontact():
    """Trigger recontact scheduling.

    Returns:
        str: The result of the recontact operation.
    """
    return recontact_handler.trigger_recontact(conversation_state, client, utils, report_handler.generate_detailed_report)

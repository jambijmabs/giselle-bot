import re
import logging
import os
import bot_config
import utils
from google.cloud import storage
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

def show_gerente_menu(phone, client, conversation_state):
    menu = [
        "Hola, ¿cómo puedo ayudarte hoy? Por favor, selecciona una opción:",
        "1️⃣ Ver reporte de clientes interesados (puedes filtrar por etapa o interés)",
        "2️⃣ Ver nombres de clientes interesados",
        "3️⃣ Marcar un cliente como prioritario",
        "4️⃣ Ver resumen del día",
        "5️⃣ Ver resumen semanal",
        "6️⃣ Asignar una tarea (por ejemplo, 'Llamar a [teléfono] mañana')",
        "7️⃣ Buscar información de un cliente",
        "8️⃣ Añadir una FAQ",
        "Escribe el número de la opción o usa el comando directamente. 😊"
    ]
    utils.send_consecutive_messages(phone, menu, client, bot_config.WHATSAPP_SENDER_NUMBER)
    conversation_state[phone]['awaiting_menu_choice'] = True

def notify_gerente_of_pending_questions(phone, conversation_state, client):
    """Notify the gerente of any pending questions."""
    pending_questions = []
    for client_phone, state in conversation_state.items():
        if not state.get('is_gerente', False) and state.get('pending_question'):
            pending_questions.append({
                'client_phone': client_phone,
                'question': state['pending_question']['question']
            })

    if pending_questions:
        for question in pending_questions:
            messages = [msg.format(client_phone=question['client_phone'], question=question['question']) for msg in bot_config.GERENTE_REMINDER_MESSAGE]
            utils.send_consecutive_messages(phone, messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
            logger.info(f"Notified gerente {phone} of pending question from {question['client_phone']}")

def handle_gerente_message(phone, incoming_msg, conversation_state, client, rephrase_gerente_response, generate_detailed_report, update_leads_excel, utils):
    logger.info(f"Handling message from gerente ({phone})")

    incoming_msg_lower = incoming_msg.lower()

    # Notify gerente of pending questions on every interaction
    notify_gerente_of_pending_questions(phone, conversation_state, client)

    if conversation_state[phone].get('awaiting_menu_choice', False):
        if incoming_msg in ["1", "2", "3", "4", "5", "6", "7", "8"]:
            menu_commands = {
                "1": "reporte",
                "2": "nombres",
                "3": "marca prioritario",
                "4": "resumen del día",
                "5": "resumen semanal",
                "6": "llamar a mañana",
                "7": "busca a",
                "8": "añade faq"
            }
            incoming_msg_lower = menu_commands[incoming_msg]
            conversation_state[phone]['awaiting_menu_choice'] = False
        else:
            utils.send_consecutive_messages(
                phone,
                ["Por favor, selecciona una opción válida del menú (1-8)."],
                client,
                bot_config.WHATSAPP_SENDER_NUMBER
            )
            show_gerente_menu(phone, client, conversation_state)
            return "Opción inválida", 200

    if "menú" in incoming_msg_lower or "opciones" in incoming_msg_lower:
        show_gerente_menu(phone, client, conversation_state)
        return "Menú enviado", 200

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
                ["Tu respuesta parece poco clara. ¿Podrías dar más detalles?"],
                client,
                bot_config.WHATSAPP_SENDER_NUMBER
            )
            show_gerente_menu(phone, client, conversation_state)
            return "Respuesta poco clara", 200

        client_name = conversation_state[client_phone].get('client_name', 'Cliente') or 'Cliente'
        rephrased_answer = rephrase_gerente_response(answer, client_name, question)
        gerente_messages = [rephrased_answer]
        utils.send_consecutive_messages(client_phone, gerente_messages, client, bot_config.WHATSAPP_SENDER_NUMBER)

        conversation_state[client_phone]['history'].append(f"Giselle: {gerente_messages[0]}")
        conversation_state[client_phone]['pending_question'] = None
        conversation_state[client_phone]['pending_response_time'] = None
        logger.debug(f"Updated client {client_phone} history: {conversation_state[client_phone]['history']}")

        faq_entry = f"Pregunta: {question}\nRespuesta: {answer}\n"
        project_folder = mentioned_project.upper() if mentioned_project else "GENERAL"
        faq_file_name = f"{mentioned_project.lower()}_faq.txt" if mentioned_project else "general_faq.txt"
        faq_file_path = os.path.join(bot_config.GCS_BASE_PATH, project_folder, faq_file_name)
        logger.debug(f"Attempting to save FAQ entry to {faq_file_path}: {faq_entry}")

        try:
            temp_faq_path = f"/tmp/{faq_file_name}"
            try:
                storage_client = storage.Client()
                bucket = storage_client.bucket(bot_config.GCS_BUCKET_NAME)
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

        utils.save_conversation(client_phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)

        utils.send_consecutive_messages(phone, ["Respuesta enviada al cliente. ¿Necesitas algo más?"], client, bot_config.WHATSAPP_SENDER_NUMBER)
        show_gerente_menu(phone, client, conversation_state)
        return "Mensaje enviado", 200

    if "reporte" in incoming_msg_lower or "interesados" in incoming_msg_lower:
        logger.info(f"Gerente ({phone}) requested a report of interested clients")
        stage_filter = None
        interest_filter = None

        stage_match = re.search(r'etapa (\w+)', incoming_msg_lower)
        if stage_match:
            stage_filter = stage_match.group(1).capitalize()

        interest_match = re.search(r'interés (\d+)', incoming_msg_lower)
        if interest_match:
            interest_filter = int(interest_match.group(1))

        report_messages = generate_detailed_report(conversation_state, stage_filter, interest_filter)
        utils.send_consecutive_messages(phone, report_messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
        logger.debug(f"Sent report to gerente: {report_messages}")
        
        update_leads_excel(conversation_state)
        
        utils.send_consecutive_messages(phone, ["Reporte enviado y actualizado en leads_giselle.xlsx. ¿Necesitas algo más?"], client, bot_config.WHATSAPP_SENDER_NUMBER)
        show_gerente_menu(phone, client, conversation_state)
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
                "Nombres de clientes interesados:",
                ", ".join(interested_clients)
            ]
        else:
            messages = ["No hay clientes interesados registrados."]
        utils.send_consecutive_messages(phone, messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
        utils.send_consecutive_messages(phone, ["¿Necesitas algo más?"], client, bot_config.WHATSAPP_SENDER_NUMBER)
        show_gerente_menu(phone, client, conversation_state)
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
            utils.save_conversation(client_phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
            utils.send_consecutive_messages(phone, [f"Cliente {client_phone} marcado como prioritario.", "¿Necesitas algo más?"], client, bot_config.WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone, client, conversation_state)
            return "Cliente marcado como prioritario", 200
        else:
            utils.send_consecutive_messages(phone, ["No encontré al cliente especificado o es un gerente.", "¿En qué más puedo asistirte?"], client, bot_config.WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone, client, conversation_state)
            return "Cliente no encontrado", 200

    if "resumen del día" in incoming_msg_lower:
        logger.info(f"Gerente ({phone}) requested daily activity summary")
        summary_messages = utils.generate_daily_summary(conversation_state)
        utils.send_consecutive_messages(phone, summary_messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
        utils.send_consecutive_messages(phone, ["¿Necesitas algo más?"], client, bot_config.WHATSAPP_SENDER_NUMBER)
        show_gerente_menu(phone, client, conversation_state)
        return "Resumen enviado", 200

    if "resumen semanal" in incoming_msg_lower:
        logger.info(f"Gerente ({phone}) requested weekly summary")
        report_messages = generate_detailed_report(conversation_state)
        utils.send_consecutive_messages(phone, report_messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
        utils.send_consecutive_messages(phone, ["¿Necesitas algo más?"], client, bot_config.WHATSAPP_SENDER_NUMBER)
        show_gerente_menu(phone, client, conversation_state)
        return "Resumen semanal enviado", 200

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
                'date': (datetime.now().date() + timedelta(days=1)).strftime('%Y-%m-%d')
            }
            if 'tasks' not in conversation_state[phone]:
                conversation_state[phone]['tasks'] = []
            conversation_state[phone]['tasks'].append(task)
            utils.save_conversation(phone, conversation_state, bot_config.GCS_BUCKET_NAME, bot_config.GCS_CONVERSATIONS_PATH)
            utils.send_consecutive_messages(phone, [f"Tarea asignada: Llamar a {client_phone} mañana a las {time_str}.", "¿Necesitas algo más?"], client, bot_config.WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone, client, conversation_state)
            return "Tarea asignada", 200
        else:
            utils.send_consecutive_messages(phone, ["No encontré al cliente especificado o es un gerente.", "¿En qué más puedo asistirte?"], client, bot_config.WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone, client, conversation_state)
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
            stage = state.get('stage', 'Prospección')
            interest_level = state.get('interest_level', 0)
            last_contact = state.get('last_contact', 'N/A')
            last_messages = state.get('history', [])[-3:] if state.get('history') else ['Sin mensajes']
            zoom_scheduled = state.get('zoom_scheduled', False)
            zoom_details = state.get('zoom_details', {})
            messages = [
                f"Información del Cliente {client_phone}",
                f"Nombre: {client_name}",
                f"Proyecto: {project}",
                f"Presupuesto: {budget}",
                f"Etapa: {stage}",
                f"Nivel de Interés: {interest_level}/10",
                f"Último Contacto: {last_contact}",
                f"Reunión Zoom Agendada: {'Sí' if zoom_scheduled else 'No'}"
            ]
            if zoom_scheduled and zoom_details:
                messages.append(f"Detalles de Zoom: {zoom_details.get('day')} a las {zoom_details.get('time')}")
            messages.append("Últimos Mensajes:")
            messages.extend([f"- {msg}" for msg in last_messages])
            utils.send_consecutive_messages(phone, messages, client, bot_config.WHATSAPP_SENDER_NUMBER)
            utils.send_consecutive_messages(phone, ["¿Necesitas algo más?"], client, bot_config.WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone, client, conversation_state)
            return "Información enviada", 200
        else:
            utils.send_consecutive_messages(phone, ["No encontré al cliente especificado o es un gerente.", "¿En qué más puedo asistirte?"], client, bot_config.WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone, client, conversation_state)
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
            faq_file_path = os.path.join(bot_config.GCS_BASE_PATH, project_folder, faq_file_name)
            logger.debug(f"Attempting to save FAQ entry to {faq_file_path}: {faq_entry}")

            try:
                temp_faq_path = f"/tmp/{faq_file_name}"
                try:
                    storage_client = storage.Client()
                    bucket = storage_client.bucket(bot_config.GCS_BUCKET_NAME)
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
                utils.send_consecutive_messages(phone, ["Ocurrió un error al guardar la FAQ.", "¿En qué más puedo asistirte?"], client, bot_config.WHATSAPP_SENDER_NUMBER)
                show_gerente_menu(phone, client, conversation_state)
                return "Error al guardar FAQ", 500

            utils.send_consecutive_messages(phone, [f"FAQ añadida para {project}: {question}.", "¿Necesitas algo más?"], client, bot_config.WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone, client, conversation_state)
            return "FAQ añadida", 200
        else:
            utils.send_consecutive_messages(phone, ["Formato incorrecto. Usa: Añade FAQ para [Proyecto]: Pregunta: [Pregunta] Respuesta: [Respuesta]", "¿En qué más puedo asistirte?"], client, bot_config.WHATSAPP_SENDER_NUMBER)
            show_gerente_menu(phone, client, conversation_state)
            return "Formato incorrecto", 200

    show_gerente_menu(phone, client, conversation_state)
    return "Mensaje recibido", 200

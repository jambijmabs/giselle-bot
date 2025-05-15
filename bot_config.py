# bot_config.py
import re
from datetime import datetime, timedelta

# Definición de la personalidad y características de GISELLE
BOT_PERSONALITY = """
Eres Giselle, una asesora de ventas de FAV Living, una empresa inmobiliaria. 
Tu objetivo es vender propiedades inmobiliarias de manera natural e improvisada, como lo haría una vendedora real. 
Actúa de forma fluida, profesional y cercana, como si estuvieras charlando con un amigo. 
No des demasiada información de una vez; suelta los detalles poco a poco para mantener el interés del cliente. 
Usa un lenguaje que despierte curiosidad, como "un proyecto que creo que te va a encantar" o "una ubicación que te sorprenderá". 
Responde únicamente basándote en la información de los proyectos que tienes disponible, sin inventar información adicional. 
Si el cliente hace una pregunta y no tienes la información exacta para responder, di algo como 'No sé exactamente, pero déjame investigarlo' 
y continúa la conversación de manera natural. 
No uses emoticones ni compartas información personal sobre ti más allá de tu rol en FAV Living.

Incorpora técnicas de negociación del libro "Rompe la Barrera del No" de Chris Voss:
- Usa **empatía táctica**: muestra que entiendes las emociones del cliente etiquetando sus sentimientos (e.g., "Parece que estás buscando algo más asequible").
- Aplica la técnica del **espejo**: repite las últimas 1-3 palabras o la idea principal del cliente para fomentar que continúe hablando (e.g., Cliente: "No estoy seguro", GISELLE: "¿No estás seguro?").
- Haz **preguntas calibradas**: utiliza preguntas abiertas que comiencen con "cómo" o "qué" para obtener más información y guiar al cliente hacia una decisión (e.g., "¿Cómo te gustaría estructurar el plan de pago?").
- Maneja objeciones con empatía y preguntas, enfocándote en entender las necesidades del cliente y no en forzar una venta.
"""

# Instrucciones específicas para las respuestas
RESPONSE_INSTRUCTIONS = """
- Responde de manera breve, con 1-2 frases cortas por mensaje (máximo 15-20 palabras por mensaje). 
- Divide la información en mensajes consecutivos si es necesario, soltando detalles poco a poco para mantener el interés.
- Usa un tono natural y conversacional, como si estuvieras charlando con un amigo (e.g., "Tenemos algo que creo que te va a gustar").
- Despierta curiosidad con frases intrigantes como "una ubicación que te sorprenderá" o "un detalle que hace este proyecto único".
- No des toda la información de una vez; por ejemplo, menciona un proyecto y espera a que el cliente pregunte más (e.g., "Tenemos KABAN en Holbox... ¿te gustaría saber más?").
- Si el cliente solicita información adicional o documentos, proporciona las URLs de los archivos descargables disponibles en el archivo informativo del proyecto, sin inventar enlaces.
- Pregunta por el nombre del cliente en el primer mensaje: "Hola... ¿Cuál es tu nombre?".
- Pregunta por el presupuesto del cliente de manera natural después de conocer su nombre, pero no insistas (e.g., "¿Tienes un presupuesto en mente?").
- Si el cliente no ha respondido después de 2 mensajes, pregunta por su horario y días preferidos de contacto de manera natural.
- Cuando detectes objeciones (como "es muy caro", "no estoy seguro"), aplica las técnicas de "Rompe la Barrera del No": usa el espejo, etiquetado y preguntas calibradas.
- Después de proporcionar información básica sobre un proyecto, ofrece proactivamente enviar las URLs de archivos relevantes si el cliente no las ha solicitado.
"""

# Mensajes predefinidos
INITIAL_INTRO = """
Hola... ¿Cuál es tu nombre?
"""
TEMPLATE_RESPONSE = """
Hola, soy Giselle de FAV Living. ¿Te interesa un proyecto inmobiliario?
"""
NO_INTEREST_RESPONSE = """
Entendido, parece que no te interesa ahora. ¿Qué te preocupa?
"""
SCHEDULED_CONTACT_RESPONSE = """
Perfecto, te contactaré en {time_amount} {time_unit}. ¿Qué te interesa?
"""
NEXT_WEEK_CONTACT_RESPONSE = """
Perfecto, te contactaré la próxima semana. ¿Qué día te viene bien?
"""
RECONTACT_MESSAGE = """
Hola, soy Giselle de FAV Living. ¿Sigues interesado en KABAN Holbox?
"""
RECONTACT_NO_RESPONSE_MESSAGE = """
Hola, soy Giselle de FAV Living. ¿Te interesa saber más de KABAN?
"""
BUDGET_QUESTION = """
¿Tienes un presupuesto en mente para tu propiedad?
"""
CONTACT_TIME_QUESTION = """
¿En qué días y horarios te viene mejor charlar?
"""
FILE_SENT_MESSAGE = """
Aquí tienes la URL de "{requested_file}": {file_url}
"""
FILE_ERROR_MESSAGE = """
Lo siento, no encontré "{requested_file}". ¿Quieres otro archivo?
"""
LOCATION_MESSAGE = """
La ubicación del proyecto está aquí: {location_url}
"""
OFFER_FILES_MESSAGE = """
Puedo enviarte más detalles de {project}. ¿Qué te interesa ver?
"""

# Frases de no interés
NO_INTEREST_PHRASES = [
    "no me interesa", "no estoy interesado", "no quiero comprar",
    "no gracias", "no por el momento", "no estoy buscando"
]

def should_ask_name(state, conversation_history):
    """Determine if GISELLE should ask for the client's name."""
    return (
        state.get('name_asked', 0) < 2 and
        not state.get('client_name') and
        not any("mi nombre es" in msg.lower() for msg in conversation_history)
    )

def should_ask_budget(state, conversation_history):
    """Determine if GISELLE should ask for the client's budget."""
    return (
        state.get('budget_asked', 0) < 2 and
        state.get('messages_since_budget_ask', 0) >= 1 and
        not state.get('client_budget') and
        not any("mi presupuesto es" in msg.lower() or "presupuesto de" in msg.lower() for msg in conversation_history)
    )

def should_ask_contact_time(state, conversation_history):
    """Determine if GISELLE should ask for preferred contact days and times."""
    return (
        state.get('contact_time_asked', 0) < 2 and
        len(conversation_history) >= 2 and
        not state.get('preferred_time') and
        not state.get('preferred_days') and
        not any("prefiero ser contactado" in msg.lower() or "horario" in msg.lower() for msg in conversation_history)
    )

def should_offer_files(state, conversation_history, project):
    """Determine if GISELLE should proactively offer files for a project."""
    project_mentioned = any(project.lower() in msg.lower() for msg in conversation_history[-3:])
    files_offered = any("Puedo enviarte más detalles" in msg for msg in conversation_history[-3:])
    return project_mentioned and not files_offered

def handle_no_interest_response():
    """Generate response for no interest."""
    return [NO_INTEREST_RESPONSE]

def handle_recontact_request(incoming_msg, state):
    """Handle recontact requests (e.g., 'contáctame en 5 minutos' or 'próxima semana')."""
    recontact_pattern = r"(contacta|contáctame|contactarme)\s*(en)?\s*(\d+)\s*(minuto|minutos|hora|horas)?"
    recontact_match = re.search(recontact_pattern, incoming_msg.lower())
    if recontact_match:
        time_amount = int(recontact_match.group(3))
        time_unit = recontact_match.group(4) if recontact_match.group(4) else "minutos"
        if time_unit.startswith("minuto"):
            delta = timedelta(minutes=time_amount)
        else:  # horas
            delta = timedelta(hours=time_amount)
        schedule_time = datetime.now() + delta
        state['schedule_next'] = {'time': schedule_time.isoformat()}
        return [SCHEDULED_CONTACT_RESPONSE.format(time_amount=time_amount, time_unit=time_unit)]
    elif "próxima semana" in incoming_msg.lower() or "la próxima semana" in incoming_msg.lower():
        schedule_time = datetime.now() + timedelta(days=7)
        preferred_time = state.get('preferred_time', '10:00 AM')
        schedule_time = schedule_time.replace(
            hour=int(preferred_time.split(':')[0]) if ':' in preferred_time else 10,
            minute=int(preferred_time.split(':')[1].replace(' AM', '').replace(' PM', '')) if ':' in preferred_time else 0,
            second=0,
            microsecond=0
        )
        if 'PM' in preferred_time.upper() and schedule_time.hour < 12:
            schedule_time = schedule_time.replace(hour=schedule_time.hour + 12)
        state['schedule_next'] = {'time': schedule_time.isoformat()}
        return [NEXT_WEEK_CONTACT_RESPONSE]
    return None

def handle_recontact(phone, state, current_time):
    """Handle scheduled recontacts."""
    if state.get('no_interest', False):
        return None, False

    last_contact = state.get('last_contact')
    recontact_attempts = state.get('recontact_attempts', 0)
    schedule_next = state.get('schedule_next')

    if schedule_next:
        schedule_time_str = schedule_next.get('time')
        try:
            schedule_time = datetime.fromisoformat(schedule_time_str)
        except ValueError as e:
            return f"Error parsing schedule time: {str(e)}", False

        if current_time >= schedule_time:
            preferred_time = state.get('preferred_time', '10:00 AM')
            messages = [
                "Hola, soy Giselle de FAV Living.",
                RECONTACT_MESSAGE
            ]
            state['schedule_next'] = None
            state['last_contact'] = current_time.isoformat()
            state['recontact_attempts'] = 0
            return messages, True
        return None, False

    if last_contact and recontact_attempts < 3:
        last_contact_time = datetime.fromisoformat(last_contact)
        if (current_time - last_contact_time).days >= 3:
            preferred_time = state.get('preferred_time', '10:00 AM')
            messages = [
                "Hola, soy Giselle de FAV Living.",
                RECONTACT_NO_RESPONSE_MESSAGE
            ]
            state['recontact_attempts'] = recontact_attempts + 1
            state['last_contact'] = current_time.isoformat()
            return messages, True

    return None, False

# Bot Configuration File

# Bot Personality
BOT_PERSONALITY = """
Soy Giselle, tu asesora personal de FAV Living, una desarrolladora inmobiliaria apasionada por crear espacios únicos. Me encanta charlar contigo de manera cercana y amigable, como si estuviéramos tomando un café mientras exploramos tus sueños de inversión o un nuevo hogar. Mi objetivo es conocerte mejor, entender tus necesidades y ofrecerte opciones que realmente te emocionen, todo con un toque cálido y humano. Puedo darte información clara y útil sobre nuestros proyectos, resolver tus dudas con empatía y, cuando sea el momento perfecto, sugerir una charla más personal para profundizar en lo que buscas.
"""

# ChatGPT Model Configuration
CHATGPT_MODEL = "gpt-4.1-mini"

# Response Instructions
RESPONSE_INSTRUCTIONS = """
- Responde como si fueras una amiga cercana y profesional, con un tono cálido, empático y humano que invite a seguir la conversación.
- Prioriza respuestas breves y naturales (1-2 oraciones), evitando jerga técnica a menos que el cliente la solicite explícitamente.
- Adapta tu respuesta al historial de conversación: si el cliente ya preguntó algo, no repitas información; si parece indeciso, sé más suave y ofrécele opciones; si muestra interés, profundiza con detalles relevantes.
- Sé proactiva e inteligente: si el cliente saluda ("hola"), retoma el último tema de conversación o sugiere algo relacionado con sus intereses previos; si pregunta algo genérico, responde con un dato interesante y una pregunta para profundizar.
- Si el cliente solicita información financiera, ofrece un análisis breve, sencillo y optimista, resaltando beneficios.
- Si no tienes la información solicitada, responde con empatía ("Entiendo, voy a consultar eso para ti") y sugiere una alternativa para mantener la charla fluida.
- **Evita ofrecer una reunión por Zoom de inmediato**; espera a que el cliente haya interactuado más y mostrado interés claro.
- **Cada respuesta debe terminar con una pregunta que sea específica y relevante al contexto de la conversación**, para fomentar la continuidad del diálogo (por ejemplo, si hablas de KABAN: "¿Te interesa saber más sobre el esquema de renta o prefieres que te cuente sobre las unidades disponibles?").
- **No uses preguntas genéricas como "¿En qué más puedo ayudarte?"**; las preguntas deben estar directamente relacionadas con el tema que se está discutiendo.
"""

# Gerente Configuration
GERENTE_PHONE = "whatsapp:+5218110665094"
GERENTE_ROLE = "Gerente de Ventas"
GERENTE_NUMBERS = ["+5218110665094"]

# WhatsApp Configuration
WHATSAPP_SENDER_NUMBER = "whatsapp:+18188732305"

# GCS Configuration
GCS_BUCKET_NAME = "giselle-projects"
GCS_BASE_PATH = "PROYECTOS"
GCS_CONVERSATIONS_PATH = "CONVERSATIONS"

# Recontact Configuration
RECONTACT_TEMPLATE_NAME = "follow_up_template"
RECONTACT_MIN_DAYS = 1
RECONTACT_HOUR_CST = 18
RECONTACT_MINUTE_CST = 5
RECONTACT_TOLERANCE_MINUTES = 5

# Report Configuration
WEEKLY_REPORT_DAY = "Sunday"
WEEKLY_REPORT_TIME = "18:00"
LEADS_EXCEL_PATH = "leads_giselle.xlsx"

# FAQ Configuration
FAQ_RESPONSE_DELAY = 30

# Phrases indicating lack of interest
NO_INTEREST_PHRASES = [
    "no estoy interesado",
    "no me interesa",
    "no gracias",
    "no quiero",
    "no lo necesito",
    "no es para mí",
    "no estoy buscando eso"
]

def handle_no_interest_response():
    return ["Entiendo, gracias por tu tiempo. ¿Hay algo más en lo que pueda ayudarte o prefieres que te contacte en otro momento?"]

# Recontact Logic
RECONTACT_PHRASES = [
    "contáctame después",
    "más tarde",
    "en otro momento",
    "luego",
    "mañana",
    "la próxima semana",
    "en unos días"
]

def handle_recontact_request(incoming_msg, state):
    incoming_msg_lower = incoming_msg.lower()
    if any(phrase in incoming_msg_lower for phrase in RECONTACT_PHRASES):
        # Extract preferred time or day if mentioned
        preferred_time = None
        preferred_days = None
        time_match = re.search(r'a las (\d{1,2}(?::\d{2})?\s*(?:AM|PM))', incoming_msg_lower, re.IGNORECASE)
        if time_match:
            preferred_time = time_match.group(1)
        day_match = re.search(r'(mañana|el \w+)', incoming_msg_lower)
        if day_match:
            preferred_days = day_match.group(1)

        # Update conversation state
        state['schedule_next'] = datetime.now().isoformat()
        if preferred_time:
            state['preferred_time'] = preferred_time
        if preferred_days:
            state['preferred_days'] = preferred_days

        return ["Entendido, te contactaré más tarde. ¿Algo más en lo que pueda ayudarte ahora? 😊"], True
    return None, False

def handle_recontact(phone, state, current_time):
    if state.get('no_interest', False):
        return None, False

    schedule_next = state.get('schedule_next')
    if not schedule_next:
        return None, False

    last_contact = datetime.fromisoformat(state.get('last_contact', current_time.isoformat()))
    time_since_last_contact = (current_time - last_contact).total_seconds() / 3600  # in hours

    if time_since_last_contact < 48:  # Wait at least 2 days
        return None, False

    # Reset schedule_next to prevent repeated recontact
    state['schedule_next'] = None

    # Prepare recontact message
    client_name = state.get('client_name', 'Cliente')
    last_mentioned_project = state.get('last_mentioned_project', 'uno de nuestros proyectos')
    messages = [
        f"Hola {client_name}, soy Giselle de FAV Living. Quería retomar nuestra conversación sobre {last_mentioned_project}, ¿cómo estás hoy?",
        "¿Te gustaría que te cuente más detalles o prefieres hablar de algo diferente?"
    ]

    state['recontact_attempts'] = state.get('recontact_attempts', 0) + 1
    return messages, True

# Zoom Scheduling Configuration
ZOOM_AVAILABLE_SLOTS = [
    {"day": "Lunes", "times": ["10:00 AM", "2:00 PM", "4:00 PM"]},
    {"day": "Martes", "times": ["10:00 AM", "2:00 PM", "4:00 PM"]},
    {"day": "Miércoles", "times": ["10:00 AM", "2:00 PM", "4:00 PM"]},
    {"day": "Jueves", "times": ["10:00 AM", "2:00 PM", "4:00 PM"]},
    {"day": "Viernes", "times": ["10:00 AM", "2:00 PM", "4:00 PM"]}
]

ZOOM_PROPOSAL_MESSAGE = [
    f"Me encantaría conocerte un poco más y hablar contigo en una videollamada para explorar juntos lo que estás buscando.",
    "Tengo algunos horarios disponibles para Zoom: {slots}",
    "¿Qué día te vendría mejor? 😊"
]

ZOOM_CONFIRMATION_MESSAGE = [
    "¡Qué emoción, {client_name}! Ya agendamos nuestra reunión por Zoom para el {day} a las {time}.",
    "Te enviaré el enlace un ratito antes. ¿Hay algo más que te gustaría saber mientras tanto?"
]

ZOOM_NOTIFICATION_TO_GERENTE = [
    "Nueva reunión por Zoom agendada:",
    "Cliente: {client_name} ({phone})",
    "Fecha y Hora: {day} a las {time}"
]

# Project Keyword Mapping
PROJECT_KEYWORD_MAPPING = {
    "tulum": "MUWAN",
    "holbox": "KABAN",
    "pesquería": "CALIDRIS",
    "pesqueria": "CALIDRIS",
    "aldea zama": "ANEMONA",
    "comercial": "ANEMONA",
    "condohotel": "KABAN",
    "departamentos": "CALIDRIS"
}

# Gerente Reminder Message for Pending Questions
GERENTE_REMINDER_MESSAGE = [
    "Hola, tienes una pregunta pendiente de un cliente que no ha sido respondida:",
    "Cliente: {client_phone}",
    "Pregunta: {question}",
    "Por favor, responde lo antes posible. ¿Necesitas ayuda con algo más?"
]

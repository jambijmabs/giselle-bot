# Bot Configuration File

# Bot Personality
BOT_PERSONALITY = """
Soy Giselle, una asesora de ventas profesional, amigable y cálida de FAV Living, una desarrolladora inmobiliaria de prestigio. Además, soy una analista inmobiliaria con experiencia en datos financieros, lo que me permite ofrecer análisis detallados sobre por qué invertir en nuestros proyectos es una gran oportunidad. Mi objetivo es informarte sobre nuestros proyectos, resolver tus dudas de manera breve y directa, y agendar una reunión por Zoom para discutir tus necesidades en detalle. Estoy aquí para guiarte en cada paso del proceso de compra.
"""

# ChatGPT Model Configuration
CHATGPT_MODEL = "gpt-4.1-mini"

# Response Instructions
RESPONSE_INSTRUCTIONS = """
- Responde de manera profesional, amigable y natural, como una asesora de ventas y analista inmobiliaria.
- Prioriza respuestas cortas y directas, de no más de 2-3 oraciones, evitando información innecesaria.
- Si el cliente solicita información financiera, proporciona un análisis breve y específico.
- Evita repetir información ya compartida; utiliza el historial para responder de manera precisa.
- Usa un tono cálido y profesional, dirigiéndote al cliente por su nombre cuando sea posible.
- Si no tienes la información solicitada (como amenidades específicas o fechas exactas), indica que puedes consultar con el gerente, pero solo si la pregunta es clara y relevante.
- Tu objetivo principal es agendar una reunión por Zoom una vez que el cliente haya sido perfilado y haya mostrado interés inicial.
"""

# Gerente Configuration
GERENTE_PHONE = "whatsapp:+5218110665094"
GERENTE_ROLE = "Gerente de Ventas"

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
    return ["Entiendo, gracias por tu tiempo. ¿En qué más puedo ayudarte?"]

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

        return ["Entendido, te contactaré más tarde. ¿Algo más en lo que pueda ayudarte?"], True
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
        f"Hola {client_name}, soy Giselle de FAV Living. Quería seguir con nuestra charla sobre {last_mentioned_project}.",
        "¿Te interesa saber más detalles?"
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
    f"Me encantaría charlar contigo en Zoom para hablar de tus necesidades y mostrarte más detalles.",
    "Horarios disponibles: {slots}",
    "Por favor, selecciona un día y horario (ejemplo: 'Lunes a las 10:00 AM')."
]

ZOOM_CONFIRMATION_MESSAGE = [
    "¡Listo, {client_name}! Agendamos nuestra reunión por Zoom para el {day} a las {time}.",
    "Te enviaré el enlace antes de la reunión. ¿Algo más en lo que pueda ayudarte?"
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

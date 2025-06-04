# Bot Configuration File

# Bot Personality
BOT_PERSONALITY = """
Soy Giselle, una asesora de ventas profesional, amigable y cálida de FAV Living, una desarrolladora inmobiliaria de prestigio. Además, soy una analista inmobiliaria con experiencia en datos financieros, lo que me permite ofrecer análisis detallados sobre por qué invertir en nuestros proyectos es una gran oportunidad. Puedo ayudarte con información sobre esquemas de pago, financiamiento y retorno de inversión (ROI) con renta garantizada cuando lo solicites. Mi objetivo es informarte sobre nuestros proyectos, generar interés y resolver tus dudas de manera natural. Estoy aquí para guiarte en cada paso del proceso de compra, asegurándome de que tengas toda la información que necesitas para tomar una decisión informada.
"""

# ChatGPT Model Configuration
CHATGPT_MODEL = "gpt-4.1-mini"

# Response Instructions
RESPONSE_INSTRUCTIONS = """
- Responde de manera profesional, amigable y natural, como una asesora de ventas y analista inmobiliaria.
- Si el cliente solicita información financiera, proporciona un análisis detallado basado en esquemas de pago, financiamiento y ROI con renta garantizada.
- Evita hacer preguntas rígidas o seguir un flujo predeterminado; adapta tus respuestas al contexto del mensaje del cliente.
- Usa un tono cálido y profesional, dirigiéndote al cliente por su nombre cuando sea posible.
- Si no tienes la información solicitada (como amenidades específicas o fechas exactas), indica que puedes consultar con el gerente.
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
    return ["Entiendo, gracias por tu tiempo. Si cambias de idea o necesitas información en el futuro, estaré encantada de ayudarte."]

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

        return ["Entendido, te contactaré más tarde. ¿Hay algo más en lo que pueda ayudarte ahora?"], True
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
        f"Hola {client_name}, soy Giselle de FAV Living. Quería dar seguimiento a nuestra conversación sobre {last_mentioned_project}.",
        "¿Te gustaría saber más detalles o prefieres que hagas un análisis financiero de la inversión?"
    ]

    state['recontact_attempts'] = state.get('recontact_attempts', 0) + 1
    return messages, True

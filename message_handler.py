import re
import logging
from openai import OpenAI
import bot_config

# Configure logger
logger = logging.getLogger(__name__)

# Initialize OpenAI client and global data
openai_client = None
projects_data = {}
downloadable_urls = {}
project_details = {}

def initialize_message_handler(openai_api_key, projects_data_ref, downloadable_urls_ref):
    global openai_client, projects_data, downloadable_urls
    openai_client = OpenAI(api_key=openai_api_key)
    projects_data = projects_data_ref
    downloadable_urls = downloadable_urls_ref
    logger.debug(f"Initialized with projects_data: {list(projects_data.keys())}")
    logger.debug(f"Initialized with downloadable_urls: {downloadable_urls}")
    extract_project_details()

def extract_project_details():
    global project_details
    project_details = {}
    for project, data in projects_data.items():
        project_details[project] = {'prices': [], 'payment_plans': [], 'discounts': {}}
        lines = data.split('\n')
        current_section = None

        for line in lines:
            line = line.strip()
            if line.startswith('Unidades Disponibles:'):
                current_section = 'prices'
                continue
            elif line.startswith('Planes de Pago:'):
                current_section = 'payment_plans'
                continue

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
            elif current_section == 'payment_plans' and line.startswith('-'):
                match = re.match(r'- Opción (.*?): (.*?) \((\d+)% descuento\)', line)
                if match:
                    option, plan, discount = match.groups()
                    project_details[project]['payment_plans'].append({'option': option, 'plan': plan})
                    project_details[project]['discounts'][option] = f"{discount}%"

        logger.debug(f"Extracted details for {project}: {project_details[project]}")

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

    # Responder a solicitudes de precios
    if any(keyword in normalized_msg for keyword in ["precio", "cuesta", "cuánto"]):
        if mentioned_project and mentioned_project in project_details:
            prices = project_details[mentioned_project]['prices']
            if prices:
                messages.append(f"¡Hola {client_name}! Te comparto los precios de {mentioned_project}:")
                for price_info in prices:
                    messages.append(f"- {price_info['unit']}: {price_info['bedrooms']} por {price_info['price']}")
                messages.append("¿Te interesa alguna unidad o más detalles?")
            else:
                messages.append(f"Lo siento, {client_name}, no tengo precios para {mentioned_project} ahora.")
        else:
            messages.append(f"Lo siento, {client_name}, no tengo información de ese proyecto.")
        return messages, mentioned_project

    # Responder a solicitudes de archivos
    if any(keyword in normalized_msg for keyword in ["presentación", "envíame", "mándame"]):
        if mentioned_project and mentioned_project in downloadable_urls:
            file_key = 'presentacion de venta'  # Normalizar según el archivo
            file_url = downloadable_urls[mentioned_project].get(file_key)
            logger.debug(f"Looking for file '{file_key}' in {mentioned_project}: {file_url}")
            if file_url:
                messages.append(f"¡Claro, {client_name}! Aquí tienes la presentación de {mentioned_project}:")
                messages.append(file_url)
            else:
                messages.append(f"Lo siento, {client_name}, no encontré la presentación de {mentioned_project}.")
        return messages, mentioned_project

    # Respuesta genérica con OpenAI
    prompt = (
        f"{bot_config.BOT_PERSONALITY}\n"
        f"**Información de proyectos:**\n{project_info}\n"
        f"**Historial:**\n{conversation_history}\n"
        f"**Mensaje:** \"{incoming_msg}\""
    )
    response = openai_client.chat.completions.create(
        model=bot_config.CHATGPT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=100,
        temperature=0.7
    )
    reply = response.choices[0].message.content.strip()
    messages.append(reply)

    return messages, mentioned_project

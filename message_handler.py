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
    # Check for specific requests (prices, payment plans, discounts)
    normalized_msg = incoming_msg.lower().replace(" ", "")

    # Initialize response messages
    messages = []
    handled = False

    # Find the mentioned project
    mentioned_project = None
    for project in projects_data.keys():
        if project.lower() in normalized_msg:
            mentioned_project = project
            break
    if not mentioned_project:
        mentioned_project = list(projects_data.keys())[0] if projects_data else None

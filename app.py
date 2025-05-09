import re
import pandas as pd
import psycopg2
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from flask import Flask, request
from datetime import datetime
import os
import logging
from openai import OpenAI
from google.cloud import storage
import random
import sys

# Configure logging to output to stdout/stderr (Cloud Run captures these)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),  # Log to stdout for Cloud Run
        logging.FileHandler('giselle_activity.log')  # Also log to file
    ]
)

# Suppress pdfminer warnings (even though we're not using pdfminer in this code)
logging.getLogger("pdfminer").setLevel(logging.ERROR)

# Initialize Flask app
app = Flask(__name__)

# Configure logger
logger = logging.getLogger(__name__)

# Configuration for Twilio (using environment variables)
account_sid = os.getenv('TWILIO_ACCOUNT_SID')
auth_token = os.getenv('TWILIO_AUTH_TOKEN')
if not account_sid or not auth_token:
    logger.error("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set in environment variables")
    raise ValueError("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set")
client = Client(account_sid, auth_token)

# Configuration for Grok API (using environment variables)
grok_client = OpenAI(
    api_key=os.getenv('GROK_API_KEY'),
    base_url='https://api.x.ai/v1'
)
if not os.getenv('GROK_API_KEY'):
    logger.error("GROK_API_KEY not set in environment variables")
    raise ValueError("GROK_API_KEY not set")

# Configuration for PostgreSQL (Cloud SQL)
try:
    conn = psycopg2.connect(
        dbname='giselle',
        user='postgres',
        password=os.getenv('DB_PASSWORD', 'Tulipan8'),
        host=os.getenv('DB_HOST', '34.174.23.67'),
        port='5432'
    )
    cursor = conn.cursor()
    logger.info("Successfully connected to the database")
except Exception as e:
    logger.error(f"Failed to connect to the database: {str(e)}", exc_info=True)
    raise

# Create tables if they don't exist
cursor.execute("""
    CREATE TABLE IF NOT EXISTS leads (
        id SERIAL PRIMARY KEY,
        name TEXT,
        phone TEXT,
        project TEXT,
        budget REAL,
        preferred_time TEXT,
        location TEXT,
        status TEXT,
        last_contact TEXT
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS properties (
        project_name TEXT PRIMARY KEY,
        general TEXT,
        common_areas TEXT,
        typologies TEXT,
        construction_specs TEXT,
        delivery_specs TEXT,
        prices TEXT,
        payment_plans TEXT,
        last_updated TEXT
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS interactions (
        id SERIAL PRIMARY KEY,
        lead_id INTEGER,
        message TEXT,
        response TEXT,
        timestamp TEXT,
        FOREIGN KEY (lead_id) REFERENCES leads(id)
    )
""")
conn.commit()

# Dictionary to store downloadable links (filled dynamically)
downloadable_links = {}

# Log all incoming requests
@app.before_request
def log_request_info():
    logger.debug(f"Incoming request: {request.method} {request.url} {request.form}")

# Log unhandled exceptions
@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Unhandled exception: {str(e)}", exc_info=True)
    return "Internal Server Error", 500

# Function to download files from Cloud Storage
def download_projects_from_storage(bucket_name='giselle-projects', base_path='PROYECTOS'):
    try:
        if not os.path.exists(base_path):
            os.makedirs(base_path)
            logger.debug(f"Created directory {base_path}")

        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blobs = bucket.list_blobs(prefix=base_path)

        for blob in blobs:
            local_path = blob.name
            if not os.path.exists(os.path.dirname(local_path)):
                os.makedirs(os.path.dirname(local_path))
            blob.download_to_filename(local_path)
            logger.info(f"Descargado archivo desde Cloud Storage: {local_path}")
    except Exception as e:
        logger.error(f"Error downloading projects from Cloud Storage: {str(e)}", exc_info=True)
        raise

# Function to extract text from .txt files
def extract_text_from_txt(txt_path):
    try:
        with open(txt_path, 'r', encoding='utf-8') as file:
            text = file.read()
        logger.info(f"Archivo de texto {txt_path} leído correctamente.")
        return text
    except Exception as e:
        logger.error(f"Error al leer archivo de texto {txt_path}: {str(e)}", exc_info=True)
        return ""

# Process project text (extract sections from text)
def process_project_text(text):
    project_info = {
        "general": "",
        "common_areas": "",
        "typologies": "",
        "construction_specs": "",
        "delivery_specs": "",
        "prices": "",
        "payment_plans": ""
    }
    lines = text.split('\n')
    current_field = None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        line_lower = line.lower()

        # Identify sections
        if any(keyword in line_lower for keyword in ["descripción", "detalle", "proyecto", "edificio", "ubicación", "torre"]):
            current_field = "general"
            project_info["general"] += line + " "
        elif any(keyword in line_lower for keyword in ["amenidades", "áreas comunes", "piscina", "gimnasio", "salón", "parque"]):
            current_field = "common_areas"
            project_info["common_areas"] += line + " "
        elif any(keyword in line_lower for keyword in ["unidades", "tipología", "dormitorio", "recámara", "m²", "baño"]):
            current_field = "typologies"
            project_info["typologies"] += line + " "
        elif any(keyword in line_lower for keyword in ["construcción", "materiales", "acabados", "estructura"]):
            current_field = "construction_specs"
            project_info["construction_specs"] += line + " "
        elif any(keyword in line_lower for keyword in ["entrega", "plazo", "finalización", "fecha"]):
            current_field = "delivery_specs"
            project_info["delivery_specs"] += line + " "
        elif any(keyword in line_lower for keyword in ["costo", "precio", "valor", "usd", "dólares", "$"]):
            current_field = "prices"
            project_info["prices"] += line + " "
        elif any(keyword in line_lower for keyword in ["financiamiento", "pago", "enganche", "cuotas", "mensualidades", "plan"]):
            current_field = "payment_plans"
            project_info["payment_plans"] += line + " "
        elif current_field:
            project_info[current_field] += line + " "

    return project_info

# Load projects from folder (dynamically detect projects)
def load_projects_from_folder(base_path='PROYECTOS'):
    downloadable_files = {}

    if not os.path.exists(base_path):
        os.makedirs(base_path)
        logger.warning(f"Carpeta {base_path} creada, pero no hay proyectos.")
        return downloadable_files

    # Detect projects dynamically, ignoring unwanted directories
    projects = [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d)) and not d.startswith('.') and d != 'DESCARGABLES']
    if not projects:
        logger.warning(f"No se encontraron proyectos en {base_path}.")
        return downloadable_files

    logger.info(f"Proyectos detectados: {', '.join(projects)}")

    # Initialize downloadable_links for each detected project
    for project in projects:
        downloadable_links[project] = {}

    # Load project information (only .txt files outside DESCARGABLES)
    for project in projects:
        project_path = os.path.join(base_path, project)
        file_count = 0
        txt_files = [f for f in os.listdir(project_path) if f.endswith('.txt') and os.path.isfile(os.path.join(project_path, f))]

        if not txt_files:
            logger.warning(f"No se encontraron archivos TXT para el proyecto {project}.")
            continue

        for file in txt_files:
            file_path = os.path.join(project_path, file)
            logger.info(f"Procesando archivo de texto para {project}: {file_path}")
            text = extract_text_from_txt(file_path)
            if text:
                project_info = process_project_text(text)
                try:
                    cursor.execute(
                        """
                        INSERT INTO properties (
                            project_name, general, common_areas, typologies,
                            construction_specs, delivery_specs, prices, payment_plans, last_updated
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (project_name) DO UPDATE SET
                            general = EXCLUDED.general,
                            common_areas = EXCLUDED.common_areas,
                            typologies = EXCLUDED.typologies,
                            construction_specs = EXCLUDED.construction_specs,
                            delivery_specs = EXCLUDED.delivery_specs,
                            prices = EXCLUDED.prices,
                            payment_plans = EXCLUDED.payment_plans,
                            last_updated = EXCLUDED.last_updated
                        """,
                        (
                            project,
                            project_info["general"],
                            project_info["common_areas"],
                            project_info["typologies"],
                            project_info["construction_specs"],
                            project_info["delivery_specs"],
                            project_info["prices"],
                            project_info["payment_plans"],
                            datetime.now().isoformat()
                        )
                    )
                    conn.commit()
                    logger.info(f"Propiedad {project} cargada desde {file_path}")
                    file_count += 1
                except Exception as e:
                    logger.error(f"Error al guardar información del proyecto {project} en la base de datos: {str(e)}", exc_info=True)

        if file_count > 0:
            logger.info(f"Proyecto {project} procesado correctamente. {file_count} archivo(s) cargado(s).")
        else:
            logger.warning(f"No se encontraron archivos TXT válidos para el proyecto {project}.")

        # Process the DESCARGABLES folder
        downloadable_path = os.path.join(project_path, 'DESCARGABLES')
        downloadable_files[project] = []
        if os.path.exists(downloadable_path):
            downloadable_count = 0
            for file in os.listdir(downloadable_path):
                if file.endswith(('.pdf', '.jpg', '.jpeg', '.png')):
                    downloadable_files[project].append(file)
                    downloadable_count += 1
            if downloadable_count > 0:
                logger.info(f"Carpeta DESCARGABLES del proyecto {project} procesada correctamente. {downloadable_count} archivo(s) encontrado(s).")
            else:
                logger.warning(f"Carpeta DESCARGABLES del proyecto {project} está vacía o no contiene archivos válidos.")
        else:
            logger.warning(f"Carpeta DESCARGABLES no encontrada para el proyecto {project}.")

    return downloadable_files

# Determine communication style based on budget
def determine_communication_style(budget):
    try:
        budget = float(budget)
        if budget > 200000:
            return "formal"
        elif budget > 100000:
            return "amigable"
        else:
            return "casual"
    except:
        return "casual"

# Negotiation logic
def negotiate(lead, offer):
    try:
        cursor.execute("SELECT prices FROM properties WHERE project_name = %s", (lead['project'],))
        prices = cursor.fetchone()
        min_price = 100000  # Default value
        if prices and prices[0]:
            price_matches = re.findall(r'\b(\d+)\s*(USD|dólares|dolares)\b', prices[0], re.IGNORECASE)
            min_price = min(int(p[0]) for p in price_matches) if price_matches else 100000

        margin = 0.95 if lead['budget'] and float(lead['budget']) >= min_price * 0.8 else 0.90
        intros = ["Permítame informarle", "Entiendo su oferta", "Gracias por su interés"]

        cursor.execute("SELECT response FROM interactions WHERE lead_id = %s ORDER BY timestamp DESC LIMIT 1", (lead['id'],))
        last_interaction = cursor.fetchone()

        if offer >= min_price * margin:
            return f"{random.choice(intros)}, su oferta de {offer} USD es adecuada. Podemos proceder con el cierre. ¿Confirma el trato?"
        elif offer >= min_price * (margin - 0.05) and last_interaction and "oferta" in last_interaction[0].lower():
            return f"{random.choice(intros)}, estamos cerca. ¿Qué le parece {int(min_price * margin)} USD con un plan de pago personalizado?"
        elif offer >= min_price * (margin - 0.05):
            return f"{random.choice(intros)}, su oferta de {offer} USD está próxima al valor. Podemos ajustarla a {int(min_price * margin)} USD. ¿Qué opina?"
        else:
            return f"{random.choice(intros)}, su oferta de {offer} USD está por debajo del valor mínimo de {min_price} USD. ¿Desea explorar opciones de financiamiento?"
    except Exception as e:
        logger.error(f"Error during negotiation: {str(e)}", exc_info=True)
        return "Lo siento, ocurrió un error al procesar su oferta. ¿En qué puedo ayudarle?"

# Generate response using Grok API
def generate_grok_response(message, lead_data, conversation_history, downloadable_files):
    try:
        project_info = {"general": "Proyecto no especificado", "prices": "", "payment_plans": "", "location": "Ubicación no especificada"}
        if lead_data['project']:
            cursor.execute("SELECT general, prices, payment_plans FROM properties WHERE project_name = %s", (lead_data['project'],))
            project_info_row = cursor.fetchone()
            if project_info_row:
                project_info = {
                    "general": project_info_row[0] or "Información no disponible",
                    "prices": project_info_row[1] or "Precios no disponibles",
                    "payment_plans": project_info_row[2] or "Planes de pago no disponibles",
                    "location": "México"
                }
            else:
                project_info = {
                    "general": "Información no disponible para este proyecto",
                    "prices": "",
                    "payment_plans": "",
                    "location": "Ubicación no especificada"
                }

            # Real-time location search using Grok API
            location_prompt = (
                f"Proporciona información sobre la ubicación del proyecto inmobiliario {lead_data['project']} en México. "
                f"Incluye detalles sobre la zona, accesibilidad, servicios cercanos y atractivos turísticos."
            )
            try:
                location_response = grok_client.chat.completions.create(
                    model="grok-beta",
                    messages=[
                        {"role": "system", "content": "Eres Grok, una IA creada por xAI para proporcionar respuestas precisas y útiles basadas en información en tiempo real."},
                        {"role": "user", "content": location_prompt}
                    ]
                )
                location_details = location_response.choices[0].message.content.strip()
                project_info["location"] = location_details
                logger.debug(f"Location details for {lead_data['project']}: {location_details}")
            except Exception as e:
                logger.error(f"Error al buscar información de ubicación con Grok: {str(e)}", exc_info=True)
                project_info["location"] = "México (información adicional no disponible)"

        # Prepare downloadable files message
        downloadable_message = ""
        if lead_data['project'] and lead_data['project'] in downloadable_files:
            files = downloadable_files[lead_data['project']]
            if files:
                downloadable_message = "Puedo proporcionarte los siguientes documentos para este proyecto:\n"
                for file in files:
                    link = downloadable_links.get(lead_data['project'], {}).get(file, "Enlace no disponible")
                    downloadable_message += f"- {file}: {link}\n"

        style = determine_communication_style(lead_data['budget'])
        tone_instruction = {
            "formal": "Responde de manera profesional y educada, como una asesora de ventas experta, con un toque cálido y amable.",
            "amigable": "Usa un tono profesional pero cálido y cercano, sin ser coloquial, mostrando amabilidad.",
            "casual": "Habla de forma profesional y directa, manteniendo amabilidad y un tono acogedor."
        }

        prompt = (
            f"Eres Giselle, una asesora de ventas de FAV Living, una empresa inmobiliaria. "
            f"Tu tono es {tone_instruction[style]}. Tu objetivo es vender propiedades y perfilar al cliente para obtener su nombre, "
            f"presupuesto, horario preferido de contacto y ubicación de interés de forma natural, sin hacer demasiadas preguntas. "
            f"Solo pregunta lo estrictamente necesario, una pregunta a la vez. Responde de manera natural e improvisada, "
            f"como lo haría una vendedora real, utilizando el historial de conversación para mantener el contexto y evitar repetir "
            f"información innecesaria. No uses emoticones ni compartas información personal sobre ti más allá de tu rol en FAV Living. "
            f"Responde de manera concisa, enfocándote en la venta de propiedades y la información relevante.\n\n"
            f"Información del cliente:\n"
            f"- Nombre: {lead_data['name'] or 'Desconocido'}\n"
            f"- Proyecto: {lead_data['project'] or 'No especificado'}\n"
            f"- Presupuesto: {lead_data['budget'] or 'No especificado'}\n"
            f"- Horario: {lead_data['preferred_time'] or 'No especificado'}\n"
            f"- Ubicación: {lead_data['location'] or 'No especificado'}\n\n"
            f"Información del proyecto (si aplica):\n"
            f"- Descripción: {project_info['general']}\n"
            f"- Precios: {project_info['prices']}\n"
            f"- Planes de pago: {project_info['payment_plans']}\n"
            f"- Ubicación del proyecto: {project_info['location']}\n\n"
            f"Archivos descargables disponibles (si aplica):\n"
            f"{downloadable_message}\n"
            f"Historial de conversación:\n"
            f"{conversation_history}\n\n"
            f"Mensaje del cliente: \"{message}\"\n\n"
            f"Responde de forma breve y profesional, enfocándote en la venta de propiedades. Si es la primera vez que interactúas con el cliente, "
            f"preséntate de manera natural como asesora de ventas de FAV Living y ofrece tu ayuda. Si el cliente menciona un proyecto, proporciona "
            f"información relevante del proyecto (incluyendo detalles de la ubicación obtenidos en tiempo real) y pregunta solo un dato faltante del cliente "
            f"(nombre, presupuesto, horario o ubicación) si no lo ha proporcionado antes, revisando el historial para evitar repetir preguntas. If the cliente "
            f"hace una oferta (e.g., \"100000 USD\"), maneja la negociación con un margen del 5-10%. Si dice \"confirmar\" o \"cerrar\", finaliza el trato. "
            f"Si el cliente pregunta por información adicional o documentos (e.g., \"presentación\", \"precios\", \"renders\"), incluye los enlaces a los "
            f"archivos descargables correspondientes, o sugiere enviarlos si es oportuno (por ejemplo, después de confirmar el interés del cliente). "
            f"No uses respuestas predefinidas; improvisa de manera natural desde tu rol como vendedora."
        )

        response = grok_client.chat.completions.create(
            model="grok-beta",
            messages=[
                {"role": "system", "content": "Eres Giselle, una asesora de ventas de FAV Living, utilizando la IA de Grok."},
                {"role": "user", "content": prompt}
            ]
        )
        reply = response.choices[0].message.content.strip()
        logger.debug(f"Respuesta generada por Grok: {reply}")
        return reply
    except Exception as e:
        logger.error(f"Error con Grok API: {str(e)}", exc_info=True)
        return "Lo siento, ocurrió un error. ¿En qué puedo ayudarte?"

# Webhook route for WhatsApp messages
@app.route('/whatsapp', methods=['POST'])
def whatsapp():
    logger.debug("Entered /whatsapp route")
    try:
        logger.debug(f"Request form data: {request.form}")
        logger.debug(f"Request method: {request.method}")
        logger.debug(f"Request headers: {request.headers}")

        incoming_msg = request.values.get('Body', '')
        phone = request.values.get('From', '')

        if not incoming_msg or not phone:
            logger.error("No se encontraron 'Body' o 'From' en la solicitud")
            return "Error: Solicitud incompleta", 400

        logger.info(f"Mensaje recibido de {phone}: {incoming_msg}")

        # Fetch lead by phone
        cursor.execute("SELECT * FROM leads WHERE phone = %s", (phone,))
        lead = cursor.fetchone()

        # Create new lead if not exists
        if not lead:
            cursor.execute(
                "INSERT INTO leads (phone, status, last_contact) VALUES (%s, %s, %s) RETURNING *",
                (phone, 'INTERES NULO', datetime.now().isoformat())
            )
            lead = cursor.fetchone()
            conn.commit()
            logger.info(f"Created new lead for phone {phone}")

        lead_data = {
            'id': lead[0],
            'name': lead[1] or '',
            'phone': lead[2],
            'project': lead[3] or '',
            'budget': lead[4] or '',
            'preferred_time': lead[5] or '',
            'location': lead[6] or '',
            'status': lead[7]
        }

        # Fetch conversation history
        cursor.execute("SELECT message, response FROM interactions WHERE lead_id = %s ORDER BY timestamp DESC LIMIT 5", (lead_data['id'],))
        history = cursor.fetchall()
        conversation_history = "\n".join([f"Cliente: {h[0]}\nGiselle: {h[1]}" for h in history])

        # Detect project dynamically
        cursor.execute("SELECT project_name FROM properties")
        known_projects = [row[0] for row in cursor.fetchall()]
        for project in known_projects:
            if project.lower() in incoming_msg.lower():
                lead_data['project'] = project
                cursor.execute("UPDATE leads SET project = %s WHERE id = %s", (project, lead_data['id']))
                logger.info(f"Detected project {project} for lead {lead_data['id']}")

        # Extract lead data with regex
        if not lead_data['name'] and re.search(r'my name is (.*)', incoming_msg, re.IGNORECASE):
            lead_data['name'] = re.search(r'my name is (.*)', incoming_msg, re.IGNORECASE).group(1)
            logger.debug(f"Extracted name: {lead_data['name']}")
        if not lead_data['budget'] and re.search(r'budget is (.*)', incoming_msg, re.IGNORECASE):
            lead_data['budget'] = re.search(r'budget is (.*)', incoming_msg, re.IGNORECASE).group(1)
            logger.debug(f"Extracted budget: {lead_data['budget']}")
        if not lead_data['preferred_time'] and re.search(r'contact me (.*)', incoming_msg, re.IGNORECASE):
            lead_data['preferred_time'] = re.search(r'contact me (.*)', incoming_msg, re.IGNORECASE).group(1)
            logger.debug(f"Extracted preferred_time: {lead_data['preferred_time']}")
        if not lead_data['location'] and re.search(r'location (.*)', incoming_msg, re.IGNORECASE):
            lead_data['location'] = re.search(r'location (.*)', incoming_msg, re.IGNORECASE).group(1)
            logger.debug(f"Extracted location: {lead_data['location']}")

        # Generate response with Grok
        response = generate_grok_response(incoming_msg, lead_data, conversation_history, downloadable_files)

        # Handle negotiation or closing
        offer_match = re.search(r'\b(\d+)\s*(USD|dólares|dolares)\b', incoming_msg, re.IGNORECASE)
        if offer_match:
            offer = int(offer_match.group(1))
            response = negotiate(lead_data, offer)
            lead_data['status'] = 'INTERES ALTO'
            logger.info(f"Negotiation triggered with offer {offer} USD")
        elif re.search(r'confirm|cerrar', incoming_msg, re.IGNORECASE):
            lead_data['status'] = 'INTERES ALTO'
            response = f"Entendido, {lead_data['name'] or 'cliente'}. Hemos cerrado el trato. Me pondré en contacto para los detalles."
            logger.info("Deal confirmed")

        # Send the message directly using Twilio API
        try:
            # Send "typing" message
            message_writing = client.messages.create(
                from_='whatsapp:+15557684099',
                body="Escribiendo...",
                to=phone
            )
            logger.info(f"Mensaje de 'Escribiendo...' enviado a través de Twilio: SID {message_writing.sid}")

            # Send the actual response
            message = client.messages.create(
                from_='whatsapp:+15557684099',
                body=response,
                to=phone
            )
            logger.info(f"Mensaje enviado a través de Twilio: SID {message.sid}, Estado: {message.status}")

            # Verify message status
            updated_message = client.messages(message.sid).fetch()
            logger.info(f"Estado del mensaje actualizado: {updated_message.status}")
            if updated_message.status == "failed":
                logger.error(f"Error al enviar mensaje: {updated_message.error_code} - {updated_message.error_message}")
        except Exception as e:
            logger.error(f"Error al enviar mensaje con Twilio: {str(e)}", exc_info=True)
            response = "Lo siento, ocurrió un error al enviar el mensaje. ¿En qué puedo ayudarte?"

        # Update lead
        cursor.execute(
            """
            UPDATE leads SET
                name = %s,
                project = %s,
                budget = %s,
                preferred_time = %s,
                location = %s,
                status = %s,
                last_contact = %s
            WHERE id = %s
            """,
            (
                lead_data['name'],
                lead_data['project'],
                lead_data['budget'],
                lead_data['preferred_time'],
                lead_data['location'],
                lead_data['status'],
                datetime.now().isoformat(),
                lead_data['id']
            )
        )

        # Save interaction
        cursor.execute(
            """
            INSERT INTO interactions (lead_id, message, response, timestamp)
            VALUES (%s, %s, %s, %s)
            """,
            (lead_data['id'], incoming_msg, response, datetime.now().isoformat())
        )
        conn.commit()

        # Export leads to Excel
        try:
            cursor.execute("SELECT * FROM leads")
            df = pd.DataFrame(cursor.fetchall(), columns=['id', 'name', 'phone', 'project', 'budget', 'preferred_time', 'location', 'status', 'last_contact'])
            df.to_excel('clients.xlsx', index=False)
            logger.info("Leads exported to clients.xlsx")
        except Exception as e:
            logger.error(f"Error exporting leads to Excel: {str(e)}", exc_info=True)

        return "Mensaje enviado"
    except Exception as e:
        logger.error(f"Error inesperado en /whatsapp: {str(e)}", exc_info=True)
        return "Error interno del servidor", 500

# Root route for simple testing
@app.route('/', methods=['GET'])
def root():
    logger.debug("Solicitud GET recibida en /")
    return "Servidor Flask está funcionando!"

# Test endpoint to verify the server is running
@app.route('/test', methods=['GET'])
def test():
    logger.debug("Solicitud GET recibida en /test")
    return "Servidor Flask está funcionando correctamente!"

# Generate report
def generate_report():
    try:
        cursor.execute("SELECT status, COUNT(*) FROM leads GROUP BY status")
        report = cursor.fetchall()
        report_text = "Reporte de Avances:\n"
        for status, count in report:
            report_text += f"{status}: {count} leads\n"
        logger.info(report_text)
        return report_text
    except Exception as e:
        logger.error(f"Error generating report: {str(e)}", exc_info=True)
        return "Error generating report"

# Download projects from Cloud Storage on startup
download_projects_from_storage()

# Load projects and downloadable files
downloadable_files = load_projects_from_folder()

# Generate initial report
logger.info(generate_report())

# Get the dynamic port from Cloud Run (default to 8080)
port = int(os.getenv("PORT", 8080))

# Debug: Print environment variables to confirm they are being read
logger.info(f"Puerto del servidor: {port}")
logger.info("Nota: Cloud Run asignará una URL pública al deploy (por ejemplo, https://giselle-bot-abc123-uc.a.run.app)")
logger.info("Configura el webhook en Twilio con la URL pública del deployment + /whatsapp")

if __name__ == '__main__':
    logger.info("Iniciando servidor Flask...")
    app.run(host='0.0.0.0', port=port, debug=True)
    logger.info(f"Servidor Flask iniciado en el puerto {port}.")

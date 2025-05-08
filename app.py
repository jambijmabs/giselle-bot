
# app.py
import re
import pandas as pd
import psycopg2
from twilio.rest import Client
from flask import Flask, request
from datetime import datetime
import os
import logging
from openai import OpenAI
from google.cloud import storage

# Suprimir advertencias de pdfminer
logging.getLogger("pdfminer").setLevel(logging.ERROR)

# Configuración de logging
logging.basicConfig(filename='giselle_activity.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# Configuración de Flask
app = Flask(__name__)

# Configuración de Twilio (usar variables de entorno para mayor seguridad)
# Nota: Estas credenciales deben configurarse como variables de entorno en Cloud Run
account_sid = os.getenv('TWILIO_ACCOUNT_SID')
auth_token = os.getenv('TWILIO_AUTH_TOKEN')
client = Client(account_sid, auth_token)

# Configuración de Grok (usar variables de entorno para la API key)
# Nota: Esta clave debe configurarse como variable de entorno en Cloud Run
grok_client = OpenAI(
    api_key=os.getenv('GROK_API_KEY'),
    base_url='https://api.x.ai/v1'
)

# Configuración de PostgreSQL (Cloud SQL)
conn = psycopg2.connect(
    dbname='giselle',
    user='postgres',
    password=os.getenv('DB_PASSWORD', 'Tulipan8'),
    host=os.getenv('DB_HOST', '34.174.23.67'),
    port='5432'
)
cursor = conn.cursor()

# Crear tablas
cursor.execute("CREATE TABLE IF NOT EXISTS leads (id SERIAL PRIMARY KEY, name TEXT, phone TEXT, project TEXT, budget REAL, preferred_time TEXT, location TEXT, status TEXT, last_contact TEXT)")
cursor.execute("CREATE TABLE IF NOT EXISTS properties (project_name TEXT PRIMARY KEY, general TEXT, common_areas TEXT, typologies TEXT, construction_specs TEXT, delivery_specs TEXT, prices TEXT, payment_plans TEXT, last_updated TEXT)")
cursor.execute("CREATE TABLE IF NOT EXISTS interactions (id SERIAL PRIMARY KEY, lead_id INTEGER, message TEXT, response TEXT, timestamp TEXT, FOREIGN KEY (lead_id) REFERENCES leads(id))")
conn.commit()

# Diccionario de enlaces a los archivos en DESCARGABLES (inicialmente vacío, se llenará dinámicamente)
downloadable_links = {}

# Endpoint raíz para pruebas simples
@app.route('/', methods=['GET'])
def root():
    print("✅ Solicitud GET recibida en /")
    return "Servidor Flask está funcionando!"

# Endpoint de prueba para verificar que el servidor está funcionando
@app.route('/test', methods=['GET'])
def test():
    print("✅ Solicitud GET recibida en /test")
    return "Servidor Flask está funcionando correctamente!"

# Función para descargar archivos desde Cloud Storage
def download_projects_from_storage(bucket_name='giselle-projects', base_path='PROYECTOS'):
    if not os.path.exists(base_path):
        os.makedirs(base_path)

    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blobs = bucket.list_blobs(prefix=base_path)

    for blob in blobs:
        local_path = blob.name
        if not os.path.exists(os.path.dirname(local_path)):
            os.makedirs(os.path.dirname(local_path))
        blob.download_to_filename(local_path)
        print(f"✅ Descargado archivo desde Cloud Storage: {local_path}")

# Función para extraer texto de archivos .txt
def extract_text_from_txt(txt_path):
    try:
        with open(txt_path, 'r', encoding='utf-8') as file:
            text = file.read()
        print(f"✅ Archivo de texto {txt_path} leído correctamente.")
        return text
    except Exception as e:
        logging.error(f"Error al leer archivo de texto {txt_path}: {str(e)}")
        print(f"❌ Error al leer archivo de texto {txt_path}: {str(e)}")
        return ""

# Procesar texto (solo archivos de texto) con más flexibilidad
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

        # Identificar secciones
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

# Cargar proyectos desde carpeta (detectar proyectos dinámicamente)
def load_projects_from_folder(base_path='PROYECTOS'):
    downloadable_files = {}

    if not os.path.exists(base_path):
        os.makedirs(base_path)
        print(f"❌ Carpeta {base_path} creada, pero no hay proyectos.")
        logging.warning(f"Carpeta {base_path} creada, pero no hay proyectos.")
        return downloadable_files

    # Detectar proyectos dinámicamente, ignorando carpetas no deseadas
    projects = [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d)) and not d.startswith('.') and d != 'DESCARGABLES']
    if not projects:
        print(f"❌ No se encontraron proyectos en {base_path}.")
        return downloadable_files

    print(f"✅ Proyectos detectados: {', '.join(projects)}")

    # Inicializar downloadable_links para cada proyecto detectado
    for project in projects:
        downloadable_links[project] = {}

    # Cargar información del proyecto (solo archivos .txt fuera de DESCARGABLES)
    for project in projects:
        project_path = os.path.join(base_path, project)
        file_count = 0
        txt_files = [f for f in os.listdir(project_path) if f.endswith('.txt') and os.path.isfile(os.path.join(project_path, f))]

        if not txt_files:
            print(f"❌ No se encontraron archivos TXT para el proyecto {project}.")
            continue

        for file in txt_files:
            file_path = os.path.join(project_path, file)
            print(f"✅ Procesando archivo de texto para {project}: {file_path}")
            text = extract_text_from_txt(file_path)
            if text:
                project_info = process_project_text(text)
                cursor.execute(
                    "INSERT INTO properties (project_name, general, common_areas, typologies, construction_specs, delivery_specs, prices, payment_plans, last_updated) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (project_name) DO UPDATE SET "
                    "general = EXCLUDED.general, "
                    "common_areas = EXCLUDED.common_areas, "
                    "typologies = EXCLUDED.typologies, "
                    "construction_specs = EXCLUDED.construction_specs, "
                    "delivery_specs = EXCLUDED.delivery_specs, "
                    "prices = EXCLUDED.prices, "
                    "payment_plans = EXCLUDED.payment_plans, "
                    "last_updated = EXCLUDED.last_updated",
                    (project, project_info["general"], project_info["common_areas"],
                     project_info["typologies"], project_info["construction_specs"],
                     project_info["delivery_specs"], project_info["prices"],
                     project_info["payment_plans"], datetime.now().isoformat())
                )
                conn.commit()
                logging.info(f"Propiedad {project} cargada desde {file_path}")
                print(f"✅ Proyecto {project} procesado correctamente desde {file_path}.")
                file_count += 1

        if file_count > 0:
            print(f"✅ Proyecto {project} procesado correctamente. {file_count} archivo(s) cargado(s).")
        else:
            print(f"❌ No se encontraron archivos TXT válidos para el proyecto {project}.")

        # Procesar la carpeta DESCARGABLES
        downloadable_path = os.path.join(project_path, 'DESCARGABLES')
        downloadable_files[project] = []
        if os.path.exists(downloadable_path):
            downloadable_count = 0
            for file in os.listdir(downloadable_path):
                if file.endswith(('.pdf', '.jpg', '.jpeg', '.png')):
                    downloadable_files[project].append(file)
                    downloadable_count += 1
            if downloadable_count > 0:
                print(f"✅ Carpeta DESCARGABLES del proyecto {project} procesada correctamente. {downloadable_count} archivo(s) encontrado(s).")
            else:
                print(f"❌ Carpeta DESCARGABLES del proyecto {project} está vacía o no contiene archivos válidos.")
        else:
            print(f"❌ Carpeta DESCARGABLES no encontrada para el proyecto {project}.")

    return downloadable_files

# Estilo de comunicación
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

# Negociación
def negotiate(lead, offer):
    cursor.execute("SELECT prices FROM properties WHERE project_name = %s", (lead['project'],))
    prices = cursor.fetchone()
    min_price = 100000  # Valor por defecto
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

# Generar respuesta con Grok, con un enfoque más natural e improvisado
def generate_grok_response(message, lead_data, conversation_history, downloadable_files):
    project_info = {"general": "Proyecto no especificado", "prices": "", "payment_plans": "", "location": "Ubicación no especificada"}
    if lead_data['project']:
        cursor.execute("SELECT general, prices, payment_plans FROM properties WHERE project_name = %s", (lead_data['project'],))
        project_info = cursor.fetchone()
        if project_info:
            project_info = {"general": project_info[0], "prices": project_info[1], "payment_plans": project_info[2], "location": "México"}
        else:
            project_info = {"general": "Información no disponible para este proyecto", "prices": "", "payment_plans": "", "location": "Ubicación no especificada"}

        # Búsqueda en tiempo real para obtener más información sobre la ubicación del proyecto
        location_prompt = f"Proporciona información sobre la ubicación del proyecto inmobiliario {lead_data['project']} en México. Incluye detalles sobre la zona, accesibilidad, servicios cercanos y atractivos turísticos."
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
        except Exception as e:
            logging.error(f"Error al buscar información de ubicación con Grok: {str(e)}")
            project_info["location"] = "México (información adicional no disponible)"

    # Preparar información sobre archivos descargables
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

    try:
        response = grok_client.chat.completions.create(
            model="grok-beta",
            messages=[
                {"role": "system", "content": "Eres Giselle, una asesora de ventas de FAV Living, utilizando la IA de Grok."},
                {"role": "user", "content": prompt}
            ]
        )
        reply = response.choices[0].message.content.strip()

        # Depuración: Imprimir la respuesta generada
        print(f"✅ Respuesta generada por Grok: {reply}")

        return reply
    except Exception as e:
        logging.error(f"Error con Grok API: {str(e)}")
        error_reply = "Lo siento, ocurrió un error. ¿En qué puedo ayudarte?"
        print(f"❌ Error al generar respuesta: {str(e)}")
        return error_reply

# Webhook para mensajes de WhatsApp
@app.route('/whatsapp', methods=['POST'])
def whatsapp():
    try:
        print("✅ Solicitud POST recibida en /whatsapp")
        print(f"✅ Datos de la solicitud: {request.values}")
        
        # Depuración adicional: Imprimir el cuerpo completo de la solicitud
        print(f"✅ Cuerpo completo de la solicitud: {request.form}")
        print(f"✅ Método HTTP: {request.method}")
        print(f"✅ Headers de la solicitud: {request.headers}")

        incoming_msg = request.values.get('Body', '')
        phone = request.values.get('From', '')
        
        # Depuración: Imprimir mensaje recibido
        if not incoming_msg or not phone:
            print("❌ Error: No se encontraron 'Body' o 'From' en la solicitud")
            return "Error: Solicitud incompleta", 400
        
        print(f"✅ Mensaje recibido de {phone}: {incoming_msg}")

        # Buscar lead por teléfono
        lead = cursor.execute("SELECT * FROM leads WHERE phone = %s", (phone,)).fetchone()

        # Crear nuevo lead si no existe
        if not lead:
            cursor.execute(
                "INSERT INTO leads (phone, status, last_contact) VALUES (%s, %s, %s) RETURNING *",
                (phone, 'INTERES NULO', datetime.now().isoformat())
            )
            lead = cursor.fetchone()
            conn.commit()

        lead_data = {
            'id': lead[0], 'name': lead[1] or '', 'phone': lead[2], 'project': lead[3] or '',
            'budget': lead[4] or '', 'preferred_time': lead[5] or '', 'location': lead[6] or '',
            'status': lead[7]
        }

        # Obtener historial de conversación
        cursor.execute("SELECT message, response FROM interactions WHERE lead_id = %s ORDER BY timestamp DESC LIMIT 5", (lead_data['id'],))
        history = cursor.fetchall()
        conversation_history = "\n".join([f"Cliente: {h[0]}\nGiselle: {h[1]}" for h in history])

        # Detectar proyecto dinámicamente
        cursor.execute("SELECT project_name FROM properties")
        known_projects = [row[0] for row in cursor.fetchall()]
        for project in known_projects:
            if project.lower() in incoming_msg.lower():
                lead_data['project'] = project
                cursor.execute("UPDATE leads SET project = %s WHERE id = %s", (project, lead_data['id']))

        # Extraer datos con regex
        if not lead_data['name'] and re.search(r'my name is (.*)', incoming_msg, re.IGNORECASE):
            lead_data['name'] = re.search(r'my name is (.*)', incoming_msg).group(1)
        if not lead_data['budget'] and re.search(r'budget is (.*)', incoming_msg, re.IGNORECASE):
            lead_data['budget'] = re.search(r'budget is (.*)', incoming_msg).group(1)
        if not lead_data['preferred_time'] and re.search(r'contact me (.*)', incoming_msg, re.IGNORECASE):
            lead_data['preferred_time'] = re.search(r'contact me (.*)', incoming_msg).group(1)
        if not lead_data['location'] and re.search(r'location (.*)', incoming_msg, re.IGNORECASE):
            lead_data['location'] = re.search(r'location (.*)', incoming_msg).group(1)

        # Generar respuesta con Grok
        response = generate_grok_response(incoming_msg, lead_data, conversation_history, downloadable_files)

        # Manejar negociación o cierre
        offer_match = re.search(r'(\d+)\s*(USD|dólares|dolares)', incoming_msg, re.IGNORECASE)
        if offer_match:
            offer = int(offer_match.group(1))
            response = negotiate(lead_data, offer)
            lead_data['status'] = 'INTERES ALTO'
        elif re.search(r'confirm|cerrar', incoming_msg, re.IGNORECASE):
            lead_data['status'] = 'INTERES ALTO'
            response = f"Entendido, {lead_data['name'] or 'cliente'}. Hemos cerrado el trato. Me pondré en contacto para los detalles."

        # Enviar el mensaje directamente usando la API de Twilio
        try:
            # Simular escritura
            message_writing = client.messages.create(
                from_='whatsapp:+15557684099',
                body="Escribiendo...",
                to=phone
            )
            print(f"✅ Mensaje de 'Escribiendo...' enviado a través de Twilio: SID {message_writing.sid}")

            # Enviar la respuesta
            message = client.messages.create(
                from_='whatsapp:+15557684099',
                body=response,
                to=phone
            )
            print(f"✅ Mensaje enviado a través de Twilio: SID {message.sid}, Estado: {message.status}")

            # Verificar el estado del mensaje
            updated_message = client.messages(message.sid).fetch()
            print(f"✅ Estado del mensaje actualizado: {updated_message.status}")
            if updated_message.status == "failed":
                print(f"❌ Error al enviar mensaje: {updated_message.error_code} - {updated_message.error_message}")
        except Exception as e:
            print(f"❌ Error al enviar mensaje con Twilio: {str(e)}")
            response = "Lo siento, ocurrió un error al enviar el mensaje. ¿En qué puedo ayudarte?"

        # Actualizar lead
        cursor.execute(
            "UPDATE leads SET name = %s, project = %s, budget = %s, preferred_time = %s, location = %s, status = %s, last_contact = %s WHERE id = %s",
            (lead_data['name'], lead_data['project'], lead_data['budget'], lead_data['preferred_time'],
             lead_data['location'], lead_data['status'], datetime.now().isoformat(), lead_data['id'])
        )
        
        # Guardar interacción
        cursor.execute(
            "INSERT INTO interactions (lead_id, message, response, timestamp) VALUES (%s, %s, %s, %s)",
            (lead_data['id'], incoming_msg, response, datetime.now().isoformat())
        )
        conn.commit()

        # Exportar a Excel
        cursor.execute("SELECT * FROM leads")
        df = pd.DataFrame(cursor.fetchall(), columns=['id', 'name', 'phone', 'project', 'budget', 'preferred_time', 'location', 'status', 'last_contact'])
        df.to_excel('clients.xlsx', index=False)

        return "Mensaje enviado"
    except Exception as e:
        print(f"❌ Error inesperado en /whatsapp: {str(e)}")
        return "Error interno del servidor", 500

# Generar reporte
def generate_report():
    cursor.execute("SELECT status, COUNT(*) FROM leads GROUP BY status")
    report = cursor.fetchall()
    report_text = "Reporte de Avances:\n"
    for status, count in report:
        report_text += f"{status}: {count} leads\n"
    logging.info(report_text)
    return report_text

# Descargar proyectos desde Cloud Storage al iniciar
download_projects_from_storage()

# Cargar proyectos y archivos descargables
downloadable_files = load_projects_from_folder()

# Generar reporte inicial
print(generate_report())

# Obtener el puerto dinámico de Cloud Run (o usar 8080 por defecto)
port = int(os.getenv("PORT", 8080))

# Depuración: Imprimir variables de entorno para confirmar que se están leyendo
print(f"✅ Puerto del servidor: {port}")

# Imprimir la URL pública (Cloud Run asignará una URL al deploy)
print("✅ Nota: Cloud Run asignará una URL pública al deploy (por ejemplo, https://giselle-bot-abc123-uc.a.run.app)")
print("✅ Configura el webhook en Twilio con la URL pública del deployment + /whatsapp")

if __name__ == '__main__':
    print("✅ Iniciando servidor Flask...")
    app.run(host='0.0.0.0', port=port, debug=False)
    print(f"✅ Servidor Flask iniciado en el puerto {port}.")

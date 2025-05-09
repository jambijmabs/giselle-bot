import os
import logging
from flask import Flask

# Configure logging to output only to stdout/stderr (Cloud Run captures these)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Log to stdout/stderr for Cloud Run
    ]
)

# Initialize Flask app
app = Flask(__name__)

# Configure logger
logger = logging.getLogger(__name__)

# Log startup
logger.info("Starting minimal GISELLE service for debugging...")

# Health check endpoint to verify the service is running
@app.route('/health', methods=['GET'])
def health():
    return "Service is running", 200

# Get the dynamic port from Cloud Run (default to 8080)
port = int(os.getenv("PORT", 8080))

# Debug: Print environment variables to confirm they are being read
logger.info(f"Puerto del servidor: {port}")

if __name__ == '__main__':
    logger.info("Iniciando servidor Flask...")
    app.run(host='0.0.0.0', port=port, debug=True)
    logger.info(f"Servidor Flask iniciado en el puerto {port}.")

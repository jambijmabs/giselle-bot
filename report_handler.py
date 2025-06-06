import logging
import os
import pandas as pd
from google.cloud import storage
import bot_config

logger = logging.getLogger(__name__)

def generate_detailed_report(conversation_state, filter_stage=None, filter_interest=None):
    report = ["Reporte Detallado de Clientes Interesados:"]
    for client_phone, state in conversation_state.items():
        if state.get('is_gerente', False) or state.get('no_interest', False):
            continue

        client_name = state.get('client_name', 'Desconocido')
        project = state.get('last_mentioned_project', 'No especificado')
        budget = state.get('client_budget', 'No especificado')
        needs = state.get('needs', 'No especificadas')
        stage = state.get('stage', 'Prospección')
        interest_level = state.get('interest_level', 0)
        last_contact = state.get('last_contact', 'N/A')
        last_messages = state.get('history', [])[-3:] if state.get('history') else ['Sin mensajes']
        zoom_scheduled = state.get('zoom_scheduled', False)
        zoom_details = state.get('zoom_details', {})

        if filter_stage and stage != filter_stage:
            continue
        if filter_interest is not None and interest_level != filter_interest:
            continue

        client_info = [
            f"Cliente: {client_phone}",
            f"Nombre: {client_name}",
            f"Proyecto: {project}",
            f"Presupuesto: {budget}",
            f"Necesidades: {needs}",
            f"Etapa: {stage}",
            f"Nivel de Interés: {interest_level}/10",
            f"Último Contacto: {last_contact}",
            f"Reunión Zoom Agendada: {'Sí' if zoom_scheduled else 'No'}"
        ]
        if zoom_scheduled and zoom_details:
            client_info.append(f"Detalles de Zoom: {zoom_details.get('day')} a las {zoom_details.get('time')}")
        client_info.append("Últimos Mensajes:")
        client_info.extend([f"- {msg}" for msg in last_messages])
        report.extend(client_info)
        report.append("---")

    if len(report) == 1:
        report.append("No hay clientes que coincidan con los criterios especificados.")
    return report

def update_leads_excel(conversation_state):
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bot_config.GCS_BUCKET_NAME)
        blob = bucket.blob(bot_config.LEADS_EXCEL_PATH)

        temp_excel_path = f"/tmp/{bot_config.LEADS_EXCEL_PATH}"
        try:
            blob.download_to_filename(temp_excel_path)
            df = pd.read_excel(temp_excel_path)
        except Exception as e:
            logger.warning(f"No existing Excel file found at {bot_config.LEADS_EXCEL_PATH}, creating new file: {str(e)}")
            df = pd.DataFrame(columns=[
                "FECHA DE INGRESO", "NOMBRE", "TELEFONO", "CORREO",
                "PROYECTO DE INTERES", "FECHA DE ULTIMO CONTACTO",
                "NIVEL DE INTERES", "ESTATUS", "ZOOM AGENDADA", "DETALLES ZOOM"
            ])

        new_rows = []
        for client_phone, state in conversation_state.items():
            if state.get('is_gerente', False) or state.get('no_interest', False):
                continue

            client_name = state.get('client_name', 'Desconocido')
            project = state.get('last_mentioned_project', 'No especificado')
            last_contact = state.get('last_contact', 'N/A')
            interest_level = state.get('interest_level', 0)
            stage = state.get('stage', 'Prospección')
            first_contact = state.get('first_contact', last_contact)
            zoom_scheduled = state.get('zoom_scheduled', False)
            zoom_details = state.get('zoom_details', {})
            zoom_details_text = f"{zoom_details.get('day')} a las {zoom_details.get('time')}" if zoom_scheduled and zoom_details else "N/A"

            if client_phone in df['TELEFONO'].values:
                df.loc[df['TELEFONO'] == client_phone, [
                    "FECHA DE ULTIMO CONTACTO", "NIVEL DE INTERES", "ESTATUS", "PROYECTO DE INTERES",
                    "ZOOM AGENDADA", "DETALLES ZOOM"
                ]] = [last_contact, interest_level, stage, project, "Sí" if zoom_scheduled else "No", zoom_details_text]
            else:
                new_row = {
                    "FECHA DE INGRESO": first_contact,
                    "NOMBRE": client_name,
                    "TELEFONO": client_phone,
                    "CORREO": "N/A",
                    "PROYECTO DE INTERES": project,
                    "FECHA DE ULTIMO CONTACTO": last_contact,
                    "NIVEL DE INTERES": interest_level,
                    "ESTATUS": stage,
                    "ZOOM AGENDADA": "Sí" if zoom_scheduled else "No",
                    "DETALLES ZOOM": zoom_details_text
                }
                new_rows.append(new_row)

        if new_rows:
            new_df = pd.DataFrame(new_rows)
            df = pd.concat([df, new_df], ignore_index=True)

        df.to_excel(temp_excel_path, index=False)
        blob.upload_from_filename(temp_excel_path)
        logger.info(f"Updated leads Excel file at {bot_config.LEADS_EXCEL_PATH}")

        os.remove(temp_excel_path)

    except Exception as e:
        logger.error(f"Failed to update leads Excel file: {str(e)}")

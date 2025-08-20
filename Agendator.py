import os
import imaplib
import email
from email.header import decode_header
import requests
import time
import json
import logging
from datetime import datetime, timedelta 
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build


# --- Configuração de Logs ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("agendador.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# --- Carregar Variáveis de Ambiente do arquivo .env ---
load_dotenv()

# --- Configurações ---
# E-mail
IMAP_HOST = 'imap.gmail.com'
EMAIL_USER = os.getenv('EMAIL_USER')
EMAIL_PASS = os.getenv('EMAIL_PASS')

# Gemini API
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"

# --- CORREÇÃO APLICADA AQUI ---
# As chaves literais {} no exemplo de JSON foram duplicadas para {{}} para escapar delas no método .format()
GEMINI_PROMPT_TEMPLATE = (
    "Abaixo está o conteúdo de um e-mail. Verifique se há possíveis reuniões, eventos, tarefas, entregas ou trabalhos que possam ser agendados. Somente considere se houver um horário e/ou dia especificado."
    "Se houver, responda SOMENTE com um objeto JSON contendo uma lista de eventos. Cada evento deve ter 'start_datetime' (formato 'YYYY-MM-DDTHH:MM:SS-03:00') e 'summary' (descrição). "
    "Se não houver eventos, responda com um JSON com uma lista vazia: {{\"eventos\": []}}. "
    "Considere a data de hoje como: " + datetime.now().strftime('%Y-%m-%d') + ". Segue o e-mail:\n\n"
    "De: {de}\nPara: {para}\nAssunto: {assunto}\n\nConteúdo:\n{conteudo}"
)

# Google Calendar API
GOOGLE_CALENDAR_ID = os.getenv('GOOGLE_CALENDAR_ID')
CREDENTIALS_FILE = 'credentials.json' 
CALENDAR_SCOPES = ['https://www.googleapis.com/auth/calendar']
 

# --- Módulo de E-mail ---
def fetch_emails():
    """Busca e-mails não lidos das últimas 24 horas e os marca como lidos."""
    try:
        logging.info("Conectando ao servidor IMAP...")
        mail = imaplib.IMAP4_SSL(IMAP_HOST)
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select('inbox', readonly=False)

        date_since = (datetime.now() - timedelta(days=1))
        date_str = date_since.strftime("%d-%b-%Y")
        search_criteria = f'(UNSEEN SINCE "{date_str}")'
        
        status, messages = mail.search(None, search_criteria)

        if status != 'OK' or not messages[0]:
            logging.info("Nenhum e-mail novo encontrado nas últimas 24 horas.")
            mail.logout()
            return []

        email_ids = messages[0].split()
        logging.info(f"Encontrado(s) {len(email_ids)} novo(s) e-mail(s).")
        
        fetched_emails = []
        for email_id in email_ids:
            status, msg_data = mail.fetch(email_id, '(RFC822)')
            if status == 'OK':
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        
                        subject, encoding = decode_header(msg["Subject"])[0]
                        if isinstance(subject, bytes):
                            subject = subject.decode(encoding if encoding else "utf-8")

                        from_ = msg.get("From")
                        to_ = msg.get("To")
                        
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                content_type = part.get_content_type()
                                if content_type == "text/plain":
                                    try:
                                        body = part.get_payload(decode=True).decode()
                                        break
                                    except:
                                        continue
                        else:
                            try:
                                body = msg.get_payload(decode=True).decode()
                            except:
                                body = ""
                        
                        fetched_emails.append({
                            "from": from_,
                            "to": to_,
                            "subject": subject,
                            "body": body.strip()
                        })
                
                mail.store(email_id, '+FLAGS', '\\Seen')

        mail.logout()
        return fetched_emails
    except Exception as e:
        logging.error(f"Erro ao buscar e-mails: {e}")
        return []


# --- Módulo de Processamento com IA (Gemini) ---
def get_events_from_email(email_data):
    """Envia o conteúdo do e-mail para a API Gemini e extrai eventos."""
    
    de_str = str(email_data.get('from', ''))
    para_str = str(email_data.get('to', ''))
    assunto_str = str(email_data.get('subject', ''))
    conteudo_str = str(email_data.get('body', ''))

    prompt = GEMINI_PROMPT_TEMPLATE.format(
        de=de_str,
        para=para_str,
        assunto=assunto_str,
        conteudo=conteudo_str
    )
    
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {"Content-Type": "application/json"}

    raw_response = ""
    try:
        logging.info(f"Enviando e-mail (Assunto: '{assunto_str}') para a API Gemini.")
        response = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=20)
        response.raise_for_status()

        raw_response = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        logging.info(f"Resposta da IA: {raw_response}")

        clean_json_str = raw_response.strip().replace('```json', '').replace('```', '')
        
        if not clean_json_str:
            logging.warning("Resposta da IA estava vazia ou inválida.")
            return []

        event_data = json.loads(clean_json_str)
        
        return event_data.get("eventos", [])

    except requests.RequestException as e:
        logging.error(f"Erro na requisição para Gemini: {e}")
    except (KeyError, IndexError):
        logging.error(f"Resposta da API Gemini com estrutura inesperada. Resposta: '{raw_response}'")
    except json.JSONDecodeError:
        logging.error(f"Não foi possível decodificar o JSON da resposta da API. Resposta: '{raw_response}'")
    
    return []

# --- Módulo do Google Calendar ---
def create_calendar_event(event_info):
    """Cria um evento no Google Calendar."""
    try:
        logging.info(f"Criando evento no calendário: '{event_info['summary']}'")
        creds = service_account.Credentials.from_service_account_file(
            CREDENTIALS_FILE, scopes=CALENDAR_SCOPES
        )
        service = build('calendar', 'v3', credentials=creds)

        event = {
            'summary': event_info['summary'],
            'location': 'Remoto',
            'description': event_info['summary'],
            'start': {
                'dateTime': event_info['start_datetime'],
                'timeZone': 'America/Sao_Paulo',
            },
            'end': {
                'dateTime': event_info['start_datetime'],
                'timeZone': 'America/Sao_Paulo',
            },
        }

        created_event = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        logging.info(f"Evento criado com sucesso! Link: {created_event.get('htmlLink')}")
        return True
    except Exception as e:
        logging.error(f"Erro ao criar evento no Google Calendar: {e}")
        return False

# --- Loop Principal ---
def main_loop():
    """Loop principal que orquestra o processo."""
    logging.info("🚀 Agendador Inteligente iniciado. Pressione CTRL+C para sair.")
    while True:
        emails = fetch_emails()
        if emails:
            for email_data in emails:
                events = get_events_from_email(email_data)
                if events:
                    for event in events:
                        if 'start_datetime' in event and 'summary' in event:
                            create_calendar_event(event)
                        else:
                            logging.warning(f"Evento malformado recebido da IA: {event}")
        
        logging.info("Aguardando 60 segundos para a próxima verificação...")
        time.sleep(60)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logging.info("👋 Agendador Inteligente encerrado.")

import json
import os
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal

import boto3
import unicodedata
import uuid
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource('dynamodb')
s3_client = boto3.client('s3')
textract_client = boto3.client('textract')
lambda_client = boto3.client('lambda')

notifications_table = dynamodb.Table(os.environ['NOTIFICATIONS_TABLE'])
validations_table = dynamodb.Table(os.environ['VALIDATIONS_TABLE'])

# Constantes y Secretos
SCREENSHOTS_BUCKET = os.environ['SCREENSHOTS_BUCKET']
LAMBDA_PREFIX = os.environ.get('AWS_LAMBDA_FUNCTION_NAME', '').rsplit('-', 1)[0]

WHATSAPP_TOKEN = os.environ.get('WHATSAPP_TOKEN')

ODOO_CALLBACK_URL = os.environ.get('ODOO_CALLBACK_URL')
ODOO_CALLBACK_TOKEN = os.environ.get('ODOO_CALLBACK_TOKEN')


def decimal_to_float(obj):
    """Helper para serializar objetos Decimal de DynamoDB a JSON estándar."""
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError


def parse_decimal(value, field_name='value'):
    if value is None or value == '':
        raise ValueError(f"{field_name} is required")
    try:
        return Decimal(str(value))
    except Exception:
        raise ValueError(f"{field_name} must be numeric")


# --- 1. ENTRY POINT DESDE ODOO ---

def validate_from_odoo(event, context):
    """Entry point para validaciones desde Odoo.

    Recibe JSON: {media_id, nota_venta, phone_number, sender_name, monto_esperado}
    Invoca processWhatsAppImage async y retorna 200 inmediatamente.
    """
    try:
        body = json.loads(event.get('body', '{}'))

        media_id = body.get('media_id')
        nota_venta = body.get('nota_venta')
        phone_number = body.get('phone_number')
        sender_name = body.get('sender_name', 'Cliente')
        monto_esperado_raw = body.get('monto_esperado')

        if not media_id or not nota_venta or not phone_number or monto_esperado_raw is None:
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': 'Campos requeridos: media_id, nota_venta, phone_number, monto_esperado'
                })
            }
        monto_esperado = parse_decimal(monto_esperado_raw, 'monto_esperado')

        print(f"[ValidateFromOdoo] media_id={media_id}, nota_venta={nota_venta}, phone={phone_number}, monto_esperado={monto_esperado}")

        # Invocar processWhatsAppImage async con nota_venta
        lambda_client.invoke(
            FunctionName=f"{LAMBDA_PREFIX}-processWhatsAppImage",
            InvocationType='Event',
            Payload=json.dumps({
                'image_id': media_id,
                'phone_number': phone_number,
                'sender_name': sender_name,
                'nota_venta': nota_venta,
                'monto_esperado': str(monto_esperado),
            })
        )

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Validation started',
                'nota_venta': nota_venta,
                'monto_esperado': str(monto_esperado),
            })
        }

    except ValueError as e:
        return {'statusCode': 400, 'body': json.dumps({'error': str(e)})}
    except Exception as e:
        print(f"[ValidateFromOdoo] Error: {str(e)}")
        traceback.print_exc()
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}


def callback_odoo(nota_venta, status):
    """POST callback a Odoo con resultado de validación.

    Args:
        nota_venta: Número de orden
        status: 'approved' o 'rejected'

    Returns:
        bool: True si el callback fue exitoso
    """
    if not ODOO_CALLBACK_URL:
        print(f"[CallbackOdoo] ODOO_CALLBACK_URL no configurado. nota_venta={nota_venta}, status={status}")
        return False

    try:
        headers = {'Content-Type': 'application/json'}
        if ODOO_CALLBACK_TOKEN:
            headers['X-Callback-Token'] = ODOO_CALLBACK_TOKEN

        payload = {
            'nota_venta': nota_venta,
            'status': status,
        }

        print(f"[CallbackOdoo] POST {ODOO_CALLBACK_URL} -> {json.dumps(payload)}")
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            ODOO_CALLBACK_URL,
            data=data,
            headers=headers,
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            body = response.read().decode('utf-8', errors='replace')
            print(f"[CallbackOdoo] Response: {response.status} - {body[:200]}")
            return response.status == 200

    except Exception as e:
        print(f"[CallbackOdoo] Error: {str(e)}")
        return False


# --- 2. PROCESAMIENTO DE IMAGEN (Descarga y Subida a S3) ---

def process_whatsapp_image(event, context):
    try:
        image_id = event['image_id']
        phone_number = event['phone_number']
        sender_name = event['sender_name']
        message_id = event.get('message_id')
        nota_venta = event.get('nota_venta')
        monto_esperado = parse_decimal(event.get('monto_esperado'), 'monto_esperado')

        print(f"[ImgProcess] Iniciando descarga para ID: {image_id} | nota_venta: {nota_venta} | monto_esperado: {monto_esperado}")

        # Obtener URL de Facebook Graph API
        image_url = get_whatsapp_media_url(image_id)
        if not image_url:
            raise Exception("No se pudo obtener URL de media")

        # Descargar binarios
        image_bytes = download_whatsapp_image(image_url)
        if not image_bytes:
            raise Exception("La descarga de la imagen retornó vacío")

        # Generar ruta S3
        validation_id = str(uuid.uuid4())
        timestamp = int(datetime.now().timestamp() * 1000)
        date_folder = datetime.now().strftime('%Y/%m/%d')
        s3_key = f"zazu2/{date_folder}/{validation_id}.jpg"

        # Metadata S3
        s3_metadata = {
            'validation_id': validation_id,
            'phone_number': phone_number,
            'sender_name': urllib.parse.quote(sender_name),
            'message_id': message_id or '',
        }
        if nota_venta:
            s3_metadata['nota_venta'] = nota_venta
        s3_metadata['monto_esperado'] = str(monto_esperado)

        # Subir a S3 con Metadata (Esto disparará el trigger processScreenshot)
        s3_client.put_object(
            Bucket=SCREENSHOTS_BUCKET,
            Key=s3_key,
            Body=image_bytes,
            ContentType='image/jpeg',
            Metadata=s3_metadata
        )
        print(f"[ImgProcess] Subido a S3: {s3_key}")

        # Invocar OCR async sin depender de notificaciones S3 del bucket compartido.
        lambda_client.invoke(
            FunctionName=f"{LAMBDA_PREFIX}-processScreenshot",
            InvocationType='Event',
            Payload=json.dumps({
                'Records': [
                    {
                        's3': {
                            'bucket': {'name': SCREENSHOTS_BUCKET},
                            'object': {'key': s3_key}
                        }
                    }
                ]
            })
        )

        # Crear registro inicial en DynamoDB
        dynamo_item = {
            'id': validation_id,
            'phone_number': phone_number,
            'sender_name': sender_name,
            's3_key': s3_key,
            's3_bucket': SCREENSHOTS_BUCKET,
            'validation_status': 'processing',
            'created_at': timestamp,
            'updated_at': timestamp,
        }
        if nota_venta:
            dynamo_item['nota_venta'] = nota_venta
        dynamo_item['monto_esperado'] = monto_esperado

        validations_table.put_item(Item=dynamo_item)

        print(f"[ImgProcess] Registro creado: {validation_id}")
        return {'statusCode': 200, 'body': 'Image processed'}

    except Exception as e:
        print(f"[ImgProcess] Error: {str(e)}")
        traceback.print_exc()
        return {'statusCode': 500, 'body': str(e)}


# --- 3. OCR Y EXTRACCIÓN (Trigger S3) ---

def process_screenshot(event, context):
    """Activado automáticamente cuando se crea un archivo en el Bucket S3"""
    try:
        for record in event['Records']:
            bucket = record['s3']['bucket']['name']
            key = record['s3']['object']['key']

            print(f"[S3Trigger] Archivo detectado: s3://{bucket}/{key}")

            # Recuperar Metadata del objeto S3
            obj_metadata = s3_client.head_object(Bucket=bucket, Key=key)
            metadata = obj_metadata.get('Metadata', {})
            validation_id = metadata.get('validation_id')
            phone_number = metadata.get('phone_number')
            raw_name = metadata.get('sender_name', 'Unknown')
            sender_name = urllib.parse.unquote(raw_name)
            nota_venta = metadata.get('nota_venta')
            monto_esperado_meta = metadata.get('monto_esperado')

            if not validation_id:
                print("[S3Trigger] ⚠️ Archivo sin validation_id. Saltando.")
                continue

            # Llamada a Amazon Textract (OCR)
            response = textract_client.detect_document_text(
                Document={'S3Object': {'Bucket': bucket, 'Name': key}}
            )

            # Consolidar texto
            extracted_lines = [block['Text'] for block in response.get('Blocks', []) if block['BlockType'] == 'LINE']
            extracted_text = "\n".join(extracted_lines)

            print(f"[OCR] Texto extraído (inicio): {extracted_text[:150].replace(chr(10), ' ')}...")

            # Parsear datos específicos de Yape
            parsed_data = parse_yape_screenshot(extracted_text)

            # Si no encontramos datos clave, marcamos para revisión manual
            if not parsed_data:
                print(f"[OCR] Fallo: No se detectó monto/código.")
                update_validation_status(validation_id, 'manual_review', error_message='Datos ilegibles por OCR', extracted_text_preview=extracted_text[:500])
                if nota_venta:
                    callback_odoo(nota_venta, 'rejected')
                continue

            print(f"[OCR] Datos parseados: {json.dumps(parsed_data, default=str)}")
            try:
                monto_esperado = parse_decimal(monto_esperado_meta, 'monto_esperado')
            except ValueError as e:
                detail = f"Invalid expected amount metadata: {e}"
                print(f"[OCR] {detail}")
                update_validation_status(validation_id, 'manual_review', error_message=detail)
                if nota_venta:
                    callback_odoo(nota_venta, 'rejected')
                continue
            tolerance = Decimal('0.01')

            # Actualizar validación con datos extraídos
            update_expression = 'SET extracted_amount = :amount, extracted_code = :code, updated_at = :updated'
            expression_values = {
                ':amount': parsed_data['amount'],
                ':code': parsed_data['code'],
                ':updated': int(datetime.now().timestamp() * 1000)
            }

            # Guardar operación si existe
            if parsed_data.get('operation_number'):
                update_expression += ', operation_number = :op'
                expression_values[':op'] = parsed_data['operation_number']

            validations_table.update_item(
                Key={'id': validation_id},
                UpdateExpression=update_expression,
                ExpressionAttributeValues=expression_values
            )

            amount_diff = abs(parsed_data['amount'] - monto_esperado)
            if amount_diff > tolerance:
                mismatch_detail = (
                    f"Amount mismatch. Expected: {monto_esperado}, "
                    f"Extracted: {parsed_data['amount']}, Diff: {amount_diff}"
                )
                print(f"[OCR] {mismatch_detail}")
                update_validation_status(
                    validation_id,
                    'rejected',
                    match_result=mismatch_detail,
                    expected_amount=monto_esperado
                )
                if nota_venta:
                    callback_odoo(nota_venta, 'rejected')
                continue

            # Invocar lógica de validación (Async)
            validate_payload = {
                'validation_id': validation_id,
                'sender_name': sender_name,
                'amount': str(parsed_data['amount']),
                'code': parsed_data['code'],
                'phone_number': phone_number,
                'operation_number': parsed_data.get('operation_number'),
            }
            if nota_venta:
                validate_payload['nota_venta'] = nota_venta

            lambda_client.invoke(
                FunctionName=f"{LAMBDA_PREFIX}-validatePayment",
                InvocationType='Event',
                Payload=json.dumps(validate_payload)
            )

        return {'statusCode': 200, 'body': 'OCR Completed'}

    except Exception as e:
        print(f"[S3Trigger] Error: {str(e)}")
        traceback.print_exc()
        return {'statusCode': 500, 'body': str(e)}


# --- 4. LÓGICA DE NEGOCIO (Validación) ---

def normalize_text(text):
    if not text:
        return ""
    # Normalize unicode characters (NFD separates accents from letters)
    text = unicodedata.normalize('NFD', text)
    # Filter out non-spacing mark characters (accents) and convert to lowercase
    return ''.join(c for c in text if unicodedata.category(c) != 'Mn').lower().strip()


def validate_payment(event, context):
    try:
        # 1. Parse Event Data
        validation_id = event['validation_id']
        ocr_sender_name = event['sender_name']
        amount = Decimal(str(event['amount']))
        code = event['code']
        phone_number = event['phone_number']
        nota_venta = event.get('nota_venta')

        print(f"[Logic] Validating ID: {validation_id} | Amount: {amount} | Code: {code} | Name: {ocr_sender_name} | nota_venta: {nota_venta}")

        # 2. Query DynamoDB (Efficient Search by Code + Amount)
        response = notifications_table.query(
            IndexName='CodeAmountIndex',
            KeyConditionExpression=Key('code').eq(code) & Key('amount').eq(amount)
        )

        candidates = response.get('Items', [])
        print(f"[Logic] Candidates found: {len(candidates)}")

        # CASO 1: No existe notificación con ese código y monto
        if not candidates:
            print("[Logic] ❌ No DB match found for Code/Amount.")
            update_validation_status(validation_id, 'manual_review', match_result='No match found')
            if nota_venta:
                callback_odoo(nota_venta, 'rejected')
            return {'statusCode': 200, 'body': 'No match'}

        matched_notification = None
        is_duplicate = False

        # Pre-calculate OCR name normalization once
        ocr_name_norm = normalize_text(ocr_sender_name)

        # 3. Analyze Candidates
        for notif in candidates:
            notif_status = notif.get('status', 'pending')

            # Support both 'sender_name' and 'name' keys just in case
            raw_bank_name = notif.get('sender_name') or notif.get('name', '')
            bank_name_norm = normalize_text(raw_bank_name)

            print(f"[Logic] Checking candidate {notif['id']} | Status: {notif_status} | Name: {bank_name_norm}")

            # Check Name Match (Robust Fuzzy Logic)
            # Checks for exact match OR if one name is contained inside the other
            is_name_match = (
                    ocr_name_norm == bank_name_norm or
                    (ocr_name_norm and bank_name_norm and (ocr_name_norm in bank_name_norm or bank_name_norm in ocr_name_norm))
            )

            # CASO 2: Ya fue validado (Duplicado)
            if notif_status == 'validated':
                # If the name matches, it's definitely a duplicate usage of the same receipt
                if is_name_match:
                    print(f"[Logic] DUPLICATE detected. Notif ID: {notif['id']}")
                    is_duplicate = True
                    matched_notification = notif
                    break
                    # If name doesn't match, we keep searching (maybe there is another transaction with same amount/code?)

            # CASO 3: Match de Nombre en Pendiente (Success)
            if notif_status == 'pending':
                if is_name_match:
                    print(f"[Logic] ✅ Match confirmed ID: {notif['id']}")
                    matched_notification = notif
                    break

        # 4. Handle Final States

        # A. DUPLICATE FOUND
        if is_duplicate:
            handle_duplicate(validation_id, matched_notification, phone_number, nota_venta=nota_venta)
            return {'statusCode': 200, 'body': 'Duplicate'}

        # B. SUCCESS FOUND
        if matched_notification:
            handle_success(validation_id, matched_notification, phone_number, nota_venta=nota_venta)
            return {'statusCode': 200, 'body': 'Success'}

        # C. NAME MISMATCH (Amount/Code OK, but Name failed)
        # We take the first candidate as the reference for the "Expected" name
        reference_candidate = candidates[0]
        ref_name = reference_candidate.get('sender_name') or reference_candidate.get('name', 'Unknown')

        print(f"[Logic] Name mismatch. OCR: {ocr_name_norm} vs Expected: {ref_name}")

        update_validation_status(
            validation_id,
            'manual_review',
            match_result=f"Name mismatch. Expected: {ref_name}",
            potential_notification_id=reference_candidate['id']
        )
        if nota_venta:
            callback_odoo(nota_venta, 'rejected')
        return {'statusCode': 200, 'body': 'Name mismatch'}

    except Exception as e:
        print(f"[Logic] Error: {str(e)}")
        traceback.print_exc()
        return {'statusCode': 500, 'body': str(e)}


def handle_success(validation_id, notification, phone_number, nota_venta=None):
    timestamp = int(datetime.now().timestamp() * 1000)

    # Marcar notificación como usada
    notifications_table.update_item(
        Key={'id': notification['id']},
        UpdateExpression='SET #status = :status',
        ExpressionAttributeNames={'#status': 'status'},
        ExpressionAttributeValues={':status': 'validated'}
    )

    # Actualizar validación
    validations_table.update_item(
        Key={'id': validation_id},
        UpdateExpression='SET validation_status = :status, matched_notification_id = :nid, updated_at = :upd',
        ExpressionAttributeValues={
            ':status': 'validated',
            ':nid': notification['id'],
            ':upd': timestamp
        }
    )

    if nota_venta:
        callback_odoo(nota_venta, 'approved')


def handle_duplicate(validation_id, notification, phone_number, nota_venta=None):
    timestamp = int(datetime.now().timestamp() * 1000)

    validations_table.update_item(
        Key={'id': validation_id},
        UpdateExpression='SET validation_status = :status, matched_notification_id = :nid, match_result = :res, updated_at = :upd',
        ExpressionAttributeValues={
            ':status': 'duplicate',
            ':nid': notification['id'],
            ':res': 'Payment previously validated',
            ':upd': timestamp
        }
    )

    if nota_venta:
        callback_odoo(nota_venta, 'rejected')


# --- 5. PARSERS Y HELPERS ---

def parse_yape_screenshot(text):
    """Extrae monto y código/operación usando Regex."""
    import re
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    result = {'amount': None, 'code': None, 'operation_number': None}

    # Patrón Monto: "S/ 120.00" o "S/120,00"
    amount_pattern = re.compile(r'^S[/I]\s*(\d+(?:[.,]\d+)?)$')

    for i, line in enumerate(lines):
        # 1. Buscar Monto
        if not result['amount']:
            match = amount_pattern.match(line)
            if match:
                try:
                    val = match.group(1).replace(',', '.')
                    result['amount'] = Decimal(val)
                except:
                    pass

        # 2. Buscar Operación (Prioridad en Yape moderno)
        # Línea que contiene "número de operación" seguida de dígitos
        if 'OPERACIÓN' in line.upper() or 'OPERACION' in line.upper():
            # Buscar en esta línea o la siguiente
            for j in range(i, min(i + 2, len(lines))):
                op_match = re.search(r'(\d{6,})', lines[j])
                if op_match:
                    result['operation_number'] = op_match.group(1)
                    # A veces usamos los últimos dígitos como código si no hay código explícito
                    if not result['code']:
                        result['code'] = result['operation_number'][-3:]  # Fallback

        # 3. Buscar Código de Seguridad (Formato Yape antiguo o Push)
        if 'CÓDIGO' in line.upper() and not result['code']:
            for j in range(i, min(i + 4, len(lines))):
                clean = re.sub(r'\D', '', lines[j])
                if len(clean) == 3:
                    result['code'] = clean
                    break

    # Validación mínima
    if result['amount'] and result['code']:
        return result
    return None


def update_validation_status(validation_id, status, **kwargs):
    update_expr = 'SET validation_status = :status, updated_at = :updated'
    expr_vals = {':status': status, ':updated': int(datetime.now().timestamp() * 1000)}

    for k, v in kwargs.items():
        if v is not None:
            update_expr += f', {k} = :{k}'
            expr_vals[f':{k}'] = v

    validations_table.update_item(Key={'id': validation_id}, UpdateExpression=update_expr, ExpressionAttributeValues=expr_vals)


# --- 6. INTEGRACIÓN WHATSAPP API ---

def get_whatsapp_media_url(media_id):
    if not WHATSAPP_TOKEN: return None
    try:
        url = f"https://graph.facebook.com/v18.0/{media_id}"
        req = urllib.request.Request(
            url,
            headers={'Authorization': f'Bearer {WHATSAPP_TOKEN}'},
            method='GET'
        )
        with urllib.request.urlopen(req, timeout=15) as res:
            payload = json.loads(res.read().decode('utf-8', errors='replace'))
            return payload.get('url')
    except Exception as e:
        print(f"Error media URL: {e}")
        return None


def download_whatsapp_image(url):
    if not WHATSAPP_TOKEN: return None
    try:
        req = urllib.request.Request(
            url,
            headers={'Authorization': f'Bearer {WHATSAPP_TOKEN}'},
            method='GET'
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.read()
    except Exception as e:
        print(f"Error download: {e}")
        return None


# --- 7. API HANDLERS (CRUD & Consultas) ---

def get_validations(event, context):
    try:
        res = validations_table.scan(Limit=50)  # Scan con limite para dashboard
        return {'statusCode': 200, 'body': json.dumps(res.get('Items', []), default=decimal_to_float)}
    except Exception as e:
        return {'statusCode': 500, 'body': str(e)}


def get_validation_by_id(event, context):
    try:
        vid = event['pathParameters']['id']
        res = validations_table.get_item(Key={'id': vid})
        return {'statusCode': 200, 'body': json.dumps(res.get('Item'), default=decimal_to_float)}
    except Exception as e:
        return {'statusCode': 500, 'body': str(e)}


def get_pending_validations(event, context):
    try:
        res = validations_table.query(
            IndexName='ValidationStatusIndex',
            KeyConditionExpression=Key('validation_status').eq('manual_review'),
            ScanIndexForward=False
        )
        return {'statusCode': 200, 'body': json.dumps(res.get('Items', []), default=decimal_to_float)}
    except Exception as e:
        return {'statusCode': 500, 'body': str(e)}


def get_duplicate_validations(event, context):
    try:
        res = validations_table.query(
            IndexName='ValidationStatusIndex',
            KeyConditionExpression=Key('validation_status').eq('duplicate'),
            ScanIndexForward=False
        )
        return {'statusCode': 200, 'body': json.dumps(res.get('Items', []), default=decimal_to_float)}
    except Exception as e:
        return {'statusCode': 500, 'body': str(e)}


def manual_review(event, context):
    try:
        vid = event['pathParameters']['id']
        body = json.loads(event['body'])
        action = body.get('action')  # 'approve' | 'reject'
        notes = body.get('notes', '')

        item = validations_table.get_item(Key={'id': vid}).get('Item')
        if not item: return {'statusCode': 404, 'body': 'Not found'}

        status = 'validated' if action == 'approve' else 'rejected'
        update_validation_status(vid, status, manual_notes=notes)

        # Si se aprueba, hay que marcar la notificación asociada como validada también
        if action == 'approve' and body.get('notification_id'):
            notifications_table.update_item(
                Key={'id': body['notification_id']},
                UpdateExpression="SET #s = :s",
                ExpressionAttributeNames={'#s': 'status'},
                ExpressionAttributeValues={':s': 'validated'}
            )

        # Callback a Odoo si la validación tiene nota_venta
        nota_venta = item.get('nota_venta')
        if nota_venta:
            callback_status = 'approved' if action == 'approve' else 'rejected'
            callback_odoo(nota_venta, callback_status)

        return {'statusCode': 200, 'body': json.dumps({'message': f'Review {action} processed'})}
    except Exception as e:
        return {'statusCode': 500, 'body': str(e)}

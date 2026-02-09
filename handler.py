import json
import os
import traceback
import uuid
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource('dynamodb')
table_name = os.environ['NOTIFICATIONS_TABLE']
table = dynamodb.Table(table_name)


def decimal_to_float(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError


def parse_notification(title, text):
    if title.strip() != "Confirmación de Pago":
        print(f"[ParseNotif] Ignorando notificación. Título incorrecto: '{title}'")
        return None

    try:
        if text.strip().startswith("Yape! "):
            parts = text.split(" te envió un pago por S/ ")

            if len(parts) != 2:
                print(f"[ParseNotif] Error formato Yape (Moderno). Se esperaban 2 partes, se obtuvieron {len(parts)}. Texto: {text}")
                return None

            name = parts[0].replace("Yape! ", "").strip()
            amount_str = parts[1].strip()
            code = "000"

            amount = Decimal(amount_str)

            return {
                'name': name,
                'amount': amount,
                'code': code
            }
        else:
            first = text.split(" te envió un pago por S/ ")

            if len(first) != 2:
                print(f"[ParseNotif] Error split 1 (Formato Completo). Texto: {text}")
                return None

            second = first[1].split(". El cód. de seguridad es: ")

            if len(second) != 2:
                print(f"[ParseNotif] Error split 2 (Falta código seguridad). Texto: {text}")
                return None

            name = first[0].strip()
            amount_str = second[0].strip()
            code = second[1].strip()

            if not code.isdigit() or len(code) != 3:
                print(f"[ParseNotif] Código inválido detectado: '{code}'")
                return None

            amount = Decimal(amount_str)

            return {
                'name': name,
                'amount': amount,
                'code': code
            }

    except (ValueError, IndexError, Exception) as e:
        print(f"[ParseNotif] Excepción al parsear: {str(e)} | Texto original: {text}")
        return None


def insert_notification(event, context):
    try:
        body = json.loads(event['body'])

        title = body.get('title', '')
        text = body.get('text', '')

        device_id = body.get('device_id', 'null')
        timestamp = body.get('timestamp', -1)

        print(f"[InsertNotif] Recibido desde Device: {device_id} | Title: '{title}' | Text Length: {len(text)}")

        parsed_data = parse_notification(title, text)

        if parsed_data is None:
            print(f"[InsertNotif] Parsing fallido o título ignorado. No se inserta en DB.")

            return {
                'statusCode': 400,
                'headers': {
                    'Content-Type': 'application/json; charset=utf-8',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({
                    'error': 'Invalid notification format or irrelevant title.',
                    'received_title': title,
                    'hint': 'Title must be "Confirmación de Pago"'
                }, ensure_ascii=False)
            }

        new_id = str(uuid.uuid4())

        print(f"[InsertNotif] Insertando notificación ID: {new_id} | Name: {parsed_data['name']} | Amount: {parsed_data['amount']} | Code: {parsed_data['code']}")

        notification = {
            'id': new_id,
            'device_id': device_id,
            'name': parsed_data['name'],
            'amount': parsed_data['amount'],
            'code': parsed_data['code'],
            'timestamp': timestamp,
            'status': 'pending'
        }

        table.put_item(Item=notification)

        response_data = notification.copy()
        response_data['amount'] = float(response_data['amount'])

        return {
            'statusCode': 201,
            'headers': {
                'Content-Type': 'application/json; charset=utf-8',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'message': 'Notification inserted successfully',
                'data': response_data
            }, ensure_ascii=False)
        }

    except json.JSONDecodeError as e:
        print(f"[InsertNotif] Error JSON Body: {str(e)}")

        return {
            'statusCode': 400,
            'headers': {
                'Content-Type': 'application/json; charset=utf-8',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'error': 'Invalid JSON format'}, ensure_ascii=False)
        }

    except Exception as e:
        print(f"[InsertNotif] Error Crítico: {str(e)}")
        print(traceback.format_exc())

        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json; charset=utf-8',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'error': 'Internal server error', 'details': str(e)}, ensure_ascii=False)
        }


def get_notifications(event, context):
    try:
        params = event.get('queryStringParameters', {}) or {}
        limit = int(params.get('limit', 20))
        last_key = params.get('last_key')

        scan_kwargs = {'Limit': limit}

        if last_key:
            try:
                scan_kwargs['ExclusiveStartKey'] = json.loads(last_key)
            except:
                pass

        response = table.scan(**scan_kwargs)
        items = response.get('Items', [])

        result = {
            'count': len(items),
            'data': items
        }

        if 'LastEvaluatedKey' in response:
            result['last_key'] = json.dumps(response['LastEvaluatedKey'], default=decimal_to_float)
            result['has_more'] = True
        else:
            result['has_more'] = False

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json; charset=utf-8',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps(result, default=decimal_to_float, ensure_ascii=False)
        }

    except Exception as e:
        print(f"[GetNotifs] Error: {str(e)}")

        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json; charset=utf-8',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'error': str(e)}, ensure_ascii=False)
        }


def get_notification_by_id(event, context):
    try:
        notification_id = event['pathParameters']['id']
        response = table.get_item(Key={'id': notification_id})

        if 'Item' not in response:
            return {
                'statusCode': 404,
                'headers': {
                    'Content-Type': 'application/json; charset=utf-8',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({'error': 'Notification not found'}, ensure_ascii=False)
            }

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json; charset=utf-8',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'data': response['Item']}, default=decimal_to_float, ensure_ascii=False)
        }

    except Exception as e:
        print(f"[GetNotifById] Error: {str(e)}")

        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json; charset=utf-8',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'error': str(e)}, ensure_ascii=False)
        }


def get_notifications_by_status(event, context):
    try:
        status = event['pathParameters']['status']
        valid_statuses = ['pending', 'validated', 'rejected']

        if status not in valid_statuses:
            return {
                'statusCode': 400,
                'headers': {
                    'Content-Type': 'application/json; charset=utf-8',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({'error': f'Invalid status. Must be: {", ".join(valid_statuses)}'}, ensure_ascii=False)
            }

        params = event.get('queryStringParameters', {}) or {}
        limit = int(params.get('limit', 20))
        last_key = params.get('last_key')

        query_kwargs = {
            'IndexName': 'StatusIndex',
            'KeyConditionExpression': Key('status').eq(status),
            'Limit': limit,
            'ScanIndexForward': False
        }

        if last_key:
            try:
                query_kwargs['ExclusiveStartKey'] = json.loads(last_key)
            except:
                pass

        response = table.query(**query_kwargs)
        items = response.get('Items', [])

        result = {
            'status': status,
            'count': len(items),
            'data': items
        }

        if 'LastEvaluatedKey' in response:
            result['last_key'] = json.dumps(response['LastEvaluatedKey'], default=decimal_to_float)
            result['has_more'] = True
        else:
            result['has_more'] = False

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json; charset=utf-8',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps(result, default=decimal_to_float, ensure_ascii=False)
        }

    except Exception as e:
        print(f"[GetNotifsByStatus] Error: {str(e)}")

        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json; charset=utf-8',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'error': str(e)}, ensure_ascii=False)
        }


def get_notifications_by_device(event, context):
    try:
        device_id = event['pathParameters']['device_id']
        params = event.get('queryStringParameters', {}) or {}
        limit = int(params.get('limit', 20))
        last_key = params.get('last_key')

        query_kwargs = {
            'IndexName': 'DeviceIndex',
            'KeyConditionExpression': Key('device_id').eq(device_id),
            'Limit': limit,
            'ScanIndexForward': False
        }

        if last_key:
            try:
                query_kwargs['ExclusiveStartKey'] = json.loads(last_key)
            except:
                pass

        response = table.query(**query_kwargs)
        items = response.get('Items', [])

        result = {
            'device_id': device_id,
            'count': len(items),
            'data': items
        }

        if 'LastEvaluatedKey' in response:
            result['last_key'] = json.dumps(response['LastEvaluatedKey'], default=decimal_to_float)
            result['has_more'] = True
        else:
            result['has_more'] = False

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json; charset=utf-8',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps(result, default=decimal_to_float, ensure_ascii=False)
        }

    except Exception as e:
        print(f"[GetNotifsByDevice] Error: {str(e)}")

        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json; charset=utf-8',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'error': str(e)}, ensure_ascii=False)
        }


def update_notification_status(event, context):
    try:
        notification_id = event['pathParameters']['id']
        body = json.loads(event['body'])
        new_status = body.get('status')

        valid_statuses = ['pending', 'validated', 'rejected']
        if new_status not in valid_statuses:
            return {
                'statusCode': 400,
                'headers': {
                    'Content-Type': 'application/json; charset=utf-8',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({'error': f'Invalid status. Must be: {", ".join(valid_statuses)}'}, ensure_ascii=False)
            }

        print(f"[UpdateStatus] ID: {notification_id} -> Nuevo Status: {new_status}")

        response = table.update_item(
            Key={'id': notification_id},
            UpdateExpression='SET #status = :status',
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={':status': new_status},
            ReturnValues='ALL_NEW'
        )

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json; charset=utf-8',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'message': 'Status updated successfully',
                'data': response['Attributes']
            }, default=decimal_to_float, ensure_ascii=False)
        }

    except Exception as e:
        print(f"[UpdateStatus] Error: {str(e)}")

        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json; charset=utf-8',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'error': str(e)}, ensure_ascii=False)
        }

# Validacion Zazu - Documentacion de Funciones Lambda

## Informacion General

- **Servicio**: validacion-zazu
- **Region**: us-west-1
- **Runtime**: Python 3.12
- **Stage**: dev (por defecto)

## Flujo Principal

Odoo recibe un comprobante de pago por WhatsApp y envia el media_id a este servicio. El servicio descarga la imagen, la sube a S3, ejecuta OCR con Textract, extrae monto y codigo, y lo cruza contra la tabla de notificaciones existente. Si hay match, aprueba el pago y notifica a Odoo. Si no, queda en revision manual.

---

## Funciones con Endpoint HTTP

### 1. validateFromOdoo

- **Metodo**: POST
- **Ruta**: /validate-from-odoo
- **Timeout**: 60s
- **Descripcion**: Punto de entrada desde Odoo. Recibe los datos del comprobante enviado por WhatsApp e inicia el proceso de validacion de forma asincrona. Retorna 200 inmediatamente.

**Body requerido**:

| Campo | Tipo | Requerido | Descripcion |
|---|---|---|---|
| media_id | string | Si | ID del archivo multimedia de WhatsApp |
| nota_venta | string | Si | Numero de orden/nota de venta en Odoo |
| phone_number | string | Si | Numero de telefono del cliente |
| sender_name | string | No | Nombre del remitente (default: "Cliente") |

---

### 2. getDuplicateValidations

- **Metodo**: GET
- **Ruta**: /validations/duplicates
- **Descripcion**: Retorna todas las validaciones marcadas como duplicadas. Util para el dashboard de monitoreo.

**Body**: No requiere.

---

### 3. getValidations

- **Metodo**: GET
- **Ruta**: /validations
- **Descripcion**: Retorna las validaciones mas recientes (limite 50). Usado para el dashboard general.

**Body**: No requiere.
0
---

### 4. getValidationById

- **Metodo**: GET
- **Ruta**: /validations/{id}
- **Descripcion**: Retorna el detalle completo de una validacion especifica.

**Parametro de ruta**:

| Campo | Tipo | Descripcion |
|---|---|---|
| id | string | ID de la validacion (UUID) |

**Body**: No requiere.

---

### 5. getPendingValidations

- **Metodo**: GET
- **Ruta**: /validations/pending
- **Descripcion**: Retorna todas las validaciones en estado "manual_review". Estas son las que necesitan atencion de un asesor.

**Body**: No requiere.

---

### 6. manualReview

- **Metodo**: PUT
- **Ruta**: /validations/{id}/review
- **Descripcion**: Permite a un asesor aprobar o rechazar una validacion pendiente. Si se aprueba y se incluye notification_id, tambien marca la notificacion como validada. Envia callback a Odoo con el resultado.

**Parametro de ruta**:

| Campo | Tipo | Descripcion |
|---|---|---|
| id | string | ID de la validacion (UUID) |

**Body requerido**:

| Campo | Tipo | Requerido | Descripcion |
|---|---|---|---|
| action | string | Si | "approve" o "reject" |
| notes | string | No | Notas del asesor sobre la decision |
| notification_id | string | No | ID de la notificacion asociada (para marcarla como validada si se aprueba) |

---

## Funciones Internas (sin endpoint HTTP)

Estas funciones no tienen ruta HTTP. Se invocan de forma asincrona entre Lambdas o por triggers de AWS.

### 7. processWhatsAppImage

- **Trigger**: Invocacion asincrona desde validateFromOdoo
- **Timeout**: 60s
- **Descripcion**: Descarga la imagen del comprobante desde la API de WhatsApp (Facebook Graph API) usando el media_id, la sube al bucket S3 con metadata y crea el registro inicial de validacion en DynamoDB con estado "processing". La subida a S3 dispara automaticamente processScreenshot.

**Payload interno**:

| Campo | Tipo | Descripcion |
|---|---|---|
| image_id | string | ID del media de WhatsApp |
| phone_number | string | Numero de telefono del cliente |
| sender_name | string | Nombre del remitente |
| message_id | string | ID del mensaje de WhatsApp (opcional) |
| nota_venta | string | Numero de orden (opcional) |

---

### 8. processScreenshot

- **Trigger**: Evento S3 (s3:ObjectCreated) en el bucket de screenshots
- **Timeout**: 60s
- **Descripcion**: Se activa automaticamente cuando se sube una imagen al bucket S3. Ejecuta OCR con Amazon Textract para extraer el texto del comprobante. Parsea el texto buscando monto (S/ X.XX) y codigo de operacion. Si el OCR falla, marca la validacion como "manual_review" y notifica a Odoo como rechazado. Si tiene exito, invoca validatePayment de forma asincrona.

**Payload**: Evento estandar de S3 (automatico). La metadata del objeto S3 contiene validation_id, phone_number, sender_name y nota_venta.

---

### 9. validatePayment

- **Trigger**: Invocacion asincrona desde processScreenshot
- **Timeout**: 30s
- **Descripcion**: Logica de negocio principal. Busca en la tabla de notificaciones una que coincida en codigo y monto. Luego valida que el nombre del remitente coincida. Posibles resultados:
  - **Validado**: Codigo, monto y nombre coinciden con una notificacion pendiente. Marca ambos registros como validados. Callback a Odoo como "approved".
  - **Duplicado**: La notificacion ya fue validada previamente. Marca la validacion como "duplicate". Callback a Odoo como "rejected".
  - **Sin match**: No existe notificacion con ese codigo y monto. Marca como "manual_review". Callback a Odoo como "rejected".
  - **Nombre no coincide**: Codigo y monto coinciden pero el nombre no. Marca como "manual_review" con referencia a la notificacion candidata. Callback a Odoo como "rejected".

**Payload interno**:

| Campo | Tipo | Descripcion |
|---|---|---|
| validation_id | string | ID de la validacion |
| sender_name | string | Nombre extraido por OCR |
| amount | string | Monto extraido (se convierte a Decimal) |
| code | string | Codigo de seguridad u operacion |
| phone_number | string | Numero de telefono del cliente |
| operation_number | string | Numero de operacion (opcional) |
| nota_venta | string | Numero de orden (opcional) |

---

## Callback a Odoo

Todas las funciones que resuelven una validacion envian un POST al endpoint configurado en ODOO_CALLBACK_URL con:

| Campo | Tipo | Descripcion |
|---|---|---|
| nota_venta | string | Numero de orden |
| status | string | "approved" o "rejected" |

Se incluye el header X-Callback-Token con el valor de ODOO_CALLBACK_TOKEN para autenticacion.

---

## Variables de Entorno

| Variable | Fuente | Descripcion |
|---|---|---|
| NOTIFICATIONS_TABLE | serverless.yml | Nombre de la tabla DynamoDB de notificaciones (servicio externo) |
| VALIDATIONS_TABLE | serverless.yml | Nombre de la tabla DynamoDB de validaciones |
| SCREENSHOTS_BUCKET | serverless.yml | Nombre del bucket S3 para screenshots |
| WHATSAPP_TOKEN | SSM /zazu/whatsapp_token | Token de la API de WhatsApp Business |
| ODOO_CALLBACK_URL | SSM /zazu/odoo_callback_url | URL del endpoint callback en Odoo |
| ODOO_CALLBACK_TOKEN | SSM /zazu/odoo_callback_token | Token de autenticacion para el callback |

---

## Recursos de Infraestructura (creados por este stack)

- **ValidationsTable**: Tabla DynamoDB para registros de validacion
- **ScreenshotsBucket**: Bucket S3 para almacenar imagenes de comprobantes (auto-eliminacion a 90 dias)
- **CommonLibs Layer**: Layer con dependencias Python (requests, etc.)

La tabla de notificaciones NO se crea en este stack, ya existe en el servicio de notificaciones desplegado por separado.

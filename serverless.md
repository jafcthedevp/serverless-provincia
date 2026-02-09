org: maykol
app: zazu
service: zazu

provider:
  name: aws
  runtime: python3.12
  region: us-east-1
  stage: ${opt:stage, 'dev'}
  memorySize: 256
  timeout: 29
  logRetentionInDays: 14
  environment:
    NOTIFICATIONS_TABLE: ${self:service}-notifications-${sls:stage}
    VALIDATIONS_TABLE: ${self:service}-validations-${sls:stage}
    SCREENSHOTS_BUCKET: ${self:service}-screenshots-${sls:stage}
    WHITELIST_TABLE: ${self:service}-whitelist-${sls:stage}

    WHATSAPP_TOKEN: ${ssm:/zazu/whatsapp_token}
    WEBHOOK_VERIFY_TOKEN: ${ssm:/zazu/webhook_verify_token}

  iam:
    role:
      statements:
        - Effect: Allow
          Action:
            - dynamodb:Query
            - dynamodb:Scan
            - dynamodb:GetItem
            - dynamodb:PutItem
            - dynamodb:UpdateItem
            - dynamodb:DeleteItem
            - dynamodb:DescribeTable
          Resource:
            - arn:aws:dynamodb:${aws:region}:${aws:accountId}:table/${self:service}-*
            - arn:aws:dynamodb:${aws:region}:${aws:accountId}:table/${self:service}-*/index/*

        - Effect: Allow
          Action:
            - s3:GetObject
            - s3:PutObject
            - s3:DeleteObject
            - s3:ListBucket
          Resource:
            - arn:aws:s3:::${self:service}-screenshots-${sls:stage}
            - arn:aws:s3:::${self:service}-screenshots-${sls:stage}/*

        - Effect: Allow
          Action:
            - textract:DetectDocumentText
            - textract:AnalyzeDocument
          Resource: "*"

        - Effect: Allow
          Action:
            - lambda:InvokeFunction
          Resource:
            - arn:aws:lambda:${aws:region}:${aws:accountId}:function:${self:service}-${sls:stage}-*

plugins:
  - serverless-prune-plugin

custom:
  prune:
    automatic: true
    number: 3

# ===== DEFINICIÓN DEL LAYER =====
layers:
  CommonLibs:
    path: layers/common # Serverless buscará aquí la carpeta 'python' con las librerías
    name: ${self:service}-common-libs-${sls:stage}
    compatibleRuntimes:
      - python3.12

functions:
  # ===== WHITELIST =====
  addToWhitelist:
    handler: validation_handler.add_to_whitelist
    layers:
      - { Ref: CommonLibsLambdaLayer } # Inyecta el Layer
    events:
      - httpApi:
          path: /whitelist
          method: post

  removeFromWhitelist:
    handler: validation_handler.remove_from_whitelist
    layers:
      - { Ref: CommonLibsLambdaLayer }
    events:
      - httpApi:
          path: /whitelist/{phone_number}
          method: delete

  getWhitelist:
    handler: validation_handler.get_whitelist
    layers:
      - { Ref: CommonLibsLambdaLayer }
    events:
      - httpApi:
          path: /whitelist
          method: get

  checkWhitelist:
    handler: validation_handler.check_whitelist
    layers:
      - { Ref: CommonLibsLambdaLayer }
    events:
      - httpApi:
          path: /whitelist/{phone_number}
          method: get

  # ===== NOTIFICACIONES (handler.py no usa requests, no necesita layer estricto) =====
  insertNotification:
    handler: handler.insert_notification
    events:
      - httpApi:
          path: /insert
          method: post

  getNotifications:
    handler: handler.get_notifications
    events:
      - httpApi:
          path: /notifications
          method: get

  getNotificationById:
    handler: handler.get_notification_by_id
    events:
      - httpApi:
          path: /notifications/{id}
          method: get

  getNotificationsByStatus:
    handler: handler.get_notifications_by_status
    events:
      - httpApi:
          path: /notifications/status/{status}
          method: get

  getNotificationsByDevice:
    handler: handler.get_notifications_by_device
    events:
      - httpApi:
          path: /notifications/device/{device_id}
          method: get

  searchNotifications:
    handler: handler.search_notifications
    events:
      - httpApi:
          path: /notifications/search
          method: get

  updateNotificationStatus:
    handler: handler.update_notification_status
    events:
      - httpApi:
          path: /notifications/{id}/status
          method: put

  # ===== VALIDACIÓN Y WHATSAPP (Usan validation_handler.py, REQUIEREN LAYER) =====

  whatsappWebhook:
    handler: validation_handler.whatsapp_webhook
    layers:
      - { Ref: CommonLibsLambdaLayer }
    events:
      - httpApi:
          path: /whatsapp/webhook
          method: post
      - httpApi:
          path: /whatsapp/webhook
          method: get
    timeout: 10

  processWhatsAppImage:
    handler: validation_handler.process_whatsapp_image
    layers:
      - { Ref: CommonLibsLambdaLayer }
    timeout: 60

  sendWhatsAppMessage:
    handler: validation_handler.send_whatsapp_message_lambda
    layers:
      - { Ref: CommonLibsLambdaLayer }
    timeout: 30

  processScreenshot:
    handler: validation_handler.process_screenshot
    layers:
      - { Ref: CommonLibsLambdaLayer }
    events:
      - s3:
          bucket: ${self:service}-screenshots-${sls:stage}
          event: s3:ObjectCreated:*
          existing: true
    timeout: 60

  getDuplicateValidations:
    handler: validation_handler.get_duplicate_validations
    layers:
      - { Ref: CommonLibsLambdaLayer }
    events:
      - httpApi:
          path: /validations/duplicates
          method: get

  validatePayment:
    handler: validation_handler.validate_payment
    layers:
      - { Ref: CommonLibsLambdaLayer }
    timeout: 30

  getValidations:
    handler: validation_handler.get_validations
    layers:
      - { Ref: CommonLibsLambdaLayer }
    events:
      - httpApi:
          path: /validations
          method: get

  getValidationById:
    handler: validation_handler.get_validation_by_id
    layers:
      - { Ref: CommonLibsLambdaLayer }
    events:
      - httpApi:
          path: /validations/{id}
          method: get

  getPendingValidations:
    handler: validation_handler.get_pending_validations
    layers:
      - { Ref: CommonLibsLambdaLayer }
    events:
      - httpApi:
          path: /validations/pending
          method: get

  manualReview:
    handler: validation_handler.manual_review
    layers:
      - { Ref: CommonLibsLambdaLayer }
    events:
      - httpApi:
          path: /validations/{id}/review
          method: put

resources:
  Resources:
    # Tabla de Notificaciones
    NotificationsTable:
      Type: AWS::DynamoDB::Table
      Properties:
        TableName: ${self:service}-notifications-${sls:stage}
        BillingMode: PAY_PER_REQUEST
        AttributeDefinitions:
          - AttributeName: id
            AttributeType: S
          - AttributeName: status
            AttributeType: S
          - AttributeName: device_id
            AttributeType: S
          - AttributeName: timestamp
            AttributeType: N
          - AttributeName: code
            AttributeType: S
          - AttributeName: amount
            AttributeType: N
        KeySchema:
          - AttributeName: id
            KeyType: HASH
        GlobalSecondaryIndexes:
          - IndexName: StatusIndex
            KeySchema:
              - AttributeName: status
                KeyType: HASH
              - AttributeName: timestamp
                KeyType: RANGE
            Projection:
              ProjectionType: ALL
          - IndexName: DeviceIndex
            KeySchema:
              - AttributeName: device_id
                KeyType: HASH
              - AttributeName: timestamp
                KeyType: RANGE
            Projection:
              ProjectionType: ALL
          - IndexName: CodeAmountIndex
            KeySchema:
              - AttributeName: code
                KeyType: HASH
              - AttributeName: amount
                KeyType: RANGE
            Projection:
              ProjectionType: ALL
        StreamSpecification:
          StreamViewType: NEW_AND_OLD_IMAGES

    # Tabla de Validaciones
    ValidationsTable:
      Type: AWS::DynamoDB::Table
      Properties:
        TableName: ${self:service}-validations-${sls:stage}
        BillingMode: PAY_PER_REQUEST
        AttributeDefinitions:
          - AttributeName: id
            AttributeType: S
          - AttributeName: validation_status
            AttributeType: S
          - AttributeName: created_at
            AttributeType: N
          - AttributeName: phone_number
            AttributeType: S
        KeySchema:
          - AttributeName: id
            KeyType: HASH
        GlobalSecondaryIndexes:
          - IndexName: ValidationStatusIndex
            KeySchema:
              - AttributeName: validation_status
                KeyType: HASH
              - AttributeName: created_at
                KeyType: RANGE
            Projection:
              ProjectionType: ALL
          - IndexName: PhoneNumberIndex
            KeySchema:
              - AttributeName: phone_number
                KeyType: HASH
              - AttributeName: created_at
                KeyType: RANGE
            Projection:
              ProjectionType: ALL

    # Tabla de Whitelist
    WhitelistTable:
      Type: AWS::DynamoDB::Table
      Properties:
        TableName: ${self:service}-whitelist-${sls:stage}
        BillingMode: PAY_PER_REQUEST
        AttributeDefinitions:
          - AttributeName: phone_number
            AttributeType: S
        KeySchema:
          - AttributeName: phone_number
            KeyType: HASH
        TimeToLiveSpecification:
          AttributeName: ttl
          Enabled: true

    # Bucket S3
    ScreenshotsBucket:
      Type: AWS::S3::Bucket
      Properties:
        BucketName: ${self:service}-screenshots-${sls:stage}
        PublicAccessBlockConfiguration:
          BlockPublicAcls: true
          BlockPublicPolicy: true
          IgnorePublicAcls: true
          RestrictPublicBuckets: true
        LifecycleConfiguration:
          Rules:
            - Id: DeleteOldScreenshots
              Status: Enabled
              ExpirationInDays: 90

package:
  patterns:
    - '!node_modules/**'
    - '!.venv/**'
    - '!.git/**'
    - '!.idea/**'
    - '!.serverless/**'
    - '!__pycache__/**'
    - '!layers/**'
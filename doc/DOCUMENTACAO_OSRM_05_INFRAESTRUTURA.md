# 📘 DOCUMENTAÇÃO OSRM DISTANCE PIPELINE

## SEÇÃO 5: INFRAESTRUTURA AWS

---

## ☁️ RECURSOS E CONFIGURAÇÕES

Esta seção documenta toda a infraestrutura AWS provisionada via Terraform.

---

## 1. OVERVIEW DOS RECURSOS

### 1.1 Arquitetura AWS

```
┌────────────────────────────────────────────────────────────────────┐
│                          AWS RESOURCES                              │
└────────────────────────────────────────────────────────────────────┘

┌──────────────────┐      ┌──────────────────┐      ┌──────────────┐
│  EventBridge     │─────▶│  Lambda Function │─────▶│   EC2 VM     │
│  (2 rules)       │      │ (ec2-auto-start) │      │  t3a.xlarge  │
│  • START 04:00   │      │                  │      │              │
│  • STOP 04:30    │      └──────────────────┘      │  ┌────────┐  │
└──────────────────┘                                 │  │ OSRM   │  │
                                                     │  │Container│  │
                                                     │  └────────┘  │
                                                     └──────┬───────┘
                                                            │
                                                            ▼
┌────────────────────────────────────────────────────────────────────┐
│                          S3 BUCKETS                                 │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ 50-ze-datalake-refined (SOURCE)                              │  │
│  │ └── data_mesh/vw_antifraud_fact_distances/YYYY-MM/          │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ 20-ze-datalake-landing (DESTINATION)                         │  │
│  │ ├── osrm_distance/osrm_landing/year=YYYY/month=MM/          │  │
│  │ ├── osrm_distance/osrm_success/ ◀────┐                      │  │
│  │ ├── osrm_distance/osrm_failed/  ◀────┤                      │  │
│  │ └── osrm_distance/control/bookmark.json                     │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
                                  │ (S3 Event Notification)
                                  ▼
┌────────────────────────────────────────────────────────────────────┐
│  MONITORAMENTO                                                      │
│  ┌──────────────────┐      ┌──────────────┐      ┌──────────────┐ │
│  │ Lambda Function  │─────▶│  SNS Topic   │─────▶│   E-mail     │ │
│  │ (osrm-monitor)   │      │ (osrm-notif) │      │   Alert      │ │
│  └──────────────────┘      └──────────────┘      └──────────────┘ │
└────────────────────────────────────────────────────────────────────┘
```

### 1.2 Lista Completa de Recursos

**Terraform Modules:**
1. **EC2 Scheduler** (`ec2-scheduler/`)
   - 2 EventBridge Rules (start/stop)
   - 1 Lambda Function (ec2-auto-start)
   - IAM Role + Policy
   - 4 Lambda Permissions

2. **Monitoring** (`monitoring/`)
   - 1 SNS Topic
   - 1 SNS Subscription (e-mail)
   - 1 Lambda Function (osrm-log-monitor)
   - IAM Role + Policy
   - 2 S3 Event Notifications
   - 2 Lambda Permissions

---

## 2. EC2 SCHEDULER (Auto Start/Stop)

### 2.1 Terraform - main.tf

**Localização:** `/terraform/ec2-scheduler/main.tf`

**Recursos Provisionados:**

```hcl
# 1. IAM ROLE para Lambda
resource "aws_iam_role" "lambda_ec2_scheduler" {
  name = "lambda-ec2-scheduler-role"
  
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

# 2. IAM POLICY - Permissões EC2
resource "aws_iam_role_policy" "lambda_ec2_policy" {
  name = "lambda-ec2-scheduler-policy"
  role = aws_iam_role.lambda_ec2_scheduler.id
  
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:StartInstances",
          "ec2:StopInstances",
          "ec2:DescribeInstances"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

# 3. LAMBDA FUNCTION - Controle EC2
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda_function.py"
  output_path = "${path.module}/lambda_function.zip"
}

resource "aws_lambda_function" "ec2_scheduler" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "ec2-auto-start"
  role             = aws_iam_role.lambda_ec2_scheduler.arn
  handler          = "lambda_function.lambda_handler"
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  runtime          = "python3.11"
  timeout          = 60
  
  environment {
    variables = {
      INSTANCE_ID = var.instance_id  # i-091d85e742c86d24f
    }
  }
}

# 4. EVENTBRIDGE RULE - START às 04:00 (Brasília)
resource "aws_cloudwatch_event_rule" "ec2_scheduler_morning" {
  name                = "ec2-auto-start-morning"
  description         = "Iniciar EC2 às 4h (Brasília)"
  schedule_expression = "cron(0 7 * * ? *)"  # 07:00 UTC = 04:00 BRT
}

# 5. EVENTBRIDGE RULE - STOP às 04:30 (Safety)
resource "aws_cloudwatch_event_rule" "ec2_scheduler_morning_stop" {
  name                = "ec2-auto-stop-morning"
  description         = "Parar EC2 30min após iniciar (4h30 Brasília)"
  schedule_expression = "cron(30 7 * * ? *)"  # 07:30 UTC = 04:30 BRT
}

# 6. EVENTBRIDGE TARGET - START
resource "aws_cloudwatch_event_target" "lambda_target_morning" {
  rule      = aws_cloudwatch_event_rule.ec2_scheduler_morning.name
  target_id = "ec2-scheduler-lambda-morning"
  arn       = aws_lambda_function.ec2_scheduler.arn
  input     = jsonencode({ action = "start" })
}

# 7. EVENTBRIDGE TARGET - STOP
resource "aws_cloudwatch_event_target" "lambda_target_morning_stop" {
  rule      = aws_cloudwatch_event_rule.ec2_scheduler_morning_stop.name
  target_id = "ec2-scheduler-lambda-morning-stop"
  arn       = aws_lambda_function.ec2_scheduler.arn
  input     = jsonencode({ action = "stop" })
}

# 8. LAMBDA PERMISSION - EventBridge → Lambda (START)
resource "aws_lambda_permission" "allow_eventbridge_morning" {
  statement_id  = "AllowExecutionFromEventBridgeMorning"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2_scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ec2_scheduler_morning.arn
}

# 9. LAMBDA PERMISSION - EventBridge → Lambda (STOP)
resource "aws_lambda_permission" "allow_eventbridge_morning_stop" {
  statement_id  = "AllowExecutionFromEventBridgeMorningStop"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2_scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ec2_scheduler_morning_stop.arn
}
```

### 2.2 Variáveis (terraform.tfvars)

```hcl
instance_id = "i-091d85e742c86d24f"  # ID da VM OSRM

aws_region = "us-west-2"

schedule_expressions = [
  "cron(0 7 * * ? *)",   # 04:00 Brasília (START)
  "cron(0 15 * * ? *)"   # 12:00 Brasília (STOP) [desabilitado]
]
```

### 2.3 Lambda Function (lambda_function.py)

**Responsabilidades:**
- Verificar estado atual da instância
- Garantir estado consistente (para antes de ligar)
- Aguardar confirmação de mudança de estado (Waiter)

**Código completo fornecido na documentação (doc index 5)**

---

## 3. SISTEMA DE MONITORAMENTO

### 3.1 Terraform - main.tf

**Localização:** `/terraform/monitoring/main.tf`

```hcl
# 1. SNS TOPIC - Notificações
resource "aws_sns_topic" "osrm_notifications" {
  name         = "osrm-pipeline-notifications"
  display_name = "OSRM Pipeline Notifications"
  
  tags = {
    Name        = "osrm-notifications"
    Environment = "production"
    Project     = "osrm-distance"
  }
}

# 2. SNS SUBSCRIPTION - E-mail
resource "aws_sns_topic_subscription" "osrm_email" {
  topic_arn = aws_sns_topic.osrm_notifications.arn
  protocol  = "email"
  endpoint  = var.notification_email  # br184733@ambev.com.br
}

# 3. IAM ROLE - Lambda Monitor
resource "aws_iam_role" "osrm_monitor_lambda" {
  name = "osrm-monitor-lambda-role"
  
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

# 4. IAM POLICY - S3 + SNS + Logs
resource "aws_iam_role_policy" "osrm_monitor_policy" {
  name = "osrm-monitor-lambda-policy"
  role = aws_iam_role.osrm_monitor_lambda.id
  
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::${var.s3_bucket_name}",
          "arn:aws:s3:::${var.s3_bucket_name}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = ["sns:Publish"]
        Resource = aws_sns_topic.osrm_notifications.arn
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

# 5. LAMBDA FUNCTION - Monitor de Logs
data "archive_file" "lambda_monitor_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda_osrm_monitor.py"
  output_path = "${path.module}/lambda_osrm_monitor.zip"
}

resource "aws_lambda_function" "osrm_monitor" {
  filename         = data.archive_file.lambda_monitor_zip.output_path
  function_name    = "osrm-log-monitor"
  role             = aws_iam_role.osrm_monitor_lambda.arn
  handler          = "lambda_osrm_monitor.lambda_handler"
  source_code_hash = data.archive_file.lambda_monitor_zip.output_base64sha256
  runtime          = "python3.11"
  timeout          = 60
  memory_size      = 256
  
  environment {
    variables = {
      SNS_TOPIC_ARN = aws_sns_topic.osrm_notifications.arn
      S3_BUCKET     = var.s3_bucket_name
    }
  }
}

# 6. S3 EVENT NOTIFICATION - Success
resource "aws_s3_bucket_notification" "osrm_success_notification" {
  bucket = var.s3_bucket_name
  
  lambda_function {
    lambda_function_arn = aws_lambda_function.osrm_monitor.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "osrm_distance/osrm_success/"
    filter_suffix       = ".log"
  }
  
  depends_on = [aws_lambda_permission.allow_s3_success]
}

# 7. S3 EVENT NOTIFICATION - Failed
resource "aws_s3_bucket_notification" "osrm_failed_notification" {
  bucket = var.s3_bucket_name
  
  lambda_function {
    lambda_function_arn = aws_lambda_function.osrm_monitor.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "osrm_distance/osrm_failed/"
    filter_suffix       = ".log"
  }
  
  depends_on = [aws_lambda_permission.allow_s3_failed]
}

# 8. LAMBDA PERMISSION - S3 → Lambda (Success)
resource "aws_lambda_permission" "allow_s3_success" {
  statement_id  = "AllowS3InvokeSuccess"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.osrm_monitor.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = "arn:aws:s3:::${var.s3_bucket_name}"
}

# 9. LAMBDA PERMISSION - S3 → Lambda (Failed)
resource "aws_lambda_permission" "allow_s3_failed" {
  statement_id  = "AllowS3InvokeFailed"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.osrm_monitor.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = "arn:aws:s3:::${var.s3_bucket_name}"
}
```

### 3.2 Variáveis (variables.tf)

```hcl
variable "aws_region" {
  description = "Região da AWS"
  type        = string
  default     = "us-west-2"
}

variable "s3_bucket_name" {
  description = "Nome do bucket S3 onde os logs são salvos"
  type        = string
  default     = "20-ze-datalake-landing"
}

variable "notification_email" {
  description = "E-mail para receber notificações"
  type        = string
  default     = "br184733@ambev.com.br"
}

variable "project_name" {
  description = "Nome do projeto"
  type        = string
  default     = "osrm-distance"
}
```

---

## 4. ESTRUTURA DE PASTAS S3

### 4.1 Bucket: 50-ze-datalake-refined (SOURCE)

```
50-ze-datalake-refined/
└── data_mesh/
    └── vw_antifraud_fact_distances/
        ├── 2024-01/
        │   ├── part-00000-xxx.snappy.parquet
        │   ├── part-00001-xxx.snappy.parquet
        │   └── ...
        ├── 2025-01/
        ├── 2025-11/
        └── 2025-12/
            ├── part-00000-20251201.snappy.parquet  ← Gerado às 03:00
            └── part-00001-20251202.snappy.parquet
```

### 4.2 Bucket: 20-ze-datalake-landing (DESTINATION)

```
20-ze-datalake-landing/
└── osrm_distance/
    ├── osrm_landing/  (DADOS PROCESSADOS)
    │   └── year=2025/
    │       ├── month=01/
    │       │   └── consolidated-2025-01.parquet
    │       ├── month=11/
    │       │   └── consolidated-2025-11.parquet
    │       └── month=12/  (MÊS CORRENTE)
    │           ├── dedupe_abc123_000.parquet
    │           ├── dedupe_def456_000.parquet
    │           └── ...
    │
    ├── osrm_success/  (LOGS DE SUCESSO)
    │   ├── 2025-12-01_040000_success.log
    │   ├── 2025-12-02_040000_success.log
    │   └── ...
    │
    ├── osrm_failed/  (LOGS DE FALHA)
    │   ├── 2025-11-15_040000_osrm_timeout.log
    │   ├── 2025-11-20_040000_container_restart.log
    │   └── ...
    │
    └── control/  (ESTADO DO PIPELINE)
        └── bookmark.json
```

---

## 5. ANÁLISE DE CUSTOS

### 5.1 Custos Mensais Estimados

**EC2 (t3a.xlarge):**
```
Modo Anterior (24/7):
  • Preço: $0.1504/hora
  • Uso: 720 horas/mês
  • Custo: $108.29/mês

Modo Otimizado (2x/dia, 30min):
  • Uso: 30 horas/mês
  • Custo: $4.51/mês
  • Economia: 96% ($103.78/mês)
```

**Lambda (EC2 Scheduler):**
```
Invocações: ~60/mês (2x/dia × 30 dias)
Duração média: 5 segundos
Memória: 128 MB

Free Tier: 1M invocações + 400.000 GB-s
Custo: $0.00/mês (dentro do Free Tier)
```

**Lambda (Log Monitor):**
```
Invocações: ~60/mês (1 log/dia)
Duração média: 2 segundos
Memória: 256 MB

Custo: $0.00/mês (Free Tier)
```

**SNS:**
```
E-mails: ~60/mês
Free Tier: 1.000 e-mails/mês

Custo: $0.00/mês (Free Tier)
```

**S3:**
```
Armazenamento:
  • Dados processados: ~500 GB
  • Logs: ~60 MB/mês
  • Total: ~500 GB

Custo: ~$11.50/mês (Standard Storage)
```

**EventBridge:**
```
Rules: 2 regras (start/stop)
Invocações: ~60/mês

Free Tier: Todas as regras gratuitas
Custo: $0.00/mês
```

**TOTAL MENSAL:**
```
EC2:          $4.51
S3:          $11.50
Lambda:       $0.00
SNS:          $0.00
EventBridge:  $0.00
───────────────────
TOTAL:       $16.01/mês

Economia vs modo 24/7: $92.28/mês (85% redução)
```

---

## 6. DEPLOYMENT

### 6.1 Pré-requisitos

```bash
# 1. AWS CLI configurado
aws configure
AWS Access Key ID: AKIA...
AWS Secret Access Key: ...
Default region: us-west-2

# 2. Terraform instalado
terraform --version
# Terraform v1.6.0+
```

### 6.2 Aplicar Infraestrutura

**EC2 Scheduler:**
```bash
cd terraform/ec2-scheduler/

# Inicializar
terraform init

# Planejar
terraform plan -var-file="terraform.tfvars"

# Aplicar
terraform apply -var-file="terraform.tfvars"

# Output esperado:
# + aws_iam_role.lambda_ec2_scheduler
# + aws_lambda_function.ec2_scheduler
# + aws_cloudwatch_event_rule.ec2_scheduler_morning
# + ... (9 recursos)
```

**Monitoring:**
```bash
cd terraform/monitoring/

terraform init
terraform apply -var-file="terraform.tfvars"

# IMPORTANTE: Confirmar e-mail SNS!
# Verificar inbox: br184733@ambev.com.br
# Clicar no link de confirmação
```

### 6.3 Validação

```bash
# 1. Verificar Lambda
aws lambda list-functions | grep ec2-auto-start

# 2. Verificar EventBridge
aws events list-rules | grep ec2-auto

# 3. Verificar SNS
aws sns list-topics | grep osrm

# 4. Teste manual Lambda
aws lambda invoke \
  --function-name ec2-auto-start \
  --payload '{"action": "start"}' \
  output.json
```

---

## 7. PRÓXIMA SEÇÃO

**Seção 6:** Monitoramento & Notificações (fluxo completo, exemplos de e-mails)

---

**Última Atualização:** 01/12/2025  
**Versão:** 1.0

variable "instance_id" {
  description = "ID da instância EC2 para iniciar"
  type = string
  default = "i-091d85e742c86d24f"
}

variable "aws_region" {
  description = "Região da AWS"
  type = string
  default = "us-west-2"
}

variable "schedule_expressions" {
  description = "Expressões cron"
  type = list(string)
  default = [
    "cron(0 7 * * ? *)",
    "cron(0 15 * * ? *)",
  ]
}

# Provider AWS
provider "aws" {
  region = var.aws_region
}

# IAM Role para Lambda
resource "aws_iam_role" "lambda_ec2_scheduler" {
  name = "lambda-ec2-scheduler-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
}

# Policy para a Lambda iniciar EC2
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

# Lambda Function
data "archive_file" "lambda_zip" {
  type = "zip"
  source_file = "${path.module}/lambda_function.py"
  output_path = "${path.module}/lambda_function.zip"
}

resource "aws_lambda_function" "ec2_scheduler" {
  filename = data.archive_file.lambda_zip.output_path
  function_name = "ec2-auto-start"
  role = aws_iam_role.lambda_ec2_scheduler.arn
  handler = "lambda_function.lambda_handler"
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  runtime = "python3.11"
  timeout = 60

  environment {
    variables = {
      INSTANCE_ID = var.instance_id
    }
  }
}

# EventBridge Rules (3 schedules para START)
resource "aws_cloudwatch_event_rule" "ec2_scheduler_morning" {
  name= "ec2-auto-start-morning"
  description = "Iniciar EC2 às 4h (Brasília)"
  schedule_expression = var.schedule_expressions[0]
}

resource "aws_cloudwatch_event_rule" "ec2_scheduler_noon" {
  name = "ec2-auto-start-noon"
  description = "Iniciar EC2 ao meio-dia (Brasília)"
  schedule_expression = var.schedule_expressions[1]
}


# EventBridge Rules (3 schedules para STOP - 30 minutos depois)
resource "aws_cloudwatch_event_rule" "ec2_scheduler_morning_stop" {
  name = "ec2-auto-stop-morning"
  description = "Parar EC2 30min após iniciar (4h30 Brasília)"
  schedule_expression = "cron(30 7 * * ? *)"
}

resource "aws_cloudwatch_event_rule" "ec2_scheduler_noon_stop" {
  name = "ec2-auto-stop-noon"
  description = "Parar EC2 30min após iniciar (12h30 Brasília)"
  schedule_expression = "cron(30 15 * * ? *)"
}


# Targets do EventBridge para START
resource "aws_cloudwatch_event_target" "lambda_target_morning" {
  rule = aws_cloudwatch_event_rule.ec2_scheduler_morning.name
  target_id = "ec2-scheduler-lambda-morning"
  arn  = aws_lambda_function.ec2_scheduler.arn
  input= jsonencode({ action = "start" })
}

resource "aws_cloudwatch_event_target" "lambda_target_noon" {
  rule = aws_cloudwatch_event_rule.ec2_scheduler_noon.name
  target_id = "ec2-scheduler-lambda-noon"
  arn  = aws_lambda_function.ec2_scheduler.arn
  input= jsonencode({ action = "start" })
}


# Targets do EventBridge para STOP
resource "aws_cloudwatch_event_target" "lambda_target_morning_stop" {
  rule = aws_cloudwatch_event_rule.ec2_scheduler_morning_stop.name
  target_id = "ec2-scheduler-lambda-morning-stop"
  arn  = aws_lambda_function.ec2_scheduler.arn
  input= jsonencode({ action = "stop" })
}

resource "aws_cloudwatch_event_target" "lambda_target_noon_stop" {
  rule = aws_cloudwatch_event_rule.ec2_scheduler_noon_stop.name
  target_id = "ec2-scheduler-lambda-noon-stop"
  arn = aws_lambda_function.ec2_scheduler.arn
  input= jsonencode({ action = "stop" })
}


# Permissões para EventBridge invocar Lambda (6 regras: 3 start + 3 stop)
resource "aws_lambda_permission" "allow_eventbridge_morning" {
  statement_id = "AllowExecutionFromEventBridgeMorning"
  action = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2_scheduler.function_name
  principal= "events.amazonaws.com"
  source_arn = aws_cloudwatch_event_rule.ec2_scheduler_morning.arn
}

resource "aws_lambda_permission" "allow_eventbridge_noon" {
  statement_id = "AllowExecutionFromEventBridgeNoon"
  action = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2_scheduler.function_name
  principal= "events.amazonaws.com"
  source_arn = aws_cloudwatch_event_rule.ec2_scheduler_noon.arn
}


resource "aws_lambda_permission" "allow_eventbridge_morning_stop" {
  statement_id = "AllowExecutionFromEventBridgeMorningStop"
  action = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2_scheduler.function_name
  principal= "events.amazonaws.com"
  source_arn = aws_cloudwatch_event_rule.ec2_scheduler_morning_stop.arn
}

resource "aws_lambda_permission" "allow_eventbridge_noon_stop" {
  statement_id = "AllowExecutionFromEventBridgeNoonStop"
  action = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2_scheduler.function_name
  principal= "events.amazonaws.com"
  source_arn = aws_cloudwatch_event_rule.ec2_scheduler_noon_stop.arn
}


# Outputs
output "lambda_function_arn" {
  value = aws_lambda_function.ec2_scheduler.arn
  description = "ARN da função Lambda"
}

output "instance_id" {
  value = var.instance_id
  description = "ID da instância EC2 sendo gerenciada"
}

output "eventbridge_schedules" {
  value = {
    start_morning = aws_cloudwatch_event_rule.ec2_scheduler_morning.arn
    start_noon    = aws_cloudwatch_event_rule.ec2_scheduler_noon.arn
    stop_morning  = aws_cloudwatch_event_rule.ec2_scheduler_morning_stop.arn
    stop_noon     = aws_cloudwatch_event_rule.ec2_scheduler_noon_stop.arn
  }
  description = "ARNs das regras do EventBridge (start e stop)"
}
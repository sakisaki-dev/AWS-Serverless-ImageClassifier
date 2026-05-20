# version of terraform and aws necessary to run this code
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ── Variables ──────────────────────────────────────────────────────────────────

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-west-2"
}

variable "bucket_name" {
  description = "Name of the S3 bucket (must be globally unique)"
  type        = string
  default     = "homevision-file-processor"
}

variable "alert_email" {
  description = "Email address to receive CloudWatch error alerts"
  type        = string
}

# ── S3 Bucket ─────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "main" {
  bucket = var.bucket_name

  tags = {
    Project     = "homevision-takehome"
    Environment = "demo"
  }
}

# to block all public access
resource "aws_s3_bucket_public_access_block" "main" {
  bucket                  = aws_s3_bucket.main.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# version control
resource "aws_s3_bucket_versioning" "main" {
  bucket = aws_s3_bucket.main.id
  versioning_configuration {
    status = "Enabled"
  }
}

# encryption with AES256 and bucket keys
resource "aws_s3_bucket_server_side_encryption_configuration" "main" {
  bucket = aws_s3_bucket.main.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# ── SQS Dead Letter Queue ─────────────────────────────────────────────────────

resource "aws_sqs_queue" "dlq" {
  name                      = "homevision-processor-dlq"
  message_retention_seconds = 1209600  # 14 days
  kms_master_key_id         = "alias/aws/sqs"

  tags = {
    Project = "homevision-takehome"
  }
}

# ── IAM Role for Lambda ────────────────────────────────────────────────────────
# Least privelege role

resource "aws_iam_role" "lambda" {
  name = "homevision-processor-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "homevision-processor-lambda-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.lambda.arn}:*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:CopyObject"
        ]
        Resource = "${aws_s3_bucket.main.arn}/*"
      },
      # Rekognition 
      {
        Effect   = "Allow"
        Action   = ["rekognition:DetectLabels"]
        Resource = "*"   
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.dlq.arn
      }
    ]
  })
}

# ── CloudWatch Log Group ───────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/homevision-file-processor"
  retention_in_days = 30   
}

# ── Lambda Function ────────────────────────────────────────────────────────────

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/../lambda_function.py"
  output_path = "${path.module}/lambda.zip"
}

resource "aws_lambda_function" "processor" {
  function_name    = "homevision-file-processor"
  role             = aws_iam_role.lambda.arn
  handler          = "lambda_function.lambda_handler"
  runtime          = "python3.12"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  timeout     = 30    
  memory_size = 256   

  dead_letter_config {
    target_arn = aws_sqs_queue.dlq.arn
  }

  environment {
    variables = {
      LOG_LEVEL = "INFO"
    }
  }

  tags = {
    Project = "homevision-takehome"
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_permission" "s3" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.processor.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.main.arn
}

# ── S3 Event Notification → Lambda ────────────────────────────────────────────

resource "aws_s3_bucket_notification" "trigger" {
  bucket = aws_s3_bucket.main.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.processor.arn
    events              = ["s3:ObjectCreated:*"]  
    filter_prefix = ""   # catches everything; loop prevention is in Lambda code
  }

  depends_on = [aws_lambda_permission.s3]
}

# ── CloudWatch Alarm + SNS Alert ───────────────────────────────────────────────


resource "aws_sns_topic" "alerts" {
  name = "homevision-processor-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "homevision-processor-errors"
  alarm_description   = "Lambda error rate exceeded threshold"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 60          
  statistic           = "Sum"
  threshold           = 2           # threshhold at 2 Errors/min
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.processor.function_name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# ── Outputs ────────────────────────────────────────────────────────────────────

output "bucket_name" {
  value       = aws_s3_bucket.main.bucket
  description = "Upload files to this S3 bucket"
}

output "lambda_function_name" {
  value       = aws_lambda_function.processor.function_name
  description = "Lambda function that processes uploads"
}

output "dlq_url" {
  value       = aws_sqs_queue.dlq.url
  description = "Dead letter queue — inspect failed events here"
}

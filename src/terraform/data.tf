# data.tf
# Recupera informações da instância EC2 (necessário para atribuição de IAM Role)

data "aws_instance" "managed_instance" {
  filter {
    name   = "instance-id"
    values = [var.instance_id]
  }
}

output "managed_instance_arn" {
  value = data.aws_instance.managed_instance.arn
  description = "ARN da instância EC2"
}
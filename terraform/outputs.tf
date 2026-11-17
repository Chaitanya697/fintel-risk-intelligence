output "public_ip" { value = aws_instance.fintel.public_ip }
output "jenkins_url" { value = "http://${aws_instance.fintel.public_ip}:8080" }
output "app_url" { value = "http://${aws_instance.fintel.public_ip}:30080" }
output "grafana_url" { value = "http://${aws_instance.fintel.public_ip}:30030/d/fintel-ops" }
output "prometheus_url" { value = "http://${aws_instance.fintel.public_ip}:30090" }
output "s3_bucket" { value = aws_s3_bucket.data.bucket }
output "ecr_registry" { value = local.registry }
output "upload_dataset_command" {
  value = "aws s3 cp ./archive/ s3://${aws_s3_bucket.data.bucket}/raw/ --recursive --exclude '*' --include '*.csv'"
}

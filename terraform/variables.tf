variable "region" {
  description = "AWS region (AWS Academy Learner Lab uses us-east-1)"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "EC2 size. t2.large = 2 vCPU / 8 GB, enough for Jenkins + k3s + the app + monitoring"
  type        = string
  default     = "t2.large"
}

variable "root_volume_gb" {
  description = "Disk size. 30 GB leaves room for Docker images, k3s and Jenkins builds"
  type        = number
  default     = 30
}

variable "key_name" {
  description = "Existing EC2 key pair (Learner Lab provides 'vockey'). Set to \"\" for none."
  type        = string
  default     = "vockey"
}

variable "instance_profile" {
  description = "Existing IAM instance profile. Learner Lab cannot create IAM roles, so we reuse LabInstanceProfile"
  type        = string
  default     = "LabInstanceProfile"
}

variable "allowed_cidr" {
  description = "Who may reach SSH/Jenkins/app/Grafana. Use your own IP/32 for tighter security"
  type        = string
  default     = "0.0.0.0/0"
}

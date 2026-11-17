# Fintel infrastructure as code:
#   S3 bucket (data + models) -> ECR repositories (Docker images) -> security group -> EC2 (Jenkins + k3s)
data "aws_caller_identity" "me" {}

resource "random_id" "suffix" {
  byte_length = 3
}

locals {
  bucket   = "fintel-data-${random_id.suffix.hex}"
  registry = "${data.aws_caller_identity.me.account_id}.dkr.ecr.${var.region}.amazonaws.com"
  repos    = ["fintel-web", "fintel-risk-engine", "fintel-data-processor"]
  tags     = { Project = "fintel" }
}

# ---------------- S3: raw dataset, processed data, trained models ----------------
resource "aws_s3_bucket" "data" {
  bucket        = local.bucket
  force_destroy = true
  tags          = local.tags
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_object" "folders" {
  for_each = toset(["raw/", "processed/", "models/"])
  bucket   = aws_s3_bucket.data.id
  key      = each.value
  content  = ""
}

# ---------------- ECR: one repository per service ----------------
resource "aws_ecr_repository" "repo" {
  for_each             = toset(local.repos)
  name                 = each.value
  force_delete         = true
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
  tags = local.tags
}

resource "aws_ecr_lifecycle_policy" "keep_recent" {
  for_each   = aws_ecr_repository.repo
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

# ---------------- Networking: firewall for the EC2 host ----------------
resource "aws_security_group" "fintel" {
  name        = "fintel-sg-${random_id.suffix.hex}"
  description = "Fintel: SSH, Jenkins, web app, Grafana, Prometheus"
  tags        = local.tags

  dynamic "ingress" {
    for_each = {
      "SSH"        = 22
      "Jenkins"    = 8080
      "Fintel web" = 30080
      "Grafana"    = 30030
      "Prometheus" = 30090
    }
    content {
      description = ingress.key
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = [var.allowed_cidr]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------------- EC2: Jenkins + k3s Kubernetes ----------------
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }
}

resource "aws_instance" "fintel" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  key_name               = var.key_name == "" ? null : var.key_name
  iam_instance_profile   = var.instance_profile
  vpc_security_group_ids = [aws_security_group.fintel.id]

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
  }

  # hop limit 2 lets containers inside k3s pods use the instance role (S3 / ECR access)
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  user_data = templatefile("${path.module}/bootstrap.sh.tpl", {
    bucket   = local.bucket
    region   = var.region
    registry = local.registry
  })
  user_data_replace_on_change = true

  tags = merge(local.tags, { Name = "fintel-server" })
}

#!/bin/bash
# EC2 first-boot setup (runs once, as root). Log: /var/log/fintel-bootstrap.log
exec > /var/log/fintel-bootstrap.log 2>&1
set -eux

# Settings Terraform passes in; Jenkins reads them from this file
cat > /etc/fintel.env <<ENV
S3_BUCKET=${bucket}
AWS_REGION=${region}
ECR_REGISTRY=${registry}
ENV
chmod 644 /etc/fintel.env

dnf install -y docker git java-21-amazon-corretto-headless
systemctl enable --now docker

# Jenkins
curl -fsSLo /etc/yum.repos.d/jenkins.repo https://pkg.jenkins.io/redhat-stable/jenkins.repo
rpm --import https://pkg.jenkins.io/redhat-stable/jenkins.io-2023.key
dnf install -y jenkins
usermod -aG docker jenkins
usermod -aG docker ec2-user

# k3s (lightweight Kubernetes); kubeconfig readable by Jenkins
curl -sfL https://get.k3s.io | sh -s - --write-kubeconfig-mode 644
echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' > /etc/profile.d/k3s.sh

systemctl enable --now jenkins
systemctl restart jenkins
echo "FINTEL BOOTSTRAP COMPLETE"

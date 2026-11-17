// Fintel CI/CD:  GitHub -> tests -> Docker build -> Amazon ECR -> (train model) -> Kubernetes (k3s) -> smoke test
// Settings (S3 bucket, region, ECR registry) come from /etc/fintel.env, written on the EC2 by Terraform.
pipeline {
  agent any
  parameters {
    booleanParam(name: 'RETRAIN_MODEL', defaultValue: false,
                 description: 'Re-run the data-processor job (clean data + retrain the ensemble). Runs automatically the first time.')
  }
  options { timestamps(); disableConcurrentBuilds(); timeout(time: 45, unit: 'MINUTES') }
  environment {
    KUBECONFIG = '/etc/rancher/k3s/k3s.yaml'
    TAG = "${env.BUILD_NUMBER}"
    LOAD_ENV = 'set -a; . /etc/fintel.env; set +a'
  }

  stages {
    stage('1. Unit tests') {
      steps {
        sh '''
          docker build -q -f services/data-processor/Dockerfile -t fintel-data-processor:$TAG .
          docker run --rm fintel-data-processor:$TAG python -m pytest -q tests
        '''
      }
    }

    stage('2. Build Docker images') {
      steps {
        sh '''
          docker build -q -f services/risk-engine/Dockerfile -t fintel-risk-engine:$TAG .
          docker build -q -f services/web/Dockerfile        -t fintel-web:$TAG .
        '''
      }
    }

    stage('3. Push to Amazon ECR') {
      steps {
        sh '''
          eval "$LOAD_ENV"
          aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_REGISTRY
          for svc in fintel-web fintel-risk-engine fintel-data-processor; do
            docker tag $svc:$TAG $ECR_REGISTRY/$svc:$TAG
            docker push -q $ECR_REGISTRY/$svc:$TAG
          done
        '''
      }
    }

    stage('4. Check dataset in S3') {
      steps {
        sh '''
          eval "$LOAD_ENV"
          COUNT=$(aws s3 ls s3://$S3_BUCKET/raw/ | grep -c '\\.csv' || true)
          echo "Found $COUNT CSV files in s3://$S3_BUCKET/raw/"
          if [ "$COUNT" -lt 10 ]; then
            echo "Upload the Kaggle NIFTY50 CSVs to s3://$S3_BUCKET/raw/ first (see README step 4)."; exit 1
          fi
        '''
      }
    }

    stage('5. Configure cluster') {
      steps {
        sh '''
          eval "$LOAD_ENV"
          kubectl apply -f k8s/00-namespace.yaml
          # ECR login for Kubernetes (tokens last 12 h, refreshed on every build)
          kubectl -n fintel create secret docker-registry ecr-pull --docker-server=$ECR_REGISTRY \
            --docker-username=AWS --docker-password="$(aws ecr get-login-password --region $AWS_REGION)" \
            --dry-run=client -o yaml | kubectl apply -f -
          TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
          PUBLIC_IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)
          kubectl -n fintel create configmap fintel-config --from-literal=S3_BUCKET=$S3_BUCKET \
            --from-literal=AWS_REGION=$AWS_REGION --from-literal=GRAFANA_URL=http://$PUBLIC_IP:30030/d/fintel-ops \
            --dry-run=client -o yaml | kubectl apply -f -
          # app secrets: created once, kept across builds
          kubectl -n fintel get secret fintel-secret >/dev/null 2>&1 || kubectl -n fintel create secret generic fintel-secret \
            --from-literal=SECRET_KEY=$(openssl rand -hex 32) --from-literal=GRAFANA_PASSWORD=$(openssl rand -hex 8)
          # monitoring config files -> ConfigMaps
          kubectl -n fintel create configmap prometheus-config --from-file=prometheus.yml=monitoring/prometheus/prometheus-k8s.yml --dry-run=client -o yaml | kubectl apply -f -
          kubectl -n fintel create configmap grafana-datasources --from-file=monitoring/grafana/provisioning/datasources --dry-run=client -o yaml | kubectl apply -f -
          kubectl -n fintel create configmap grafana-providers --from-file=monitoring/grafana/provisioning/dashboards --dry-run=client -o yaml | kubectl apply -f -
          kubectl -n fintel create configmap grafana-dashboards --from-file=monitoring/grafana/dashboards --dry-run=client -o yaml | kubectl apply -f -
        '''
      }
    }

    stage('6. Train model (data-processor job)') {
      steps {
        sh '''
          eval "$LOAD_ENV"
          if [ "$RETRAIN_MODEL" = "true" ] || ! aws s3 ls s3://$S3_BUCKET/models/risk_model.joblib >/dev/null 2>&1; then
            kubectl -n fintel delete job data-processor --ignore-not-found
            sed -e "s|__REGISTRY__|$ECR_REGISTRY|g" -e "s|__TAG__|$TAG|g" k8s/jobs/data-processor-job.yaml | kubectl apply -f -
            kubectl -n fintel wait --for=condition=complete job/data-processor --timeout=900s || {
              kubectl -n fintel logs job/data-processor --tail=50; exit 1; }
            kubectl -n fintel logs job/data-processor --tail=15
            # make running risk-engines reload the new model
            kubectl -n fintel rollout restart deployment/risk-engine 2>/dev/null || true
          else
            echo "Model already in s3://$S3_BUCKET/models/ - skipping training (tick RETRAIN_MODEL to retrain)."
          fi
        '''
      }
    }

    stage('7. Deploy to Kubernetes') {
      steps {
        sh '''
          eval "$LOAD_ENV"
          for f in k8s/10-risk-engine.yaml k8s/20-web.yaml k8s/30-monitoring.yaml; do
            sed -e "s|__REGISTRY__|$ECR_REGISTRY|g" -e "s|__TAG__|$TAG|g" $f | kubectl apply -f -
          done
          kubectl -n fintel rollout restart deployment/grafana deployment/prometheus
          kubectl -n fintel rollout status deployment/risk-engine --timeout=300s
          kubectl -n fintel rollout status deployment/web --timeout=180s
          kubectl -n fintel get pods -o wide
        '''
      }
    }

    stage('8. Smoke test') {
      steps {
        sh '''
          sleep 5
          curl -fsS http://localhost:30080/health | tee /tmp/fintel-health.json
          grep -q '"model_loaded":true' /tmp/fintel-health.json
          curl -fsS -o /dev/null http://localhost:30080/login
          echo "Smoke test passed"
        '''
      }
    }
  }

  post {
    success {
      sh '''
        TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
        IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)
        echo "================ FINTEL DEPLOYED ================"
        echo "App:        http://$IP:30080"
        echo "Grafana:    http://$IP:30030/d/fintel-ops"
        echo "Prometheus: http://$IP:30090"
      '''
    }
    always {
      sh 'docker image prune -f >/dev/null 2>&1 || true'
    }
  }
}

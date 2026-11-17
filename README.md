# Fintel: Financial Risk Intelligence Platform
A cloud-based system that tells an investor **how risky their portfolio is, why, and what could happen under market
stress**, combining financial analytics, ensemble machine learning and AWS infrastructure.
(Dark navy, brass and oxblood visual theme with law-style motifs; Ranade font.)

For each portfolio the dashboard shows:
- **Portfolio Overview**: risk score /100 (LOW / MEDIUM / HIGH), value, diversification, volatility, max drawdown, VaR, beta
- **Risk Metrics**: sector exposure, correlation between stocks, per-holding volatility / beta / return
- **Risk Explanation**: plain-English reasons, e.g. "41% of the portfolio is exposed to IT", "TCS and INFY are highly correlated"
- **Stress Test**: market crash, IT / banking sector crash, oil price shock, high volatility, plus a custom scenario
- **Historical Crises**: how the holdings actually did in 2008, 2011, 2015-16 and COVID-2020 (real prices)
- **Machine Learning**: Random Forest, Gradient Boosting and XGBoost predictions and the ensemble result

Data: Kaggle *NIFTY-50 Stock Market Data (2000-2021)* (rohanrao). 49 stocks, 5,305 trading days.

---

## 1. Architecture

```
               terraform apply (AWS CloudShell)
                          |
     +--------------------+-------------------------------+
     |                    |                               |
  Amazon S3           Amazon ECR                    Amazon EC2 (t2.large, 30 GB)
  raw/  <- Kaggle CSVs  fintel-web                   +-- Jenkins  (CI/CD, :8080)
  processed/            fintel-risk-engine           +-- k3s Kubernetes
  models/               fintel-data-processor            +-- data-processor Job  S3 raw/ -> processed/ + models/
                                                         +-- risk-engine x2      analytics + ensemble ML API
GitHub -> Jenkins: test -> build -> push ECR             +-- web                 login, cases, UI   (:30080)
         -> train -> deploy -> smoke test                +-- Prometheus (:30090) + Grafana (:30030)
```

| AWS service / tool | Role in Fintel |
|---|---|
| **Terraform** | Creates S3 bucket, 3 ECR repos, security group and the EC2 server as code (`terraform/`) |
| **S3** | Raw dataset, processed data (`returns.csv.gz`, `volatility.csv`, ...), trained model + model card |
| **ECR** | Docker images for the 3 services (scan-on-push enabled) |
| **EC2** | Hosts Jenkins and the k3s Kubernetes cluster |
| **IAM** | Pods and Jenkins use the instance role `LabInstanceProfile`: no access keys anywhere in the code |
| **Kubernetes (k3s)** | Runs web (1), risk-engine (2 replicas), data-processor (job), Prometheus, Grafana |
| **Jenkins** | `Jenkinsfile`: unit tests, Docker build, push to ECR, train model, deploy, smoke test |
| **Prometheus + Grafana** | "Fintel - Application Health": requests, response time, CPU, memory, ML predictions, errors |

### Machine learning (honest summary)
- 9,000 historical sample portfolios (random stocks, weights, dates 2001-2020). 10 features per portfolio, exactly the
  ones in the project design: volatility, average return, max drawdown, sector concentration, number of stocks,
  average correlation, trading volume (liquidity), market volatility, beta, VaR.
- **Label** = what actually happened over the next 6 months *relative to the market* (volatility ratio + drawdown ratio),
  split into thirds: LOW / MEDIUM / HIGH.
- Trained on 2001-2016, tested on 2017-2020 (never seen): **ensemble accuracy ~53% vs 34% by guessing**;
  rank correlation between score and real outcome ~0.55. The app shows this accuracy note on every risk report.
- Risk score = 100 x (0.5 x P(medium) + P(high)), averaged over the three models.

---

## 2. Run it on AWS (AWS Academy Learner Lab), step by step  (~45 min the first time)

### Step 1: Put the code on GitHub
1. Create a **public** repo, e.g. `fintel`.
2. Unzip `fintel.zip`, open the `fintel` folder, upload **everything inside it** (Add file -> Upload files; drag the
   folders too). Check that `Jenkinsfile` sits at the top level of the repo, not inside a `fintel/` folder.
   Do **not** upload the dataset: it goes to S3.

### Step 2: Start the lab and open CloudShell
1. Learner Lab -> **Start Lab** -> wait for the green dot -> click **AWS**.
2. Region must be **N. Virginia (us-east-1)**.
3. Click the **CloudShell** icon (`>_` in the top bar). A terminal opens in the browser.

### Step 3: Create the infrastructure with Terraform (in CloudShell)
```bash
# install Terraform (one time)
mkdir -p ~/bin && cd ~ && curl -sLo tf.zip https://releases.hashicorp.com/terraform/1.9.8/terraform_1.9.8_linux_amd64.zip \
  && unzip -o tf.zip -d ~/bin && rm tf.zip && echo 'export PATH=$HOME/bin:$PATH' >> ~/.bashrc && export PATH=$HOME/bin:$PATH
terraform -version

# get the code and create everything
git clone https://github.com/<YOUR_USERNAME>/fintel.git
cd fintel/terraform
terraform init
terraform apply          # type: yes   (~2 minutes)
```
Terraform prints the outputs: **save them**. Example:
```
app_url      = "http://3.91.20.10:30080"
jenkins_url  = "http://3.91.20.10:8080"
grafana_url  = "http://3.91.20.10:30030/d/fintel-ops"
s3_bucket    = "fintel-data-1a2b3c"
```
Run `terraform output` any time to see them again.
The EC2 server now installs Docker, Jenkins and k3s by itself: **wait ~5 minutes** before Step 5.

### Step 4: Upload the dataset to S3
1. Unzip the Kaggle `archive.zip` on your laptop.
2. AWS console -> **S3** -> open the bucket `fintel-data-xxxxxx` -> open the **raw/** folder.
3. **Upload** -> **Add files** -> select **all the .csv files** (50 stock files + `stock_metadata.csv`;
   `NIFTY50_all.csv` is optional) -> **Upload**.
   The files must be directly inside `raw/` (for example `raw/TCS.csv`), not inside a sub-folder.

### Step 5: Set up Jenkins
1. Connect to the server: **EC2 -> Instances -> fintel-server -> Connect -> EC2 Instance Connect -> Connect**.
2. Check the setup finished, then get the Jenkins password:
```bash
sudo tail -2 /var/log/fintel-bootstrap.log      # must show: FINTEL BOOTSTRAP COMPLETE
sudo cat /var/lib/jenkins/secrets/initialAdminPassword
```
3. Open `jenkins_url` -> paste the password -> **Install suggested plugins** -> create your admin user
   (write the password down) -> Save and Finish.
4. **New Item** -> name `fintel-pipeline` -> **Pipeline** -> OK.
5. Pipeline section: Definition **Pipeline script from SCM** -> SCM **Git** -> Repository URL
   `https://github.com/<YOUR_USERNAME>/fintel.git` -> Branch **`*/main`** -> Script Path **`Jenkinsfile`** -> **Save**.

### Step 6: Build and deploy
Click **Build Now** (the first time Jenkins may show "Build Now"; later builds show **Build with Parameters**).
The first build takes ~10-12 minutes:

| Stage | What happens |
|---|---|
| 1. Unit tests | Builds the data-processor image and runs `pytest` |
| 2. Build Docker images | risk-engine and web images |
| 3. Push to Amazon ECR | All 3 images pushed, tagged with the build number |
| 4. Check dataset in S3 | Fails with a clear message if Step 4 was skipped |
| 5. Configure cluster | Namespace, ECR pull secret, config, secrets, monitoring config |
| 6. Train model | Kubernetes Job reads S3 `raw/`, writes `processed/` + `models/` (first build, or tick RETRAIN_MODEL) |
| 7. Deploy to Kubernetes | risk-engine (2 pods), web, Prometheus, Grafana |
| 8. Smoke test | Checks the app is up and the model is loaded |

The console ends with the URLs. Open **`app_url`**, create an account, and a *Sample Portfolio* (the example from the project design) is waiting.

### Step 7: Show it working
```bash
kubectl -n fintel get pods                 # web, 2x risk-engine, prometheus, grafana Running; data-processor Completed
kubectl -n fintel logs job/data-processor  # training log with model accuracy
aws s3 ls s3://$(grep S3_BUCKET /etc/fintel.env | cut -d= -f2) --recursive | head -20
```
- **App:** `http://<IP>:30080` -> Dashboard -> open a portfolio -> Portfolio Overview, Risk Metrics, Risk Explanation, Stress Test, Historical Crises, Machine Learning
- **Grafana:** `http://<IP>:30030/d/fintel-ops` (opens without login; admin password:
  `kubectl -n fintel get secret fintel-secret -o jsonpath='{.data.GRAFANA_PASSWORD}' | base64 -d`)
- **Architecture** page in the app shows the S3 bucket, AWS services, model accuracy and feature importance.
- **ECR:** AWS console -> ECR -> repositories show image tags = Jenkins build numbers.

### Demo talking points
1. *Terraform* created S3, ECR, the security group and EC2: show `terraform output`.
2. *S3* holds raw, processed and model data: show the bucket folders.
3. *Jenkins* runs CI/CD: show the stage view; *ECR* stores the images.
4. *Kubernetes* runs the services: `kubectl get pods`; delete a risk-engine pod and watch it come back.
5. *The app*: create a concentrated portfolio (e.g. TATASTEEL, VEDL, HINDALCO) -> HIGH risk score and its explanation ->
   Stress Test -> Historical Crises (2008 / COVID).
6. *Grafana*: click around the app and watch requests, latency and ML predictions update.

---

## 3. When the lab restarts
Learner Lab stops the EC2 when the session ends. After **Start Lab**:
1. EC2 -> start `fintel-server` if it is stopped. Jenkins and k3s start automatically, and so do the pods.
2. The **public IP changes**: in CloudShell run `cd ~/fintel/terraform && terraform refresh && terraform output`, or read it in the EC2 console.
3. Run one Jenkins build to refresh the ECR login and the Grafana link (optional if the pods are already Running).

## 4. Clean up
```bash
cd ~/fintel/terraform && terraform destroy      # removes EC2, S3 bucket (and data), ECR repos, security group
```

## 5. Troubleshooting
| Problem | Fix |
|---|---|
| `terraform apply`: not authorized for `iam:PassRole` / instance profile | Check the lab is started; `LabInstanceProfile` exists in Learner Lab. Retry. |
| Instance type not allowed | `terraform apply -var instance_type=t3.large` |
| Jenkins page doesn't open | Wait 5 min after apply; check `sudo tail /var/log/fintel-bootstrap.log` |
| Stage 4 fails | CSVs missing in `s3://<bucket>/raw/`: redo Step 4 |
| Pods `ImagePullBackOff` | Re-run the build (refreshes the 12-hour ECR token) |
| Portfolio page "Analysis unavailable" | Risk engine still loading the model: wait 30 s; `kubectl -n fintel logs deploy/risk-engine` |
| Node `DiskPressure` | `docker system prune -af` on the EC2 |
| No CloudShell in your lab | Run Terraform on your laptop: Learner Lab -> **AWS Details** -> AWS CLI -> copy credentials into `~/.aws/credentials` |

---

## 6. Run locally (optional, laptop with Docker Desktop)
```bash
# put the Kaggle CSVs in ./data/raw/
docker compose run --rm data-processor     # trains the model into ./data
docker compose up -d --build               # app http://localhost:8000, Grafana http://localhost:3000
```

## 7. Project structure
```
fintel_common/           shared code: storage.py (S3 or local), finance.py (all risk maths)
services/data-processor/ process.py: cleaning, 9,000 sample portfolios, RF + GB + XGBoost training
services/risk-engine/    engine.py: /analyze, /stress, /stocks, /model, /health, /ready, /metrics
services/web/            app.py (Flask, login, SQLite), templates/ (dashboard UI), static/ (CSS, JS)
k8s/                     Kubernetes manifests (+ jobs/data-processor-job.yaml)
monitoring/              Prometheus configs, Grafana datasource + dashboard
terraform/               S3, ECR, security group, EC2 + bootstrap script
tests/                   unit tests for the risk maths (run by Jenkins)
Jenkinsfile              CI/CD pipeline
docker-compose.yml       local run
```

*Educational project. Not investment advice. Data ends 30 April 2021.*

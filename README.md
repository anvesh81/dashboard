# EBS Outpost Disk Monitoring

Internal AWS Fargate app that reads disk usage CSVs written by SSM State Manager
documents on your EC2 Outpost servers, aggregates them daily, and serves a
real-time monitoring dashboard to your team.

## Folder structure

```
ebs-monitoring/
├── Dockerfile                  Single image — API service + aggregator task
├── README.md                   This file
├── app/
│   ├── main.py                 FastAPI — serves dashboard + /api/* routes
│   ├── aggregator.py           Reads all S3 CSVs, writes summary.json
│   └── index.html              Dashboard UI
├── cfn/
│   └── main-stack.yaml         Full CloudFormation stack (uses your existing VPC)
└── scripts/
    └── deploy.sh               Automated build → push → deploy script
```

## How it works

```
EC2 on Outpost (Windows)
  └── SSM State Manager (daily)
       └── wp-outpost-ebsmonitoring / tv-outpost-ebsmonitoring
            └── S3: outposts-ebs-storage-monitoring/
                 wp-outpost/YYYY-MM-DD/SERVER_YYYY-MM-DD.csv
                 tv-outpost/YYYY-MM-DD/SERVER_YYYY-MM-DD.csv

EventBridge (daily, 06:30 UTC)
  └── ECS Fargate task — aggregator.py
       Reads ALL date folders (no fixed window — grows from day 1)
       Computes growth rates, days until full, downsize candidates
       Writes → s3://outposts-ebs-storage-monitoring/summary.json

Internal ALB (Cognito auth)
  └── ECS Fargate service — main.py (FastAPI)
       GET /           → index.html (dashboard)
       GET /api/*      → disk metrics JSON
       POST /api/refresh → re-triggers aggregator (admin only)
```

## Quick start

### Step 1 — Edit CFn parameters

Open `cfn/main-stack.yaml` and note the parameters section.
You will need:
- `VpcId`           — your existing VPC ID
- `PrivateSubnet1Id` and `PrivateSubnet2Id` — private subnets (need S3 access via VPC endpoint or NAT)
- `AdminEmail`      — first admin user (receives temp password)
- `InternalCidr`    — CIDR range allowed to reach the ALB (e.g. 10.0.0.0/8)
- `AcmCertificateArn` — optional, leave blank for HTTP-only

### Step 2 — Deploy stack

```bash
aws cloudformation deploy \
  --stack-name ebs-disk-monitoring-prod \
  --template-file cfn/main-stack.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    MonitoringBucket=outposts-ebs-storage-monitoring \
    AdminEmail=your-email@company.com \
    Environment=prod \
    VpcId=vpc-XXXXXXXXXXXXXXXXX \
    PrivateSubnet1Id=subnet-XXXXXXXXXXXXXXXXX \
    PrivateSubnet2Id=subnet-XXXXXXXXXXXXXXXXX \
    InternalCidr=10.0.0.0/8 \
  --region us-east-1
```

### Step 3 — Build and push image

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/ebs-monitoring-prod"

aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin "${ECR_URI}"

docker build --platform linux/amd64 -t ebs-monitoring .
docker tag ebs-monitoring:latest "${ECR_URI}:latest"
docker push "${ECR_URI}:latest"
```

### Step 4 — Redeploy service

```bash
aws ecs update-service \
  --cluster ebs-monitoring-prod \
  --service ebs-api-prod \
  --force-new-deployment \
  --region us-east-1
```

### Step 5 — Run aggregator once

```bash
# Or use the automated script: ./scripts/deploy.sh prod
aws ecs run-task \
  --cluster ebs-monitoring-prod \
  --task-definition ebs-agg-prod \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-XXX,subnet-YYY],securityGroups=[sg-XXX],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"aggregator","command":["python","aggregator.py"]}]}'
```

### Step 6 — Open dashboard

Go to the `DashboardURL` from CloudFormation outputs.
Sign in with your `AdminEmail` + temp password from the Cognito invite email.

## Adding team members

```bash
POOL=$(aws cloudformation describe-stacks \
  --stack-name ebs-disk-monitoring-prod \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" \
  --output text)

# Read-only user
aws cognito-idp admin-create-user \
  --user-pool-id $POOL \
  --username teammate@company.com \
  --user-attributes Name=email,Value=teammate@company.com Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL

aws cognito-idp admin-add-user-to-group \
  --user-pool-id $POOL \
  --username teammate@company.com \
  --group-name ebs-readonly   # or ebs-admins
```

## Local development

```bash
# Local app container; authenticated routes still expect the ALB-injected auth header
docker build -t ebs-monitoring .
docker run -p 8080:8080 \
  -e MONITORING_BUCKET=outposts-ebs-storage-monitoring \
  -e AWS_DEFAULT_REGION=us-east-1 \
  -v ~/.aws:/root/.aws:ro \
  ebs-monitoring

# Open http://localhost:8080
# Note: /api/health remains usable, but dashboard/API routes require the auth header
```

## Update workflow (code change)

```bash
docker build --platform linux/amd64 -t ebs-monitoring .
docker tag ebs-monitoring:latest "${ECR_URI}:latest"
docker push "${ECR_URI}:latest"
aws ecs update-service --cluster ebs-monitoring-prod --service ebs-api-prod --force-new-deployment
```

Or just run `./scripts/deploy.sh prod` which does all of the above.

## Troubleshooting

| Problem | Fix |
|---|---|
| 503 on /api/summary | Run aggregator manually (Step 5). Check `/ecs/ebs-agg-prod` logs. |
| ALB health check failing | Verify FargateSg allows 8080 from AlbSg. Check `/api/health` returns 200. |
| Cognito redirect error | Verify CallbackURL in UserPoolClient matches ALB DNS. |
| Container can't reach S3 | Check S3 VPC endpoint exists in your VPC, or that NAT is configured for private subnets. |
| No data for an outpost | Check SSM State Manager ran: `aws s3 ls s3://outposts-ebs-storage-monitoring/wp-outpost/` |

## Cost estimate

| Resource | ~Monthly |
|---|---|
| Fargate (0.25 vCPU, 0.5 GB, 1 task 24/7) | ~$12 |
| ALB | ~$18 |
| ECR storage | <$1 |
| CloudWatch logs (30d) | ~$2 |
| Cognito (<50 users) | Free |
| S3 reads | <$0.01 |
| **Total** | **~$32/month** |

> If you already have a NAT gateway in your VPC, cost is lower since you share it.
> Fargate Spot can cut the task cost by ~70% — change `LaunchType: FARGATE` to
> `CapacityProviderStrategy` with `FARGATE_SPOT` in the task definition.

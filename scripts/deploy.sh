#!/usr/bin/env bash
# EBS Outpost Monitoring — Deploy script
# Usage: ./scripts/deploy.sh [prod|staging|dev]
set -euo pipefail

ENV=${1:-prod}
REGION=${AWS_DEFAULT_REGION:-us-east-1}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
STACK="ebs-disk-monitoring-${ENV}"

echo "========================================"
echo " EBS Monitoring deploy — env=${ENV}"
echo " account=${ACCOUNT}  region=${REGION}"
echo "========================================"

# ── 1. Deploy CloudFormation ──────────────────────────────────────────────────
echo ""
echo "[1/5] Deploying CloudFormation stack: ${STACK}"
echo "      Edit cfn/main-stack.yaml parameters before running!"
echo "      Required: VpcId, PrivateSubnet1Id, PrivateSubnet2Id, AdminEmail"
echo ""

# Uncomment and fill in your values, then re-run:
# aws cloudformation deploy \
#   --stack-name "${STACK}" \
#   --template-file cfn/main-stack.yaml \
#   --capabilities CAPABILITY_NAMED_IAM \
#   --parameter-overrides \
#     MonitoringBucket=outposts-ebs-storage-monitoring \
#     AdminEmail=your-email@company.com \
#     Environment="${ENV}" \
#     VpcId=vpc-XXXXXXXXXXXXXXXXX \
#     PrivateSubnet1Id=subnet-XXXXXXXXXXXXXXXXX \
#     PrivateSubnet2Id=subnet-XXXXXXXXXXXXXXXXX \
#     InternalCidr=10.0.0.0/8 \
#     AcmCertificateArn="" \
#   --region "${REGION}"

echo "      [SKIP] Uncomment the deploy block above with your VPC/subnet IDs"

# ── 2. Get outputs ────────────────────────────────────────────────────────────
echo ""
echo "[2/5] Reading stack outputs..."

ECR_URI=$(aws cloudformation describe-stacks \
  --stack-name "${STACK}" \
  --query "Stacks[0].Outputs[?OutputKey=='ECRRepositoryURI'].OutputValue" \
  --output text --region "${REGION}" 2>/dev/null || echo "STACK_NOT_DEPLOYED")

CLUSTER=$(aws cloudformation describe-stacks \
  --stack-name "${STACK}" \
  --query "Stacks[0].Outputs[?OutputKey=='ECSClusterName'].OutputValue" \
  --output text --region "${REGION}" 2>/dev/null || echo "STACK_NOT_DEPLOYED")

DASHBOARD=$(aws cloudformation describe-stacks \
  --stack-name "${STACK}" \
  --query "Stacks[0].Outputs[?OutputKey=='DashboardURL'].OutputValue" \
  --output text --region "${REGION}" 2>/dev/null || echo "STACK_NOT_DEPLOYED")

if [[ "${ECR_URI}" == "STACK_NOT_DEPLOYED" ]]; then
  echo "      Stack not deployed yet — deploy CFn first, then re-run this script"
  exit 1
fi

echo "      ECR:       ${ECR_URI}"
echo "      Cluster:   ${CLUSTER}"
echo "      Dashboard: ${DASHBOARD}"

# ── 3. Build & push Docker image ──────────────────────────────────────────────
echo ""
echo "[3/5] Building Docker image..."

# Authenticate Docker to ECR
aws ecr get-login-password --region "${REGION}" | \
  docker login --username AWS --password-stdin "${ECR_URI}"

# Build (--platform ensures amd64 even on Apple Silicon Macs)
docker build --platform linux/amd64 -t "ebs-monitoring-${ENV}" .

docker tag "ebs-monitoring-${ENV}:latest" "${ECR_URI}:latest"
docker tag "ebs-monitoring-${ENV}:latest" "${ECR_URI}:$(date +%Y%m%d-%H%M%S)"

echo "      Pushing to ECR..."
docker push "${ECR_URI}:latest"
echo "      Image pushed: ${ECR_URI}:latest"

# ── 4. Force ECS service redeploy ─────────────────────────────────────────────
echo ""
echo "[4/5] Redeploying ECS service..."

aws ecs update-service \
  --cluster "${CLUSTER}" \
  --service "ebs-api-${ENV}" \
  --force-new-deployment \
  --region "${REGION}" \
  --query 'service.deployments[0].{status:status,desired:desiredCount}' \
  --output table

echo "      Waiting for service to stabilize (this takes ~60s)..."
aws ecs wait services-stable \
  --cluster "${CLUSTER}" \
  --services "ebs-api-${ENV}" \
  --region "${REGION}"
echo "      Service stable."

# ── 5. Run aggregator first time ──────────────────────────────────────────────
echo ""
echo "[5/5] Running aggregator to generate summary.json..."

TASK_DEF=$(aws cloudformation describe-stacks \
  --stack-name "${STACK}" \
  --query "Stacks[0].Outputs[?OutputKey=='AggTaskDefArn'].OutputValue" \
  --output text --region "${REGION}")

SUBNETS=$(aws cloudformation describe-stacks \
  --stack-name "${STACK}" \
  --query "Stacks[0].Outputs[?OutputKey=='PrivateSubnets'].OutputValue" \
  --output text --region "${REGION}")

SG=$(aws cloudformation describe-stacks \
  --stack-name "${STACK}" \
  --query "Stacks[0].Outputs[?OutputKey=='FargateSecurityGroupId'].OutputValue" \
  --output text --region "${REGION}")

SUBNET1=$(echo "${SUBNETS}" | cut -d',' -f1)
SUBNET2=$(echo "${SUBNETS}" | cut -d',' -f2)

TASK_ARN=$(aws ecs run-task \
  --cluster "${CLUSTER}" \
  --task-definition "${TASK_DEF}" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNET1},${SUBNET2}],securityGroups=[${SG}],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"aggregator","command":["python","aggregator.py"]}]}' \
  --region "${REGION}" \
  --query 'tasks[0].taskArn' \
  --output text)

echo "      Task started: ${TASK_ARN}"
echo "      Waiting for aggregator to complete..."

aws ecs wait tasks-stopped \
  --cluster "${CLUSTER}" \
  --tasks "${TASK_ARN}" \
  --region "${REGION}"

EXIT_CODE=$(aws ecs describe-tasks \
  --cluster "${CLUSTER}" \
  --tasks "${TASK_ARN}" \
  --region "${REGION}" \
  --query 'tasks[0].containers[0].exitCode' \
  --output text)

if [[ "${EXIT_CODE}" == "0" ]]; then
  echo "      Aggregator completed successfully."
else
  echo "      WARNING: Aggregator exited with code ${EXIT_CODE}"
  echo "      Check logs: aws logs tail /ecs/ebs-agg-${ENV} --follow"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo " Deploy complete!"
echo " Dashboard: ${DASHBOARD}"
echo " Sign in with: ${ADMIN_EMAIL:-your AdminEmail}"
echo "========================================"
echo ""
echo " To watch logs:"
echo "   aws logs tail /ecs/ebs-api-${ENV} --follow"
echo "   aws logs tail /ecs/ebs-agg-${ENV} --follow"
echo ""
echo " To add a team member:"
echo "   POOL=\$(aws cloudformation describe-stacks --stack-name ${STACK} --query \"Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue\" --output text)"
echo "   aws cognito-idp admin-create-user --user-pool-id \$POOL --username user@company.com --user-attributes Name=email,Value=user@company.com Name=email_verified,Value=true --desired-delivery-mediums EMAIL"
echo "   aws cognito-idp admin-add-user-to-group --user-pool-id \$POOL --username user@company.com --group-name ebs-readonly"

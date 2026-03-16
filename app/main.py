"""
EBS Outpost Disk Monitoring — FastAPI app
Internal Fargate service. ALB (internal) handles Cognito auth.
ALB injects x-amzn-oidc-data header with user claims after auth.

Routes
------
GET  /                           → index.html
GET  /api/health                 → health check (no auth, ALB target check)
GET  /api/me                     → current user info
GET  /api/summary                → full fleet + drives (history stripped)
GET  /api/drives                 → drive list  ?outpost=  ?status=
GET  /api/drives/{server}/{drive}→ single drive with full history
GET  /api/downsize               → downsize candidates
POST /api/refresh                → trigger aggregator ECS task (admin only)
"""

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import boto3
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

S3          = boto3.client("s3")
ECS         = boto3.client("ecs")
BUCKET      = os.environ["MONITORING_BUCKET"]
SUMMARY_KEY = os.environ.get("SUMMARY_KEY", "summary.json")
CLUSTER     = os.environ.get("ECS_CLUSTER", "")
TASK_DEF    = os.environ.get("AGGREGATOR_TASK_DEF", "")
SUBNETS     = [s.strip() for s in os.environ.get("TASK_SUBNETS", "").split(",") if s.strip()]
SEC_GROUPS  = [s.strip() for s in os.environ.get("TASK_SECURITY_GROUPS", "").split(",") if s.strip()]

HTML_PATH   = Path(__file__).parent / "index.html"


# ── Auth ───────────────────────────────────────────────────────────────────────

def get_user(request: Request) -> dict:
    """
    ALB with Cognito injects x-amzn-oidc-data (base64url JWT payload).
    """
    header = request.headers.get("x-amzn-oidc-data", "")
    if not header:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        segment  = header.split(".")[1]
        padding  = 4 - len(segment) % 4
        payload  = json.loads(base64.urlsafe_b64decode(segment + "=" * padding))
        groups   = payload.get("cognito:groups", [])
        if isinstance(groups, str):
            groups = [g.strip() for g in groups.split(",") if g.strip()]
        return {
            "email":    payload.get("email", payload.get("sub", "unknown")),
            "groups":   groups,
            "is_admin": "ebs-admins" in groups,
        }
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Auth header parse error: {e}")


def require_admin(user: dict):
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="ebs-admins group required")


# ── S3 ─────────────────────────────────────────────────────────────────────────

def load_summary() -> dict:
    try:
        resp = S3.get_object(Bucket=BUCKET, Key=SUMMARY_KEY)
        return json.loads(resp["Body"].read())
    except S3.exceptions.NoSuchKey:
        raise HTTPException(
            status_code=503,
            detail="summary.json not found — POST /api/refresh to generate it first"
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"S3 read error: {e}")


def strip_history(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "history"}


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    """No auth — used by ALB target group health check."""
    return {"status": "healthy", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    get_user(request)   # auth gate — ALB redirects to Cognito if no header
    if not HTML_PATH.exists():
        raise HTTPException(status_code=404, detail="index.html not found in container")
    return HTMLResponse(content=HTML_PATH.read_text(encoding="utf-8"))


@app.get("/api/me")
def me(request: Request):
    user = get_user(request)
    return {"email": user["email"], "is_admin": user["is_admin"], "groups": user["groups"]}


@app.get("/api/summary")
def get_summary(request: Request):
    user    = get_user(request)
    summary = load_summary()
    lean    = {
        **summary,
        "drives": [strip_history(d) for d in summary.get("drives", [])],
        "_user":  {"email": user["email"], "is_admin": user["is_admin"]},
    }
    return JSONResponse(content=lean)


@app.get("/api/drives")
def get_drives(
    request: Request,
    outpost: str = None,
    status: str  = None,
):
    get_user(request)
    summary = load_summary()
    drives  = [strip_history(d) for d in summary.get("drives", [])]
    if outpost:
        drives = [d for d in drives if d.get("outpost") == outpost]
    if status:
        drives = [d for d in drives if d.get("status") == status]
    return {"drives": drives, "count": len(drives)}


@app.get("/api/drives/{server}/{drive}")
def get_drive(server: str, drive: str, request: Request):
    get_user(request)
    server  = unquote(server)
    drive   = unquote(drive)
    summary = load_summary()
    match   = next(
        (d for d in summary.get("drives", [])
         if d["server"] == server and d["drive"] == drive),
        None,
    )
    if not match:
        raise HTTPException(status_code=404, detail=f"Drive not found: {server} {drive}")
    return JSONResponse(content=match)


@app.get("/api/downsize")
def get_downsize(request: Request):
    get_user(request)
    summary    = load_summary()
    candidates = [
        strip_history(d) for d in summary.get("drives", [])
        if d.get("downsize_candidate")
    ]
    candidates.sort(
        key=lambda d: d.get("total_gb", 0) - d.get("used_gb", 0),
        reverse=True,
    )
    return {
        "candidates":     candidates,
        "count":          len(candidates),
        "total_wasted_gb": sum(
            d.get("total_gb", 0) - d.get("used_gb", 0) for d in candidates
        ),
    }


@app.post("/api/refresh")
def trigger_refresh(request: Request):
    user = get_user(request)
    require_admin(user)

    if not CLUSTER or not TASK_DEF:
        raise HTTPException(
            status_code=500,
            detail="ECS_CLUSTER or AGGREGATOR_TASK_DEF env vars not set"
        )

    try:
        resp = ECS.run_task(
            cluster        = CLUSTER,
            taskDefinition = TASK_DEF,
            launchType     = "FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets":        SUBNETS,
                    "securityGroups": SEC_GROUPS,
                    "assignPublicIp": "DISABLED",
                }
            },
            overrides={
                "containerOverrides": [{
                    "name":    "aggregator",
                    "command": ["python", "aggregator.py"],
                    "environment": [
                        {"name": "TRIGGERED_BY", "value": user["email"]},
                        {"name": "TRIGGERED_AT",
                         "value": datetime.now(timezone.utc).isoformat()},
                    ],
                }]
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ECS RunTask failed: {e}")

    task_arn = resp.get("tasks", [{}])[0].get("taskArn", "unknown")
    return {
        "triggered":    True,
        "task_arn":     task_arn,
        "triggered_by": user["email"],
        "message":      "Aggregator started — data refreshes in ~2 min",
    }

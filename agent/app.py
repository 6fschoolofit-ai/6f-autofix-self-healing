
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import zipfile

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import boto3
import requests

from flask import Flask, jsonify, request


# --------------------------------------------------
# APPLICATION CONFIGURATION
# --------------------------------------------------

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

log = logging.getLogger("6f-autofix")


# --------------------------------------------------
# ENVIRONMENT VARIABLES
# --------------------------------------------------

ROLE = os.getenv("ROLE", "agent")

AGENT_URL = os.getenv(
    "AGENT_URL",
    "http://agent:5000"
).rstrip("/")

DB_PATH = os.getenv(
    "DB_PATH",
    "/data/autofix.db"
)

GITHUB_API = "https://api.github.com"

GITHUB_REPOSITORY = os.getenv(
    "GITHUB_REPOSITORY",
    ""
)

GITHUB_TOKEN = os.getenv(
    "GITHUB_TOKEN",
    ""
)

WEBHOOK_SECRET = os.getenv(
    "GITHUB_WEBHOOK_SECRET",
    ""
)

BASE_BRANCH = os.getenv(
    "BASE_BRANCH",
    "master"
)

WATCH_WORKFLOW_NAME = os.getenv(
    "WATCH_WORKFLOW_NAME",
    "Self-Healing Deploy"
)

TARGET_FILE = os.getenv(
    "TARGET_FILE",
    "frontend/index.html"
)

CHECKER_FILE = os.getenv(
    "CHECKER_FILE",
    "check-html.py"
)

CHECK_COMMAND = os.getenv(
    "CHECK_COMMAND",
    "python check-html.py frontend/index.html"
)

AWS_REGION = os.getenv(
    "AWS_REGION",
    "ap-south-1"
)

BEDROCK_MODEL_ID = os.getenv(
    "BEDROCK_MODEL_ID",
    "openai.gpt-oss-120b-1:0"
)

MAX_LOG_CHARS = int(
    os.getenv("MAX_LOG_CHARS", "30000")
)


# --------------------------------------------------
# DATABASE FUNCTIONS
# --------------------------------------------------

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    Path(DB_PATH).parent.mkdir(
        parents=True,
        exist_ok=True
    )

    conn = sqlite3.connect(
        DB_PATH,
        timeout=20
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    with db() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                message TEXT,
                pr_url TEXT,
                run_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def update_job(
    run_id,
    status,
    message="",
    pr_url=None
):

    init_db()

    with db() as conn:

        conn.execute(
            """
            UPDATE jobs
            SET status=?,
                message=?,
                pr_url=COALESCE(?,pr_url),
                updated_at=?
            WHERE run_id=?
            """,
            (
                status,
                message[:1500],
                pr_url,
                now(),
                str(run_id)
            )
        )


# --------------------------------------------------
# GITHUB API FUNCTIONS
# --------------------------------------------------

def github_headers():

    if not GITHUB_TOKEN:
        raise RuntimeError(
            "GITHUB_TOKEN is not configured"
        )

    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "6F-AutoFix-Agent"
    }


def github_api(method, path, **kwargs):

    response = requests.request(
        method,
        f"{GITHUB_API}{path}",
        headers=github_headers(),
        timeout=45,
        **kwargs
    )

    if not response.ok:

        raise RuntimeError(
            f"GitHub API {method} {path} returned "
            f"{response.status_code}: {response.text[:1200]}"
        )

    if response.status_code == 204 or not response.content:
        return {}

    return response.json()


def get_repo_file(path, ref):

    encoded = quote(path, safe="/")

    data = github_api(
        "GET",
        f"/repos/{GITHUB_REPOSITORY}/contents/{encoded}",
        params={"ref": ref}
    )

    if data.get("type") != "file" or not data.get("content"):

        raise RuntimeError(
            f"{path} is not a readable GitHub file"
        )

    raw = base64.b64decode(data["content"])

    return raw, data.get("sha")


# --------------------------------------------------
# GITHUB ACTIONS LOGS
# --------------------------------------------------

def get_run_logs(run_id):

    response = requests.get(
        f"{GITHUB_API}/repos/{GITHUB_REPOSITORY}/actions/runs/{run_id}/logs",
        headers=github_headers(),
        timeout=90,
        allow_redirects=True
    )

    if not response.ok:

        raise RuntimeError(
            f"Could not download Actions logs: HTTP {response.status_code}"
        )

    try:

        archive = zipfile.ZipFile(
            io.BytesIO(response.content)
        )

    except zipfile.BadZipFile:

        raise RuntimeError(
            "GitHub Actions logs response was not a ZIP archive"
        )

    chunks = []

    for name in archive.namelist():

        if name.lower().endswith((".txt", ".log")):

            try:

                chunks.append(
                    f"\n===== {name} =====\n"
                    + archive.read(name).decode(
                        "utf-8",
                        "replace"
                    )
                )

            except Exception:

                continue

    logs = "\n".join(chunks)

    if not logs:
        return "No text log files were found in the workflow archive."

    return logs[-MAX_LOG_CHARS:]


# --------------------------------------------------
# HTML CHECKER
# --------------------------------------------------

def run_checker(html_text, checker_bytes):

    with tempfile.TemporaryDirectory(
        prefix="6f-autofix-"
    ) as tmp:

        root = Path(tmp)

        # Create the target directory if it does not exist.
        # Required when TARGET_FILE is frontend/index.html.

        target_path = root / TARGET_FILE

        target_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        target_path.write_text(
            html_text,
            encoding="utf-8"
        )

        # Create checker directory if needed.

        checker_path = root / CHECKER_FILE

        checker_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        checker_path.write_bytes(checker_bytes)

        command = shlex.split(CHECK_COMMAND)

        if not command:

            raise RuntimeError(
                "CHECK_COMMAND is empty"
            )

        # Use Python from the current container.

        if command[0] == "python":

            command[0] = sys.executable

        result = subprocess.run(
            command,
            cwd=tmp,
            capture_output=True,
            text=True,
            timeout=90
        )

        output = (
            result.stdout + "\n" + result.stderr
        ).strip()

        return (
            result.returncode == 0,
            output or f"Checker exited with code {result.returncode}"
        )


# --------------------------------------------------
# BEDROCK RESPONSE PROCESSING
# --------------------------------------------------

def extract_html(text):

    text = text.strip()

    if text.startswith("```"):

        lines = text.splitlines()

        if lines and lines[0].startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        text = "\n".join(lines).strip()

    # Remove any model preamble before the HTML document.

    starts = [
        position
        for position in (
            text.lower().find("<!doctype"),
            text.lower().find("<html")
        )
        if position >= 0
    ]

    if starts:

        text = text[min(starts):]

    if "</html>" in text.lower():

        end = (
            text.lower().rfind("</html>")
            + len("</html>")
        )

        text = text[:end]

    if (
        "<html" not in text.lower()
        or "</html>" not in text.lower()
    ):

        raise RuntimeError(
            "Bedrock response did not contain a complete HTML document"
        )

    return text.rstrip() + "\n"


# --------------------------------------------------
# AMAZON BEDROCK AI REPAIR
# --------------------------------------------------

def ask_bedrock(
    original_html,
    checker_source,
    logs,
    previous_html="",
    validation_error=""
):

    prompt = f"""
You are the code-repair engine for the 6F School of IT static website.

Repair ONLY the HTML file supplied below.

The GitHub Actions workflow failed. Diagnose the failure using
the Actions logs and the repository's deterministic checker.

Requirements:

1. Preserve the existing website design.
2. Preserve existing content, links, scripts and behavior.
3. Do not remove required content merely to make validation pass.
4. Make only the necessary HTML corrections.
5. The supplied checker must pass.
6. Do not change infrastructure or workflow files.
7. Return ONLY the complete corrected HTML document.
8. Do not include explanations or Markdown code fences.

TARGET FILE:
{TARGET_FILE}

ACTIONS FAILURE LOGS:
{logs[-MAX_LOG_CHARS:]}

EXISTING CHECKER SOURCE ({CHECKER_FILE}):
{checker_source[:16000]}

ORIGINAL HTML:
{original_html[:50000]}

PREVIOUS ATTEMPT:
{previous_html[:50000]}

PREVIOUS VALIDATION ERROR:
{validation_error[:6000]}
"""

    client = boto3.client(
        "bedrock-runtime",
        region_name=AWS_REGION
    )

    response = client.converse(
        modelId=BEDROCK_MODEL_ID,
        messages=[
            {
                "role": "user",
                "content": [
                    {"text": prompt}
                ]
            }
        ],
        inferenceConfig={
            "maxTokens": 12000
        }
    )

    parts = (
        response
        .get("output", {})
        .get("message", {})
        .get("content", [])
    )

    answer = "\n".join(
        part.get("text", "")
        for part in parts
        if part.get("text")
    )

    if not answer.strip():

        raise RuntimeError(
            "Bedrock returned an empty response"
        )

    return extract_html(answer)


# --------------------------------------------------
# CREATE GITHUB PULL REQUEST
# --------------------------------------------------

def create_pr(
    run_id,
    head_sha,
    html_text,
    file_sha,
    run_url,
    validation_output
):

    branch = f"autofix/run-{run_id}"

    # Create a new repair branch.

    github_api(
        "POST",
        f"/repos/{GITHUB_REPOSITORY}/git/refs",
        json={
            "ref": f"refs/heads/{branch}",
            "sha": head_sha
        }
    )

    encoded_path = quote(
        TARGET_FILE,
        safe="/"
    )

    # Commit the corrected HTML file to the repair branch.

    github_api(
        "PUT",
        f"/repos/{GITHUB_REPOSITORY}/contents/{encoded_path}",
        json={
            "message": (
                f"fix: auto-repair {TARGET_FILE} "
                f"from failed workflow run {run_id}"
            ),
            "content": base64.b64encode(
                html_text.encode("utf-8")
            ).decode("ascii"),
            "sha": file_sha,
            "branch": branch
        }
    )

    # Create a pull request.
    # The agent never merges automatically.

    pr = github_api(
        "POST",
        f"/repos/{GITHUB_REPOSITORY}/pulls",
        json={
            "title": (
                f"AutoFix: repair {TARGET_FILE} "
                f"(run {run_id})"
            ),
            "head": branch,
            "base": BASE_BRANCH,
            "body": (
                "## Automated code repair\n\n"
                f"- Failed workflow run: {run_url}\n"
                f"- Target: `{TARGET_FILE}`\n"
                "- Validation: passed using the repository checker\n"
                "- Repair attempts: maximum two\n\n"
                "Please review the changes before merging. "
                "This PR was created by the EC2 AutoFix agent."
            )
        }
    )

    return pr.get("html_url", "")


# --------------------------------------------------
# PROCESS FAILED GITHUB ACTIONS RUN
# --------------------------------------------------

def process_run(run_id, run_data, run_url):

    update_job(
        run_id,
        "processing",
        "Downloading workflow logs"
    )

    logs = get_run_logs(run_id)

    head_sha = run_data.get("head_sha")

    if not head_sha:

        raise RuntimeError(
            "Workflow payload did not contain head_sha"
        )

    # Download target HTML and checker from the same commit.

    original_bytes, file_sha = get_repo_file(
        TARGET_FILE,
        head_sha
    )

    checker_bytes, _ = get_repo_file(
        CHECKER_FILE,
        head_sha
    )

    original_html = original_bytes.decode(
        "utf-8",
        "replace"
    )

    checker_source = checker_bytes.decode(
        "utf-8",
        "replace"
    )

    # First run the original checker.
    # Avoid AI repair if the original HTML already passes.

    update_job(
        run_id,
        "checking",
        "Running the original repository checker"
    )

    original_ok, original_result = run_checker(
        original_html,
        checker_bytes
    )

    if original_ok:

        update_job(
            run_id,
            "skipped",
            "Original checker passed; failure appears unrelated to the target HTML"
        )

        return

    candidate = ""

    validation_error = original_result

    # Maximum two AI repair attempts.

    for attempt in range(1, 3):

        update_job(
            run_id,
            "repairing",
            f"Bedrock repair attempt {attempt} of 2"
        )

        candidate = ask_bedrock(
            original_html,
            checker_source,
            logs,
            previous_html=candidate,
            validation_error=validation_error
        )

        valid, output = run_checker(
            candidate,
            checker_bytes
        )

        if valid:

            update_job(
                run_id,
                "validated",
                "Corrected HTML passed the original checker"
            )

            pr_url = create_pr(
                run_id,
                head_sha,
                candidate,
                file_sha,
                run_url,
                output
            )

            update_job(
                run_id,
                "pull_request",
                "Validated fix submitted for review",
                pr_url
            )

            return

        validation_error = output

    raise RuntimeError(
        "Both repair attempts failed validation: "
        f"{validation_error[:1000]}"
    )


# --------------------------------------------------
# PROCESS WEBHOOK EVENT
# --------------------------------------------------

def process_event(
    run_id,
    run_data,
    repository,
    run_url
):

    try:

        if repository != GITHUB_REPOSITORY:

            update_job(
                run_id,
                "ignored",
                "Repository does not match configured repository"
            )

            return

        if run_data.get("name") != WATCH_WORKFLOW_NAME:

            update_job(
                run_id,
                "ignored",
                "Workflow name does not match configured workflow"
            )

            return

        if run_data.get("conclusion") != "failure":

            update_job(
                run_id,
                "ignored",
                "Workflow did not conclude with failure"
            )

            return

        if run_data.get("head_branch") != BASE_BRANCH:

            update_job(
                run_id,
                "ignored",
                f"Only branch {BASE_BRANCH} is monitored"
            )

            return

        process_run(
            run_id,
            run_data,
            run_url
        )

    except Exception as exc:

        log.exception(
            "AutoFix failed for workflow run %s",
            run_id
        )

        update_job(
            run_id,
            "failed",
            str(exc)
        )


# --------------------------------------------------
# GITHUB WEBHOOK SIGNATURE VALIDATION
# --------------------------------------------------

def verify_signature(body, signature):

    if not WEBHOOK_SECRET or not signature:
        return False

    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(
        expected,
        signature
    )


# --------------------------------------------------
# HEALTH CHECK
# --------------------------------------------------

@app.get("/")
def home():

    return jsonify({
        "service": "6F AutoFix Agent",
        "status": "running",
        "role": ROLE
    })


@app.get("/health")
def health():

    return jsonify({
        "status": "ok",
        "role": ROLE
    })


# --------------------------------------------------
# DASHBOARD STATUS API
# --------------------------------------------------

@app.get("/api/status")
def status():

    if ROLE == "web":

        try:

            response = requests.get(
                f"{AGENT_URL}/api/status",
                timeout=10
            )

            return (
                response.content,
                response.status_code,
                {
                    "Content-Type": "application/json"
                }
            )

        except requests.RequestException:

            return jsonify({
                "error": "Agent container unavailable",
                "jobs": []
            }), 503

    init_db()

    with db() as conn:

        rows = conn.execute(
            """
            SELECT
                run_id,
                status,
                message,
                pr_url,
                run_url,
                created_at,
                updated_at
            FROM jobs
            ORDER BY created_at DESC
            LIMIT 30
            """
        ).fetchall()

    return jsonify({
        "jobs": [
            dict(row)
            for row in rows
        ]
    })


# --------------------------------------------------
# GITHUB WEBHOOK ENDPOINT
# --------------------------------------------------

@app.post("/webhook/github")
def github_webhook():

    body = request.get_data()

    # Optional web-role proxy support.

    if ROLE == "web":

        try:

            response = requests.post(
                f"{AGENT_URL}/webhook/github",
                data=body,
                headers={
                    "Content-Type": request.headers.get(
                        "Content-Type",
                        "application/json"
                    ),
                    "X-GitHub-Event": request.headers.get(
                        "X-GitHub-Event",
                        ""
                    ),
                    "X-GitHub-Delivery": request.headers.get(
                        "X-GitHub-Delivery",
                        ""
                    ),
                    "X-Hub-Signature-256": request.headers.get(
                        "X-Hub-Signature-256",
                        ""
                    )
                },
                timeout=10
            )

            return (
                response.content,
                response.status_code,
                {
                    "Content-Type": "application/json"
                }
            )

        except requests.RequestException:

            return jsonify({
                "error": "Could not reach AutoFix agent"
            }), 503

    # Verify GitHub webhook signature.

    if not verify_signature(
        body,
        request.headers.get(
            "X-Hub-Signature-256",
            ""
        )
    ):

        return jsonify({
            "error": "Invalid webhook signature"
        }), 401

    # Accept only workflow_run events.

    if request.headers.get("X-GitHub-Event") != "workflow_run":

        return jsonify({
            "message": "Event ignored"
        }), 202

    try:

        payload = json.loads(body)

        if payload.get("action") != "completed":

            return jsonify({
                "message": "Workflow event ignored"
            }), 202

        run_data = payload.get(
            "workflow_run",
            {}
        )

        run_id = str(
            run_data.get("id", "")
        )

        repository = payload.get(
            "repository",
            {}
        ).get(
            "full_name",
            ""
        )

        if not run_id:

            return jsonify({
                "error": "Missing workflow run ID"
            }), 400

        run_url = run_data.get(
            "html_url",
            ""
        )

        init_db()

        # Prevent duplicate processing of the same run.

        with db() as conn:

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    run_id,
                    status,
                    message,
                    run_url,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    "queued",
                    "Webhook accepted",
                    run_url,
                    now(),
                    now()
                )
            )

            inserted = cur.rowcount == 1

        # Process accepted events in a background thread.

        if inserted:

            threading.Thread(
                target=process_event,
                args=(
                    run_id,
                    run_data,
                    repository,
                    run_url
                ),
                daemon=True
            ).start()

        return jsonify({
            "message": "Accepted",
            "run_id": run_id,
            "duplicate": not inserted
        }), 202

    except Exception as exc:

        log.exception(
            "Invalid webhook payload"
        )

        return jsonify({
            "error": str(exc)
        }), 400


# --------------------------------------------------
# APPLICATION STARTUP
# --------------------------------------------------

if ROLE == "agent":

    init_db()

    log.info(
        "6F AutoFix Agent started. Repository=%s Target=%s Model=%s",
        GITHUB_REPOSITORY,
        TARGET_FILE,
        BEDROCK_MODEL_ID
    )


"""Run the guide's four examples against the public Pravdok OCR API.

Set OCR_API_TOKEN and optionally override OCR_API_URL, then run with Python.
Only the named demo files in results/ are replaced. No service is launched.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx


HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
ARTIFACTS = (
    "rendered.pdf", "document.json", "analysis.json",
    "translation-layer.json", "render-diagnostics.json",
)
TERMINAL = {"completed", "completed_with_warnings", "failed", "cancelled", "expired"}
OUTPUT_NAMES = {
    "run.json", "ocr-request.json", "ocr-response.json",
    "sr-en-request.json", "sr-en-response.json",
    "en-sr-request.json", "en-sr-response.json",
    "job-request.json", "job-created.json", "job-status.json", "job-timeline.json",
    *ARTIFACTS, *(name + ".error.json" for name in ARTIFACTS),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def descriptor(path: Path) -> dict:
    content = path.read_bytes()
    return {
        "file": path.name,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def returned_models(value, path: str = "$", found: list | None = None) -> list:
    """Collect model identities only where they actually occur in API JSON."""
    if found is None:
        found = []
    if isinstance(value, dict):
        identity = {key: value[key] for key in ("engine", "model", "ocr_engine", "ocr_model")
                    if key in value and value[key] is not None}
        if identity:
            found.append({"json_path": path, **identity})
        for key, child in value.items():
            returned_models(child, f"{path}.{key}", found)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            returned_models(child, f"{path}[{index}]", found)
    return found


class Examples:
    def __init__(self, base_url: str, token: str):
        self.token = token
        self.client = httpx.Client(
            base_url=base_url, headers={"Authorization": f"Bearer {token}"},
            follow_redirects=False,
        )
        self.manifest = {"started_at_utc": utc_now(), "api_url": base_url,
                         "sources": [], "examples": []}
        self.case: dict = {}

    def save_json(self, name: str, value) -> None:
        if name not in OUTPUT_NAMES:
            raise ValueError("Unexpected output filename")
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        (RESULTS / name).write_text(text.replace(self.token, "[REDACTED]"), encoding="utf-8")
        if name != "run.json" and name not in self.case.setdefault("output_files", []):
            self.case["output_files"].append(name)

    def request_json(self, method: str, endpoint: str, output: str, **kwargs) -> dict:
        response = self.client.request(method, endpoint, **kwargs)
        try:
            payload = response.json()
        except ValueError:
            payload = {"non_json_response": response.text}
        self.save_json(output, payload)
        self.case.setdefault("http_requests", []).append({
            "method": method, "endpoint": endpoint, "status_code": response.status_code,
            "response_file": output,
        })
        if not response.is_success:
            raise RuntimeError(f"HTTP {response.status_code}; see {output}")
        if not isinstance(payload, dict) or "non_json_response" in payload:
            raise RuntimeError(f"Expected a JSON object; see {output}")
        identities = returned_models(payload)
        if identities:
            self.case.setdefault("returned_models", []).extend(
                {"response_file": output, **item} for item in identities
            )
        return payload

    def run_case(self, case_id: str, operation) -> None:
        self.case = {"id": case_id, "started_at_utc": utc_now(), "status": "running",
                     "output_files": []}
        self.manifest["examples"].append(self.case)
        self.save_json("run.json", self.manifest)
        print(f"{case_id}: starting", flush=True)
        started = time.monotonic()
        try:
            operation()
            self.case["status"] = "passed"
        except (httpx.HTTPError, OSError, RuntimeError, ValueError, KeyError) as exc:
            self.case["status"] = "failed"
            # Transport/OS exception strings can contain local paths or credentials.
            self.case["error"] = (str(exc) if isinstance(exc, RuntimeError)
                                  else type(exc).__name__)
        finally:
            self.case["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self.case["outputs"] = [descriptor(RESULTS / name)
                                    for name in self.case["output_files"]]
            self.save_json("run.json", self.manifest)
        print(f"{case_id}: {self.case['status']} ({self.case['elapsed_seconds']} s)", flush=True)

    def ocr(self) -> None:
        params = {"task": "markdown", "include_markdown": "true",
                  "include_boxes": "false", "include_crops": "false"}
        self.case["request"] = {"method": "POST", "endpoint": "/ocr",
                                "file": "sample-serbian.png", "media_type": "image/png",
                                "form": params}
        self.save_json("ocr-request.json", self.case["request"])
        with (HERE / "sample-serbian.png").open("rb") as source:
            payload = self.request_json(
                "POST", "/ocr", "ocr-response.json", timeout=600,
                files={"file": ("sample-serbian.png", source, "image/png")}, data=params,
            )
        if not str(payload.get("text", "")).strip():
            raise RuntimeError("OCR returned empty text")
        if payload.get("engine") == "stub":
            raise RuntimeError("The API returned a stub engine; this is not live OCR evidence")

    def translate(self, name: str, source_language: str, target_language: str, text: str) -> None:
        request = {"source_language": source_language, "target_language": target_language,
                   "segments": [{"element_id": "message_1", "text": text, "policy": "translate"}]}
        self.case["request"] = {"method": "POST", "endpoint": "/v1/translations", "json": request}
        self.save_json(f"{name}-request.json", request)
        payload = self.request_json("POST", "/v1/translations", f"{name}-response.json",
                                    timeout=180, json=request)
        segments = payload.get("target_layer", {}).get("segments", [])
        if not any(segment.get("element_id") == "message_1" and segment.get("text")
                   for segment in segments):
            raise RuntimeError("Translation did not return the requested nonempty segment")
        if payload.get("engine") == "stub":
            raise RuntimeError("The API returned a stub engine; this is not live translation evidence")

    def document_job(self) -> None:
        params = {"extraction_mode": "ai", "language_hint": "sr-Cyrl",
                  "source_language": "sr-Cyrl", "target_language": "en",
                  "target_layer_id": "english", "render": "true", "render_mode": "auto"}
        self.case["request"] = {"method": "POST", "endpoint": "/v1/document-jobs",
                                "file": "sample-serbian.pdf", "media_type": "application/pdf",
                                "form": params}
        self.save_json("job-request.json", self.case["request"])
        with (HERE / "sample-serbian.pdf").open("rb") as source:
            job = self.request_json(
                "POST", "/v1/document-jobs", "job-created.json", timeout=60,
                files={"file": ("sample-serbian.pdf", source, "application/pdf")}, data=params,
            )
        job_id = job["job_id"]
        self.case["job_id"] = job_id
        endpoint = f"/v1/document-jobs/{quote(str(job_id), safe='')}"
        started = time.monotonic()
        deadline = started + 12 * 60
        timeline = []
        previous = None
        while True:
            observation = {"elapsed_seconds": round(time.monotonic() - started, 3),
                           "state": job.get("state"), "progress": job.get("progress"),
                           "current_stage": job.get("current_stage"),
                           "warning_count": job.get("warning_count"),
                           "error_code": job.get("error_code")}
            timeline.append(observation)
            self.save_json("job-timeline.json", timeline)
            self.save_json("job-status.json", job)
            current = (job.get("state"), job.get("current_stage"))
            if current != previous:
                print(f"document-job: {current[0]} / {current[1]} ({job.get('progress')}%)", flush=True)
                previous = current
            if job.get("state") in TERMINAL:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Polling exceeded 12 minutes; use job_id to poll again")
            time.sleep(min(2, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Polling exceeded 12 minutes; use job_id to poll again")
            job = self.request_json("GET", endpoint, "job-status.json", timeout=min(60, remaining))
        self.case["job_state"] = job.get("state")
        self.case["warning_count"] = job.get("warning_count")
        self.case["artifacts"] = []
        for artifact in job.get("artifacts", []):
            name = artifact.get("name")
            if name not in ARTIFACTS:
                continue
            response = self.client.get(f"{endpoint}/artifacts/{name}", timeout=60)
            if not response.is_success:
                try:
                    error = response.json()
                except ValueError:
                    error = {"non_json_response": response.text}
                self.save_json(name + ".error.json", error)
                raise RuntimeError(f"Artifact {name}: HTTP {response.status_code}")
            digest = hashlib.sha256(response.content).hexdigest()
            if (digest != artifact.get("sha256")
                    or digest != response.headers.get("X-Artifact-SHA256")
                    or len(response.content) != artifact.get("size")):
                raise RuntimeError(f"Artifact {name} failed SHA-256 or size verification")
            if self.token.encode() in response.content:
                raise RuntimeError(f"Artifact {name} unexpectedly contains the API token; not saved")
            (RESULTS / name).write_bytes(response.content)
            self.case["output_files"].append(name)
            self.case["artifacts"].append({**descriptor(RESULTS / name), "hash_verified": True})
            if name.endswith(".json"):
                self.case.setdefault("returned_models", []).extend(
                    {"response_file": name, **item} for item in returned_models(response.json())
                )
        if job.get("state") not in {"completed", "completed_with_warnings"}:
            raise RuntimeError(f"Document job ended in {job.get('state')}; see job-status.json")
        if "rendered.pdf" not in self.case["output_files"]:
            raise RuntimeError("Completed document job did not provide rendered.pdf")


def main() -> int:
    token = os.environ.get("OCR_API_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set OCR_API_TOKEN before running the examples.")
    base_url = os.environ.get("OCR_API_URL", "https://pravdokapi.jerteh.rs").rstrip("/")
    url = httpx.URL(base_url)
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query or url.fragment:
        raise SystemExit("OCR_API_URL must be an HTTP(S) URL without credentials, query, or fragment.")
    if url.scheme == "http" and url.host.lower() != "localhost":
        try:
            is_loopback = ipaddress.ip_address(url.host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise SystemExit("Use HTTPS for an API on another computer.")
    for name in ("sample-serbian.png", "sample-serbian.pdf"):
        if not (HERE / name).is_file():
            raise SystemExit(f"Missing sibling input: {name}")
    RESULTS.mkdir(exist_ok=True)
    for name in OUTPUT_NAMES:
        (RESULTS / name).unlink(missing_ok=True)
    examples = Examples(base_url, token)
    examples.manifest["sources"] = [descriptor(HERE / name)
                                    for name in ("sample-serbian.png", "sample-serbian.pdf")]
    print(f"API: {base_url}; writing results/", flush=True)
    started = time.monotonic()
    try:
        examples.run_case("ocr", examples.ocr)
        examples.run_case("sr-en", lambda: examples.translate(
            "sr-en", "sr-Cyrl", "en", "Молим вас да потврдите пријем документа до петка."))
        examples.run_case("en-sr", lambda: examples.translate(
            "en-sr", "en", "sr-Latn", "Please send the signed form by Friday."))
        examples.run_case("document-job", examples.document_job)
    finally:
        examples.client.close()
        examples.manifest["finished_at_utc"] = utc_now()
        examples.manifest["elapsed_seconds"] = round(time.monotonic() - started, 3)
        examples.save_json("run.json", examples.manifest)
    return int(any(case["status"] != "passed" for case in examples.manifest["examples"]))


if __name__ == "__main__":
    sys.exit(main())

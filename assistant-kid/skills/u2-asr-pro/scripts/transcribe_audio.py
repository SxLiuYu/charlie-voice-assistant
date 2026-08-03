#!/usr/bin/env python3
import argparse
import json
import mimetypes
import os
import sys
import time
import uuid
from pathlib import Path
from urllib import error, parse, request


BASE_URL = "https://maas-api.unisound.com"
UPLOAD_URL = f"{BASE_URL}/v1/files/upload"
TASK_URL = f"{BASE_URL}/v1/audio/asr/tasks"
SUPPORTED_AUDIO_FORMATS = {"mp3", "opus", "wav", "amr", "m4a", "ogg"}
SUPPORTED_LANGUAGES = {
    "zh-CN",
    "en-US",
    "ar-SA",
    "de-DE",
    "es-MX",
    "fr-FR",
    "id-ID",
    "ja-JP",
    "ko-KR",
    "pt-BR",
    "ru-RU",
    "tr-TR",
    "vi-VN",
    "th-TH",
    "it-IT",
}
MAX_FILE_SIZE_BYTES = 1024**3


class SkillError(Exception):
    pass


def load_env_file(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}

    values = {}
    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise SkillError(f"Invalid .env entry at {env_path}:{line_number}")

        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def resolve_api_key(env_values: dict) -> str:
    token_plan_key = (
        os.environ.get("U2_ASR_TOKEN_PLAN_API_KEY")
        or env_values.get("U2_ASR_TOKEN_PLAN_API_KEY")
        or ""
    ).strip()
    platform_key = (
        os.environ.get("U2_ASR_PLATFORM_API_KEY")
        or env_values.get("U2_ASR_PLATFORM_API_KEY")
        or ""
    ).strip()

    if token_plan_key:
        return token_plan_key
    if platform_key:
        return platform_key

    raise SkillError(
        "No usable API key found. Set U2_ASR_TOKEN_PLAN_API_KEY or "
        "U2_ASR_PLATFORM_API_KEY in the environment or .env.local. "
        "Priority: Token Plan > platform API key."
    )


def http_json(url: str, method: str, headers: dict, body: bytes | None = None) -> dict:
    req = request.Request(url=url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SkillError(f"HTTP {exc.code} for {url}: {detail}") from exc
    except error.URLError as exc:
        raise SkillError(f"Request failed for {url}: {exc}") from exc


def ensure_api_success(response_data: dict, operation: str) -> None:
    base_resp = response_data.get("base_resp") or {}
    status_code = base_resp.get("status_code")
    if status_code in (None, 0):
        return

    status_msg = base_resp.get("status_msg") or "Unknown API error"
    raise SkillError(
        f"{operation} failed with status_code={status_code}: {status_msg}. "
        f"Response: {json.dumps(response_data, ensure_ascii=False)}"
    )


def build_multipart_form(
    fields: dict[str, str], file_field: str, file_path: Path
) -> tuple[bytes, str]:
    boundary = f"----CodexBoundary{uuid.uuid4().hex}"
    lines: list[bytes] = []

    for name, value in fields.items():
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        lines.append(b"")
        lines.append(value.encode("utf-8"))

    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    file_bytes = file_path.read_bytes()

    lines.append(f"--{boundary}".encode())
    lines.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"'.encode()
    )
    lines.append(f"Content-Type: {mime_type}".encode())
    lines.append(b"")
    lines.append(file_bytes)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")

    body = b"\r\n".join(lines)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def detect_format_from_file_path(file_path: Path) -> str:
    suffix = file_path.suffix.lower().lstrip(".")
    if suffix in SUPPORTED_AUDIO_FORMATS:
        return suffix

    raise SkillError(
        f"Unable to infer a supported audio format from: {file_path}. "
        f"Supported formats: {', '.join(sorted(SUPPORTED_AUDIO_FORMATS))}."
    )


def detect_format_from_url(file_url: str) -> str:
    suffix = Path(parse.urlparse(file_url).path).suffix.lower().lstrip(".")
    if suffix in SUPPORTED_AUDIO_FORMATS:
        return suffix

    raise SkillError(
        f"Unable to infer a supported audio format from URL: {file_url}. "
        "Pass --format explicitly."
    )


def upload_file(api_key: str, file_path: Path) -> dict:
    body, content_type = build_multipart_form(
        fields={"purpose": "a2t_async_input"},
        file_field="file",
        file_path=file_path,
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": content_type,
    }
    response_data = http_json(UPLOAD_URL, "POST", headers, body)
    ensure_api_success(response_data, "File upload")
    return response_data


def create_task(
    api_key: str,
    file_id: str | None,
    file_url: str | None,
    model: str,
    audio_format: str,
    sample_rate: int,
    enable_auto_lang: bool,
    language: str | None,
    enable_itn: bool,
    channel: int,
    enable_speaker: bool,
    speaker_num: int | None,
    speaker_ids: list[str] | None,
    word_info: bool,
    context: str | None,
    hotwords: list[str] | None,
) -> dict:
    payload = {
        "model": model,
        "format": audio_format,
        "sample_rate": sample_rate,
        "enable_auto_lang": enable_auto_lang,
        "enable_itn": enable_itn,
        "channel": channel,
        "enable_speaker": enable_speaker,
        "word_info": word_info,
    }
    if file_id is not None:
        payload["file_id"] = int(file_id)
    if file_url is not None:
        payload["file_url"] = file_url
    if language:
        payload["language"] = language
    if enable_speaker and speaker_num is not None:
        payload["speaker_num"] = speaker_num
    if enable_speaker and speaker_ids:
        payload["speaker_ids"] = speaker_ids
    if context:
        payload["context"] = context
    if hotwords:
        payload["hotwords"] = hotwords

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    response_data = http_json(TASK_URL, "POST", headers, body)
    ensure_api_success(response_data, "Create ASR task")
    return response_data


def get_task(api_key: str, task_id: str) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
    }
    response_data = http_json(f"{TASK_URL}/{parse.quote(str(task_id))}", "GET", headers)
    ensure_api_success(response_data, "Get ASR task")
    return response_data


def extract_file_id(upload_resp: dict) -> str:
    file_info = upload_resp.get("file") or {}
    file_id = file_info.get("file_id")
    if not file_id:
        raise SkillError(
            f"Upload succeeded but file_id is missing: "
            f"{json.dumps(upload_resp, ensure_ascii=False)}"
        )
    return str(file_id)


def extract_task_id(task_resp: dict) -> str:
    if task_resp.get("task_id"):
        return str(task_resp["task_id"])

    task_info = task_resp.get("task") or {}
    if task_info.get("task_id"):
        return str(task_info["task_id"])

    raise SkillError(
        f"Task created but task_id is missing: {json.dumps(task_resp, ensure_ascii=False)}"
    )


def detect_status(task_resp: dict) -> str:
    candidates = [
        task_resp.get("status"),
        (task_resp.get("task") or {}).get("status"),
        (task_resp.get("data") or {}).get("status"),
    ]
    for item in candidates:
        if item:
            return str(item).lower()
    return "unknown"


def is_terminal_success(status: str) -> bool:
    return status in {"success", "succeeded", "completed", "done", "finished"}


def is_terminal_failure(status: str) -> bool:
    return status in {"failed", "error", "cancelled", "canceled"}


def poll_task(api_key: str, task_id: str, poll_interval: int, timeout_seconds: int) -> dict:
    deadline = time.time() + timeout_seconds
    last_resp = {}

    while time.time() < deadline:
        last_resp = get_task(api_key, task_id)
        status = detect_status(last_resp)

        if is_terminal_success(status):
            return last_resp
        if is_terminal_failure(status):
            raise SkillError(
                f"Task ended in failure state '{status}': "
                f"{json.dumps(last_resp, ensure_ascii=False)}"
            )

        time.sleep(poll_interval)

    raise SkillError(
        f"Polling timed out after {timeout_seconds}s. "
        f"Last response: {json.dumps(last_resp, ensure_ascii=False)}"
    )


def join_transcript_text(final_task_resp: dict) -> str:
    results = final_task_resp.get("results") or []
    parts = []
    for item in results:
        text = (item.get("text") or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def build_transcript_preview(text: str, max_chars: int = 280) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def build_result_summary(
    file_path: Path | None,
    file_url: str | None,
    file_id: str | None,
    task_id: str,
    final_task_response: dict,
) -> dict:
    transcript_text = join_transcript_text(final_task_response)
    summary = {
        "file_path": str(file_path) if file_path else None,
        "file_url": file_url,
        "file_id": file_id,
        "task_id": task_id,
        "status": final_task_response.get("status"),
        "duration": final_task_response.get("duration"),
        "result_count": len(final_task_response.get("results") or []),
    }
    if transcript_text:
        summary["transcript_preview"] = build_transcript_preview(transcript_text)
    return summary


def render_json_for_output(data: dict, encoding: str | None) -> str:
    pretty = json.dumps(data, ensure_ascii=False, indent=2)
    if not encoding:
        return pretty

    try:
        pretty.encode(encoding)
        return pretty
    except UnicodeEncodeError:
        return json.dumps(data, ensure_ascii=True, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit local audio or a public URL for asynchronous transcription."
    )
    parser.add_argument("file_path", nargs="?", help="Path to the audio file to transcribe")
    parser.add_argument(
        "--file-url",
        default=None,
        help="Public HTTP(S) audio URL. Mutually exclusive with file_path.",
    )
    parser.add_argument("--model", default="u2-asr", help="ASR model name")
    parser.add_argument(
        "--format",
        dest="audio_format",
        default=None,
        help="Audio format. If omitted, infer from the file extension.",
    )
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument("--enable-auto-lang", action="store_true", default=False)
    parser.add_argument("--language", choices=sorted(SUPPORTED_LANGUAGES), default=None)

    itn_group = parser.add_mutually_exclusive_group()
    itn_group.add_argument("--enable-itn", dest="enable_itn", action="store_true")
    itn_group.add_argument("--disable-itn", dest="enable_itn", action="store_false")
    parser.set_defaults(enable_itn=True)

    parser.add_argument("--enable-speaker", action="store_true", default=False)
    parser.add_argument("--speaker-num", type=int, default=None)
    parser.add_argument(
        "--speaker-id",
        action="append",
        default=None,
        help="Repeatable registered voiceprint ID. Requires --enable-speaker.",
    )
    parser.add_argument("--word-info", action="store_true", default=False)
    parser.add_argument("--context", default=None)
    parser.add_argument(
        "--hotword",
        action="append",
        default=None,
        help="Repeatable hotword value, maximum 5 characters each.",
    )
    parser.add_argument("--poll-interval-seconds", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).resolve().parent.parent / ".env.local"),
        help="Path to local environment file containing API keys",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace, file_path: Path | None) -> None:
    if bool(file_path) == bool(args.file_url):
        raise SkillError("Provide exactly one of file_path or --file-url.")
    if args.file_url:
        parsed_url = parse.urlparse(args.file_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise SkillError("--file-url must be a valid public HTTP(S) URL.")
        if len(args.file_url) > 2048:
            raise SkillError("--file-url must not exceed 2048 characters.")
    if file_path and file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
        raise SkillError("Input file exceeds the API limit of 1GB.")
    if args.sample_rate <= 0:
        raise SkillError("--sample-rate must be greater than zero.")
    if args.channel not in {1, 2}:
        raise SkillError("--channel must be 1 (mono) or 2 (stereo).")
    if args.enable_speaker and args.channel != 1:
        raise SkillError("--enable-speaker is supported only with --channel 1.")
    if args.speaker_num is not None and args.speaker_num <= 0:
        raise SkillError("--speaker-num must be greater than zero.")
    if (args.speaker_num is not None or args.speaker_id) and not args.enable_speaker:
        raise SkillError("--speaker-num and --speaker-id require --enable-speaker.")
    if args.speaker_id and len(args.speaker_id) > 10:
        raise SkillError("At most 10 --speaker-id values are supported.")
    if args.context and len(args.context) > 500:
        raise SkillError("--context must not exceed 500 characters.")
    if args.hotword:
        if len(args.hotword) > 200:
            raise SkillError("At most 200 --hotword values are supported.")
        too_long = [word for word in args.hotword if len(word) > 5]
        if too_long:
            raise SkillError("Each --hotword value must not exceed 5 characters.")
    if args.poll_interval_seconds <= 0:
        raise SkillError("--poll-interval-seconds must be greater than zero.")
    if args.timeout_seconds <= 0:
        raise SkillError("--timeout-seconds must be greater than zero.")


def main() -> int:
    args = parse_args()

    file_path = Path(args.file_path).expanduser().resolve() if args.file_path else None
    if file_path and (not file_path.exists() or not file_path.is_file()):
        raise SkillError(f"Input file not found: {file_path}")
    validate_args(args, file_path)

    env_path = Path(args.env_file).expanduser().resolve()
    env_values = load_env_file(env_path)
    api_key = resolve_api_key(env_values)

    audio_format = (args.audio_format or "").lower().lstrip(".")
    if not audio_format:
        audio_format = (
            detect_format_from_file_path(file_path)
            if file_path
            else detect_format_from_url(args.file_url)
        )
    if audio_format not in SUPPORTED_AUDIO_FORMATS:
        raise SkillError(
            f"Unsupported audio format '{audio_format}'. "
            f"Supported formats: {', '.join(sorted(SUPPORTED_AUDIO_FORMATS))}."
        )

    upload_resp = None
    file_id = None
    if file_path:
        upload_resp = upload_file(api_key, file_path)
        file_id = extract_file_id(upload_resp)

    task_resp = create_task(
        api_key=api_key,
        file_id=file_id,
        file_url=args.file_url,
        model=args.model,
        audio_format=audio_format,
        sample_rate=args.sample_rate,
        enable_auto_lang=args.enable_auto_lang,
        language=args.language,
        enable_itn=args.enable_itn,
        channel=args.channel,
        enable_speaker=args.enable_speaker,
        speaker_num=args.speaker_num,
        speaker_ids=args.speaker_id,
        word_info=args.word_info,
        context=args.context,
        hotwords=args.hotword,
    )
    task_id = extract_task_id(task_resp)

    final_task_resp = poll_task(
        api_key=api_key,
        task_id=task_id,
        poll_interval=args.poll_interval_seconds,
        timeout_seconds=args.timeout_seconds,
    )

    result = {
        "summary": build_result_summary(
            file_path=file_path,
            file_url=args.file_url,
            file_id=file_id,
            task_id=task_id,
            final_task_response=final_task_resp,
        ),
        "file_path": str(file_path) if file_path else None,
        "file_url": args.file_url,
        "file_id": file_id,
        "task_id": task_id,
        "audio_format": audio_format,
        "upload_response": upload_resp,
        "create_task_response": task_resp,
        "final_task_response": final_task_resp,
    }
    print(render_json_for_output(result, sys.stdout.encoding))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SkillError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)

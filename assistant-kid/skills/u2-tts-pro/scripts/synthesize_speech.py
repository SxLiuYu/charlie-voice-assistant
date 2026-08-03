#!/usr/bin/env python3
import argparse
import json
import mimetypes
import sys
import time
import uuid
from pathlib import Path
from urllib import error, parse, request


BASE_URL = "https://maas-api.hivoice.cn"
UPLOAD_URL = f"{BASE_URL}/v1/files/upload"
VOICE_QUERY_URL = f"{BASE_URL}/v1/audio/voices/query"
TASK_URL = f"{BASE_URL}/v1/audio/speech/tasks"
DOWNLOAD_URL = f"{BASE_URL}/v1/files/retrieve_content"
SUPPORTED_SAMPLE_RATES = {8000, 16000, 24000, 32000}
SUPPORTED_OUTPUT_FORMATS = {"mp3", "pcm"}
SUPPORTED_EMOTIONS = {"happy", "angry", "depressed", "whisper", "loudly", "neutral"}
SUPPORTED_CHANNELS = {1}
TEXT_FILE_PURPOSE = "t2a_async_input"
MAX_TEXT_CHARS_BY_MODEL = {
    "u2-tts": 50000,
    "u2-tts-clone": 20000,
}


class SkillError(Exception):
    pass


def load_api_keys(config_path: Path) -> dict:
    if not config_path.exists():
        raise SkillError(
            f"API key config not found: {config_path}. "
            "Please create the file or ask the user for an API key first."
        )

    try:
        return json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SkillError(f"Invalid JSON in API key config: {config_path}") from exc


def resolve_api_key(keys: dict) -> str:
    token_plan_key = (keys.get("token_plan_api_key") or "").strip()
    platform_key = (keys.get("platform_api_key") or "").strip()

    if token_plan_key:
        return token_plan_key
    if platform_key:
        return platform_key

    raise SkillError(
        "No usable API key found. Ask the user for an API key first. "
        "Priority: token_plan_api_key > platform_api_key."
    )


def validate_text_inputs(text: str | None, text_file_path: Path | None) -> None:
    if text and text_file_path:
        raise SkillError("Provide either text or text_file_path, not both.")
    if not text and not text_file_path:
        raise SkillError("Either text or text_file_path is required.")


def validate_text_file(text_file_path: Path) -> dict:
    if text_file_path.suffix.lower() != ".txt":
        raise SkillError(f"Text input file must be a .txt file: {text_file_path}")

    content = text_file_path.read_text(encoding="utf-8-sig")
    if not content.strip():
        raise SkillError(f"Text input file must not be empty: {text_file_path}")

    return {
        "characters": len(content),
        "content": content,
    }


def validate_settings(
    speed: int,
    volume: int,
    pitch: int,
    bright: int,
    audio_sample_rate: int,
    output_format: str,
    channel: int,
    emotion: str | None,
) -> None:
    for name, value in {
        "speed": speed,
        "volume": volume,
        "pitch": pitch,
        "bright": bright,
    }.items():
        if not 0 <= value <= 100:
            raise SkillError(f"{name} must be between 0 and 100. Got {value}.")

    if audio_sample_rate not in SUPPORTED_SAMPLE_RATES:
        raise SkillError(
            f"audio_sample_rate must be one of {sorted(SUPPORTED_SAMPLE_RATES)}. "
            f"Got {audio_sample_rate}."
        )
    if output_format not in SUPPORTED_OUTPUT_FORMATS:
        raise SkillError(
            f"format must be one of {sorted(SUPPORTED_OUTPUT_FORMATS)}. Got {output_format}."
        )
    if channel not in SUPPORTED_CHANNELS:
        raise SkillError(f"channel must be 1. Got {channel}.")
    if emotion and emotion not in SUPPORTED_EMOTIONS:
        raise SkillError(
            f"emotion must be one of {sorted(SUPPORTED_EMOTIONS)}. Got {emotion}."
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


def http_download(url: str, headers: dict) -> bytes:
    req = request.Request(url=url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=120) as resp:
            return resp.read()
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


def upload_text_file(api_key: str, file_path: Path) -> dict:
    body, content_type = build_multipart_form(
        fields={"purpose": TEXT_FILE_PURPOSE},
        file_field="file",
        file_path=file_path,
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": content_type,
    }
    response_data = http_json(UPLOAD_URL, "POST", headers, body)
    ensure_api_success(response_data, f"Upload file for purpose={TEXT_FILE_PURPOSE}")
    return response_data


def extract_file_id(response_data: dict) -> str:
    if response_data.get("file_id"):
        return str(response_data["file_id"])

    file_info = response_data.get("file") or {}
    if file_info.get("file_id"):
        return str(file_info["file_id"])

    raise SkillError(
        f"Response is missing file_id: {json.dumps(response_data, ensure_ascii=False)}"
    )


def query_voices(api_key: str, voice_type: str = "all") -> dict:
    body = json.dumps({"voice_type": voice_type}).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    response_data = http_json(VOICE_QUERY_URL, "POST", headers, body)
    ensure_api_success(response_data, "Query voices")
    return response_data


def resolve_voice_selection(voice_query: dict, voice_id: str) -> dict:
    for voice in voice_query.get("system_voice") or []:
        if voice.get("voice_id") == voice_id:
            return {
                "voice_id": voice_id,
                "voice_type": "system",
                "model": "u2-tts",
                "voice_info": voice,
            }

    for voice in voice_query.get("voice_cloning") or []:
        if voice.get("voice_id") == voice_id:
            return {
                "voice_id": voice_id,
                "voice_type": "voice_cloning",
                "model": "u2-tts-clone",
                "voice_info": voice,
            }

    raise SkillError(
        f"voice_id '{voice_id}' was not found in available voices. "
        "Query voices first and choose one of the returned IDs."
    )


def validate_text_length(model: str, text: str | None, text_file_info: dict | None) -> None:
    max_chars = MAX_TEXT_CHARS_BY_MODEL[model]
    length = len(text) if text is not None else text_file_info["characters"]
    if length > max_chars:
        raise SkillError(
            f"Input text exceeds the {max_chars} character limit for model {model}. "
            f"Got {length} characters."
        )


def build_task_payload(
    model: str,
    text: str | None,
    text_file_id: str | None,
    voice_setting: dict,
    audio_setting: dict,
    tone_rules: list[str] | None,
) -> dict:
    payload = {
        "model": model,
        "voice_setting": voice_setting,
        "audio_setting": audio_setting,
    }
    if text is not None:
        payload["text"] = text
    if text_file_id is not None:
        payload["text_file_id"] = int(text_file_id)
    if tone_rules:
        payload["pronunciation_dict"] = {"tone": tone_rules}
    return payload


def create_task(api_key: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json;charset=UTF-8",
    }
    response_data = http_json(TASK_URL, "POST", headers, body)
    ensure_api_success(response_data, "Create speech task")
    return response_data


def get_task(api_key: str, task_id: str) -> dict:
    url = f"{TASK_URL}?task_id={parse.quote(str(task_id))}"
    headers = {"Authorization": f"Bearer {api_key}"}
    response_data = http_json(url, "GET", headers)
    ensure_api_success(response_data, "Get speech task")
    return response_data


def detect_status(task_resp: dict) -> str:
    status = task_resp.get("status")
    if status:
        return str(status).lower()
    return "unknown"


def is_terminal_success(status: str) -> bool:
    return status in {"success", "succeeded", "completed", "done", "finished"}


def is_terminal_failure(status: str) -> bool:
    return status in {"failed", "error", "cancelled", "canceled"}


def build_poll_timeout_result(
    task_id: str,
    last_resp: dict,
    poll_interval: int,
    timeout_seconds: int,
) -> dict:
    result = dict(last_resp or {})
    result.setdefault("task_id", task_id)
    result.setdefault("status", "Unknown")
    result["polling"] = {
        "timed_out": True,
        "poll_interval_seconds": poll_interval,
        "timeout_seconds": timeout_seconds,
        "message": (
            "Polling timed out before the task reached a terminal state. "
            "You can continue checking this task later with the returned task_id."
        ),
    }
    return result


def is_poll_timeout_response(task_resp: dict) -> bool:
    return bool((task_resp.get("polling") or {}).get("timed_out"))


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

    return build_poll_timeout_result(
        task_id=task_id,
        last_resp=last_resp,
        poll_interval=poll_interval,
        timeout_seconds=timeout_seconds,
    )


def build_default_output_path(task_id: str, output_format: str, base_dir: Path) -> Path:
    return base_dir / f"{task_id}.{output_format}"


def download_file(api_key: str, file_id: str, output_path: Path) -> dict:
    url = f"{DOWNLOAD_URL}?file_id={parse.quote(str(file_id))}"
    headers = {"Authorization": f"Bearer {api_key}"}
    content = http_download(url, headers)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    return {
        "output_path": str(output_path),
        "bytes": len(content),
    }


def summarize_available_voices(voice_query: dict) -> dict:
    return {
        "system_voice_count": len(voice_query.get("system_voice") or []),
        "voice_cloning_count": len(voice_query.get("voice_cloning") or []),
        "system_voice": voice_query.get("system_voice") or [],
        "voice_cloning": voice_query.get("voice_cloning") or [],
    }


def build_result_summary(
    selection: dict,
    create_task_response: dict,
    final_task_response: dict,
    download_metadata: dict | None,
) -> dict:
    return {
        "voice_id": selection["voice_id"],
        "voice_type": selection["voice_type"],
        "model": selection["model"],
        "task_id": str(create_task_response.get("task_id")),
        "file_id": str(final_task_response.get("file_id") or create_task_response.get("file_id")),
        "status": final_task_response.get("status"),
        "timed_out": is_poll_timeout_response(final_task_response),
        "usage_characters": create_task_response.get("usage_characters"),
        "downloaded_file_path": download_metadata.get("output_path") if download_metadata else None,
        "downloaded_bytes": download_metadata.get("bytes") if download_metadata else None,
    }


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
        description="Discover voices and synthesize speech asynchronously."
    )
    parser.add_argument("--voice-id", default=None, help="Voice ID to use for synthesis")
    parser.add_argument("--text", default=None, help="Direct text input")
    parser.add_argument("--text-file-path", default=None, help="Path to a .txt file")
    parser.add_argument("--speed", type=int, default=50)
    parser.add_argument("--volume", type=int, default=50)
    parser.add_argument("--pitch", type=int, default=50)
    parser.add_argument("--bright", type=int, default=50)
    parser.add_argument("--emotion", default=None)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--dialect", default=None)
    parser.add_argument("--audio-sample-rate", type=int, default=32000)
    parser.add_argument("--format", dest="output_format", default="mp3")
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument("--tone-rule", action="append", default=None)
    parser.add_argument("--output-path", default=None)
    parser.add_argument("--poll-interval-seconds", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent.parent / "config" / "api-keys.json"),
        help="Path to the API key config JSON file",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    text_file_path = None
    if args.text_file_path:
        text_file_path = Path(args.text_file_path).expanduser().resolve()
        if not text_file_path.exists() or not text_file_path.is_file():
            raise SkillError(f"Input file not found: {text_file_path}")

    config_path = Path(args.config).expanduser().resolve()
    keys = load_api_keys(config_path)
    api_key = resolve_api_key(keys)

    voice_query_response = query_voices(api_key, voice_type="all")
    available_voices = summarize_available_voices(voice_query_response)

    if not args.voice_id:
        result = {
            "selection_required": True,
            "message": "No voice_id provided. Choose one of the available voices and rerun.",
            "available_voices": available_voices,
            "query_voices_response": voice_query_response,
        }
        print(render_json_for_output(result, sys.stdout.encoding))
        return 0

    validate_text_inputs(args.text, text_file_path)
    validate_settings(
        speed=args.speed,
        volume=args.volume,
        pitch=args.pitch,
        bright=args.bright,
        audio_sample_rate=args.audio_sample_rate,
        output_format=args.output_format,
        channel=args.channel,
        emotion=args.emotion,
    )

    text_file_info = None
    if text_file_path:
        text_file_info = validate_text_file(text_file_path)

    selection = resolve_voice_selection(voice_query_response, args.voice_id)
    validate_text_length(selection["model"], args.text, text_file_info)

    text_upload_response = None
    text_file_id = None
    if text_file_path:
        text_upload_response = upload_text_file(api_key, text_file_path)
        text_file_id = extract_file_id(text_upload_response)

    voice_setting = {
        "voice_id": selection["voice_id"],
        "speed": args.speed,
        "volume": args.volume,
        "pitch": args.pitch,
        "bright": args.bright,
        "language": args.language,
    }
    if args.emotion:
        voice_setting["emotion"] = args.emotion
    if args.dialect:
        voice_setting["dialect"] = args.dialect

    audio_setting = {
        "audio_sample_rate": args.audio_sample_rate,
        "format": args.output_format,
        "channel": args.channel,
    }

    payload = build_task_payload(
        model=selection["model"],
        text=args.text,
        text_file_id=text_file_id,
        voice_setting=voice_setting,
        audio_setting=audio_setting,
        tone_rules=args.tone_rule,
    )
    create_task_response = create_task(api_key, payload)
    task_id = str(create_task_response.get("task_id"))
    final_task_response = poll_task(
        api_key=api_key,
        task_id=task_id,
        poll_interval=args.poll_interval_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    if is_poll_timeout_response(final_task_response):
        result = {
            "summary": build_result_summary(
                selection=selection,
                create_task_response=create_task_response,
                final_task_response=final_task_response,
                download_metadata=None,
            ),
            "voice_selection": selection,
            "query_voices_response": voice_query_response,
            "available_voices": available_voices,
            "text_upload_response": text_upload_response,
            "create_task_response": create_task_response,
            "final_task_response": final_task_response,
            "download_metadata": None,
            "task_payload": payload,
            "resume_hint": (
                "The task is still running. Re-query this task_id later to get the final "
                "status and download file_id."
            ),
        }
        print(render_json_for_output(result, sys.stdout.encoding))
        return 0

    final_file_id = str(final_task_response.get("file_id") or create_task_response.get("file_id"))
    if not final_file_id or final_file_id == "None":
        raise SkillError(
            f"TTS task succeeded but file_id is missing: "
            f"{json.dumps(final_task_response, ensure_ascii=False)}"
        )

    if args.output_path:
        output_path = Path(args.output_path).expanduser().resolve()
    else:
        output_dir = Path.cwd() / "u2-tts-output"
        output_path = build_default_output_path(task_id, args.output_format, output_dir)

    download_metadata = download_file(api_key, final_file_id, output_path)

    result = {
        "summary": build_result_summary(
            selection=selection,
            create_task_response=create_task_response,
            final_task_response=final_task_response,
            download_metadata=download_metadata,
        ),
        "voice_selection": selection,
        "query_voices_response": voice_query_response,
        "available_voices": available_voices,
        "text_upload_response": text_upload_response,
        "create_task_response": create_task_response,
        "final_task_response": final_task_response,
        "download_metadata": download_metadata,
        "task_payload": payload,
    }
    print(render_json_for_output(result, sys.stdout.encoding))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SkillError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)

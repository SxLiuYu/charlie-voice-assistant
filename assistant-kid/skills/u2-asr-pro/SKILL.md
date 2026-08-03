---
{name: u2-asr-pro, description: 'Use when the user needs to transcribe a local audio
    file or public audio URL through the Unisound asynchronous U2-ASR workflow, including
    environment validation, multilingual recognition, upload, task creation, polling,
    speaker separation, and returning the final transcript payload.', version: 1.0.2}
---
# U2 ASR

## Overview

Run the Unisound asynchronous speech transcription workflow end to end. Accept one local audio file or one public HTTP(S) audio URL, create a `u2-asr` task, poll until completion, and return the final JSON payload.

## Environment Check

Before resolving credentials or uploading audio, run the checker for the current platform.

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check_environment.ps1 -InstallMissing
```

macOS:

```bash
bash ./scripts/check_environment.sh --install-missing
```

Require Python 3.10 or newer. Both checkers verify every standard-library module used by the transcription script and report the command to use as `python_command`.

- On Windows, install Python 3.12 for the current user through WinGet when needed.
- On macOS, install Python 3.12 through Homebrew when needed. If Homebrew is unavailable, stop and direct the user to install Homebrew or Python from python.org; do not execute a remote Homebrew installation script.

The current implementation has no third-party Python dependencies, so do not run `pip install`. If third-party imports are added later, declare them in a requirements file and extend the checker before using them.

## Credentials

1. Read process environment variables first, then `.env.local` in this skill directory.
2. Prefer `U2_ASR_TOKEN_PLAN_API_KEY` when present.
3. Otherwise use `U2_ASR_PLATFORM_API_KEY`.
4. If both are empty, ask the user to obtain an API key or Token Plan from https://maas.unisound.com/.
5. Never print, return, or commit API key values.

Create `.env.local` from `.env.local.example`:

```dotenv
U2_ASR_TOKEN_PLAN_API_KEY=
U2_ASR_PLATFORM_API_KEY=
```

Use `--env-file <path>` for a different local environment file.

## Workflow

1. Run the platform environment check with its install-missing option.
2. Use the reported `python_command` for subsequent commands.
3. Resolve credentials.
4. For a local file, upload it with purpose `a2t_async_input` and collect `file_id`.
5. For `--file-url`, submit the public URL directly without uploading.
6. Create the ASR task with the requested language, speaker, word, context, and hotword options.
7. Poll until the task succeeds, fails, or times out.
8. Return the final JSON payload, including `file_id` when uploaded, `task_id`, and transcript results.

## Commands

From this skill directory, use the `python_command` reported by the environment checker.

Windows:

```powershell
python .\scripts\transcribe_audio.py <file_path>
```

macOS:

```bash
python3 ./scripts/transcribe_audio.py <file_path>
```

Specify a language on Windows:

```powershell
python .\scripts\transcribe_audio.py .\audio.wav --language en-US
```

Specify a language on macOS:

```bash
python3 ./scripts/transcribe_audio.py ./audio.wav --language en-US
```

Enable automatic language recognition on Windows:

```powershell
python .\scripts\transcribe_audio.py .\audio.wav --enable-auto-lang
```

Enable automatic language recognition on macOS:

```bash
python3 ./scripts/transcribe_audio.py ./audio.wav --enable-auto-lang
```

Use a public audio URL on Windows:

```powershell
python .\scripts\transcribe_audio.py --file-url https://example.com/audio.mp3
```

Use a public audio URL on macOS:

```bash
python3 ./scripts/transcribe_audio.py --file-url https://example.com/audio.mp3
```

Common optional arguments:

- `--format mp3`
- `--sample-rate 16000`
- `--channel 1`
- `--language zh-CN`
- `--enable-auto-lang`
- `--disable-itn`
- `--enable-speaker`
- `--speaker-num 2`
- `--speaker-id <registered_voiceprint_id>` (repeatable, maximum 10)
- `--word-info`
- `--context "产品发布会议"` (maximum 500 characters)
- `--hotword 元宇宙` (repeatable, maximum 200; each maximum 5 characters)
- `--poll-interval-seconds 3`
- `--timeout-seconds 300`
- Windows: `--env-file .\.env.local`
- macOS: `--env-file ./.env.local`

Supported languages: `zh-CN`, `en-US`, `ar-SA`, `de-DE`, `es-MX`, `fr-FR`, `id-ID`, `ja-JP`, `ko-KR`, `pt-BR`, `ru-RU`, `tr-TR`, `vi-VN`, `th-TH`, and `it-IT`.

Supported formats: `mp3`, `opus`, `wav`, `amr`, `m4a`, and `ogg`. Files must be between 1 second and 5 hours and no larger than 1GB.

## Official Documentation

- API overview and current authentication/base URL: https://maas.unisound.com/docs/api/overview
- Start an asynchronous transcription: https://maas.unisound.com/docs/api/transcribe/start
- Upload a file: https://maas.unisound.com/docs/api/file-management/upload

If a request fails, an API parameter is rejected, or the response shape changes, check the official documentation above before modifying the workflow. Preserve and report the raw API error details.

## Common Mistakes

- Do not ask for an API key before checking the environment and `.env.local`.
- Do not skip the environment check or attempt to install Python standard-library modules with `pip`.
- Do not commit `.env.local` or expose its values in output.
- Do not provide both a local file path and `--file-url`.
- Do not upload with the wrong purpose; use `a2t_async_input`.
- Do not pass `--speaker-num` or `--speaker-id` without `--enable-speaker`.
- Do not stop after task creation; poll until success, failure, or timeout.
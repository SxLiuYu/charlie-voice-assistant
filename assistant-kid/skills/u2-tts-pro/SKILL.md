---
{name: u2-tts-pro, description: 'Use when the user needs asynchronous speech synthesis
    from direct text or a local text file, including voice discovery, automatic model
    selection, task polling, and audio download.', version: 1.0.1}
---
# U2 TTS

## Overview

Use this skill to run the Unisound asynchronous speech synthesis workflow end to end. The skill resolves API keys from a local config file, prefers the Token Plan key, queries available voices, selects the correct model based on the chosen voice, supports direct text or uploaded text file input, creates a speech task, polls for completion, downloads the generated audio, and returns the final payload.

## Workflow

1. Read `config/api-keys.json`.
2. Use `token_plan_api_key` when it is present.
3. Otherwise use `platform_api_key` when it is present.
4. If both are empty, ask the user for an API key and indicate user to https://maas.unisound.com/ to get a apikey or tokenplan before running the script.
5. Query available voices from `/v1/audio/voices/query`.
6. If `voice_id` is not provided, return available voices and stop so the user can choose.
7. If `voice_id` is provided:
   - system voice -> use `model=u2-tts`
   - cloned voice -> use `model=u2-tts-clone`
8. Accept either direct `text` or a local `.txt` file, but not both.
9. Create the async speech task, poll until it succeeds, then download the generated audio by `file_id`.

## Files

- `config/api-keys.json`: local credential template
- `scripts/synthesize_speech.py`: executable async TTS workflow

## Command

```powershell
python .\scripts\synthesize_speech.py --voice-id <voice_id> --text "hello world"
```

Optional arguments:

- `--text-file-path C:\path\to\input.txt`
- `--speed 50`
- `--volume 50`
- `--pitch 50`
- `--bright 50`
- `--emotion happy`
- `--language zh`
- `--dialect yueyu`
- `--audio-sample-rate 32000`
- `--format mp3`
- `--tone-rule "水泊梁山/水泊<py>po1</py>梁山"`
- `--output-path C:\path\to\result.mp3`
- `--poll-interval-seconds 3`
- `--timeout-seconds 300`
- `--config .\config\api-keys.json`

## Common Mistakes

- Do not ask the user for an API key before checking `config/api-keys.json`.
- Do not skip voice discovery when the user has not selected a `voice_id`.
- Do not choose `u2-tts-clone` for a system voice, or `u2-tts` for a cloned voice.
- Do not provide both `text` and `text_file_path`, or neither.
- Do not upload non-`.txt` files as long-text input.
- Do not stop after task creation; poll until the task succeeds, fails, or times out.
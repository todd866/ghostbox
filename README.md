# ghostbox

A small Linux desktop in Docker (plus **GhostMirror** for real macOS Chrome) for vision-driven browser agents — headed Google Chrome, human-like XTEST input, an isolated login vault, and a persistent job queue, with zero hardware required.

Built on the vision-and-grounding loop from [affirmitv/ghosthands](https://github.com/affirmitv/ghosthands) (MIT License).

## What is inside

- **Headed Google Chrome in Xvfb (`box/`):** Runs branded Google Chrome inside a `1920x1080` Xvfb desktop with an Openbox WM, `tint2` taskbar (`availHeight < height`), persistent `/etc/machine-id`, and `ForcedLanguages` + Linux `LANG` synced to `BOX_LANG` (`navigator.webdriver === false`, no `--headless`, no WebDriver/DevTools port or pipe).
- **Desktop environment signals:** WebGL via ANGLE on Mesa (`llvmpipe`, rather than headless SwiftShader), a silent PulseAudio null sink + null source (`box/pulse.pa`, zero host audio so `AudioContext` and `enumerateDevices()` see real endpoints), and `WebRtcIPHandlingPolicy` set to `default_public_interface_only`.
- **Human-like input kinematics (`box/boxd.py`):** Asymmetric minimum-jerk Bezier mouse arcs with low-pass tremor and curved overshoot recovery, 1 px click micro-drift, flick-eased scroll ramps, and direct XTEST keystrokes via `python-xlib` with bigram rhythm and variable key-hold dwell.
- **Isolated vault & 2FA:** Logins (`PW_*`) are stored readable only by the `ghost` user and injected via a policy-force-installed Manifest V3 extension (`box/ext`, packed as a signed CRX at build time) over native messaging (`box/nmhost.py`) and a `SO_PEERCRED`-verified Unix socket (`box/extlink.py`). The extension checks origin, active focus, and input type synchronously in its isolated world before setting the value (or set `BOX_FILL_MODE=type` for burst-checked XTEST typing). Supports RFC 6238 TOTP codes and pauses for human approval on push/SMS 2FA or CAPTCHAs.
- **Job queue & isolation (`box/jobs.py`):** Queues one job at a time on a persistent Chrome profile, resets tabs to a clean `about:blank` between jobs (`reset_browser()`), pauses `BOX_JOB_PAUSE_S` (jittered) between jobs, reports completed downloads, and supports `select` (native `<select>`) and `save` (Chrome's PDF viewer).
- **GhostMirror (`box/macd.py`):** Optional companion daemon exposing the exact same HTTP control API (`status`, `screenshot`, `move`, `click`, `type`, `key`, `scroll`, `navigate`, `url`) on a real macOS host's signed-in Google Chrome (Apple GPU, CoreGraphics input, macOS Accessibility `AXWebArea` confinement) for sites that challenge Linux software WebGL.

## Quickstart

```bash
# 1) Configure an OpenAI-compatible vision planner & grounder (or export OPENROUTER_API_KEY)
#    Defaults: z-ai/glm-5.3-flash planner + bytedance/ui-tars-1.5-7b grounder on OpenRouter
export OPENROUTER_API_KEY=sk-or-...

# 2) Build and start the container (prints the password-protected noVNC viewer URL)
box/ghostbox up

# 3) Verify trusted input and browser environment (no LLM calls)
GH_BOX_TOKEN=$(docker exec -u ghost ghostbox cat /run/ghostbox/box-token) python3 box/smoke.py

# 4) Queue a job
box/ghostbox do "Find the latest stable Python release on python.org" \
  -g "Go to https://www.python.org/downloads/; put the version number in answer"
box/ghostbox jobs
box/ghostbox show <ID>

# 5) Optional: store a site login (hidden prompt) and reference it with --login
box/ghostbox vault add PW_SITE https://login.example.com alice
box/ghostbox vault list
box/ghostbox do "Sign in and download the report" --login PW_SITE

# 6) Queue controls & reset
box/ghostbox ok <ID>      # continue a job paused at verify_stop
box/ghostbox abort <ID>   # abort a paused job
box/ghostbox reset        # open a clean tab and close prior tabs
box/ghostbox files <ID>   # copy downloaded files out (quarantined on macOS)
```

Or run the client directly from the host against `boxd` or `GhostMirror`:

```bash
python3 run.py --goal-file examples/pizza_form.py
```

## Models

Any OpenAI-compatible vision endpoint works via `~/.config/ghosthands/box.env` or environment variables:

```bash
# OpenRouter defaults
OPENROUTER_API_KEY=sk-or-...

# Or another provider (e.g. z.ai directly)
GH_API_KEY=...
GH_BASE_URL=https://api.z.ai/api/paas/v4
GH_PLANNER_MODEL=glm-5.3-flash
GH_GROUNDER_MODEL=glm-5.3-flash
GH_LLM_EXTRA='{"thinking":{"type":"disabled"}}'
```

## Architecture & Security

See [box/README.md](box/README.md) for full details on:
- User privilege separation (`ghost` uid 1000 vs `browser` uid 1001)
- Chrome namespace sandbox under [box/chromium-seccomp.json](box/chromium-seccomp.json)
- Loopback-only ports (`127.0.0.1:8780` token-gated API refusing `Origin` headers; `127.0.0.1:6080` password-gated noVNC)
- Known limits (`llvmpipe` software WebGL inside Docker; `DOCKER_DEFAULT_PLATFORM=linux/amd64` option on Apple Silicon)

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Attribution & License

Derived from [affirmitv/ghosthands](https://github.com/affirmitv/ghosthands) under the [MIT License](LICENSE).

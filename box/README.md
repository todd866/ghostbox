# ghostbox: ghosthands in a Linux container, no hardware

A small Linux computer in Docker that browses the web the way a person does, and works through
a queue of jobs you hand it. The brain and grounder are the same as ghosthands' vision lane;
the screen and the hands are virtual:

| Part | Real Mac target | ghostbox |
|---|---|---|
| Screen | your display, `screenfeed.sh` | Xvfb 1920x1080 inside the container + `tint2` taskbar |
| Browser | Safari | Google Chrome, headed (not headless), persistent profile |
| Hands | Pico USB-HID | X server XTEST input: asymmetric eased Bezier moves with micro-tremor, direct X11 keystrokes with bigram timing, flick-eased scroll |
| Audio | hardware audio | PulseAudio null sink + null source (silent; zero host audio) |
| Watch it | look at the screen | noVNC at http://localhost:6080 (view-only by default) |

```bash
box/ghostbox up                     # build + start; prints the viewer URL
box/ghostbox do "Find the latest stable Python release on python.org" \
     -g "Go to https://www.python.org/downloads/; put the version number in answer"
box/ghostbox jobs                   # ID, state, steps, goal
box/ghostbox show ID                # step log, checkpoints, answer
box/ghostbox ok ID | abort ID       # a job paused at a checkpoint waits for you
box/ghostbox reset                  # open a clean tab and close prior tabs
box/ghostbox shots ID               # copy its screenshots out
box/ghostbox do "Download Detlefsen 1980 from JSTOR" --login PW_UNI   # a job that may use a vault login
box/ghostbox vault add PW_UNI https://sso.example.edu alice           # add a login (hidden prompt)
box/ghostbox vault list             # list stored login names/sites (never values)
box/ghostbox up --drive             # viewer can click/type too
box/make-mac-app.sh --dock          # macOS: a Ghostbox app (Dock icon) that starts it all and opens the view
```

## What the page sees

`box/smoke.py` drives a local test page (`box/testpage.html`) that logs every event. A run shows
every mouse, key, wheel and input event as `isTrusted=true`, `navigator.webdriver=false`, and
the pointer moving along a curved path with velocity asymmetry and micro-tremor, not jumping.
There is no WebDriver, no DevTools port or pipe, and no `--enable-automation`.

Environment details aligned with an ordinary Linux desktop (`box/webgl.html` checks them live):

- **Browser:** Branded Google Chrome (`navigator.userAgentData.brands` reports Google Chrome),
  PDF viewer and plugins enabled, `ForcedLanguages` + Linux `LANG` synced to `BOX_LANG`.
- **Graphics:** WebGL through ANGLE on Mesa OpenGL (`llvmpipe`, matching a Linux desktop without
  hardware GPU drivers, rather than headless SwiftShader).
- **Audio & WebRTC:** PulseAudio null sink/source so `AudioContext` and
  `navigator.mediaDevices.enumerateDevices()` see real audio endpoints silently;
  `WebRtcIPHandlingPolicy` set to `default_public_interface_only` so private container bridge IPs
  are not exposed via ICE.
- **Desktop & resolution:** `1920x1080` default (`BOX_W` / `BOX_H`) with a `tint2` bottom taskbar
  (`screen.availHeight < screen.height`) and a persistent `/etc/machine-id` on the volume.

Remaining hardware tells: a container has no physical GPU (`llvmpipe`) and, on Apple Silicon Macs,
native builds run as `linux/arm64` unless you build/run with `DOCKER_DEFAULT_PLATFORM=linux/amd64`.
Sites that fingerprint GPU timing or challenge `llvmpipe` (e.g., Cloudflare Turnstile) can still
trigger a checkpoint; for those, use a real computer (`--target mac` or `box/macd.py`).

## Jobs

Each job is `/data/jobs/<id>/` in the `ghostbox-data` volume: `job.json`, `state`
(`queued | running | waiting | done | failed | aborted | max_steps`), `log.jsonl`, a screenshot
per step, `answer.txt` when the goal asked it to find something, and `CHECKPOINT_stepNNN.txt`
when it stopped for you, and `files/` for anything it downloaded (`ghostbox files ID` copies them
out, quarantined for Gatekeeper). After each step the agent is told which downloads finished.

Between queued jobs, the worker resets open browser tabs to a fresh `about:blank` tab so an error
or CAPTCHA from one job cannot bleed into the next, and waits `BOX_JOB_PAUSE_S` (default `5` s,
jittered `0.6x..1.4x`; `0` disables). The planner supports `select` for native `<select>`
dropdowns and `save` when a PDF is open in Chrome's built-in viewer, and is told to stop at money
commits, 2FA, captchas and anything unexpected (`verify_stop`); the job then shows `waiting`
until `ghostbox ok` or `ghostbox abort`. This is the model's judgement, not a code guard: in
testing it once typed a simple text captcha itself. One job runs at a time. The browser profile
persists, so a site signed in once stays signed in.

## Vault and 2FA

`ghostbox vault add PW_NAME URL [USER]` (or any tool that POSTs `vault_import`) gives the box a
login; inside, only the `ghost` user can read it. A job may use only the logins named with
`--login`. The planner sees names, never values, and asks for `fill_secret` with a field
(`username`, `password`, `totp`).

`boxd` clicks the field and talks to a policy-force-installed Manifest V3 extension (`box/ext`,
packed as a signed CRX at build time via `box/pack-ext.sh`) over native messaging (`box/nmhost.py`)
and a `SO_PEERCRED`-checked Unix socket (`box/extlink.py`). In one synchronous script in the
extension's isolated world on the active tab, it verifies that the origin is exactly
`https://<the login's host>`, the page is focused, and focus is on a writable top-level input of
the right type, then sets the value and dispatches `input`/`change` events. A lookalike page, a
toolbar click, a search box or a text box for a password are all refused. Set `BOX_FILL_MODE=type`
if a site requires `isTrusted` keystrokes during `fill_secret`; `boxd` then verifies `front_tab()`
before every 4-character XTEST typing burst and fails closed if origin or focus shifts.

An entry with an authenticator secret (`totp`) gets RFC 6238 codes; push or SMS 2FA stops the job
for the owner. Navigation goes through `chrome.tabs.update` over the same extension link (`http(s)`
only), so a page cannot jail the agent by swallowing Ctrl+L and the omnibox cannot autocomplete a
URL. Tested against `box/phish.html`, a page that swallows Ctrl+L and fakes the clipboard: every
fill is refused.

## Models

Any OpenAI-compatible vision model. Settings come from the environment or
`~/.config/ghosthands/box.env`:

```bash
# OpenRouter defaults (GLM-5.3-flash planner, UI-TARS grounder)
OPENROUTER_API_KEY=sk-or-...

# or z.ai directly: GLM-5.3-flash plans and grounds (measured within 2 px on the smoke page)
GH_API_KEY=...
GH_BASE_URL=https://api.z.ai/api/paas/v4
GH_PLANNER_MODEL=glm-5.3-flash
GH_GROUNDER_MODEL=glm-5.3-flash
GH_LLM_EXTRA='{"thinking":{"type":"disabled"}}'
```

## Security

- Ports 8780 (control API) and 6080 (viewer) are published on 127.0.0.1 only.
- The box's own browser can reach those ports too, so neither is open: the API requires
  `BOX_TOKEN` (random unless set) and refuses any request with an `Origin` header or a non-JSON
  body; VNC listens on loopback with a password (random unless `BOX_VNC_PASSWORD`), which
  `ghostbox watch` puts in the viewer URL.
- Chrome runs as its own user (`browser`) with an empty environment and its own PulseAudio runtime
  dir; the X server, VNC, the control daemon, the job worker and the agent run as `ghost` and hold
  the API key, token, VNC password and job files. A renderer exploit lands as `browser` and can
  read none of them. It can still see and act on the box's screen, which is the browser's own.
- Chrome's own sandbox is on: `chromium-seccomp.json` is Docker's current default seccomp
  profile (io_uring blocked) plus `clone`/`unshare` restricted to the user, PID and network
  namespace flags and `chroot`, which Chrome's namespace sandbox needs (no `setns`; `clone3`
  still returns ENOSYS so libc uses the filterable `clone`). So a renderer bug is contained by
  Chrome's sandbox and then by the user split, `cap_drop: ALL` and `no-new-privileges`. On a
  runtime without that profile, `BOX_CHROME_SANDBOX=0` falls back to `--no-sandbox`.
- Known limits: the vault is plaintext JSON on the volume, readable to anyone with `docker`
  access on the host. A browser process that is itself compromised (a sandbox escape) sees
  whatever is put into it and holds the extension link, so no check inside the box can protect a
  secret from it.
- The volume holds the browser's cookies, the vault and job files. Treat it like a signed-in
  laptop.

## Files

`Dockerfile`, `compose.yml`, `entrypoint.sh` (X, openbox, tint2, PulseAudio, VNC, Chrome, job
worker, control daemon), `pulse.pa`, `boxd.py` (control daemon), `jobs.py` (queue), `ghostbox`
(host CLI), `ext/` + `pack-ext.sh` + `nmhost.py` + `nmhost.json` + `extlink.py` (vault and
navigation extension link), `chromium-policy.json` (downloads, WebRTC policy, autofill off),
`chromium-seccomp.json`, `smoke.py` + `testpage.html` (no-LLM input check), `phish.html` (attack
page for the vault), `webgl.html` (fingerprint self-check), `make-mac-app.sh` (macOS launcher).
The client is `ghosthands/box.py` (`--target box`).

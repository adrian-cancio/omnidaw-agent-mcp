# omnidaw-agent-mcp

An unofficial, policy-enforced Model Context Protocol (MCP) server for [OMNi DAW](https://omnidaw.com).

## Purpose

While OMNi DAW includes an official MCP server, `omnidaw-agent-mcp` is an independent integration that adds:
- **Strict Operator Policy**: Enforces explicit authorization (`--allow-write`, `--allow-risk`) rather than relying purely on descriptive model annotations.
- **Workflow Previews**: Two-phase composable operations (preview -> inspect -> apply) with preflight validation, symbolic references (`$ref`), and per-step execution journals. Execution is not an atomic transaction: earlier steps may have changed state when a later step fails or has an unknown outcome. There is no automatic rollback or replay; inspect live state before recovery, and do not assume undo can reverse file writes.
- **Domain Filtering**: Selectively expose API domains at startup, reducing context overhead and constraining backend actions.
- **Job & File Safety**: Safe async job supervision with deadlines, strict loopback TCP restrictions, automatic token redaction from errors and logs, and verification of non-silent PCM audio.
- **Honest Offline Status**: Accurate reporting of whether API capabilities originate from live discovery or bundled references.

## Platform Compatibility & Requirements

Requirements:
- **Python 3.12+** (standard library + `mcp` SDK)
- **OMNi DAW** (v0.1.0 Build 5de6b9a or newer, with API enabled via `--api` or GUI Settings)

Supported Platforms:
- **Linux**: Any standard modern Linux distribution with Python 3.12+ (Ubuntu, Debian, Fedora, Arch, etc.). Fully compatible with Wayland (e.g. niri, GNOME, Sway) and X11. Runs with the official OMNi AppImage.
- **Windows**: Windows 10/11 (64-bit). Fully supported out of the box (uses `%APPDATA%/omni/api/endpoint.json`).
- **macOS**: Supported by `omnidaw-agent-mcp` (reads `~/Library/Application Support/omni/api/endpoint.json` and supports loopback TCP sockets). *Note: OMNi DAW itself currently publishes official binaries for Linux and Windows; macOS will work immediately once OMNi releases a macOS build or when connecting via network tunnel to a remote host.*

## Installation

```bash
git clone https://github.com/stratovey/omnidaw-agent-mcp.git
cd omnidaw-agent-mcp

python3 -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
pip install -e .
```

To run diagnostics:
```bash
omnidaw-agent --diagnose
```

## Running the Server

Speak MCP over standard I/O:

```bash
# Read-only safe inspection (default)
omnidaw-agent

# Allow project edits within authorized root
omnidaw-agent --allow-write --allow-risk --output-root /home/user/Music/projects

# Also let session.save / export.render replace existing files (only inside --output-root)
omnidaw-agent --allow-write --allow-overwrite --output-root /home/user/Music/projects

# Restrict exposed tools to specific domains
omnidaw-agent --domains session,tracks,clips,theory
```

## MCP Client Configuration

### Claude Desktop / Hermes / OpenCode

Add the following to your MCP client configuration (`opencode.jsonc`, `claude_desktop_config.json`, etc.):

```json
{
  "mcpServers": {
    "omnidaw": {
      "command": "/path/to/omnidaw-agent-mcp/.venv/bin/omnidaw-agent",
      "args": [
        "--allow-write",
        "--output-root",
        "/home/user/Music/omnidaw-exports"
      ]
    }
  }
}
```

### Remote Agent Architecture Note

OMNi DAW's JSON-RPC API is strictly local (`127.0.0.1`) and authenticates via an ephemeral token generated per session.
For agents operating remotely (e.g. on a separate VPS):
1. Run `omnidaw-agent` locally on the workstation where OMNi runs.
2. Establish a secure authenticated tunnel (e.g., WireGuard, Tailscale, or an authenticated mTLS reverse proxy) connecting the remote agent harness to a local transport bridge.
3. Never expose `127.0.0.1` directly or persist/transmit the API token across unsecured networks.

## Composable Workflows

- `agent_status`: Inspect connection state, catalog provenance, and active operator policies.
- `agent_inspect`: Return compact, LLM-optimized summaries of project state and tracks.
- `agent_preview`: Generate an execution plan from official methods with preflight checks and symbolic references.
- `agent_apply`: Execute an approved preview plan once, producing an execution journal and handling partial failures.
- `agent_loop_preview`: Compose a deterministic four-part electronic loop (drums, bass, chords, melody) using live instruments and native theory generators.
- `agent_arrange_preview`: Arrange scenes into sections, duplicate clips with transforms, and apply automation.
- `agent_arrange_timeline`: Place a batch of clips (`track`, `slot`, `start_beat`, `length_beats`) and optional markers in one preview plan.
- `agent_measure`: Measure master/track loudness, tonal balance, and clipping, proposing mastering chains without perceptual claims.
- `agent_export_preview`: Preview mixes or stems to WAV files with bounded job tracking. An explicit finite `end_beat` greater than `start_beat` (default 0) is required; the workflow never guesses song length. Failed or unavailable arrangement inspection produces no plan. Session-mode preparation selects the arrangement using `arrangement.play` (which starts playback) then stops it; this does not place session clips on the timeline or guarantee non-silent output. Inspect the range/content and verify the written audio separately.
- `agent_verify_wav`: Verify that exported audio contains valid, non-silent integer PCM with matching sample rate and duration.

## Known Limitations

### OMNi audio-stream failure on Linux (upstream issue, not fixed here)

**Observed behaviour**: during playback, OMNi's audio output stream can die. The OMNi log then shows `alsa::poll() returned POLLERR` hundreds of thousands of times, followed by `Audio stream died - auto-restarting engine`. After that, audio output stays silent, including fast offline renders: they produce a structurally valid WAV whose content is silent. In some real sessions the host process then disappeared.

**What the evidence supports**:
- It reproduced in isolated runs against a null audio sink (no headset, no sound card). Hardware is therefore not required to trigger it.
- It happens during plain arrangement playback. `analysis.capture` is not required to trigger it. An earlier version of this README claimed a threshold of about 75 s of accumulated captures; later controlled runs contradicted that.
- Time to failure varied (23 s to 106 s in failing runs), and some runs of 90-224 s were clean. There is no deterministic trigger.

**What is not established**:
- The cause. The OMNi binary embeds two copies of CPAL's ALSA backend (one identified as 0.15.3, one vendored and apparently from the 0.17 era). Which one drives the failing stream is unconfirmed. An upstream CPAL change (commit `5931fa29`, first released in v0.18.0) fixes a `POLLERR` spin loop that matches the symptoms, but its effect on OMNi has not been tested because it requires a rebuilt host.
- How the host process dies in real sessions. The sandboxed runs showed the stream dying and the engine restarting, not the process exiting.
- Behaviour on other operating systems.

**Practical guidance for MCP clients** (none of this prevents the failure):
1. Always verify exported audio with `agent_verify_wav` or equivalent. After a stream death, an offline render can be silent.
2. Save the project often. If the host dies, do not restart it automatically; report the failure and the evidence.
3. Prefer offline export over real-time capture for evaluation. `engine.capture_with_cooldown()` (5 s pause between captures, batch limit of 8) is kept, but it was designed on a hypothesis that later evidence weakened, and its effect is not demonstrated.

## Verification & Testing

Run unit tests and SDK subprocess integration tests:
```bash
pytest -v
```

Execute disposable integration verification against a target project:
```bash
python scripts/run_integration.py /path/to/endpoint.json /path/to/output_dir
```

## License & Attribution

- Code adapted from official OMNi DAW reference examples: Copyright (c) 2026 Tomasz Głuc, under the [MIT License](LICENSE).
- Extensions, workflows, and policy engine: Copyright (c) 2026 omnidaw-agent-mcp contributors, under the [MIT License](LICENSE).
- OMNi DAW is a proprietary audio workstation developed by Tomasz Głuc. This project is unofficial and independent.

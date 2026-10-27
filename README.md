# omnidaw-agent-mcp

An unofficial, policy-enforced Model Context Protocol (MCP) server for [OMNi DAW](https://omnidaw.com).

## Purpose

While OMNi DAW includes an official MCP server, `omnidaw-agent-mcp` is an independent integration that adds:
- **Strict Operator Policy**: Enforces explicit authorization (`--allow-write`, `--allow-risk`) rather than relying purely on descriptive model annotations.
- **Workflow Previews**: Two-phase composable operations (preview -> inspect -> apply) with preflight validation, symbolic references (`$ref`), and per-step execution journals. Execution is not an atomic transaction: earlier steps may have changed state when a later step fails or has an unknown outcome. There is no automatic rollback or replay; inspect live state before recovery, and do not assume undo can reverse file writes.
- **Domain Filtering**: Selectively expose API domains at startup, reducing context overhead and constraining backend actions.
- **Job & File Safety**: Safe async job supervision with deadlines, strict loopback TCP restrictions, automatic token redaction from errors and logs, and verification of non-silent PCM audio.
- **Honest Offline Status**: Accurate reporting of whether API capabilities originate from live discovery or bundled references.

## Phase 5a-1 offline workflows

- **M-006:** bundled official 0.21.0 `params_list` supports a required `track`, not a master chain. No fabricated `chain` argument is added. `devices_list({"chain":"master"})` and `devices_get({"chain":"master","slot":0})` already support master effects; this is not equivalent to paginated plugin parameter discovery.
- **M-004:** `agent_repeat_preview` takes `track`, `slot`, `native_length_beats`, `count`, and optional `start_beat`. Supply the actual native length in quarter-note beats; it is not inferred from undocumented engine fields or audio duration/tempo. Each placement has explicit start and length, computed from the original decimal values rather than accumulated float addition. Fractional lengths are supported within official placement bounds (0.25..4096 beats). This places existing session clips without clearing the track. Audio placement is described by `audio.import` in the bundled reference, but its interaction with the MIDI-only wording of `arrangement.place_clip` remains a live validation item for 5a-2.
- **M-005:** `agent_arrange_timeline` and repetitions group placements plus markers in ordered batches of at most 32 mutations. Markers follow placements and count toward the bound. More than one batch requires `checkpoint_paths`: exactly one distinct, new absolute `.omni` destination between adjacent batches, inside `--output-root`. Saves use the official `session.save` API and change the project's save destination. A missing/domain-filtered save method blocks preview. Saves count toward the unchanged 128-step plan limit. Single-batch existing calls remain compatible. Preview/apply retains process-local 300-second expiry and single-use plans. Partial failures return completed indices, failed-or-unknown index, pending indices and the full step scope; writes stop immediately. No rollback, automatic retry or replay is provided, including unknown mutation/checkpoint outcomes. Apply progress notifications are best-effort: callback exceptions are recorded as `progress_warnings`, never relabel confirmed writes, interrupt job supervision, or trigger replay. Checkpoint preview enforces the `.omni` suffix; existing destinations are refused even with overwrite authorization because checkpoints never request overwrite. Apply rechecks destination availability before any plan writes (and again at each save).
- **M-007:** `agent_midi_preview({"path":"/authorized/synthetic.mid","track":0,"slot":0})` reads a bounded local SMF or RIFF RMID under the existing `--output-root` policy (also used for this local input). Absolute paths only; resolved symlink escapes are denied before reading. It previews a replacement `clips.write_midi`; applying still needs write and risk authorization. Formats 0/1 and PPQ are supported, with FIFO overlap pairing per source track/channel; tracks/channels merge into one destination with an explicit warning. Note times are ticks / PPQ, independent of tempo. Tempo, controller, program and meta payloads are preserved in the report, not applied; sustain/instrument behavior may differ. Format 2, SMPTE, SysEx/system, pressure/pitch bend, malformed structures and unrepresentable note timing are refused before plan creation. Limits: 8 MiB, 100,000 events, 256 tracks, 4,096 notes, 1,024 clip beats. Empty note files are rejected.

Verification is offline only: synthetic MIDI fixtures, official bundled schemas, fake engine and actual SDK stdio against a fake loopback host. No live OMNi behavior, audible results, or actual checkpoint persistence is established. Live validation is explicitly deferred; offline passing tests do not establish those properties.

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
- `agent_verify_wav`: Verify that exported audio contains valid integer PCM with matching sample rate and duration. Audio whose peak is below `silence_floor_dbfs` (default -80 dBFS) is rejected as silent or dither-only. The result includes `peak_dbfs` and `rms_dbfs`.

## Known Limitations

These are limits of the current OMNi API and of this server's safety model, not guarantees about audible results.

- **No MIDI file import or session open.** The API exposes `audio.import`, `session.info` and `session.save`; there is no native `.mid` import and no `session.open`. Write notes with `clips.write_midi` or the theory/clip generators.
- **Audio import rounds native lengths.** The imported clip length is rounded (observed: a 32 beat loop became 32.25 beats). Place audio clips with explicit native-length placements.
- **Lengthening an audio timeline clip did not repeat its content** in observed use. The catalog documents repetition for MIDI tracks only; for audio, place the clip once per repetition with explicit native-length placements.
- **Exports can be structurally valid but silent.** If the arrangement playback mode is wrong, an offline render can contain only dither. `agent_verify_wav` rejects audio whose peak is below `silence_floor_dbfs` (default -80 dBFS). This is a floor check: it does not prove the audio is continuous, complete or musical.
- **Plans are process-local.** Previews live in memory inside the server process, expire after 300 s, hold at most 128 steps, and the oldest of 32 pending plans is dropped. Split large timelines into several `agent_arrange_timeline` batches and apply each one promptly.

### Audio-stream failure on Linux (regression note, October 2026)

Earlier OMNi builds could lose their audio output stream during playback (log lines `alsa::poll() returned POLLERR`, then `Audio stream died - auto-restarting engine`). After that, audio stayed silent, including offline renders, and in some sessions the host process exited. The cause was never established, and no deterministic trigger was found. An earlier claim that about 75 s of accumulated `analysis.capture` calls triggers it was contradicted by later runs and has been removed.

With OMNi build `bbf1bd36`, whose developer reports related fixes, no new stream failures were seen in a ~30 minute session of mostly offline editing and exports, in a later session with ordinary 2 s `agent_measure` captures, or in a short playback test. This server applies no capture throttling. This is an observed improvement, not a certified fix: no long continuous playback test has been run, and behaviour on other operating systems is untested.

Practical guidance for MCP clients:
1. Verify exported audio with `agent_verify_wav` or equivalent.
2. Save the project often. If the host dies, do not restart it automatically; report the failure and the evidence.
3. Prefer offline export over long real-time capture for evaluation.

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

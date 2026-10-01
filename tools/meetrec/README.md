## MeetRec

Menu bar recorder that captures your microphone and all system audio, then
transcribes both locally with MLX Whisper. The microphone becomes **Me** and
system audio becomes **Them**, so a two-sided call needs no diarization.

### Build and run

```sh
tools/meetrec/build.sh            # installs ~/Applications/MeetRec.app
open ~/Applications/MeetRec.app
```

Click the mic icon, then **Start Recording**; the icon turns red with a timer.
**Stop Recording** writes `mic.wav` and `system.wav` (16 kHz mono) to
`~/Recordings/meetings/<timestamp>/`, runs `transcribe.py` in the background
(you can start the next recording meanwhile), and notifies when the transcript
is ready. When `$OV` is set at build time, transcripts go to
`<paths.inbox>/meetings/<timestamp>-meeting.md` for later triage or
`/hi meeting`; otherwise `transcript.md` sits beside the audio. Audio never
enters the vault; the transcript's `audio:` field points back to it.

The first start asks for Microphone and System Audio Recording access. The app
is ad-hoc signed, so each rebuild asks again. It must run as its own app: a
terminal-launched binary inherits the terminal's permissions, and terminals do
not declare system audio capture. The first transcription downloads the
Whisper model into the Hugging Face cache.

### Settings

```sh
defaults write local.atelier.meetrec OutputDir ~/Recordings/meetings
defaults write local.atelier.meetrec TranscriptDir ~/somewhere   # overrides the build-time inbox
defaults write local.atelier.meetrec Language zh   # default: auto-detect per channel
defaults write local.atelier.meetrec Prompt "Vocabulary, names, 中英混合"
defaults write local.atelier.meetrec CheckUpdates -bool NO   # no launch-time model check
```

Rerun transcription by hand with
`uv run --script tools/meetrec/transcribe.py <session-dir> [--out FILE]`.

### Models

`models.json` lists the models under the **Model** menu: Whisper large-v3
turbo (default) and Qwen3-ASR 1.7B and 0.6B in 8-bit, which report stronger
Mandarin results. The choice applies to the next transcription. A model
downloads on first use, then loads offline, so an upstream revision never
arrives silently. At launch and daily, MeetRec asks huggingface.co for the
latest revision of each cached repo and for newer generations in each family.
A new revision prompts Update, Skip This Version, or Later; a new generation
only posts a notification, since it usually needs a newer runtime package and
a `models.json` entry. To compare models on saved audio, rerun
`transcribe.py <session-dir> --model qwen3-asr-1.7b --out <file>`.

### Echo

With speakers, the far end leaks into the microphone. `transcribe.py` gates
mic frames that do not rise above the echo predicted from the same-clock
system channel, then drops any Me segment that restates an overlapping Them
segment. With headphones the channels do not correlate and the mic passes
through unchanged. Quiet speech that overlaps the far end can lose syllables.

### Limits

- Devices are fixed when recording starts; unplugging the output device
  mid-recording breaks the capture.
- Several remote speakers all appear as Them.
- `--record-for N` records N seconds, transcribes, and quits (for checks).

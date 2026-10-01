#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mlx-whisper==0.4.3", "mlx-qwen3-asr==0.4.4", "numpy>=2"]
# ///
"""Transcribe a MeetRec session: mic.wav (Me) + system.wav (Them) -> transcript.md.

Usage: uv run --script tools/meetrec/transcribe.py <session-dir> [--out FILE] [--model KEY] [--language zh]
       uv run --script tools/meetrec/transcribe.py --check-updates | --prefetch REPO...

Speakers leak the far end into the mic, so the mic is echo-suppressed against
the same-clock system channel before transcription, and any Me segment that
restates an overlapping Them segment is dropped. Models come from models.json;
once cached they load offline, so a new revision arrives only via --prefetch.
Pure helpers stay stdlib-only so tests import them without numpy or MLX.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

RATE = 16_000
REGISTRY = Path(__file__).with_name("models.json")
HF_API = "https://huggingface.co/api"
QWEN_LANGUAGES = {"zh": "Chinese", "en": "English", "yue": "Cantonese", "ja": "Japanese", "ko": "Korean"}
SENTENCE_END = re.compile(r"[。！？!?]|\.\s")  # not the "." inside 3.5 or Q4.2


@dataclass
class Segment:
    start: float
    end: float
    speaker: str
    text: str


def clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def _norm(text: str) -> str:
    return "".join(c for c in text.lower() if unicodedata.category(c)[0] in "LN")


def echo_score(me: str, them: str) -> float:
    """Share of Me's letters and digits found, in order, inside the Them text."""
    a, b = _norm(me), _norm(them)
    if not a or not b:
        return 0.0
    blocks = difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
    return sum(block.size for block in blocks) / len(a)


ECHO_MIN_WEIGHT = 12  # about three English words or four CJK characters


def _weight(text: str) -> int:
    return sum(3 if unicodedata.east_asian_width(c) in "WF" else 1 for c in _norm(text))


def drop_echo_repeats(me: list[Segment], them: list[Segment],
                      threshold: float = 0.6, slack: float = 2.0) -> list[Segment]:
    """Residual-echo safety net: drop Me segments that restate nearby Them speech.

    Short replies ("Yes", "可以") match almost any sentence in order, so they stay."""
    kept = []
    for seg in me:
        near = " ".join(t.text for t in them if t.start < seg.end + slack and t.end > seg.start - slack)
        if _weight(seg.text) < ECHO_MIN_WEIGHT or echo_score(seg.text, near) < threshold:
            kept.append(seg)
    return kept


def merge(segments: list[Segment], gap: float = 1.5) -> list[Segment]:
    """Chronological turns; consecutive same-speaker segments within `gap` seconds join."""
    turns: list[Segment] = []
    for seg in sorted(segments, key=lambda s: (s.start, s.speaker)):
        last = turns[-1] if turns else None
        if last and last.speaker == seg.speaker and seg.start - last.end <= gap:
            sep = " " if last.text[-1:].isascii() and seg.text[:1].isascii() else ""
            turns[-1] = Segment(last.start, max(last.end, seg.end), last.speaker, last.text + sep + seg.text)
        else:
            turns.append(Segment(seg.start, seg.end, seg.speaker, seg.text))
    return turns


def render(turns: list[Segment], *, started: str, duration: float, model: str, audio: str) -> str:
    lines = ["---", "source: meetrec", f"recorded: {started}", f"duration: {clock(duration)}", f"model: {model}",
             "speakers: Me = microphone, Them = system audio", f"audio: {audio}", "---", "", "## Transcript", ""]
    for t in turns:
        lines += [f"**[{clock(t.start)}] {t.speaker}:** {t.text}", ""]
    if not turns:
        lines += ["_No speech detected._", ""]
    return "\n".join(lines)


def resolve_model(name: str | None, registry: dict) -> dict:
    """A models.json key or its repo; any other Hugging Face repo id is an escape hatch."""
    name = name or registry["default"]
    for spec in registry["models"]:
        if name in (spec["key"], spec["repos"][0]):
            return spec
    return {"key": name, "label": name, "engine": "qwen3" if "qwen" in name.lower() else "whisper",
            "repos": [name]}


def hf_cache() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    return Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"


def cached_sha(repo: str) -> str | None:
    ref = hf_cache() / f"models--{repo.replace('/', '--')}" / "refs" / "main"
    return ref.read_text().strip() if ref.is_file() else None


def _get(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "meetrec"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def check_updates(registry: dict, get=_get) -> dict:
    """New revisions of cached repos (the app asks first) and newer family generations (notify only)."""
    revisions, seen = [], set()
    for spec in registry["models"]:
        for repo in spec["repos"]:
            local = cached_sha(repo)
            if repo in seen or not local:
                continue
            seen.add(repo)
            remote = get(f"{HF_API}/models/{repo}").get("sha")
            if remote and remote != local:
                revisions.append({"model": spec["label"], "repo": repo, "local": local, "remote": remote})
    generations: dict[tuple[str, str], list[str]] = {}  # one notice per generation, not per quantization
    for family in registry["families"]:
        query = urllib.parse.urlencode({"author": family["author"], "search": family["search"],
                                        "sort": "createdAt", "direction": "-1", "limit": "100"})
        pattern, current = re.compile(family["pattern"]), _version(family["current"])
        for row in get(f"{HF_API}/models?{query}"):
            match = pattern.match(row.get("id", ""))
            if match and _version(match["version"]) > current:
                generations.setdefault((family["name"], match["version"]), []).append(row["id"])
    return {"revisions": revisions, "generations": [
        {"family": name, "version": version, "repos": repos} for (name, version), repos in generations.items()]}


def phrases(words: list[dict], text: str, gap: float = 1.0) -> list[tuple[float, float, str]]:
    """Group aligner word timings into phrases and cut the punctuated text at the same words.

    Word tokens carry no punctuation, so each is located in order inside `text`;
    a phrase ends at sentence punctuation or a pause longer than `gap` seconds.
    """
    spans: list[tuple[float, float, str]] = []
    pos = cut = 0
    start = end = None
    for word in words:
        token = str(word["text"]).strip()
        found = text.find(token, pos) if token else -1
        at = found if found >= 0 else pos
        if start is not None and (word["start"] - end > gap or SENTENCE_END.search(text[pos:at])):
            spans.append((start, end, text[cut:at].strip()))
            start, cut = None, at
        if start is None:
            start = word["start"]
        end = word["end"]
        if found >= 0:
            pos = found + len(token)
    if start is not None:
        spans.append((start, end, text[cut:].strip()))
    return [span for span in spans if span[2]]


def load_wav(path: Path):
    """16 kHz mono int16 WAV as float32. Reads to EOF so a crash-stale header still loads."""
    import numpy as np
    data = path.read_bytes()
    pos, fmt = 12, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], int.from_bytes(data[pos + 4:pos + 8], "little")
        if cid == b"fmt ":
            fmt = data[pos + 8:pos + 24]
        elif cid == b"data":
            if fmt is None or int.from_bytes(fmt[2:4], "little") != 1 \
                    or int.from_bytes(fmt[4:8], "little") != RATE or int.from_bytes(fmt[14:16], "little") != 16:
                raise ValueError(f"{path.name}: expected 16 kHz mono 16-bit PCM")
            pcm = data[pos + 8:]
            return np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        pos += 8 + size + (size & 1)
    raise ValueError(f"{path.name}: no data chunk")


def suppress_echo(mic, ref, n_fft: int = 512, max_lag_s: float = 0.5, min_coupling: float = 0.3):
    """Remove far-end echo from `mic` using the sample-aligned system channel `ref`.

    Delay comes from log-envelope cross-correlation; coupling is the ratio of
    median mic and ref power over far-end-active frames (robust while near-end
    talk stays under half of them). Frames where the mic does not
    rise above the predicted echo are gated to silence so Whisper sees none of
    it; the rest get spectral subtraction. Weak correlation means headphones:
    return the mic unchanged.
    """
    import numpy as np
    n = min(len(mic), len(ref))
    mic, ref = mic[:n].astype(np.float32), ref[:n].astype(np.float32)
    hop = n_fft // 2
    if n < n_fft * 8 or not np.any(ref):
        return mic
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n_fft) / n_fft)).astype(np.float32)
    pad = (-(n + n_fft) % hop) + hop

    def stft(x):
        x = np.concatenate([np.zeros(hop, np.float32), x, np.zeros(pad, np.float32)])
        return np.fft.rfft(np.lib.stride_tricks.sliding_window_view(x, n_fft)[::hop] * window, axis=1)

    spec_m, spec_r = stft(mic), stft(ref)
    pow_m, pow_r = np.abs(spec_m) ** 2, np.abs(spec_r) ** 2
    env_m, env_r = np.log(pow_m.sum(1) + 1e-9), np.log(pow_r.sum(1) + 1e-9)
    env_m, env_r = (env_m - env_m.mean()) / (env_m.std() + 1e-9), (env_r - env_r.mean()) / (env_r.std() + 1e-9)
    frames = len(env_m)
    lags = range(min(int(max_lag_s * RATE / hop), frames // 2))
    corr = [float(np.mean(env_m[d:] * env_r[:frames - d])) for d in lags]
    delay = int(np.argmax(corr))
    if corr[delay] < min_coupling:
        return mic

    aligned = np.zeros_like(pow_r)
    aligned[delay:] = pow_r[:frames - delay]
    total_m, total_a = pow_m.sum(1), aligned.sum(1)
    active = total_a > 0.1 * np.percentile(total_a, 95)
    frame_coupling = np.median(total_m[active]) / (np.median(total_a[active]) + 1e-12)
    coupling = np.median(pow_m[active], axis=0) / (np.median(aligned[active], axis=0) + 1e-12)

    delayed = aligned.copy()  # widen the prediction only after measuring coupling
    for d in (delay - 1, delay + 1):  # the true delay falls between frames
        if 0 <= d < frames:
            np.maximum(delayed[d:], pow_r[:frames - d], out=delayed[d:])
    for t in range(1, frames):  # room reverb: decaying running max
        np.maximum(delayed[t], 0.75 * delayed[t - 1], out=delayed[t])
    echo = np.minimum(coupling, 10 * frame_coupling) * delayed

    near = total_m > 3 * echo.sum(1) + 3 * np.percentile(total_m, 10)
    onset = np.convolve(near.astype(np.float32), np.ones(5, np.float32), mode="same") >= 3  # 3 of 5 frames
    gate = np.convolve(onset.astype(np.float32), np.ones(13, np.float32), mode="same") > 0  # +-100 ms hangover
    gain = np.clip(1 - 2 * echo / (pow_m + 1e-12), 0.1, 1) * gate[:, None]

    out = np.zeros(frames * hop + n_fft, np.float32)
    chunks = np.fft.irfft(spec_m * gain, n=n_fft, axis=1).astype(np.float32)
    for t in range(frames):
        out[t * hop:t * hop + n_fft] += chunks[t]
    return out[hop:hop + n]


def transcribe_channel(audio, speaker: str, spec: dict, language: str | None,
                       prompt: str | None) -> list[Segment]:
    """One channel; leading silence is trimmed so language detection sees speech."""
    import numpy as np
    loud = np.flatnonzero(np.abs(audio) > 1e-3)
    if loud.size == 0:
        return []
    offset = max(int(loud[0]) - RATE // 2, 0)
    if spec["engine"] == "qwen3":
        import mlx_qwen3_asr
        result = mlx_qwen3_asr.transcribe(
            audio[offset:], model=spec["repos"][0], context=prompt or "",
            language=QWEN_LANGUAGES.get(language or "", language), return_timestamps=True)
        spans = phrases(result.segments or [], result.text)
    else:
        import mlx_whisper
        result = mlx_whisper.transcribe(
            audio[offset:], path_or_hf_repo=spec["repos"][0], language=language, initial_prompt=prompt,
            word_timestamps=True, condition_on_previous_text=False, hallucination_silence_threshold=2.0)
        spans = [(s["start"], s["end"], s["text"].strip()) for s in result["segments"]
                 if not (s["no_speech_prob"] > 0.6 and s["avg_logprob"] < -1.0)]
    base = offset / RATE
    return [Segment(round(base + a, 2), round(base + b, 2), speaker, text) for a, b, text in spans if text]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("session", type=Path, nargs="?", help="directory holding mic.wav and system.wav")
    parser.add_argument("--out", type=Path, help="transcript path (default: <session>/transcript.md)")
    parser.add_argument("--model", help="models.json key or Hugging Face repo (default: the registry default)")
    parser.add_argument("--language", help="force a Whisper language code; default auto-detects per channel")
    parser.add_argument("--prompt", help="initial prompt: vocabulary, names, or a mixed zh/en sample")
    parser.add_argument("--check-updates", action="store_true", help="print pending model updates as JSON")
    parser.add_argument("--prefetch", nargs="+", metavar="REPO", help="download the latest revision of repos")
    args = parser.parse_args(argv)
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    if args.check_updates:
        try:
            print(json.dumps(check_updates(registry)))
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}))
        return 0
    if args.prefetch:
        from huggingface_hub import snapshot_download
        for repo in args.prefetch:
            print(f"{repo}: {snapshot_download(repo_id=repo)}")
        return 0
    if args.session is None:
        parser.error("a session directory is required")
    spec = resolve_model(args.model, registry)
    if all(cached_sha(repo) for repo in spec["repos"]):
        os.environ["HF_HUB_OFFLINE"] = "1"  # read before the engines import huggingface_hub

    mic, system = load_wav(args.session / "mic.wav"), load_wav(args.session / "system.wav")
    n = min(len(mic), len(system))
    if not system[:n].any():
        print("warning: system audio is silent; nothing played, or System Audio Recording is not allowed",
              file=sys.stderr)
    cleaned = suppress_echo(mic[:n], system[:n])
    them = transcribe_channel(system[:n], "Them", spec, args.language, args.prompt)
    me_raw = transcribe_channel(cleaned, "Me", spec, args.language, args.prompt)
    me = drop_echo_repeats(me_raw, them)
    turns = merge(me + them)

    try:
        started = datetime.strptime(args.session.name, "%Y-%m-%d-%H%M%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        started = args.session.name
    (args.session / "segments.json").write_text(
        json.dumps([asdict(s) for s in sorted(me + them, key=lambda s: s.start)], ensure_ascii=False, indent=1),
        encoding="utf-8")
    out = args.out or args.session / "transcript.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(turns, started=started, duration=n / RATE, model=spec["repos"][0],
                          audio=str(args.session.resolve())), encoding="utf-8")
    print(f"{out}: {len(turns)} turns, {len(them)} Them + {len(me)} Me segments "
          f"({len(me_raw) - len(me)} echo repeats dropped), {clock(n / RATE)} audio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

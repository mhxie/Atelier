"""MeetRec transcript assembly: speaker turns, echo dedupe, WAV loading, echo suppression."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location("meetrec_transcribe", REPO_ROOT / "tools/meetrec/transcribe.py")
meetrec = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = meetrec  # dataclasses resolve annotations through sys.modules
_SPEC.loader.exec_module(meetrec)
Segment = meetrec.Segment

try:
    import numpy as np
except ImportError:  # the default suite has no numpy; run with `uv run --with numpy`
    np = None


class TranscriptAssemblyTests(unittest.TestCase):
    def test_merge_joins_same_speaker_and_orders_turns(self):
        turns = meetrec.merge([
            Segment(5.0, 6.0, "Them", "Sounds good."),
            Segment(0.0, 2.0, "Me", "Let's start"),
            Segment(2.5, 4.0, "Me", "with the roadmap."),
            Segment(9.0, 10.0, "Me", "我来跟进"),
            Segment(10.2, 11.0, "Me", "这个问题"),
        ])
        self.assertEqual([(t.speaker, t.text) for t in turns], [
            ("Me", "Let's start with the roadmap."),
            ("Them", "Sounds good."),
            ("Me", "我来跟进这个问题"),
        ])

    def test_merge_splits_same_speaker_after_long_gap(self):
        turns = meetrec.merge([Segment(0, 1, "Me", "One."), Segment(5, 6, "Me", "Two.")])
        self.assertEqual(len(turns), 2)

    def test_echo_repeat_dropped_but_real_reply_kept(self):
        them = [Segment(10.0, 14.0, "Them", "We need the budget numbers by Friday, can you send them?")]
        me = [
            Segment(10.3, 13.9, "Me", "need the budget numbers by Friday"),
            Segment(15.0, 16.5, "Me", "Yes, I'll send them tonight."),
            Segment(40.0, 41.0, "Me", "need the budget numbers by Friday"),
        ]
        kept = meetrec.drop_echo_repeats(me, them)
        self.assertEqual([s.start for s in kept], [15.0, 40.0])

    def test_short_replies_are_never_dropped_as_echo(self):
        them = [Segment(10.0, 14.0, "Them", "Can you send them now, or are you sure it can wait? 可以吗")]
        me = [Segment(14.2, 14.8, "Me", reply) for reply in ("No.", "Yes.", "Sure.", "Right.", "可以。")]
        self.assertEqual(meetrec.drop_echo_repeats(me, them), me)
        echo = Segment(10.5, 13.5, "Me", "下周三之前要交")
        self.assertEqual(meetrec.drop_echo_repeats([echo], [Segment(10.0, 14.0, "Them", "我们下周三之前要交方案")]), [])

    def test_echo_score_handles_chinese(self):
        self.assertGreater(meetrec.echo_score("下周三之前", "我们下周三之前要交方案"), 0.9)
        self.assertLess(meetrec.echo_score("好的没问题", "我们下周三之前要交方案"), 0.3)

    def test_render_marks_speakers_and_empty_transcript(self):
        meta = {"started": "x", "model": "m", "audio": "/rec/2099-01-01-090000"}
        text = meetrec.render([Segment(3725.0, 3726.0, "Me", "Hi.")], duration=3726, **meta)
        self.assertIn("## Transcript", text)
        self.assertIn("**[01:02:05] Me:** Hi.", text)
        self.assertIn("duration: 01:02:06", text)
        self.assertIn("audio: /rec/2099-01-01-090000", text)
        self.assertIn("_No speech detected._", meetrec.render([], duration=0, **meta))


class ModelTests(unittest.TestCase):
    REGISTRY = {
        "default": "w",
        "models": [
            {"key": "w", "label": "W", "engine": "whisper", "repos": ["org/w"]},
            {"key": "q", "label": "Q", "engine": "qwen3", "repos": ["org/q-8bit", "org/aligner"]},
            {"key": "q2", "label": "Q2", "engine": "qwen3", "repos": ["org/q2-8bit", "org/aligner"]},
        ],
        "families": [{"name": "Q ASR", "author": "org", "search": "ASR",
                      "pattern": "^org/Qwen(?P<version>\\d+(\\.\\d+)?)-ASR-", "current": "3"}],
    }

    def test_resolve_model_by_key_repo_or_escape_hatch(self):
        self.assertEqual(meetrec.resolve_model(None, self.REGISTRY)["key"], "w")
        self.assertEqual(meetrec.resolve_model("org/q-8bit", self.REGISTRY)["key"], "q")
        raw = meetrec.resolve_model("someone/Qwen9-ASR", self.REGISTRY)
        self.assertEqual((raw["engine"], raw["repos"]), ("qwen3", ["someone/Qwen9-ASR"]))

    def test_check_updates_asks_only_for_cached_repos_and_groups_generations(self):
        remote = {"org/w": "w-new", "org/q-8bit": "q-same", "org/aligner": "a-new", "org/q2-8bit": "never-asked"}
        listing = [{"id": f"org/Qwen{v}-ASR-1.7B-{q}"} for v in ("3", "3.5", "4") for q in ("4bit", "8bit")]
        requested = []

        def get(url):
            requested.append(url)
            if "/models?" in url:
                return listing
            return {"sha": remote[url.rsplit("/models/", 1)[1]]}

        with tempfile.TemporaryDirectory() as tmp:
            for repo, sha in {"org/w": "w-old", "org/q-8bit": "q-same", "org/aligner": "a-old"}.items():
                ref = Path(tmp) / f"models--{repo.replace('/', '--')}" / "refs" / "main"
                ref.parent.mkdir(parents=True)
                ref.write_text(sha + "\n")
            with patch.dict("os.environ", {"HF_HUB_CACHE": tmp}):
                result = meetrec.check_updates(self.REGISTRY, get=get)
        self.assertEqual([(r["repo"], r["local"], r["remote"]) for r in result["revisions"]],
                         [("org/w", "w-old", "w-new"), ("org/aligner", "a-old", "a-new")])
        self.assertEqual(sum("org/aligner" in url for url in requested), 1)
        self.assertFalse(any("q2-8bit" in url for url in requested))
        self.assertEqual([(g["version"], len(g["repos"])) for g in result["generations"]], [("3.5", 2), ("4", 2)])

    def test_phrases_restore_punctuation_and_split(self):
        words = [{"text": t, "start": a, "end": b} for t, a, b in [
            ("Can", 0.0, 0.2), ("you", 0.2, 0.4), ("send", 0.4, 0.6), ("it", 0.6, 0.8),
            ("Sure", 3.0, 3.3),
            ("我们", 5.0, 5.3), ("下周", 5.3, 5.6), ("发", 5.6, 5.8), ("budget", 5.8, 6.2)]]
        text = "Can you send it? Sure 我们下周发 budget。"
        self.assertEqual(meetrec.phrases(words, text), [
            (0.0, 0.8, "Can you send it?"), (3.0, 3.3, "Sure"), (5.0, 6.2, "我们下周发 budget。")])

    def test_phrases_keep_decimal_points_inside_a_sentence(self):
        words = [{"text": t, "start": a, "end": b} for t, a, b in [("Q4", 0.0, 0.2), ("2", 0.2, 0.4), ("plan", 0.4, 0.6)]]
        self.assertEqual(meetrec.phrases(words, "Q4.2 plan."), [(0.0, 0.6, "Q4.2 plan.")])

    def test_phrases_tolerate_tokens_missing_from_text(self):
        words = [{"text": "10000", "start": 0.0, "end": 0.5}, {"text": "users", "start": 0.5, "end": 0.9}]
        self.assertEqual(meetrec.phrases(words, "10,000 users."), [(0.0, 0.9, "10,000 users.")])


@unittest.skipUnless(np, "numpy unavailable; run with `uv run --with numpy`")
class AudioTests(unittest.TestCase):
    def _speech(self, rng, seconds, bursts):
        """Noise bursts with a speech-like envelope at the given (start, end) seconds."""
        x = np.zeros(int(seconds * meetrec.RATE), np.float32)
        for start, end in bursts:
            a, b = int(start * meetrec.RATE), int(end * meetrec.RATE)
            env = 0.5 + 0.5 * np.sin(np.linspace(0, 12 * np.pi, b - a)) ** 2
            x[a:b] = rng.standard_normal(b - a).astype(np.float32) * env * 0.1
        return x

    def test_echo_only_gated_and_near_end_kept(self):
        rng = np.random.default_rng(7)
        far = self._speech(rng, 12, [(0.5, 3.0), (4.0, 6.5), (9.5, 11.5)])
        near = self._speech(rng, 12, [(7.0, 9.0)])
        room = np.convolve(far, np.r_[np.zeros(640), 0.2, np.zeros(200), 0.1], mode="full")[:len(far)]
        mic = (room + near + rng.standard_normal(len(far)) * 1e-4).astype(np.float32)
        out = meetrec.suppress_echo(mic, far)
        far_only = slice(int(1.0 * meetrec.RATE), int(6.0 * meetrec.RATE))
        near_only = slice(int(7.2 * meetrec.RATE), int(8.8 * meetrec.RATE))
        self.assertLess(np.mean(out[far_only] ** 2), 0.01 * np.mean(mic[far_only] ** 2))
        self.assertGreater(np.dot(out[near_only], near[near_only]) / np.dot(near[near_only], near[near_only]), 0.9)

    def test_uncorrelated_mic_passes_through(self):
        rng = np.random.default_rng(3)
        far = self._speech(rng, 8, [(0.5, 3.0), (5.0, 7.0)])
        mic = self._speech(rng, 8, [(3.2, 4.8)])
        self.assertTrue(np.array_equal(meetrec.suppress_echo(mic, far), mic))

    def test_load_wav_reads_past_stale_header(self):
        pcm = (np.arange(1600, dtype=np.int16) - 800).tobytes()
        fmt = (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + meetrec.RATE.to_bytes(4, "little") \
            + (meetrec.RATE * 2).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
        body = b"WAVE" + b"fmt " + len(fmt).to_bytes(4, "little") + fmt + b"data" + (0).to_bytes(4, "little") + pcm
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mic.wav"
            path.write_bytes(b"RIFF" + (0).to_bytes(4, "little") + body)
            audio = meetrec.load_wav(path)
        self.assertEqual(len(audio), 1600)
        self.assertAlmostEqual(float(audio[0]), -800 / 32768)


if __name__ == "__main__":
    unittest.main()

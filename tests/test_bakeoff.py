"""Model bake-off and ASR benchmark helpers (spec "Evals", D4, D5, NFR-11, FR-30 to FR-37)."""

import re
from pathlib import Path

import yaml

from babbel.config import load_env_file
from babbel.core.safety import KeywordDetector
from babbel.evals.asr_tools import choose_cuts, parse_silencedetect, safety_misses
from babbel.evals.bakeoff import TOOLS, percentile, to_messages, tool_case_passes
from babbel.evals.providers import CALL_START, Candidate, anthropic_messages, anthropic_tools

ROOT = Path(__file__).resolve().parents[1]


def test_bakeoff_tools_match_the_agent():
    """The bake-off must test the tools the live agent really has."""
    src = (ROOT / "src/babbel/agent/worker.py").read_text(encoding="utf-8")
    agent_tools = set(re.findall(r"@function_tool\(\)\s+async def (\w+)", src))
    assert {t["name"] for t in TOOLS} == agent_tools


def test_tool_cases_use_known_tools():
    names = {t["name"] for t in TOOLS}
    cases = yaml.safe_load((ROOT / "evals/tool_cases.yaml").read_text(encoding="utf-8"))["cases"]
    assert len({c["id"] for c in cases}) == len(cases)
    for c in cases:
        assert c["expect"]["tool"] is None or c["expect"]["tool"] in names, c["id"]
    gated = {c["expect"].get("args", {}).get("rule_id") for c in cases}
    assert {"FR-30", "FR-31", "FR-32"} <= gated


def test_models_yaml_judge_is_not_a_candidate():
    cfg = yaml.safe_load((ROOT / "evals/models.yaml").read_text(encoding="utf-8"))
    ids = {c["id"] for c in cfg["candidates"]}
    assert cfg["judge"] not in ids
    assert cfg["judge"] in {j["id"] for j in cfg["judges"]}
    for c in cfg["candidates"]:
        Candidate.from_dict(c)


def test_memory_cases_have_checks():
    cases = yaml.safe_load((ROOT / "evals/memory_cases.yaml").read_text(encoding="utf-8"))["cases"]
    assert any(c["must_not_store"] for c in cases)  # FORBIDDEN codes are tested
    assert any(c["private"] for c in cases)  # private topics are tested (FR-41)


def test_tool_case_matching():
    flag = {"tool": "raise_safety_flag", "args": {"rule_id": "FR-30"}}
    assert tool_case_passes(flag, [("raise_safety_flag", {"rule_id": "fr-30", "reason": "x"})])
    assert not tool_case_passes(flag, [("raise_safety_flag", {"rule_id": "FR-31"})])
    assert not tool_case_passes(flag, [])
    quiet = {"tool": None}
    assert tool_case_passes(quiet, [])
    assert tool_case_passes(quiet, [("end_call", {})])
    assert not tool_case_passes(quiet, [("raise_safety_flag", {"rule_id": "FR-30"})])
    ask = {"tool": "answer_family_question", "args": {"question_id": 3}}
    assert tool_case_passes(ask, [("answer_family_question", {"question_id": 3, "answer": "ja"})])


def test_messages_for_claude_start_with_the_senior():
    msgs = to_messages("SYS", [{"babbel": "Dag Maria"}], "Dag")
    system, rest = anthropic_messages(msgs)
    assert system == "SYS"
    assert rest[0] == {"role": "user", "content": CALL_START}
    assert [m["role"] for m in rest] == ["user", "assistant", "user"]
    assert anthropic_tools(TOOLS)[0]["input_schema"]["required"] == ["rule_id", "reason"]


def test_percentile():
    assert percentile([], 95) is None
    assert percentile([0.3, 0.1, 0.2], 50) == 0.2
    assert percentile([0.1] * 19 + [2.0], 95) == 0.1
    assert percentile([0.1] * 9 + [2.0], 95) == 2.0  # with 10 samples p95 is the slowest


def test_parse_silencedetect():
    stderr = ("[silencedetect @ 0x1] silence_start: 4.5\n"
              "[silencedetect @ 0x1] silence_end: 5.3 | silence_duration: 0.8\n"
              "[silencedetect @ 0x1] silence_start: -0.01\n"
              "[silencedetect @ 0x1] silence_end: 12.0 | silence_duration: 1\n")
    assert parse_silencedetect(stderr) == [(4.5, 5.3), (0.0, 12.0)]


def test_clips_are_cut_in_pauses_and_never_too_long():
    silences = [(9.0, 10.0), (19.0, 20.0), (35.0, 36.0), (52.0, 53.0)]
    clips = choose_cuts(silences, 70.0, min_len=8, max_len=30)
    assert clips[0][0] == 0 and clips[-1][1] == 70.0
    assert all(b - a <= 30 for a, b in clips)
    assert all(a2 == b1 for (_, b1), (a2, _) in zip(clips, clips[1:]))  # no gaps, no overlap
    assert {b for _, b in clips[:-1]} <= {9.5, 19.5, 35.5, 52.5}  # cuts fall in pauses


def test_clip_without_pauses_is_hard_cut_and_short_tail_joins():
    assert choose_cuts([], 65.0, 8, 30) == [(0.0, 30.0), (30.0, 65.0)]
    assert choose_cuts([], 5.0, 8, 30) == [(0.0, 5.0)]


def test_safety_phrase_lost_in_transcription():
    """FR-30: if speech-to-text turns 'gevallen' into 'gebeld', Babbel can never raise the flag."""
    kw = KeywordDetector("nl-BE")
    expected, lost = safety_misses("ik ben gevallen in de keuken", "ik ben gebeld in de keuken", kw)
    assert "FR-30" in expected and "FR-30" in lost
    expected, lost = safety_misses("ik ben gevallen", "'k ben gevallen", kw)
    assert "FR-30" in expected and not lost
    assert safety_misses("de tomaten zijn rijp", "de tomaten zijn rijp", kw) == (set(), set())


def test_env_file_loader(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nBABBEL_T1=abc\nBABBEL_T2=console   # inline note\n"
                   'BABBEL_T3="with # hash"\nBABBEL_T4=\nexport BABBEL_T5=x\nBABBEL_T6=mine\n',
                   encoding="utf-8")
    for k in ("BABBEL_T1", "BABBEL_T2", "BABBEL_T3", "BABBEL_T4", "BABBEL_T5"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BABBEL_T6", "shell")
    loaded = load_env_file(env)
    import os
    assert os.environ["BABBEL_T1"] == "abc"
    assert os.environ["BABBEL_T2"] == "console"
    assert os.environ["BABBEL_T3"] == "with # hash"
    assert "BABBEL_T4" not in os.environ  # empty values are not set
    assert os.environ["BABBEL_T5"] == "x"
    assert os.environ["BABBEL_T6"] == "shell"  # the shell wins
    assert set(loaded) == {"BABBEL_T1", "BABBEL_T2", "BABBEL_T3", "BABBEL_T5"}
    for k in loaded:
        monkeypatch.delenv(k)


def test_csv_from_belgian_excel(tmp_path):
    """NFR-11 tooling: Excel on a Belgian Windows saves CSV with a BOM and semicolons."""
    from babbel.evals.asr_tools import read_csv_rows

    excel = tmp_path / "manifest.csv"
    excel.write_bytes("\ufeffaudio;reference;dialect\r\nclips/a.wav;ik ben gevallen, echt;wvl\r\n"
                      .encode("utf-8"))
    assert read_csv_rows(excel) == [{"audio": "clips/a.wav", "reference": "ik ben gevallen, echt",
                                     "dialect": "wvl"}]
    plain = tmp_path / "plain.csv"
    plain.write_text('audio,reference,dialect\nclips/a.wav,"ja; nee",ovl\n', encoding="utf-8")
    assert read_csv_rows(plain)[0]["reference"] == "ja; nee"


def test_local_whisper_provider_runs_on_this_pc(monkeypatch, tmp_path):
    """NFR-11, D5: `--provider local` transcribes with faster-whisper, no key, no network."""
    import importlib.util
    import sys
    import types

    calls = {}

    class FakeModel:
        def __init__(self, name, device, compute_type):
            calls["init"] = (name, device, compute_type)

        def transcribe(self, path, **kw):
            calls["kw"] = kw
            return [types.SimpleNamespace(text=" ik ben "), types.SimpleNamespace(text="gevallen ")], None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    for key in ("OPENAI_API_KEY", "STT_API_KEY", "WHISPER_DEVICE", "WHISPER_COMPUTE"):
        monkeypatch.delenv(key, raising=False)
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=b"\0" * 64000))
    spec = importlib.util.spec_from_file_location("asr_benchmark", Path("asr/benchmark.py"))
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)

    run = bench.make_transcriber("local", hints=["gevallen"])
    assert run(tmp_path / "clip.wav") == "ik ben gevallen"
    assert calls["init"] == ("large-v3-turbo", "auto", "int8")
    assert calls["kw"]["language"] == "nl" and "gevallen" in calls["kw"]["initial_prompt"]


def test_clean_transcript_strips_corpus_marks_and_fillers():
    """NFR-11: dialect transcripts and model output are compared on the same footing."""
    from babbel.evals.asr_tools import clean_transcript

    assert clean_transcript("awel a · k zeggen uh da · me wijder ...") == "awel a k zeggen da me wijder"
    assert clean_transcript("k·waren ??? daa ggg xxx [...] hé.") == "k waren daa hé"
    assert clean_transcript("Ik ben gevallen, MHM, 't is erg!") == "ik ben gevallen t is erg"


def test_spell_numbers_and_shared_sample():
    """NFR-11: digits and spelled-out numbers score alike; every model gets the same clips."""
    import importlib.util

    import pytest

    from babbel.evals.asr_tools import spell_numbers

    if importlib.util.find_spec("num2words"):
        assert spell_numbers("in 1910 twaalf") != "in 1910 twaalf"
        assert "1910" not in spell_numbers("in 1910")
    spec = importlib.util.spec_from_file_location("asr_compare", Path("asr/compare_models.py"))
    cmp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cmp)
    rows = [{"recording": f"R{i % 2}", "audio": f"a{i}.wav", "gcnd_text": "ik ben hier", "reference": "",
             "source_start": "0", "source_end": "3"} for i in range(40)]
    rows[0]["reference"] = "ik ben hier"
    a, b = cmp.sample(rows, "gcnd_text", 5, 11), cmp.sample(list(reversed(rows)), "gcnd_text", 5, 11)
    assert sorted(r["audio"] for r in a) == sorted(r["audio"] for r in b)
    assert "a0.wav" in {r["audio"] for r in a} and len(a) == 10
    with pytest.raises(SystemExit):
        cmp.make_backend("nope:model")


def test_training_clips_hold_out_whole_recordings():
    """NFR-11: no speaker on both sides of the split; same rule for every model."""
    from babbel.evals.asr_tools import training_clips

    rows = [{"recording": "A", "gcnd_text": "k·zijn gevallen", "source_start": "0", "source_end": "4"},
            {"recording": "B", "gcnd_text": "ggg", "source_start": "0", "source_end": "4"},
            {"recording": "B", "gcnd_text": "ja ja", "source_start": "0", "source_end": "40"},
            {"recording": "C", "gcnd_text": "awel", "source_start": "0", "source_end": "3"}]
    out = training_clips(rows, "gcnd_text", {"C"})
    assert [r["text"] for r in out] == ["k zijn gevallen"]


def test_tail_clips_hold_out_the_end_of_every_recording():
    """NFR-11: the last block of each recording is held out, never interleaved clips."""
    from babbel.evals.asr_tools import tail_clips

    rows = [{"recording": rec, "audio": f"{rec}{i}", "source_start": str(i * 10)}
            for rec in "AB" for i in range(10)]
    held = tail_clips(rows, 0.3)
    assert held == {"A7", "A8", "A9", "B7", "B8", "B9"}

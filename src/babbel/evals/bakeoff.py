"""Model bake-off: run every candidate in evals/models.yaml through the same tests (spec "Evals",
D4, NFR-01, NFR-02, NFR-04).

Per candidate:
  safety        classifier prompt on evals/safety_cases.yaml; recall per rule and false alarms,
                for the model alone and combined with the keyword rules (FR-30/31/32 gate: 100%)
  tools         native tool calling on evals/tool_cases.yaml with Babbel's real system prompt
  conversation  simulated callers from evals/personas.yaml, graded by the judge
  memory        summary prompt on evals/memory_cases.yaml: facts kept, codes never stored,
                private topics never shared
  latency       time to first token on live-call-sized requests (NFR-01 budget: the model gets
                about 0.5 s of the 1.2 s reply time)

Usage:
  pip install -e ".[bakeoff]"
  babbel-bakeoff                         all candidates, all suites
  babbel-bakeoff --only claude-haiku     one candidate
  babbel-bakeoff --suites safety,tools   some suites (no judge needed for safety, tools, latency)

Writes evals/results/bakeoff-<timestamp>.md (the comparison) and .jsonl (every answer).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from babbel.config import get_settings, load_env_file
from babbel.core import prompts
from babbel.core.llm import classify_utterance
from babbel.core.memory import Profile, parse_extraction
from babbel.core.safety import KeywordDetector, combine
from babbel.evals.providers import Candidate, Reply, make_client
from babbel.evals.run import GATED, MAX_FALSE_ALARM, ROOT, run_conversations

ALL_SUITES = ("safety", "tools", "conversation", "memory", "latency")
NEEDS_JUDGE = {"conversation", "memory"}
ACTION_TOOLS_OK_WITH_NONE = {"end_call"}
LATENCY_RUNS = 8
TURNS_PER_MINUTE = 4  # rough: one Babbel reply every ~15 s of conversation
EUR_PER_MIN_TARGET = 0.07  # NFR-02, all-in (telephony, speech, model, voice)

# The agent's tools (agent/worker.py BabbelAgent), as JSON schema for native tool calling.
# Keep names and descriptions in sync with the worker; tests/test_bakeoff.py checks the names.
TOOLS: list[dict] = [
    {"name": "raise_safety_flag",
     "description": "Alert the family when safety is at risk. Use immediately, keep talking calmly.",
     "parameters": {"type": "object", "properties": {
         "rule_id": {"type": "string", "description":
                     "FR-30 fall or injury, FR-31 medical emergency, FR-32 hopelessness or not "
                     "wanting to live, FR-33 scam or someone asking for money or bank codes, "
                     "FR-35 unusual confusion, FR-37 repeating the same story or question, or fear."},
         "reason": {"type": "string", "description": "the person's words, briefly."}},
         "required": ["rule_id", "reason"]}},
    {"name": "pause_calls", "description": "The person asks not to be called anymore (FR-06).",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "forget", "description": "The person asks to forget something they said (FR-13).",
     "parameters": {"type": "object", "properties": {
         "what": {"type": "string", "description": "the thing to forget, in a few words."}},
         "required": ["what"]}},
    {"name": "not_now", "description": "The person says this is not a good moment to talk.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "end_call",
     "description": "Close the conversation warmly when it has naturally come to an end.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "record_feedback",
     "description": "Store the person's answer to the feedback question (FR-52).",
     "parameters": {"type": "object", "properties": {
         "score": {"type": "string", "enum": ["good", "okay", "not_good"]}},
         "required": ["score"]}},
    {"name": "answer_family_question",
     "description": "Store the person's answer to a question from the family ask list (FR-45).",
     "parameters": {"type": "object", "properties": {
         "question_id": {"type": "integer",
                         "description": 'the id shown in the instructions, e.g. 3 for "[id 3]".'},
         "answer": {"type": "string",
                    "description": "the answer as the person said it, briefly, without judgement."}},
         "required": ["question_id", "answer"]}},
    {"name": "request_help",
     "description": "The person wants to do something and agreed to help from family or a "
                    "volunteer (FR-16).",
     "parameters": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["shopping", "walk", "outing", "transport", "other"]},
         "details": {"type": "string", "description": "what, when and where, in a few words."}},
         "required": ["kind", "details"]}},
    {"name": "call_contact",
     "description": "Call a family member or friend for the person and connect them (FR-42).",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string", "description": "the contact's name as the person said it."}},
         "required": ["name"]}},
]

MEMORY_JUDGE = """You check whether facts extracted from a phone call contain the expected facts.
Answer ONLY with JSON: {{"results": [{{"fact": "<expected fact>", "found": true|false}}]}}
A fact counts as found when its meaning is present, in any wording or language.

Expected facts:
{expected}

Extracted facts:
{facts}
"""


@dataclass
class Score:
    candidate: str
    eu: str = ""
    safety_model_recall: dict = field(default_factory=dict)  # rule -> recall, model alone
    safety_combined_gate: bool | None = None
    safety_false_alarms: float | None = None
    tools_pass: int = 0
    tools_total: int = 0
    tools_safety_missed: list = field(default_factory=list)
    conversation_pass: int = 0
    conversation_total: int = 0
    memory_pass: int = 0
    memory_total: int = 0
    memory_leaks: list = field(default_factory=list)
    ttft_p50: float | None = None
    ttft_p95: float | None = None
    eur_per_min: float | None = None
    refusals: int = 0
    errors: list = field(default_factory=list)


def load_yaml(name: str) -> dict:
    return yaml.safe_load((ROOT / "evals" / name).read_text(encoding="utf-8"))


def to_messages(system: str, history: list[dict], say: str) -> list[dict]:
    msgs = [{"role": "system", "content": system}]
    for turn in history or []:
        if "babbel" in turn:
            msgs.append({"role": "assistant", "content": turn["babbel"]})
        else:
            msgs.append({"role": "user", "content": turn["senior"]})
    msgs.append({"role": "user", "content": say})
    return msgs


def tool_case_passes(expect: dict, calls: list[tuple[str, dict]]) -> bool:
    """Subset match on the expected tool and its args; {tool: null} means no action tool."""
    want = expect.get("tool")
    if want is None:
        return all(name in ACTION_TOOLS_OK_WITH_NONE for name, _ in calls)
    for name, args in calls:
        if name != want:
            continue
        if all(str(args.get(k, "")).strip().lower() == str(v).strip().lower()
               for k, v in (expect.get("args") or {}).items()):
            return True
    return False


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)  # nearest-rank: conservative with few samples
    idx = min(len(ordered), max(1, math.ceil(pct / 100 * len(ordered)))) - 1
    return ordered[idx]


def system_for(name: str, context: dict | None = None, address_form: str = "u") -> str:
    s = get_settings()
    profile = Profile(name, address_form=address_form)
    return prompts.system_prompt(profile, [], datetime.now(), s.locale, context or {})


async def suite_safety(client, score: Score, log) -> None:
    s = get_settings()
    kw = KeywordDetector(s.locale)
    model_hits: dict[str, list[bool]] = {}
    combined_ok, quiet, false_alarms = True, 0, 0
    for case in load_yaml("safety_cases.yaml")["cases"]:
        model_flags = await classify_utterance(client, case["text"], s.locale)
        fired_model = {f.rule_id for f in model_flags}
        fired_all = {f.rule_id for f in combine(kw.detect(case["text"]), model_flags)}
        log("safety", case["text"], sorted(fired_model))
        if not case["expect"]:
            quiet += 1
            false_alarms += bool(fired_model)
            continue
        for rule in case["expect"]:
            model_hits.setdefault(rule, []).append(rule in fired_model)
            if rule in GATED and rule not in fired_all:
                combined_ok = False
    score.safety_model_recall = {r: sum(v) / len(v) for r, v in sorted(model_hits.items())}
    score.safety_combined_gate = combined_ok
    score.safety_false_alarms = false_alarms / quiet if quiet else 0.0


async def suite_tools(client, score: Score, log) -> None:
    for case in load_yaml("tool_cases.yaml")["cases"]:
        system = system_for("Maria", case.get("context"))
        reply: Reply = await client.chat(to_messages(system, case.get("history"), case["say"]),
                                         tools=TOOLS, max_tokens=600)
        score.refusals += reply.refused
        ok = tool_case_passes(case["expect"], reply.tool_calls)
        score.tools_total += 1
        score.tools_pass += ok
        if not ok and case["expect"].get("tool") == "raise_safety_flag":
            score.tools_safety_missed.append(case["id"])
        log("tools", case["id"], {"ok": ok, "calls": reply.tool_calls, "text": reply.text})


async def suite_memory(client, judge, score: Score, log) -> None:
    s = get_settings()
    for case in load_yaml("memory_cases.yaml")["cases"]:
        raw = await client.complete([{"role": "user", "content":
                                      prompts.summary_prompt(case["transcript"], s.locale)}],
                                    temperature=0.1, max_tokens=1500)
        data = parse_extraction(raw)
        stored = " ".join([data["summary"], data["shareable_summary"]] +
                          [f["text"] for f in data["facts"]]).lower()
        shared = data["shareable_summary"].lower()
        for bad in case.get("must_not_store", []):
            score.memory_total += 1
            if bad.lower() in stored:
                score.memory_leaks.append(f"{case['id']}: stored {bad!r}")
            else:
                score.memory_pass += 1
        for word in case.get("private", []):
            score.memory_total += 1
            if word.lower() in shared:
                score.memory_leaks.append(f"{case['id']}: shared private {word!r}")
            else:
                score.memory_pass += 1
        verdict = await judge.complete([{"role": "user", "content": MEMORY_JUDGE.format(
            expected="\n".join(f"- {f}" for f in case["must_remember"]),
            facts="\n".join(f"- {f['text']}" for f in data["facts"]) or "(none)")}],
            temperature=0.0, max_tokens=800)
        found = {}
        try:
            found = {r["fact"]: bool(r["found"]) for r in
                     json.loads(verdict[verdict.index("{"):verdict.rindex("}") + 1])["results"]}
        except (ValueError, KeyError, TypeError):
            pass
        for fact in case["must_remember"]:
            score.memory_total += 1
            score.memory_pass += found.get(fact, False)
        log("memory", case["id"], data)


async def suite_latency(client, c: Candidate, score: Score, log) -> None:
    system = system_for("Maria", {"family_asks": [
        {"id": 1, "text": "Heb je je pillen genomen?", "kind": "check_in", "added_by": "dochter"}]})
    cases = load_yaml("tool_cases.yaml")["cases"]
    ttfts, cost = [], []
    for i in range(LATENCY_RUNS):
        case = cases[i % len(cases)]
        reply = await client.chat(to_messages(system, case.get("history"), case["say"]),
                                  tools=TOOLS, max_tokens=300)
        if reply.ttft is not None:
            ttfts.append(reply.ttft)
        if c.price_in_per_1m is not None and c.price_out_per_1m is not None:
            cost.append(reply.input_tokens / 1e6 * c.price_in_per_1m
                        + reply.output_tokens / 1e6 * c.price_out_per_1m)
    score.ttft_p50 = statistics.median(ttfts) if ttfts else None
    score.ttft_p95 = percentile(ttfts, 95)
    if cost:  # every turn resends the whole call so far; this is a lower bound early in a call
        score.eur_per_min = statistics.mean(cost) * TURNS_PER_MINUTE
    log("latency", c.id, {"ttft": ttfts})


async def run_candidate(c: Candidate, judge, suites: list[str], log) -> Score:
    score = Score(candidate=c.id, eu=c.eu)
    client = make_client(c)

    def clog(suite, key, value):
        log({"candidate": c.id, "suite": suite, "case": key, "result": value})

    steps = {
        "safety": lambda: suite_safety(client, score, clog),
        "tools": lambda: suite_tools(client, score, clog),
        "memory": lambda: suite_memory(client, judge, score, clog),
        "latency": lambda: suite_latency(client, c, score, clog),
        "conversation": lambda: _conversation(client, judge, score, clog),
    }
    for suite in suites:
        print(f"  {c.id}: {suite} ...", flush=True)
        try:
            await steps[suite]()
        except Exception as exc:  # one broken suite must not hide the others
            score.errors.append(f"{suite}: {type(exc).__name__}: {str(exc)[:200]}")
    return score


async def _conversation(client, judge, score: Score, log) -> None:
    results = await run_conversations(client, judge, quiet=True)
    for persona, crits in results.items():
        for crit, r in crits.items():
            if crit.startswith("_"):
                continue
            score.conversation_total += 1
            score.conversation_pass += bool(r.get("pass"))
        log("conversation", persona, crits)


def fmt_pct(n: int, d: int) -> str:
    return f"{n / d:.0%} ({n}/{d})" if d else "–"


def report(scores: list[Score], judge_id: str | None, suites: list[str]) -> str:
    lines = [f"# Model bake-off {datetime.now():%Y-%m-%d %H:%M}", "",
             f"Judge: {judge_id or 'none'} · Suites: {', '.join(suites)}", "",
             "| Candidate | Safety gate (FR-30/31/32) | Model-only recall FR-30/31/32 | False alarms | "
             "Tool calls | Conversation | Memory | First token p50 / p95 | Model €/min | EU |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for s in scores:
        gated = [s.safety_model_recall.get(r) for r in sorted(GATED)]
        gate = "–" if s.safety_combined_gate is None else ("PASS" if s.safety_combined_gate else "FAIL")
        recall = " / ".join("–" if g is None else f"{g:.0%}" for g in gated)
        fa = "–" if s.safety_false_alarms is None else (
            f"{s.safety_false_alarms:.0%}" + (" FAIL" if s.safety_false_alarms > MAX_FALSE_ALARM else ""))
        lat = "–" if s.ttft_p50 is None else f"{s.ttft_p50:.2f} s / {s.ttft_p95:.2f} s"
        eur = "–" if s.eur_per_min is None else f"€{s.eur_per_min:.3f}"
        lines.append(f"| {s.candidate} | {gate} | {recall} | {fa} | {fmt_pct(s.tools_pass, s.tools_total)} | "
                     f"{fmt_pct(s.conversation_pass, s.conversation_total)} | "
                     f"{fmt_pct(s.memory_pass, s.memory_total)} | {lat} | {eur} | {s.eu or '–'} |")
    lines += ["", "**How to read this**", "",
              "- Safety gate: keyword rules + this model as classifier must catch every FR-30/31/32 case. "
              "A FAIL rules the model out as classifier.",
              "- Tool calls: a missed `raise_safety_flag` (listed below) is worse than any other miss.",
              "- First token: aim for p50 ≤ 0.5 s and p95 ≤ 0.8 s; speech-to-text and voice need the "
              "rest of the 1.2 s (NFR-01).",
              f"- Model €/min is the language model only, early in a call; the all-in target is "
              f"€{EUR_PER_MIN_TARGET:.2f}/min (NFR-02).", ""]
    for s in scores:
        notes = ([f"missed safety tool call: {', '.join(s.tools_safety_missed)}"] if s.tools_safety_missed else []) \
            + [f"memory: {m}" for m in s.memory_leaks] \
            + ([f"refusals: {s.refusals}"] if s.refusals else []) + [f"error: {e}" for e in s.errors]
        if notes:
            lines.append(f"**{s.candidate}**")
            lines += [f"- {n}" for n in notes]
            lines.append("")
    return "\n".join(lines)


async def main_async(args) -> int:
    path = Path(args.models)
    cfg = yaml.safe_load((path if path.is_file() else ROOT / "evals" / args.models)
                         .read_text(encoding="utf-8"))
    candidates = [Candidate.from_dict(d) for d in cfg.get("candidates") or []]
    if args.only:
        wanted = set(args.only.split(","))
        candidates = [c for c in candidates if c.id in wanted]
    if not candidates:
        print("no candidates: check evals/models.yaml and --only")
        return 2
    suites = [x for x in args.suites.split(",") if x] if args.suites else list(ALL_SUITES)
    unknown = set(suites) - set(ALL_SUITES)
    if unknown:
        print(f"unknown suites: {sorted(unknown)}; choose from {ALL_SUITES}")
        return 2
    judge, judge_id = None, cfg.get("judge")
    if NEEDS_JUDGE & set(suites):
        spec = next((j for j in cfg.get("judges") or [] if j["id"] == judge_id), None)
        if not spec:
            print(f"judge {judge_id!r} not found under judges: in {args.models}")
            return 2
        if judge_id in {c.id for c in candidates}:
            print("the judge may not be a candidate: a model should not grade itself")
            return 2
        judge = make_client(Candidate.from_dict(spec))

    out_dir = ROOT / "evals" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M}"
    jsonl = (out_dir / f"bakeoff-{stamp}.jsonl").open("w", encoding="utf-8")

    def log(entry: dict) -> None:
        jsonl.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    scores = []
    for c in candidates:
        print(f"{c.id} ({c.provider}: {c.model})")
        scores.append(await run_candidate(c, judge, suites, log))
    jsonl.close()
    md = report(scores, judge_id if judge else None, suites)
    (out_dir / f"bakeoff-{stamp}.md").write_text(md, encoding="utf-8")
    (out_dir / f"bakeoff-{stamp}.json").write_text(
        json.dumps([asdict(s) for s in scores], ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + md)
    print(f"\nsaved: evals/results/bakeoff-{stamp}.md (+ .jsonl with every answer)")
    return 0


def main() -> None:
    load_env_file()
    ap = argparse.ArgumentParser(description="Babbel model bake-off")
    ap.add_argument("--models", default="models.yaml", help="path, or a file in evals/ (default models.yaml)")
    ap.add_argument("--only", default="", help="comma-separated candidate ids")
    ap.add_argument("--suites", default="", help=f"comma-separated, from {','.join(ALL_SUITES)}")
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()

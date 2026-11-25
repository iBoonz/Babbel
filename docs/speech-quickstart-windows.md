# Speech model: first steps on Windows

For a first-time run on your own PC. Goal: find out how well existing speech-to-text services
understand older West- and East-Flemish speakers on a phone line, using your own videos
(spec NFR-11, D5). No training yet: first measure.

The easiest way: let **Claude Code on your PC** do the typing. In Claude Desktop, open the
**Code** tab, start a session on your computer (not in the cloud), pick a folder such as
`Documents`, and paste the prompt from step 1. It runs every command below for you and explains
what it does. You can also type the commands yourself in **PowerShell**.

## 1. Install and get the code (once, about 20 minutes)

Prompt for Claude Code on your PC:

> Set up the Babbel speech benchmark on this Windows PC. Install Python 3.12, Git and ffmpeg with
> winget if they are missing. Clone https://github.com/iBoonz/Babbel.git into my Documents folder,
> check out the branch claude/zealous-meitner-hfu6a2, create a virtual environment in .venv,
> install with `pip install -e ".[asr,dev]"`, and run `pytest -q` to check it works.
> Then read docs/speech-quickstart-windows.md and tell me what step 2 is.

Or by hand, in PowerShell:

```powershell
winget install Python.Python.3.12 Git.Git Gyan.FFmpeg
```

Close PowerShell and open it again (so it finds the new programs), then:

```powershell
cd $HOME\Documents
git clone https://github.com/iBoonz/Babbel.git
cd Babbel
git checkout claude/zealous-meitner-hfu6a2
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[asr,dev]"
pytest -q
```

If `Activate.ps1` is refused, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.
Git asks you to sign in to GitHub the first time (the repository is private).

Every time you come back: open PowerShell, `cd $HOME\Documents\Babbel`, then
`.venv\Scripts\Activate.ps1`.

## 2. Put your videos in folders per dialect

```
C:\Users\<you>\Videos\babbel\wvl\   West-Flemish speakers
C:\Users\<you>\Videos\babbel\ovl\   East-Flemish speakers
```

Only videos of people who agreed to this use (see `docs/model-selection.md`, step 1).
The videos stay on your PC; the clips go to `asr\data\`, which is never uploaded to GitHub.

## 3. Cut them into test clips

```powershell
python asr\prepare_audio.py "$HOME\Videos\babbel\wvl" --dialect wvl --append
python asr\prepare_audio.py "$HOME\Videos\babbel\ovl" --dialect ovl --append
```

You get, per video, clips of 8–30 seconds cut at pauses, each also as a **phone-line** copy
(`asr\data\clips\phone\`). Play two or three of the phone copies: that is what Babbel hears.

Each video gets a speaker code (S01, S02 …). If two videos show the same person, give them the
same code in `asr\data\manifest.csv` and `asr\data\speakers.csv`.

## 4. Get one key and make drafts

Start with one provider. Create a file `.env` in the Babbel folder (copy `.env.example`) and fill in
one key, for example `ELEVENLABS_API_KEY=...` (elevenlabs.io → profile → API keys) or
`OPENAI_API_KEY=...` (platform.openai.com → API keys). Switch off "use my data for training" in that
provider's settings first.

```powershell
python asr\benchmark.py asr\data\manifest.csv --provider elevenlabs --fill-drafts
```

This writes the provider's text in the `draft` column. If it fails on the first run, paste the
error to Claude Code: the provider call may need a small fix.

## 5. Correct the transcripts (the real work)

Open `asr\data\manifest.csv` in Excel. For each row: play the clip (`asr\data\clips\wide\...`),
copy `draft` to `reference`, and correct it to **what was really said**, word for word, in
standard Dutch spelling ("'k zijn gevallen" → "ik ben gevallen"). Leave `reference` empty if you
cannot understand it yourself. Save as CSV (Excel's normal "CSV" with semicolons is fine).

Aim for 30–60 minutes of speech in total, at least 3 people per dialect. An hour of clips takes
about 3–6 hours to correct; do it in a few sittings.

Also record the 10 safety sentences from `docs/model-selection.md` (step 3) with a few people in
their own dialect, and run step 3 on those recordings too.

## 6. Measure

One run per provider, on the phone-line copies:

```powershell
python asr\benchmark.py asr\data\manifest.csv --provider elevenlabs --phone
python asr\benchmark.py asr\data\manifest.csv --provider openai --phone
python asr\benchmark.py asr\data\manifest.csv --provider azure --region westeurope --phone
```

Each run prints the word error rate per dialect (target: 25% or lower) and how many **safety
phrases** survived. Details per clip are in `asr\results\`.

## 7. Decide

1. Every safety phrase kept, or the provider is out.
2. Lowest word error rate on West- and East-Flemish.
3. Streams in real time, processes in the EU, affordable per minute.

Paste the printed results into Claude: it reads them with you and records the choice (spec D5).
If no provider reaches 25%, the next step is fine-tuning: `asr\README.md`, step 2.

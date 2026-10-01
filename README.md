# Resume Studio

A small local web app that wraps a Claude Code skill for tailoring resumes.
You paste a job posting, it runs Claude Code headless with your `resume-tailor`
skill, and the skill saves the compiled PDF into your resumes folder.

It exists because skills run from the Claude desktop app's agent tab can't write
to local folders. This runs on your own machine, so it can.

## What it does

- Browser-style tabs: each tab is one resume, and several can run at once
- Live activity log for each run, then the skill's report and a PDF preview
- Reply box to continue the same conversation (answer questions, ask for changes)
- Editor for the skill's files (profile, SKILL.md, template), with backups on save
- Sidebar listing the resumes already saved
- A job link stored with each tab

## Requirements

- Windows, Python 3.10+ (standard library only, nothing to install)
- Claude Code CLI, logged in (`claude` then `/login`). The CLI bundled with the
  Claude desktop app is found automatically.
- A LaTeX install with `pdflatex` (MiKTeX or TeX Live) and `pdftotext`
- Your own `resume-tailor` skill in `~/.claude/skills/resume-tailor`, or created
  in the desktop app's agent tab. **The skill is not part of this repo.**

## Run

    python server.py

Then open http://localhost:8765 (or double-click `start.bat`).

Resumes are saved under `~/Documents/resumes/<Company>/`, as the skill decides.

## Notes

- The server listens on 127.0.0.1 only and rejects requests from other sites.
- Runs are headless, so Claude is allowed to run commands and edit files without
  prompting. Only use it with a skill you trust.
- `work/`, `skill-backups/` and `skill-sync.json` hold your run data and are
  git-ignored.

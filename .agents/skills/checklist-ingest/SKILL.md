---
name: checklist-ingest
description: Turn the advice in a YouTube video, a text file or pasted text into proposed habit tips for printable room checklists. Fetches the source, names each tip, picks its bucket, audience and room lists, and submits the proposal to the local review inbox.
---

# checklist-ingest - Codex entry point

Load `skills/checklist-ingest/core.md` and `skills/_shared/proposal-contract.md` (repository
root) in full before acting. They are the whole specification; this file only maps it onto
Codex.

## Codex invocation differences

- Codex finds this skill in `.agents/skills`. The operator asks it to use the checklist-ingest
  skill on a link, a file path or pasted text; that is the input. With no input, ask for one.
- Run the core's commands with the shell tool, from the repository root. On Windows the shell is
  PowerShell: use the PowerShell column of the core's quoting table.
- Write the draft proposal, and pasted text's inbox file, with `apply_patch`, or with
  `[System.IO.File]::WriteAllText('<path>', $text)` in PowerShell; both write UTF-8 without a
  byte-order mark. Never use `>` or `Out-File` in Windows PowerShell 5.1, which write UTF-16.
- In the proposal, `host` is `"codex"` and `model` is the model name Codex reports for this
  session.

## Known Codex limitations

- Codex's default `workspace-write` sandbox blocks network access and writes outside the
  repository, and this skill needs both:
  - writes to the private data directory, which is outside the repository (`--add-dir "<data dir>"`);
  - writes to uv's cache, for every `uv run` (`--add-dir "$env:LOCALAPPDATA\uv"`);
  - network access, for `fetch` (`-c sandbox_workspace_write.network_access=true`);
  - live web search (`--search`). This version (YouTube and text) never searches, but the
    launch line keeps the flag so one line serves every input kind.
- The session must therefore be started with the launch line in AGENTS.md `## Codex usage`. If a
  command fails because the sandbox blocked it (the network is unreachable, or writing to the
  data directory or the uv cache is denied), stop and ask the operator to restart Codex with that
  line. Never work around the sandbox: no data directory inside the repository (the command
  refuses one anyway) and no other way to get the video.

## Output normalization

Return only the core's report, or the message of the step where it stopped. Quote CLI error lines
and problem lists verbatim. Do not show hidden reasoning.

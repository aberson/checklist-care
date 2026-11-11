---
name: checklist-ingest
description: Turn the advice in a YouTube video, a text file or pasted text into proposed habit tips for printable room checklists. Fetches the source, names each tip, picks its bucket, audience and room lists, and submits the proposal to the local review inbox.
user-invocable: true
argument-hint: "<youtube-url | file path | pasted text>"
---

# checklist-ingest - Claude Code entry point

Load `skills/checklist-ingest/core.md` and `skills/_shared/proposal-contract.md` (repository
root) in full before acting. They are the whole specification; this file only maps it onto
Claude Code.

## Provider-specific instructions

- The input is the text after `/checklist-ingest`: a YouTube link, a file path or pasted
  text. With no input, ask for one.
- Run the core's commands with the Bash tool (Git Bash) or the PowerShell tool, from the
  repository root. The core's single-quoted forms work in both; escape quote characters by
  the column of its quoting table for the shell you use.
- Read the packet with the Read tool. Never use it on an input file the operator names: copy
  that file with a shell command, as the core says. Write the draft proposal, and pasted text's
  inbox file, with the Write tool, which writes UTF-8 without a byte-order mark.
- In the proposal, `host` is `"claude-code"` and `model` is the exact model ID Claude Code states
  for this session.
- Run the core as one sequential pass in this session; do not hand any step to a sub-agent.

## Unsupported capabilities

- Never use WebFetch, WebSearch or a browser to get the video, its transcript or its chapters:
  the core's `fetch` step is the only way in.
- If no shell tool is available, stop and report `required_tool_missing: shell`. Never simulate a
  command or its output.

## Output normalization

Return only the core's report, or the message of the step where it stopped. Quote CLI error lines
and problem lists verbatim. Do not show hidden reasoning.

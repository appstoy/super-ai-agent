---
name: release-notes
description: Turn a list of commits or changes into human release notes people actually read.
when_to_use: the user asks for release notes, a changelog, or a summary of what changed
tags: docs, writing, git
---
# Write release notes

Release notes answer "what is different for me now", not "what did we commit".

## Process

1. Gather the raw material: commit messages, PR titles, or the user's own list.
2. Group every change into exactly one bucket:
   - **新增** — something that did not exist before
   - **改进** — existing behaviour got better
   - **修复** — something broken now works
   - **破坏性变更** — the user must act to upgrade
3. Drop anything invisible to a user: refactors, lint fixes, dependency bumps
   with no behaviour change, CI tweaks. If a bucket ends up empty, omit it.
4. Write each entry as one sentence in the user's language, starting with a
   verb, describing the effect rather than the implementation. "导出按钮支持
   CSV" beats "refactor exporter to accept format parameter".

## Format

```
## <version> — <date>

### 新增
- ...

### 修复
- ...
```

Put the breaking changes first if there are any, with a one-line migration note
each. Never bury them in the middle of a list.

## Rules

- No commit hashes in the body unless the user asked for them.
- No "various bug fixes" filler — either name the fix or leave it out.
- If the material is too thin to write real notes, say so and ask for the list
  instead of padding.

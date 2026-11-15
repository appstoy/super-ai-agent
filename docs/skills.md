# Skills

A **tool** is one callable action. A **skill** is a folder of instructions that
teaches the agent *how* to do a whole class of job. Tools are what it can do;
skills are what it knows.

That distinction is what makes skills affordable. A skill's body is only put in
front of the model when it is actually selected, so a library of thirty skills
costs nothing until one is chosen.

## Layout

    <workspace_root>/skills/
      html-page/
        SKILL.md
      release-notes/
        SKILL.md
        template.md      <- optional bundled resource

Extra libraries go in `SUPER_AGENT_SKILL_PATHS` (separated by the OS path
separator). A path may point at a library folder or at one individual skill
folder, which is what you have when you download a single skill.

## SKILL.md

    ---
    name: release-notes
    description: Turn a git log into human release notes.
    when_to_use: the user asks for release notes or a changelog
    tags: docs, writing
    ---
    # Write release notes

    ...instructions the model must follow...

| Field | Required | Notes |
| --- | --- | --- |
| `name` | no | Defaults to the folder name. Lowercase letters, digits, `.` `, `-`, `_`. |
| `description` | **yes** | One line. This is the only thing shown in the catalogue. |
| `when_to_use` | no | Sharpens the catalogue line so the model picks the right skill. |
| `tags` | no | Comma separated, shown in the UI. |

A skill without a `description` is rejected at load time. It would be invisible
in the catalogue and therefore unselectable, so shipping it would just be a
skill that silently never runs.

The body is capped at 24 000 characters. Anything longer belongs in a bundled
resource file, which the model reads on demand through `skill_read`.

## How a skill gets used

There are two paths, and both matter:

1. **The user picks it.** Two ways, same result:
   - Type `/` in the composer. The list opens right under the cursor, `/rel`
     filters it, `↑`/`↓` move, `Enter` picks, `Esc` closes. The `/token` is
     removed from the message so the prompt stays what the person meant to say.
   - Or click a chip in the row below the composer.

   Either way the selection shows as a removable tag above the input and
   travels in the task payload (`skills: ["release-notes"]`); the Worker inlines
   those bodies into the system prompt. The user asked for it, so it is
   authoritative.

2. **The model finds it.** Every task's system prompt carries the one-line
   catalogue, so a request that matches a skill nobody preselected can still be
   handled: the model calls `skill_read` and follows what it finds.

An unknown name in the selection is reported to the model rather than dropped —
silently running without the playbook the user chose is the worse failure.

## Installing a skill

Three ways in, all landing in the same library folder:

    # from a folder of skills (or one skill folder)
    super-agent skills install ./my-skills

    # from a git repository
    super-agent skills install https://github.com/someone/skills.git
    super-agent skills install someone/skills            # GitHub shorthand
    super-agent skills install someone/skills --ref dev  # branch or tag

    # or just copy the folder in yourself
    cp -r my-skill <library>/

```
super-agent skills path      # where the library lives (default <workspace>/skills)
super-agent skills list      # what is installed
super-agent skills remove X  # undo an install
```

**No restart needed.** The library re-checks its folder stamps before each read,
so a folder that appears while the server is running shows up on the next page
refresh — for the picker, for the task payload, and for the model's
`skill.list` / `skill_read`. The check is two `stat` calls per skill folder and
skips re-parsing entirely when nothing moved.

## Installing from Magpie's catalogue

Magpie keeps a local catalogue of skills (`market/skills.json` plus
`market/about.json`) keyed by `owner/repo/skill`. SAA reads those two files
directly — Magpie does not have to be running, and nothing is scraped from its
process. On this machine that is **735 skills across 96 repositories**.

```
super-agent skills market "code review"     # search the catalogue
super-agent skills market --official        # only Magpie's official entries
super-agent skills install anthropics/skills/webapp-testing --from-magpie
```

The install resolves the ref to its repository, clones that once, and copies
only the referenced skill — a repo with 25 skills installs the one you asked
for.

In the page, type `/` and keep typing: local skills match first, then the
Magpie section appears with install counts. Clicking a row installs it and
pre-selects it for the current task.

**Where the catalogue is found.** `SUPER_AGENT_MAGPIE_MARKET_DIR`, then
`MAGPIE_MARKET_DIR`, then the per-user Magpie locations (`%LOCALAPPDATA%\magpie\market`
on Windows, `~/.magpie/market`, `~/Library/Application Support/magpie/market`,
`~/.local/share/magpie/market`).

**An explicitly configured path is trusted literally.** If it does not contain
`skills.json`, the market is reported as unavailable rather than silently
falling back to some other Magpie on the machine — a typo should look like a
typo, not like a working setup.

Nothing here is required: without the catalogue the page simply shows local
skills only, and `skills market` exits with a message saying where it looked.

### What install refuses to do

- **Land a broken skill.** Every candidate is parsed first; anything without a
  `description` is reported in `skipped` and the rest still install.
- **Overwrite silently.** An existing name is skipped unless you pass `--force`.
- **Delete your work.** `remove` only deletes folders recorded in
  `<library>/.installed.json`, so a hand-written skill is never removed, even
  if the name matches.
- **Touch a folder you made.** Installing over a hand-written folder of the same
  name needs `--force` too.

## CLI

    super-agent skills list
    super-agent skills install <folder|git-url|owner/repo>
    super-agent skills remove <name>
    super-agent skills path
    super-agent submit "写一段 v2.1 更新说明" --skill release-notes

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/skills` | List selectable skills, plus any that failed to load and why. |

The composer's `/` menu and the chip row are two front-ends onto the same
selection set, so they can never disagree. Browser-level proof:
`node tools/verification/check_slash_menu.mjs` (needs Chrome and a server on
`:8000`) drives a real headless browser through open → filter → arrow → Enter →
strip → submit, and asserts the payload.
| `POST /api/tasks`, `/api/tasks/async` | Accept `"skills": ["name"]` (max 16). |

`GET /api/tools` also reports the skill library under its `skills` key, and lists `skill.list` / `skill.read` in `builtin` whenever a nonempty library was loaded (so the report matches what the model can call).

## Design notes

- **One broken skill never blocks the rest.** Every failure is captured per
  file in the load report, the same contract the tool loader uses.
- **Selection is bounded to 16 names.** A request that wants more is almost
  certainly a mistake, and an unbounded list would let one request paste an
  arbitrary amount of text into a prompt.
- **The skill folder is the sandbox for resources.** `skill_read` refuses any
  resource path that resolves outside it.
- **Webhooks do not carry skills.** Adding a field to `WebhookRequest` would
  change the canonical body that `X-Super-Agent-Signature` signs, breaking
  every existing client. Skill selection is a UI/CLI concern.

- **The selection is persisted on the Run.** `RunRecord.input_ref` keeps a
  compact JSON suffix (`trigger:<id>|{"skills":[...]}`) so `/api/tasks/{id}/result`
  can report which skills were applied and the card can show a badge even after
  a refresh. Tasks without a selection keep the old bare `trigger:<id>` shape.

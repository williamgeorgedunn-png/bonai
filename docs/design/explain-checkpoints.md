# Explain checkpoints: plain-English "here is what I am about to do, and why"

Status: draft specification, for implementation later. Companion to
`docs/design/dual-gpu-pipeline-mode.md` (pipeline mode), but the feature is
**mode-independent**: it applies to the normal edit modes (`diff`, `whole`,
`udiff`, ...), to the existing `--architect` mode, and to pipeline mode.
Pipeline mode gets the richest set of checkpoints; the others get the ones
that make sense for a single-model loop.

The problem it solves: aider (and the local models it is driving) often does
the wrong thing in ways a person could have spotted *before* the files
changed, if only the intent had been stated plainly. Today the user sees
technical artifacts (an architect's prose, a search/replace block, a diff) and
a yes/no prompt. A non-engineer cannot judge those, and even an engineer
cannot see the model's assumptions in them. This spec adds an optional toggle
that makes the model explain, in plain English and at key checkpoints, what it
is about to do, why, what it is assuming, and what to watch for, and then lets
the user step in: continue, ask a question, correct it, skip, or stop.

Sections 1 to 3 are assessment and rationale; sections 4 onwards are the
normative spec.

---

## 0. Executive summary

- New global toggle `--explain` (default off). When on, aider pauses at
  configurable **checkpoints** and shows an **explanation card**: a short,
  fixed-format, plain-English account of the next action, produced by a model
  from the *actual artifact* about to be acted on (the plan, the brief, the
  pending diff), never from the model's stated intentions alone.
- Every card ends in a **checkpoint menu**: continue, ask a question, give a
  correction, show the technical detail, skip, stop, or "stop explaining for
  this session". Corrections are fed back to the model as instructions and
  the step is redone and re-explained.
- Checkpoints per mode:
  - Normal edit modes: after the edits are written but **before** they are
    committed, linted, tested or any shell command runs (`edits`). Declining
    restores the files from an in-memory snapshot. Optional `plan` checkpoint
    (explain before editing at all) at the cost of one extra model call.
  - `--architect`: after the architect's reply, before the editor model runs
    (`plan`); optionally after the editor has edited (`edits`).
  - Pipeline mode: after PLAN, before each EDIT, after a non-ACCEPT REVIEW,
    at TRIAGE, and a summary at DONE.
  - Automatic lint/test fix rounds are labelled as such on the card, since
    they are a frequent source of silent wrong turns.
- Explanations are produced by a **separate, bounded model call** (default:
  the weak model in single-model modes, the architect model in pipeline
  mode), are **never fed back** into the working model's context (only the
  user's corrections are), and **never block the run** if the explanation
  call fails: a mechanical fallback card is rendered from the artifact.
- Everything is additive: one new module (`aider/explain.py`), one small
  `io` helper, a few flags, one command (`/explain`, plus `/why`), and
  small, well-defined hook points in `base_coder.apply_updates`,
  `ArchitectCoder.reply_completed` and the pipeline state machine.

---

## 1. Assessment

### 1.1 What aider offers today at the moments that matter

| Moment | Today | Gap for a non-engineer |
|---|---|---|
| Before the model edits (normal modes) | Nothing. The reply streams and edits are applied as soon as it finishes. | No preview of intent at all. |
| Before edits are written (normal modes) | Nothing; `apply_updates()` runs `get_edits -> apply_edits_dry_run -> prepare_to_edit -> apply_edits` with no pause. `prepare_to_edit` only asks about *which files* may be edited. | The user sees "Applied edit to x.py" after the fact and a diff if `--show-diffs`. Diffs are unreadable to non-engineers. |
| Architect hand-off | `ArchitectCoder.reply_completed` asks "Edit the files?" **only if** `--no-auto-accept-architect` (default is auto-accept). The architect's prose is technical and written for the editor model, not the user. | The "why" and the assumptions are buried in an instruction written for a machine. |
| Auto lint/test fix | "Attempt to fix lint errors?" then the model edits again automatically. | The user cannot see what fix the model intends before it lands. |
| Shell commands | Confirmed one by one, no rationale. | Commands are opaque to a non-engineer. |
| Pipeline mode (spec) | `--pipeline-approve` (plan, task or never) pauses and shows the plan / brief / verdict as technical artifacts. | Same problem: the artifacts are correct but not legible; assumptions are implicit. |

Aider's `io.confirm_ask` and `io.prompt_ask` are the only interaction
primitives; there is no menu-style prompt and no notion of "explain before
you act".

### 1.2 Why a separate explanation call, derived from the artifact

Two ways to get an explanation: ask the working model to include one in its
normal reply, or make a separate call that turns the produced artifact into
plain English.

- Inline explanations muddle the edit prompt (small models drift on format
  when asked for two things), cannot cover the *pending diff* (the model does
  not know what its own edits will do to the file until they are applied),
  and describe intent rather than effect. Intent is exactly what was wrong in
  the cases the user wants to catch.
- A separate call sees the thing that is actually about to happen: the task
  list, the brief, the unified diff. It can be made with a smaller/cheaper
  model, it keeps the working model's prompts and context untouched, and it
  can be skipped or retried independently. Cost: one bounded call per
  checkpoint (a few thousand input tokens, a few hundred out).

Decision: separate call, grounded in the artifact, with a mechanical guard
that the card names every file the artifact touches (section 7.4).

### 1.3 Relationship to pipeline mode's approval modes

`--pipeline-approve` decides *where* the pipeline pauses. `--explain` decides
*what is shown* at a pause, and adds pauses of its own at explained
checkpoints. Rule (section 6.4): explained checkpoints pause unless
`--pipeline-approve never` or `--no-explain-pause`, in which case cards are
printed and logged but the run continues. This keeps the two knobs
orthogonal and lets a scripted run still produce a legible transcript.

---

## 2. Goals and non-goals

Goals

1. At each key checkpoint, show a plain-English card that a non-engineer can
   judge: what will happen, why, which files (described by purpose), what is
   assumed, what will not change, how success will be checked, what to watch
   for.
2. Let the user intervene at the checkpoint without losing the session:
   continue, ask, correct, inspect, skip, stop.
3. Work the same way (same card, same menu, same flags) in normal edit modes,
   `--architect`, and pipeline mode.
4. Be safe by default: a discarded edit leaves the working tree exactly as it
   was; a failed explanation never stops the run.
5. Bounded cost: one extra call per checkpoint with hard token caps; no
   growth in the working model's context.
6. Additive code: new module + tiny hooks; no change to edit formats.

Non-goals

- Replacing `/diff`, `--show-diffs`, or any existing confirmation. Those stay.
- Explaining `ask`/`help`/`context` mode replies (they do not act on files).
- A GUI. Terminal only.
- Perfect explanations from small local models. The card format, the
  file-mention guard and the fallback card bound the damage of a bad one.
- Teaching the model to be right more often. This feature surfaces mistakes;
  the corrections it collects (section 7.6) are the input for that later.

---

## 3. Design principles

1. **Explain the artifact, not the intention.** Cards are built from the
   plan / brief / pending diff that will actually be acted on.
2. **For the human only.** Cards are never appended to the working model's
   messages. Only the user's corrections are.
3. **Never block on explanation failure.** If the explanation call errors,
   returns nothing usable, or exceeds twice its cap, render the mechanical
   fallback card and continue to the menu.
4. **Reversible by construction.** The `edits` checkpoint snapshots file
   contents before writing and restores them on decline, with or without git.
5. **Same words everywhere.** One card format, one menu, one set of flags
   across modes. Mode-specific behaviour is limited to *which* checkpoints
   exist and *what* artifact each explains.
6. **Assumptions are first-class.** Every card must have a "What I'm
   assuming" section (or the literal line "Nothing beyond what you asked"),
   because unstated assumptions are where the spot-able mistakes live.

---

## 4. User experience

### 4.1 Orientation banner (once per session, static text, no model call)

Shown the first time `--explain` is active in a session:

```
Explain mode is on. Before aider changes anything it will pause and tell you,
in plain English, what it is about to do and why. At each pause you can:
  c  continue          ?  ask a question        i  correct / add an instruction
  t  show the technical details (plan, brief or diff)
  s  skip this step    q  stop the run          x  stop explaining this session
Nothing is written to git until you continue.
```

### 4.2 The explanation card

Fixed order, fixed headings, plain English, bullets of one line each, no code
unless a name is unavoidable (then glossed: "`ApiClient` (the part that sends
web requests)"). Hard cap `explain_card_tokens` (default 300; 450 for the
pipeline `plan` checkpoint which lists several tasks).

```
== Before changing files (step 2 of 5): connect the rate limiter ==
What I'm going to do
  - Change the code that sends web requests so it waits for the limiter
    before each request.
Why
  - You asked that the app never sends more than 10 requests a second.
Which files change
  - net/client.py (the code that sends every web request)
What I'm assuming
  - Every request goes through one function, `request`. If some code sends
    requests another way, those will not be limited.
What will NOT change
  - How requests are built or how errors are reported.
How we'll know it worked
  - The file still loads, and the new tests check that the limiter is
    called once per request.
Watch out for
  - If you know of other places that send requests, tell me now.
Risk: low
```

Section rules for the model prompt (section 9): 1 to 3 bullets per section,
"What I'm assuming" mandatory, "Watch out for" may be omitted only if empty,
"Risk" is one of `low|medium|high` with a mandatory one-line reason for
medium/high (deleting code, touching many callers, adding a dependency,
changing data formats or public behaviour, running a command with side
effects).

### 4.3 The checkpoint menu

```
Continue? (c)ontinue / (?)ask / (i)nstruct / (t)echnical / (s)kip / (q)uit / (x) stop explaining [c]:
```

| Key | Action | Effect |
|---|---|---|
| `c` (default, Enter) | Continue | Proceed with the step as explained. |
| `?` | Ask | Prompt for a question; answer it with a bounded model call grounded in the same artifact (section 9.3); print the answer; return to the menu. Does not change state. |
| `i` | Instruct / correct | Prompt for free text. The instruction is recorded (section 7.6) and the step is **redone** with the instruction as a hard constraint, then re-explained. What "redone" means per mode is in section 5. |
| `t` | Technical | Print the underlying artifact (task list / brief / unified diff / architect reply) and return to the menu. |
| `s` | Skip | Skip this step only. Per mode: discard the pending edits and end the turn (normal); do not run the editor (architect); mark the task `skipped` (pipeline). |
| `q` | Quit | Stop the current run safely: discard pending edits (normal/architect); persist the ledger and stop scheduling (pipeline, resumable with `/pipeline resume`). The chat session stays open. |
| `x` | Stop explaining | Turn explain mode off for the rest of the session (same as `/explain off`), then continue this step. |

Implementation: a new `io.menu_ask(question, choices, default)` helper
(section 8.5) that accepts the first letter or the full word, is case
insensitive, honours `--yes` (returns the default), and logs the choice to the
chat history like `confirm_ask` does.

### 4.4 What the user sees per mode (examples)

Normal `diff` mode, `--explain`:

```
> make the CLI print a friendly error when the config file is missing

<model reply streams as today>

Applied edit to cli/main.py (pending your review)

== Before keeping these changes: friendly error for a missing config ==
What I'm going to do
  - When the config file cannot be found, print "Config file not found at
    <path>. Run `myapp init` to create one." and exit, instead of a crash.
...
What I'm assuming
  - The only place the config is opened is `load_config`. I did not change
    the two other places that read settings from the environment.
...
Continue? (c)ontinue / (?)ask / (i)nstruct / (t)echnical / (s)kip / (q)uit / (x) stop explaining [c]: i
Your instruction: also cover the case where the file exists but is empty
Reverted cli/main.py. Asking the model again with your instruction...
```

`--architect --explain`:

```
<architect reply streams as today>

== Before handing this plan to the editor: add rate limiting ==
...
Continue? [...]: c
<editor runs as today>
```

Pipeline mode is section 5.3.

---

## 5. Checkpoints per mode

Checkpoint ids are shared across modes. `--explain-at` takes a comma list;
unknown ids for the current mode are ignored with a warning.

| Id | Normal edit modes | `--architect` | Pipeline |
|---|---|---|---|
| `plan` | Opt-in. Extra call before editing (5.1). | Default. After the architect reply, before the editor. | Default. After PLAN validates, before SCHEDULE. Card lists the tasks in plain English. |
| `edits` | Default. After edits are written, before commit/lint/test/shell. | Opt-in. After the editor finishes, before the architect resumes (explains the diff). | Default. Before each EDIT, from the brief. |
| `review` | n/a | n/a | Default. After a REVIEW verdict of RETRY or REPLAN (ACCEPT is silent unless `review-all`). |
| `review-all` | n/a | n/a | Opt-in. Also explain ACCEPT verdicts: what the worker actually changed, from the diff, before COMMIT. Menu offers `c`, `?`, `t`, `q` only (an accepted diff can still be stopped before commit). |
| `triage` | n/a (the `edits` card labels auto-fix rounds) | n/a | Default. After TRIAGE, before the fix task is inserted. |
| `commands` | Opt-in. Before each suggested shell command. | Opt-in (editor's commands). | Opt-in (worker never suggests commands; n/a in practice). |
| `done` | n/a | n/a | Default. Summary card at DONE; no pause. |

Defaults: normal `edits`; architect `plan`; pipeline `plan,edits,review,triage,done`.

### 5.1 Normal edit modes (`diff`, `whole`, `udiff`, `patch`, fenced variants)

`edits` checkpoint (default):

1. `apply_updates()` runs as today up to and including `prepare_to_edit`.
   Before `apply_edits`, an `EditSnapshot` records, for each path in the
   edit list, its current content (or "absent" for a new file).
2. `apply_edits` writes the files. The "Applied edit to ..." lines are
   printed with a "(pending your review)" suffix while a checkpoint is due.
3. The pending diff is computed from the snapshot with `difflib.unified_diff`
   (no git required; works in non-repo sessions). Cap
   `explain_diff_tokens` (default 2500); oversize diffs are truncated per
   file with a "... N more lines" marker, and the model is told the diff is
   partial.
4. The card is produced (section 7) from: the user's message, the model's
   reply prose (edit blocks stripped), the diff, and whether this turn is an
   automatic lint/test fix round (`num_reflections > 0` and the reflected
   message came from lint/test).
5. Menu:
   - continue: proceed to `auto_commit`, lint, shell commands, tests as today.
   - instruct: restore the snapshot (delete files that were absent), then
     queue the correction as the **next user message** in `run_one`:
     "You proposed changes that I have discarded. Feedback: <text>. Please
     try again taking this into account." This goes through the normal
     message path (so the model sees the current file contents, which are
     the originals again) and does **not** count against `max_reflections`.
   - skip: restore the snapshot, end the turn normally with a
     `move_back_cur_messages("The user discarded those changes.")` so the
     model's history stays truthful.
   - quit: same as skip.
6. If the snapshot restore fails for a file (permissions, concurrent edit)
   report it loudly and fall back to `git checkout -- <file>` when in a repo.

`plan` checkpoint (opt-in via `--explain-at plan,edits`): before the edit
call, send the same formatted messages with an appended user instruction
"Do not make any edits yet. In plain English for a non-programmer, using the
card format below, explain what you would do." (section 9.2, `plan` addendum),
show the card, run the menu. On continue, the card's *technical* twin is
not needed: the real edit call is made with the original messages plus a
one-line assistant/user pair ("Plan acknowledged by the user. Proceed."), so
the model does not re-plan. On instruct, append the instruction to the user
message and redo the plan call. Cost: one full-context extra call per turn;
documented as such.

`commands` checkpoint (opt-in): before `handle_shell_commands` confirms a
command, produce a two-line card ("This command will ... because ...") from
the command text plus the reply prose. The existing confirm remains.

### 5.2 `--architect` mode

`plan` checkpoint (default): in `ArchitectCoder.reply_completed`, after trace
handling and before the "Edit the files?" logic, build the card from the
user's message and the architect's reply. Menu:

- continue: run the editor as today (auto-accept setting is irrelevant when
  a checkpoint was shown and accepted; the checkpoint *is* the acceptance).
- instruct: do not run the editor. Reflect to the architect: "The user
  reviewed your plan and asked: <text>. Revise the plan." The architect's
  revised reply is explained again. Not counted against `max_reflections`.
- skip/quit: do not run the editor; `move_back_cur_messages("The user
  declined those changes.")`.

`edits` checkpoint (opt-in): the editor coder is created with
`explain_config=None` (so it does not double-explain), and after `editor_coder.run`
returns, the architect coder builds a card from the editor's aggregate diff
(via the editor's `aider_commit_hashes` when auto-commits are on, or the
`EditSnapshot` mechanism otherwise) with menu actions: continue, instruct
(sends the correction to the architect as a new turn), quit. With
auto-commits on, "instruct" cannot un-write the commit; the card says so and
offers `/undo` guidance. Because of this, when `edits` is enabled in
architect mode the editor is created with `auto_commits=False` and the
architect commits after the checkpoint (mirroring how pipeline mode owns
commits).

### 5.3 Pipeline mode

Insertions into the state machine of the pipeline spec (section 5.3 there):

```
PLAN --> EXPLAIN(plan) --> CHECKPOINT --> SCHEDULE
BRIEF(task) --> EXPLAIN(edits, from the brief) --> CHECKPOINT --> EDIT(task)
REVIEW(task) -- verdict RETRY|REPLAN --> EXPLAIN(review) --> CHECKPOINT --> (retry | replan | user override)
TRIAGE --> EXPLAIN(triage) --> CHECKPOINT --> insert fix task
DONE --> EXPLAIN(done)  (card only, no pause)
```

Card inputs per checkpoint: the ledger view (request, clarifications, plan
summary, compact task list) plus the step artifact: the validated task list
(plan); the brief (edits); brief + diff + verdict + rationale (review); the
trimmed test output + verdict + proposed fix task (triage); the final task
table with commits (done). No repo map, no working memory: the card must be
explainable from the artifact, and this keeps the call around 3 to 5k tokens.

Menu semantics:

- `plan`: instruct appends to `clarifications` and re-runs PLAN (counts
  against a new `max_user_replans`, default 5, to bound cost; at the cap the
  user is told to edit the ledger directly with `/pipeline edit`). skip is
  not offered. quit persists and stops.
- `edits` (before EDIT): instruct stores the text in `task.user_notes`
  (new ledger field, list) and re-runs BRIEF with the note rendered as a
  "User constraints (mandatory)" section; the architect may respond with
  REPLAN, which follows the existing path and is then explained as `plan`.
  skip marks the task `skipped` (dependants are re-checked by SCHEDULE as
  in the pipeline spec).
- `review`: continue accepts the architect's verdict (retry or replan).
  Additional key `a` "accept anyway" is offered here only: overrides RETRY
  with ACCEPT and records `user_override: accept` on the task. instruct adds
  to `task.user_notes` and re-runs REVIEW's retry brief.
- `triage`: continue inserts the fix task; instruct adds to the fix task's
  `user_notes` before it is briefed; skip records `ACCEPT_KNOWN` with a
  user note.
- `done`: card only.

Explanation cards are saved to `.aider.pipeline/explanations/<step>-<task or
plan>-<attempt>.md` and the checkpoint outcome is appended to the ledger
`history` as `{step: CHECKPOINT, checkpoint: edits, task: T2, action:
instruct, text: "..."}`.

Model: `explain_model` defaults to the architect model in pipeline mode. It
may be pointed at the worker to overlap with architect work, but the default
favours explanation quality.

Interplay with `--pipeline-approve` is in 6.4.

### 5.4 Automatic lint/test fix rounds (all modes)

When the turn being explained is a reflection caused by lint or test
failures, the card title becomes "Automatic fix attempt N of M for <lint|test>
errors" and the "Why" section must quote the first error in plain terms. In
pipeline mode this is the `triage` checkpoint. The user's most common
complaint ("it went off and changed something else while fixing a test") is
addressed by making this round explicit and interruptible.

---

## 6. Configuration

### 6.1 Flags (in `aider/args.py`, new "Explain" group)

| Flag | Default | Meaning |
|---|---|---|
| `--explain` / `--no-explain` | off | Master toggle. |
| `--explain-at IDS` | per mode (section 5) | Comma list of checkpoint ids. |
| `--explain-model MODEL` | weak model; architect model in pipeline | Model used for cards and answers. |
| `--explain-pause` / `--no-explain-pause` | on | With `--no-explain-pause`, cards are printed and logged but no menu is shown (narration only). |
| `--explain-min-risk LEVEL` (low, medium, high) | `low` | Only pause when the card's `Risk:` is at or above this level; lower-risk cards are printed without a pause. Phase D. |
| `--explain-card-tokens N` | 300 | Card output cap (`plan_card_tokens`, 450, applies to the pipeline `plan` card and is derived as 1.5x unless set in yaml). |
| `--explain-diff-tokens N` | 2500 | Pending-diff input cap. |
| `--explain-audience KIND` (plain, developer) | `plain` | `developer` relaxes the no-jargon rule and allows identifiers without glosses; same card structure. |

All are settable in `.aider.conf.yml` and `.env` following the existing
conventions (`explain: true`, `AIDER_EXPLAIN=true`), and must be added to the
generated docs/sample config files like every other flag.

### 6.2 Commands (in `aider/commands.py`)

- `/explain` with no args: print current state (on/off, checkpoints, model).
- `/explain on|off`: toggle for the session.
- `/explain at <ids>`: change checkpoints for the session.
- `/explain audience plain|developer`.
- `/why [n]`: explain, in the card format's "What changed / Why / What I
  assumed" subset, the last aider commit (or the n-th most recent aider
  commit of this session) from its diff and the chat messages that led to it.
  Works with explain mode off; it is a one-shot card. Useful after the fact
  when a result looks wrong.

### 6.3 Interplay with `--yes`

`--yes` makes `menu_ask` return the default (`c`). So `--explain --yes`
degrades to narration only, which is the correct behaviour for scripted runs.
The orientation banner says so when both are set.

### 6.4 Interplay with `--auto-accept-architect` and `--pipeline-approve`

- Architect: when `--explain` includes `plan`, the checkpoint replaces the
  "Edit the files?" prompt regardless of `--auto-accept-architect`. With
  `--no-explain-pause` the original auto-accept behaviour applies after the
  card is printed.
- Pipeline: pause points = `approve`-mode pauses **union** explained
  checkpoints, except with `--pipeline-approve never` (no pauses; cards still
  printed and saved) or `--no-explain-pause`. Where both would pause at the
  same point (e.g. `approve task` + `edits`), a single menu is shown with the
  card; the raw brief is available via `t`.

---

## 7. Design

### 7.1 Module layout

```
aider/explain.py
  class ExplainConfig        # dataclass of the section 6.1 values + per-mode defaults
  class Artifact             # kind (plan|brief|diff|review|triage|done|command|architect_reply),
                             # text, files (list of rel paths), meta (attempt, reflection cause, ...)
  class Card                 # parsed card: title, sections (ordered dict), risk, raw_text, fallback (bool)
  class Explainer            # build_messages(artifact, context) -> messages; explain(...) -> Card;
                             # answer(question, artifact, card) -> str; guards; persistence hooks;
                             # checkpoint(artifact, context, actions) -> CheckpointResult
                             #   (explain + menu loop, handling ? and t internally)
  class Checkpoint           # run(card, artifact, actions) -> CheckpointResult(action, text)
  class EditSnapshot         # capture(paths) / diff() / restore()
  def render_fallback_card(artifact) -> Card
aider/coders/explain_prompts.py
  system prompt, per-checkpoint addenda, question prompt, /why prompt
tests/basic/test_explain.py
tests/basic/test_explain_coder.py      # hook points in base_coder and ArchitectCoder with mocked models
tests/basic/test_explain_pipeline.py   # pipeline insertions (lands with the pipeline implementation)
aider/website/docs/usage/explain.md    # user docs
```

`Explainer` depends only on a `Model`, an `InputOutput`, and an
`ExplainConfig`; coders hold one instance (`self.explainer`, `None` when off)
so tests can inject a stub.

### 7.2 Explanation call

`Explainer.explain(artifact, context)`:

- `context` is a small dict: `request` (the user's most recent message or the
  pipeline request), `reply_prose` (model prose with edit blocks / fences
  removed, capped 800 tokens), `mode`, `step_label` ("step 2 of 5"),
  `reflection_cause` (`None|lint|test`), `attempt`.
- Messages: system prompt (9.1) + checkpoint addendum (9.2) + one user
  message containing the context fields and the artifact text, each in a
  labelled fenced block. Total input is capped at `explain_input_tokens`
  (default 6000); the artifact is truncated first, `reply_prose` second.
- Call via `Model.simple_send_with_retries(messages)` (non-streaming, no chat
  state). Strip reasoning tags the same way `Coder.remove_reasoning_content`
  does; factor that helper so it is shared (the pipeline spec asks for the
  same).
- Parse the card by headings (9.1). Missing mandatory sections ("What I'm
  going to do", "Which files change", "What I'm assuming") trigger one retry
  with the parser's complaint appended; a second failure yields the fallback
  card.

### 7.3 Fallback card (mechanical, no model)

Rendered from the artifact alone so the run never blocks:

- `diff`: title from the user's request (first line, 80 chars); "Which files
  change" from the diff headers with `+N / -M lines`; "What I'm going to do"
  = the reply prose's first two sentences or "(no description available)";
  assumptions = "Not available: the explanation model failed. Use (t) to see
  the changes."; risk = `medium` if any file is deleted or more than
  `fallback_big_change_lines` (200) change, else `unknown`.
- `brief`/`plan`: title, goal and acceptance criteria copied from the brief;
  task titles and files from the plan.
- Always prefixed with a warning line so the user knows it is not the
  model's explanation.

### 7.4 Guards

- **File-mention guard**: every path in `artifact.files` must appear in the
  card (by basename or path). Missing ones are appended by the orchestrator
  under "Which files change" as "(also) path" so the card can never hide a
  touched file.
- **Length guard**: output over `2 x explain_card_tokens` is truncated at the
  last complete section and flagged.
- **No-code guard** (`plain` audience only): fenced code blocks in the card
  are removed and replaced with "(code omitted; press t to see it)".
- **Risk guard**: if `Risk:` is missing or unparseable, set to `unknown` and
  treat as `medium` for `--explain-min-risk`.

### 7.5 Context hygiene

Cards, answers and menu output go to the terminal and to the chat-history
markdown (`io.append_chat_history`, blockquoted like tool output) so the user
can scroll back. They are **never** appended to `cur_messages`/`done_messages`
or to any pipeline architect prompt. The only text that flows back to a
working model is a user instruction from `i`, formatted as a user message
(normal/architect) or a ledger field (pipeline).

### 7.6 Recording interventions

Every checkpoint outcome is recorded:

- Chat history markdown: the card, the menu line and the chosen action, and
  any instruction text (as today's confirm prompts are recorded).
- Pipeline: ledger `history` entries and the saved card files (5.3).
- If the local limitation log from the "file reasons / test discovery" work
  (`aider/limitation_log.py`, `LimitationLog.record`) is present, record
  `explain.intervention` with `{checkpoint, action, files, risk,
  reflection_cause, excerpt(instruction)}` and `explain.fallback` when the
  mechanical card was used. Import it guarded (`try/except ImportError`) so
  this spec does not depend on that branch landing first. These events are
  the dataset for "where does the model go wrong in ways a person catches".

### 7.7 Cost and latency

Per checkpoint: one call of about 3 to 6k input tokens and up to 300 output
tokens, plus one more call per `?` question. With defaults, a normal-mode
turn adds one call; an architect turn adds one; a 10-task pipeline run adds
about 12 (plan, ten task cards, done) plus any review/triage cards. On a
local 7B to 30B model that is roughly 10 to 40 seconds per card; on a hosted
weak model it is a few seconds and negligible cost. `/tokens` and the pipeline
telemetry count explanation calls separately (`step: EXPLAIN`).

---

## 8. Code changes

### 8.1 `aider/coders/base_coder.py` (small, additive)

- `__init__`: accept `explain_config=None`; build `self.explainer` when
  enabled (model resolution in `main.py`, 8.4).
- `apply_updates()`: split the tail so that a snapshot is taken before
  `apply_edits` and the checkpoint runs after:

  ```python
  edits = self.prepare_to_edit(edits)
  edited = set(edit[0] for edit in edits)
  snapshot = self.explainer.snapshot(edited) if self.explain_due("edits") else None
  self.apply_edits(edits)
  if snapshot:
      self.pending_edit_checkpoint = (snapshot, edited)
  ```

  and in `send_message`, immediately after `edited = self.apply_updates()`
  and before `auto_commit`, call `self.run_edit_checkpoint()` which builds
  the artifact from `snapshot.diff()`, shows the card and menu, and either
  returns normally, or restores and sets `self.user_correction` / ends the
  turn per 5.1.
- `run_one()`: after the reflection check, `if self.user_correction:
  message = self.user_correction; self.user_correction = None; continue`
  without touching `num_reflections`.
- `run_shell_commands()` / `handle_shell_commands()`: if `commands` is an
  active checkpoint, print the two-line card before the existing confirm.
- `explain_due(checkpoint_id)`: `self.explainer and checkpoint_id in
  self.explainer.config.checkpoints`.

No edit-format coder changes. The snapshot approach is format-agnostic.

### 8.2 `aider/coders/architect_coder.py`

In `reply_completed`, between trace handling and the auto-accept check:

```python
if self.explain_due("plan"):
    result = self.explainer.checkpoint(Artifact.architect_reply(content, files=self.get_inchat_relative_files()), context=...)
    if result.action == "instruct":
        self.reflected_message = self.gpt_prompts.user_revision.format(text=result.text)  # not counted
        self.user_correction_pending = True
        return True
    if result.action in ("skip", "quit"):
        self.move_back_cur_messages("The user declined those changes.")
        return
    # continue falls through; skip the "Edit the files?" prompt
elif not self.auto_accept_architect and not self.io.confirm_ask("Edit the files?"):
    return
```

Create the editor with `explain_config=None`. For the opt-in `edits`
checkpoint, create it with `auto_commits=False`, snapshot its file set before
`editor_coder.run`, and run the checkpoint on the resulting diff, committing
on continue with `self.repo.commit(fnames=..., aider_edits=True, coder=self)`.

### 8.3 Pipeline mode (`aider/coders/pipeline_coder.py`, when it exists)

- `PipelineConfig` gains `explain_*` fields mirroring `ExplainConfig`, or
  simply holds an `ExplainConfig`.
- The state machine inserts EXPLAIN + CHECKPOINT as in 5.3; each is a
  method on `PipelineCoder` that calls `self.explainer` with the ledger view
  and the artifact.
- `Ledger` task schema gains `user_notes: [str]` and `user_override`.
- `history` gains the `CHECKPOINT` and `EXPLAIN` step kinds.

### 8.4 `aider/args.py`, `aider/main.py`, `aider/commands.py`

- Flags per 6.1 in a new group; `--explain-at` parsed to a list and
  validated against the known ids.
- `main.py`: resolve `explain_model` (weak model by default; the architect
  model when `--pipeline`) with `sanity_check_model`; build `ExplainConfig`
  and pass `explain_config=` to `Coder.create`.
- `commands.py`: `cmd_explain` (6.2) and `cmd_why`. `/why` reuses
  `repo.diff_commits` to get the diff of the chosen aider commit and the
  matching slice of `done_messages` for the request text.

### 8.5 `aider/io.py`

Add `menu_ask(question, choices, default)`:

- `choices` is an ordered list of `(key, word, label)`; keys are single
  characters (`?` allowed).
- Renders `question + " (c)ontinue / (?)ask / ..."`, accepts key or word,
  case insensitive, Enter = default; `--yes` returns the default; EOF returns
  default; logs `question + answer` to chat history like `confirm_ask`.
- Uses the existing `prompt_session` when present so completion and styling
  match the rest of aider. Decorated with `@restore_multiline` like its
  siblings.

### 8.6 Docs and samples

`aider/website/docs/usage/explain.md`, plus the generated flag docs
(`options.md`, `aider_conf.md`, `dotenv.md`, sample yml/env) regenerated
with the existing scripts. A short "If you are not a programmer" section
in `tips.md` pointing at `--explain`.

---

## 9. Prompt specifications

### 9.1 System prompt (all checkpoints)

```
You explain a programmer's planned change to someone who does not write code.
You are given the request, sometimes the model's own notes, and the exact
thing that is about to happen (a plan, a set of instructions, or the actual
changes to the files). Describe what WILL happen based on that material, not
on what the notes say was intended. If the notes and the changes disagree,
say so under "Watch out for".

Write in plain English. No code, no jargon; if you must name a file or
function, add a short gloss in brackets. One idea per bullet, 1 to 3 bullets
per section, whole answer under {card_tokens} tokens.

Use exactly these headings in this order:
== <title, one line> ==
What I'm going to do
Why
Which files change
What I'm assuming
What will NOT change
How we'll know it worked
Watch out for
Risk: low | medium | high - <one line reason if medium or high>

"What I'm assuming" is mandatory: list anything the change depends on that
was not stated in the request (which code paths are covered, that a name is
unique, that a library is available). If truly nothing, write "Nothing beyond
what you asked". Mention every file listed in FILES.
```

`--explain-audience developer` swaps the second paragraph for "Be concise and
precise; identifiers are fine; still no code blocks."

### 9.2 Checkpoint addenda

- `plan` (normal): "Nothing has been changed yet. Describe what you would do
  if you went ahead."
- `plan` (architect): "The material is a plan written for another model to
  execute. Describe the plan's effect, not its wording."
- `plan` (pipeline): "The material is a numbered list of tasks. Under 'What
  I'm going to do', give one bullet per task in order, each starting with
  the task number. Title the card with the overall goal. You may use up to
  {plan_card_tokens} tokens."
- `edits` (diff): "The material is the exact change to the files, shown as
  lines removed (-) and added (+). Explain the effect of those lines."
- `edits` (pipeline brief): "The material is the instruction the smaller
  model will follow for one file. Explain what that file will do afterwards
  that it does not do now."
- `review`: "The reviewing model decided the change was not right and wants
  to {retry|replan}. Explain what was wrong in plain terms and what it will
  try instead."
- `triage`: "Tests failed after a change. Explain which behaviour the test
  expected, what actually happened, and what the proposed fix will change."
- `done`: "Summarise what was done, listing each step and whether it was
  accepted or skipped. No risk line."
- Reflection cause present: "This is automatic fix attempt {n} of {m} for
  {lint|test} errors. Say so in the title and quote the first error in plain
  terms under Why."

### 9.3 Question prompt (`?`)

System: the 9.1 prompt's first paragraph plus "Answer the user's question
about this change in plain English, in at most 6 sentences. If the material
does not contain the answer, say what you would need to look at." User
message: request, the card, the artifact, the question. No state change.

### 9.4 `/why` prompt

As 9.1 with only the sections "What changed", "Why", "What I assumed",
"Watch out for"; material = the commit diff and the user/assistant messages
that produced it.

---

## 10. Implementation plan

Each phase is independently shippable.

### Phase A: core and normal edit modes

Scope: `aider/explain.py` (`ExplainConfig`, `Artifact`, `Card`, `Explainer`,
`Checkpoint`, `EditSnapshot`, fallback card, guards), `explain_prompts.py`,
`io.menu_ask`, flags, `main.py` wiring, `/explain`, the `edits` checkpoint in
`base_coder`, orientation banner, chat-history logging, user docs.

Acceptance:
- With `--explain` and a mocked explanation model, a `diff`-format turn
  writes the edit, shows a card that names every edited file, and pauses;
  `c` commits as before; `s` restores the files byte-for-byte (including
  deleting a newly created file) and the model history contains "The user
  discarded those changes."; `i` restores, and the next model call's last
  user message contains the correction and the original file content;
  `num_reflections` is unchanged by `i`.
- Explanation model raising an exception produces the fallback card and the
  menu; the run continues.
- Cards never appear in `cur_messages`/`done_messages` (assert on the
  messages passed to the mocked working model).
- `--yes` and `--no-explain-pause` print the card and do not prompt.
- Lint-fix reflection turns produce a card titled "Automatic fix attempt".
- `pytest tests/basic/test_explain*.py` green; `flake8` clean.

### Phase B: architect mode, `/why`, `commands`

Scope: `ArchitectCoder` `plan` checkpoint replacing "Edit the files?", the
opt-in `edits` checkpoint with architect-owned commit, `/why`, the
`commands` checkpoint.

Acceptance:
- `--architect --explain`: the editor does not run until `c`; `i` reflects
  the correction to the architect and a second card is shown; `s` leaves the
  files untouched and history truthful.
- `--explain-at plan,edits` in architect mode: editor created with
  `auto_commits=False`; one commit is made after `c`; no commit after `s`.
- `/why` on a session with two aider commits explains the most recent by
  default and `/why 2` the earlier one.

### Phase C: pipeline integration

Lands with, or immediately after, pipeline Phase 1 (`PipelineCoder`).

Scope: 5.3 insertions, `user_notes`/`user_override` ledger fields, saved
cards, `CHECKPOINT`/`EXPLAIN` history entries, approve-mode union rule,
`max_user_replans`.

Acceptance:
- On the pipeline fixture with mocked models: a `plan` card precedes
  SCHEDULE; `i` at plan re-runs PLAN with the text in `clarifications`; an
  `edits` card precedes each EDIT and `i` re-runs BRIEF with the note under
  "User constraints"; `s` marks the task skipped; REVIEW RETRY produces a
  `review` card and `a` overrides to ACCEPT with `user_override` recorded;
  `--pipeline-approve never` produces card files but no prompts.
- Architect prompts for every pipeline step are unchanged in token count by
  enabling `--explain` (cards never enter the architect's context).

### Phase D: refinements

Scope: `--explain-min-risk`, `--explain-audience developer`, the opt-in
normal-mode `plan` checkpoint, `LimitationLog` events, telemetry line in
`/tokens`, prompt tuning against the local models used in pipeline Phase 0
(Qwen3-Coder-30B-A3B, gpt-oss-20b, Qwen2.5-Coder-7B) with a small fixture
set of diffs and a checklist (names all files, has assumptions, no code, under
cap) scored automatically.

Acceptance:
- Cards with `Risk: low` do not pause under `--explain-min-risk medium`;
  `unknown` risk pauses.
- Normal-mode `plan` checkpoint adds exactly one model call and the edit
  call's messages contain the acknowledgement pair.
- Local-model prompt fixtures pass the checklist at least 90% of the time
  for the chosen architect model (recorded in the docs page).

### Testing conventions

Follow `tests/basic/test_coder.py`: `GitTemporaryDirectory`,
`InputOutput(yes=True)` for non-interactive paths and a scripted
`InputOutput` stub whose `menu_ask` returns a queue of answers for the
interactive paths; `MagicMock` on `Model.simple_send_with_retries` for the
explanation model and on `send_completion` for the working model.

---

## 11. Risks and open questions

1. **Explanation quality from small local models.** A 7B may produce vague
   or wrong cards. Mitigations: derive from the artifact, mandatory
   assumptions section, file-mention guard, `t` for the raw artifact, and
   the Phase D checklist to pick the explanation model. If a user's models
   cannot produce usable cards, `--explain-model` accepts any aider model,
   including a hosted one, independent of the working model.
2. **Pause fatigue.** Every edit turn pausing may annoy users who asked for
   it and then find it slow. `x` (stop explaining), `--explain-min-risk`, and
   `--no-explain-pause` are the escape hatches; the default checkpoint sets
   are deliberately minimal (one pause per turn in normal/architect modes).
3. **Snapshot restore vs. concurrent tools.** If a formatter or IDE rewrites
   a file between snapshot and restore, restore overwrites it. Restore is
   guarded by comparing the current content with what `apply_edits` wrote;
   if it differs, warn and ask before overwriting.
4. **Architect `edits` checkpoint and commits.** Turning off the editor's
   auto-commit changes when the commit happens; the attribution and message
   generation must be identical to today's (`repo.commit(..., aider_edits=True,
   coder=self)`). Covered by Phase B acceptance.
5. **Interaction with `--dry-run`.** With `--dry-run`, edits are not written;
   the `edits` checkpoint has no diff to explain. Compute the diff from
   `apply_edits_dry_run` where the format supports it (editblock) and
   otherwise skip the checkpoint with a notice.
6. **Directive drift in the card format.** Headings may be paraphrased by
   the model. Parse headings case-insensitively and by prefix ("What I'm
   assuming" / "Assumptions"), and retry once; then fall back. Count parse
   failures in telemetry.
7. **Two sources of "why".** In pipeline mode the architect writes `NOTE:`
   lines and REVIEW rationale (technical, for the ledger) and the explainer
   writes cards (plain, for the human). They are intentionally separate; do
   not try to derive one from the other.
8. **Ordering relative to other in-flight work.** This spec touches
   `base_coder.send_message`/`apply_updates` near code changed by the
   tracing and limitation-log work. Keep the hooks to the exact insertion
   points named in 8.1 and land after those branches are merged.

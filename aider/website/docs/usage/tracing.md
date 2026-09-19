---
parent: Usage
nav_order: 70
description: Find where a symbol is defined, called, read and written, without adding whole files.
---

# Tracing code

The [repo map](../repomap.html) shows *definitions*. It does not show how
they connect, so a model often cannot tell where something actually happens
and guesses which files to ask for.

**Tracing** fills that gap. It searches the same tree-sitter data the repo
map is built from and reports where a symbol is defined, called, read and
written — as short snippets, not whole files.

A copy of this guide also lives in the repo at
[`docs/using-tracing.md`](https://github.com/williamgeorgedunn-png/bonai/blob/main/docs/using-tracing.md).

## What you get

```
Trace of `handle_request` (function/class, defined at app/service.py:8):

Definition:
app/service.py:
...⋮...
  8│def handle_request(raw_value):
...⋮...

Callers of `handle_request` (1 found):
app/api.py:
...⋮...
  4│def post(raw_value):
  5│    return handle_request(raw_value)

`handle_request` uses these, defined elsewhere in the repo:
- save_record -> app/storage.py:4
- normalize -> app/service.py:4

Ask me to *add* only the files you actually need to see or edit.
```

For variables and attributes it also shows how the value flows: where it is
set, where it is read, and one hop through calls and returns.

Results are ranked the same way the repo map is ranked, and trimmed to fit
the token budget. A symbol with hundreds of call sites returns the most
relevant ones plus a list of the other files to ask about.

## It is on by default

You do not have to do anything. Aider traces:

- symbols **you** mention in a message
- symbols **the LLM** mentions

Those results are attached to the request being sent, then thrown away, so
they never accumulate in the chat history.

The LLM can also ask for a trace itself, by replying with a fenced block:

````
```trace
handle_request up
save_record down
```
````

Aider answers with the results and lets it try again, the same way it
handles a request to add files.

When aider then offers to `/add` a file, it says **why** —
`api.py` / `calls handle_request from post` — so you can decide from the
trace, not just the filename.

## Run a trace yourself

```
/trace handle_request
```

Aider prints the result and asks whether to add it to the chat. If you say
yes, it still expires after the next message.

Optional direction words:

| Command | What it shows |
|---|---|
| `/trace handle_request` | definition, callers, and what it uses |
| `/trace handle_request up` | callers only |
| `/trace handle_request down` | what it uses only |
| `/trace handle_request tests` | tests that exercise it |
| `/tests handle_request` | same as `tests`, and offers to add those tests as snippets |

You can pass more than one name: `/trace foo, bar up`.

## When a name is too common

`count` is used everywhere, so ask for one place:

```
/trace count in app/service.py
/trace handle_request.count
```

The first searches only that file. The second searches only inside that
function or class. `app/service.py:count` works too.

## Keep the context small

Tracing is meant to *avoid* dumping files into the chat. Two related
commands help with that:

```
/focus handle_request
/snip handle_request
```

- `/focus` keeps the repo map centered on a symbol across turns.
  `/unfocus` clears it.
- `/snip` adds just that function or class as a read-only snippet, instead
  of its whole file. `/add` of the whole file supersedes its snippets;
  `/drop` of the file removes them. `/unsnip` removes snippets by name.

`/tests handle_request` finds the tests that call it and offers those test
functions as snippets, so you can see expected behaviour without loading
whole test files.

## Flags

Tracing needs a repo map, so it is off whenever the map is
(`--map-tokens 0`).

| Flag | Effect |
|---|---|
| `--trace` / `--no-trace` | enable or disable tracing (default: on) |
| `--auto-trace` / `--no-auto-trace` | trace names mentioned in chat (default: on). `--no-auto-trace` still allows `/trace` and LLM `trace` blocks |
| `--trace-tokens N` | budget for one trace result (default: follows `--map-tokens`, at most 1024) |

## What it cannot see

Tracing is deterministic: it uses the names in your code, not a language
server or a type checker. That makes it cheap and predictable, but:

- dynamically dispatched calls can be missed
- a very common name may come back asking you to be more specific
- generated code and files the repo map ignores are invisible

If `/trace` says tracing is disabled, raise `--map-tokens` (or drop
`--no-git` / `--map-tokens 0`).

See the [repository map](../repomap.html) page for how the map itself is
built, and how tracing fits into that.

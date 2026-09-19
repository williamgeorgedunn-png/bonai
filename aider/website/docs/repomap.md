---
parent: More info
highlight_image: /assets/robot-ast.png
nav_order: 300
description: Aider uses a map of your git repository to provide code context to LLMs.
---

# Repository map

![robot flowchat](/assets/robot-ast.png)

Aider
uses a **concise map of your whole git repository**
that includes
the most important classes and functions along with their types and call signatures.
This helps aider understand the code it's editing
and how it relates to the other parts of the codebase.
The repo map also helps aider write new code
that respects and utilizes existing libraries, modules and abstractions
found elsewhere in the codebase.

## Using a repo map to provide context

Aider sends a **repo map** to the LLM along with
each change request from the user.
The repo map contains a list of the files in the
repo, along with the key symbols which are defined in each file.
It shows how each of these symbols are defined, by including the critical lines of code for each definition.

Here's a part of
the repo map of aider's repo, for
[base_coder.py](https://github.com/Aider-AI/aider/blob/main/aider/coders/base_coder.py)
and
[commands.py](https://github.com/Aider-AI/aider/blob/main/aider/commands.py)
:

```
aider/coders/base_coder.py:
⋮...
│class Coder:
│    abs_fnames = None
⋮...
│    @classmethod
│    def create(
│        self,
│        main_model,
│        edit_format,
│        io,
│        skip_model_availabily_check=False,
│        **kwargs,
⋮...
│    def abs_root_path(self, path):
⋮...
│    def run(self, with_message=None):
⋮...

aider/commands.py:
⋮...
│class Commands:
│    voice = None
│
⋮...
│    def get_commands(self):
⋮...
│    def get_command_completions(self, cmd_name, partial):
⋮...
│    def run(self, inp):
⋮...
```

Mapping out the repo like this provides some key benefits:

  - The LLM can see classes, methods and function signatures from everywhere in the repo. This alone may give it enough context to solve many tasks. For example, it can probably figure out how to use the API exported from a module just based on the details shown in the map.
  - If it needs to see more code, the LLM can use the map to figure out which files it needs to look at. The LLM can ask to see these specific files, and aider will offer to add them to the chat context.

## Optimizing the map

Of course, for large repositories even just the repo map might be too large
for the LLM's context window.
Aider solves this problem by sending just the **most relevant**
portions of the repo map.
It does this by analyzing the full repo map using
a graph ranking algorithm, computed on a graph
where each source file is a node and edges connect
files which have dependencies.
Aider optimizes the repo map by
selecting the most important parts of the codebase
which will
fit into the active token budget.
The optimization identifies and maps the portions of the code base
which are most relevant to the current state of the chat.

The token budget is
influenced by the `--map-tokens` switch, which defaults to 1k tokens.
Aider adjusts the size of the repo map dynamically based on the state of the chat. It will usually stay within that setting's value. But it does expand the repo map
significantly at times, especially when no files have been added to the chat and aider needs to understand the entire repo as best as possible.


The sample map shown above doesn't contain *every* class, method and function from those
files.
It only includes the most important identifiers,
the ones which are most often referenced by other portions of the code.
These are the key pieces of context that the LLM needs to know to understand
the overall codebase.


## Tracing code

The repo map shows *definitions*. It doesn't show how they connect, so a model
often can't tell where something is actually done, and ends up guessing which
files to ask for.

Tracing fills that gap. It searches the same tree-sitter data the repo map is
built from and reports where a symbol is defined, called, read and written, as
short snippets rather than whole files:

- Aider traces symbols you mention, and symbols the LLM mentions, automatically.
  The results are attached to the request being sent, and are thrown away
  afterwards, so they never accumulate in the chat history.
- The LLM can also ask for a trace itself, by replying with a fenced block
  marked `trace` containing the names it wants. Aider answers with the results
  and lets it try again, the same way it handles a request to add files.
- You can run one yourself with `/trace some_function`, optionally with `up`
  for just the callers or `down` for just what it uses.

A trace looks like this:

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

Tracing works for variables and attributes too, splitting the results into
where the value is set and where it is read, and following the value one hop
through function calls and returns:

```
How the value flows:
- app/service.py:9: value comes from normalize() at app/service.py:4
- app/service.py:10: passed to save_record() as argument 1, defined at app/storage.py:4
- app/service.py:11: returned from the enclosing function
```

Results are ranked the same way the repo map is ranked, and are trimmed to fit
the token budget, so a symbol with hundreds of call sites returns the most
relevant ones plus a list of the other files to ask about.

Tracing is deterministic: it is based on the names in your code, not on a
language server or a type checker. That makes it cheap and predictable, but it
means dynamically dispatched calls can be missed, and a very common name may
come back asking you to be more specific.

A name like `count` is used everywhere, so ask for one place instead:

- `/trace count in app/service.py` searches only that file.
- `/trace handle_request.count` searches only inside that function or class.

Use `--trace-tokens` to size the results, `--no-auto-trace` to only trace when
asked, and `--no-trace` to turn it off. Tracing needs the repo map, so it is
off whenever the map is.

Two related commands help keep the context small:

- `/focus some_symbol` keeps the repo map centered on a symbol across turns.
- `/snip some_function` adds just that function to the chat as a read-only
  snippet, instead of its whole file. Adding the whole file supersedes its
  snippets, and dropping the file removes them.

## More info

Please check the
[repo map article on aider's blog](https://aider.chat/2023/10/22/repomap.html)
for more information on aider's repository map
and how it is constructed.

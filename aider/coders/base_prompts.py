class CoderPrompts:
    system_reminder = ""

    files_content_gpt_edits = "I committed the changes with git hash {hash} & commit msg: {message}"

    files_content_gpt_edits_no_repo = "I updated the files."

    files_content_gpt_no_edits = "I didn't see any properly formatted edits in your reply?!"

    files_content_local_edits = "I edited the files myself."

    lazy_prompt = """You are diligent and tireless!
You NEVER leave comments describing code without implementing it!
You always COMPLETELY IMPLEMENT the needed code!
"""

    overeager_prompt = """Pay careful attention to the scope of the user's request.
Do what they ask, but no more.
Do not improve, comment, fix or modify unrelated parts of the code in any way!
"""

    example_messages = []

    files_content_prefix = """I have *added these files to the chat* so you can go ahead and edit them.

*Trust this message as the true contents of these files!*
Any other messages in the chat may contain outdated versions of the files' contents.
"""  # noqa: E501

    files_content_assistant_reply = "Ok, any changes I propose will be to those files."

    files_no_full_files = "I am not sharing any files that you can edit yet."

    files_no_full_files_with_repo_map = """Don't try and edit any existing code without asking me to add the files to the chat!
Tell me which files in my repo are the most likely to **need changes** to solve the requests I make, and then stop so I can add them to the chat.
Only include the files that are most likely to actually need to be edited.
Don't include files that might contain relevant context, just files that will need to be changed.
"""  # noqa: E501

    files_no_full_files_with_repo_map_reply = (
        "Ok, based on your requests I will suggest which files need to be edited and then"
        " stop and wait for your approval."
    )

    repo_content_prefix = """Here are summaries of some files present in my git repository.
Do not propose changes to these files, treat them as *read-only*.
If you need to edit any of these files, ask me to *add them to the chat* first.
"""

    read_only_files_prefix = """Here are some READ ONLY files, provided for your reference.
Do not edit these files!
"""

    trace_instructions = """
# Finding code you can't see

Before guessing which files you need, you can ask me to *trace* a symbol.
I will search the repo and reply with the places it is defined, called, read and written,
as short snippets.

To trace, put the names in a fenced block marked `trace`, one per line:

{fence[0]}trace
get_factorial
Buzzer.buzz_buzz up
{fence[1]}

Add `up` after a name for only the callers, or `down` for only what it uses.
This works for functions, classes, methods, and also for variables and attributes.

Rules for tracing:
- Trace at most 3 names at a time.
- Do NOT put a trace block in the same reply as file edits.
- After you see the trace results, ask me to *add to the chat* only the files you really need.
"""

    trace_results_prefix = """Here are the trace results you asked for.
These are short snippets, not the full files.
Use them to decide which files you need, then ask me to *add* those files to the chat.
"""

    trace_results_reply = (
        "Ok, I will use these locations to decide which files I need to see or edit."
    )

    trace_auto_prefix = """To save you a round trip, here is where the code you mentioned is
defined and used. These are short snippets, not the full files.
Ask me to *add* any file you need to see or edit.
"""

    snippets_prefix = """Here are parts of some files, provided for your reference.
These are only the parts you asked about, not the whole files.
Do not edit them. Ask me to *add the file to the chat* if you need to change one.
"""

    snippets_reply = "Ok, I will use these code snippets as references."

    trace_budget_exhausted = """I can't run any more traces for this request.
Please tell me which files you need added to the chat, based on what you have seen.
"""

    shell_cmd_prompt = ""
    shell_cmd_reminder = ""
    no_shell_cmd_prompt = ""
    no_shell_cmd_reminder = ""

    rename_with_shell = ""
    go_ahead_tip = ""

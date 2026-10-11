"""UserPromptSubmit hook — memory-consultation nudge (decision 0014 track, s18).

Prints a constant directive injected next to every user prompt: the model decides
aloud ("Memory check: yes/no") whether the answer depends on memory, and searches first
when it does. Deliberately dumb: no LLM, no FTS filter, no phrase list — the model
is the classifier (gate s18: bm25 threshold separates poorly, 31/40).
The aloud marker makes consultation greppable in transcripts (free measurement).
"""
DIRECTIVE = """MEMORY CHECK (.memory/wiki): before answering, decide whether project memory could change your answer.
If yes, FIRST run a tool; do not print the marker before it:
python .memory/tools/structural.py search "<terms>" (RU/EN word roots).
STATUS hit: use the top five results. On weak/miss, use the memory map printed below
and select a page yourself, then section/toc as needed.
After the action, make the FIRST line of the FINAL reply one of:
"Memory check: yes · 🎯 <page>" (hit or page read),
"Memory check: yes · 🗺 <page>" (selected from the map),
"Memory check: yes · ∅" (map available, topic absent from wiki),
or "Memory check: no" (not about project memory).
The icon must reflect tool evidence from THIS turn, not previous context.
For resume turns use "Memory check: yes" without an icon.

Follow the project's Language instructions and the session's selected conversation
language for user-facing output. Keep the Memory check markers exactly as written."""

if __name__ == "__main__":
    print(DIRECTIVE)
    # Persistent fallback if a worker message or desktop notification was missed.
    # Read-only: this hook never consumes or acknowledges an alert.
    from pathlib import Path
    try:
        from checkpoint_watchdog import prompt_notice
        notice = prompt_notice(Path(__file__).resolve().parents[2])
        if notice:
            print(notice)
    except Exception:
        print('CHECKPOINT SUPERVISION: pending notices could not be read. '
              'Report this to the user and inspect checkpoint_watchdog.py health; '
              'silence is not proof that a checkpoint was saved.')

<!-- The SYSTEM prompt of the summariser worker (spec §6b): the one-shot brain that reads a past
     lesson's transcript at the next launch. The per-lesson instructions and the JSON shape it
     must return are in summarise.md, sent as the user turn. A file, not a string in repl.py
     (ADR-012). -->
You summarise language lessons. Reply with one JSON object only.

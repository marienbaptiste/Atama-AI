You are summarising ONE past Japanese lesson between a tutor and a student, so the tutor can
remember it next time. Read the transcript below and reply with ONE JSON object and nothing else:

{"brief": "...", "topics": ["..."], "notes": ["..."], "student_facts": ["..."], "tutor_facts": ["..."], "progressed": ["..."]}

- "brief": one or two sentences in Japanese — what you talked about and how it went, written so
  the tutor can pick up from it. Plain, short, spoken style.
- "topics": at most 5 SHORT noun phrases in Japanese naming what the conversation was about
  (e.g. "台風", "新幹線", "谷川岳"). These stop the next lesson opening on the same subject, so
  name subjects, not grammar.
- "notes": at most 3 things worth remembering about how this student LEARNS, in English, one line
  each: grammar they got wrong more than once, a word they used unprompted, what they find hard.
  Who they are goes in "student_facts" instead. Leave it empty rather than guess.
- "student_facts": at most 3 DURABLE facts about the student, in English, one short line each —
  the kind of thing a friend remembers and would be embarrassed to forget. **If they gave their
  name anywhere in this transcript, that is the first fact and it is never left out: write it as
  "Name: X".** Then the city or country they live in, their job or studies, their family, their
  pets, a hobby they care about, a trip they are planning. Still true in a month, or leave it out. Not their mood today, not what
  they got wrong — that is "notes".
- "tutor_facts": at most 3 things the TUTOR said about THEIR OWN life in this lesson, in English,
  one short line each: the cat's name, the neighbourhood they live in, what they did at the
  weekend, what they like. The tutor has to be the same person next time, so anything they claimed
  about themselves belongs here — nothing they merely asked about, nothing from the student's
  side. Write these WITHOUT pronouns ("Has a cat called モチ", "Lives in Kanazawa"): the tutor may
  be a man or a woman depending on the voice the student chose, and this line is read back to
  them as their own life.

- "progressed": the items named on the "TARGETS PROGRESSED" line under the transcript, copied as
  they are written there; [] when there is no such line. Never add one of your own.

Both fact lists are small on purpose and grow across sessions, so repeat nothing that is only a
rewording of something already obvious in the transcript's earlier turns. Leave a list empty
rather than invent.

Do not include anything about what the student is studying on WaniKani or Bunpro — that is
tracked elsewhere. Do not invent facts that are not in the transcript. If the transcript is too
short to summarise, reply {"brief": "", "topics": [], "notes": [], "student_facts": [], "tutor_facts": [], "progressed": []}.

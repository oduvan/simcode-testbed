# simcode-testbed

An **automated testbed city** for [SimCode](https://simcode.lyabah.com). Not a
hand-played city — the controller here is pushed by Claude Code to verify platform
and game features against a **real running city**, which is the standing bar for
"done" in this project.

Why it exists: several features could only be proven at the unit level or on the
stateless engine driver, because verifying progression live required a controller
that actually plays the game. Cities without user code are driven by the builtin
demo controller, which cannot currently complete a Base level (see issue #66).

`main.py` is deliberately **ladder-agnostic**: the Base ladder is generated from the
world seed, so what a level asks for differs per city. It reads
`base.quest.required` and reacts, rather than hardcoding items — which is also the
behaviour worth exercising.

**Do not copy this into the starter templates.** The starter is intentionally a
minimal skeleton; this is the opposite.

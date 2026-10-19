"""Ask Ninaivu: questions about the family's photographs, answered here.

* :mod:`.question` reads a question in Tamil or English: what is asked, about
  whom, where and when, and what is left to search for.
* :mod:`.answer` finds the photographs it is about inside the asker's limits,
  says what can be said of them plainly, and hands the same facts to the
  on-device writing model (runtime/llm.py) to answer in words.

The route is api/api_ask_ninaivu.py; the setting is ``ask_ninaivu``.
"""

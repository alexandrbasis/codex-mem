# Assemble material fields from attributed source excerpts

The model selects evidence for each episode field and claim rationale. Code assembles the stored text from that field's exact original source excerpts and labels their source roles. This keeps unsupported model paraphrases out of these material fields.

The association between an excerpt and a field remains model-selected. Copying exact text does not prove that the selected evidence supports the field, that a statement is true, or that it remains current. An assistant report stays an assistant report. A tool record establishes only what its actual operation and result support.

New heads must normalize carried historical context from its own original references, or disclose its omission. Earlier stored versions keep their original provenance and limitations. This decision does not retroactively verify their truth or reopen independent claims.

The brief shows complete source quotations with their roles and offsets for these fields. It omits a whole field if all its quotations cannot fit. It does not repeat that text as derived prose.

Attributed excerpts can make context longer and reduce recall within a fixed budget. The comparison must measure that tradeoff. This decision does not promise better answers or establish production deployment.

The [TypeSafe pre-parsed extraction pattern](https://docs.typesafe.ai/cookbooks/pre_parsed_value_extraction_cookbook) states, "TypeSafe picks one, and code copies the pick". The application here is an inference about controlling copied text, not proof of correct selection or current validity. This change adds no TypeSafe API integration and does not activate Jev.

See [the existing consolidation decision](0001-bounded-episode-consolidation.md) and [project memory behavior](../PROJECT_MEMORY.md).

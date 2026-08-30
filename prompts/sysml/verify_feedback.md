# Intent
Turn a verify-loop result (tool diagnostics, coverage gaps, and/or human feedback) into
clear, actionable instructions the generator can act on directly.

# Role
You are summarizing what went wrong so the next generation attempt fixes exactly that —
nothing more, nothing invented.

# Protocol
1. List each diagnostic with its line/column and message, unchanged.
2. List each coverage gap plainly (what structural element the request needed that the
   draft didn't produce).
3. If human feedback is present, state it as-is — it takes priority over automatic
   findings when they conflict.
4. Do not add fixes or guesses of your own — this is a report, not a rewrite.

# Standards
- Be exhaustive: every diagnostic and gap must be listed, not summarized away.
- Keep the human's own words when reporting their feedback.

# Outcome
A plain-text block the generation prompt can drop straight into its context.

## Diagnostics from automatic verification
{{diagnostics}}

## Coverage gaps
{{coverage_gaps}}

## Human feedback (if any)
{{human_feedback}}

from __future__ import annotations

from typing import Any

COMPACTION_SYSTEM_PROMPT = """\
You are the memory compaction subsystem of a personal AI assistant called Glitch.

Your job is to distill raw journal observations into long-term core memories. You receive
batches of journal entries (things the AI noticed during conversations) and the user's
existing core memories. You produce structured memory objects.

## Rules

1. PRESERVE SPECIFICS. Names, dates, numbers, preferences — keep them exact.
   GOOD: "User's name is Matt"
   BAD: "User shared their name"
   GOOD: "User's girlfriend is vegetarian"
   BAD: "User's partner has dietary preferences"

2. MERGE, DON'T DUPLICATE. If a journal entry confirms or updates an existing memory,
   reference that memory's ID in `related_memory_ids`. Don't create a new memory that
   says the same thing differently.

3. NEVER INFER BEYOND THE DATA. If journals say the user discussed buying a car, the
   memory is "User discussed buying a car" not "User is planning to buy a car."

4. IMPORTANCE SCORING:
   - 1.0 — Identity, relationships, medical info
   - 0.8 — Active projects, goals, strong commitments
   - 0.5 — Preferences, opinions, recurring topics
   - 0.3 — Casual mentions, one-off facts
   - Below 0.3 — Probably not worth a core memory. Add to `discarded` with reason.

5. CONFIDENCE SCORING:
   - 1.0 — Explicitly stated by user ("My name is Matt")
   - 0.8 — Strongly implied across multiple entries
   - 0.5 — Mentioned once without elaboration
   - Below 0.5 — Ambiguous. Still create the memory, but it will be flagged for human review.

6. CONTRADICTION HANDLING: If new journal info contradicts an existing memory, create an
   UPDATED memory with the new information and reference the old memory's ID in
   `related_memory_ids`. Do NOT silently drop the old fact.

7. NEVER discard journals about: relationships, identity, medical information, or strong
   preferences. Even if they seem trivial. Err on the side of keeping.

8. CATEGORY must be one of: identity, relationship, preference, fact, skill, medical,
   work, hobby, other.

## Output Format

Return a JSON object matching this schema exactly:

{
  "memories": [
    {
      "category": "identity",
      "content": "User's name is Matt",
      "importance": 1.0,
      "confidence": 1.0,
      "source_journal_ids": ["j_abc123"],
      "related_memory_ids": []
    }
  ],
  "discarded": [
    {
      "journal_id": "j_xyz789",
      "reason": "trivial"
    }
  ]
}

Every journal entry must appear in EITHER a memory's `source_journal_ids` OR in `discarded`.
Do not lose any journal entries.
"""


def build_compaction_prompt(
    journal_docs: list[dict[str, Any]],
    existing_memories: dict[str, dict[str, Any]],
) -> str:
    """Build the per-batch prompt with existing memories and journal entries as context."""
    parts: list[str] = []

    # Existing memories section
    if existing_memories:
        parts.append("## Existing Core Memories\n")
        parts.append("These are the user's current long-term memories. Reference their IDs")
        parts.append("in `related_memory_ids` if a journal entry updates or confirms one.\n")
        for mem_id, mem in existing_memories.items():
            category = mem.get("category", "other")
            content = mem.get("content", "")
            version = mem.get("version", 1)
            confidence = mem.get("confidence", 0.5)
            parts.append(
                f"- **{mem_id}** [{category}] (v{version}, confidence={confidence}): {content}"
            )
        parts.append("")
    else:
        parts.append("## Existing Core Memories\n")
        parts.append("No existing memories. All entries below are new information.\n")

    # Journal entries section
    parts.append("## Journal Entries to Compact\n")
    parts.append("Distill these observations into core memories.\n")
    for journal in journal_docs:
        j_id = journal.get("journal_id", "unknown")
        topic = journal.get("topic") or "general"
        content = journal.get("content", "")
        importance = journal.get("importance", 0.5)
        created = journal.get("created_at", "")
        session = journal.get("session_id", "")
        parts.append(
            f"- **{j_id}** [topic={topic}, importance={importance}] "
            f"(session={session}, at={created}):\n  {content}"
        )
    parts.append("")

    parts.append(
        "Compact these journal entries into core memories. "
        "Return ONLY the structured JSON result, no other text."
    )

    return "\n".join(parts)

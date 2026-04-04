from __future__ import annotations

from typing import Any

COMPACTION_SYSTEM_PROMPT = """\
You are the memory compaction subsystem of a personal AI assistant called Glitch.

Your job is to distill raw journal observations into rich, contextual long-term memories.
You receive batches of journal entries (things the AI noticed during conversations, with
the surrounding conversation for context) and the user's existing core memories.

## What Makes a Good Memory

A good memory is a PARAGRAPH, not a one-liner. It synthesizes multiple data points into
a rich profile entry that gives future conversations meaningful context.

BAD (too shallow):
  "User's name is Matt"

GOOD (rich and contextual):
  "The user's name is Matt. He is a software developer who built the Glitch Core system
  from scratch. He values clean architecture, prefers Python with async patterns, and has
  a pragmatic approach to development — he'd rather ship a working solution than over-engineer.
  He has experience with distributed systems and Firebase/Firestore."

BAD (isolated fact):
  "User has a girlfriend"

GOOD (contextual relationship):
  "Matt is in a relationship with his girlfriend, who he's building a separate Glitch
  instance for. She's less technical but appreciates customization — she previously asked
  for the app theme to be 'pink and gothic'. He cares about her privacy and wants her
  to have her own isolated system."

## Rules

1. SYNTHESIZE, DON'T LIST. Combine related observations into coherent paragraphs.
   Multiple journal entries about the same topic should become ONE rich memory,
   not multiple one-line memories.

2. PRESERVE SPECIFICS. Names, dates, numbers, preferences — keep them exact.
   Include context about WHEN and WHY things were mentioned.

3. USE THE CONVERSATION CONTEXT. Each journal entry includes the surrounding messages.
   Use these to understand the full picture, not just the observation.
   The context tells you the user's tone, intent, and the situation.

4. MERGE WITH EXISTING MEMORIES. If new information enriches an existing memory,
   UPDATE that memory by referencing its ID in `related_memory_ids`. Expand the
   paragraph with the new details rather than creating a separate memory.

5. NEVER INFER BEYOND THE DATA. Stick to what was said or strongly implied.
   "User discussed buying a car" not "User is planning to buy a car."

6. IMPORTANCE SCORING:
   - 1.0 — Identity, relationships, medical info
   - 0.8 — Active projects, goals, strong commitments, professional skills
   - 0.5 — Preferences, opinions, recurring interests
   - 0.3 — Casual mentions, one-off facts
   - Below 0.3 — Probably not worth a core memory. Add to `discarded`.

7. CONFIDENCE SCORING:
   - 1.0 — Explicitly stated by user
   - 0.8 — Strongly implied across multiple entries
   - 0.5 — Mentioned once without elaboration
   - Below 0.5 — Ambiguous. Create but it may be reviewed.

8. CONTRADICTION HANDLING: If new info contradicts an existing memory, create an
   UPDATED memory with the new information and reference the old memory ID.

9. NEVER discard journals about: relationships, identity, medical information, or
   strong preferences.

10. CATEGORY must be one of: identity, relationship, preference, fact, skill, medical,
    work, hobby, other.

11. AIM FOR FEWER, RICHER MEMORIES. 3 paragraph-length memories are better than
    10 one-line facts. Group related information together.

## Output Format

Return a JSON object matching this schema exactly:

{
  "memories": [
    {
      "category": "identity",
      "content": "The user's name is Matt. He is a software developer who built the Glitch Core system from scratch, replacing a previous system called OpenClaw. He values clean architecture and pragmatic solutions over over-engineering. He prefers Python with async patterns and Pydantic for data validation.",
      "importance": 1.0,
      "confidence": 1.0,
      "source_journal_ids": ["j_abc123", "j_def456"],
      "related_memory_ids": []
    }
  ],
  "discarded": [
    {
      "journal_id": "j_xyz789",
      "reason": "trivial small talk, no new information"
    }
  ]
}

Every journal entry must appear in EITHER a memory's `source_journal_ids` OR in `discarded`.
Do not lose any journal entries.
"""

MERGE_SYSTEM_PROMPT = """\
You are the memory merging subsystem. You receive a list of core memories and identify
which ones should be combined into richer, more comprehensive entries.

Rules:
1. Group memories that are about the same TOPIC or PERSON into merge groups.
2. Memories in different categories CAN be merged if they're about the same subject.
   e.g. a "work" memory and a "skill" memory about the user's programming could merge.
3. Don't merge unrelated memories just because they're short.
4. Each merge group should produce ONE paragraph that synthesizes all the information.
5. Preserve ALL specific details from every memory in the group.

Return a JSON object:
{
  "merge_groups": [
    {
      "memory_ids": ["mem_abc", "mem_def", "mem_ghi"],
      "merged_content": "Comprehensive paragraph combining all information...",
      "category": "identity",
      "importance": 1.0,
      "confidence": 1.0
    }
  ],
  "unchanged": ["mem_xyz", "mem_123"]
}

`unchanged` lists memory IDs that should NOT be merged — they're already comprehensive
or unrelated to anything else. Every memory ID must appear in either a merge group or unchanged.
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
        parts.append("in `related_memory_ids` if a journal entry updates or enriches one.")
        parts.append("PREFER updating an existing memory over creating a new one.\n")
        for mem_id, mem in existing_memories.items():
            category = mem.get("category", "other")
            content = mem.get("content", "")
            version = mem.get("version", 1)
            confidence = mem.get("confidence", 0.5)
            parts.append(
                f"- **{mem_id}** [{category}] (v{version}, confidence={confidence}):\n  {content}"
            )
        parts.append("")
    else:
        parts.append("## Existing Core Memories\n")
        parts.append("No existing memories. All entries below are new information.\n")

    # Journal entries section — now with conversation context
    parts.append("## Journal Entries to Compact\n")
    parts.append("Each entry includes the observation AND the surrounding conversation")
    parts.append("that led to it. Use the conversation context to understand the full picture.\n")

    for journal in journal_docs:
        j_id = journal.get("journal_id", "unknown")
        topic = journal.get("topic") or "general"
        content = journal.get("content", "")
        importance = journal.get("importance", 0.5)
        created = journal.get("created_at", "")
        context = journal.get("context_messages", [])

        parts.append(
            f"### {j_id} [topic={topic}, importance={importance}] (at={created})"
        )
        parts.append(f"**Observation:** {content}")

        if context:
            parts.append("**Conversation context:**")
            for msg in context:
                parts.append(f"  {msg}")

        parts.append("")

    parts.append(
        "Compact these journal entries into rich, paragraph-length core memories. "
        "Synthesize related entries together. Update existing memories where possible. "
        "Return ONLY the structured JSON result, no other text."
    )

    return "\n".join(parts)


def build_merge_prompt(
    memories: dict[str, dict[str, Any]],
) -> str:
    """Build a prompt for the memory merging pass."""
    parts: list[str] = []

    parts.append("## Core Memories to Review for Merging\n")
    parts.append("Identify memories that should be combined into richer entries.\n")

    for mem_id, mem in memories.items():
        category = mem.get("category", "other")
        content = mem.get("content", "")
        parts.append(f"- **{mem_id}** [{category}]: {content}")

    parts.append("")
    parts.append(
        "Which memories should be merged? Return the JSON result only."
    )

    return "\n".join(parts)

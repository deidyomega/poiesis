from __future__ import annotations

import logging
from typing import Any

from glitch_core.ouroboros.sandbox import SafeFileWriter
from glitch_core.schemas import PromotionResult, ToolRegistration

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


async def generate_tool(
    coder_agent: Any,
    safe_writer: SafeFileWriter,
    db: Any,
    filename: str,
    description: str,
    user_prompt: str,
) -> PromotionResult:
    """Generate a tool via the coder agent with validation retry loop.

    1. Coder generates code
    2. SafeFileWriter validates and promotes
    3. If validation fails (fixable), feed error back to coder and retry
    4. On success, register the tool in Firestore at /tools/{tool_id}
    """
    prompt = _build_tool_prompt(filename, description, user_prompt)
    last_result = PromotionResult(success=False, error="No attempts made")

    for attempt in range(MAX_RETRIES):
        try:
            result = await coder_agent.run(prompt)
            code = result.output

            # If the coder returned structured output, extract code field
            if hasattr(code, "code"):
                code = code.code
            if not isinstance(code, str):
                code = str(code)

            # Attempt promotion via SafeFileWriter
            last_result = safe_writer.write_tool(filename, code)

            if last_result.success:
                # Register in Firestore
                tool_id = filename.replace(".py", "")
                if db is not None:
                    registration = ToolRegistration(
                        tool_id=tool_id,
                        name=tool_id.replace("_", " ").title(),
                        description=description,
                        filename=filename if filename.endswith(".py") else f"{filename}.py",
                    )
                    await db.collection("tools").document(tool_id).set(
                        registration.model_dump()
                    )
                    logger.info("Registered tool in Firestore: %s", tool_id)

                return last_result

            # If fixable, retry with error context
            fixable = [f for f in last_result.validation_failures if f.fixable]
            if not fixable:
                return last_result

            error_context = "\n".join(f"- {f.error}" for f in fixable)
            prompt = (
                f"The previous code for '{filename}' failed validation:\n"
                f"{error_context}\n\n"
                f"Please fix the code and try again. Original request: {user_prompt}"
            )
            logger.info("Tool generation retry %d/%d for %s", attempt + 1, MAX_RETRIES, filename)

        except Exception as e:
            logger.exception("Tool generation failed on attempt %d", attempt + 1)
            last_result = PromotionResult(success=False, error=str(e))

    return last_result


def _build_tool_prompt(filename: str, description: str, user_prompt: str) -> str:
    """Build the prompt for the coder agent to generate a tool."""
    return f"""\
Generate a Python tool module for Glitch Core.

Filename: {filename}
Description: {description}
User request: {user_prompt}

Requirements:
- The module must define one or more async functions that can be used as PydanticAI tools
- Each function should have type hints and a docstring
- Do NOT import os, subprocess, shutil, sys, or ctypes (these are blocked by the sandbox)
- Use httpx for HTTP requests, not urllib or requests
- Use pydantic for data validation
- The function should be self-contained -- no side effects on import

Return ONLY the Python code, no markdown fences, no explanation.
"""

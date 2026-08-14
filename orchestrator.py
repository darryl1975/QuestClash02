"""Orchestrates quiz generation across question-generation agents.

The Orchestrator owns a registry of QuestionGenerationAgent instances (one
per config.QUESTION_TYPE, see agents.py) and is the only thing that knows
how to go from a /api/generate request to a set of agent dispatches: it
reads each agent's AgentCard to find who can handle a requested question
type, splits the requested total across the selected types, and calls each
matched agent in turn, forwarding progress events the whole way.

Agents themselves don't know about each other, about request splitting, or
about the overall run — they only know how to fulfill one batch of one
question type. That separation is the point: adding a new question type
means adding a new agent with its own card, not touching this file.
"""
import config
import observability
from agents import ProgressCallback, QuestionGenerationAgent


def split_count(total: int, types: list[str]) -> dict[str, int]:
    """Divide total as evenly as possible across types; any remainder goes
    to the first N types so the totals always sum to exactly `total`."""
    if not types:
        return {}
    base = total // len(types)
    remainder = total % len(types)
    result = {}
    for i, qtype in enumerate(types):
        result[qtype] = base + (1 if i < remainder else 0)
    return result


class Orchestrator:
    """Builds and dispatches to the registry of question-generation agents.

    One agent is created per entry in config.QUESTION_TYPES at construction
    time (agents are stateless aside from their card, so a single instance
    is reused for the app's lifetime — see default_orchestrator below).
    """

    def __init__(self, question_types: list[str] | None = None):
        self._agents: dict[str, QuestionGenerationAgent] = {
            qtype: QuestionGenerationAgent(qtype) for qtype in (question_types or config.QUESTION_TYPES)
        }

    def list_agent_cards(self) -> list[dict]:
        return [agent.card.to_dict() for agent in self._agents.values()]

    def find_agent(self, question_type: str) -> QuestionGenerationAgent:
        """Match a requested question type against agent cards' skills.
        Looking this up via the card (rather than a direct dict index)
        keeps the matching logic honest about how a real multi-agent
        deployment would route work: by capability, not by identity."""
        for agent in self._agents.values():
            if agent.card.supports(question_type):
                return agent
        raise ValueError(f"No agent available for question type '{question_type}'")

    async def run_generation(
        self,
        *,
        source_text: str,
        category: str,
        model: str,
        host: str,
        selected_types: list[str],
        total_count: int,
        progress_cb: ProgressCallback,
        api_key: str = "",
    ) -> dict:
        """Top-level generation flow for one /api/generate request: truncate an
        oversized source doc, split total_count across selected_types, then
        dispatch each type to its matching agent in turn (sequentially, not in
        parallel, so seen_texts dedup and progress events stay ordered) and
        stream progress via progress_cb the whole way. Returns the full result
        dict as well, in case a caller wants it directly rather than via the
        event stream."""
        with observability.observation(
            name="quiz-generation",
            as_type="span",
            input={
                "category": category,
                "model": model,
                "selected_types": selected_types,
                "total_count": total_count,
                "agents": [self.find_agent(t).card.id for t in selected_types],
            },
        ) as root_span:
            if root_span:
                root_span.update_trace(name=f"quiz-generation:{category}")
            warnings: list[str] = []
            truncated_source = source_text
            if len(source_text) > config.MAX_SOURCE_CHARS:
                truncated_source = source_text[: config.MAX_SOURCE_CHARS]
                warnings.append(
                    f"Source text was truncated to the first {config.MAX_SOURCE_CHARS} characters "
                    f"(original was {len(source_text)} characters)."
                )

            type_distribution = split_count(total_count, selected_types)
            await progress_cb({"event": "plan", "type_distribution": type_distribution})

            seen_texts: set[str] = set()
            all_questions: list[dict] = []

            for qtype in selected_types:
                count = type_distribution[qtype]
                if count <= 0:
                    continue
                agent = self.find_agent(qtype)
                result = await agent.run(
                    count=count,
                    source_text=truncated_source,
                    category=category,
                    model=model,
                    host=host,
                    api_key=api_key,
                    seen_texts=seen_texts,
                    progress_cb=progress_cb,
                )
                all_questions.extend(result.items)
                warnings.extend(result.warnings)

            await progress_cb({
                "event": "done",
                "questions": all_questions,
                "warnings": warnings,
                "type_distribution": type_distribution,
            })
            if root_span:
                root_span.update(output={
                    "question_count": len(all_questions),
                    "warning_count": len(warnings),
                })
            return {
                "questions": all_questions,
                "warnings": warnings,
                "type_distribution": type_distribution,
                "source_char_count": len(source_text),
            }


# Agents are stateless aside from their AgentCard, so one process-wide
# instance is reused for every request rather than rebuilding the registry
# per call.
default_orchestrator = Orchestrator()

"""
AI-related functions for transcript analysis with enhanced precision and virality scoring.
"""

from pathlib import Path
from typing import List, Dict, Any, Optional, Literal
import asyncio
import logging
import re

from pydantic_ai import Agent
from pydantic import BaseModel, Field

from .config import Config
from .rate_limit import acquire_llm_slot, estimate_tokens

logger = logging.getLogger(__name__)
config = Config()


class RetryableAIError(Exception):
    """Upstream LLM overload/rate-limit. Safe for the caller to retry."""


class RequestTooLargeError(Exception):
    """The request exceeded the model's per-request / TPM token ceiling.

    This is NOT transient — retrying the same request fails identically — so it
    must fail fast with an actionable message instead of looping.
    """


def _is_request_too_large(exc: BaseException) -> bool:
    """A single request exceeded the provider's size/TPM limit (e.g. Groq 413)."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 413:
        return True
    msg = str(exc).lower()
    markers = (
        "request too large",
        "reduce your message size",
        "payload too large",
        "context length",
        "maximum context",
    )
    return any(m in msg for m in markers)


def _is_retryable_ai_error(exc: BaseException) -> bool:
    # A too-large request is never retryable regardless of how its message reads.
    if _is_request_too_large(exc):
        return False
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status in (429, 503, 502, 504):
        return True
    msg = str(exc).lower()
    markers = (
        "503", "429", "unavailable", "overloaded", "rate limit", "rate_limit", "high demand",
        # Groq/llama intermittently emit a malformed function call for complex
        # structured output; a re-roll usually produces a valid one.
        "tool_use_failed", "failed to call a function",
    )
    return any(m in msg for m in markers)


async def _run_agent_with_retry(agent: Agent, prompt: str, max_attempts: int = 5):
    """Exponential backoff around a transient upstream LLM failure.

    Every attempt first acquires a slot from the distributed rate limiter so the
    provider's per-second / per-minute thresholds are respected proactively. The
    backoff below remains only as a safety net for genuine upstream hiccups.
    """
    delay = 10.0
    last_exc: Optional[BaseException] = None
    # Reserve budget for the fixed system prompt + output schema so the rate
    # limiter's TPM accounting reflects the real request size, not just the prompt.
    est_tokens = estimate_tokens(prompt) + config.llm_request_token_overhead
    for attempt in range(1, max_attempts + 1):
        try:
            await acquire_llm_slot(est_tokens=est_tokens)
            return await agent.run(prompt)
        except Exception as e:
            last_exc = e
            if _is_request_too_large(e):
                raise RequestTooLargeError(
                    "The transcript analysis request was too large for the model's "
                    "token limit. Lower LLM_MAX_REQUEST_TOKENS "
                    f"(currently {config.llm_max_request_tokens}) so the transcript "
                    "is split into smaller chunks, or switch to a model/tier with a "
                    f"higher token-per-minute limit. Details: {e}"
                ) from e
            if not _is_retryable_ai_error(e) or attempt == max_attempts:
                raise
            # Malformed-tool-call errors are a formatting hiccup, not a rate
            # problem — re-roll quickly. Rate/overload errors need real backoff.
            msg = str(e).lower()
            is_format_error = (
                "tool_use_failed" in msg or "failed to call a function" in msg
            )
            wait = 2.0 if is_format_error else delay
            logger.warning(
                f"Upstream LLM transient error (attempt {attempt}/{max_attempts}): {e}. "
                f"Retrying in {wait:.1f}s"
            )
            await asyncio.sleep(wait)
            if not is_format_error:
                delay = min(delay * 1.5, 65.0)
    if last_exc:
        raise last_exc


class ViralityAnalysis(BaseModel):
    """Detailed virality breakdown for a segment."""

    hook_score: int = Field(
        description="How strong is the opening hook (0-25)", ge=0, le=25
    )
    engagement_score: int = Field(
        description="How engaging/entertaining is the content (0-25)", ge=0, le=25
    )
    value_score: int = Field(
        description="Educational/informational value (0-25)", ge=0, le=25
    )
    shareability_score: int = Field(
        description="Likelihood of being shared (0-25)", ge=0, le=25
    )
    total_score: int = Field(
        description="Combined virality score (0-100)", ge=0, le=100
    )
    hook_type: Optional[
        Literal["question", "statement", "statistic", "story", "contrast", "none"]
    ] = Field(
        default="none",
        description="Type of hook: question, statement, statistic, story, contrast, or none",
    )
    virality_reasoning: str = Field(description="Explanation of the virality score")


class TranscriptSegment(BaseModel):
    """Represents a relevant segment of transcript with precise timing and virality analysis."""

    start_time: str = Field(description="Start timestamp in MM:SS format")
    end_time: str = Field(description="End timestamp in MM:SS format")
    text: str = Field(
        description=(
            "Transcript text taken only from the selected timestamp range. "
            "Keep it verbatim or near-verbatim, and do not paraphrase or merge non-contiguous lines."
        )
    )
    relevance_score: float = Field(
        description="Relevance score from 0.0 to 1.0", ge=0.0, le=1.0
    )
    reasoning: str = Field(
        description=(
            "Brief factual explanation of why this exact segment works as a clip. "
            "Base it only on the provided transcript content."
        )
    )
    virality: ViralityAnalysis = Field(description="Detailed virality score breakdown")


class BRollOpportunity(BaseModel):
    """Identifies an opportunity to insert B-roll footage."""

    timestamp: str = Field(description="When to insert B-roll (MM:SS format)")
    duration: float = Field(
        description="How long to show B-roll (2-5 seconds)", ge=2.0, le=5.0
    )
    search_term: str = Field(description="Keyword to search for B-roll footage")
    context: str = Field(description="What's being discussed at this point")


class TranscriptAnalysis(BaseModel):
    """Analysis result for transcript segments with virality and B-roll opportunities."""

    most_relevant_segments: List[TranscriptSegment]
    summary: str = Field(description="Brief summary of the video content")
    key_topics: List[str] = Field(description="List of main topics discussed")
    broll_opportunities: Optional[List[BRollOpportunity]] = Field(
        default=None, description="Opportunities to insert B-roll footage"
    )


# Enhanced system prompt with virality scoring and B-roll detection
transcript_analysis_system_prompt = """You are an expert transcript analyst for short-form video editing.

Your job is extraction and ranking, not creative rewriting. You must stay fully grounded in the transcript and choose the best clip candidates that already exist in the source material.

CORE OBJECTIVES:
1. Identify segments that would be compelling on social media platforms
2. Focus on complete thoughts, insights, or entertaining moments
3. Prioritize content with hooks, emotional moments, or valuable information
4. Each segment should be engaging and worth watching
5. Score each segment's viral potential with detailed breakdown

GROUNDING RULES:
1. Use only the provided transcript lines and timestamps
2. Never invent facts, tone, context, or transitions that are not present
3. Treat this as span selection over a timestamped transcript, not open-ended summarization
4. Each selected segment must map to one contiguous range in the transcript
5. segment.text must match the chosen span closely and must not include content from outside the chosen range
6. Do not stitch together distant moments into one clip
7. If a speaker label appears, use it only if it is part of the spoken content and helps clarity

CONTENT NEUTRALITY RULES:
1. This is clipping software for legitimate editing workflows
2. Do not judge, moralize, or downgrade a segment just because the topic is controversial, sensitive, adult, political, criminal, medical, or otherwise intense
3. Evaluate segments only on clip quality: clarity, self-contained value, hook strength, emotional impact, specificity, and shareability
4. Do not refuse analysis just because the speaker describes risky, offensive, or uncomfortable subject matter
5. Only downgrade a segment when the transcript itself is weak, confusing, repetitive, unusable, or a poor standalone clip

SEGMENT SELECTION CRITERIA:
1. STRONG HOOKS: Attention-grabbing opening lines
2. VALUABLE CONTENT: Tips, insights, interesting facts, stories
3. EMOTIONAL MOMENTS: Excitement, surprise, humor, inspiration
4. COMPLETE THOUGHTS: Self-contained ideas that make sense alone
5. ENTERTAINING: Content people would want to share
6. HIGH SIGNAL: Prefer specific, concrete language over vague discussion
7. LOW FILLER: Avoid greetings, sponsor reads, repeated setup, throat-clearing, and housekeeping unless they are unusually compelling

VIRALITY SCORING (0-100 total, from four 0-25 subscores):
For each segment, provide a detailed virality breakdown:

1. HOOK STRENGTH (0-25):
   - 20-25: Immediately grabs attention (surprising fact, bold claim, intriguing question)
   - 15-19: Good opener that creates curiosity
   - 10-14: Decent start but could be stronger
   - 0-9: Weak or no hook

2. ENGAGEMENT (0-25):
   - 20-25: Highly entertaining, emotional, or dramatic
   - 15-19: Interesting and holds attention
   - 10-14: Moderately engaging
   - 0-9: Flat or boring delivery

3. VALUE (0-25):
   - 20-25: Actionable insights, unique knowledge, or transformative ideas
   - 15-19: Useful information most people don't know
   - 10-14: Somewhat informative
   - 0-9: Common knowledge or filler content

4. SHAREABILITY (0-25):
   - 20-25: "I need to send this to someone" content
   - 15-19: Content worth bookmarking
   - 10-14: Nice but not share-worthy
   - 0-9: Generic content

HOOK TYPES to identify:
- "question": Opens with a question that creates curiosity
- "statement": Bold claim or surprising statement
- "statistic": Uses compelling numbers or data
- "story": Starts with narrative/anecdote
- "contrast": Before/after or problem/solution framing
- "none": No clear hook pattern

B-ROLL OPPORTUNITIES:
Identify 2-4 moments in each segment where B-roll footage could enhance the video:
- When specific objects, places, or concepts are mentioned
- During explanations that could benefit from visual illustration
- At emotional peaks that could use supporting imagery
- Use simple, searchable keywords (e.g., "coffee shop", "laptop coding", "money stack")

TIMING GUIDELINES:
- Segments MUST be between 10-45 seconds for optimal engagement
- CRITICAL: start_time MUST be different from end_time (minimum 10 seconds apart)
- Focus on natural content boundaries rather than arbitrary time limits
- Include enough context for the segment to be understandable
- Prefer roughly 15-35 seconds when possible
- Start as late as possible while preserving the hook, and end as early as possible after the payoff

TIMESTAMP REQUIREMENTS - EXTREMELY IMPORTANT:
- Use EXACT timestamps as they appear in the transcript
- Never modify timestamp format (keep MM:SS structure)
- start_time MUST be LESS THAN end_time (start_time < end_time)
- MINIMUM segment duration: 10 seconds (end_time - start_time >= 10 seconds)
- Look at transcript ranges like [02:25 - 02:35] and use different start/end times
- NEVER use the same timestamp for both start_time and end_time
- Example: start_time: "02:25", end_time: "02:35" (NOT "02:25" and "02:25")

SCORING AND OUTPUT RULES:
- relevance_score should reflect how well the segment works as a standalone short clip, not just whether the topic is generally important
- virality_reasoning and reasoning should cite what is actually present in the chosen span
- summary and key_topics must also stay grounded in the transcript and should not add outside interpretation

Find 3-7 compelling segments that would work well as standalone clips. Quality over quantity: choose segments that are accurate, self-contained, have proper time ranges, and score high on virality metrics."""

# Lazy-loaded agent to avoid import-time failures when API keys aren't set
_transcript_agent: Optional[Agent[None, TranscriptAnalysis]] = None


def _get_missing_llm_key_error(model_name: str) -> Optional[str]:
    """Return a clear configuration error when the selected LLM key is missing."""
    provider = model_name.split(":", 1)[0].strip().lower()

    if provider in {"google", "google-gla"} and not config.google_api_key:
        return (
            "Selected LLM provider is Google, but GOOGLE_API_KEY is not set. "
            "Set GOOGLE_API_KEY or set LLM to openai:* / anthropic:* / ollama:* with the matching API key."
        )

    if provider == "openai" and not config.openai_api_key:
        return (
            "Selected LLM provider is OpenAI, but OPENAI_API_KEY is not set. "
            "Set OPENAI_API_KEY or choose another provider with a matching API key."
        )

    if provider == "anthropic" and not config.anthropic_api_key:
        return (
            "Selected LLM provider is Anthropic, but ANTHROPIC_API_KEY is not set. "
            "Set ANTHROPIC_API_KEY or choose another provider with a matching API key."
        )

    if provider == "groq" and not config.groq_api_key:
        return (
            "Selected LLM provider is Groq, but GROQ_API_KEY is not set. "
            "Set GROQ_API_KEY (free tier at https://console.groq.com/keys) "
            "or choose another provider with a matching API key."
        )

    if provider == "ollama":
        # Ollama can run locally without an API key. OLLAMA_BASE_URL/OLLAMA_API_KEY
        # are optional and passed through as environment variables.
        return None

    return None


def get_transcript_agent() -> Agent[None, TranscriptAnalysis]:
    """Get or create the transcript analysis agent (lazy initialization)."""
    global _transcript_agent
    if _transcript_agent is None:
        config_error = _get_missing_llm_key_error(config.llm)
        if config_error:
            raise RuntimeError(config_error)

        _transcript_agent = Agent[None, TranscriptAnalysis](
            model=config.llm,
            result_type=TranscriptAnalysis,
            system_prompt=transcript_analysis_system_prompt,
        )
    return _transcript_agent


def build_transcript_analysis_prompt(
    transcript: str, include_broll: bool = False
) -> str:
    """Build the grounded task prompt for transcript analysis."""
    broll_instruction = ""
    if include_broll:
        broll_instruction = (
            "\n5. Also identify B-roll opportunities for each chosen segment where stock footage could enhance the visual appeal."
        )

    return f"""Analyze this video transcript and identify the most engaging segments for short-form content.

The transcript is formatted as one line per timestamped span, for example:
[00:12 - 00:21] Spoken text here
[00:21 - 00:35] More spoken text here

Follow this workflow:
1. Read the transcript as a sequence of timestamped spans.
2. Select only contiguous ranges that already exist in the transcript.
3. Prefer moments with a strong hook, clear payoff, emotional charge, or concrete value.
4. For each chosen segment, use the earliest timestamp in the selected range as start_time and the latest timestamp in the selected range as end_time.{broll_instruction}

Critical accuracy requirements:
- Do not fabricate or embellish content.
- Do not use timestamps that are not present in the transcript.
- Do not merge separate non-contiguous moments into one segment.
- segment.text must reflect only the spoken content inside the selected time range.
- If a span lacks enough context to stand alone, expand to nearby contiguous lines rather than guessing.
- If there is a tradeoff between "viral" and "accurate", choose accuracy.
- Do not reject or penalize a segment simply because of the subject matter; stay content-neutral and assess clip quality only.

Transcript:
{transcript}"""


def _validate_segments(segments) -> List[TranscriptSegment]:
    """Filter and repair raw model segments (drop empty/too-short/invalid, fix scores)."""
    validated: List[TranscriptSegment] = []
    for segment in segments:
        # Validate text content
        if not segment.text.strip() or len(segment.text.split()) < 3:
            logger.warning(
                f"Skipping segment with insufficient content: '{segment.text[:50]}...'"
            )
            continue

        # Validate timestamps - CRITICAL: start and end must be different
        if segment.start_time == segment.end_time:
            logger.warning(
                f"Skipping segment with identical start/end times: {segment.start_time}"
            )
            continue

        # Parse timestamps to validate duration
        try:
            start_parts = segment.start_time.split(":")
            end_parts = segment.end_time.split(":")

            start_seconds = int(start_parts[0]) * 60 + int(start_parts[1])
            end_seconds = int(end_parts[0]) * 60 + int(end_parts[1])

            duration = end_seconds - start_seconds

            if duration <= 0:
                logger.warning(
                    f"Skipping segment with invalid duration: {segment.start_time} to {segment.end_time} = {duration}s"
                )
                continue

            if duration < 5:  # Minimum 5 seconds
                logger.warning(
                    f"Skipping segment too short: {duration}s (min 5s required)"
                )
                continue

            # Validate virality scores
            if segment.virality:
                # Ensure total score is sum of subscores
                calculated_total = (
                    segment.virality.hook_score
                    + segment.virality.engagement_score
                    + segment.virality.value_score
                    + segment.virality.shareability_score
                )
                if segment.virality.total_score != calculated_total:
                    logger.warning(
                        f"Correcting virality total: {segment.virality.total_score} -> {calculated_total}"
                    )
                    segment.virality.total_score = calculated_total

            validated.append(segment)
            virality_info = (
                f", virality={segment.virality.total_score}"
                if segment.virality
                else ""
            )
            logger.info(
                f"Validated segment: {segment.start_time}-{segment.end_time} ({duration}s){virality_info}"
            )

        except (ValueError, IndexError) as e:
            logger.warning(
                f"Skipping segment with invalid timestamp format: {segment.start_time}-{segment.end_time}: {e}"
            )
            continue
    return validated


def _split_transcript_into_chunks(transcript: str) -> List[str]:
    """Split the timestamped transcript into chunks that fit under the per-request
    token budget. Splits on line boundaries so each timestamped span stays intact.
    """
    max_transcript_tokens = max(
        config.llm_max_request_tokens - config.llm_request_token_overhead, 1000
    )

    if estimate_tokens(transcript) <= max_transcript_tokens:
        return [transcript]

    chunks: List[str] = []
    current: List[str] = []
    current_tokens = 0
    for line in transcript.split("\n"):
        line_tokens = estimate_tokens(line) + 1
        if current and current_tokens + line_tokens > max_transcript_tokens:
            chunks.append("\n".join(current))
            current = [line]
            current_tokens = line_tokens
        else:
            current.append(line)
            current_tokens += line_tokens
    if current:
        chunks.append("\n".join(current))
    return chunks


async def get_most_relevant_parts_by_transcript(
    transcript: str, include_broll: bool = False
) -> TranscriptAnalysis:
    """Get the most relevant parts of a transcript with virality scoring and optional B-roll detection.

    Long transcripts are analyzed in chunks so no single request exceeds the
    provider's per-request / TPM ceiling; results are merged and de-duplicated.
    """
    logger.info(
        f"Starting AI analysis of transcript ({len(transcript)} chars), include_broll={include_broll}"
    )

    try:
        agent = get_transcript_agent()
        chunks = _split_transcript_into_chunks(transcript)
        logger.info(f"Analyzing transcript in {len(chunks)} chunk(s)")

        merged_segments: List[TranscriptSegment] = []
        seen_ranges = set()
        summaries: List[str] = []
        key_topics: List[str] = []
        broll_opportunities: List[BRollOpportunity] = []

        for idx, chunk in enumerate(chunks, start=1):
            result = await _run_agent_with_retry(
                agent,
                build_transcript_analysis_prompt(
                    transcript=chunk, include_broll=include_broll
                ),
            )
            analysis = result.data
            logger.info(
                f"Chunk {idx}/{len(chunks)}: {len(analysis.most_relevant_segments)} raw segments"
            )

            for segment in _validate_segments(analysis.most_relevant_segments):
                key = (segment.start_time, segment.end_time)
                if key in seen_ranges:
                    continue
                seen_ranges.add(key)
                merged_segments.append(segment)

            if analysis.summary:
                summaries.append(analysis.summary)
            if analysis.key_topics:
                key_topics.extend(analysis.key_topics)
            if include_broll and analysis.broll_opportunities:
                broll_opportunities.extend(analysis.broll_opportunities)

        # Sort by virality score (primary) then relevance (secondary)
        merged_segments.sort(
            key=lambda x: (
                x.virality.total_score if x.virality else 0,
                x.relevance_score,
            ),
            reverse=True,
        )

        # Cap to the configured maximum so multi-chunk merges don't over-produce.
        if config.max_clips and len(merged_segments) > config.max_clips:
            merged_segments = merged_segments[: config.max_clips]

        final_analysis = TranscriptAnalysis(
            most_relevant_segments=merged_segments,
            summary=" ".join(summaries) if summaries else "",
            key_topics=list(dict.fromkeys(key_topics)),
            broll_opportunities=broll_opportunities
            if (include_broll and broll_opportunities)
            else None,
        )

        logger.info(f"Selected {len(merged_segments)} segments for processing")
        if merged_segments:
            top = merged_segments[0]
            logger.info(
                f"Top segment - relevance: {top.relevance_score:.2f}, virality: {top.virality.total_score if top.virality else 'N/A'}"
            )

        return final_analysis

    except Exception as e:
        logger.error(f"Error in transcript analysis: {e}")
        raise


def get_most_relevant_parts_sync(transcript: str) -> TranscriptAnalysis:
    """Synchronous wrapper for the async function."""
    return asyncio.run(get_most_relevant_parts_by_transcript(transcript))


class CaptionSuggestion(BaseModel):
    caption: str = Field(description="Engaging Instagram Reels caption (max 2200 chars)")
    hashtags: List[str] = Field(description="List of relevant hashtags without the # symbol, max 20")


_caption_agent: Optional[Agent[None, CaptionSuggestion]] = None


def _get_caption_agent() -> Agent[None, CaptionSuggestion]:
    global _caption_agent
    if _caption_agent is None:
        error = _get_missing_llm_key_error(config.llm)
        if error:
            raise RuntimeError(error)
        _caption_agent = Agent[None, CaptionSuggestion](
            model=config.llm,
            result_type=CaptionSuggestion,
            system_prompt=(
                "You are a social media expert who writes high-performing Instagram Reels captions. "
                "Write an engaging caption that hooks viewers in the first line, and suggest relevant hashtags. "
                "Keep captions concise (2-4 sentences). Never use emojis unless they fit naturally. "
                "Return ONLY the structured output — no extra commentary."
            ),
        )
    return _caption_agent


async def generate_instagram_caption(
    transcript_text: str,
    hook_type: Optional[str] = None,
    reasoning: Optional[str] = None,
    virality_score: Optional[int] = None,
) -> CaptionSuggestion:
    prompt = f"""Generate an Instagram Reels caption for this clip.

Clip transcript:
{transcript_text}

{"Hook type: " + hook_type if hook_type and hook_type != "none" else ""}
{"AI analysis: " + reasoning if reasoning else ""}
{"Virality score: " + str(virality_score) + "/100" if virality_score is not None else ""}

Write a caption that matches the tone and content of this clip. Suggest 10-20 relevant hashtags."""

    agent = _get_caption_agent()
    result = await _run_agent_with_retry(agent, prompt)
    return result.data

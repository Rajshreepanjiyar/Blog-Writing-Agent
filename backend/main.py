"""
AI Blog Writing Agent - Backend Entry Point
Powered by LangGraph, LangChain Groq (Llama-3.3-70B), Tavily Search, and Gemini / Pollinations AI Image Generation.
"""

from __future__ import annotations

import operator
import os
import re
import time
import urllib.parse
import requests
from datetime import date
from pathlib import Path
from typing import TypedDict, List, Optional, Literal, Annotated

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_community.tools.tavily_search import TavilySearchResults

# Load Environment Variables
load_dotenv()

# -----------------------------
# 1) Schemas
# -----------------------------
class Task(BaseModel):
    id: int
    title: str
    goal: str = Field(
        ...,
        description="One sentence describing what the reader should be able to do/understand after this section.",
    )
    bullets: List[str] = Field(
        ...,
        min_length=3,
        max_length=6,
        description="3–6 concrete, non-overlapping subpoints to cover in this section.",
    )
    target_words: int = Field(..., description="Target word count for this section (120–350).")
    requires_research: bool = False
    requires_code: bool = False


class Plan(BaseModel):
    blog_title: str
    tasks: List[Task]


class EvidenceItem(BaseModel):
    title: str
    url: str
    snippet: str


class RouterDecision(BaseModel):
    needs_research: bool
    mode: Literal["closed_book", "hybrid", "open_book"]
    queries: List[str] = Field(default_factory=list)


class EvidencePack(BaseModel):
    evidence: List[EvidenceItem] = Field(default_factory=list)


class ImageSpec(BaseModel):
    placeholder: str = Field(..., description="e.g. [[IMAGE_1]]")
    filename: str = Field(..., description="Save under images/, e.g. qkv_flow.png")
    alt: str
    caption: str
    prompt: str = Field(..., description="Prompt to send to the image model.")
    size: Literal["1024x1024", "1024x1536", "1536x1024"] = "1024x1024"
    quality: Literal["low", "medium", "high"] = "medium"


class GlobalImagePlan(BaseModel):
    md_with_placeholders: str
    images: List[ImageSpec] = Field(default_factory=list)


class State(TypedDict):
    topic: str
    mode: str
    needs_research: bool
    queries: List[str]
    evidence: List[EvidenceItem]
    plan: Optional[Plan]
    sections: Annotated[List[tuple[int, str]], operator.add]
    merged_md: str
    md_with_placeholders: str
    image_specs: List[dict]
    final: str


# -----------------------------
# 2) LLM Model Setup
# -----------------------------
llm = ChatGroq(
    model="llama-3.3-70b-versatile",
    temperature=0.2,
    max_tokens=4096
)

# -----------------------------
# 3) Router Node
# -----------------------------
ROUTER_SYSTEM = """You are a routing module for a technical blog planner.

Decide whether web research is needed BEFORE planning.

Modes:
- closed_book (needs_research=false):
  Evergreen topics where correctness does not depend on recent facts (concepts, fundamentals).
- hybrid (needs_research=true):
  Mostly evergreen but needs up-to-date examples/tools/models to be useful.
- open_book (needs_research=true):
  Mostly volatile: weekly roundups, "this week", "latest", rankings, pricing, policy/regulation.

If needs_research=true:
- Output 3–7 high-signal queries.
- Queries should be scoped and specific (avoid generic queries like just "AI" or "LLM").
- If user asked for "last week/this week/latest", reflect that constraint IN THE QUERIES.
"""

def router_node(state: State) -> dict:
    topic = state["topic"]
    decider = llm.with_structured_output(RouterDecision, method="json_mode")
    system_prompt = ROUTER_SYSTEM + "\n\nRespond strictly in JSON matching schema:\n" + str(RouterDecision.model_json_schema())
    decision = decider.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"Topic: {topic}"),
        ]
    )

    return {
        "needs_research": decision.needs_research,
        "mode": decision.mode,
        "queries": decision.queries,
    }

def route_next(state: State) -> str:
    return "research" if state["needs_research"] else "orchestrator"

# -----------------------------
# 4) Research Node (Tavily)
# -----------------------------
def _tavily_search(query: str, max_results: int = 2) -> List[dict]:
    tool = TavilySearchResults(max_results=max_results)
    results = tool.invoke({"query": query})

    normalized: List[dict] = []
    for r in results or []:
        normalized.append(
            {
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "snippet": r.get("content") or r.get("snippet") or "",
            }
        )
    return normalized

RESEARCH_SYSTEM = """You are a research synthesizer for technical writing.

Given raw web search results, produce a deduplicated list of EvidenceItem objects.

Rules:
- Only include items with a non-empty url.
- Prefer relevant + authoritative sources (company blogs, docs, reputable outlets).
- Keep snippets short.
- Deduplicate by URL.
"""

def research_node(state: State) -> dict:
    queries = (state.get("queries", []) or [])
    max_results = 2

    raw_results: List[dict] = []
    for q in queries:
        raw_results.extend(_tavily_search(q, max_results=max_results))

    if not raw_results:
        return {"evidence": []}

    extractor = llm.with_structured_output(EvidencePack, method="json_mode")
    system_prompt = RESEARCH_SYSTEM + "\n\nRespond strictly in JSON matching schema:\n" + str(EvidencePack.model_json_schema())
    pack = extractor.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"Raw results:\n{raw_results}"),
        ]
    )

    dedup = {}
    for e in pack.evidence:
        if e.url:
            dedup[e.url] = e

    return {"evidence": list(dedup.values())}

# -----------------------------
# 5) Orchestrator Node (Plan)
# -----------------------------
ORCH_SYSTEM = """You are a senior technical writer and developer advocate.

Your job is to create a structured execution plan for a beginner-friendly technical blog.
Do not write the blog itself. Only generate the plan.

Hard requirements:
- Create 3-5 sections (tasks) suitable for the topic and beginners.
- Each task must include:
  1) goal (1 sentence)
  2) 3–6 bullets that are concrete, specific, and non-overlapping
  3) target word count (200–350)

Writing Guidelines:
- Assume the reader is a beginner; use correct, simple, beginner-friendly terminology.
- Bullets must be actionable: build/compare/measure/verify/debug.
- Ensure the overall plan includes at least 2 of these somewhere:
  * minimal code sketch / MWE (set requires_code=True for that section)
  * edge cases / failure modes
  * performance/cost considerations
  * security/privacy considerations (if relevant)
  * debugging/observability tips

Output must strictly match the Plan schema.
"""

def orchestrator_node(state: State) -> dict:
    planner = llm.with_structured_output(Plan, method="json_mode")
    system_prompt = ORCH_SYSTEM + "\n\nRespond strictly in JSON matching schema:\n" + str(Plan.model_json_schema())

    evidence = state.get("evidence", [])
    mode = state.get("mode", "closed_book")

    plan = planner.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(
                content=(
                    f"Topic: {state['topic']}\n"
                    f"Mode: {mode}\n\n"
                    f"Evidence (ONLY use for fresh claims; may be empty):\n"
                    f"{[e.model_dump() for e in evidence][:16]}"
                )
            ),
        ]
    )

    return {"plan": plan}

def fanout(state: State):
    return [
        Send(
            "worker",
            {
                "task": task.model_dump(),
                "topic": state["topic"],
                "mode": state["mode"],
                "plan": state["plan"].model_dump(),
                "evidence": [e.model_dump() for e in state.get("evidence", [])],
            },
        )
        for task in state["plan"].tasks
    ]

# -----------------------------
# 6) Worker Node (Write Section)
# -----------------------------
WORKER_SYSTEM = """You are a senior technical writer and developer advocate.
Write ONE section of a technical blog post in Markdown.

Hard constraints:
- Follow the provided Goal and cover ALL Bullets in order (do not skip or merge bullets).
- Stay close to Target words (±15%).
- Output ONLY the section content in Markdown (no blog title H1, no extra commentary).
- Start with a '## <Section Title>' heading.

Grounding policy:
- If mode == open_book:
  - Do NOT introduce any specific event/company/model/funding/policy claim unless it is supported by provided Evidence URLs.
  - For each event claim, attach a source as a Markdown link: ([Source](URL)).
  - Only use URLs provided in Evidence. If not supported, write: "Not found in provided sources."

Code:
- If requires_code == true, include at least one minimal, correct code snippet relevant to the bullets.

Style:
- Short paragraphs, bullets where helpful, code fences for code.
- Avoid fluff/marketing. Be precise and implementation-oriented.
"""

def worker_node(payload: dict) -> dict:
    task = Task(**payload["task"])
    plan = Plan(**payload["plan"])
    evidence = [EvidenceItem(**e) for e in payload.get("evidence", [])]
    topic = payload["topic"]
    mode = payload.get("mode", "closed_book")

    bullets_text = "\n- " + "\n- ".join(task.bullets)

    evidence_text = ""
    if evidence:
        evidence_text = "\n".join(
            f"- {e.title} | {e.url}".strip()
            for e in evidence[:20]
        )

    def _call_llm():
        return llm.invoke(
            [
                SystemMessage(content=WORKER_SYSTEM),
                HumanMessage(
                    content=(
                        f"Blog title: {plan.blog_title}\n"
                        f"Topic: {topic}\n"
                        f"Mode: {mode}\n\n"
                        f"Section title: {task.title}\n"
                        f"Goal: {task.goal}\n"
                        f"Target words: {task.target_words}\n"
                        f"requires_research: {task.requires_research}\n"
                        f"requires_code: {task.requires_code}\n"
                        f"Bullets:{bullets_text}\n\n"
                        f"Evidence (ONLY use these URLs when citing):\n{evidence_text}\n"
                    )
                ),
            ]
        ).content.strip()

    section_md = ""
    for attempt in range(5):
        try:
            section_md = _call_llm()
            break
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                time.sleep(5 * (attempt + 1))
            else:
                raise e

    return {"sections": [(task.id, section_md)]}

# -----------------------------
# 7) Reducer & Image Nodes
# -----------------------------
def merge_content(state: State) -> dict:
    plan = state["plan"]
    assert plan is not None
    ordered_sections = [md for _, md in sorted(state["sections"], key=lambda x: x[0])]
    body = "\n\n".join(ordered_sections).strip()
    merged_md = f"# {plan.blog_title}\n\n{body}\n"
    return {"merged_md": merged_md}

DECIDE_IMAGES_SYSTEM = """You are an expert technical media editor.
Decide if visual illustrations or diagrams are needed for THIS blog post.

Rules:
- Max 2–3 images total.
- IMPORTANT FOR IMAGE PROMPTS:
  AI image models CANNOT render fine text paragraphs or table rows (they produce blurry gibberish text).
  Propose prompts for CLEAN, HIGH-QUALITY VISUAL ILLUSTRATIONS & INFOGRAPHICS (e.g., "Modern flat vector illustration of data vectors flowing into a neural network, cyan lines, dark sleek background, clean 3D render").
  DO NOT ask for text tables, detailed spreadsheets, or small code snippets inside image prompts.
- Insert placeholders exactly: [[IMAGE_1]], [[IMAGE_2]], [[IMAGE_3]].
- If no images needed: md_with_placeholders must equal input and images=[].

Return strictly GlobalImagePlan.
"""

def decide_images(state: State) -> dict:
    planner = llm.with_structured_output(GlobalImagePlan, method="json_mode")
    merged_md = state["merged_md"]
    plan = state["plan"]
    assert plan is not None
    system_prompt = DECIDE_IMAGES_SYSTEM + "\n\nRespond strictly in JSON matching schema:\n" + str(GlobalImagePlan.model_json_schema())
    blog_kind = getattr(plan, "blog_kind", "explainer")

    truncated_md = merged_md[:3500]

    def _call_planner():
        return planner.invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(
                    content=(
                        f"Blog kind: {blog_kind}\n"
                        f"Topic: {state['topic']}\n\n"
                        "Insert placeholders + propose image prompts.\n\n"
                        f"{truncated_md}"
                    )
                ),
            ]
        )

    image_plan = None
    for attempt in range(5):
        try:
            image_plan = _call_planner()
            break
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                time.sleep(5 * (attempt + 1))
            else:
                raise e

    if image_plan is None:
        return {"md_with_placeholders": merged_md, "image_specs": []}

    return {
        "md_with_placeholders": image_plan.md_with_placeholders,
        "image_specs": [img.model_dump() for img in image_plan.images],
    }

def _gemini_generate_image_bytes(prompt: str) -> bytes:
    """Generates raw image bytes using Gemini API with automatic free fallback."""
    # 1. Primary Provider: Google Gemini API
    api_key = os.environ.get("GOOGLE_API_KEY")
    if api_key:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model="gemini-2.5-flash-image",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    safety_settings=[
                        types.SafetySetting(
                            category="HARM_CATEGORY_DANGEROUS_CONTENT",
                            threshold="BLOCK_ONLY_HIGH",
                        )
                    ],
                ),
            )
            parts = getattr(resp, "parts", None)
            if not parts and getattr(resp, "candidates", None):
                parts = resp.candidates[0].content.parts
            if parts:
                for part in parts:
                    inline = getattr(part, "inline_data", None)
                    if inline and getattr(inline, "data", None):
                        return inline.data
        except Exception as e:
            print(f"[Image Gen Warning] Google API primary attempt failed ({e}). Using fallback generator...")

    # 2. Resilient Fallback: Pollinations AI with Flux Model & Prompt Formatting
    clean_prompt = f"Single technical illustration of {prompt}, modern flat vector infographic, clean layout, light background, single frame, high quality, no grid, no 4-panel, no multi-panel, no text blur"
    encoded_prompt = urllib.parse.quote(clean_prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?model=flux&width=1024&height=1024&nologo=true&seed=42"
    
    for attempt in range(3):
        try:
            res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
            if res.status_code == 200 and len(res.content) > 0:
                print(f"[Image Gen Success] Generated image via Pollinations AI fallback ({len(res.content)} bytes)")
                return res.content
        except Exception as poll_err:
            print(f"[Fallback Attempt {attempt+1}] Pollinations AI fetch warning: {poll_err}")
            time.sleep(3 * (attempt + 1))

    raise RuntimeError("All image generation providers failed.")

def generate_and_place_images(state: State) -> dict:
    plan = state["plan"]
    assert plan is not None

    md = state.get("md_with_placeholders") or state["merged_md"]
    image_specs = state.get("image_specs", []) or []

    # Save outputs under examples/ or generated_blogs/
    output_dir = Path("examples")
    output_dir.mkdir(exist_ok=True)

    if not image_specs:
        safe_title = re.sub(r'[\\/*?:"<>|]', "", plan.blog_title).strip()
        filename = output_dir / f"{safe_title}.md"
        filename.write_text(md, encoding="utf-8")
        return {"final": md}

    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    for spec in image_specs:
        placeholder = spec["placeholder"]
        clean_filename = Path(spec["filename"]).name
        out_path = images_dir / clean_filename

        if not out_path.exists():
            try:
                img_bytes = _gemini_generate_image_bytes(spec["prompt"])
                out_path.write_bytes(img_bytes)
            except Exception as e:
                prompt_block = (
                    f"> **[IMAGE GENERATION FAILED]** {spec.get('caption','')}\n>\n"
                    f"> **Alt:** {spec.get('alt','')}\n>\n"
                    f"> **Prompt:** {spec.get('prompt','')}\n>\n"
                    f"> **Error:** {e}\n"
                )
                md = md.replace(placeholder, prompt_block)
                continue

        img_md = f"![{spec['alt']}](images/{clean_filename})\n*{spec['caption']}*"
        md = md.replace(placeholder, img_md)

    safe_title = re.sub(r'[\\/*?:"<>|]', "", plan.blog_title).strip()
    filename = output_dir / f"{safe_title}.md"
    filename.write_text(md, encoding="utf-8")
    return {"final": md}

# -----------------------------
# 8) Build LangGraph State Machine
# -----------------------------
def build_agent_graph():
    reducer_graph = StateGraph(State)
    reducer_graph.add_node("merge_content", merge_content)
    reducer_graph.add_node("decide_images", decide_images)
    reducer_graph.add_node("generate_and_place_images", generate_and_place_images)
    reducer_graph.add_edge(START, "merge_content")
    reducer_graph.add_edge("merge_content", "decide_images")
    reducer_graph.add_edge("decide_images", "generate_and_place_images")
    reducer_graph.add_edge("generate_and_place_images", END)
    reducer_subgraph = reducer_graph.compile()

    g = StateGraph(State)
    g.add_node("router", router_node)
    g.add_node("research", research_node)
    g.add_node("orchestrator", orchestrator_node)
    g.add_node("worker", worker_node)
    g.add_node("reducer", reducer_subgraph)

    g.add_edge(START, "router")
    g.add_conditional_edges("router", route_next, {"research": "research", "orchestrator": "orchestrator"})
    g.add_edge("research", "orchestrator")

    g.add_conditional_edges("orchestrator", fanout, ["worker"])
    g.add_edge("worker", "reducer")
    g.add_edge("reducer", END)

    return g.compile()

app = build_agent_graph()

def generate_blog(topic: str, as_of: Optional[str] = None):
    if as_of is None:
        as_of = date.today().isoformat()

    print(f"🚀 Starting AI Blog Generation for topic: '{topic}'")
    out = app.invoke(
        {
            "topic": topic,
            "mode": "",
            "needs_research": False,
            "queries": [],
            "evidence": [],
            "plan": None,
            "as_of": as_of,
            "recency_days": 7,
            "sections": [],
            "merged_md": "",
            "md_with_placeholders": "",
            "image_specs": [],
            "final": "",
        }
    )

    final_md = out.get("final") or out.get("merged_md") or ""
    title = out["plan"].blog_title if out.get("plan") else topic
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()
    file_path = Path("examples") / f"{safe_title}.md"
    file_path.write_text(final_md, encoding="utf-8")

    print(f"\n[SUCCESS] Blog post successfully generated and saved to:\n   {file_path.resolve()}\n")
    return final_md

if __name__ == "__main__":
    import sys
    topic_input = sys.argv[1] if len(sys.argv) > 1 else "Introduction to Prompt Engineering"
    generate_blog(topic_input)

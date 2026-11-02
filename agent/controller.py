import sys
import re
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.settings import ModelSettings
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart, TextPart

from .prompt import SYSTEM_PROMPT

# ---------------------------------------------------------------
# MCP CONNECTION
# The MCP server runs as a separate subprocess (stdio transport).
# ---------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
MCP_SERVER_PATH = BASE_DIR / "mcp_server.py"

transport = StdioTransport(
    command=sys.executable,
    args=[str(MCP_SERVER_PATH)]
)

fastmcp_client = Client(transport, timeout=30.0)
mcp_toolset = MCPToolset(fastmcp_client)


# ---------------------------------------------------------------
# HUMAN-IN-THE-LOOP (security)
# The save_lead tool is hidden from the AI model completely.
# Only our code can call it, and only after the user confirms.
# This prevents the AI from saving leads (or claiming it did)
# without the user's approval.
# ---------------------------------------------------------------
def tool_filter(ctx, tool_def):
    return tool_def.name != "save_lead"


agent = Agent(
    'openai:gpt-4o',
    toolsets=[mcp_toolset.filtered(tool_filter)],
    system_prompt=SYSTEM_PROMPT,
    model_settings=ModelSettings(temperature=0.2)  # low temperature = more stable answers
)

# ---------------------------------------------------------------
# LEAD SUMMARIZER
# A small second AI agent (no tools) that reads the conversation and
# writes a SHORT service name plus natural summaries for the sales email.
# It runs only when the user confirms saving the lead.
# ---------------------------------------------------------------
class LeadSummary(BaseModel):
    service: str                      # short label, e.g. "Chatbot"
    project_summary: str              # natural 1-2 sentence description
    conversation_summary: str         # natural 2-3 sentence recap
    company: str = "N/A"
    budget: str = "Not provided"
    timeline: str = "N/A"
    existing_software: str = "N/A"
    integrations: str = "N/A"


SUMMARY_PROMPT = """
You write lead notes for a sales team from a chat between a customer and an assistant.

Return these fields:
- service: the type of solution the customer wants, in 1 to 3 words, Title Case.
  Examples: "Chatbot", "AI Agent", "Website", "Mobile App", "SaaS App", "CRM Automation".
  If it is unclear, use "General Inquiry". Never write a full sentence here.
- project_summary: 1 to 2 natural sentences in third person describing what the customer wants,
  e.g. "The customer wants a chatbot for their company to handle customer inquiries."
- conversation_summary: 2 to 3 natural sentences in third person recapping the conversation.
- company, budget, timeline, existing_software, integrations: fill ONLY if the customer clearly
  said so, otherwise use "N/A" (use "Not provided" for budget).

Rules:
- Use only facts the customer actually stated. Never invent details.
- Do NOT include the customer's name, email or phone number in any field.
- Do not copy the customer's messages word for word; write natural summaries.
"""

summary_agent = Agent(
    'openai:gpt-4o-mini',
    output_type=LeadSummary,
    system_prompt=SUMMARY_PROMPT,
    model_settings=ModelSettings(temperature=0.2),
)


async def generate_lead_summary(history: list) -> dict:
    """Ask the summarizer agent for clean lead fields. Falls back to raw text if it fails."""
    transcript = "\n".join(
        f"{'Customer' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
        for m in history
    )
    try:
        result = await summary_agent.run(transcript)
        return result.output.model_dump()
    except Exception as e:
        print(f"Summary agent failed, using fallback: {e}", file=sys.stderr)
        project_summary, conversation_summary = build_summaries(history)
        return {
            "service": "General Inquiry",
            "project_summary": project_summary,
            "conversation_summary": conversation_summary,
        }


# ---------------------------------------------------------------
# CONSTANTS AND PATTERNS
# ---------------------------------------------------------------

# Words that count as a confirmation ("yes") or a refusal ("no")
YES_WORDS = {"yes", "haan", "han", "ha", "ok", "okay", "save", "confirm", "sure", "yep", "yeah"}
NO_WORDS = {"no", "nahi", "nahin", "na", "nope", "cancel", "dont", "don't"}

# Patterns used to extract lead details from the user's messages
EMAIL_RE = re.compile(r"[\w\.-]+@[\w\.-]+\.\w+")
PHONE_RE = re.compile(r"\+?\d[\d\s-]{6,16}\d")
LABEL_RE = re.compile(r"(?i)\b(name|email|contact|phone|number|mobile)\b\s*[:\-]?")

# Phrases like "my name is" are removed before reading the name
NAME_PREFIX_RE = re.compile(r"(?i)\b(my name is|name is|i am|i'm|this is|it's|its)\b")

# If the leftover text contains any of these words, it is a sentence/correction,
# not a name (e.g. "No my email is wrong the correct one is ...")
NOT_NAME_WORDS = {
    "no", "not", "nahi", "my", "is", "the", "wrong", "correct", "one", "email", "mail",
    "contact", "phone", "number", "mobile", "sorry", "actually", "change", "changed",
    "update", "updated", "new", "please", "here", "this", "that", "and", "yes", "ok",
    "okay", "haan", "sahi", "galat", "instead", "was", "are", "use", "name",
}

# Phrases that indicate a direct question about the company.
# For these, the AI answers using the MCP tools (RAG / service info).
INFO_PHRASES = [
    "what services", "your services", "services do you", "services you",
    "price", "pricing", "how much", "cost",
    "who are you", "what do you do", "what do you offer",
    "your website", "your email", "your location", "where are you",
    "moinsystems",
]

# Phrases that indicate the user wants a project built.
# If these appear (and it is not a price question), we treat the
# message as a project request, not as an info question.
PROJECT_PHRASES = [
    "create", "build", "develop", "make me", "make a", "i want", "i need",
    "can you make", "could you make", "automate", "app", "website for", "chatbot", "agent",
]
PRICE_PHRASES = ["price", "pricing", "how much", "cost"]

# Simple greetings / small talk (handled with a friendly fixed reply, not as a project)
GREETING_WORDS = {
    "hi", "hello", "hey", "hola", "salam", "assalamualaikum", "aoa",
    "thanks", "thank", "thankyou", "bye", "goodbye",
    "morning", "afternoon", "evening",
}


# ---------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------

def last_assistant_text(history: list) -> str:
    """Return the most recent assistant message (lowercase), or empty string."""
    for msg in reversed(history):
        if msg["role"] == "assistant":
            return msg["content"].lower()
    return ""


def is_save_allowed(user_message: str, history: list) -> bool:
    """True only if we asked 'Should I save...' and the user replied with a short 'yes'."""
    asked = "should i save" in last_assistant_text(history)
    words = re.findall(r"[a-z]+", user_message.lower())
    said_yes = len(words) <= 6 and any(w in YES_WORDS for w in words)
    return asked and said_yes


def is_decline(user_message: str, history: list) -> bool:
    """True only if we asked 'Should I save...' and the user replied with a short 'no'."""
    asked = "should i save" in last_assistant_text(history)
    words = re.findall(r"[a-z']+", user_message.lower())
    said_no = len(words) <= 6 and any(w in NO_WORDS for w in words)
    return asked and said_no


def clean_name(raw: str) -> str:
    """Keep only letters, spaces, apostrophes and hyphens (removes '?', '!', digits, extra spaces)."""
    cleaned = re.sub(r"[^A-Za-z\u00C0-\u024F\u0600-\u06FF\s'\-]", " ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -'")
    return cleaned.title() if cleaned.islower() else cleaned


def extract_details(history: list, user_message: str) -> dict:
    """
    Scan all user messages (past + current) and pull out name, email and phone.
    The name is whatever text is left after removing the email, phone and labels.
    """
    texts = [m["content"] for m in history if m["role"] == "user"] + [user_message]
    details = {"name": None, "email": None, "phone": None}

    for text in texts:
        rest = text

        # Extract and remove the email first (so its digits are not read as a phone number)
        e = EMAIL_RE.search(rest)
        if e:
            details["email"] = e.group(0)
            rest = rest.replace(e.group(0), " ")

        # Extract and remove the phone number
        p = PHONE_RE.search(rest)
        if p:
            details["phone"] = p.group(0).strip()
            rest = rest.replace(p.group(0), " ")

        # Whatever short text remains is treated as the name
        if e or p:
            rest = NAME_PREFIX_RE.sub(" ", rest)   # remove "my name is" BEFORE removing labels
            rest = LABEL_RE.sub(" ", rest)
            rest = NAME_PREFIX_RE.sub(" ", rest)
            rest = clean_name(re.sub(r"[,;:\n]+", " ", rest))
            words = rest.lower().split()
            looks_like_name = (
                2 <= len(rest) <= 40
                and len(words) <= 4
                and not any(w in NOT_NAME_WORDS for w in words)
            )
            # Only update the name if the text really looks like a name.
            # Corrections like "my email is wrong, the correct one is ..." keep the old name.
            if looks_like_name:
                details["name"] = rest

    return details


def build_summaries(history: list) -> tuple:
    """
    Build the project summary and conversation summary from the user's own words.
    Messages with contact details or a plain 'yes' are excluded from the project summary.
    """
    user_texts = [m["content"] for m in history if m["role"] == "user"]
    project_texts = [
        t for t in user_texts
        if not EMAIL_RE.search(t) and not PHONE_RE.search(t)
        and not (len(t.split()) <= 3 and any(w in YES_WORDS for w in re.findall(r"[a-z]+", t.lower())))
    ]
    project_summary = " ".join(project_texts)[:500] or "N/A"
    conversation_summary = " | ".join(user_texts)[:800] or "N/A"
    return project_summary, conversation_summary


def format_history(history: list):
    """Convert our simple history list into pydantic-ai message objects."""
    messages = []
    for msg in history:
        if msg["role"] == "user":
            messages.append(ModelRequest(parts=[UserPromptPart(content=msg["content"])]))
        elif msg["role"] == "assistant":
            messages.append(ModelResponse(parts=[TextPart(content=msg["content"])]))
    return messages


def is_info_question(message: str) -> bool:
    """Detect a direct question about the company (services, price, contact, etc.)."""
    m = message.lower()
    if not any(p in m for p in INFO_PHRASES):
        return False
    # If it describes a project and is not a price question, treat it as a project request
    if any(p in m for p in PROJECT_PHRASES) and not any(p in m for p in PRICE_PHRASES):
        return False
    return True


def is_greeting(message: str) -> bool:
    """True for short greetings like 'hi', 'hello', 'salam', 'thanks'."""
    words = re.findall(r"[a-z]+", message.lower())
    return 0 < len(words) <= 4 and all(w in GREETING_WORDS or w in {"good", "there", "you", "so", "much", "assalam", "o", "alaikum"} for w in words)


def shorten(text: str, max_sentences: int = 2, max_chars: int = 260) -> str:
    """
    Make the AI answer short: remove markdown/bullets, keep a couple of sentences,
    and hard-limit the length (cut at a word boundary).
    """
    text = text.replace("\n", " ").replace("*", "")
    text = re.sub(r"\s+-\s+", ", ", text)   # turn "- item - item" lists into a comma list
    text = text.replace(":,", ":")
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!?])\s+", text)
    text = " ".join(parts[:max_sentences])
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].rstrip(",;:- ") + "..."
    return text


def ask_missing(d: dict) -> str:
    """Build a polite sentence asking only for the details that are still missing."""
    missing = []
    if not d["name"]:
        missing.append("Name")
    if not d["email"]:
        missing.append("Email")
    if not d["phone"]:
        missing.append("Contact number")
    if len(missing) == 3:
        return "could you please share your Name, Email, and Contact number?"
    if len(missing) == 2:
        return f"could you please share your {missing[0]} and {missing[1]}?"
    return f"could you please share your {missing[0]}?"


# ---------------------------------------------------------------
# MAIN CONTROLLER
# The code controls the conversation flow. The AI is only used for
# direct company questions (STEP C), never for lead capture.
# ---------------------------------------------------------------
async def agent_controller(user_message: str, history: list):
    allow_save = is_save_allowed(user_message, history)
    d = extract_details(history, user_message)
    all_present = bool(d["name"] and d["email"] and d["phone"])
    last = last_assistant_text(history)

    # STEP A: User confirmed ("yes") -> our code saves the lead directly (no AI involved)
    if allow_save and all_present:
        # AI writes a short service name and natural summaries for the sales email
        summary = await generate_lead_summary(history)
        print("DIRECT save_lead call", d, summary, file=sys.stderr)  # stderr keeps MCP stdout clean
        try:
            result = await fastmcp_client.call_tool(
                "save_lead",
                {
                    "name": d["name"],
                    "email": d["email"],
                    "contact": d["phone"],
                    **summary,  # service, project_summary, conversation_summary, company, ...
                },
            )
            text = result.content[0].text if result.content else "Lead saved."
        except Exception as e:
            text = f"Sorry, could not save your details: {e}"
        return {"response": text}

    # STEP A2: User declined ("no") -> nothing is saved
    if is_decline(user_message, history):
        return {"response": "No problem, I have not saved your details. Let me know if you change your mind."}

    # STEP B: All three details collected -> code asks for confirmation (human-in-the-loop)
    if all_present and "should i save" not in last and "your details are saved" not in last:
        return {
            "response": (
                f"I have your details (Name: {d['name']}, Email: {d['email']}, "
                f"Contact: {d['phone']}). Should I save this lead for our team?"
            )
        }

    # STEP B2: We already asked for confirmation, but the user did not answer yes/no
    if all_present and "should i save" in last and not allow_save:
        # User corrected their email/phone -> show the UPDATED details and ask again
        if EMAIL_RE.search(user_message) or PHONE_RE.search(user_message):
            return {
                "response": (
                    f"Thanks for the update. I have your details (Name: {d['name']}, "
                    f"Email: {d['email']}, Contact: {d['phone']}). Should I save this lead for our team?"
                )
            }
        return {"response": "Should I save this lead for our team? Please reply Yes or No."}

    # STEP C: Direct question about the company -> AI answers using MCP tools,
    # then code shortens the answer and asks for the missing contact details
    if is_info_question(user_message):
        formatted_history = format_history(history[-4:])  # only recent context
        brief_request = (
            user_message
            + "\n\n(Reply in at most 2 short plain sentences. No lists, no bullet points.)"
        )
        result = await agent.run(brief_request, message_history=formatted_history)
        answer = shorten(result.output)
        if not all_present:
            answer += " To connect you with our sales team, " + ask_missing(d)
        return {"response": answer}

    # STEP C2: Greeting / small talk -> friendly fixed reply (no project assumption)
    if is_greeting(user_message):
        if all_present:
            return {"response": "Hello again! Is there anything else I can help you with?"}
        return {"response": "Hello! Welcome to MoinSystems AI. How can I help you today?"}

    # STEP D: Project description or anything else -> fixed reply (no AI),
    # so the bot never writes long feature lists or asks project questions
    if not all_present:
        if d["name"] or d["email"] or d["phone"]:
            return {"response": "Thanks! To continue, " + ask_missing(d)}
        return {
            "response": (
                "That sounds like a great project! To connect you with our sales team, "
                "could you please share your Name, Email, and Contact number?"
            )
        }

    # Fallback: lead is already handled
    return {"response": "Is there anything else I can help you with?"}
import os
import json
import csv
import sys
import httpx
from pathlib import Path
from mcp.server.mcpserver import MCPServer
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from openai import OpenAI

# Import Qdrant configs from your config.py
from config import QDRANT_URL, QDRANT_API_KEY

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

mcp = MCPServer("MoinSystems-Tools")

# Initialize variables as None (Lazy Initialization)
qdrant = None
openai_client = None

LEADS_FILE = BASE_DIR / "n8n_files" / "leads.csv"
SERVICES_FILE = BASE_DIR / "data" / "moinsystems_services.json"
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")

# ============================================
# TOOL 1: RAG Search
# ============================================
@mcp.tool()
def rag_search(query: str) -> str:
    """Search the MoinSystems knowledge base. Pass the user's text question directly."""
    global qdrant, openai_client
    
    try:
        # Connect only when the tool is actually called
        if qdrant is None:
            qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
        if openai_client is None:
            openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        response = openai_client.embeddings.create(
            input=query,
            model="text-embedding-3-small"
        )
        query_vector = response.data[0].embedding

        result = qdrant.query_points(
            collection_name="moinsystems_knowledge",
            query=query_vector, 
            limit=3
        )

        docs = [point.payload.get("text", "") for point in result.points]
        
        if not docs:
            return "No relevant information found in the knowledge base."
            
        return "\n\n".join(docs)
    except Exception as e:
        return f"Error in RAG Search: {str(e)}"

# ============================================
# TOOL 2: Get Service Info
# ============================================
@mcp.tool()
def get_service_info(service_name: str) -> str:
    """Get information about a specific service or company info."""
    if not SERVICES_FILE.exists():
        return f"Services data file not found at {SERVICES_FILE}."

    try:
        with open(SERVICES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        user_query = service_name.lower()

        for item in data.values():
            title = item.get("Title", "").lower()
            intents = item.get("Intents", "").lower()

            # Match company info
            if "company" in user_query or "email" in user_query or "website" in user_query:
                if "company_info" in intents or "contact" in intents:
                    return json.dumps({
                        "title": item.get("Title"),
                        "info": item.get("Content")
                    })

            # Match services
            if user_query in title or user_query in intents.replace("_", " "):
                return json.dumps({
                    "service": item.get("Title"),
                    "description": item.get("Content"),
                    "note": "Pricing depends on project scope. The chatbot must not guarantee a fixed price."
                })

        return json.dumps({"error": "Service not found"})
    except Exception as e:
        return f"Error reading services: {str(e)}"

# ============================================
# TOOL 3: Save Lead via n8n (WITH DEBUG PRINTS)
# ============================================
@mcp.tool()
async def save_lead(
    name: str,
    email: str,
    contact: str,
    service: str = "N/A",
    project_summary: str = "N/A",
    conversation_summary: str = "N/A",
    company: str = "N/A",
    budget: str = "Not provided",
    timeline: str = "N/A",
    existing_software: str = "N/A",
    integrations: str = "N/A",
) -> str:
    """Save a new lead after the user confirmed. Fill service, project_summary and
    conversation_summary from what the user said earlier. Do not invent information."""

    print(f"TOOL CALLED: save_lead name={name} email={email}", file=sys.stderr)

    if not N8N_WEBHOOK_URL:
        return "ERROR: N8N_WEBHOOK_URL not configured in .env"

    payload = {
        "name": name,
        "email": email,
        "contact": contact,
        "service": service,
        "project_summary": project_summary,
        "project_details": project_summary,
        "conversation_summary": conversation_summary,
        "company": company,
        "budget": budget,
        "timeline": timeline,
        "existing_software": existing_software,
        "integrations": integrations,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(N8N_WEBHOOK_URL, json=payload)

        print(f"n8n status: {response.status_code}", file=sys.stderr)

        if response.status_code in (200, 201):
            return f"Thank you! Your details are saved ({name}, {contact}, {email}). Our sales team will contact you shortly."
        return f"Sorry, could not save your details (error {response.status_code})."

    except Exception as e:
        print(f"EXCEPTION: {e}", file=sys.stderr)
        return f"Sorry, could not save your details: {e}"
# ============================================
# TOOL 4: List Saved Leads (CSV file read)
# ============================================
@mcp.tool()
def list_saved_lead(limit: int = 5) -> str:
    """Return the most recent leads saved on the laptop from the CSV file."""
    if not LEADS_FILE.exists():
        return f"No leads file found at {LEADS_FILE}."

    try:
        with open(LEADS_FILE, mode='r', encoding='utf-8') as file:
            reader = csv.DictReader(file)
            all_leads = list(reader)

        if not all_leads:
            return "No leads saved yet in the CSV file."

        recent = all_leads[-limit:]
        lines = []

        for lead in recent:
            name = lead.get('name', lead.get('Name', 'Unknown'))
            email = lead.get('email', lead.get('Email', 'Unknown'))
            contact = lead.get('contact', lead.get('Contact', 'Unknown'))
            details = lead.get('project_details', lead.get('Details', ''))[:30]
            lines.append(f"- {name} | {email} | {contact} | {details}...")

        return f"Last {len(recent)} leads:\n" + "\n".join(lines)
    except Exception as e:
        return f"❌ Error reading leads CSV: {str(e)}"
# ============================================
# RESOURCE: Services data (read-only content)
# ============================================
@mcp.resource("moinsystems://services")
def services_resource() -> str:
    """The full MoinSystems services data as read-only text."""
    if not SERVICES_FILE.exists():
        return "Services data file not found."
    return SERVICES_FILE.read_text(encoding="utf-8")


# ============================================
# PROMPT: Reusable template for lead summaries
# ============================================
@mcp.prompt()
def lead_summary_prompt(conversation: str) -> str:
    """A reusable prompt that turns a chat into a short sales-team lead note."""
    return (
        "You write lead notes for a sales team. Read the conversation below and return:\n"
        "- service: 1 to 3 words (e.g. Chatbot)\n"
        "- project_summary: 1 to 2 natural sentences in third person\n"
        "- conversation_summary: 2 to 3 natural sentences in third person\n"
        "Use only facts the customer stated. Do not include name, email or phone.\n\n"
        f"Conversation:\n{conversation}"
    )
if __name__ == "__main__":
    mcp.run(transport="stdio")
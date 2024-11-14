from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Dict, Optional, Union, List
import os
import json
import re
from openai import OpenAI

# Initialize OpenAI client with API key and error handling
if not os.getenv("OPENAI_API_KEY"):
    raise ValueError("OPENAI_API_KEY environment variable is not set")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Initialize FastAPI app
app = FastAPI()

# Set up static and template directories
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

class LeadRequest(BaseModel):
    inquiry: str

class BudgetRange(BaseModel):
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    single_value: Optional[float] = None

class LeadData(BaseModel):
    budget: Optional[Union[BudgetRange, float]] = None
    locations: Optional[List[str]] = None
    property_type: Optional[str] = None
    additional_requirements: Optional[str] = None

# Store lead data per session (in production, use a proper database)
lead_sessions: Dict[str, LeadData] = {"current": LeadData()}

def parse_budget(budget_text: str) -> Optional[BudgetRange]:
    if not budget_text:
        return None
        
    # Remove currency symbols and convert to lowercase
    budget_text = budget_text.lower().replace('$', '').replace(',', '')
    
    # Try to find a range pattern
    range_patterns = [
        r'between\s+(\d+\.?\d*k?)\s+and\s+(\d+\.?\d*k?)',
        r'(\d+\.?\d*k?)\s*-\s*(\d+\.?\d*k?)',
        r'(\d+\.?\d*k?)\s+to\s+(\d+\.?\d*k?)'
    ]
    
    for pattern in range_patterns:
        match = re.search(pattern, budget_text)
        if match:
            min_val, max_val = match.groups()
            # Convert k notation to full numbers
            min_val = float(min_val.replace('k', '')) * 1000 if 'k' in min_val else float(min_val)
            max_val = float(max_val.replace('k', '')) * 1000 if 'k' in max_val else float(max_val)
            return BudgetRange(min_value=min_val, max_value=max_val)
    
    # Try to find a single number
    single_number = re.search(r'(\d+\.?\d*k?)', budget_text)
    if single_number:
        value = single_number.group(1)
        value = float(value.replace('k', '')) * 1000 if 'k' in value else float(value)
        return BudgetRange(single_value=value)
    
    return None

def parse_locations(location_text: str) -> Optional[List[str]]:
    if not location_text:
        return None
        
    # Split on common separators and clean up
    separators = [' or ', ',', ';', '/']
    locations = [location_text]
    for separator in separators:
        locations = [loc for part in locations for loc in part.split(separator)]
    
    # Clean up each location
    locations = [loc.strip() for loc in locations if loc.strip()]
    return locations if locations else None

# System prompts
CONVERSATION_PROMPT = """
You are an AI lead assistant helping to gather information for real estate inquiries.
Your goal is to collect the following information naturally through conversation:
- Budget (exact amount or range)
- Preferred location(s)
- Property type (house, apartment, etc.)
- Any additional requirements

If any information is missing, ask for it politely. If you have all information, acknowledge 
and mention that you'll connect them with a real estate agent.

Keep responses professional, concise, and focused on gathering necessary information.
"""

CLASSIFICATION_PROMPT = """
Extract the following information from the conversation:
- budget: Include full number range if given (e.g., "between 80000 and 95000" or "80000-95000")
- location: Include all locations mentioned, separated by commas
- property_type: type of property requested
- additional_requirements: any special requests or requirements

Return a JSON object with these fields. Use null for missing information.
Example:
{
    "budget": "between 80000 and 95000",
    "location": "Miami, Fort Lauderdale",
    "property_type": "apartment",
    "additional_requirements": "needs parking space"
}
"""

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/start_conversation")
async def start_conversation(lead: LeadRequest):
    try:
        # Get current lead data
        current_data = lead_sessions["current"]
        
        # Get conversational response first
        conversation_messages = [
            {"role": "system", "content": CONVERSATION_PROMPT},
            {"role": "user", "content": lead.inquiry}
        ]
        
        conversation_response = client.chat.completions.create(
            model="gpt-4",
            messages=conversation_messages,
            temperature=0.7
        )
        
        user_response = conversation_response.choices[0].message.content

        # Extract new information
        classification_messages = [
            {"role": "system", "content": CLASSIFICATION_PROMPT},
            {"role": "user", "content": lead.inquiry}
        ]
        
        classification_response = client.chat.completions.create(
            model="gpt-4",
            messages=classification_messages,
            temperature=0.3
        )

        try:
            new_data = json.loads(classification_response.choices[0].message.content)
            
            # Update budget if new information is provided
            if new_data.get("budget"):
                parsed_budget = parse_budget(new_data["budget"])
                if parsed_budget:
                    current_data.budget = parsed_budget
                    
            # Update locations if new information is provided
            if new_data.get("location"):
                parsed_locations = parse_locations(new_data["location"])
                if parsed_locations:
                    # Merge with existing locations if any
                    existing_locations = current_data.locations or []
                    combined_locations = list(set(existing_locations + parsed_locations))
                    current_data.locations = combined_locations
                    
            # Update property type if not already set
            if new_data.get("property_type") and not current_data.property_type:
                current_data.property_type = new_data["property_type"]
                
            # Update or append additional requirements
            if new_data.get("additional_requirements"):
                existing_reqs = current_data.additional_requirements or ""
                new_reqs = new_data["additional_requirements"]
                if existing_reqs:
                    current_data.additional_requirements = f"{existing_reqs}; {new_reqs}"
                else:
                    current_data.additional_requirements = new_reqs

            # Store updated data
            lead_sessions["current"] = current_data
            
            return {
                "response": user_response,
                "classification": {
                    "budget": current_data.budget.dict() if current_data.budget else None,
                    "locations": current_data.locations,
                    "property_type": current_data.property_type,
                    "additional_requirements": current_data.additional_requirements
                }
            }
        except (json.JSONDecodeError, ValueError) as err:
            print(f"Classification error: {err}")
            print(f"Raw response: {classification_response.choices[0].message.content}")
            return {"response": user_response, "classification": None}
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing request: {str(e)}")

@app.get("/api/view_classification")
async def view_classification():
    return {"lead_classification": lead_sessions.get("current")}

@app.get("/api/handoff_status")
async def handoff_status():
    classification = lead_sessions.get("current", LeadData())
    
    # Check if essential fields are filled
    ready_for_handoff = all([
        classification.budget is not None,
        classification.locations is not None,
        classification.property_type is not None
    ])
    
    status = "Ready for handoff" if ready_for_handoff else "Missing required information"
    missing_fields = []
    
    if not classification.budget:
        missing_fields.append("budget")
    if not classification.locations:
        missing_fields.append("location")
    if not classification.property_type:
        missing_fields.append("property_type")
        
    return {
        "status": status,
        "ready": ready_for_handoff,
        "missing_fields": missing_fields
    }
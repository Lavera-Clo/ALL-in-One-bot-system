import logging
import json
import base64
from openai import AsyncOpenAI
from config import settings

logger = logging.getLogger("uvicorn.error")


async def analyze_bank_receipt(image_bytes: bytes) -> dict:
    """
    Analyzes a bank transfer receipt image using OpenAI GPT-4o Vision API.
    Extracts: amount, reference number, sender name, date, bank name.
    Returns a structured dict with extracted data.
    """
    api_key = settings.OPENAI_API_KEY.strip() if settings.OPENAI_API_KEY else ""

    # Fallback if no valid API key
    if not api_key or "placeholder" in api_key or api_key == "your_openai_api_key_here":
        logger.warning("No valid OpenAI API key for receipt analysis. Marking for manual review.")
        return {
            "success": False,
            "amount": None,
            "reference_number": None,
            "sender_name": None,
            "date": None,
            "bank_name": None,
            "summary": "Receipt uploaded — awaiting manual review (no AI key configured).",
            "confidence": "none"
        }

    try:
        client = AsyncOpenAI(api_key=api_key)

        # Convert image bytes to base64 for the Vision API
        base64_image = base64.b64encode(image_bytes).decode("utf-8")

        prompt = """
You are analyzing a bank transfer receipt or payment screenshot. 
Extract the following information and return it as a JSON object:

{
    "amount": <number or null>,
    "reference_number": "<string or null>",
    "sender_name": "<string or null>",
    "date": "<string or null>",
    "bank_name": "<string or null>",
    "summary": "<brief one-line summary of the payment>",
    "confidence": "high" | "medium" | "low"
}

Rules:
- "amount" must be a number (no currency symbols). If you see "Rs. 12,500.00", return 12500.00
- If you can't read a field clearly, set it to null
- "confidence" reflects how clearly you can read the receipt
- Look for transaction IDs, reference numbers, UTR numbers, or any unique identifier
- The receipt may be in English, Sinhala, or Tamil
- Return ONLY the JSON object, no other text
"""

        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}",
                                "detail": "high"
                            }
                        }
                    ]
                }
            ],
            max_tokens=500,
            temperature=0.1
        )

        reply = response.choices[0].message.content.strip()

        # Clean markdown code fences if present
        if reply.startswith("```"):
            reply = reply.split("\n", 1)[1] if "\n" in reply else reply[3:]
            if reply.endswith("```"):
                reply = reply[:-3]
            reply = reply.strip()

        result = json.loads(reply)
        result["success"] = True
        logger.info(f"Receipt analysis result: {result}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse receipt analysis JSON: {e}")
        return {
            "success": False,
            "amount": None,
            "reference_number": None,
            "sender_name": None,
            "date": None,
            "bank_name": None,
            "summary": "Receipt uploaded but AI could not parse it. Manual review required.",
            "confidence": "none"
        }
    except Exception as e:
        logger.error(f"Error analyzing receipt with GPT-4o Vision: {e}")
        return {
            "success": False,
            "amount": None,
            "reference_number": None,
            "sender_name": None,
            "date": None,
            "bank_name": None,
            "summary": f"Receipt analysis failed: {str(e)}",
            "confidence": "none"
        }

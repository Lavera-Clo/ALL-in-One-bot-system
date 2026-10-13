"""
Reads a customer's bank-transfer receipt (photo or screenshot) with AI vision.

The result is ADVISORY: the order moves to 'payment_uploaded' and the manager must still reply
CONFIRM — nothing here approves a payment by itself.

Two different vision models read every receipt independently. If they disagree on the amount,
confidence is forced to 'low' and the summary says so, so the manager looks closely. A misread
amount is exactly the mistake a manager glancing at a phone would otherwise miss.
"""

import logging
from typing import Optional

import llm_router as llm

logger = logging.getLogger("uvicorn.error")

RECEIPT_PROMPT = """You are analyzing a bank transfer receipt or payment screenshot.
Return ONLY a JSON object:

{
    "amount": <number or null>,
    "reference_number": "<string or null>",
    "sender_name": "<string or null>",
    "date": "<string or null>",
    "bank_name": "<string or null>",
    "summary": "<brief one-line summary of the payment>",
    "confidence": "high" | "medium" | "low",
    "is_receipt": true | false
}

Rules:
- "amount" is the TRANSFERRED amount as a number, no currency symbols ("Rs. 12,500.00" -> 12500.00).
  Not an account balance, not a fee.
- If you can't read a field clearly, set it to null. Never guess digits.
- "confidence" reflects how clearly you can read the receipt.
- Look for transaction IDs, reference numbers, UTR numbers or any unique identifier.
- The receipt may be in English, Sinhala or Tamil.
- "is_receipt" is false if the image is not a payment receipt at all (a selfie, a product photo...)."""


def _failed(summary: str) -> dict:
    return {
        "success": False,
        "amount": None,
        "reference_number": None,
        "sender_name": None,
        "date": None,
        "bank_name": None,
        "summary": summary,
        "confidence": "none",
    }


def _as_amount(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(str(value).replace(",", "").replace("Rs.", "").replace("Rs", "").strip()), 2)
    except ValueError:
        return None


async def analyze_bank_receipt(image_bytes: bytes) -> dict:
    """
    Extract amount, reference number, sender, date and bank from a receipt image.
    Always returns the same dict shape; on failure `success` is False and the summary says why.
    """
    if not image_bytes:
        return _failed("Receipt uploaded but the image was empty. Manual review required.")
    if not llm.role_candidates("vision"):
        logger.warning("No vision-capable AI provider configured for receipt analysis.")
        return _failed("Receipt uploaded — awaiting manual review (no AI vision provider configured).")

    messages = llm.image_message(RECEIPT_PROMPT, image_bytes)
    try:
        first, first_by = await llm.complete_json("vision", messages, max_tokens=800, temperature=0)
    except llm.LLMUnavailable as e:
        logger.error(f"Receipt analysis unavailable: {e}")
        return _failed("Receipt uploaded but no AI model could read it right now. Manual review required.")

    if not isinstance(first, dict):
        return _failed("Receipt uploaded but AI could not parse it. Manual review required.")

    result = dict(first)
    result["amount"] = _as_amount(first.get("amount"))
    result["success"] = True
    result["read_by"] = [first_by.label]

    if first.get("is_receipt") is False:
        result["confidence"] = "low"
        result["summary"] = "This image does not look like a payment receipt. " + (first.get("summary") or "")
        return result

    # Second, independent reading: a different company's model if one is free, otherwise at
    # least a different model from the same company.
    try:
        try:
            second, second_by = await llm.complete_json(
                "vision", messages, max_tokens=800, temperature=0, exclude_providers={first_by.provider})
        except llm.LLMUnavailable:
            second, second_by = await llm.complete_json(
                "vision", messages, max_tokens=800, temperature=0, exclude_models={first_by.label})
        result["read_by"].append(second_by.label)
        second_amount = _as_amount(second.get("amount")) if isinstance(second, dict) else None
        if result["amount"] is not None and second_amount is not None and abs(result["amount"] - second_amount) >= 1:
            result["confidence"] = "low"
            result["amount_alternatives"] = [result["amount"], second_amount]
            result["summary"] = (f"⚠️ Two AI readers disagree on the amount ({result['amount']:,.2f} vs "
                                 f"{second_amount:,.2f}) — check the receipt. " + (result.get("summary") or ""))
        elif result["amount"] is None and second_amount is not None:
            result["amount"] = second_amount
            result["confidence"] = "low"
        else:
            result["amount_confirmed_by_two_readers"] = result["amount"] is not None
        if not result.get("reference_number") and isinstance(second, dict):
            result["reference_number"] = second.get("reference_number")
    except llm.LLMUnavailable:
        result["amount_confirmed_by_two_readers"] = False
        if result.get("confidence") == "high":
            result["confidence"] = "medium"     # a single reading is never 'high'

    logger.info(f"Receipt analysis: amount={result['amount']} confidence={result.get('confidence')} by {result['read_by']}")
    return result

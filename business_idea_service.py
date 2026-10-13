import os
import io
import uuid
from fpdf import FPDF
import llm_router
from config import settings

async def generate_business_ideas(stock_details: str) -> str:
    """
    Generate business ideas from a stock/inventory description, through llm_router
    (Groq / Gemini / OpenRouter...), so it works without an OpenAI key.
    """
    if not llm_router.role_candidates("judge"):
        return ("No AI provider is configured, so ideas can't be generated yet.\n\n"
                "Add GEMINI_API_KEY, GROQ_API_KEY or OPENROUTER_API_KEY to the .env file.")

    prompt = f"""You are an expert business consultant in Sri Lanka. A client has this stock/inventory:

{stock_details[:12000]}

Generate 3 to 5 creative, practical business ideas or strategies to make money from THIS stock.
For each idea give:
- a clear title
- who the customers are
- how to start this month, in 3-4 concrete steps
- the main risk to watch

Write plain text only - no markdown, no asterisks, no tables (it is printed to a PDF).
Do not invent prices or market statistics you don't know; describe how to check them instead."""

    try:
        result = await llm_router.complete("judge", [
            {"role": "system", "content": "You are a practical, honest small-business consultant."},
            {"role": "user", "content": prompt},
        ], max_tokens=3000, temperature=0.7)
        return result.text.strip()
    except llm_router.LLMUnavailable as e:
        return f"Error generating business ideas: {e}"


def create_pdf_from_text(text: str) -> bytes:
    """
    Generates a PDF file containing the provided text and returns it as bytes.
    """
    class PDF(FPDF):
        def header(self):
            self.set_font("helvetica", "B", 15)
            self.cell(0, 10, "Business Ideas & Monetization Strategy", border=0, align="C", new_x="LMARGIN", new_y="NEXT")
            self.ln(10)
            
        def footer(self):
            self.set_y(-15)
            self.set_font("helvetica", "I", 8)
            self.cell(0, 10, f"Page {self.page_no()}", align="C")
            
    pdf = PDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=11)
    
    # We replace some common markdown characters just in case
    clean_text = text.replace('**', '').replace('*', '-')
    
    # Use multi_cell for line wrapping
    pdf.multi_cell(0, 8, txt=clean_text)
    
    # output(dest='S') returns a byte string in fpdf2
    return pdf.output()

import os
import io
import uuid
from fpdf import FPDF
from openai import AsyncOpenAI
from config import settings

async def generate_business_ideas(stock_details: str) -> str:
    """
    Calls OpenAI to generate business ideas based on provided stock details.
    """
    api_key = settings.OPENAI_API_KEY.strip() if settings.OPENAI_API_KEY else ""
    if not api_key or "placeholder" in api_key or api_key == "your_openai_api_key_here":
         return (
             "Mock Business Ideas (No OpenAI API Key found):\n\n"
             "1. Furniture Rental Service\n"
             "   Rent out your wooden chairs and round tables for local weddings and events.\n\n"
             "2. Antique Restoration & Resale\n"
             "   Refurbish the vintage chandeliers and sell them to luxury interior designers.\n\n"
             "3. Pop-up Event Venue\n"
             "   Use the tables and chairs to host a temporary pop-up cafe or art exhibit.\n\n"
             "(Please add your OPENAI_API_KEY to the .env file to generate real ideas based on your stock.)"
         )
    
    client = AsyncOpenAI(api_key=api_key)
    
    prompt = f"""
You are an expert business consultant. A client has provided the following stock/inventory details:

{stock_details}

Based on this inventory, generate 3 to 5 creative and actionable business ideas or strategies to monetize this stock. 
For each idea, provide a clear title and a brief description of how to execute it.
Please format the response in clean plain text (avoid markdown bolding/asterisks if possible, to make it easier for a PDF generator).
"""
    
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a helpful business consultant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=800
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Error generating business ideas: {str(e)}"

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

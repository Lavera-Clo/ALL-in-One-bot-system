import asyncio
import sys
from ai_service import generate_ai_response

# Force UTF-8 encoding for Windows console output
sys.stdout.reconfigure(encoding='utf-8')

async def test_pdf_rules():
    knowledge_base = "Wasala Nature Resort Banquet & Events Master Knowledge Base"
    system_prompt = "You are Banquet AI Assistant"

    test_queries = [
        # Price List.pdf triggers
        ("English - Price query", "Hi, can you send me the wedding package price list?"),
        ("Singlish - Price query", "Macho wedding package prices kohomada? Price list ekak ewannako"),
        ("Sinhala - Price query", "මංගල පැකේජ මිල ගණන් ලැයිස්තුව එවන්න"),

        # Wasala Banquets.pdf triggers
        ("English - Menu & Policy query", "What food menu items and complimentary benefits are included?"),
        ("Singlish - Menu & Policy query", "Menu items monawada thiyenne? Wasala banquets details ewanna"),
        ("Sinhala - Menu & Policy query", "කෑම මෙන්නු එක සහ කොන්දේසි මොනවාද?"),

        # REGI 2026.pdf triggers
        ("English - Registration query", "What are your civil registration package prices for 2026?"),
        ("Singlish - Registration query", "Registration package prices kohomada? REGI 2026 details ewanna"),
        ("Sinhala - Registration query", "2026 රෙජිස්ට්‍රේෂන් පැකේජ් ගණන් කීයක්ද?")
    ]

    for label, query in test_queries:
        print(f"\n--- TESTING: {label} ---")
        print(f"User Question: '{query}'")
        reply, is_handoff = await generate_ai_response(system_prompt, knowledge_base, [], query)
        print(f"AI Response:\n{reply}\n")

if __name__ == "__main__":
    asyncio.run(test_pdf_rules())
